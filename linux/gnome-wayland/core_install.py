"""Stage, verify, commit, and roll back the per-user agent deployment.

Called by install.sh with the exact selected interpreter. No privileged writes.
Failed and previous artifacts are retained on the same filesystem as each target.
"""

import argparse
import ast
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time


SERVICE = 'sleep-disabler-gnome.service'
READINESS_ATTEMPTS = 10
READINESS_DELAY = 1
READINESS_TIMEOUT = 70
STATE_SCRIPT = '''# sleep-disabler-install-state
import json
import dbus
bus = dbus.SessionBus()
owner = bus.get_name_owner('org.sleepdisabler.App')
app = dbus.Interface(bus.get_object(owner, '/org/sleepdisabler/App', introspect=False),
                     'org.sleepdisabler.App1')
state = app.GetState(timeout=3)
agent_version = state.get('agentRuntimeVersion', '')
api = state.get('apiVersion', 0)
panel_version = state.get('panelRuntimeVersion', '')
registered = state.get('panelRuntimeRegistered', False)
if (not isinstance(agent_version, str) or not isinstance(panel_version, str) or
        not isinstance(api, int) or isinstance(api, (bool, dbus.Boolean)) or
        not isinstance(registered, (bool, dbus.Boolean))):
    raise ValueError('Malformed runtime state types')
print(json.dumps({'agentRuntimeVersion': str(agent_version), 'apiVersion': int(api),
                  'panelRuntimeVersion': str(panel_version),
                  'panelRuntimeRegistered': bool(registered)}))
'''


def run(command, *, timeout=40):
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout,
                          check=False)


def systemctl(*arguments, timeout=40):
    return run(['systemctl', '--user', *arguments], timeout=timeout)


def state(python, *, timeout=6):
    try:
        result = run([python, '-c', STATE_SCRIPT], timeout=timeout)
        if result.returncode:
            return None
        values = json.loads(result.stdout)
        if (not isinstance(values, dict) or
                not isinstance(values.get('agentRuntimeVersion'), str) or
                type(values.get('apiVersion')) is not int or
                not isinstance(values.get('panelRuntimeVersion'), str) or
                type(values.get('panelRuntimeRegistered')) is not bool):
            return None
        return values
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def ready(python, version, api, panel=None):
    deadline = time.monotonic() + READINESS_TIMEOUT
    for attempt in range(READINESS_ATTEMPTS):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        try:
            active = systemctl('is-active', '--quiet', SERVICE,
                               timeout=min(3, remaining)).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            active = False
        remaining = deadline - time.monotonic()
        if active and remaining > 0:
            current = state(python, timeout=min(6, remaining))
            if (current and current['agentRuntimeVersion'] == version and current['apiVersion'] == api
                    and (panel is None or (current['panelRuntimeRegistered'] is True
                                           and current['panelRuntimeVersion'] == panel))):
                return True
        if attempt + 1 < READINESS_ATTEMPTS:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            time.sleep(min(READINESS_DELAY, remaining))
    return False


def source_versions(path):
    values = {}
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id in ('AGENT_RUNTIME_VERSION', 'API_VERSION'):
                    values[target.id] = ast.literal_eval(node.value)
    version, api = values.get('AGENT_RUNTIME_VERSION'), values.get('API_VERSION')
    if not isinstance(version, str) or not version or type(api) is not int or api < 1:
        raise ValueError('agent runtime/API constants are invalid')
    return version, api


def launcher(python, module):
    return '#!/bin/sh\nexec ' + shlex.quote(python) + ' ' + shlex.quote(str(module)) + ' "$@"\n'


def unit_escape(path):
    text = str(path)
    if '\n' in text or '\r' in text:
        raise ValueError('unit executable path contains a newline')
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def config_home():
    path = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config')
    if not path.is_absolute():
        raise ValueError('XDG_CONFIG_HOME must be an absolute path')
    return path


