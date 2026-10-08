"""Core installer fault tests; all user-manager and bus calls are isolated."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import types
import io
from contextlib import contextmanager, redirect_stdout
import unittest
from unittest import mock

SOURCE = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('core_install_under_test', SOURCE / 'core_install.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class CoreTests(unittest.TestCase):
    def test_process_start_time_parses_stat_with_parenthesis_in_name(self):
        proc = self.root / '321'
        fields = ['S'] + ['0'] * 18 + ['12345']
        with mock.patch.object(Path, 'read_text', return_value='321 (shell ) worker) ' + ' '.join(fields)):
            self.assertEqual(core.process_start_time(proc), 12345)
        with mock.patch.object(Path, 'read_text', return_value='321 (shell) S 0'):
            with self.assertRaisesRegex(ValueError, 'identity cannot be established'):
                core.process_start_time(proc)

    def discovery_fixture(self, unit_paths, shell_data, *, changing_owner=False):
        manager = types.SimpleNamespace(Get=lambda _iface, _key, **_kwargs: unit_paths)
        owner_calls = iter((':1.shell', ':1.replaced', ':1.replaced',
                            ':1.replaced', ':1.replaced'))
        names = types.SimpleNamespace(
            GetNameOwner=lambda _name, **_kwargs: next(owner_calls) if changing_owner else ':1.shell',
            GetConnectionUnixUser=lambda _owner, **_kwargs: os.getuid(),
            GetConnectionUnixProcessID=lambda _owner, **_kwargs: 321)
        bus = types.SimpleNamespace(get_object=lambda name, *_args, **_kwargs:
                                    manager if name == 'org.freedesktop.systemd1' else names)
        fake = types.SimpleNamespace(SessionBus=lambda: bus, Interface=lambda value, _iface: value,
                                     Array=list, DBusException=type('BusError', (Exception,), {}))
        original_read = Path.read_bytes
        original_stat = Path.stat
        def read(path):
            if str(path) == '/proc/321/environ':
                return b'XDG_DATA_HOME=' + shell_data.encode() + b'\0'
            return original_read(path)
        def stat(path, *args, **kwargs):
            if str(path) == '/proc/321':
                return types.SimpleNamespace(st_uid=os.getuid())
            return original_stat(path, *args, **kwargs)
        def extension_command(command, **_kwargs):
            if command[:2] == ['gnome-extensions', 'info']:
                return subprocess.CompletedProcess(command, 1, '', 'not found')
            if command == ['gnome-extensions', 'list']:
                return subprocess.CompletedProcess(command, 0, '', '')
            self.fail('unexpected extension command: ' + str(command))
        @contextmanager
        def fake_session():
            with mock.patch.dict(sys.modules, {'dbus': fake}), \
                    mock.patch.object(core, 'run', side_effect=extension_command):
                yield
        @contextmanager
        def fake_proc():
            with mock.patch.object(Path, 'read_bytes', read), mock.patch.object(Path, 'stat', stat), \
                    mock.patch.object(core, 'process_start_time', return_value=12345):
                yield
        return fake_session(), fake_proc()

    def test_discovery_preflight_accepts_running_manager_and_shell_paths(self):
        unit_dir = self.root / 'config/systemd/user'
        data = self.root / 'shell-data'
        fake_bus, fake_proc = self.discovery_fixture([str(unit_dir)], str(data))
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), \
                fake_bus, fake_proc:
            core.preflight_discovery()

    def test_discovery_preflight_rejects_unit_path_alias(self):
        alias = self.root / 'config-alias'
        alias.symlink_to(self.root / 'config')
        data = self.root / 'shell-data'
        manager_path = self.root / 'config/systemd/user'
        fake_bus, fake_proc = self.discovery_fixture([str(manager_path)], str(data))
        with mock.patch.dict(os.environ, {'XDG_CONFIG_HOME': str(alias),
                                          'XDG_DATA_HOME': str(data)}), fake_bus, fake_proc:
            with self.assertRaisesRegex(ValueError, 'not in the running manager UnitPath'):
                core.preflight_discovery()

    def test_discovery_preflight_rejects_relative_manager_unit_path(self):
        selected = self.root / 'config/systemd/user'
        data = self.root / 'shell-data'
        fake_bus, fake_proc = self.discovery_fixture(['relative/units', str(selected)], str(data))
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), fake_bus, fake_proc:
            with self.assertRaisesRegex(ValueError, 'UnitPath contains a nonabsolute directory'):
                core.preflight_discovery()

    def test_discovery_preflight_rejects_earlier_unit_shadow(self):
        earlier = self.root / 'earlier-units'
        earlier.mkdir()
        (earlier / core.SERVICE).write_text('foreign unit')
        selected = self.root / 'config/systemd/user'
        data = self.root / 'shell-data'
        fake_bus, fake_proc = self.discovery_fixture([str(earlier), str(selected)], str(data))
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), fake_bus, fake_proc:
            with self.assertRaisesRegex(ValueError, 'earlier running-manager UnitPath shadows'):
                core.preflight_discovery()

    def test_discovery_preflight_rejects_earlier_unit_directory_alias(self):
        selected = self.root / 'config/systemd/user'
        selected.mkdir(parents=True, exist_ok=True)
        earlier = self.root / 'alias-units'
        earlier.symlink_to(selected)
        data = self.root / 'shell-data'
        fake_bus, fake_proc = self.discovery_fixture([str(earlier), str(selected)], str(data))
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), fake_bus, fake_proc:
            with self.assertRaisesRegex(ValueError, 'earlier running-manager UnitPath aliases'):
                core.preflight_discovery()

    def test_xdg_homes_reject_controls_before_discovery(self):
        for name, default in (('XDG_CONFIG_HOME', '.config'),
                              ('XDG_DATA_HOME', '.local/share'),
                              ('XDG_STATE_HOME', '.local/state')):
            for suffix in ('\nunsafe', '\tunsafe', '\x7funsafe', '\x85unsafe',
                           '\u200bunsafe'):
                with self.subTest(name=name, suffix=repr(suffix)), \
                        mock.patch.dict(os.environ, {name: str(self.root) + suffix}):
                    with self.assertRaisesRegex(ValueError, 'non-printable character'):
                        core.effective_xdg(name, default)

    def test_installation_home_requires_one_safe_absolute_value(self):
        for value in ('', 'relative-home', str(self.home) + '\nunsafe',
                      str(self.home) + '\tunsafe', str(self.home) + '\x7funsafe',
                      str(self.home) + '\x85unsafe', str(self.home) + '\u200bunsafe'):
            with self.subTest(value=repr(value)), mock.patch.dict(os.environ, {'HOME': value}):
                with self.assertRaisesRegex(ValueError, 'HOME must be'):
                    core.installation_home()
                with self.assertRaisesRegex(ValueError, 'HOME must be'):
                    core.CoreTransaction(self.source, sys.executable)

    def test_managed_parent_symlink_rejected_before_lock_directory_creation(self):
        alternate = self.root / 'alternate-home'
        (alternate / '.local').mkdir(parents=True)
        (alternate / '.local/bin').symlink_to(self.home / '.local/bin')
        with mock.patch.dict(os.environ, {'HOME': str(alternate)}):
            transaction = core.CoreTransaction(self.source, sys.executable)
            with self.assertRaisesRegex(ValueError, 'unsupported managed core parent'):
                transaction.validate_destinations()
            self.assertFalse(transaction.program.parent.exists())

    def test_discovery_preflight_uses_shell_home_for_default_data(self):
        unit_dir = self.root / 'config/systemd/user'
        fake_bus, fake_proc = self.discovery_fixture([str(unit_dir)], '')
        shell_env = b'HOME=' + str(self.home).encode() + b'\0'
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': ''}), fake_bus, fake_proc, \
                mock.patch.object(Path, 'read_bytes', return_value=shell_env):
            core.preflight_discovery()

    def test_discovery_preflight_rejects_ambiguous_shell_environment(self):
        data = self.root / 'shell-data'
        fake_bus, fake_proc = self.discovery_fixture(
            [str(self.root / 'config/systemd/user')], str(data))
        shell_env = (b'XDG_DATA_HOME=' + os.fsencode(data) + b'\0'
                     b'XDG_DATA_HOME=' + os.fsencode(self.root / 'shadow-data') + b'\0')
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), fake_bus, fake_proc, \
                mock.patch.object(Path, 'read_bytes', return_value=shell_env):
            with self.assertRaisesRegex(ValueError, 'duplicate XDG_DATA_HOME'):
                core.preflight_discovery()

    def test_discovery_preflight_rejects_mismatch_before_mutation(self):
        unit_dir = self.root / 'config/systemd/user'
        data = self.root / 'installer-only'
        fake_bus, fake_proc = self.discovery_fixture([str(unit_dir)], str(self.root / 'shell-data'))
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), fake_bus, fake_proc:
            with self.assertRaisesRegex(ValueError, 'differs from running GNOME Shell'):
                core.preflight_discovery()
        fake_bus, fake_proc = self.discovery_fixture([str(self.root / 'other-units')], str(data))
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), fake_bus, fake_proc:
            with self.assertRaisesRegex(ValueError, 'not in the running manager UnitPath'):
                core.preflight_discovery()

    def test_discovery_preflight_rechecks_shell_generation(self):
        data = self.root / 'shell-data'
        fake_bus, fake_proc = self.discovery_fixture(
            [str(self.root / 'config/systemd/user')], str(data), changing_owner=True)
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), fake_bus, fake_proc:
            core.preflight_discovery()

    def test_discovery_preflight_fails_when_shell_proc_is_unreadable(self):
        data = self.root / 'shell-data'
        fake_bus, _fake_proc = self.discovery_fixture(
            [str(self.root / 'config/systemd/user')], str(data))
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), fake_bus, \
                mock.patch.object(Path, 'read_bytes', side_effect=PermissionError('denied')):
            with self.assertRaisesRegex(ValueError, 'GNOME Shell environment is unavailable'):
                core.preflight_discovery()

    def test_discovery_preflight_rejects_shell_pid_uid_mismatch(self):
        data = self.root / 'shell-data'
        fake_bus, _fake_proc = self.discovery_fixture(
            [str(self.root / 'config/systemd/user')], str(data))
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), fake_bus, \
                mock.patch.object(Path, 'stat', return_value=types.SimpleNamespace(st_uid=os.getuid() + 1)):
            with self.assertRaisesRegex(ValueError, 'no longer belongs to its D-Bus user'):
                core.preflight_discovery()

    def test_discovery_preflight_rejects_reused_shell_pid(self):
        data = self.root / 'shell-data'
        fake_bus, fake_proc = self.discovery_fixture(
            [str(self.root / 'config/systemd/user')], str(data))
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), fake_bus, fake_proc, \
                mock.patch.object(core, 'process_start_time', side_effect=(1, 2, 3, 4)):
            with self.assertRaisesRegex(ValueError, 'owner changed during environment preflight'):
                core.preflight_discovery()

    def test_discovery_preflight_reports_unavailable_session_bus(self):
        bus_error = type('BusError', (Exception,), {})
        fake = types.SimpleNamespace(SessionBus=mock.Mock(side_effect=bus_error('offline')),
                                     DBusException=bus_error)
        with mock.patch.dict(sys.modules, {'dbus': fake}):
            with self.assertRaisesRegex(ValueError, 'Session discovery preflight unavailable'):
                core.preflight_discovery()

    def test_discovery_preflight_rejects_shadowed_extension(self):
        data = self.root / 'shell-data'
        fake_bus, fake_proc = self.discovery_fixture(
            [str(self.root / 'config/systemd/user')], str(data))
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), fake_bus, fake_proc, \
                mock.patch.object(core, 'discovered_extension_path', return_value=self.root / 'shadow'):
            with self.assertRaisesRegex(ValueError, 'shadows the selected UUID'):
                core.preflight_discovery()

    def test_discovery_preflight_rejects_symlink_alias_to_selected_extension(self):
        data = self.root / 'shell-data'
        target = data / 'gnome-shell/extensions/sleep-disabler@local'
        target.mkdir(parents=True)
        alias = self.root / 'shadow-extension'
        alias.symlink_to(target)
        fake_bus, fake_proc = self.discovery_fixture(
            [str(self.root / 'config/systemd/user')], str(data))
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(data)}), fake_bus, fake_proc, \
                mock.patch.object(core, 'discovered_extension_path', return_value=alias):
            with self.assertRaisesRegex(ValueError, 'shadows the selected UUID'):
                core.preflight_discovery()

    def test_discovery_preflight_accepts_canonical_selected_data_parent(self):
        real_data = self.root / 'real-data'
        real_data.mkdir()
        selected_data = self.root / 'selected-data'
        selected_data.symlink_to(real_data)
        canonical = real_data / 'gnome-shell/extensions/sleep-disabler@local'
        fake_bus, fake_proc = self.discovery_fixture(
            [str(self.root / 'config/systemd/user')], str(selected_data))
        with mock.patch.dict(os.environ, {'XDG_DATA_HOME': str(selected_data)}), fake_bus, fake_proc, \
                mock.patch.object(core, 'discovered_extension_path', return_value=canonical):
            core.preflight_discovery()

    def test_extension_info_requires_one_absolute_path(self):
        for output, expected in (('Path: /tmp/extension\n', Path('/tmp/extension')),
                                 ('Name: extension\n', ValueError),
                                 ('Path: relative\n', ValueError),
                                 ('Path: /tmp/a\nPath: /tmp/b\n', ValueError)):
            with self.subTest(output=output), mock.patch.object(core, 'run', return_value=
                    subprocess.CompletedProcess([], 0, output, '')):
                if expected is ValueError:
                    with self.assertRaises(ValueError):
                        core.discovered_extension_path('sleep-disabler@local')
                else:
                    self.assertEqual(core.discovered_extension_path('sleep-disabler@local'), expected)

    def test_extension_info_preserves_reported_symlink_path(self):
        target = self.root / 'selected-extension'
        target.mkdir()
        alias = self.root / 'shadow-extension'
        alias.symlink_to(target)
        with mock.patch.object(core, 'run', return_value=subprocess.CompletedProcess(
                [], 0, 'Path: ' + str(alias) + '\n', '')):
            self.assertEqual(core.discovered_extension_path('sleep-disabler@local'), alias)

    def test_missing_extension_path_requires_a_successful_discovery_list(self):
        info = subprocess.CompletedProcess([], 1, '', 'not found')
        for listed, message in ((subprocess.CompletedProcess([], 1, '', 'offline'), 'unavailable'),
                                (subprocess.CompletedProcess([], 0, 'sleep-disabler@local\n', ''),
                                 'cannot prove its path')):
            with self.subTest(message=message), mock.patch.object(core, 'run', side_effect=(info, listed)):
                with self.assertRaisesRegex(ValueError, message):
                    core.discovered_extension_path('sleep-disabler@local')
        with mock.patch.object(core, 'run', side_effect=(info, subprocess.CompletedProcess([], 0, '', ''))):
            self.assertIsNone(core.discovered_extension_path('sleep-disabler@local'))

    def test_previous_state_home_requires_evidence_for_legacy_unit(self):
        self.assertIsNone(core.previous_state_home(self.transaction.unit, None, False))
        old = self.root / 'prior state'
        self.transaction.unit.write_text('[Service]\nEnvironment=' +
                                         core.environment_assignment('XDG_STATE_HOME', old) + '\n')
        self.assertEqual(core.previous_state_home(self.transaction.unit, None, False), old)
        self.transaction.unit.write_text('[Install]\nEnvironment=' +
                                         core.environment_assignment('XDG_STATE_HOME', old) + '\n')
        self.assertIsNone(core.previous_state_home(self.transaction.unit, None, False))

    def test_inactive_unit_rejects_noncanonical_systemd_state_home_escapes(self):
        self.active = False
        self.enabled = False
        for assignment in ('"XDG_STATE_HOME=/tmp/with\\x20space"',
                           '"XDG_STATE_HOME=/tmp/%h"',
                           'XDG_STATE_HOME=/tmp/simple'):
            with self.subTest(assignment=assignment):
                self.transaction.unit.write_text('[Service]\nEnvironment=' + assignment + '\n')
                self.assertIsNone(core.previous_state_home(self.transaction.unit, None, False))
                with self.patches(), mock.patch.object(self.transaction, 'stage') as stage:
                    with self.assertRaisesRegex(ValueError, 'cannot be established'):
                        self.transaction.install()
                stage.assert_not_called()
                self.assertFalse(self.transaction.record.exists())

    def test_active_legacy_state_home_uses_process_not_unit_environment(self):
        unit_home = self.root / 'unit state'
        process_home = self.root / 'process state'
        self.transaction.unit.write_text('[Service]\nEnvironment=' +
                                         core.environment_assignment('XDG_STATE_HOME', unit_home) + '\n')
        pid = subprocess.CompletedProcess([], 0, '4242\n', '')
        with mock.patch.object(core, 'systemctl', return_value=pid), \
                mock.patch.object(Path, 'stat', return_value=types.SimpleNamespace(st_uid=os.getuid())), \
                mock.patch.object(core, 'process_start_time', return_value=12345), \
                mock.patch.object(Path, 'read_bytes', return_value=(
                    b'HOME=/tmp\0XDG_STATE_HOME=' + os.fsencode(process_home) + b'\0')):
            self.assertEqual(core.previous_state_home(self.transaction.unit, None, True), process_home)
        with mock.patch.object(core, 'systemctl', return_value=pid), \
                mock.patch.object(Path, 'stat', return_value=types.SimpleNamespace(st_uid=os.getuid())), \
                mock.patch.object(core, 'process_start_time', return_value=12345), \
                mock.patch.object(Path, 'read_bytes', side_effect=PermissionError('unreadable')):
            self.assertIsNone(core.previous_state_home(self.transaction.unit, None, True))

    def test_active_legacy_state_home_rejects_reused_pid(self):
        pid = subprocess.CompletedProcess([], 0, '4242\n', '')
        with mock.patch.object(core, 'systemctl', return_value=pid), \
                mock.patch.object(Path, 'stat', return_value=types.SimpleNamespace(st_uid=os.getuid())), \
                mock.patch.object(core, 'process_start_time', side_effect=(1, 2)), \
                mock.patch.object(Path, 'read_bytes', return_value=b'HOME=/tmp\0XDG_STATE_HOME=/tmp/old\0'):
            self.assertIsNone(core.previous_state_home(self.transaction.unit, None, True))

    def test_active_legacy_state_home_rejects_ambiguous_process_environment(self):
        pid = subprocess.CompletedProcess([], 0, '4242\n', '')
        values = b'HOME=/tmp\0XDG_STATE_HOME=/tmp/first\0XDG_STATE_HOME=/tmp/second\0'
        with mock.patch.object(core, 'systemctl', return_value=pid), \
                mock.patch.object(Path, 'stat', return_value=types.SimpleNamespace(st_uid=os.getuid())), \
                mock.patch.object(core, 'process_start_time', return_value=12345), \
                mock.patch.object(Path, 'read_bytes', return_value=values):
            self.assertIsNone(core.previous_state_home(self.transaction.unit, None, True))

    def test_state_home_transition_rejected_before_switch(self):
        old_unit = self.transaction.unit.read_text()
        old_state = self.root / 'old-state'
        with self.patches(), mock.patch.object(core, 'state', return_value={
                'agentRuntimeVersion': '0.4.0', 'apiVersion': 4,
                'panelRuntimeVersion': '', 'panelRuntimeRegistered': False,
                'stateHome': str(old_state), 'ownerPID': 4242}):
            with self.assertRaisesRegex(ValueError, 'differs from the active previous agent state home'):
                self.transaction.install()
        self.assertEqual(self.transaction.unit.read_text(), old_unit)
        self.assertFalse(self.transaction.record.exists())

    def test_inactive_state_home_transition_requires_no_recovery_record(self):
        old = self.root / 'old-state'
        new = self.root / 'new-state'
        core.verify_state_home_transition(old, new, False)
        for home in (old, new):
            directory = home / 'sleep-disabler'
            directory.mkdir(parents=True, exist_ok=True)
            for name in ('brightness-recovery.json', 'brightness-recovery.json.tmp'):
                with self.subTest(home=home, name=name):
                    record = directory / name
                    record.write_text('pending')
                    with self.assertRaisesRegex(ValueError, 'unresolved brightness recovery record'):
                        core.verify_state_home_transition(old, new, False)
                    record.unlink()
        with self.assertRaisesRegex(ValueError, 'active previous agent state home'):
            core.verify_state_home_transition(old, new, True)

    def test_inactive_state_home_transition_rejects_symlinked_state(self):
        old = self.root / 'old-state'
        new = self.root / 'new-state'
        (old / 'sleep-disabler').parent.mkdir(parents=True)
        (old / 'sleep-disabler').symlink_to(self.root / 'foreign-state')
        with self.assertRaisesRegex(ValueError, 'unsafe state directory'):
            core.verify_state_home_transition(old, new, False)

    def test_inactive_resolved_state_home_transition_can_install(self):
        old = self.root / 'old-state'
        self.transaction.unit.write_text(self.managed_prior_unit(old))
        self.active = False
        self.enabled = False
        with self.patches():
            self.assertEqual(self.transaction.install(), 0)
        self.assertTrue((self.transaction.program / 'agent.py').is_file())
        self.assertIn('XDG_STATE_HOME=', self.transaction.unit.read_text())
        self.assertTrue((self.transaction.record.with_name(
            self.transaction.record.name + '.committed.' + self.transaction.tag)).exists())

    def test_inactive_state_home_requires_exact_launcher_contract(self):
        old = self.root / 'old-state'
        managed = self.managed_prior_unit(old)
        path = self.transaction.program / 'agent-launcher'
        original_launcher = path.read_text()
        cases = (
            (managed.replace('ExecStart=' + core.unit_escape(path) + '\n', ''), original_launcher),
            (managed.replace('ExecStart=' + core.unit_escape(path),
                             'ExecStart=/usr/bin/env XDG_STATE_HOME=/tmp/other ' + str(path)),
             original_launcher),
            (managed + '[Install]\nExecStart=' + core.unit_escape(path) + '\n',
             original_launcher),
            (managed, '#!/bin/sh\nexec env XDG_STATE_HOME=/tmp/other ' +
             sys.executable + ' ' + str(self.transaction.program / 'agent.py') + ' "$@"\n'),
        )
        self.active = False
        self.enabled = False
        for unit_text, launcher_text in cases:
            with self.subTest(unit_text=unit_text, launcher_text=launcher_text):
                self.transaction.unit.write_text(unit_text)
                path.write_text(launcher_text)
                with self.patches(), mock.patch.object(self.transaction, 'stage') as stage:
                    with self.assertRaisesRegex(ValueError, 'launcher contract is unproven'):
                        self.transaction.install()
                stage.assert_not_called()
                self.assertFalse(self.transaction.record.exists())
        path.write_text(original_launcher)

    def test_inactive_unit_dropin_rejects_main_unit_state_home_proof(self):
        old = self.root / 'old-state'
        self.transaction.unit.write_text('[Service]\nEnvironment=' +
                                         core.environment_assignment('XDG_STATE_HOME', old) + '\n')
        self.active = False
        self.enabled = False
        def control(*arguments, **kwargs):
            if arguments[0] == 'show' and '--property=DropInPaths' in arguments:
                return subprocess.CompletedProcess(arguments, 0, '/tmp/override.conf\n', '')
            return self.systemctl(*arguments, **kwargs)
        with self.patches(), mock.patch.object(core, 'systemctl', side_effect=control), \
                mock.patch.object(self.transaction, 'stage') as stage:
            with self.assertRaisesRegex(ValueError, 'unproven environment drop-ins'):
                self.transaction.install()
        stage.assert_not_called()
        self.assertFalse(self.transaction.record.exists())

    def test_inactive_state_home_transition_rolls_back_old_contract(self):
        old = self.root / 'old-state'
        old_unit = self.managed_prior_unit(old)
        self.transaction.unit.write_text(old_unit)
        self.active = False
        self.enabled = False
        with self.patches(), mock.patch.object(core, 'ready', return_value=False):
            self.assertEqual(self.transaction.install(), 1)
        self.assertEqual(self.transaction.unit.read_text(), old_unit)
        self.assertFalse(self.active)
        self.assertFalse(self.enabled)

    def test_state_home_transition_rechecks_service_after_staging(self):
        old = self.root / 'old-state'
        old_unit = self.managed_prior_unit(old)
        self.transaction.unit.write_text(old_unit)
        self.active = False
        self.enabled = False
        original_stage = self.transaction.stage
        def start_during_stage():
            result = original_stage()
            self.active = True
            return result
        with self.patches(), mock.patch.object(self.transaction, 'stage', side_effect=start_during_stage):
            with self.assertRaisesRegex(ValueError, 'changed during staging'):
                self.transaction.install()
        self.assertEqual(self.transaction.unit.read_text(), old_unit)
        self.assertFalse(self.transaction.record.exists())
        self.assertFalse(any(command[0] in ('stop', 'restart', 'daemon-reload') for command in self.commands))

    def test_state_home_transition_rechecks_journal_after_staging(self):
        old = self.root / 'old-state'
        old_unit = self.managed_prior_unit(old)
        self.transaction.unit.write_text(old_unit)
        self.active = False
        self.enabled = False
        original_stage = self.transaction.stage
        record = old / 'sleep-disabler/brightness-recovery.json'
        def write_during_stage():
            result = original_stage()
            record.parent.mkdir(parents=True, exist_ok=True)
            record.write_text('pending')
            return result
        with self.patches(), mock.patch.object(self.transaction, 'stage', side_effect=write_during_stage):
            with self.assertRaisesRegex(ValueError, 'unresolved brightness recovery record'):
                self.transaction.install()
        self.assertEqual(self.transaction.unit.read_text(), old_unit)
        self.assertEqual(record.read_text(), 'pending')
        self.assertFalse(self.transaction.record.exists())

    def test_inactive_prior_unit_and_launcher_cannot_change_during_staging(self):
        self.transaction.unit.write_text(self.managed_prior_unit(self.transaction.state_home))
        self.active = False
        self.enabled = False
        launcher = self.transaction.program / 'agent-launcher'
        for changed, expected in ((self.transaction.unit, 'unit file changed'),
                                  (launcher, 'launcher changed')):
            with self.subTest(changed=changed):
                original_text = changed.read_text()
                original_stage = self.transaction.stage
                def change_during_stage():
                    result = original_stage()
                    changed.write_text(original_text + '\n# changed')
                    return result
                with self.patches(), mock.patch.object(self.transaction, 'stage',
                                                       side_effect=change_during_stage):
                    with self.assertRaisesRegex(ValueError, expected):
                        self.transaction.install()
                changed.write_text(original_text)
                self.assertFalse(self.transaction.record.exists())
                self.assertFalse(any(command[0] in ('stop', 'restart', 'daemon-reload')
                                     for command in self.commands))

    def test_inactive_prior_dropins_cannot_change_during_staging(self):
        self.transaction.unit.write_text(self.managed_prior_unit(self.transaction.state_home))
        self.active = False
        self.enabled = False
        original_stage = self.transaction.stage
        changed = False
        def change_during_stage():
            nonlocal changed
            result = original_stage()
            changed = True
            return result
        def control(*arguments, **kwargs):
            if arguments[0] == 'show' and '--property=DropInPaths' in arguments and changed:
                return subprocess.CompletedProcess(arguments, 0, '/tmp/override.conf\n', '')
            return self.systemctl(*arguments, **kwargs)
        with self.patches(), mock.patch.object(core, 'systemctl', side_effect=control), \
                mock.patch.object(self.transaction, 'stage', side_effect=change_during_stage):
            with self.assertRaisesRegex(ValueError, 'drop-ins changed during staging'):
                self.transaction.install()
        self.assertFalse(self.transaction.record.exists())
        self.assertFalse(any(command[0] in ('stop', 'restart', 'daemon-reload') for command in self.commands))

    def test_staging_cannot_replace_prior_unit_with_same_content_symlink(self):
        original_stage = self.transaction.stage
        alias = self.root / 'unit-alias.service'
        alias.write_bytes(self.transaction.unit.read_bytes())
        def replace_during_stage():
            result = original_stage()
            self.transaction.unit.unlink()
            self.transaction.unit.symlink_to(alias)
            return result
        with self.patches(), mock.patch.object(self.transaction, 'stage', side_effect=replace_during_stage):
            with self.assertRaisesRegex(ValueError, 'unsupported managed core destination'):
                self.transaction.install()
        self.assertFalse(self.transaction.record.exists())
        self.assertFalse(any(command[0] in ('stop', 'restart', 'daemon-reload') for command in self.commands))

    def test_loaded_fragment_rechecked_after_staging_without_state_home_change(self):
        original_stage = self.transaction.stage
        shadow = self.root / 'shadow.service'
        def change_fragment_during_stage():
            result = original_stage()
            shadow.write_text('foreign unit')
            return result
        def fragment(**_kwargs):
            return shadow if shadow.exists() else self.transaction.unit
        with self.patches(), mock.patch.object(self.transaction, 'stage',
                                               side_effect=change_fragment_during_stage), \
                mock.patch.object(core, 'loaded_fragment', side_effect=fragment):
            with self.assertRaisesRegex(ValueError, 'fragment changed during staging'):
                self.transaction.install()
        self.assertEqual(self.transaction.unit.read_text(), 'old unit')
        self.assertFalse(self.transaction.record.exists())
        self.assertFalse(any(command[0] in ('stop', 'restart', 'daemon-reload') for command in self.commands))

    def test_active_state_home_rechecked_after_staging_before_switch(self):
        original_stage = self.transaction.stage
        other_home = self.root / 'other-state'
        changed = False
        def change_state_home_during_stage():
            nonlocal changed
            result = original_stage()
            changed = True
            return result
        def current_state(python, **kwargs):
            value = self.state(python, **kwargs)
            if changed:
                value['stateHome'] = str(other_home)
            return value
        with self.patches(), mock.patch.object(self.transaction, 'stage',
                                               side_effect=change_state_home_during_stage), \
                mock.patch.object(core, 'state', side_effect=current_state):
            with self.assertRaisesRegex(ValueError, 'state home changed during staging'):
                self.transaction.install()
        self.assertEqual(self.transaction.unit.read_text(), 'old unit')
        self.assertFalse(self.transaction.record.exists())
        self.assertFalse(any(command[0] in ('stop', 'restart', 'daemon-reload') for command in self.commands))

    def test_active_runtime_owner_rechecked_after_staging_before_switch(self):
        original_stage = self.transaction.stage
        changed = False
        def change_owner_during_stage():
            nonlocal changed
            result = original_stage()
            changed = True
            return result
        def current_state(python, **kwargs):
            value = self.state(python, **kwargs)
            if changed:
                value['ownerPID'] = 9999
            return value
        with self.patches(), mock.patch.object(self.transaction, 'stage',
                                               side_effect=change_owner_during_stage), \
                mock.patch.object(core, 'state', side_effect=current_state):
            with self.assertRaisesRegex(ValueError, 'runtime changed during staging'):
                self.transaction.install()
        self.assertEqual(self.transaction.unit.read_text(), 'old unit')
        self.assertFalse(self.transaction.record.exists())
        self.assertFalse(any(command[0] in ('stop', 'restart', 'daemon-reload') for command in self.commands))

    def test_active_legacy_state_home_rechecked_after_staging_before_switch(self):
        with self.patches(), mock.patch.object(core, 'state', return_value=None), \
                mock.patch.object(core, 'previous_state_home', side_effect=(
                    self.transaction.state_home.resolve(), self.root / 'other-state')):
            with self.assertRaisesRegex(ValueError, 'state home changed during staging'):
                self.transaction.install()
        self.assertEqual(self.transaction.unit.read_text(), 'old unit')
        self.assertFalse(self.transaction.record.exists())
        self.assertFalse(any(command[0] in ('stop', 'restart', 'daemon-reload') for command in self.commands))

    def test_shadowed_unit_rejected_before_staging(self):
        old_unit = self.transaction.unit.read_text()
        with self.patches(), mock.patch.object(core, 'loaded_fragment',
                                                return_value=self.root / 'shadow.service'):
            with self.assertRaisesRegex(ValueError, 'shadows the managed unit'):
                self.transaction.install()
        self.assertEqual(self.transaction.unit.read_text(), old_unit)
        self.assertEqual(self.transaction.staged, {})

    def test_fragment_alias_is_not_exact_managed_unit(self):
        alias = self.root / 'loaded-through-alias.service'
        alias.symlink_to(self.transaction.unit)
        response = subprocess.CompletedProcess(['systemctl'], 0, str(alias) + '\n', '')
        with mock.patch.object(core, 'systemctl', return_value=response):
            reported = core.loaded_fragment()
        self.assertEqual(reported, alias)
        self.assertNotEqual(reported, self.transaction.unit)
        with self.patches(), mock.patch.object(core, 'loaded_fragment', return_value=reported), \
                mock.patch.object(self.transaction, 'stage') as stage:
            with self.assertRaisesRegex(ValueError, 'shadows the managed unit'):
                self.transaction.install()
        stage.assert_not_called()
        self.assertFalse(self.transaction.record.exists())

    def test_shadowed_unit_rejected_on_first_install_before_staging(self):
        self.transaction.unit.rename(self.root / 'retained-old-unit')
        self.transaction.program.rename(self.root / 'retained-old-program')
        self.transaction.cli.rename(self.root / 'retained-old-cli')
        with mock.patch.object(core, 'service_snapshot', return_value=(False, False)), \
                mock.patch.object(core, 'loaded_fragment', return_value=self.root / 'shadow.service'), \
                mock.patch.object(self.transaction, 'stage') as stage:
            with self.assertRaisesRegex(ValueError, 'shadows the managed unit'):
                self.transaction.install()
        stage.assert_not_called()
        self.assertFalse(self.transaction.record.exists())

    def test_first_install_rejects_failed_fragment_query_before_staging(self):
        self.transaction.unit.unlink()
        self.transaction.program.rename(self.root / 'retained-old-program')
        self.transaction.cli.rename(self.root / 'retained-old-cli')
        self.enabled = False
        self.active = False
        def control(*arguments, **kwargs):
            if arguments[0] == 'show':
                return subprocess.CompletedProcess(arguments, 1, '', 'manager unavailable')
            return self.systemctl(*arguments, **kwargs)
        with self.patches(), mock.patch.object(core, 'systemctl', side_effect=control), \
                mock.patch.object(self.transaction, 'stage') as stage:
            with self.assertRaisesRegex(ValueError, 'fragment query failed'):
                self.transaction.install()
        stage.assert_not_called()
        self.assertFalse(self.transaction.record.exists())

    def test_unit_environment_renderer_rejects_unsafe_values(self):
        value = '/tmp/quoted " path\\name % Café'
        rendered = core.environment_assignment('XDG_STATE_HOME', value)
        self.assertIn('%%', rendered)
        self.transaction.unit.write_text('[Service]\nEnvironment=' + rendered + '\n')
        self.assertEqual(core.previous_state_home(self.transaction.unit, None, False), Path(value).resolve())
        for value in ('relative/path', '/tmp/new\nline', '/tmp/control\x00path',
                      '/tmp/control\x85path', '/tmp/zero\u200bwidth'):
            with self.subTest(value=value), self.assertRaises(ValueError):
                core.environment_assignment('XDG_STATE_HOME', value)

    def test_systemd_accepts_special_state_home_assignment_when_available(self):
        analyzer = shutil.which('systemd-analyze')
        if not analyzer:
            self.skipTest('systemd-analyze unavailable')
        rendered = core.environment_assignment('XDG_STATE_HOME', '/tmp/quoted " path\\name % Café')
        candidate = self.root / 'state-home-quoting.service'
        candidate.write_text('[Unit]\nDescription=Unit quoting check\n[Service]\nType=oneshot\n'
                             'ExecStart=/bin/true\nEnvironment=' + rendered + '\n')
        checked = subprocess.run([analyzer, '--user', 'verify', str(candidate)],
                                 text=True, capture_output=True)
        sandbox_errors = {
            'Failed to turn off SO_PASSRIGHTS on user lookup socket, ignoring: Operation not permitted',
            'Failed to enable SO_PASSCRED on handoff timestamp socket: Operation not permitted',
        }
        diagnostics = set(checked.stderr.splitlines())
        if checked.returncode and diagnostics and diagnostics <= sandbox_errors:
            self.skipTest('systemd verifier blocked by sandbox socket restrictions')
        self.assertEqual(checked.returncode, 0, checked.stderr)

    def test_ambiguous_inactive_unit_state_home_rejected_before_switch(self):
        original = ('[Service]\nEnvironment=' +
                    core.environment_assignment('XDG_STATE_HOME', self.root / 'old-state') + '\n')
        for suffix in ('Environment="XDG_STATE_HOME=/tmp/other"\n',
                       '  Environment="XDG_STATE_HOME=/tmp/other"\n',
                       'Environment=\n',
                       'EnvironmentFile=/tmp/override.env\n',
                       '  EnvironmentFile=/tmp/override.env\n',
                       'EnvironmentFile = /tmp/override.env\n',
                       'Environment=OTHER=continued\\\n XDG_STATE_HOME=/tmp/other\n',
                       'PAMName=login\n',
                       'UnsetEnvironment=XDG_STATE_HOME\n'):
            with self.subTest(suffix=suffix):
                self.transaction.unit.write_text(original + suffix)
                self.assertIsNone(core.previous_state_home(self.transaction.unit, None, False))
                self.active = False
                self.enabled = False
                with self.patches(), mock.patch.object(self.transaction, 'stage') as stage:
                    with self.assertRaisesRegex(ValueError, 'state home cannot be established'):
                        self.transaction.install()
                stage.assert_not_called()
                self.assertFalse(self.transaction.record.exists())

    def test_unsupported_managed_destinations_preserved_before_staging(self):
        for target in (self.transaction.program, self.transaction.cli, self.transaction.unit):
            saved = target.with_name(target.name + '.saved')
            target.rename(saved)
            for link_target in (saved, self.root / 'missing-target'):
                with self.subTest(target=target, link_target=link_target):
                    target.symlink_to(link_target)
                    with self.patches(), mock.patch.object(self.transaction, 'stage') as stage:
                        with self.assertRaisesRegex(ValueError, 'unsupported managed core destination'):
                            self.transaction.install()
                    stage.assert_not_called()
                    self.assertEqual(target.readlink(), link_target)
                    self.assertFalse(self.commands)
                    target.rename(target.with_name(target.name + '.link.' + str(link_target == saved)))
            if target == self.transaction.program:
                target.write_text('wrong type')
            else:
                target.mkdir()
            with self.patches(), mock.patch.object(self.transaction, 'stage') as stage:
                with self.assertRaisesRegex(ValueError, 'unsupported managed core destination'):
                    self.transaction.install()
            stage.assert_not_called()
            self.assertFalse(self.commands)
            target.rename(target.with_name(target.name + '.wrong-type'))
            saved.rename(target)
        self.assert_old_restored()

    def test_dangling_transaction_record_blocks_before_staging(self):
        self.transaction.record.symlink_to(self.root / 'missing-record-target')
        with self.patches(), mock.patch.object(self.transaction, 'stage') as stage:
            with self.assertRaisesRegex(ValueError, 'unfinished core transaction'):
                self.transaction.install()
        stage.assert_not_called()
        self.assertTrue(self.transaction.record.is_symlink())
        self.assert_old_restored()
        self.assertFalse(self.commands)

    def test_missing_unit_requires_independently_absent_managed_artifacts(self):
        unit = self.root / 'missing.service'
        program = self.root / 'orphan-program'
        cli = self.root / 'orphan-cli'
        answers = [subprocess.CompletedProcess([], 4, 'not-found\n', ''),
                   subprocess.CompletedProcess([], 3, 'inactive\n', '')]
        def snapshot():
            with mock.patch.object(core, 'systemctl', side_effect=answers):
                return core.service_snapshot(unit, (program, cli))
        self.assertEqual(snapshot(), (False, False))
        program.mkdir()
        with self.assertRaisesRegex(ValueError, 'enablement cannot be restored'):
            snapshot()
        program.rename(self.root / 'retained-program')
        cli.write_text('orphan')
        with self.assertRaisesRegex(ValueError, 'enablement cannot be restored'):
            snapshot()
        cli.rename(self.root / 'retained-cli')
        unit.symlink_to(self.root / 'absent-target')
        with self.assertRaisesRegex(ValueError, 'enablement cannot be restored'):
            snapshot()

    def test_target_ordering_cycle_rejected_before_switch(self):
        path = self.source / core.SERVICE
        path.write_text(path.read_text().replace('[Unit]', '[Unit]\nAfter=graphical-session.target'))
        with self.patches():
            with self.assertRaisesRegex(ValueError, 'target ordering'):
                self.transaction.install()
        self.assert_old_restored()
        self.assertFalse(self.transaction.record.exists())

    def test_wrong_service_section_rejected_without_external_verifier(self):
        path = self.source / core.SERVICE
        path.write_text(path.read_text().replace('Type=dbus', 'Type=simple') + '\n[Unit]\nType=dbus\n')
        with self.patches(), mock.patch.object(core.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(ValueError, 'service structure'):
                self.transaction.install()
        self.assert_old_restored()
        self.assertFalse(self.transaction.record.exists())

    def test_wrong_inherited_lock_descriptor_rejected(self):
        lock_path = self.root / 'installer.lock'
        lock_path.touch()
        with (self.root / 'different.lock').open('a') as other, \
                mock.patch.dict(os.environ, {'SLEEP_DISABLER_INSTALL_LOCK_FD': str(other.fileno())}):
            with self.assertRaisesRegex(ValueError, 'invalid inherited installer lock'):
                with core.installation_lock(lock_path):
                    self.fail('wrong lock must not enter deployment')

    def test_raw_state_probe_rejects_coercible_malformed_types(self):
        class Boolean(int):
            pass
        class UInt32(int):
            pass
        valid = {'agentRuntimeVersion': '0.5.0', 'apiVersion': UInt32(5),
                 'panelRuntimeVersion': '5', 'panelRuntimeRegistered': Boolean(1)}
        snapshot = dict(valid)
        app = types.SimpleNamespace(GetState=lambda **_kwargs: snapshot)
        daemon = types.SimpleNamespace(GetConnectionUnixProcessID=lambda *_args, **_kwargs: 4242)
        bus = types.SimpleNamespace(get_name_owner=lambda _name: ':1.agent',
                                    get_object=lambda name, *_args, **_kwargs:
                                    daemon if name == 'org.freedesktop.DBus' else app)
        fake = types.SimpleNamespace(Boolean=Boolean, SessionBus=lambda: bus,
                                     Interface=lambda obj, _iface: obj)
        def probe():
            output = io.StringIO()
            with mock.patch.dict(sys.modules, {'dbus': fake}), redirect_stdout(output):
                exec(core.STATE_SCRIPT, {})
            return json.loads(output.getvalue())
        self.assertEqual(probe()['apiVersion'], 5)
        self.assertIs(probe()['panelRuntimeRegistered'], True)
        for key, values in {'apiVersion': ['5', 5.0, 5.5, True, Boolean(1)],
                            'panelRuntimeRegistered': ['false', 'true', 0, 1],
                            'agentRuntimeVersion': [5, None],
                            'panelRuntimeVersion': [5, None]}.items():
            for value in values:
                with self.subTest(key=key, value=value):
                    snapshot = dict(valid, **{key: value})
                    with self.assertRaises(ValueError):
                        probe()
        snapshot = {}
        self.assertEqual(probe(), {'agentRuntimeVersion': '', 'apiVersion': 0,
                                  'panelRuntimeVersion': '', 'panelRuntimeRegistered': False,
                                  'stateHome': '', 'ownerPID': 4242})
        owners = iter((':1.agent', ':1.replaced'))
        bus.get_name_owner = lambda _name: next(owners)
        with self.assertRaisesRegex(ValueError, 'owner changed'):
            probe()
        bus.get_name_owner = lambda _name: ':1.agent'
        daemon.GetConnectionUnixProcessID = lambda *_args, **_kwargs: 0
        with self.assertRaisesRegex(ValueError, 'Invalid runtime bus owner PID'):
            probe()

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'agent.py').write_text('AGENT_RUNTIME_VERSION = "0.6.0"\nAPI_VERSION = 6\n')
        (self.source / 'ctl.py').write_text('print("new cli")\n')
        (self.source / core.SERVICE).write_text((SOURCE / core.SERVICE).read_text())
        environment = mock.patch.dict(os.environ, {'HOME': str(self.home),
                                                    'XDG_CONFIG_HOME': str(self.root / 'config')})
        environment.start()
        self.addCleanup(environment.stop)
        self.transaction = core.CoreTransaction(self.source, sys.executable)
        self.enabled = True
        self.active = True
        self.commands = []
        for target in (self.transaction.program, self.transaction.cli, self.transaction.unit):
            target.parent.mkdir(parents=True, exist_ok=True)
        self.transaction.program.mkdir()
        (self.transaction.program / 'old-marker').write_text('old core')
        (self.transaction.program / 'agent.py').write_text('AGENT_RUNTIME_VERSION = "0.4.0"\nAPI_VERSION = 4\n')
        (self.transaction.program / 'agent-launcher').write_text(
            core.launcher(sys.executable, self.transaction.program / 'agent.py'))
        self.transaction.cli.write_text('old cli')
        self.transaction.unit.write_text('old unit')

    def managed_prior_unit(self, state_home):
        return ('[Service]\nExecStart=' +
                core.unit_escape(self.transaction.program / 'agent-launcher') +
                '\nEnvironment=' + core.environment_assignment('XDG_STATE_HOME', state_home) + '\n')

    def systemctl(self, *arguments, **_kwargs):
        self.commands.append(arguments)
        command = arguments[0]
        if command == 'is-enabled':
            return subprocess.CompletedProcess(arguments, 0 if self.enabled else 1,
                                               'enabled\n' if self.enabled else 'disabled\n', '')
        if command == 'is-active':
            return subprocess.CompletedProcess(arguments, 0 if self.active else 3,
                                               'active\n' if self.active else 'inactive\n', '')
        if command == 'show':
            if '--property=DropInPaths' in arguments:
                return subprocess.CompletedProcess(arguments, 0, '', '')
            if '--property=MainPID' in arguments:
                return subprocess.CompletedProcess(arguments, 0, '4242\n', '')
            return subprocess.CompletedProcess(arguments, 0, str(self.transaction.unit) + '\n', '')
        if command in ('enable', 'disable'):
            self.enabled = command == 'enable'
        if command in ('restart', 'start', 'stop'):
            self.active = command != 'stop'
        return subprocess.CompletedProcess(arguments, 0, '', '')

    def state(self, _python, **_kwargs):
        if not self.active:
            return None
        old = (self.transaction.program / 'old-marker').exists()
        return {'agentRuntimeVersion': '0.4.0' if old else '0.6.0',
                'apiVersion': 4 if old else 6, 'panelRuntimeVersion': '',
                'panelRuntimeRegistered': False,
                'stateHome': str(self.transaction.state_home), 'ownerPID': 4242}

    def patches(self):
        from contextlib import ExitStack
        stack = ExitStack()
        stack.enter_context(mock.patch.object(core, 'systemctl', side_effect=self.systemctl))
        stack.enter_context(mock.patch.object(core, 'state', side_effect=self.state))
        stack.enter_context(mock.patch.object(core, 'run', return_value=
            subprocess.CompletedProcess([], 0, '', '')))
        return stack

    def assert_old_restored(self):
        self.assertTrue((self.transaction.program / 'old-marker').exists())
        self.assertEqual(self.transaction.cli.read_text(), 'old cli')
        self.assertEqual(self.transaction.unit.read_text(), 'old unit')
        self.assertTrue(self.enabled)
        self.assertTrue(self.active)

    def test_each_switch_failure_restores_all_previous_artifacts(self):
        original_rename = Path.rename
        for destination in (self.transaction.program, self.transaction.cli, self.transaction.unit):
            with self.subTest(destination=destination):
                self.transaction = core.CoreTransaction(self.source, sys.executable)
                failed = False
                def rename(path, target):
                    nonlocal failed
                    if Path(target) == destination and 'stage' in path.name and not failed:
                        failed = True
                        raise OSError('injected switch failure')
                    return original_rename(path, target)
                with self.patches(), mock.patch.object(Path, 'rename', rename):
                    self.assertEqual(self.transaction.install(), 1)
                self.assertTrue(failed)
                self.assert_old_restored()
                self.assertFalse(self.transaction.record.exists())

    def test_sigterm_during_restart_rolls_back_before_restoring_handler(self):
        original_handler = signal.getsignal(signal.SIGTERM)
        failed = False
        def control(*arguments, **kwargs):
            nonlocal failed
            if arguments[0] == 'restart' and not failed:
                failed = True
                signal.raise_signal(signal.SIGTERM)
            return self.systemctl(*arguments, **kwargs)
        with self.patches(), mock.patch.object(core, 'systemctl', side_effect=control):
            self.assertEqual(self.transaction.install(), 1)
        self.assert_old_restored()
        self.assertEqual(signal.getsignal(signal.SIGTERM), original_handler)
        self.assertFalse(self.transaction.record.exists())

    def test_interruptions_on_both_sides_of_every_core_switch_restore_prior_install(self):
        original_rename = Path.rename
        for signum in (signal.SIGINT, signal.SIGTERM):
            for artifact in ('program', 'cli', 'unit'):
                for move in ('backup', 'replacement'):
                    for timing in ('before', 'after'):
                        with self.subTest(signal=signum, artifact=artifact, move=move, timing=timing):
                            self.transaction = core.CoreTransaction(self.source, sys.executable)
                            target = getattr(self.transaction, artifact)
                            delivered = False
                            def rename(path, destination):
                                nonlocal delivered
                                relevant = (path == target and '.previous.' in str(destination)
                                            if move == 'backup' else
                                            Path(destination) == target and 'stage' in path.name)
                                if relevant and not delivered:
                                    delivered = True
                                    if timing == 'before':
                                        signal.raise_signal(signum)
                                    result = original_rename(path, destination)
                                    if timing == 'after':
                                        signal.raise_signal(signum)
                                    return result
                                return original_rename(path, destination)
                            with self.patches(), mock.patch.object(Path, 'rename', rename):
                                self.assertEqual(self.transaction.install(), 1)
                            self.assertTrue(delivered)
                            self.assert_old_restored()
                            self.assertFalse(self.transaction.record.exists())

    def test_stale_commit_receipt_is_rejected_before_staging_or_mutation(self):
        receipt = self.root / 'receipt.json'
        receipt.write_text('{"committed": true, "transaction": "old"}')
        self.transaction.commit_receipt = receipt
        with self.patches(), mock.patch.object(self.transaction, 'stage') as stage:
            with self.assertRaisesRegex(ValueError, 'prior core commit receipt'):
                self.transaction.install()
            stage.assert_not_called()
        self.assertEqual(json.loads(receipt.read_text())['transaction'], 'old')
        self.assert_old_restored()
        self.assertEqual(self.commands, [])

    def test_signal_immediately_after_each_backup_move_restores_original(self):
        original_rename = Path.rename
        for artifact in ('program', 'cli', 'unit'):
            with self.subTest(artifact=artifact):
                self.transaction = core.CoreTransaction(self.source, sys.executable)
                target = getattr(self.transaction, artifact)
                delivered = False
                def rename(path, destination):
                    nonlocal delivered
                    result = original_rename(path, destination)
                    if path == target and '.previous.' in str(destination) and not delivered:
                        delivered = True
                        signal.raise_signal(signal.SIGTERM)
                    return result
                with self.patches(), mock.patch.object(Path, 'rename', rename):
                    self.assertEqual(self.transaction.install(), 1)
                self.assertTrue(delivered)
                self.assert_old_restored()

    def test_partial_rollback_keeps_transaction_evidence(self):
        def control(*arguments, **kwargs):
            if arguments[0] == 'enable':
                return subprocess.CompletedProcess(arguments, 1, '', 'cannot enable')
            return self.systemctl(*arguments, **kwargs)
        with self.patches(), mock.patch.object(core, 'systemctl', side_effect=control), \
                mock.patch('sys.stderr') as stderr:
            self.assertEqual(self.transaction.install(), 1)
        self.assertTrue(self.transaction.record.exists())
        record = json.loads(self.transaction.record.read_text())
        self.assertEqual(record['priorInterpreter'], sys.executable)
        self.assertEqual(len(record['artifacts']), 3)
        output = ''.join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn('enablement=FAILED', output)
        self.assertIn('API=restored', output)
        self.assert_old_restored()

    def test_rollback_proves_previous_state_home_with_runtime(self):
        prior = self.state(sys.executable)
        with self.patches(), mock.patch.object(core, 'ready', return_value=True) as readiness, \
                mock.patch('sys.stderr') as stderr:
            self.assertTrue(self.transaction.rollback(True, True, prior, sys.executable,
                                                      self.transaction.unit))
        self.assertEqual(readiness.call_args.kwargs['state_home'], prior['stateHome'])
        self.assertEqual(readiness.call_args.kwargs['unit'], self.transaction.unit)
        output = ''.join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn('state-home=restored', output)

    def test_rollback_rejects_shadowed_restored_unit(self):
        prior = self.state(sys.executable)
        with self.patches(), mock.patch.object(core, 'loaded_fragment',
                                               return_value=self.root / 'shadow.service'), \
                mock.patch('sys.stderr') as stderr:
            self.assertFalse(self.transaction.rollback(True, True, prior, sys.executable,
                                                       self.transaction.unit))
        output = ''.join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn('fragment=FAILED', output)

    def test_install_keeps_rollback_record_when_restored_fragment_is_shadowed(self):
        fragments = iter((self.transaction.unit, self.transaction.unit,
                          self.root / 'shadow.service'))
        with self.patches(), mock.patch.object(core, 'loaded_fragment',
                                               side_effect=lambda **_kwargs: next(fragments)), \
                mock.patch.object(core, 'ready', return_value=False), \
                mock.patch('sys.stderr') as stderr:
            self.assertEqual(self.transaction.install(), 1)
        self.assert_old_restored()
        self.assertTrue(self.transaction.record.exists())
        output = ''.join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn('fragment=FAILED', output)

    def test_rollback_does_not_treat_failed_fragment_query_as_absence(self):
        self.transaction.unit.unlink()
        self.enabled = False
        self.active = False
        def control(*arguments, **kwargs):
            if arguments[0] == 'show':
                return subprocess.CompletedProcess(arguments, 1, '', 'manager unavailable')
            return self.systemctl(*arguments, **kwargs)
        with self.patches(), mock.patch.object(core, 'systemctl', side_effect=control), \
                mock.patch('sys.stderr') as stderr:
            self.assertFalse(self.transaction.rollback(False, False, None, None, None))
        output = ''.join(call.args[0] for call in stderr.write.call_args_list)
        self.assertIn('fragment=FAILED', output)

    def test_readiness_has_a_total_deadline(self):
        with mock.patch.object(core.time, 'monotonic', side_effect=[0, 1, 71, 71]), \
                mock.patch.object(core, 'systemctl', return_value=
                    subprocess.CompletedProcess([], 0, '', '')) as control, \
                mock.patch.object(core, 'state') as state:
            self.assertFalse(core.ready(sys.executable, '0.6.0', 6))
        control.assert_called_once()
        state.assert_not_called()

    def test_panel_readiness_covers_registration_retry_envelope(self):
        polls = 0
        def panel_state(_python, *, timeout):
            nonlocal polls
            polls += 1
            return {'agentRuntimeVersion': '0.6.0', 'apiVersion': 6,
                    'panelRuntimeRegistered': polls >= 20, 'panelRuntimeVersion': '6',
                    'stateHome': str(self.transaction.state_home), 'ownerPID': 4242}
        with mock.patch.object(core, 'systemctl', side_effect=lambda *args, **_kwargs:
                subprocess.CompletedProcess(args, 0, '4242\n' if '--property=MainPID' in args else '', '')), \
                mock.patch.object(core, 'state', side_effect=panel_state), \
                mock.patch.object(core.time, 'sleep') as pause:
            self.assertTrue(core.ready(sys.executable, '0.6.0', 6, '6'))
        self.assertEqual(polls, 20)
        self.assertEqual(pause.call_count, 19)

    def test_readiness_requires_selected_state_home_and_fragment(self):
        with self.patches(), mock.patch.object(core, 'READINESS_ATTEMPTS', 1):
            self.active = True
            self.assertTrue(core.ready(sys.executable, '0.4.0', 4,
                                       state_home=self.transaction.state_home,
                                       unit=self.transaction.unit))
            self.assertFalse(core.ready(sys.executable, '0.4.0', 4,
                                        state_home=self.root / 'other-state',
                                        unit=self.transaction.unit))
            with mock.patch.object(core, 'loaded_fragment', return_value=self.root / 'shadow.service'):
                self.assertFalse(core.ready(sys.executable, '0.4.0', 4,
                                            state_home=self.transaction.state_home,
                                            unit=self.transaction.unit))
            alias = self.root / 'fragment-alias.service'
            alias.symlink_to(self.transaction.unit)
            with mock.patch.object(core, 'loaded_fragment', return_value=alias):
                self.assertFalse(core.ready(sys.executable, '0.4.0', 4,
                                            state_home=self.transaction.state_home,
                                            unit=self.transaction.unit))

    def test_readiness_rejects_api_from_another_process(self):
        with self.patches(), mock.patch.object(core, 'READINESS_ATTEMPTS', 1):
            self.active = True
            with mock.patch.object(core, 'service_main_pid', return_value=9999):
                self.assertFalse(core.ready(sys.executable, '0.4.0', 4,
                                            state_home=self.transaction.state_home,
                                            unit=self.transaction.unit))

    def test_readiness_rejects_pid_proof_that_finishes_after_deadline(self):
        clock = {'now': 0}
        def slow_pid(**_kwargs):
            clock['now'] = core.READINESS_TIMEOUT + 1
            return 4242
        with self.patches(), mock.patch.object(core, 'READINESS_ATTEMPTS', 1), \
                mock.patch.object(core.time, 'monotonic', side_effect=lambda: clock['now']), \
                mock.patch.object(core, 'service_main_pid', side_effect=slow_pid):
            self.active = True
            self.assertFalse(core.ready(sys.executable, '0.4.0', 4,
                                        state_home=self.transaction.state_home,
                                        unit=self.transaction.unit))

    def test_install_rejects_prior_api_from_another_process_before_mutation(self):
        old_unit = self.transaction.unit.read_text()
        with self.patches(), mock.patch.object(core, 'service_main_pid', return_value=9999):
            with self.assertRaisesRegex(ValueError, 'API owner does not match'):
                self.transaction.install()
        self.assertEqual(self.transaction.unit.read_text(), old_unit)
        self.assertFalse(self.transaction.record.exists())

    def test_commit_receipt_is_published_after_readiness_with_signals_ignored(self):
        receipt = self.root / 'commit-receipt.json'
        self.transaction.commit_receipt = receipt
        original = self.transaction.acknowledge_commit
        def acknowledge(version, api):
            self.assertEqual(signal.getsignal(signal.SIGTERM), signal.SIG_IGN)
            self.assertEqual(signal.getsignal(signal.SIGINT), signal.SIG_IGN)
            self.assertEqual(self.state(sys.executable)['agentRuntimeVersion'], version)
            original(version, api)
        with self.patches(), mock.patch.object(self.transaction, 'acknowledge_commit', side_effect=acknowledge):
            self.assertEqual(self.transaction.install(), 0)
        self.assertTrue(json.loads(receipt.read_text())['committed'])
        self.assertFalse(self.transaction.record.exists())

    def test_partial_file_running_or_api_rollback_reports_dimensions_independently(self):
        original_rename = Path.rename
        for dimension in ('files', 'running-state', 'API'):
            with self.subTest(dimension=dimension):
                transaction = core.CoreTransaction(self.source, sys.executable)
                # Stage and switch using the normal transaction, then inject a
                # readiness failure and one distinct rollback failure.
                def rename(path, destination):
                    if dimension == 'files' and '.previous.' in path.name and Path(destination) == transaction.cli:
                        raise OSError('cannot restore CLI')
                    return original_rename(path, destination)
                def control(*arguments, **kwargs):
                    if dimension == 'running-state' and arguments[0] == 'stop':
                        return subprocess.CompletedProcess(arguments, 1, '', 'cannot stop')
                    return self.systemctl(*arguments, **kwargs)
                with self.patches(), mock.patch.object(core, 'ready', side_effect=
                        lambda _python, version, _api, **_kwargs: version == '0.4.0' and dimension != 'API'), \
                        mock.patch.object(core, 'systemctl', side_effect=control), \
                        mock.patch.object(Path, 'rename', rename), mock.patch('sys.stderr') as stderr:
                    self.assertEqual(transaction.install(), 1)
                output = ''.join(call.args[0] for call in stderr.write.call_args_list)
                self.assertIn(dimension + '=FAILED', output)
                self.assertIn('enablement=restored', output)
                self.assertTrue(transaction.record.exists())
                # Restore only this fixture's failed CLI and archive the retained
                # evidence so the next independent case can execute.
                if dimension == 'files':
                    transaction.cli.write_text('old cli')
                transaction.record.rename(transaction.record.with_name('evidence.' + dimension))

    def test_commit_receipt_failure_rolls_back_and_never_claims_commit(self):
        receipt = self.root / 'missing-parent' / 'receipt.json'
        self.transaction.commit_receipt = receipt
        with self.patches():
            self.assertEqual(self.transaction.install(), 1)
        self.assertFalse(receipt.exists())
        self.assert_old_restored()

    def test_commit_receipt_rename_failure_rolls_back(self):
        receipt = self.root / 'receipt.json'
        self.transaction.commit_receipt = receipt
        rename = Path.rename
        def fail_receipt(path, destination):
            if Path(destination) == receipt:
                raise OSError('receipt rename failed')
            return rename(path, destination)
        with self.patches(), mock.patch.object(Path, 'rename', fail_receipt):
            self.assertEqual(self.transaction.install(), 1)
        self.assertFalse(receipt.exists())
        self.assertTrue(list(self.root.glob('receipt.json.prepared.*')))
        self.assert_old_restored()

    def test_unit_verifier_generic_not_supported_is_a_real_failure(self):
        for message, allowed in (("unrecognized option '--user'", True),
                                 ('Executable not supported', False), ('invalid unit', False)):
            with self.subTest(message=message):
                transaction = core.CoreTransaction(self.source, sys.executable)
                def run(command, **_kwargs):
                    return subprocess.CompletedProcess(command, 1 if 'verify' in command else 0,
                                                       '', message)
                with mock.patch.object(core.shutil, 'which', return_value='/fake/systemd-analyze'), \
                        mock.patch.object(core, 'run', side_effect=run):
                    if allowed:
                        self.assertEqual(transaction.stage(), ('0.6.0', 6))
                    else:
                        with self.assertRaisesRegex(ValueError, 'unit validation failed'):
                            transaction.stage()
                self.assert_old_restored()

    def test_unit_lifecycle_directive_is_validated_in_its_own_section(self):
        unit = self.source / core.SERVICE
        unit.write_text(unit.read_text().replace('CollectMode=inactive-or-failed\n', '').replace(
            '[Service]\n', '[Service]\nCollectMode=inactive-or-failed\n'))
        with self.patches():
            with self.assertRaisesRegex(ValueError, 'CollectMode'):
                self.transaction.install()
        self.assert_old_restored()

    def test_generated_launcher_executes_literal_paths_and_arguments(self):
        folder = self.root / 'spaces $() `ticks` ; % and "quotes"'
        folder.mkdir()
        module = folder / 'module.py'
        module.write_text('import json,sys; print(json.dumps(sys.argv[1:]))\n')
        launcher = folder / 'launcher'
        launcher.write_text(core.launcher(sys.executable, module))
        launcher.chmod(0o755)
        argument = 'literal $(touch unexpected) `touch unexpected`'
        result = subprocess.run([str(launcher), argument], text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), [argument])
        self.assertFalse((self.root / 'unexpected').exists())
        escaped = core.unit_escape(folder / 'launcher')
        self.assertIn('$$()', escaped)
        self.assertIn('%%', escaped)
        self.assertIn('\\"quotes\\"', escaped)
        for unsafe in ('line\nbreak', 'tab\tpath', 'control\x00path', 'delete\x7fpath',
                       'control\x85path', 'zero\u200bwidth'):
            with self.subTest(unsafe=unsafe), self.assertRaises(ValueError):
                core.unit_escape(unsafe)

    def test_arbitrary_prior_launcher_is_never_evaluated(self):
        path = self.transaction.program / 'agent-launcher'
        path.write_text('#!/bin/sh\ntouch ' + str(self.root / 'unexpected') + '\n')
        self.assertIsNone(core.prior_interpreter(self.transaction.program))
        self.assertFalse((self.root / 'unexpected').exists())

    def test_legacy_direct_unit_interpreter_is_recognized_without_execution(self):
        (self.transaction.program / 'agent-launcher').unlink()
        self.transaction.unit.write_text('[Service]\nExecStart=' + sys.executable +
            ' %h/.local/lib/sleep-disabler-gnome/agent.py\n')
        self.assertEqual(core.prior_interpreter(self.transaction.program, self.transaction.unit),
                         sys.executable)
        self.transaction.unit.write_text('[Service]\nExecStart=/bin/sh -c "touch unexpected"\n')
        self.assertIsNone(core.prior_interpreter(self.transaction.program, self.transaction.unit))

    def test_unit_verifier_timeout_does_not_switch_active_files(self):
        def run(command, **_kwargs):
            if 'verify' in command:
                raise subprocess.TimeoutExpired(command, 40)
            return subprocess.CompletedProcess(command, 0, '', '')
        with mock.patch.object(core.shutil, 'which', return_value='/fake/systemd-analyze'), \
                mock.patch.object(core, 'systemctl', side_effect=self.systemctl), \
                mock.patch.object(core, 'state', side_effect=self.state), \
                mock.patch.object(core, 'run', side_effect=run):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.transaction.install()
        self.assert_old_restored()
        self.assertFalse(self.transaction.record.exists())


if __name__ == '__main__':
    unittest.main()
