"""Core installer fault tests; all user-manager and bus calls are isolated."""
import importlib.util
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import types
import io
from contextlib import redirect_stdout
import unittest
from unittest import mock

SOURCE = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('core_install_under_test', SOURCE / 'core_install.py')
core = importlib.util.module_from_spec(spec)
spec.loader.exec_module(core)


class CoreTests(unittest.TestCase):
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
        bus = types.SimpleNamespace(get_name_owner=lambda _name: ':1.agent',
                                    get_object=lambda *_args, **_kwargs: app)
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
                                  'panelRuntimeVersion': '', 'panelRuntimeRegistered': False})

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.home = self.root / 'home'
        self.home.mkdir()
        self.source = self.root / 'source'
        self.source.mkdir()
        (self.source / 'agent.py').write_text('AGENT_RUNTIME_VERSION = "0.5.0"\nAPI_VERSION = 5\n')
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

    def systemctl(self, *arguments, **_kwargs):
        self.commands.append(arguments)
        command = arguments[0]
        if command == 'is-enabled':
            return subprocess.CompletedProcess(arguments, 0 if self.enabled else 1,
                                               'enabled\n' if self.enabled else 'disabled\n', '')
        if command == 'is-active':
            return subprocess.CompletedProcess(arguments, 0 if self.active else 3,
                                               'active\n' if self.active else 'inactive\n', '')
        if command in ('enable', 'disable'):
            self.enabled = command == 'enable'
        if command in ('restart', 'start', 'stop'):
            self.active = command != 'stop'
        return subprocess.CompletedProcess(arguments, 0, '', '')

    def state(self, _python, **_kwargs):
        if not self.active:
            return None
        old = (self.transaction.program / 'old-marker').exists()
        return {'agentRuntimeVersion': '0.4.0' if old else '0.5.0',
                'apiVersion': 4 if old else 5, 'panelRuntimeVersion': '',
                'panelRuntimeRegistered': False}

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

    def test_readiness_has_a_total_deadline(self):
        with mock.patch.object(core.time, 'monotonic', side_effect=[0, 1, 71, 71]), \
                mock.patch.object(core, 'systemctl', return_value=
                    subprocess.CompletedProcess([], 0, '', '')) as control, \
                mock.patch.object(core, 'state') as state:
            self.assertFalse(core.ready(sys.executable, '0.5.0', 5))
        control.assert_called_once()
        state.assert_not_called()

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
                        lambda _python, version, _api: version == '0.4.0' and dimension != 'API'), \
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
                        self.assertEqual(transaction.stage(), ('0.5.0', 5))
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
        with self.assertRaises(ValueError):
            core.unit_escape('line\nbreak')

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
                mock.patch.object(core, 'run', side_effect=run):
            with self.assertRaises(subprocess.TimeoutExpired):
                self.transaction.install()
        self.assert_old_restored()
        self.assertFalse(self.transaction.record.exists())


if __name__ == '__main__':
    unittest.main()