def prior_interpreter(program, unit=None):
    """Read only the narrowly defined launcher we generate; never evaluate it."""
    try:
        lines = (program / 'agent-launcher').read_text().splitlines()
        tokens = shlex.split(lines[1]) if len(lines) == 2 and lines[0] == '#!/bin/sh' else []
        if (len(tokens) == 4 and tokens[0] == 'exec' and tokens[3] == '$@'
                and tokens[2] == str(program / 'agent.py')
                and Path(tokens[1]).is_absolute() and os.access(tokens[1], os.X_OK)):
            return tokens[1]
    except (OSError, ValueError):
        pass
    # The plan-0.4 unit used a direct Python ExecStart before launchers existed.
    if unit is not None:
        try:
            commands = [line[len('ExecStart='):] for line in unit.read_text().splitlines()
                        if line.startswith('ExecStart=')]
            tokens = shlex.split(commands[0]) if len(commands) == 1 else []
            modules = (str(program / 'agent.py'), '%h/.local/lib/sleep-disabler-gnome/agent.py')
            if (len(tokens) == 2 and tokens[1] in modules and Path(tokens[0]).is_absolute()
                    and '%' not in tokens[0] and '$' not in tokens[0]
                    and os.access(tokens[0], os.X_OK)):
                return tokens[0]
        except (OSError, ValueError):
            pass
    return None


def service_snapshot(unit, artifacts=()):
    enabled = systemctl('is-enabled', SERVICE)
    active = systemctl('is-active', SERVICE)
    enable_text, active_text = enabled.stdout.strip(), active.stdout.strip()
    if enable_text == 'enabled' and enabled.returncode == 0:
        was_enabled = True
    elif enable_text == 'disabled' and enabled.returncode == 1:
        was_enabled = False
    elif (enable_text == 'not-found' and enabled.returncode in (1, 4)
          and not any(path.exists() or path.is_symlink() for path in (unit, *artifacts))):
        was_enabled = False
    else:
        raise ValueError('prior service enablement cannot be restored safely: ' +
                         (enable_text or enabled.stderr.strip() or 'unknown'))
    if active_text == 'active' and active.returncode == 0:
        was_active = True
    elif active_text == 'inactive' and active.returncode == 3:
        was_active = False
    else:
        raise ValueError('prior service running state cannot be restored safely: ' +
                         (active_text or active.stderr.strip() or 'unknown'))
    return was_enabled, was_active


@contextmanager
def installation_lock(path):
    """An inherited descriptor holds one lock across both core and panel phases."""
    descriptor = os.environ.get('SLEEP_DISABLER_INSTALL_LOCK_FD')
    if descriptor:
        try:
            descriptor = int(descriptor)
            held, expected = os.fstat(descriptor), path.stat()
            if descriptor < 3 or (held.st_dev, held.st_ino) != (expected.st_dev, expected.st_ino):
                raise ValueError('invalid inherited installer lock')
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ValueError):
            raise ValueError('invalid inherited installer lock') from None
        yield descriptor
        return
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('another installer is running') from None
        yield lock.fileno()


def run_installer(source, python, descriptor):
    environment = os.environ.copy()
    environment['SLEEP_DISABLER_INSTALL_LOCK_FD'] = str(descriptor)
    environment['PYTHON'] = python
    process = subprocess.Popen(['sh', str(source / 'install.sh'), '--under-install-lock'],
                               env=environment, pass_fds=(descriptor,), start_new_session=True)
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    def forward(signum, _frame):
        try:
            os.killpg(process.pid, signum)
        except ProcessLookupError:
            pass
    for sig in previous:
        signal.signal(sig, forward)
    try:
        return process.wait()
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)


