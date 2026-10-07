"""CLI input checks with an isolated bus and observable mutation calls."""
import importlib.util
import io
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


class Boolean(int):
    pass


class CliTests(unittest.TestCase):
    def setUp(self):
        self.app = mock.Mock()
        self.app.GetState.return_value = {'threshold': 20}
        self.bus = mock.Mock()
        self.dbus = types.SimpleNamespace(Boolean=Boolean, UInt32=int, UInt64=int,
            Int32=int, Int64=int, Double=float, DBusException=type('BusError', (Exception,), {}),
            SessionBus=mock.Mock(return_value=self.bus),
            Interface=mock.Mock(return_value=self.app))
        spec = importlib.util.spec_from_file_location('ctl_under_test', Path(__file__).parents[1] / 'ctl.py')
        self.ctl = importlib.util.module_from_spec(spec)
        with mock.patch.dict(sys.modules, {'dbus': self.dbus}):
            spec.loader.exec_module(self.ctl)

    def invoke(self, *args):
        with mock.patch.object(sys, 'argv', ['sleep-disablerctl', *args]), \
                mock.patch('sys.stdout', new_callable=io.StringIO), \
                mock.patch('sys.stderr', new_callable=io.StringIO) as error:
            try:
                result = self.ctl.main()
            except SystemExit as exit:
                result = exit.code
            return result, error.getvalue()

    def test_explicit_invalid_threshold_never_connects_or_dispatches(self):
        for value in ('-1', '0', '100', '9999999999999999999999', '1.5', 'bad'):
            with self.subTest(value=value):
                result, error = self.invoke('failsafe', 'on', value)
                self.assertEqual(result, 2)
                self.assertNotIn('Traceback', error)
                self.dbus.SessionBus.assert_not_called()
                self.app.SetFailsafe.assert_not_called()

    def test_valid_threshold_endpoints_dispatch(self):
        for value in (1, 99):
            self.assertEqual(self.invoke('failsafe', 'on', str(value))[0], 0)
            self.app.SetFailsafe.assert_called_with(True, value)

    def test_malformed_state_threshold_never_dispatches(self):
        for value in (None, '20', True, Boolean(1), 1.5, 0, 100):
            with self.subTest(value=value):
                self.app.GetState.return_value = {'threshold': value}
                result, error = self.invoke('failsafe', 'off')
                self.assertEqual(result, 2)
                self.assertIn('agent threshold', error)
                self.assertNotIn('Traceback', error)
                self.app.SetFailsafe.assert_not_called()

    def test_missing_state_threshold_never_dispatches(self):
        self.app.GetState.return_value = {}
        self.assertEqual(self.invoke('failsafe', 'off')[0], 2)
        self.app.SetFailsafe.assert_not_called()


if __name__ == '__main__':
    unittest.main()
