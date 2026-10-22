"""All deployment writes stay in a temporary root; no real systemctl calls."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location('guard_install',
    Path(__file__).resolve().parents[1] / 'system_guard_install.py')
installer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(installer)


class DeploymentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.reload = mock.Mock()
        self.verify = mock.Mock()

    def deploy(self, **kwargs):
        return installer.deploy(b'print("fixture")\n', root=self.root,
            reload=self.reload, verify=self.verify, check=lambda path: None, **kwargs)

    def helper(self):
        return self.root / installer.HELPER.relative_to('/')

    def test_install_all_four_conditions_and_helper(self):
        self.deploy()
        self.assertTrue(self.helper().read_text().startswith(installer.MARKER))
        for unit in installer.UNITS:
            path = self.root / 'etc/systemd/system' / (unit + '.d') / installer.DROPIN_NAME
            self.assertEqual(path.read_bytes(), installer.DROPIN)
        self.reload.assert_called_once_with()
        self.verify.assert_called_once_with()

    def test_conflict_refuses_before_any_changes(self):
        path = self.root / 'etc/systemd/system' / (installer.UNITS[-1] + '.d') / installer.DROPIN_NAME
        path.parent.mkdir(parents=True)
        path.write_text('unrelated admin policy')
        with self.assertRaisesRegex(RuntimeError, 'Unrelated'):
            self.deploy()
        self.assertFalse(self.helper().exists())
        self.assertEqual(path.read_text(), 'unrelated admin policy')
        self.reload.assert_not_called()

    def test_loaded_verification_failure_restores_previous_install(self):
        self.deploy()
        self.helper().write_text(installer.MARKER + 'old helper')
        self.verify.side_effect = RuntimeError('loaded config mismatch')
        with self.assertRaisesRegex(RuntimeError, 'loaded config mismatch'):
            self.deploy()
        self.assertEqual(self.helper().read_text(), installer.MARKER + 'old helper')
        self.assertEqual(self.reload.call_count, 3)

    def test_failed_first_install_leaves_no_active_managed_files(self):
        self.verify.side_effect = RuntimeError('verify failed')
        with self.assertRaises(RuntimeError):
            self.deploy()
        self.assertFalse(self.helper().exists())
        self.assertTrue(list((self.root / 'var/lib/sleep-disabler-guard').glob('*/*.failed')))

    def test_remove_retains_backups_without_touching_unrelated_dropin(self):
        self.deploy()
        other = self.root / 'etc/systemd/system/systemd-suspend.service.d/10-other.conf'
        other.write_text('[Service]\nEnvironment=KEEP=1\n')
        backup = self.deploy(remove=True)
        self.assertFalse(self.helper().exists())
        self.assertEqual(len(list(backup.glob('*.original'))), 5)
        self.assertTrue(other.exists())
        self.assertEqual(self.verify.call_count, 1)

    def test_protected_rejects_symlink_even_if_target_is_missing(self):
        path = self.root / 'link'
        path.symlink_to(self.root / 'missing')
        with self.assertRaises(RuntimeError):
            installer.protected(path)

    def test_readback_checks_each_unit_without_starting_it(self):
        with mock.patch.object(installer.subprocess, 'run') as run:
            run.return_value.stdout = '\n\n'.join('Id=' + unit +
                '\nNeedDaemonReload=no\nSuccessExitStatus=\nExecCondition={ path=/usr/bin/python3 ; argv[]=' +
                installer.CONDITION + ' ; ignore_errors=no ; }' for unit in installer.UNITS)
            installer.verify_manager()
        self.assertEqual(run.call_count, 1)
        for call in run.call_args_list:
            self.assertEqual(call.args[0][1], 'show')

    def test_incomplete_transaction_blocks_retry_before_any_destination_changes(self):
        abandoned = self.root / 'var/lib/sleep-disabler-guard/abandoned'
        abandoned.mkdir(parents=True)
        (abandoned / 'transaction.json').write_text('{"status":"deploying"}')
        with self.assertRaisesRegex(RuntimeError, 'Unfinished'):
            self.deploy()
        self.assertFalse(self.helper().exists())
        self.reload.assert_not_called()

    def test_success_exit_override_cannot_allow_the_skip_code(self):
        output = '\n\n'.join('Id=' + unit +
            '\nNeedDaemonReload=no\nSuccessExitStatus=1\nExecCondition={ path=/usr/bin/python3 ; argv[]=' +
            installer.CONDITION + ' ; ignore_errors=no ; }' for unit in installer.UNITS)
        with self.assertRaisesRegex(RuntimeError, 'success exit status'):
            installer.validate_loaded(output)


if __name__ == '__main__':
    unittest.main()