class CoreTransaction:
    def __init__(self, source, python, commit_receipt=None):
        self.source = source
        self.python = python
        self.program = Path.home() / '.local/lib/sleep-disabler-gnome'
        self.cli = Path.home() / '.local/bin/sleep-disablerctl'
        self.unit = config_home() / 'systemd/user' / SERVICE
        self.staged = {}
        self.backups = {}
        self.switched = set()
        self.tag = str(time.time_ns()) + '.' + str(os.getpid())
        self.record = self.program.parent / '.sleep-disabler-core-transaction.json'
        self.commit_receipt = Path(commit_receipt) if commit_receipt else None

    def acknowledge_commit(self, version, api):
        """Publish the shell's commit boundary only after exact API readiness."""
        if self.commit_receipt is None:
            return
        temporary = self.commit_receipt.with_name(self.commit_receipt.name + '.prepared.' + self.tag)
        with temporary.open('x') as stream:
            json.dump({'committed': True, 'agentRuntimeVersion': version,
                       'apiVersion': api, 'transaction': self.tag}, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.rename(self.commit_receipt)

    def record_intent(self, enabled, active, python, prior_api):
        """Persist all planned paths before the first active-file mutation."""
        values = {'tag': self.tag, 'enabled': enabled, 'active': active,
                  'priorInterpreter': python, 'priorAPI': prior_api,
                  'artifacts': [{'target': str(target), 'staged': str(staged),
                                 'backup': str(target.with_name(target.name + '.previous.' + self.tag)),
                                 'existed': target.exists()}
                                for target, staged in self.staged.items()]}
        temporary = self.record.with_name(self.record.name + '.prepared.' + self.tag)
        with temporary.open('x') as stream:
            json.dump(values, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.rename(self.record)
        directory = os.open(self.record.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

    def archive_record(self, outcome):
        if self.record.exists():
            self.record.rename(self.record.with_name(self.record.name + '.' + outcome + '.' + self.tag))

    def stage(self):
        version, api = source_versions(self.source / 'agent.py')
        for target in (self.program, self.cli, self.unit):
            target.parent.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix='.sleep-disabler-core-stage.', dir=self.program.parent))
        for name in ('agent.py', 'ctl.py'):
            shutil.copyfile(self.source / name, stage / name)
            (stage / name).chmod(0o644)
        (stage / 'agent-launcher').write_text(launcher(self.python, self.program / 'agent.py'))
        (stage / 'agent-launcher').chmod(0o755)
        cli_stage = self.cli.with_name('.sleep-disabler-cli-stage.' + self.tag)
        cli_stage.write_text(launcher(self.python, self.program / 'ctl.py'))
        cli_stage.chmod(0o755)
        unit_stage = self.unit.with_name('.sleep-disabler-unit-stage.' + self.tag + '.service')
        template = (self.source / SERVICE).read_text()
        if 'ExecStart=@AGENT_LAUNCHER@' not in template:
            raise ValueError('service launcher placeholder is missing')
        unit_stage.write_text(template.replace('@AGENT_LAUNCHER@', unit_escape(self.program / 'agent-launcher')))
        unit_stage.chmod(0o644)
        self.staged = {self.program: stage, self.cli: cli_stage, self.unit: unit_stage}
        # Compile staging sources without importing/starting the agent.
        compile_script = 'import pathlib,sys; [compile(pathlib.Path(p).read_text(),p,"exec") for p in sys.argv[1:]]'
        for command in ([self.python, '-c', 'import dbus, gi'],
                        [self.python, '-c', compile_script, str(stage / 'agent.py'), str(stage / 'ctl.py')],
                        ['sh', '-n', str(stage / 'agent-launcher')], ['sh', '-n', str(cli_stage)]):
            result = run(command)
            if result.returncode:
                raise ValueError('staging validation failed: ' + result.stderr.strip())
        unit_sections = {}
        section = ''
        for line in unit_stage.read_text().splitlines():
            if line.startswith('[') and line.endswith(']'):
                section = line[1:-1]
            elif '=' in line and not line.startswith(('#', ';')):
                key, value = line.split('=', 1)
                if (section, key) in unit_sections:
                    raise ValueError('duplicate unit directive: ' + section + '.' + key)
                unit_sections[(section, key)] = value
        required = {'Type': 'dbus', 'BusName': 'org.sleepdisabler.App',
                    'TimeoutStartSec': '20', 'Restart': 'on-failure', 'TimeoutStopSec': '30',
                    'ExecStart': unit_escape(self.program / 'agent-launcher')}
        if any(unit_sections.get(('Service', key)) != value for key, value in required.items()):
            raise ValueError('service structure validation failed')
        if unit_sections.get(('Unit', 'CollectMode')) != 'inactive-or-failed' or ('Service', 'CollectMode') in unit_sections:
            raise ValueError('CollectMode must be specified in the Unit section')
        if (unit_sections.get(('Unit', 'PartOf')) != 'graphical-session.target' or
                unit_sections.get(('Install', 'WantedBy')) != 'graphical-session.target' or
                'graphical-session.target' in unit_sections.get(('Unit', 'After'), '').split()):
            raise ValueError('graphical session target ordering/lifecycle validation failed')
        analyzer = shutil.which('systemd-analyze')
        if analyzer:
            # Verify against the executable staging launcher; the final path
            # need not exist yet on a first install.
            verify_unit = stage / SERVICE
            verify_unit.write_text(template.replace('@AGENT_LAUNCHER@', unit_escape(stage / 'agent-launcher')))
            checked = run([analyzer, '--user', 'verify', str(verify_unit)])
            if checked.returncode:
                # Older systemd releases may not implement --user verify.
                unsupported = ('unrecognized option \'--user\'', 'Unknown command verb \'verify\'',
                               'Unknown operation verify')
                if not any(text in checked.stderr for text in unsupported):
                    raise ValueError('systemd unit validation failed: ' + checked.stderr.strip())
        return version, api

    def rollback(self, enabled, active, prior_api, prior_python=None):
        results = {'files': True, 'enablement': True, 'running-state': True, 'API': True}
        try:
            results['running-state'] = systemctl('stop', SERVICE).returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            results['running-state'] = False
        for target in reversed(tuple(self.staged)):
            backup = self.backups.get(target)
            if target not in self.switched and not (backup and backup.exists()):
                continue
            try:
                if target.exists():
                    target.rename(target.with_name(target.name + '.failed.' + self.tag))
                if backup and backup.exists():
                    backup.rename(target)
            except OSError:
                results['files'] = False
        for arguments, dimension in (
                (('daemon-reload',), 'files'),
                ((('enable' if enabled else 'disable'), SERVICE), 'enablement'),
                ((('restart' if active else 'stop'), SERVICE), 'running-state')):
            try:
                command = systemctl(*arguments)
                absent_unit_cleanup = (not self.unit.exists() and
                    (arguments[0] == 'disable' and not enabled or
                     arguments[0] == 'stop' and not active))
                if command.returncode and not absent_unit_cleanup:
                    results[dimension] = False
            except (OSError, subprocess.TimeoutExpired):
                results[dimension] = False
        try:
            restored_enabled, restored_active = service_snapshot(self.unit, (self.program, self.cli))
            if restored_enabled != enabled:
                results['enablement'] = False
            if restored_active != active:
                results['running-state'] = False
        except (OSError, ValueError, subprocess.TimeoutExpired):
            results['enablement'] = False
            results['running-state'] = False
        if active and prior_api and prior_python:
            try:
                results['API'] = ready(prior_python, prior_api['agentRuntimeVersion'], prior_api['apiVersion'])
            except (OSError, subprocess.TimeoutExpired):
                results['API'] = False
        elif not active:
            results['API'] = None  # No API was required in the prior inactive state.
        else:
            results['API'] = None  # Prior running process had no proven usable API.
        print('Core rollback: ' + ', '.join(
            key + '=' + ('unproven/not required' if value is None else 'restored' if value else 'FAILED')
            for key, value in results.items()), file=sys.stderr)
        return all(value is not False for value in results.values())

    def install(self):
        if self.commit_receipt is not None and (self.commit_receipt.exists() or self.commit_receipt.is_symlink()):
            raise ValueError('prior core commit receipt retained at ' + str(self.commit_receipt) +
                             '; no deployment started')
        if self.record.exists() or self.record.is_symlink():
            raise ValueError('unfinished core transaction retained at ' + str(self.record) +
                             '; inspect its artifact/backup paths before retrying; no files were switched')
        for target, directory in ((self.program, True), (self.cli, False), (self.unit, False)):
            if target.is_symlink() or (target.exists() and
                    not (target.is_dir() if directory else target.is_file())):
                raise ValueError('unsupported managed core destination at ' + str(target) +
                                 '; symlinks and unexpected file types require manual inspection; no deployment started')
        version, api = self.stage()
        enabled, active = service_snapshot(self.unit, (self.program, self.cli))
        prior_python = prior_interpreter(self.program, self.unit)
        prior_api = state(prior_python) if active and prior_python else None
        if active and not prior_python:
            print('Prior interpreter identity unavailable; API rollback will be unproven.', file=sys.stderr)
        self.record_intent(enabled, active, prior_python, prior_api)
        previous_handlers = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
        def interrupted(signum, _frame):
            raise RuntimeError('installation interrupted by signal ' + str(signum))
        for sig in previous_handlers:
            signal.signal(sig, interrupted)
        try:
            for target, staged in self.staged.items():
                if target.exists():
                    backup = target.with_name(target.name + '.previous.' + self.tag)
                    # Register intent before rename so a delivered signal cannot
                    # hide a successfully moved prior artifact from rollback.
                    self.backups[target] = backup
                    target.rename(backup)
                self.switched.add(target)
                staged.rename(target)
            for arguments in (('daemon-reload',), ('enable', SERVICE), ('restart', SERVICE)):
                result = systemctl(*arguments)
                if result.returncode:
                    raise RuntimeError('systemctl ' + arguments[0] + ' failed: ' + result.stderr.strip())
            if not ready(self.python, version, api):
                raise RuntimeError('bounded exact-version agent/API readiness failed')
            # No interruption may split proven readiness from its shell receipt.
            # Signals during deployment still roll back; after this boundary the
            # shell retains the core and classifies interruption as panel exit 2.
            for sig in previous_handlers:
                signal.signal(sig, signal.SIG_IGN)
            self.acknowledge_commit(version, api)
        except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired, KeyboardInterrupt) as error:
            print('Core installation failed: ' + str(error), file=sys.stderr)
            for sig in previous_handlers:
                signal.signal(sig, signal.SIG_IGN)
            if self.rollback(enabled, active, prior_api, prior_python):
                try:
                    self.archive_record('rolled-back')
                except OSError as archive_error:
                    print('Rollback succeeded; record retained: ' + str(archive_error), file=sys.stderr)
            return 1
        finally:
            for sig, handler in previous_handlers.items():
                signal.signal(sig, handler)
        try:
            self.archive_record('committed')
        except OSError as error:
            print('Core healthy, but transaction-record archival failed: ' + str(error), file=sys.stderr)
        print('Agent usable: exact runtime ' + version + ', API ' + str(api) + '. Prevention starts OFF.')
        print('CLI: ' + str(self.cli) + ' status')
        return 0


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--python', required=True)
    parser.add_argument('--state-field')
    parser.add_argument('--state-json', action='store_true')
    parser.add_argument('--panel-runtime')
    parser.add_argument('--run-installer', action='store_true')
    parser.add_argument('--commit-receipt')
    parser.add_argument('--gnome', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.gnome is not None:
        try:
            result = run(['gnome-extensions', *args.gnome], timeout=10)
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            return result.returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            print('GNOME extension command failed: ' + str(error), file=sys.stderr)
            return 1
    if args.panel_runtime:
        try:
            version, api = source_versions(Path(__file__).resolve().parent / 'agent.py')
            return 0 if ready(args.python, version, api, args.panel_runtime) else 1
        except (OSError, ValueError, subprocess.TimeoutExpired):
            return 1
    if args.state_field or args.state_json:
        current = state(args.python)
        if current is None:
            return 1
        print(json.dumps(current) if args.state_json else current.get(args.state_field, ''))
        return 0
    try:
        transaction = CoreTransaction(Path(__file__).resolve().parent, args.python, args.commit_receipt)
        transaction.program.parent.mkdir(parents=True, exist_ok=True)
        with installation_lock(transaction.program.parent / '.sleep-disabler-install.lock') as descriptor:
            if args.run_installer:
                return run_installer(transaction.source, args.python, descriptor)
            return transaction.install()
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        print('Core staging failed before switch: ' + str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
