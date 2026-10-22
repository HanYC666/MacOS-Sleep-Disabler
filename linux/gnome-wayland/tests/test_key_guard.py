"""Use in-memory settings only; never connect to dconf or change real keys."""

import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock


spec = importlib.util.spec_from_file_location(
    'key_guard', Path(__file__).parents[1] / 'key_guard.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Variant:
    def __init__(self, value):
        self.value = value

    def unpack(self):
        return self.value


class Settings:
    def __init__(self):
        self.defaults = {key: [''] for key in module.KEYS}
        self.defaults.update({'suspend-static': ['XF86Sleep'],
                              'hibernate-static': ['XF86Suspend', 'XF86Hibernate'],
                              'power-static': ['XF86PowerOff'],
                              'screensaver': ['<Super>l']})
        self.values = {}
        self.writable = True
        self.write = mock.Mock(return_value=True)

    def get_user_value(self, key):
        return Variant(self.values[key]) if key in self.values else None

    def get_strv(self, key):
        return list(self.values.get(key, self.defaults[key]))

    def is_writable(self, _key):
        return self.writable

    def set_strv(self, key, value):
        if not self.write(key, value):
            return False
        self.values[key] = list(value)
        return True

    def reset(self, key):
        self.values.pop(key, None)


class KeyGuardTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.path = Path(self.directory.name) / 'key-recovery.json'
        self.settings = Settings()
        self.persist = mock.Mock(side_effect=lambda path, value: path.write_text(json.dumps(value)))
        self.guard = module.GnomeKeyGuard(self.path, self.persist, self.settings, lambda: None)

    def test_all_hardware_sleep_bindings_go_to_lock_and_restore_unset_defaults(self):
        self.guard.acquire()
        self.assertTrue(self.guard.active())
        for key in module.SLEEP_KEYS:
            self.assertEqual(self.settings.get_strv(key), [])
        self.assertEqual(self.settings.get_strv('screensaver'), [
            '<Super>l', 'XF86Sleep', 'XF86Suspend', 'XF86Hibernate', 'XF86PowerOff'])
        self.guard.restore()
        self.assertEqual(self.settings.values, {})
        self.assertEqual(self.guard.read(), {})
        self.assertFalse(self.guard.active())

    def test_user_bindings_preserved_and_deduplicated(self):
        self.settings.values = {'suspend': ['<Super>s', 'XF86Sleep'],
                                'screensaver': ['XF86Sleep'], 'power': []}
        prior = copy.deepcopy(self.settings.values)
        self.guard.acquire()
        bindings = self.settings.get_strv('screensaver')
        self.assertEqual(bindings.count('XF86Sleep'), 1)
        self.assertIn('<Super>s', bindings)
        self.guard.restore()
        self.assertEqual(self.settings.values, prior)

    def test_snapshot_is_persisted_before_any_write(self):
        self.persist.side_effect = OSError('Disk full')
        with self.assertRaises(OSError):
            self.guard.acquire()
        self.settings.write.assert_not_called()

    def test_crash_recovery_restores_only_our_values(self):
        self.guard.acquire()
        self.settings.values['suspend'] = ['<Super>x']
        recovered = module.GnomeKeyGuard(self.path, self.persist, self.settings, lambda: None)
        self.assertFalse(recovered.active())
        recovered.restore()
        self.assertEqual(self.settings.values, {'suspend': ['<Super>x']})

    def test_reset_by_user_is_preserved(self):
        self.settings.values['screensaver'] = ['<Super>x']
        self.guard.acquire()
        self.settings.reset('screensaver')
        self.guard.restore()
        self.assertEqual(self.settings.values, {})

    def test_partial_write_failure_rolls_back(self):
        self.settings.write.side_effect = lambda key, _value: key != 'hibernate-static'
        with self.assertRaises(ValueError):
            self.guard.acquire()
        self.assertEqual(self.settings.values, {})
        self.assertEqual(self.guard.read(), {})

    def test_restore_failure_keeps_only_unresolved_entry_for_retry(self):
        self.settings.values['suspend'] = ['<Super>s']
        self.guard.acquire()
        self.settings.write.side_effect = lambda key, _value: key != 'suspend'
        with self.assertRaises(ValueError):
            self.guard.restore()
        self.assertEqual(set(self.guard.read()), {'suspend'})
        self.settings.write.side_effect = None
        self.guard.restore()
        self.assertEqual(self.settings.values, {'suspend': ['<Super>s']})

    def test_locked_schema_cannot_claim_active_guard(self):
        self.settings.writable = False
        with self.assertRaisesRegex(ValueError, 'administrator policy'):
            self.guard.acquire()
        self.settings.write.assert_not_called()
        self.assertFalse(self.guard.active())

    def test_reacquire_does_not_snapshot_our_own_bindings(self):
        self.guard.acquire()
        prior = self.path.read_text()
        self.guard.acquire()
        self.assertEqual(self.path.read_text(), prior)
        self.guard.restore()
        self.assertEqual(self.settings.values, {})

    def test_malformed_journal_is_retained_without_settings_writes(self):
        for contents in ('broken', '{"schema": 1}',
                         '{"schema": 1, "uid": -1, "keys": {}}'):
            with self.subTest(contents=contents):
                self.path.write_text(contents)
                with self.assertRaises(ValueError):
                    self.guard.acquire()
                self.assertEqual(self.path.read_text(), contents)
                self.settings.write.assert_not_called()

    def test_intervening_binding_edit_is_detected(self):
        self.guard.acquire()
        self.settings.values['hibernate-static'] = ['XF86Hibernate']
        self.assertFalse(self.guard.active())

    def test_deleted_journal_keeps_guard_and_does_not_recreate_until_release(self):
        self.settings.values['suspend'] = ['<Super>s']
        self.guard.acquire()
        self.path.rename(self.path.with_suffix('.backup'))
        protected = copy.deepcopy(self.settings.values)
        for _ in range(3):
            self.assertTrue(self.guard.journal_missing())
            self.assertTrue(self.guard.active())
            self.guard.acquire()
        self.assertFalse(self.path.exists())
        self.assertEqual(self.settings.values, protected)
        self.guard.restore()
        self.assertEqual(self.settings.values, {})
        self.assertEqual(self.guard.read(), {})
        self.assertFalse(self.guard.journal_missing())

    def test_reinstated_journal_restores_original_custom_values(self):
        self.settings.values['screensaver'] = ['<Super>x']
        self.settings.values['suspend'] = ['<Super>s']
        prior = copy.deepcopy(self.settings.values)
        self.guard.acquire()
        backup = self.path.with_suffix('.backup')
        self.path.rename(backup)
        self.assertTrue(self.guard.active())
        backup.rename(self.path)
        self.guard.restore()
        self.assertEqual(self.settings.values, prior)

    def test_missing_journal_defaults_preserve_intervening_user_edit(self):
        self.guard.acquire()
        self.path.rename(self.path.with_suffix('.backup'))
        self.settings.values['screensaver'] = ['<Super>x']
        self.guard.restore()
        self.assertEqual(self.settings.values, {'screensaver': ['<Super>x']})

    def test_missing_journal_default_restore_failure_is_retryable(self):
        self.guard.acquire()
        self.path.rename(self.path.with_suffix('.backup'))
        with mock.patch.object(self.settings, 'reset', side_effect=OSError('read-only')):
            with self.assertRaises(ValueError):
                self.guard.restore()
        self.assertEqual(set(self.guard.read()), set(module.KEYS))
        self.guard.restore()
        self.assertEqual(self.settings.values, {})
