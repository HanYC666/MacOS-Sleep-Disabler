"""No live bus or sleep service: test only the read-only guard decision."""

import importlib.util
from pathlib import Path
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    'sleep_guard', Path(__file__).parents[1] / 'sleep_guard.py')
guard = importlib.util.module_from_spec(spec)
spec.loader.exec_module(guard)


class SleepGuardTests(unittest.TestCase):
    held = ('sleep', 'Sleep Disabler', 'Keep this session awake', 'block', 1000, 42)

    def test_held_block_prevents_all_sleep_service_modes(self):
        self.assertTrue(guard.prevention_held([self.held]))
        self.assertTrue(guard.prevention_held([
            ('idle:sleep', *self.held[1:])]))

    def test_other_apps_weak_and_delay_locks_are_not_our_guard(self):
        for index, replacement in ((0, 'idle'), (1, 'Another app'),
                                   (2, 'Different reason'), (3, 'delay'),
                                   (3, 'block-weak')):
            row = list(self.held)
            row[index] = replacement
            with self.subTest(row=row):
                self.assertFalse(guard.prevention_held([row]))
        self.assertFalse(guard.prevention_held([]))

    def test_block_removed_allows_timer_or_failsafe_after_release(self):
        with mock.patch.object(guard, 'read_inhibitors', side_effect=[
                [self.held], [], []]), mock.patch('sys.stderr'):
            self.assertEqual(guard.main(), 1)
            self.assertEqual(guard.main(), 0)
            self.assertEqual(guard.main(), 0)

    def test_unreadable_state_blocks_without_requesting_sleep(self):
        for error in (TimeoutError('No reply'), OSError('Bus unavailable')):
            with self.subTest(error=error), \
                    mock.patch.object(guard, 'read_inhibitors', side_effect=error), \
                    mock.patch('sys.stderr'):
                self.assertEqual(guard.main(), 255)

    def test_malformed_record_is_not_treated_as_no_inhibitor(self):
        for row in ((), self.held[:5], (*self.held[:4], True, 1),
                    (*self.held[:5], -1), (None, *self.held[1:])):
            with self.subTest(row=row), \
                    mock.patch.object(guard, 'read_inhibitors', return_value=[row]), \
                    mock.patch('sys.stderr'):
                self.assertEqual(guard.main(), 255)

    def test_probe_uses_only_read_only_manager_method_and_closes_bus(self):
        bus, manager = mock.Mock(), mock.Mock()
        manager.ListInhibitors.return_value = [self.held]
        fake_dbus = mock.Mock()
        fake_dbus.SystemBus.return_value = bus
        fake_dbus.Interface.return_value = manager
        with mock.patch.dict('sys.modules', {'dbus': fake_dbus}):
            self.assertEqual(guard.read_inhibitors(), [self.held])
        manager.ListInhibitors.assert_called_once_with(timeout=2)
        self.assertEqual([call[0] for call in manager.method_calls], ['ListInhibitors'])
        bus.close.assert_called_once_with()

    def test_probe_closes_bus_after_timeout(self):
        bus, manager = mock.Mock(), mock.Mock()
        manager.ListInhibitors.side_effect = TimeoutError()
        fake_dbus = mock.Mock()
        fake_dbus.SystemBus.return_value = bus
        fake_dbus.Interface.return_value = manager
        with mock.patch.dict('sys.modules', {'dbus': fake_dbus}):
            with self.assertRaises(TimeoutError):
                guard.read_inhibitors()
        bus.close.assert_called_once_with()
