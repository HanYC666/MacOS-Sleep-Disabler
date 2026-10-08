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
PANEL_READINESS_ATTEMPTS = 45
READINESS_DELAY = 1
READINESS_TIMEOUT = 70
STATE_SCRIPT = '''# sleep-disabler-install-state
import json
import dbus
bus = dbus.SessionBus()
owner = bus.get_name_owner('org.sleepdisabler.App')
daemon = dbus.Interface(bus.get_object('org.freedesktop.DBus', '/org/freedesktop/DBus',
                                       introspect=False), 'org.freedesktop.DBus')
owner_pid = daemon.GetConnectionUnixProcessID(owner, timeout=3)
if type(owner_pid) is bool or int(owner_pid) <= 0:
    raise ValueError('Invalid runtime bus owner PID')
app = dbus.Interface(bus.get_object(owner, '/org/sleepdisabler/App', introspect=False),
                     'org.sleepdisabler.App1')
state = app.GetState(timeout=3)
if bus.get_name_owner('org.sleepdisabler.App') != owner:
    raise ValueError('Runtime bus owner changed during state probe')
agent_version = state.get('agentRuntimeVersion', '')
api = state.get('apiVersion', 0)
panel_version = state.get('panelRuntimeVersion', '')
registered = state.get('panelRuntimeRegistered', False)
state_home = state.get('stateHome', '')
if (not isinstance(agent_version, str) or not isinstance(panel_version, str) or
        not isinstance(api, int) or isinstance(api, (bool, dbus.Boolean)) or
        not isinstance(registered, (bool, dbus.Boolean)) or not isinstance(state_home, str)):
    raise ValueError('Malformed runtime state types')
print(json.dumps({'agentRuntimeVersion': str(agent_version), 'apiVersion': int(api),
                  'panelRuntimeVersion': str(panel_version),
                  'panelRuntimeRegistered': bool(registered), 'stateHome': str(state_home),
                  'ownerPID': int(owner_pid)}))
'''


def run(command, *, timeout=40, env=None):
    return subprocess.run(command, text=True, capture_output=True, timeout=timeout,
                          check=False, env=env)


def systemctl(*arguments, timeout=40):
    return run(['systemctl', '--user', *arguments], timeout=timeout)


def loaded_fragment(*, timeout=3, require_query=False):
    result = systemctl('show', '--property=FragmentPath', '--value', SERVICE, timeout=timeout)
    if result.returncode:
        if require_query:
            raise ValueError('loaded service fragment query failed')
        return None
    if not result.stdout.strip():
        return None
    path = Path(result.stdout.strip())
    # FragmentPath identifies the file systemd actually loaded. Resolving it
    # would turn a shadowing alias into an apparent match for our unit path.
    if not path.is_absolute():
        if require_query:
            raise ValueError('loaded service fragment is not absolute')
        return None
    return path


def service_main_pid(*, timeout=3):
    try:
        result = systemctl('show', '--property=MainPID', '--value', SERVICE, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode:
        return None
    try:
        pid = int(result.stdout.strip())
    except ValueError:
        return None
    return pid if pid > 0 else None


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
                type(values.get('panelRuntimeRegistered')) is not bool or
                not isinstance(values.get('stateHome', ''), str) or
                type(values.get('ownerPID')) is not int or values['ownerPID'] <= 0):
            return None
        return values
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def ready(python, version, api, panel=None, *, state_home=None, unit=None):
    deadline = time.monotonic() + READINESS_TIMEOUT
    # Agent startup can consume TimeoutStartSec=20 before the panel's three
    # registration calls (up to 5+1+5+2+5 seconds) complete.
    attempts = PANEL_READINESS_ATTEMPTS if panel is not None else READINESS_ATTEMPTS
    for attempt in range(attempts):
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
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            if (current and current['agentRuntimeVersion'] == version and current['apiVersion'] == api
                    and (state_home is None or current.get('stateHome') == str(state_home))
                    and (panel is None or (current['panelRuntimeRegistered'] is True
                                           and current['panelRuntimeVersion'] == panel))):
                if current['ownerPID'] == service_main_pid(timeout=min(3, remaining)):
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        return False
                    if unit is None or loaded_fragment(timeout=min(3, remaining)) == unit:
                        if time.monotonic() < deadline:
                            return True
        if attempt + 1 < attempts:
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
    if not text.isprintable():
        raise ValueError('unit executable path contains a control character')
    return '"' + text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%').replace('$', '$$') + '"'


