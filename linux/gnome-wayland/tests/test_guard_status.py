import importlib.util
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

SOURCE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SOURCE))
import guard_status
import system_guard_install as install


class StatusTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        install.deploy((SOURCE / 'sleep_guard.py').read_bytes(), root=self.root,
                       reload=lambda: None, verify=lambda: None, check=lambda path: None)
        self.run = mock.Mock(return_value=mock.Mock(stdout='\n\n'.join(
            'Id=' + unit + '\nNeedDaemonReload=no\nSuccessExitStatus=\nExecCondition={ path=/usr/bin/python3 ; argv[]=' +
            install.CONDITION + ' ; ignore_errors=no ; }'
            for unit in install.UNITS)))
        patch = mock.patch.object(guard_status, 'protected')
        patch.start()
        self.addCleanup(patch.stop)

    def inspect(self):
        return guard_status.inspect_guard(root=self.root, run=self.run)

    def test_all_files_and_loaded_units_required(self):
        self.assertEqual(self.inspect(), ('verified', ''))
        self.assertEqual(self.run.call_args.args[0][1], 'show')

    def test_one_missing_loaded_condition_is_not_verified(self):
        self.run.return_value.stdout = self.run.return_value.stdout.replace(
            install.CONDITION, '/usr/bin/true', 1)
        self.assertEqual(self.inspect()[0], 'unavailable')

    def test_stale_manager_config_is_not_verified(self):
        self.run.return_value.stdout = self.run.return_value.stdout.replace(
            'NeedDaemonReload=no', 'NeedDaemonReload=yes', 1)
        self.assertEqual(self.inspect()[0], 'unavailable')

    def test_modified_helper_does_not_claim_coverage(self):
        (self.root / install.HELPER.relative_to('/')).write_text('different version')
        self.assertEqual(self.inspect()[0], 'unavailable')
        self.run.assert_not_called()

    def test_readback_timeout_does_not_claim_coverage(self):
        self.run.side_effect = TimeoutError('manager readback timeout')
        self.assertEqual(self.inspect(), ('unavailable', 'manager readback timeout'))

    def test_ignored_condition_is_not_verified(self):
        self.run.return_value.stdout = self.run.return_value.stdout.replace(
            'ignore_errors=no', 'ignore_errors=yes', 1)
        self.assertEqual(self.inspect()[0], 'unavailable')


if __name__ == '__main__':
    unittest.main()
