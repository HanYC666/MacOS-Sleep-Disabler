"""Isolated installer transaction tests using a temporary HOME and command stubs."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).parents[1]
UUID = 'sleep-disabler@local'


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.home = self.root / 'home'
        self.data = self.root / 'data'
        self.stubs = self.root / 'bin'
        self.source = self.root / 'source'
        self.home.mkdir()
        self.stubs.mkdir()
        shutil.copytree(SOURCE, self.source)
        helper = self.source / 'core_install.py'
        helper.write_text(helper.read_text().replace('READINESS_DELAY = 1', 'READINESS_DELAY = 0'))
        self.real_python = sys.executable
        self._write_stub('sleep', '#!/bin/sh\nexit 0\n')
        self._write_stub('systemd-analyze', '#!/bin/sh\nexit "${FAIL_UNIT_VERIFY:-0}"\n')
        self._write_stub('systemctl', f'''#!{self.real_python}
import json, os, sys
from pathlib import Path
path = Path(os.environ['HOME']) / '.service-state'
state = json.loads(path.read_text()) if path.exists() else {{'enabled': os.environ.get('PREV_CORE_ENABLED') == '1', 'active': os.environ.get('PREV_CORE_ACTIVE') == '1'}}
args = sys.argv[2:]
command = args[0]
with (path.parent / '.service-commands').open('a') as log:
    log.write(' '.join(args) + '\\n')
if command in ('is-enabled', 'is-active') and os.environ.get('UNKNOWN_SERVICE') == '1':
    print('masked' if command == 'is-enabled' else 'activating')
    sys.exit(1)
failure = path.parent / '.systemctl-failed'
if command == os.environ.get('FAIL_SYSTEMCTL') and not failure.exists():
    failure.touch()
    sys.exit(1)
if command == 'is-active':
    if '--quiet' not in args: print('active' if state['active'] else 'inactive')
    sys.exit(0 if state['active'] else 3)
if command == 'is-enabled':
    if '--quiet' not in args: print('enabled' if state['enabled'] else 'disabled')
    sys.exit(0 if state['enabled'] else 1)
if command == 'enable': state['enabled'] = True
if command == 'disable': state['enabled'] = False
if command in ('restart', 'start'): state['active'] = True
if command == 'stop': state['active'] = False
path.write_text(json.dumps(state))
''')
        self._write_stub('installer-state', f'''#!{self.real_python}
import ast, json, os, sys
from pathlib import Path
home = Path(os.environ['HOME'])
program = home / '.local/lib/sleep-disabler-gnome/agent.py'
service = home / '.service-state'
if not program.exists() or (service.exists() and not json.loads(service.read_text())['active']): sys.exit(1)
constants = {{}}
for node in ast.parse(program.read_text()).body:
    if isinstance(node, ast.Assign):
        for target in node.targets:
            if isinstance(target, ast.Name) and target.id in ('AGENT_RUNTIME_VERSION', 'API_VERSION'):
                constants[target.id] = ast.literal_eval(node.value)
version = constants['AGENT_RUNTIME_VERSION']
if version == '0.5.0':
    counter = home / '.core-api-count'
    count = int(counter.read_text()) + 1 if counter.exists() else 1
    counter.write_text(str(count))
    if os.environ.get('CORE_API_FAIL') == '1' or count <= int(os.environ.get('CORE_API_DELAY', '0')): sys.exit(1)
    if os.environ.get('STALE_AGENT') == '1': version = '0.4.0'
api = constants['API_VERSION']
if version == '0.5.0' and os.environ.get('BAD_API') == '1': api = 999
panel = '1' if os.environ.get('PREV_ACTIVE') == '1' else ''
target = Path(os.environ['XDG_DATA_HOME']) / 'gnome-shell/extensions/sleep-disabler@local'
if (home / '.gnome-test-enabled').exists():
    panel = '5' if (target / 'extension.js').exists() else '1'
    if panel == '5' and 'PANEL_RUNTIME' in os.environ: panel = os.environ['PANEL_RUNTIME']
    if panel == '5':
        counter = home / '.panel-api-count'
        count = int(counter.read_text()) + 1 if counter.exists() else 1
        counter.write_text(str(count))
        if count <= int(os.environ.get('PANEL_API_DELAY', '0')): panel = ''
print(json.dumps({{'agentRuntimeVersion': version, 'apiVersion': api,
                  'panelRuntimeVersion': panel,
                  'panelRuntimeRegistered': bool(panel) and os.environ.get('PANEL_UNREGISTERED') != '1'}}))
''')
        self._write_stub('test-python', f'''#!/bin/sh
if [ "$1" = "-c" ]; then
    case "$2" in
      'import dbus, gi') exit "${{FAIL_IMPORT:-0}}";;
      '# sleep-disabler-install-state'*) exec "{self.stubs}/installer-state";;
    esac
fi
exec "{self.real_python}" "$@"
''')
        self._write_stub('gnome-extensions', '''#!/bin/sh
state="$HOME/.gnome-test-enabled"
count="$HOME/.gnome-test-enable-count"
case "$1" in
  info)
    [ "$FAIL_DISCOVERY" != 1 ]
    ;;
  enable)
    n=0
    [ -f "$count" ] && n=$(cat "$count")
    n=$((n + 1))
    printf '%s\n' "$n" > "$count"
    if [ "$FAIL_ENABLE" = 1 ] && [ "$n" -eq 1 ]; then exit 1; fi
    : > "$state"
    ;;
  disable)
    [ "$FAIL_DISABLE" = 1 ] && exit 1
    "$PYTHON_REAL" - "$state" <<'PYREMOVE'
from pathlib import Path
import sys
Path(sys.argv[1]).unlink(missing_ok=True)
PYREMOVE
    ;;
  list)
    if [ "$2" = "--enabled" ]; then
      if [ "$FAIL_ENABLED_LIST" = 1 ] && [ -f "$count" ] &&
         [ ! -f "$HOME/.gnome-test-enabled-list-failed" ]; then
        : > "$HOME/.gnome-test-enabled-list-failed"
        exit 1
      fi
      if [ -f "$state" ] || { [ "$PREV_ENABLED" = 1 ] && [ ! -f "$count" ]; }; then
        printf '%s\n' 'sleep-disabler@local'
      fi
    elif [ "$2" = "--active" ]; then
      if [ "$FAIL_ACTIVE" != 1 ] &&
         { [ -f "$state" ] || { [ "$PREV_ACTIVE" = 1 ] && [ ! -f "$count" ]; }; }; then
        printf '%s\n' 'sleep-disabler@local'
      fi
    fi
    ;;
esac
''')
        self._write_stub('mv', '''#!/bin/sh
case "$2" in
  *sleep-disabler-failed*) [ "$FAIL_FAILED_RETAIN" = 1 ] && exit 1;;
esac
case "$2" in
  *sleep-disabler-rollback*)
    if [ "$SIGNAL_PANEL_SWITCH" = 1 ] && [ ! -f "$HOME/.panel-signalled" ]; then
      /bin/mv "$@" || exit 1
      : > "$HOME/.panel-signalled"
      kill -TERM "$PPID"
      exit 0
    fi
    ;;
esac
case "$1" in
  *sleep-disabler-stage*)
    if [ "$FAIL_SWITCH_MOVE" = 1 ]; then exit 1; fi
    ;;
  *sleep-disabler-rollback*)
    if [ "$FAIL_ROLLBACK_MOVE" = 1 ]; then exit 1; fi
    case "$2" in
      *sleep-disabler-previous*) [ "$FAIL_BACKUP_ARCHIVE" = 1 ] && exit 1;;
    esac
    ;;
esac
exec /bin/mv "$@"
''')

    def _write_stub(self, name, content):
        path = self.stubs / name
        path.write_text(content)
        path.chmod(0o755)

    @property
    def target(self):
        return self.data / 'gnome-shell/extensions' / UUID

    def previous_install(self):
        self.target.mkdir(parents=True)
        (self.target / 'old-marker').write_text('working previous extension')
        (self.target / 'metadata.json').write_text(json.dumps({'uuid': UUID, 'version': 1}))

    def run_installer(self, **changes):
        env = os.environ.copy()
        env.update({
            'HOME': str(self.home),
            'XDG_DATA_HOME': str(self.data),
            'XDG_CONFIG_HOME': str(self.home / '.config'),
            'XDG_STATE_HOME': str(self.home / '.local/state'),
            'XDG_CURRENT_DESKTOP': 'GNOME',
            'XDG_SESSION_TYPE': 'wayland',
            'PYTHON': str(self.stubs / 'test-python'),
            'PYTHON_REAL': self.real_python,
            'PATH': str(self.stubs) + os.pathsep + env.get('PATH', ''),
            'PREV_ENABLED': '0', 'PREV_ACTIVE': '0',
            'FAIL_DISCOVERY': '0', 'FAIL_ENABLE': '0',
            'FAIL_ENABLED_LIST': '0', 'FAIL_ACTIVE': '0', 'FAIL_SWITCH_MOVE': '0',
            'FAIL_ROLLBACK_MOVE': '0',
            'FAIL_DISABLE': '0',
            'DBUS_SESSION_BUS_ADDRESS': 'unix:path=/nonexistent-sleep-disabler-test-bus',
        })
        env.update({key: str(value) for key, value in changes.items()})
        return subprocess.run(
            ['sh', str(self.source / 'install.sh')],
            env=env, text=True, capture_output=True, check=False)

    def assert_agent_preserved(self):
        self.assertTrue((self.home / '.local/lib/sleep-disabler-gnome/agent.py').is_file())
        self.assertTrue((self.home / '.local/bin/sleep-disablerctl').is_file())

    def test_staged_upgrade_success_preserves_old_copy_until_verification(self):
        self.previous_install()
        result = self.run_installer(PREV_ENABLED=1, PREV_ACTIVE=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.target / 'extension.js').is_file())
        self.assertFalse((self.target / 'old-marker').exists())
        retained = list(self.target.parent.glob('.sleep-disabler-previous.*'))
        self.assertEqual(len(retained), 1)
        self.assertTrue((retained[0] / 'old-marker').is_file())

    def test_validation_failure_does_not_touch_active_extension(self):
        self.previous_install()
        metadata = self.source / 'extension' / UUID / 'metadata.json'
        values = json.loads(metadata.read_text())
        values['uuid'] = 'wrong@example'
        metadata.write_text(json.dumps(values))
        result = self.run_installer(PREV_ENABLED=1, PREV_ACTIVE=1)
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.target / 'old-marker').is_file())
        self.assertIn('metadata validation failed', result.stderr)
        self.assert_agent_preserved()

    def test_discovery_failure_restores_previous_directory_and_state(self):
        self.previous_install()
        result = self.run_installer(
            PREV_ENABLED=1, PREV_ACTIVE=1, FAIL_DISCOVERY=1)
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.target / 'old-marker').is_file())
        self.assertIn('Previous extension state restored', result.stderr)
        self.assertTrue((self.home / '.gnome-test-enabled').is_file())
        self.assert_agent_preserved()

    def test_enable_failure_rolls_back_previous_extension(self):
        self.previous_install()
        result = self.run_installer(
            PREV_ENABLED=1, PREV_ACTIVE=1, FAIL_ENABLE=1)
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.target / 'old-marker').is_file())
        self.assertIn('Previous extension state restored', result.stderr)

    def test_unload_failure_rolls_back_previous_extension(self):
        self.previous_install()
        result = self.run_installer(
            PREV_ENABLED=1, PREV_ACTIVE=1, FAIL_DISABLE=1)
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.target / 'old-marker').is_file())
        self.assertIn('could not unload the previous extension', result.stderr)
        self.assertIn('Previous extension state restored', result.stderr)

    def test_active_list_failure_rolls_back_previous_extension(self):
        self.previous_install()
        result = self.run_installer(
            PREV_ENABLED=1, PREV_ACTIVE=0, FAIL_ACTIVE=1)
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.target / 'old-marker').is_file())
        self.assertIn('Previous extension state restored', result.stderr)

    def test_enabled_list_failure_rolls_back_previous_extension(self):
        self.previous_install()
        result = self.run_installer(
            PREV_ENABLED=1, PREV_ACTIVE=1, FAIL_ENABLED_LIST=1)
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.target / 'old-marker').is_file())
        self.assertIn('Previous extension state restored', result.stderr)

    def test_rollback_failure_is_reported(self):
        self.previous_install()
        result = self.run_installer(
            PREV_ENABLED=1, PREV_ACTIVE=1, FAIL_DISCOVERY=1,
            FAIL_ROLLBACK_MOVE=1)
        self.assertEqual(result.returncode, 2)
        self.assertIn('rollback failed', result.stderr)
        self.assert_agent_preserved()

    def test_failed_staged_switch_restores_previous_directory(self):
        self.previous_install()
        result = self.run_installer(
            PREV_ENABLED=1, PREV_ACTIVE=1, FAIL_SWITCH_MOVE=1)
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.target / 'old-marker').is_file())
        self.assertIn('directory and state remain in place', result.stderr)
        self.assert_agent_preserved()

    def test_failed_staged_switch_reports_restore_failure(self):
        self.previous_install()
        result = self.run_installer(
            PREV_ENABLED=1, PREV_ACTIVE=1, FAIL_SWITCH_MOVE=1,
            FAIL_ROLLBACK_MOVE=1)
        self.assertEqual(result.returncode, 2)
        self.assertIn('rollback failed after the staged switch failed', result.stderr)
        self.assert_agent_preserved()

    def test_first_install_failure_keeps_canonical_copy(self):
        result = self.run_installer(FAIL_DISCOVERY=1)
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.target / 'extension.js').is_file())
        self.assertTrue((self.target / 'metadata.json').is_file())
        self.assertIn('canonical UUID directory', result.stderr)
        self.assert_agent_preserved()
        next_login = self.run_installer()
        self.assertEqual(next_login.returncode, 0, next_login.stderr)

    def test_first_install_rollback_reports_unrestored_enabled_state(self):
        result = self.run_installer(FAIL_ACTIVE=1, FAIL_DISABLE=1)
        self.assertEqual(result.returncode, 2)
        self.assertIn('canonical UUID directory', result.stderr)
        self.assertTrue((self.home / '.gnome-test-enabled').is_file())
        self.assertIn('configured enabled; active runtime remains unproven', result.stderr)
        self.assert_agent_preserved()

    def previous_core(self, *, enabled=True, active=True):
        program = self.home / '.local/lib/sleep-disabler-gnome'
        program.mkdir(parents=True)
        (program / 'agent.py').write_text('AGENT_RUNTIME_VERSION = "0.4.0"\nAPI_VERSION = 4\n')
        (program / 'old-marker').write_text('previous core')
        import shlex
        (program / 'agent-launcher').write_text('#!/bin/sh\nexec ' +
            shlex.quote(str(self.stubs / 'test-python')) + ' ' + shlex.quote(str(program / 'agent.py')) + ' "$@"\n')
        cli = self.home / '.local/bin/sleep-disablerctl'
        cli.parent.mkdir(parents=True)
        cli.write_text('previous CLI')
        unit = self.home / '.config/systemd/user/sleep-disabler-gnome.service'
        unit.parent.mkdir(parents=True)
        unit.write_text('previous unit')
        (self.home / '.service-state').write_text(json.dumps({'enabled': enabled, 'active': active}))

    def test_core_validation_failure_leaves_previous_install_untouched(self):
        self.previous_core()
        (self.source / 'agent.py').write_text('invalid syntax !!!')
        result = self.run_installer()
        self.assertEqual(result.returncode, 1)
        self.assertTrue((self.home / '.local/lib/sleep-disabler-gnome/old-marker').exists())
        self.assertFalse((self.home / '.service-commands').exists())

    def test_core_health_failures_restore_files_service_and_api(self):
        for failure in ('CORE_API_FAIL', 'STALE_AGENT', 'BAD_API'):
            with self.subTest(failure=failure):
                # Reuse the restored prior installation between independent upgrades.
                if not (self.home / '.local/lib/sleep-disabler-gnome').exists():
                    self.previous_core()
                result = self.run_installer(**{failure: 1})
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertTrue((self.home / '.local/lib/sleep-disabler-gnome/old-marker').exists())
                self.assertEqual((self.home / '.local/bin/sleep-disablerctl').read_text(), 'previous CLI')
                self.assertEqual(json.loads((self.home / '.service-state').read_text()),
                                 {'enabled': True, 'active': True})
                self.assertIn('API=restored', result.stderr)

    def test_disabled_inactive_core_remains_disabled_inactive_on_rollback(self):
        self.previous_core(enabled=False, active=False)
        result = self.run_installer(CORE_API_FAIL=1)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads((self.home / '.service-state').read_text()),
                         {'enabled': False, 'active': False})

    def test_delayed_exact_agent_and_panel_runtime_readiness(self):
        result = self.run_installer(CORE_API_DELAY=2, PANEL_API_DELAY=2)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(int((self.home / '.core-api-count').read_text()), 3)
        self.assertGreaterEqual(int((self.home / '.panel-api-count').read_text()), 3)

    def test_wrong_panel_runtime_restores_previous_runtime(self):
        self.previous_install()
        result = self.run_installer(PREV_ENABLED=1, PREV_ACTIVE=1, PANEL_RUNTIME='4')
        self.assertEqual(result.returncode, 2)
        self.assertTrue((self.target / 'old-marker').exists())
        self.assertIn('Previous panel runtime proven restored', result.stderr)

    def test_custom_xdg_config_and_exact_interpreter_launchers(self):
        config = self.root / 'config with spaces $()'
        result = self.run_installer(XDG_CONFIG_HOME=config)
        self.assertEqual(result.returncode, 0, result.stderr)
        unit = config / 'systemd/user/sleep-disabler-gnome.service'
        self.assertIn('Type=dbus', unit.read_text())
        for path in (self.home / '.local/bin/sleep-disablerctl',
                     self.home / '.local/lib/sleep-disabler-gnome/agent-launcher'):
            self.assertIn(str(self.stubs / 'test-python'), path.read_text())

    def test_unregistered_matching_panel_is_not_proof(self):
        result = self.run_installer(PANEL_UNREGISTERED=1)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue((self.target / 'extension.js').exists())
        self.assertIn('not proven loaded', result.stderr)

    def test_invalid_panel_reports_retain_first_install(self):
        for report in ('', 'bogus', '4', '6'):
            with self.subTest(report=report):
                # Each attempt becomes an upgrade after the retained first install.
                result = self.run_installer(PANEL_RUNTIME=report)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertTrue((self.target / 'extension.js').exists())
                self.assert_agent_preserved()

    def test_panel_copy_failure_after_core_commit_is_exit_two(self):
        (self.source / 'extension' / UUID / 'extension.js').unlink()
        result = self.run_installer()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('file copy failed', result.stderr)
        self.assert_agent_preserved()

    def test_backup_archival_failure_does_not_misclassify_success(self):
        self.previous_install()
        result = self.run_installer(PREV_ENABLED=1, PREV_ACTIVE=1, FAIL_BACKUP_ARCHIVE=1)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Deployment healthy', result.stderr)
        self.assertTrue(list(self.target.parent.glob('.sleep-disabler-rollback.*')))

    def test_relative_xdg_paths_fail_before_core_mutation(self):
        for variable in ('XDG_CONFIG_HOME', 'XDG_DATA_HOME', 'XDG_STATE_HOME'):
            with self.subTest(variable=variable):
                result = self.run_installer(**{variable: 'relative'})
                self.assertEqual(result.returncode, 1)
                self.assertIn('absolute path', result.stderr)
                self.assertFalse((self.home / '.local/lib/sleep-disabler-gnome').exists())

    def test_unknown_prior_state_is_rejected_before_switch(self):
        self.previous_core()
        result = self.run_installer(UNKNOWN_SERVICE=1)
        self.assertEqual(result.returncode, 1)
        self.assertIn('cannot be restored safely', result.stderr)
        self.assertTrue((self.home / '.local/lib/sleep-disabler-gnome/old-marker').exists())

    def test_mutation_command_failure_restores_previous_core(self):
        self.previous_core()
        for command in ('daemon-reload', 'enable', 'restart'):
            with self.subTest(command=command):
                (self.home / '.systemctl-failed').unlink(missing_ok=True)
                result = self.run_installer(FAIL_SYSTEMCTL=command)
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertTrue((self.home / '.local/lib/sleep-disabler-gnome/old-marker').exists())
                self.assertIn('API=restored', result.stderr)

    def test_failed_first_core_has_no_active_files_or_enabled_claim(self):
        result = self.run_installer(CORE_API_FAIL=1)
        self.assertEqual(result.returncode, 1)
        self.assertFalse((self.home / '.local/lib/sleep-disabler-gnome').exists())
        self.assertFalse((self.home / '.local/bin/sleep-disablerctl').exists())
        self.assertEqual(json.loads((self.home / '.service-state').read_text()),
                         {'enabled': False, 'active': False})
        self.assertTrue(list((self.home / '.local/lib').glob('*.failed.*')))

    def test_unfinished_transaction_is_detected_before_switch(self):
        self.previous_core()
        record = self.home / '.local/lib/.sleep-disabler-core-transaction.json'
        record.write_text('{"interrupted": true}')
        result = self.run_installer()
        self.assertEqual(result.returncode, 1)
        self.assertIn('unfinished core transaction', result.stderr)
        self.assertEqual(record.read_text(), '{"interrupted": true}')
        self.assertTrue((self.home / '.local/lib/sleep-disabler-gnome/old-marker').exists())

    def test_prior_interpreter_is_used_after_python_changes(self):
        self.previous_core()
        alternate = self.stubs / 'alternate python $()'
        alternate.write_text((self.stubs / 'test-python').read_text())
        alternate.chmod(0o755)
        # New Python deliberately cannot probe the old API; the prior launcher can.
        alternate.write_text(alternate.read_text().replace(
            "exec \"" + str(self.stubs / 'installer-state') + "\"",
            'if [ -f "$HOME/.local/lib/sleep-disabler-gnome/old-marker" ]; then exit 1; fi; exec "' +
            str(self.stubs / 'installer-state') + '"'))
        result = self.run_installer(PYTHON=alternate, CORE_API_FAIL=1)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('API=restored', result.stderr)

    def test_whole_installer_is_serialized_before_core_and_panel(self):
        import fcntl
        self.previous_core()
        lock = self.home / '.local/lib/.sleep-disabler-install.lock'
        with lock.open('a') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_installer()
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn('another installer is running', result.stderr)
        self.assertTrue((self.home / '.local/lib/sleep-disabler-gnome/old-marker').exists())

    def test_signal_after_core_commit_before_shell_assignment_is_panel_exit_two(self):
        wrapper = self.stubs / 'test-python'
        content = wrapper.read_text()
        content = content.replace('exec "' + self.real_python + '" "$@"', '''for argument in "$@"; do
    if [ "$argument" = '--commit-receipt' ]; then
        "''' + self.real_python + '''" "$@"
        outcome=$?
        if [ "$outcome" -eq 0 ]; then kill -TERM "$PPID"; fi
        exit "$outcome"
    fi
done
exec "''' + self.real_python + '''" "$@"''')
        wrapper.write_text(content)
        result = self.run_installer()
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('core and CLI remain usable', result.stderr)
        self.assert_agent_preserved()
        service = json.loads((self.home / '.service-state').read_text())
        self.assertTrue(service['enabled'])
        self.assertTrue(service['active'])

    def test_installer_lock_remains_held_during_panel_activation(self):
        command = self.stubs / 'gnome-extensions'
        content = command.read_text().replace('  info)\n', '''  info)
    "$PYTHON_REAL" - <<'PYLOCK'
import fcntl, os
from pathlib import Path
path = Path(os.environ['HOME']) / '.local/lib/.sleep-disabler-install.lock'
with path.open('a') as stream:
    try:
        fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        (path.parent / '.panel-lock-proven').touch()
    else:
        raise SystemExit('installer lock was released before panel commit')
PYLOCK
    [ "$?" -eq 0 ] || exit 1
''')
        command.write_text(content)
        result = self.run_installer()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.home / '.local/lib/.panel-lock-proven').exists())

    def test_custom_xdg_config_unit_is_restored_after_failed_upgrade(self):
        self.previous_core()
        config = self.root / 'custom config'
        custom_unit = config / 'systemd/user/sleep-disabler-gnome.service'
        custom_unit.parent.mkdir(parents=True)
        old_unit = self.home / '.config/systemd/user/sleep-disabler-gnome.service'
        old_unit.rename(custom_unit)
        result = self.run_installer(XDG_CONFIG_HOME=config, CORE_API_FAIL=1)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertEqual(custom_unit.read_text(), 'previous unit')
        self.assertFalse(old_unit.exists())
        self.assertIn('API=restored', result.stderr)

    def test_signal_after_previous_panel_move_restores_directory(self):
        self.previous_install()
        result = self.run_installer(PREV_ENABLED=1, PREV_ACTIVE=1, SIGNAL_PANEL_SWITCH=1)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertTrue((self.target / 'old-marker').exists())
        self.assertIn('interrupted', result.stderr)
        self.assertIn('Previous extension state restored', result.stderr)
        self.assertFalse((self.target.parent / '.sleep-disabler-panel-transaction.json').exists())
        self.assert_agent_preserved()

    def test_signal_on_both_sides_of_panel_backup_and_replacement_restores_old_copy(self):
        self.previous_install()
        command = self.stubs / 'mv'
        content = command.read_text().replace('#!/bin/sh\n', '''#!/bin/sh
move=''
case "$2" in *sleep-disabler-rollback*) move=backup;; esac
case "$1" in *sleep-disabler-stage*) move=replacement;; esac
if [ -n "$move" ] && [ ! -f "$HOME/.panel-window-signalled" ]; then
    case "$SIGNAL_PANEL_WINDOW" in
      "$move-before")
        : > "$HOME/.panel-window-signalled"
        kill -"$SIGNAL_KIND" "$PPID"
        exit 0;;
      "$move-after")
        /bin/mv "$@" || exit 1
        : > "$HOME/.panel-window-signalled"
        kill -"$SIGNAL_KIND" "$PPID"
        exit 0;;
    esac
fi
''', 1)
        command.write_text(content)
        for signum in ('INT', 'TERM'):
            for move in ('backup', 'replacement'):
                for timing in ('before', 'after'):
                    with self.subTest(signal=signum, move=move, timing=timing):
                        (self.home / '.panel-window-signalled').unlink(missing_ok=True)
                        result = self.run_installer(PREV_ENABLED=1, PREV_ACTIVE=1,
                                                    SIGNAL_KIND=signum,
                                                    SIGNAL_PANEL_WINDOW=move + '-' + timing)
                        self.assertEqual(result.returncode, 2, result.stderr)
                        self.assertTrue((self.target / 'old-marker').exists())
                        self.assertIn('Previous extension state restored', result.stderr)
                        self.assertFalse((self.target.parent / '.sleep-disabler-panel-transaction.json').exists())
                        self.assert_agent_preserved()

    def test_unfinished_panel_record_blocks_retry_before_core(self):
        self.previous_core()
        self.target.parent.mkdir(parents=True)
        record = self.target.parent / '.sleep-disabler-panel-transaction.json'
        for contents in ('{"interrupted": true}', '{"target":', '', None):
            with self.subTest(contents=contents):
                if contents is None:
                    record.symlink_to(self.target.parent / 'missing-record')
                else:
                    record.write_text(contents)
                result = self.run_installer()
                self.assertEqual(result.returncode, 1, result.stderr)
                self.assertIn('Unfinished panel transaction', result.stderr)
                if contents is None:
                    self.assertTrue(record.is_symlink())
                else:
                    self.assertEqual(record.read_text(), contents)
                self.assertTrue((self.home / '.local/lib/sleep-disabler-gnome/old-marker').exists())
                record.rename(record.with_name(record.name + '.retained.' + str(contents is None) + '.' + str(len(contents or ''))))

    def test_interruptions_during_panel_intent_publication_retain_blocking_record(self):
        installer = self.source / 'install.sh'
        original = installer.read_text()
        record = self.target.parent / '.sleep-disabler-panel-transaction.json'
        for phase in ('partial', 'published'):
            with self.subTest(phase=phase):
                injected = original.replace("with open(record, 'x', encoding='utf-8') as stream:\n",
                    "with open(record, 'x', encoding='utf-8') as stream:\n"
                    "    if os.environ.get('RECORD_SIGNAL_PHASE') == 'partial':\n"
                    "        stream.write('{\\\"target\\\":')\n"
                    "        stream.flush()\n"
                    "        os.kill(os.getppid(), 15)\n"
                    "        raise SystemExit(1)\n")
                injected = injected.replace('    os.close(directory)\nPYRECORD',
                    "    os.close(directory)\n"
                    "if os.environ.get('RECORD_SIGNAL_PHASE') == 'published':\n"
                    "    os.kill(os.getppid(), 15)\nPYRECORD")
                installer.write_text(injected)
                result = self.run_installer(RECORD_SIGNAL_PHASE=phase)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertTrue(record.exists())
                self.assertFalse(self.target.exists())
                self.assert_agent_preserved()
                if phase == 'partial':
                    self.assertEqual(record.read_text(), '{"target":')
                else:
                    self.assertEqual(json.loads(record.read_text())['target'], str(self.target))
                retry = self.run_installer()
                self.assertEqual(retry.returncode, 1, retry.stderr)
                self.assertIn('Unfinished panel transaction', retry.stderr)
                record.rename(record.with_name(record.name + '.evidence.' + phase))
        installer.write_text(original)

    def test_failed_new_panel_retention_never_nests_old_backup(self):
        self.previous_install()
        result = self.run_installer(PREV_ENABLED=1, PREV_ACTIVE=1, PANEL_RUNTIME='4',
                                    FAIL_FAILED_RETAIN=1)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn('rollback failed', result.stderr)
        backups = list(self.target.parent.glob('.sleep-disabler-rollback.*'))
        self.assertEqual(len(backups), 1)
        self.assertTrue((backups[0] / 'old-marker').exists())
        self.assertFalse(list(self.target.glob('.sleep-disabler-rollback.*')))
        self.assertTrue((self.target.parent / '.sleep-disabler-panel-transaction.json').exists())

    def test_missing_node_is_reported_and_requires_actual_runtime_proof(self):
        # Restrict PATH to the isolated stubs plus explicitly required utilities.
        for command in ('dirname', 'install', 'mktemp', 'date', 'cat', 'sh'):
            (self.stubs / command).symlink_to(shutil.which(command))
        result = self.run_installer(PATH=self.stubs)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('syntax precheck skipped', result.stderr)
        self.assertIn('Exact staged top-bar runtime active', result.stdout)


if __name__ == '__main__':
    unittest.main()