def environment_assignment(name, value):
    text = str(value)
    if not text or not Path(text).is_absolute() or not text.isprintable():
        raise ValueError(name + ' is not a safely representable absolute path')
    return '"' + name + '=' + text.replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def process_environment_value(environment, name):
    """Read one effective process variable; duplicates cannot prove a path."""
    prefix = name.encode('ascii') + b'='
    values = [item[len(prefix):] for item in environment.split(b'\0') if item.startswith(prefix)]
    if len(values) > 1:
        raise ValueError('process environment has duplicate ' + name + ' entries')
    return values[0].decode('utf-8') if values else ''


def process_start_time(proc):
    """Read /proc stat field 22 without confusing spaces in the command name."""
    stat = (proc / 'stat').read_text()
    end = stat.rfind(')')
    fields = stat[end + 1:].split() if end >= 0 else []
    if not stat.startswith(proc.name + ' (') or len(fields) < 20:
        raise ValueError('process identity cannot be established from /proc stat')
    start = int(fields[19])
    if start <= 0:
        raise ValueError('process start time is invalid')
    return start


def previous_state_home(unit, prior_api, active):
    """Prove the old state location before a versioned unit replaces it."""
    if prior_api and prior_api.get('stateHome'):
        previous = prior_api['stateHome']
        if Path(previous).is_absolute():
            return Path(previous).resolve()
    if active:
        try:
            result = systemctl('show', '--property=MainPID', '--value', SERVICE, timeout=3)
            pid = int(result.stdout.strip()) if result.returncode == 0 else 0
            if pid > 0:
                proc = Path('/proc') / str(pid)
                if proc.stat().st_uid != os.getuid():
                    raise ValueError('previous agent PID is not owned by this user')
                start = process_start_time(proc)
                environ = (proc / 'environ').read_bytes()
                again = systemctl('show', '--property=MainPID', '--value', SERVICE, timeout=3)
                if (again.returncode == 0 and again.stdout.strip() == str(pid) and
                        process_start_time(proc) == start):
                    value = process_environment_value(environ, 'XDG_STATE_HOME')
                    home = process_environment_value(environ, 'HOME')
                    if value:
                        path = Path(value)
                    elif home:
                        path = Path(home) / '.local/state'
                    else:
                        path = Path('.')
                    if path.is_absolute():
                        return path.resolve()
        except (OSError, ValueError, UnicodeError, subprocess.TimeoutExpired):
            pass
        # A unit file cannot prove the effective environment of a running
        # legacy process; manager overrides may have changed it.
        return None
    try:
        lines = unit.read_text().splitlines()
        # An inactive unit is only proof when its effective state-home
        # assignment is unambiguous. Other environment sources can override
        # or remove a value parsed from the unit text.
        assignments = []
        section = ''
        for line in lines:
            # Continuations and alternate spacing need systemd's own parser;
            # a text inspection must not infer an effective old path from them.
            if line.rstrip().endswith('\\'):
                return None
            if line.strip().startswith('[') and line.strip().endswith(']'):
                section = line.strip()[1:-1]
                continue
            key, separator, value = line.lstrip().partition('=')
            if section != 'Service':
                continue
            if key.strip() in ('EnvironmentFile', 'UnsetEnvironment', 'PAMName'):
                return None
            if key.strip() == 'Environment':
                if not separator:
                    return None
                assignments.append(value)
        previous = None
        for assignment in assignments:
            if not assignment.strip():
                return None
            words = shlex.split(assignment)
            for word in words:
                if word.startswith('XDG_STATE_HOME='):
                    value = word.partition('=')[2].replace('%%', '%')
                    if previous is not None or not value or not Path(value).is_absolute():
                        return None
                    # shlex is not systemd's parser. Only trust the exact
                    # spelling emitted by this installer, so C escapes and
                    # unexpanded specifiers cannot change the actual path.
                    if assignment.strip() != environment_assignment('XDG_STATE_HOME', value):
                        return None
                    previous = Path(value).resolve()
        return previous
    except (OSError, ValueError):
        pass
    return None


def verify_state_home_transition(previous, selected, active):
    """Allow an attended inactive transition only after recovery is absent."""
    if previous == selected:
        return
    if active:
        raise ValueError('selected XDG_STATE_HOME differs from the active previous agent state home; '
                         'stop the old service and resolve recovery records before deployment')
    for home in (previous, selected):
        directory = home / 'sleep-disabler'
        if directory.is_symlink() or (directory.exists() and not directory.is_dir()):
            raise ValueError('state-home transition has an unsafe state directory: ' + str(directory))
        for name in ('brightness-recovery.json', 'brightness-recovery.json.tmp'):
            record = directory / name
            if record.exists() or record.is_symlink():
                raise ValueError('state-home transition has an unresolved brightness recovery record: ' +
                                 str(record))


def config_home():
    return effective_xdg('XDG_CONFIG_HOME', '.config')


def installation_home():
    value = os.environ.get('HOME', '')
    if not value or not Path(value).is_absolute() or not value.isprintable():
        raise ValueError('HOME must be a safely representable absolute path')
    return Path(value)


def effective_xdg(name, default):
    value = str(os.environ.get(name) or installation_home() / default)
    if not value.isprintable():
        raise ValueError(name + ' contains a non-printable character')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError(name + ' must be an absolute path')
    return path


def discovered_extension_path(uuid):
    """Return Shell's reported path, or None when the UUID is not discovered."""
    environment = dict(os.environ, LC_ALL='C')
    try:
        result = run(['gnome-extensions', 'info', uuid], timeout=3, env=environment)
        if result.returncode:
            listed = run(['gnome-extensions', 'list'], timeout=3, env=environment)
            if listed.returncode:
                raise ValueError('GNOME extension discovery list is unavailable')
            if uuid in listed.stdout.splitlines():
                raise ValueError('GNOME lists the selected UUID but cannot prove its path')
            return None
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError('GNOME extension path query unavailable: ' + str(error)) from error
    paths = [line.partition(':')[2].strip() for line in result.stdout.splitlines()
             if line.lstrip().startswith('Path:')]
    if len(paths) != 1 or not Path(paths[0]).is_absolute():
        raise ValueError('GNOME reported an extension without one absolute discovery path')
    # Keep the path Shell reports: resolving it would hide a shadowing symlink
    # whose target happens to be the managed extension directory.
    return Path(paths[0])


def preflight_discovery():
    """Compare installer paths with the two running discovery authorities."""
    import dbus
    config = effective_xdg('XDG_CONFIG_HOME', '.config')
    selected_data = effective_xdg('XDG_DATA_HOME', '.local/share')
    effective_xdg('XDG_STATE_HOME', '.local/state')
    try:
        bus = dbus.SessionBus()
        manager = dbus.Interface(bus.get_object('org.freedesktop.systemd1',
            '/org/freedesktop/systemd1', introspect=False), 'org.freedesktop.DBus.Properties')
        paths = manager.Get('org.freedesktop.systemd1.Manager', 'UnitPath', timeout=3)
        if not isinstance(paths, (list, tuple, dbus.Array)) or not paths:
            raise ValueError('user manager returned no supported UnitPath')
        selected_unit_dir = config / 'systemd/user'
        manager_unit_paths = [Path(str(path)) for path in paths]
        if any(not path.is_absolute() for path in manager_unit_paths):
            raise ValueError('running manager UnitPath contains a nonabsolute directory')
        if selected_unit_dir not in manager_unit_paths:
            raise ValueError('selected systemd user unit directory is not in the running manager UnitPath; '
                             'start a session using the intended XDG_CONFIG_HOME')
        for directory in manager_unit_paths[:manager_unit_paths.index(selected_unit_dir)]:
            shadow = directory / SERVICE
            if directory.resolve() == selected_unit_dir.resolve():
                raise ValueError('an earlier running-manager UnitPath aliases the managed service directory: ' +
                                 str(directory))
            if shadow.exists() or shadow.is_symlink():
                raise ValueError('an earlier running-manager UnitPath shadows the managed service: ' +
                                 str(shadow))
        names = dbus.Interface(bus.get_object('org.freedesktop.DBus',
            '/org/freedesktop/DBus', introspect=False), 'org.freedesktop.DBus')
        for _attempt in range(2):
            owner = str(names.GetNameOwner('org.gnome.Shell', timeout=3))
            uid = int(names.GetConnectionUnixUser(owner, timeout=3))
            pid = int(names.GetConnectionUnixProcessID(owner, timeout=3))
            if uid != os.getuid() or pid <= 0:
                raise ValueError('GNOME Shell owner is not a verifiable same-user process')
            try:
                proc = Path('/proc') / str(pid)
                if proc.stat().st_uid != uid:
                    raise ValueError('GNOME Shell PID no longer belongs to its D-Bus user')
                start = process_start_time(proc)
                environment = (proc / 'environ').read_bytes()
                if process_start_time(proc) != start:
                    continue
            except OSError as error:
                raise ValueError('GNOME Shell environment is unavailable before deployment: ' + str(error)) from error
            observed = process_environment_value(environment, 'XDG_DATA_HOME')
            shell_home = process_environment_value(environment, 'HOME')
            if not observed and (not shell_home or not Path(shell_home).is_absolute()):
                raise ValueError('GNOME Shell default data home cannot be established from its HOME')
            shell_data = Path(observed) if observed else Path(shell_home) / '.local/share'
            if not shell_data.is_absolute():
                raise ValueError('GNOME Shell XDG_DATA_HOME is not absolute')
            if (str(names.GetNameOwner('org.gnome.Shell', timeout=3)) != owner or
                    int(names.GetConnectionUnixProcessID(owner, timeout=3)) != pid or
                    process_start_time(proc) != start):
                continue
            if selected_data.resolve() != shell_data.resolve():
                raise ValueError('installer XDG_DATA_HOME differs from running GNOME Shell; '
                                 'start a session using the intended data path')
            extension_target = selected_data / 'gnome-shell/extensions/sleep-disabler@local'
            discovered = discovered_extension_path('sleep-disabler@local')
            if (str(names.GetNameOwner('org.gnome.Shell', timeout=3)) != owner or
                    int(names.GetConnectionUnixProcessID(owner, timeout=3)) != pid or
                    process_start_time(proc) != start):
                continue
            if discovered is not None and discovered not in (extension_target, extension_target.resolve()):
                raise ValueError('another GNOME extension path shadows the selected UUID: ' + str(discovered))
            if discovered is None and extension_target.exists():
                raise ValueError('installed GNOME extension discovery path is unproven before deployment')
            return
        raise ValueError('GNOME Shell owner changed during environment preflight')
    except (dbus.DBusException, OSError, UnicodeError) as error:
        raise ValueError('Session discovery preflight unavailable before deployment: ' + str(error)) from error


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
        home = installation_home()
        self.program = home / '.local/lib/sleep-disabler-gnome'
        self.cli = home / '.local/bin/sleep-disablerctl'
        self.unit = config_home() / 'systemd/user' / SERVICE
        self.state_home = effective_xdg('XDG_STATE_HOME', '.local/state')
        self.staged = {}
        self.backups = {}
        self.switched = set()
        self.tag = str(time.time_ns()) + '.' + str(os.getpid())
        self.record = self.program.parent / '.sleep-disabler-core-transaction.json'
        self.commit_receipt = Path(commit_receipt) if commit_receipt else None

    def validate_destinations(self):
        """Reject unsupported managed entries before creating the lock parent."""
        for parent in (self.program.parent, self.cli.parent, self.unit.parent):
            if parent.is_symlink() or (parent.exists() and not parent.is_dir()):
                raise ValueError('unsupported managed core parent at ' + str(parent) +
                                 '; symlinks and non-directories require manual inspection')
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
        if ('ExecStart=@AGENT_LAUNCHER@' not in template or
                'Environment=@STATE_HOME_ENV@' not in template):
            raise ValueError('service launcher/environment placeholder is missing')
        state_environment = environment_assignment('XDG_STATE_HOME', self.state_home)
        unit_stage.write_text(template.replace('@AGENT_LAUNCHER@', unit_escape(self.program / 'agent-launcher'))
                              .replace('@STATE_HOME_ENV@', state_environment))
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
                    'ExecStart': unit_escape(self.program / 'agent-launcher'),
                    'Environment': state_environment}
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
            verify_unit.write_text(template.replace('@AGENT_LAUNCHER@', unit_escape(stage / 'agent-launcher'))
                                   .replace('@STATE_HOME_ENV@', state_environment))
            checked = run([analyzer, '--user', 'verify', str(verify_unit)])
            if checked.returncode:
                # Older systemd releases may not implement --user verify.
                unsupported = ('unrecognized option \'--user\'', 'Unknown command verb \'verify\'',
                               'Unknown operation verify')
                if not any(text in checked.stderr for text in unsupported):
                    raise ValueError('systemd unit validation failed: ' + checked.stderr.strip())
        return version, api

    def rollback(self, enabled, active, prior_api, prior_python, prior_fragment):
        results = {'files': True, 'enablement': True, 'running-state': True,
                   'fragment': True, 'API': True, 'state-home': None}
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
        try:
            results['fragment'] = loaded_fragment(require_query=True) == prior_fragment
        except (OSError, ValueError, subprocess.TimeoutExpired):
            results['fragment'] = False
        if active and prior_api and prior_python:
            try:
                prior_state_home = prior_api.get('stateHome') or None
                results['API'] = ready(prior_python, prior_api['agentRuntimeVersion'], prior_api['apiVersion'],
                                       state_home=prior_state_home, unit=self.unit)
                if prior_state_home is not None:
                    results['state-home'] = results['API']
            except (OSError, subprocess.TimeoutExpired):
                results['API'] = False
                if prior_api.get('stateHome'):
                    results['state-home'] = False
        elif not active:
            results['API'] = None  # No API was required in the prior inactive state.
        else:
            results['API'] = None  # Prior running process had no proven usable API.
        print('Core rollback: ' + ', '.join(
            key + '=' + ('unproven/not required' if value is None else 'restored' if value else 'FAILED')
            for key, value in results.items()), file=sys.stderr)
        return all(value is not False for value in results.values())

    def install(self):
        self.validate_destinations()
        enabled, active = service_snapshot(self.unit, (self.program, self.cli))
        fragment = loaded_fragment(require_query=True)
        if ((fragment is not None and fragment != self.unit) or
                (self.unit.exists() and fragment is None)):
            raise ValueError('a different or unverified service fragment shadows the managed unit; '
                             'no deployment started')
        prior_unit_bytes = self.unit.read_bytes() if self.unit.exists() else None
        prior_python = prior_interpreter(self.program, self.unit)
        prior_api = state(prior_python) if active and prior_python else None
        if prior_api and prior_api['ownerPID'] != service_main_pid():
            raise ValueError('previous agent API owner does not match the running service PID; '
                             'no deployment started')
        old_state_home = None
        if self.unit.exists():
            prior_launcher = self.program / 'agent-launcher'
            prior_launcher_bytes = prior_launcher.read_bytes() if not active else None
            if not active:
                dropins = systemctl('show', '--property=DropInPaths', '--value', SERVICE, timeout=3)
                if dropins.returncode or dropins.stdout.strip():
                    raise ValueError('previous inactive unit has unproven environment drop-ins; '
                                     'inspect its effective state home before deployment')
            old_state_home = previous_state_home(self.unit, prior_api, active)
            if old_state_home is None:
                raise ValueError('previous agent state home cannot be established; inspect prior unit and '
                                 'brightness journal before changing the XDG_STATE_HOME contract')
            if not active:
                starts = []
                section = ''
                for line in self.unit.read_text().splitlines():
                    if line.strip().startswith('[') and line.strip().endswith(']'):
                        section = line.strip()[1:-1]
                        continue
                    key, separator, _value = line.lstrip().partition('=')
                    if separator and key.strip() == 'ExecStart':
                        starts.append((section, line.strip()))
                expected = 'ExecStart=' + unit_escape(self.program / 'agent-launcher')
                if (starts != [('Service', expected)] or prior_python is None or
                        (self.program / 'agent-launcher').read_text() !=
                        launcher(prior_python, self.program / 'agent.py')):
                    raise ValueError('previous inactive unit launcher contract is unproven; '
                                     'inspect ExecStart and the installed launcher before deployment')
            verify_state_home_transition(old_state_home, self.state_home.resolve(), active)
        if active and not prior_python:
            print('Prior interpreter identity unavailable; API rollback will be unproven.', file=sys.stderr)
        version, api = self.stage()
        # Staging can outlive the prior service/fragment snapshot. Recheck
        # both before recording intent or moving any managed artifact.
        current_enabled, current_active = service_snapshot(self.unit, (self.program, self.cli))
        if (current_enabled, current_active) != (enabled, active):
            raise ValueError('previous service changed during staging; no files were switched')
        self.validate_destinations()
        current_fragment = loaded_fragment(require_query=True)
        if current_fragment != fragment:
            raise ValueError('loaded service fragment changed during staging; no files were switched')
        if (self.unit.read_bytes() if self.unit.exists() else None) != prior_unit_bytes:
            raise ValueError('previous unit file changed during staging; no files were switched')
        if not active and prior_unit_bytes is not None:
            if prior_launcher.read_bytes() != prior_launcher_bytes:
                raise ValueError('previous launcher changed during staging; no files were switched')
            dropins = systemctl('show', '--property=DropInPaths', '--value', SERVICE, timeout=3)
            if dropins.returncode or dropins.stdout.strip():
                raise ValueError('previous inactive unit drop-ins changed during staging; no files were switched')
            if previous_state_home(self.unit, None, False) != old_state_home:
                raise ValueError('previous inactive state home changed during staging; no files were switched')
        if active:
            current_api = state(prior_python) if prior_python else None
            if prior_api:
                if (current_api is None or
                        current_api['ownerPID'] != prior_api['ownerPID'] or
                        current_api['agentRuntimeVersion'] != prior_api['agentRuntimeVersion'] or
                        current_api['apiVersion'] != prior_api['apiVersion'] or
                        current_api['ownerPID'] != service_main_pid()):
                    raise ValueError('previous agent runtime changed during staging; no files were switched')
            if old_state_home is not None and previous_state_home(self.unit, current_api, True) != old_state_home:
                raise ValueError('previous agent state home changed during staging; no files were switched')
        # A changed state home also needs fresh recovery-record evidence.
        if old_state_home is not None and old_state_home != self.state_home.resolve():
            verify_state_home_transition(old_state_home, self.state_home.resolve(), current_active)
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
            if not ready(self.python, version, api, state_home=self.state_home, unit=self.unit):
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
            if self.rollback(enabled, active, prior_api, prior_python, fragment):
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
    parser.add_argument('--preflight-discovery', action='store_true')
    parser.add_argument('--commit-receipt')
    parser.add_argument('--gnome', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    if args.preflight_discovery:
        try:
            preflight_discovery()
            return 0
        except (OSError, ValueError) as error:
            print('Installation preflight failed: ' + str(error), file=sys.stderr)
            return 1
    if args.gnome is not None:
        try:
            result = run(['gnome-extensions', *args.gnome], timeout=10,
                         env=dict(os.environ, LC_ALL='C'))
            sys.stdout.write(result.stdout)
            sys.stderr.write(result.stderr)
            return result.returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            print('GNOME extension command failed: ' + str(error), file=sys.stderr)
            return 1
    if args.panel_runtime:
        try:
            version, api = source_versions(Path(__file__).resolve().parent / 'agent.py')
            return 0 if ready(args.python, version, api, args.panel_runtime,
                              state_home=effective_xdg('XDG_STATE_HOME', '.local/state'),
                              unit=config_home() / 'systemd/user' / SERVICE) else 1
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
        transaction.validate_destinations()
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
