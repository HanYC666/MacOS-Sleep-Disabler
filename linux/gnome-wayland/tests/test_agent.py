"""Deterministic state-machine tests; no GNOME session or real D-Bus required."""

import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest import mock


class FakeDBusException(Exception):
    def __init__(self, message="", name=None):
        super().__init__(message)
        self.name = name

    def get_dbus_name(self):
        return self.name


class FakeGLib:
    SOURCE_REMOVE = False

    def __init__(self):
        self.next_id = 1
        self.sources = {}
        self.removed = []
        self.idle_add = mock.Mock()

    def timeout_add_seconds(self, seconds, callback):
        source = self.next_id
        self.next_id += 1
        self.sources[source] = (seconds, callback)
        return source

    def timeout_add(self, milliseconds, callback):
        return self.timeout_add_seconds(milliseconds / 1000, callback)

    def source_remove(self, source):
        self.removed.append(source)
        self.sources.pop(source, None)
        return True


fake_glib = FakeGLib()
dbus = types.ModuleType("dbus")
dbus.DBusException = FakeDBusException
dbus.Int32 = int
dbus.UInt32 = int
dbus.UInt64 = int
dbus.Boolean = bool
dbus.Double = float
dbus.String = str
dbus.Array = lambda value, signature=None: value
dbus.Dictionary = lambda value, signature=None: value
dbus.service = types.ModuleType("dbus.service")
class FakeServiceObject:
    def __init__(self, *_args):
        pass


dbus.service.Object = FakeServiceObject
dbus.service.method = lambda *_args, **_kwargs: lambda function: function
dbus.service.signal = lambda *_args, **_kwargs: lambda function: function
dbus.mainloop = types.ModuleType("dbus.mainloop")
dbus.mainloop.glib = types.ModuleType("dbus.mainloop.glib")
gi = types.ModuleType("gi")
gi.repository = types.ModuleType("gi.repository")
gi.repository.GLib = fake_glib
sys.modules.update({
    "dbus": dbus, "dbus.service": dbus.service,
    "dbus.mainloop": dbus.mainloop, "dbus.mainloop.glib": dbus.mainloop.glib,
    "gi": gi, "gi.repository": gi.repository,
})
spec = importlib.util.spec_from_file_location(
    "sleep_disabler_agent", Path(__file__).parents[1] / "agent.py")
agent_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_module)


class FakeBrightness:
    identity = agent_module.LegacyBrightness.identity

    def __init__(self, value=70):
        self.value = value
        self.writes = []

    def read(self):
        return self.value

    def read_async(self, callback, timeout=3):
        callback(self.read(), None)

    def output_identity(self):
        return self.identity + ':card0-eDP-1:intel_backlight'

    def write(self, value):
        self.writes.append(value)
        self.value = value

    def write_async(self, value, callback, timeout=3):
        self.write(value)
        callback(None)


class AgentTests(unittest.TestCase):
    def test_upower_property_evidence_rejects_malformed_values(self):
        for value in (True, False, '2', b'2', 2.0, -1, 0x100000000, None):
            with self.subTest(kind=value):
                with self.assertRaises(ValueError):
                    agent_module.dbus_uint32(value)
        for value in (True, False, '5', b'5', None, -1, 101, float('nan'), float('inf')):
            with self.subTest(percentage=value):
                self.assertIsNone(agent_module.valid_percent(value))
        self.assertEqual(agent_module.dbus_uint32(dbus.UInt32(2)), 2)
        self.assertEqual(agent_module.valid_percent(dbus.Double(5.5)), 5.5)
        for value in ('', {}, None, 0):
            with self.subTest(enumeration=value):
                with self.assertRaises(ValueError):
                    agent_module.upower_device_list(value)
        wrong_signature = type('WrongSignature', (list,), {'signature': 's'})()
        with self.assertRaises(ValueError):
            agent_module.upower_device_list(wrong_signature)
        self.assertEqual(agent_module.upower_device_list([]), [])

    def test_clock_interval_rejects_inter_read_pause_without_suspend_proof(self):
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=[10, 10.7]), \
                mock.patch.object(agent_module, 'boottime', return_value=10.7):
            self.assertIsNone(agent_module.sample_clock_gap())
        baseline = agent_module.ClockGapSample(10, 10, 0, 0)
        current = agent_module.ClockGapSample(10.7, 10.7, 0, 0.7)
        self.assertFalse(agent_module.gap_proves_suspend(current, baseline))
        actual_suspend = agent_module.ClockGapSample(10.7, 10, 0.7, 0.7)
        self.assertTrue(agent_module.gap_proves_suspend(actual_suspend, baseline))
        self.assertFalse(agent_module.gap_proves_suspend(actual_suspend, None))

    def test_clock_interval_boundary_and_invalid_samples(self):
        baseline = agent_module.ClockGapSample(0, 0, 0, 0)
        at_boundary = agent_module.ClockGapSample(0.5, 0, 0.5, 0.5)
        below_boundary = agent_module.ClockGapSample(0.499, 0, 0.499, 0.499)
        self.assertTrue(agent_module.gap_proves_suspend(at_boundary, baseline))
        self.assertFalse(agent_module.gap_proves_suspend(below_boundary, baseline))
        for readings in ((10, 10.051), (float('nan'), 10), (10, 9.9)):
            with self.subTest(readings=readings):
                with mock.patch.object(agent_module.time, 'monotonic', side_effect=readings), \
                        mock.patch.object(agent_module, 'boottime', return_value=10):
                    self.assertIsNone(agent_module.sample_clock_gap())

    def test_dim_requires_target_readback_and_preserves_prepared_recovery(self):
        for actual in (70, 5):
            with self.subTest(actual=actual):
                a = self.agent
                a.brightness_record = None
                a.brightness_journal_state = 'absent'
                a.brightness_path.unlink(missing_ok=True)
                a.brightness_adapter = FakeBrightness()
                a.enabled = a.lid_dimming = a.lid_closed = True
                a.brightness_adapter.write = lambda _value: setattr(a.brightness_adapter, 'value', actual)
                self.assertEqual(a.dim_brightness(), 'pending')
                self.assertEqual(a.brightness_record['phase'], 'set-confirmed')
                self.assertEqual(json.loads(a.brightness_path.read_text()), a.brightness_record)
                self.assertFalse(a.state()['brightnessDimmed'])
                self.assertTrue(a.state()['brightnessRecoveryPending'])
                self.assertIn('Dim readback differs', a.brightness_error)
                result = a.restore_brightness()
                self.assertEqual(result, 'pending')
                self.assertEqual(a.brightness_adapter.value, actual)
                self.assertEqual(json.loads(a.brightness_path.read_text()), a.brightness_record)

    def test_recovery_bus_operations_have_explicit_short_timeouts(self):
        a = self.agent
        remote = mock.Mock()
        remote.Get.return_value = 70
        remote.GetNameOwner.return_value = ':1.gnome'
        adapter = agent_module.LegacyBrightness(a)
        with mock.patch.object(a, 'proxy', return_value=remote):
            self.assertEqual(adapter.read(), 70)
            adapter.write(0)
            self.assertEqual(agent_module.Agent.session_manager_owner(a), ':1.gnome')
            self.held_cookie()
            self.assertTrue(a.attempt_session_release())
        remote.Get.assert_called_once_with(agent_module.BRIGHTNESS_IFACE, 'Brightness', timeout=3)
        remote.Set.assert_called_once_with(agent_module.BRIGHTNESS_IFACE, 'Brightness', 0, timeout=3)
        self.assertEqual(remote.GetNameOwner.call_args_list, [
            mock.call(agent_module.SESSION, timeout=3),
            mock.call(':1.gnome', timeout=3)])
        remote.Uninhibit.assert_called_once_with(7, timeout=3)

    def test_brightness_async_read_validates_reply_and_uses_short_timeout(self):
        remote = mock.Mock()
        callbacks = []
        adapter = agent_module.LegacyBrightness(self.agent)
        with mock.patch.object(self.agent, 'proxy', return_value=remote):
            adapter.read_async(lambda value, failure: callbacks.append((value, failure)))
        request = remote.Get.call_args
        self.assertEqual(request.args, (agent_module.BRIGHTNESS_IFACE, 'Brightness'))
        self.assertEqual(request.kwargs['timeout'], 3)
        request.kwargs['reply_handler'](101)
        self.assertIsNone(callbacks[0][0])
        self.assertIsInstance(callbacks[0][1], ValueError)
        for malformed in (True, 50.5, '50', agent_module.dbus.Boolean(True)):
            with self.subTest(value=repr(malformed)):
                request.kwargs['reply_handler'](malformed)
                self.assertIsNone(callbacks[-1][0])
                self.assertIsInstance(callbacks[-1][1], ValueError)
        request.kwargs['reply_handler'](agent_module.dbus.Int32(50))
        self.assertEqual(callbacks[-1], (50, None))

    def test_brightness_sync_read_rejects_coercible_malformed_types(self):
        remote = mock.Mock()
        adapter = agent_module.LegacyBrightness(self.agent)
        with mock.patch.object(self.agent, 'proxy', return_value=remote):
            for malformed in (True, 50.5, '50', agent_module.dbus.Boolean(True)):
                with self.subTest(value=repr(malformed)):
                    remote.Get.return_value = malformed
                    with self.assertRaisesRegex(ValueError, 'No usable'):
                        adapter.read()
            remote.Get.return_value = agent_module.dbus.Int32(50)
            self.assertEqual(adapter.read(), 50)

    def test_brightness_probe_pins_owner_and_reconciles_after_async_read(self):
        a = self.agent
        a.reconcile_brightness_async = mock.Mock()
        a.brightness_adapter = None
        a.brightness_available = False
        names, introspection, props = mock.Mock(), mock.Mock(), mock.Mock()
        a.proxy = mock.Mock(side_effect=lambda _bus, owner, _path, interface:
            names if owner == 'org.freedesktop.DBus' else
            introspection if interface == 'org.freedesktop.DBus.Introspectable' else props)
        with mock.patch.object(agent_module.LegacyBrightness, 'output_identity',
                               return_value='gnome-settings-daemon:built-in-panel:eDP-1:backlight'):
            agent_module.Agent.refresh_brightness_capability(a, 'owner-return')
            self.assertFalse(a.brightness_available)
            first = names.GetNameOwner.call_args.kwargs
            self.assertEqual(first['timeout'], 3)
            first['reply_handler'](':1.power')
            self.assertEqual(introspection.Introspect.call_args.kwargs['timeout'], 3)
            introspection.Introspect.call_args.kwargs['reply_handler'](
                '<node><interface name="org.gnome.SettingsDaemon.Power.Screen">'
                '<property name="Brightness" type="i" access="readwrite"/>'
                '</interface></node>')
            self.assertEqual(props.Get.call_args.kwargs['timeout'], 3)
            props.Get.call_args.kwargs['reply_handler'](70)
            self.assertFalse(a.brightness_available)
            names.GetNameOwner.call_args.kwargs['reply_handler'](':1.power')
        self.assertTrue(a.brightness_available)
        self.assertEqual(a.brightness_adapter.owner, ':1.power')
        a.reconcile_brightness_async.assert_called_once_with('owner-return')

    def test_brightness_probe_deadline_setup_failure_leaves_no_operation(self):
        a = self.agent
        a.reconcile_brightness_async = mock.Mock()
        with mock.patch.object(fake_glib, 'timeout_add', side_effect=OSError('source setup')):
            agent_module.Agent.refresh_brightness_capability(a, 'owner-return')
        self.assertIsNone(a.brightness_probe_operation)
        self.assertFalse(a.brightness_available)
        a.reconcile_brightness_async.assert_called_once_with('owner-return')

    def test_power_refresh_deadline_setup_failure_completes_waiter(self):
        a = self.agent
        completed = []
        with mock.patch.object(fake_glib, 'timeout_add', side_effect=OSError('source setup')), \
                mock.patch.object(agent_module.LOG, 'warning'):
            agent_module.Agent.refresh_power_async(a, callback=completed.append,
                                                    reconcile_lid=False)
        self.assertIsNone(a.power_refresh)
        self.assertEqual(completed, [False])
        self.assertEqual(a.battery_discharge_state, 'unknown')

    def test_power_refresh_rejects_expired_parent_deadline_without_new_work(self):
        a = self.agent
        completed = []
        with mock.patch.object(agent_module.time, 'monotonic', return_value=100.0), \
                mock.patch.object(fake_glib, 'timeout_add') as timeout_add:
            agent_module.Agent.refresh_power_async(a, callback=completed.append,
                                                    deadline=99.0)
        self.assertEqual(completed, [False])
        self.assertIsNone(a.power_refresh)
        timeout_add.assert_not_called()

    def test_power_refresh_caps_source_to_parent_deadline(self):
        a = self.agent
        completed = []
        with mock.patch.object(agent_module.time, 'monotonic', return_value=100.0), \
                mock.patch.object(fake_glib, 'timeout_add', side_effect=OSError('source setup')) as timeout_add, \
                mock.patch.object(agent_module.LOG, 'warning'):
            agent_module.Agent.refresh_power_async(a, callback=completed.append,
                                                    deadline=100.75)
        self.assertEqual(timeout_add.call_args.args[0], 750)
        self.assertEqual(completed, [False])
        self.assertIsNone(a.power_refresh)

    def test_coalesced_power_waiter_observes_earlier_parent_deadline(self):
        a = self.agent
        completed = []
        a.power_refresh = {'deadline': 106.0, 'waiters': [],
                           'reconcile_lid': False, 'check_failsafe': False}
        with mock.patch.object(agent_module.time, 'monotonic', return_value=100.0):
            agent_module.Agent.refresh_power_async(
                a, callback=completed.append, deadline=100.75)
        self.assertEqual(len(a.power_refresh['waiters']), 1)
        self.assertEqual(len(fake_glib.sources), 1)
        source = next(iter(fake_glib.sources))
        self.assertEqual(fake_glib.sources[source][0], 0.75)
        fake_glib.sources[source][1]()
        self.assertEqual(completed, [False])
        a.power_refresh['waiters'][0](True)
        self.assertEqual(completed, [False])

    def test_coalesced_power_waiter_success_removes_earlier_deadline(self):
        a = self.agent
        completed = []
        a.power_refresh = {'deadline': 106.0, 'waiters': [],
                           'reconcile_lid': False, 'check_failsafe': False}
        with mock.patch.object(agent_module.time, 'monotonic', return_value=100.0):
            agent_module.Agent.refresh_power_async(
                a, callback=completed.append, deadline=100.75)
            source = next(iter(fake_glib.sources))
            a.power_refresh['waiters'][0](True)
        self.assertEqual(completed, [True])
        self.assertNotIn(source, fake_glib.sources)

    def test_power_snapshot_waits_for_all_devices_and_final_owner(self):
        a = self.agent
        names, root, first, second, display = (mock.Mock() for _ in range(5))
        remotes = {agent_module.UPOWER_PATH: root,
                   '/battery/one': first, '/battery/two': second,
                   '/battery/display': display}
        a.proxy = mock.Mock(side_effect=lambda _bus, owner, path, _iface:
                            names if owner == 'org.freedesktop.DBus' else remotes[path])
        a.publish = mock.Mock()
        a.handle_lid_change = mock.Mock()
        completed = []
        agent_module.Agent.refresh_power_async(a, callback=completed.append)
        names.GetNameOwner.call_args.kwargs['reply_handler'](':1.upower')
        for index, value in enumerate((True, True, False)):
            root.Get.call_args_list[index].kwargs['reply_handler'](value)
        root.EnumerateDevices.call_args.kwargs['reply_handler'](
            ['/battery/one', '/battery/two'])
        first.GetAll.call_args.kwargs['reply_handler'](
            {'Type': 2, 'PowerSupply': True, 'IsPresent': True,
             'State': 2, 'Percentage': 10.0})
        self.assertEqual(completed, [])
        second.GetAll.call_args.kwargs['reply_handler'](
            {'Type': 2, 'PowerSupply': True, 'IsPresent': True,
             'State': 1, 'Percentage': 20.0})
        root.GetDisplayDevice.call_args.kwargs['reply_handler']('/battery/display')
        display.Get.call_args.kwargs['reply_handler'](15.0)
        self.assertEqual(completed, [])
        self.assertIsNone(a.battery_percent)
        names.GetNameOwner.call_args.kwargs['reply_handler'](':1.upower')
        self.assertEqual(completed, [True])
        self.assertEqual((a.on_battery, a.lid_closed, a.battery_percent,
                          a.battery_discharge_state), (True, False, 15.0, 'discharging'))

    def test_power_snapshot_failed_device_is_all_unknown(self):
        a = self.agent
        names, root, first, second = (mock.Mock() for _ in range(4))
        remotes = {agent_module.UPOWER_PATH: root,
                   '/battery/one': first, '/battery/two': second}
        a.proxy = mock.Mock(side_effect=lambda _bus, owner, path, _iface:
                            names if owner == 'org.freedesktop.DBus' else remotes[path])
        a.publish = mock.Mock()
        completed = []
        agent_module.Agent.refresh_power_async(a, callback=completed.append)
        names.GetNameOwner.call_args.kwargs['reply_handler'](':1.upower')
        for index, value in enumerate((True, False)):
            root.Get.call_args_list[index].kwargs['reply_handler'](value)
        root.EnumerateDevices.call_args.kwargs['reply_handler'](
            ['/battery/one', '/battery/two'])
        first.GetAll.call_args.kwargs['reply_handler'](
            {'Type': 2, 'PowerSupply': True, 'IsPresent': True,
             'State': 2, 'Percentage': 10.0})
        second.GetAll.call_args.kwargs['error_handler'](RuntimeError('unreadable'))
        self.assertEqual(completed, [False])
        self.assertEqual((a.on_battery, a.lid_closed, a.battery_percent),
                         (None, None, None))

    def test_power_snapshot_rejects_replaced_owner_after_complete_read(self):
        a = self.agent
        names, root = mock.Mock(), mock.Mock()
        a.proxy = mock.Mock(side_effect=lambda _bus, owner, _path, _iface:
                            names if owner == 'org.freedesktop.DBus' else root)
        a.publish = mock.Mock()
        completed = []
        agent_module.Agent.refresh_power_async(a, callback=completed.append)
        names.GetNameOwner.call_args.kwargs['reply_handler'](':1.old')
        for index, value in enumerate((False, False)):
            root.Get.call_args_list[index].kwargs['reply_handler'](value)
        root.EnumerateDevices.call_args.kwargs['reply_handler']([])
        self.assertEqual(completed, [])
        names.GetNameOwner.call_args.kwargs['reply_handler'](':1.new')
        self.assertEqual(completed, [False])
        self.assertIsNone(a.on_battery)
        names.GetNameOwner.call_args.kwargs['reply_handler'](':1.old')
        self.assertEqual(completed, [False])

    def test_brightness_probe_rejects_owner_replacement_after_read(self):
        a = self.agent
        names, introspection, props = mock.Mock(), mock.Mock(), mock.Mock()
        a.proxy = mock.Mock(side_effect=lambda _bus, owner, _path, interface:
            names if owner == 'org.freedesktop.DBus' else
            introspection if interface == 'org.freedesktop.DBus.Introspectable' else props)
        a.reconcile_brightness_async = mock.Mock()
        with mock.patch.object(agent_module.LegacyBrightness, 'output_identity', return_value='output'):
            agent_module.Agent.refresh_brightness_capability(a, 'owner-return')
            names.GetNameOwner.call_args.kwargs['reply_handler'](':1.old')
            introspection.Introspect.call_args.kwargs['reply_handler'](
                '<node><interface name="org.gnome.SettingsDaemon.Power.Screen">'
                '<property name="Brightness" type="i" access="readwrite"/>'
                '</interface></node>')
            props.Get.call_args.kwargs['reply_handler'](70)
            names.GetNameOwner.call_args.kwargs['reply_handler'](':1.new')
        self.assertIsNone(a.brightness_adapter)
        self.assertFalse(a.brightness_available)
        a.reconcile_brightness_async.assert_called_once_with('owner-return')

    def test_brightness_probe_ignores_late_reply_after_owner_loss(self):
        a = self.agent
        a.proxy = mock.Mock(return_value=mock.Mock())
        a.reconcile_brightness_async = mock.Mock()
        agent_module.Agent.refresh_brightness_capability(a, 'periodic')
        callback = a.proxy.return_value.GetNameOwner.call_args.kwargs['reply_handler']
        a.on_owner_change(agent_module.BRIGHTNESS, ':1.old', '')
        callback(':1.old')
        self.assertIsNone(a.brightness_adapter)
        self.assertFalse(a.brightness_available)
        a.reconcile_brightness_async.assert_called_once_with('owner-loss')

    def test_brightness_probe_deadline_ignores_late_owner_reply(self):
        a = self.agent
        a.proxy = mock.Mock(return_value=mock.Mock())
        a.reconcile_brightness_async = mock.Mock()
        agent_module.Agent.refresh_brightness_capability(a, 'periodic')
        callback = a.proxy.return_value.GetNameOwner.call_args.kwargs['reply_handler']
        source = a.brightness_probe_operation['source']
        self.assertFalse(fake_glib.sources[source][1]())
        callback(':1.old')
        self.assertIsNone(a.brightness_probe_operation)
        self.assertFalse(a.brightness_available)
        a.reconcile_brightness_async.assert_called_once_with('periodic')

    def test_brightness_probe_rejects_late_final_owner_success(self):
        a = self.agent
        names, introspection, props = mock.Mock(), mock.Mock(), mock.Mock()
        a.proxy = mock.Mock(side_effect=lambda _bus, owner, _path, interface:
            names if owner == 'org.freedesktop.DBus' else
            introspection if interface == 'org.freedesktop.DBus.Introspectable' else props)
        a.reconcile_brightness_async = mock.Mock()
        with mock.patch.object(agent_module.LegacyBrightness, 'output_identity', return_value='output'), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=100):
            agent_module.Agent.refresh_brightness_capability(a, 'periodic')
            names.GetNameOwner.call_args.kwargs['reply_handler'](':1.old')
            introspection.Introspect.call_args.kwargs['reply_handler'](
                '<node><interface name="org.gnome.SettingsDaemon.Power.Screen">'
                '<property name="Brightness" type="i" access="readwrite"/>'
                '</interface></node>')
            props.Get.call_args.kwargs['reply_handler'](70)
            final_owner = names.GetNameOwner.call_args.kwargs['reply_handler']
        with mock.patch.object(agent_module.time, 'monotonic', return_value=113):
            final_owner(':1.old')
        self.assertIsNone(a.brightness_probe_operation)
        self.assertIsNone(a.brightness_adapter)
        self.assertFalse(a.brightness_available)
        a.reconcile_brightness_async.assert_called_once_with('periodic')

    def test_canceled_brightness_probe_does_not_dispatch_read_after_late_introspection(self):
        a = self.agent
        names, introspection, props = mock.Mock(), mock.Mock(), mock.Mock()
        a.proxy = mock.Mock(side_effect=lambda _bus, owner, _path, interface:
            names if owner == 'org.freedesktop.DBus' else
            introspection if interface == 'org.freedesktop.DBus.Introspectable' else props)
        agent_module.Agent.refresh_brightness_capability(a)
        names.GetNameOwner.call_args.kwargs['reply_handler'](':1.old')
        late = introspection.Introspect.call_args.kwargs['reply_handler']
        a.cancel_brightness_probe()
        late('<node><interface name="org.gnome.SettingsDaemon.Power.Screen">'
             '<property name="Brightness" type="i" access="readwrite"/>'
             '</interface></node>')
        props.Get.assert_not_called()

    def test_shell_brightness_owner_change_does_not_drop_legacy_adapter(self):
        a = self.agent
        adapter = a.brightness_adapter
        a.on_owner_change(agent_module.SHELL_BRIGHTNESS, ':1.old', '')
        self.assertIs(a.brightness_adapter, adapter)
        self.assertTrue(a.brightness_available)
        a.refresh_brightness_capability.assert_not_called()

    def test_dim_readback_exception_retains_prepared_record_without_healthy_claim(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        with mock.patch.object(a.brightness_adapter, 'read',
                               side_effect=[70, FakeDBusException('readback timed out')]):
            self.assertEqual(a.dim_brightness(), 'pending')
        self.assertEqual(a.brightness_record['phase'], 'set-confirmed')
        self.assertEqual(json.loads(a.brightness_path.read_text()), a.brightness_record)
        self.assertFalse(a.state()['brightnessDimmed'])
        self.assertTrue(a.state()['brightnessRecoveryPending'])
        self.assertEqual(a.brightness_adapter.value, 0)
        # A later readable target can be restored after the durable Set acknowledgement.
        self.assertEqual(a.restore_brightness(), 'restored')
        self.assertEqual(a.brightness_adapter.value, 70)

    def test_timed_out_brightness_set_keeps_prepared_journal_through_late_write(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        adapter = a.brightness_adapter
        adapter.write = mock.Mock(side_effect=FakeDBusException('Set timed out'))
        self.assertEqual(a.dim_brightness(), 'pending')
        self.assertEqual(a.brightness_record['phase'], 'prepared')
        record = json.loads(a.brightness_path.read_text())
        self.assertEqual(a.restore_brightness(), 'pending')
        self.assertEqual(adapter.value, 70)
        adapter.value = 0  # A dispatched remote Set applies after the timeout.
        self.assertEqual(a.restore_brightness(), 'pending')
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)
        self.assertTrue(a.brightness_recovery_pending())
        # Restart cannot infer whether that timed-out Set has finished.
        a.brightness_record = None
        a.brightness_journal_state = 'absent'
        self.assertEqual(a.recover_brightness(), 'loaded')
        self.assertEqual(a.restore_brightness(), 'pending')
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)

    def test_legacy_prepared_brightness_journal_remains_uncertain(self):
        a = self.agent
        record = {"schema": 2, "identity": a.brightness_adapter.output_identity(),
                  "machine": agent_module.machine_identity(), "before": 70,
                  "target": 0, "phase": "prepared"}
        a.brightness_path.write_text(json.dumps(record))
        a.brightness_record = None
        self.assertEqual(a.recover_brightness(), 'loaded')
        self.assertEqual(a.restore_brightness(), 'pending')
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)

    def test_method_proxy_disables_hidden_introspection(self):
        bus = mock.Mock()
        remote = mock.Mock()
        bus.get_object.return_value = remote
        with mock.patch.object(agent_module.dbus, 'Interface', create=True, return_value=remote) as interface:
            self.assertIs(agent_module.Agent.proxy(self.agent, bus, 'name', '/path', 'interface'), remote)
        bus.get_object.assert_called_once_with('name', '/path', introspect=False)
        interface.assert_called_once_with(remote, 'interface')

    def test_both_bus_disconnects_use_explicit_cleanup_instead_of_automatic_exit(self):
        a = self.agent
        a.session_bus = mock.Mock()
        a.system_bus = mock.Mock()
        a.watch_bus_disconnects()
        for bus, handler in ((a.session_bus, a.on_session_bus_disconnected),
                             (a.system_bus, a.on_system_bus_disconnected)):
            bus.add_signal_receiver.assert_called_once_with(handler, signal_name='Disconnected',
                dbus_interface='org.freedesktop.DBus.Local', path='/org/freedesktop/DBus/Local')

    def test_startup_disables_bus_exit_before_any_name_or_property_traffic(self):
        events = []
        session, system = mock.Mock(), mock.Mock()
        for name, bus in (('session', session), ('system', system)):
            bus.get_is_connected.return_value = True
            bus.set_exit_on_disconnect.side_effect = lambda value, name=name: events.append((name, value))
            bus.add_signal_receiver.side_effect = lambda *args, name=name, **kwargs: events.append((name, kwargs.get('signal_name')))
        with mock.patch.object(dbus, 'SessionBus', return_value=session, create=True), \
                mock.patch.object(dbus, 'SystemBus', return_value=system, create=True), \
                mock.patch.object(dbus.service, 'BusName', side_effect=lambda *args, **kwargs: events.append(('name', 'requested')), create=True), \
                mock.patch.object(dbus.mainloop.glib, 'DBusGMainLoop', create=True), \
                mock.patch.object(fake_glib, 'MainLoop', return_value=mock.Mock(), create=True), \
                mock.patch.object(agent_module, 'data_home', return_value=Path(self.directory.name)), \
                mock.patch.object(agent_module.Agent, 'load_settings', side_effect=lambda: events.append(('settings', 'loaded'))), \
                mock.patch.object(agent_module.Agent, 'refresh_brightness_capability'), \
                mock.patch.object(agent_module.Agent, 'refresh_power_async'), \
                mock.patch.object(agent_module.Agent, 'reconcile_brightness'):
            agent_module.Agent()
        self.assertEqual(events[:3], [('session', False), ('system', False), ('name', 'requested')])
        for name in ('session', 'system'):
            self.assertLess(events.index((name, 'Disconnected')), events.index(('settings', 'loaded')))
        session.get_is_connected.assert_has_calls([mock.call(), mock.call()])
        system.get_is_connected.assert_has_calls([mock.call(), mock.call()])

    def test_startup_bus_construction_failure_closes_existing_connection(self):
        session = mock.Mock()
        with mock.patch.object(dbus, 'SessionBus', return_value=session, create=True), \
                mock.patch.object(dbus, 'SystemBus', side_effect=RuntimeError('system bus failed'), create=True), \
                mock.patch.object(dbus.mainloop.glib, 'DBusGMainLoop', create=True):
            with self.assertRaisesRegex(RuntimeError, 'system bus failed'):
                agent_module.Agent()
        session.close.assert_called_once_with()

    def test_startup_signal_registration_failure_closes_both_buses(self):
        session, system = mock.Mock(), mock.Mock()
        system.add_signal_receiver.side_effect = RuntimeError('signal registration failed')
        with mock.patch.object(dbus, 'SessionBus', return_value=session, create=True), \
                mock.patch.object(dbus, 'SystemBus', return_value=system, create=True), \
                mock.patch.object(dbus.service, 'BusName', create=True), \
                mock.patch.object(dbus.mainloop.glib, 'DBusGMainLoop', create=True), \
                mock.patch.object(fake_glib, 'MainLoop', return_value=mock.Mock(), create=True), \
                mock.patch.object(agent_module, 'data_home', return_value=Path(self.directory.name)):
            with self.assertRaisesRegex(RuntimeError, 'signal registration failed'):
                agent_module.Agent()
        session.close.assert_called_once_with()
        system.close.assert_called_once_with()
        self.assertEqual(fake_glib.sources, {})

    def test_startup_disconnected_without_signal_cleans_locally_and_refuses_run(self):
        a = self.agent
        a.session_bus, a.system_bus = mock.Mock(), mock.Mock()
        a.session_bus.get_is_connected.return_value = False
        a.system_bus.get_is_connected.return_value = False
        a.deadline, a.timer_phase = 60, 'running'
        a.enabled = a.desired = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        journal = a.brightness_path.read_text()
        a.fd, a.lid_fd = 101, 102
        with mock.patch.object(agent_module.os, 'close') as close:
            with self.assertRaises(FakeDBusException):
                a.check_startup_connections()
            close.assert_has_calls([mock.call(102), mock.call(101)])
            close.assert_called_with(101)
        self.assertIsNone(a.deadline)
        self.assertTrue(a.shutting_down)
        self.assertTrue(a.exit_failure)
        self.assertFalse(a.tick())
        self.assertFalse(a.reconcile())
        self.assertEqual(a.brightness_path.read_text(), journal)
        self.assertEqual(a.brightness_adapter.writes, [0])

    def test_repeated_disconnect_during_stop_never_redims_or_reacquires(self):
        a = self.agent
        a.enabled = a.desired = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        self.held_cookie()
        remote = mock.Mock()
        def disconnect(_cookie, **_kwargs):
            a.on_session_bus_disconnected()
            a.on_system_bus_disconnected()
            a.on_session_bus_disconnected()
            _kwargs['reply_handler']()
        remote.Uninhibit.side_effect = disconnect
        remote.GetNameOwner.side_effect = lambda _name, **kwargs: kwargs['reply_handler'](':1.gnome')
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True)
        with mock.patch.object(a, 'proxy', return_value=remote), \
                mock.patch.object(a, 'acquire') as acquire:
            a.on_system_bus_disconnected()
            a.on_system_bus_disconnected()
            self.assertFalse(a.tick())
            self.assertFalse(a.reconcile())
            acquire.assert_not_called()
        self.assertEqual(a.brightness_adapter.writes, [0, 70])
        remote.Uninhibit.assert_called_once()
        self.assertTrue(a.exit_failure)
        self.assertIsNone(a.fd)
        self.assertIsNone(a.lid_fd)

    def test_system_bus_disconnect_restores_before_release_and_exits_for_restart(self):
        a = self.agent
        a.enabled = a.desired = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        self.held_cookie()
        events = []
        write = a.brightness_adapter.write
        a.brightness_adapter.write = lambda value: (events.append('brightness'), write(value))[1]
        remote = mock.Mock()
        remote.GetNameOwner.side_effect = lambda _name, **kwargs: kwargs['reply_handler'](':1.gnome')
        remote.Uninhibit.side_effect = lambda _cookie, **kwargs: (
            events.append('uninhibit'), kwargs['reply_handler']())
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True)
        with mock.patch.object(a, 'proxy', return_value=remote):
            a.on_system_bus_disconnected()
        self.assertEqual(events[:2], ['brightness', 'uninhibit'])
        self.assertTrue(a.exit_failure)
        self.assertTrue(a.shutting_down)
        self.assertFalse(a.enabled)
        a.loop.quit.assert_called_once()

    def test_timer_transport_failure_keeps_tick_and_countdown_alive(self):
        a = self.agent
        a.deadline = 60
        a.timer_phase = 'running'
        a.TimerChanged.side_effect = FakeDBusException('disconnected')
        boot, mono = self.clock(0, 0)
        with boot, mono:
            self.assertTrue(a.tick())
        self.assertEqual(a.deadline, 60)
        self.assertEqual(a.timer_phase, 'running')

    def test_state_transport_failure_does_not_abort_stop_or_committed_action(self):
        a = self.agent
        a.publish = types.MethodType(agent_module.Agent.publish, a)
        a.StateChanged = mock.Mock(side_effect=FakeDBusException('disconnected'))
        a.SetLidMode(False, reply=mock.Mock(), error=mock.Mock())
        self.assertFalse(a.lid_mode)
        self.assertFalse(json.loads(a.settings_path.read_text())['lid_mode'])
        a.stop()
        a.loop.quit.assert_called_once()
        self.assertIsNone(a.fd)
        self.assertIsNone(a.lid_fd)
        # Programming/state construction failures remain visible.
        a.StateChanged.side_effect = ValueError('bad signal payload')
        with self.assertRaises(ValueError):
            a.publish()

    def setUp(self):
        fake_glib.sources.clear()
        fake_glib.removed.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        identity = mock.patch.object(agent_module, "machine_identity", return_value="test-machine")
        identity.start()
        self.addCleanup(identity.stop)
        boot_clock = mock.patch.object(agent_module, 'boottime', return_value=0)
        boot_clock.start()
        self.addCleanup(boot_clock.stop)
        a = agent_module.Agent.__new__(agent_module.Agent)
        self.agent = a
        a.brightness_path = Path(self.directory.name) / "recovery.json"
        a.settings_path = Path(self.directory.name) / "preferences.json"
        a.brightness_record = None
        a.brightness_error = ""
        a.brightness_journal_state = 'absent'
        a.brightness_retry_after = 0
        a.brightness_retry_pending = False
        a.brightness_redim_suppressed = False
        a.brightness_release_pending = False
        a.dimming_verifications = set()
        a.dimming_verification = None
        a.brightness_adapter = FakeBrightness()
        a.brightness_available = True
        a.timer_phase = 'idle'
        a.timer_outcome = ''
        a.deadline = None
        a.require_lid = False
        a.require_prevention = False
        a.last_timer_state = -1
        a.clock_gap = agent_module.ClockGapSample(0, 0, 0, 0)
        a.logind_owner_generation = 0
        a.last_prepare_signal = None
        a.sleep_tx = a.idle_sleep_transaction()
        a.sleep_reconcile_operation = None
        a.suspend_preflight_operation = None
        a.suspend_attempt = None
        a.failsafe_evaluation = None
        a.sleep_outcome = 'none'
        a.suspend_request_outcome = 'none'
        a.system_bus = object()
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True,
                                               close=mock.Mock())
        a.session_cookie = None
        a.session_cookie_state = 'absent'
        a.session_cookie_owner = ''
        a.session_owner_generation = 0
        a.session_release_operation = None
        a.prevention_release = None
        a.prevention_acquisition = None
        a.shell_owner_generation = 0
        a.inhibitor_outcome = 'absent'
        a.release_attempts = 0
        a.release_retry_after = 0
        a.release_retry_source = 0
        a.exit_failure = False
        a.shutting_down = False
        a.stop_deadline_source = 0
        a.panel_runtime_version = ''
        a.panel_runtime_sender = ''
        a.panel_registration_cancellers = set()
        a.prevention_enable_requests = set()
        a.prevention_disable_requests = set()
        a.dimming_disable_requests = set()
        a.lid_error = ''
        a.last_error = ''
        a.acquisition_error = ''
        a.enabled = False
        a.desired = False
        a.lid_mode = False
        a.lid_dimming = False
        a.lid_closed = False
        a.lid_fd = None
        a.lid_acquisition = None
        a.lid_mode_request = None
        a.fd = None
        a.sleep_fd_owner = ''
        a.lid_outcome = 'unverified'
        a.failsafe = False
        a.failsafe_triggered = False
        a.threshold = 20
        a.on_battery = False
        a.battery_discharging = False
        a.battery_discharge_state = 'unknown'
        a.battery_percent = None
        a.power_generation = 0
        a.power_refresh = None
        a.power_last_known_lid = None
        a.timer_start = None
        a.brightness_probe_generation = 0
        a.brightness_probe_operation = None
        a.brightness_dim_operation = None
        a.brightness_restore_operation = None
        a.recovering = False
        a.last_timer_minutes = 30
        a.last_timer_require_lid = False
        a.last_timer_require_prevention = False
        a.session_manager_owner = mock.Mock(return_value=':1.gnome')
        a.publish = mock.Mock()
        a.notify = mock.Mock()
        a.refresh_power = mock.Mock()
        a.refresh_power_async = mock.Mock(side_effect=lambda callback=None, **_kwargs:
                                          callback(True) if callback is not None else None)
        a.refresh_brightness_capability = mock.Mock()
        a.TimerChanged = mock.Mock()
        a.check_failsafe = mock.Mock()
        a.retry_lid_lock = mock.Mock()
        a.loop = types.SimpleNamespace(quit=mock.Mock())

    def clock(self, boot, mono):
        return (mock.patch.object(agent_module, 'boottime', return_value=boot),
                mock.patch.object(agent_module.time, 'monotonic', return_value=mono))

    def begin_request(self, reply='accepted', origin='timer', reason='Countdown elapsed',
                      baseline=0, requested=0):
        self.agent.sleep_tx = {
            'phase': 'request-pending', 'origin': origin, 'reason': reason,
            'requested_at': requested, 'request_reply': reply,
            'saw_prepare_true': False, 'saw_prepare_false': False,
            'gap_baseline': agent_module.ClockGapSample(baseline, requested, baseline, baseline),
            'uncertain_since': requested,
            'owner_generation': self.agent.logind_owner_generation,
        }
        self.agent.suspend_request_outcome = reply

    def drive_preflight(self, preparing=False, login_owner=':1.login',
                        session_owner=':1.gnome', capability='yes'):
        """Complete only the fake read callbacks used by current async preflight."""
        def proxy(bus, _owner, _path, interface):
            if interface == 'org.freedesktop.DBus':
                owner = login_owner if bus is self.agent.system_bus else session_owner
                return types.SimpleNamespace(GetNameOwner=lambda _name, **kwargs:
                    kwargs['reply_handler'](owner))
            if interface == 'org.freedesktop.DBus.Properties':
                return types.SimpleNamespace(Get=lambda _iface, _key, **kwargs:
                    kwargs['reply_handler'](preparing))
            if interface == 'org.freedesktop.login1.Manager':
                return types.SimpleNamespace(CanSuspend=lambda **kwargs:
                    kwargs['reply_handler'](capability))
            raise AssertionError('Unexpected preflight interface: ' + interface)

        self.agent.proxy = mock.Mock(side_effect=proxy)
        outcomes = []
        self.agent.suspend_preflight_async(lambda *result: outcomes.append(result))
        self.assertEqual(len(outcomes), 1)
        return outcomes[0]

    def queue_reconcile_bus(self, preparing=False, login_owner=':1.login'):
        """Install a fake logind read whose callbacks require explicit delivery."""
        pending = []
        def proxy(_bus, _owner, _path, interface):
            if interface == 'org.freedesktop.DBus':
                return types.SimpleNamespace(GetNameOwner=lambda _name, **kwargs:
                    pending.append(lambda: kwargs['reply_handler'](login_owner)))
            if interface == 'org.freedesktop.DBus.Properties':
                return types.SimpleNamespace(Get=lambda _iface, _key, **kwargs:
                    pending.append(lambda: kwargs['reply_handler'](preparing)))
            raise AssertionError('Unexpected reconcile interface: ' + interface)

        self.agent.proxy = mock.Mock(side_effect=proxy)
        def drain():
            while pending:
                pending.pop(0)()
        return self.agent.proxy, drain

    def drive_reconcile(self, trigger, preparing=False, login_owner=':1.login'):
        """Deliver a fake logind owner/property/final-owner read in order."""
        proxy, drain = self.queue_reconcile_bus(preparing, login_owner)
        self.agent.reconcile_sleep_state(trigger)
        drain()
        return proxy

    def allow_async_sleep_steps(self):
        """Drive only fake successful prerequisite callbacks for an attempt."""
        self.agent.suspend_preflight_async = mock.Mock(side_effect=lambda callback, **_kwargs:
            callback(True, '', ':1.login'))
        self.agent.release_async = mock.Mock(side_effect=lambda callback, **_kwargs:
            callback(True))
        self.agent.refresh_power_async = mock.Mock(side_effect=lambda callback=None, **_kwargs:
            callback(True) if callback is not None else None)

    # Brightness journal and desired-state coordinator

    def test_journal_loading_never_writes(self):
        self.agent.dim_brightness()
        self.agent.brightness_adapter.writes.clear()
        self.agent.brightness_record = None
        self.assertEqual(self.agent.recover_brightness(), 'loaded')
        self.assertEqual(self.agent.brightness_adapter.writes, [])
        self.assertEqual(self.agent.brightness_journal_state, 'pending')

    def test_closed_lid_wake_restores_then_redims_through_fresh_journal(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        self.agent.brightness_adapter.writes.clear()
        self.assertEqual(self.agent.reconcile_brightness('wake'), 'redimmed')
        self.assertEqual(self.agent.brightness_adapter.writes, [70, 0])
        self.assertEqual(self.agent.brightness_record['before'], 70)
        self.assertEqual(self.agent.brightness_record['phase'], 'applied')

    def test_open_lid_wake_restores_once_without_redim(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        self.agent.lid_closed = False
        self.agent.brightness_adapter.writes.clear()

        self.assertEqual(self.agent.reconcile_brightness('wake'), 'restored')
        self.assertEqual(self.agent.brightness_adapter.writes, [70])
        self.assertIsNone(self.agent.brightness_record)

    def test_manual_conflict_is_preserved_and_periodic_redim_suppressed(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        self.agent.brightness_adapter.value = 35
        self.assertEqual(self.agent.reconcile_brightness('wake'), 'manual-change-preserved')
        self.assertEqual(self.agent.brightness_adapter.value, 35)
        self.assertTrue(self.agent.brightness_redim_suppressed)
        self.agent.reconcile_brightness('periodic')
        self.assertEqual(self.agent.brightness_adapter.value, 35)
        self.agent.handle_lid_change()
        self.assertEqual(self.agent.brightness_adapter.value, 35)
        self.assertTrue(self.agent.brightness_redim_suppressed)

    def test_inhibitor_reacquisition_while_closed_preserves_manual_conflict(self):
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.brightness_adapter.value = 35
        self.agent.brightness_redim_suppressed = True
        login = types.SimpleNamespace(Inhibit=mock.Mock(
            return_value=types.SimpleNamespace(take=mock.Mock(return_value=91))))
        gnome = types.SimpleNamespace(Inhibit=mock.Mock(return_value=27))
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(return_value=':1.login'))
        self.agent.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
                                     names if name == 'org.freedesktop.DBus' else
                                     login if name == ':1.login' else gnome)

        self.agent.acquire()

        self.assertTrue(self.agent.enabled)
        self.assertTrue(self.agent.brightness_redim_suppressed)
        self.assertEqual(self.agent.brightness_adapter.value, 35)
        self.assertEqual(self.agent.brightness_adapter.writes, [])

    def test_periodic_does_not_cycle_healthy_dim(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        self.agent.brightness_adapter.writes.clear()
        self.assertEqual(self.agent.reconcile_brightness('periodic'), 'unchanged')
        self.assertEqual(self.agent.brightness_adapter.writes, [])

    def test_periodic_failed_operation_retry_restores_then_redims(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        self.agent.brightness_retry_pending = True
        self.agent.brightness_adapter.writes.clear()
        self.assertEqual(self.agent.reconcile_brightness('periodic'), 'redimmed')
        self.assertEqual(self.agent.brightness_adapter.writes, [70, 0])
        self.assertFalse(self.agent.brightness_retry_pending)

    def test_prevention_reconcile_does_not_clear_manual_suppression(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.brightness_redim_suppressed = True
        self.assertEqual(self.agent.reconcile_brightness('prevention-enable'), 'unchanged')
        self.assertTrue(self.agent.brightness_redim_suppressed)
        self.assertEqual(self.agent.brightness_adapter.writes, [])

    def test_upower_lid_close_rearms_manual_conflict_suppression(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = False
        self.agent.brightness_redim_suppressed = True
        self.agent.brightness_adapter.value = 35
        self.agent.property = mock.Mock(side_effect=[False, True, True])
        self.agent.proxy = mock.Mock(return_value=types.SimpleNamespace(
            GetNameOwner=mock.Mock(return_value=':1.upower'),
            EnumerateDevices=mock.Mock(return_value=[])))
        self.agent.refresh_power = agent_module.Agent.refresh_power.__get__(self.agent)

        self.agent.refresh_power()

        self.assertFalse(self.agent.brightness_redim_suppressed)
        self.assertEqual(self.agent.brightness_adapter.value, 0)

    def test_upower_unknown_to_closed_preserves_manual_conflict_suppression(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = None
        self.agent.brightness_redim_suppressed = True
        self.agent.brightness_adapter.value = 35
        self.agent.property = mock.Mock(side_effect=[False, True, True])
        self.agent.proxy = mock.Mock(return_value=types.SimpleNamespace(
            GetNameOwner=mock.Mock(return_value=':1.upower'),
            EnumerateDevices=mock.Mock(return_value=[])))
        self.agent.refresh_power = agent_module.Agent.refresh_power.__get__(self.agent)

        self.agent.refresh_power()

        self.assertTrue(self.agent.brightness_redim_suppressed)
        self.assertEqual(self.agent.brightness_adapter.value, 35)
        self.assertEqual(self.agent.brightness_adapter.writes, [])

    def test_owner_return_closed_lid_restores_then_redims(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        adapter = self.agent.brightness_adapter
        adapter.writes.clear()
        self.agent.refresh_brightness_capability.side_effect = lambda trigger: (
            setattr(self.agent, 'brightness_adapter', adapter),
            setattr(self.agent, 'brightness_available', True),
            self.agent.reconcile_brightness(trigger))
        self.agent.on_owner_change(agent_module.BRIGHTNESS, '', ':1.power')
        self.assertEqual(adapter.writes, [70, 0])

    def test_service_loss_and_return_closed_lid_ends_dimmed(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        adapter = self.agent.brightness_adapter
        adapter.writes.clear()
        self.agent.on_owner_change(agent_module.BRIGHTNESS, ':1.old', '')
        self.agent.refresh_brightness_capability.side_effect = lambda trigger: (
            setattr(self.agent, 'brightness_adapter', adapter),
            setattr(self.agent, 'brightness_available', True),
            self.agent.reconcile_brightness(trigger))
        self.agent.on_owner_change(agent_module.BRIGHTNESS, '', ':1.new')
        self.assertEqual(adapter.writes, [70, 0])
        self.assertEqual(adapter.value, 0)

    def test_service_return_after_lid_open_restores_only(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        adapter = self.agent.brightness_adapter
        adapter.writes.clear()
        self.agent.on_owner_change(agent_module.BRIGHTNESS, ':1.old', '')
        self.agent.lid_closed = False
        self.agent.refresh_brightness_capability.side_effect = lambda trigger: (
            setattr(self.agent, 'brightness_adapter', adapter),
            setattr(self.agent, 'brightness_available', True),
            self.agent.reconcile_brightness(trigger))
        self.agent.on_owner_change(agent_module.BRIGHTNESS, '', ':1.new')
        self.assertEqual(adapter.writes, [70])
        self.assertEqual(adapter.value, 70)
        self.assertIsNone(self.agent.brightness_record)

    def test_owner_loss_with_journal_stays_pending_without_write(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        adapter = self.agent.brightness_adapter
        adapter.writes.clear()
        self.agent.on_owner_change(agent_module.BRIGHTNESS, ':1.power', '')
        self.assertIsNone(self.agent.brightness_adapter)
        self.assertTrue(self.agent.brightness_retry_pending)
        self.assertTrue(self.agent.has_brightness_journal())
        self.assertEqual(adapter.writes, [])
        self.assertIn('service unavailable', self.agent.brightness_error)

    def test_restore_with_record_and_no_adapter_is_pending(self):
        self.agent.dim_brightness()
        self.agent.brightness_adapter = None
        self.assertEqual(self.agent.restore_brightness(), 'pending')
        self.assertTrue(self.agent.brightness_retry_pending)
        self.assertTrue(self.agent.has_brightness_journal())

    def test_foreign_machine_journal_is_retained_without_write(self):
        self.agent.dim_brightness()
        record = json.loads(self.agent.brightness_path.read_text())
        record['machine'] = 'foreign'
        agent_module.atomic_json(self.agent.brightness_path, record)
        self.agent.brightness_record = None
        self.agent.brightness_adapter.writes.clear()
        self.assertEqual(self.agent.recover_brightness(), 'invalid')
        self.assertEqual(self.agent.brightness_adapter.writes, [])
        self.assertTrue(self.agent.brightness_path.exists())

    def test_persisted_record_change_is_retained_without_restore_write(self):
        self.agent.dim_brightness()
        record = json.loads(self.agent.brightness_path.read_text())
        record['before'] = 71
        agent_module.atomic_json(self.agent.brightness_path, record)
        self.agent.brightness_adapter.writes.clear()
        self.assertEqual(self.agent.restore_brightness(), 'invalid')
        self.assertEqual(self.agent.brightness_adapter.writes, [])
        self.assertTrue(self.agent.brightness_path.exists())

    def test_output_identity_change_is_retained_without_restore_write(self):
        self.agent.dim_brightness()
        self.agent.brightness_adapter.identity = 'gnome-settings-daemon:built-in-panel-changed'
        self.agent.brightness_adapter.writes.clear()
        self.assertEqual(self.agent.restore_brightness(), 'invalid')
        self.assertEqual(self.agent.brightness_adapter.writes, [])
        self.assertTrue(self.agent.brightness_path.exists())

    def test_failed_redim_keeps_recovery_diagnostic(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        original = self.agent.brightness_adapter.write
        calls = 0
        def fail_second(value):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise FakeDBusException('write failed')
            original(value)
        self.agent.brightness_adapter.write = fail_second
        self.assertEqual(self.agent.reconcile_brightness('wake'), 'pending')
        self.assertTrue(self.agent.brightness_retry_pending)
        self.assertTrue(self.agent.has_brightness_journal())

    def test_no_adapter_never_writes_or_blocks_prevention(self):
        self.agent.brightness_adapter = None
        self.agent.brightness_available = False
        self.agent.enabled = True
        self.agent.lid_dimming = False
        self.assertIn(self.agent.reconcile_brightness('startup'), ('unchanged', 'invalid'))
        self.assertTrue(self.agent.enabled)

    def test_startup_closed_lid_recovers_existing_journal_once_with_prevention_off(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        self.agent.brightness_adapter.writes.clear()
        self.agent.enabled = False
        self.agent.lid_closed = None
        self.agent.brightness_record = None
        self.agent.brightness_journal_state = 'absent'
        self.agent.property = mock.Mock(side_effect=[False, True, True])
        self.agent.proxy = mock.Mock(return_value=types.SimpleNamespace(
            GetNameOwner=mock.Mock(return_value=':1.upower'),
            EnumerateDevices=mock.Mock(return_value=[])))
        self.agent.refresh_power = agent_module.Agent.refresh_power.__get__(self.agent)

        self.agent.refresh_power()
        self.agent.reconcile_brightness('startup')

        self.assertEqual(self.agent.brightness_adapter.writes, [70])
        self.assertIsNone(self.agent.brightness_record)

    # Dedicated GNOME inhibitor release retries

    def held_cookie(self):
        self.agent.session_cookie = 7
        self.agent.session_cookie_state = 'held'
        self.agent.session_cookie_owner = ':1.gnome'
        self.agent.enabled = True

    def fake_release_proxy(self, uninhibit):
        """Pin the issuing GNOME owner before delivering a fake release result."""
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(
            side_effect=lambda _name, **kwargs: kwargs['reply_handler'](':1.gnome')))
        gnome = types.SimpleNamespace(Uninhibit=mock.Mock(side_effect=uninhibit))
        self.agent.proxy = mock.Mock(side_effect=lambda _bus, owner, _path, _interface:
                                     names if owner == 'org.freedesktop.DBus' else gnome)
        return names, gnome

    def test_release_failure_schedules_exactly_one_five_second_source(self):
        self.held_cookie()
        names, gnome = self.fake_release_proxy(lambda _cookie, **kwargs:
            kwargs['error_handler'](FakeDBusException('timeout')))
        outcomes = []
        self.agent.release_async(outcomes.append, brightness_reconciled=True)
        self.assertEqual(outcomes, [False])
        self.assertEqual(len(fake_glib.sources), 1)
        self.assertEqual(next(iter(fake_glib.sources.values()))[0], 5)
        self.agent.retry_session_release()
        self.assertEqual(len(fake_glib.sources), 1)
        names.GetNameOwner.assert_called_once()
        gnome.Uninhibit.assert_called_once()

    def test_release_retry_success_cancels_pending_state(self):
        self.held_cookie()
        attempts = 0
        def uninhibit(_cookie, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                kwargs['error_handler'](FakeDBusException('timeout'))
            else:
                kwargs['reply_handler']()
        names, gnome = self.fake_release_proxy(uninhibit)
        outcomes = []
        self.agent.release_async(outcomes.append, brightness_reconciled=True)
        self.assertEqual(outcomes, [False])
        source = self.agent.release_retry_source
        callback = fake_glib.sources[source][1]
        callback()
        self.assertEqual(self.agent.session_cookie_state, 'absent')
        self.assertEqual(self.agent.release_retry_source, 0)
        self.assertEqual(names.GetNameOwner.call_count, 2)
        self.assertEqual(gnome.Uninhibit.call_count, 2)

    def test_release_disconnects_after_three_total_attempts(self):
        self.held_cookie()
        names, gnome = self.fake_release_proxy(lambda _cookie, **kwargs:
            kwargs['error_handler'](FakeDBusException('timeout')))
        bus = types.SimpleNamespace(close=mock.Mock(), get_is_connected=lambda: True)
        self.agent.session_bus = bus
        outcomes = []
        self.agent.release_async(outcomes.append, brightness_reconciled=True)
        self.assertEqual(outcomes, [False])
        for _ in range(2):
            source = self.agent.release_retry_source
            fake_glib.sources[source][1]()
        self.assertEqual(names.GetNameOwner.call_count, 3)
        self.assertEqual(gnome.Uninhibit.call_count, 3)
        bus.close.assert_called_once()
        self.assertTrue(self.agent.exit_failure)

    def test_disconnect_closes_cookie_bus_when_publication_fails(self):
        self.held_cookie()
        bus = types.SimpleNamespace(close=mock.Mock())
        self.agent.session_bus = bus
        self.agent.publish.side_effect = RuntimeError('signal publication failed')
        self.agent.fd = 91
        self.agent.lid_fd = 92
        with mock.patch.object(agent_module.os, 'close') as close:
            self.agent.disconnect_session_bus('unknown acquisition outcome')
        bus.close.assert_called_once()
        self.assertEqual({call.args[0] for call in close.call_args_list}, {91, 92})
        self.assertIsNone(self.agent.fd)
        self.assertIsNone(self.agent.lid_fd)
        self.assertTrue(self.agent.shutting_down)
        self.assertEqual(self.agent.session_cookie_state, 'absent')
        self.assertTrue(self.agent.exit_failure)
        self.agent.loop.quit.assert_called_once()

    def test_disconnect_closes_local_inhibitors_even_when_bus_close_fails(self):
        bus = types.SimpleNamespace(close=mock.Mock(side_effect=OSError('bus close failed')))
        self.agent.session_bus = bus
        self.agent.fd = 91
        self.agent.lid_fd = 92
        with mock.patch.object(agent_module.os, 'close') as close:
            self.agent.disconnect_session_bus('release uncertain')
        self.assertEqual({call.args[0] for call in close.call_args_list}, {91, 92})
        self.assertIsNone(self.agent.fd)
        self.assertIsNone(self.agent.lid_fd)
        self.assertTrue(self.agent.exit_failure)
        self.agent.loop.quit.assert_called_once()

    def test_owner_loss_cancels_release_source(self):
        self.held_cookie()
        self.agent.session_cookie_state = 'release-pending'
        self.agent.release_retry_source = fake_glib.timeout_add_seconds(5, lambda: False)
        source = self.agent.release_retry_source
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lambda _name, **kwargs:
            kwargs['error_handler'](FakeDBusException(
                'unique owner gone', name='org.freedesktop.DBus.Error.NameHasNoOwner'))))
        self.agent.proxy = mock.Mock(return_value=names)
        self.agent.on_owner_change(agent_module.SESSION, ':1.gnome', ':1.new')
        self.assertEqual(self.agent.session_cookie_state, 'absent')
        self.assertEqual(self.agent.release_retry_source, 0)
        self.assertIn(source, fake_glib.removed)
        names.GetNameOwner.assert_called_once()
        self.assertEqual(names.GetNameOwner.call_args.args, (':1.gnome',))

    def test_session_name_change_releases_old_unique_owner_cookie(self):
        self.held_cookie()
        names, gnome = self.fake_release_proxy(lambda _cookie, **kwargs:
            kwargs['reply_handler']())
        self.agent.on_owner_change(agent_module.SESSION, ':1.gnome', ':1.new')
        names.GetNameOwner.assert_called_once()
        self.assertEqual(names.GetNameOwner.call_args.args, (':1.gnome',))
        gnome.Uninhibit.assert_called_once()
        self.assertEqual(gnome.Uninhibit.call_args.args, (7,))
        self.assertEqual(self.agent.session_cookie_state, 'absent')

    def test_stop_cancels_release_source(self):
        self.agent.release_retry_source = fake_glib.timeout_add_seconds(5, lambda: False)
        source = self.agent.release_retry_source
        self.agent.release = mock.Mock(return_value=True)
        self.agent.reconcile_brightness = mock.Mock()
        self.agent.stop()
        self.assertEqual(self.agent.release_retry_source, 0)
        self.assertIn(source, fake_glib.removed)

    def test_session_bus_disconnect_cancels_release_source_and_exits(self):
        self.held_cookie()
        self.agent.session_cookie_state = 'release-pending'
        self.agent.release_retry_source = fake_glib.timeout_add_seconds(5, lambda: False)
        source = self.agent.release_retry_source
        self.agent.fd = 91
        self.agent.lid_fd = 92
        with mock.patch.object(agent_module.os, 'close') as close:
            self.agent.on_session_bus_disconnected()
        self.assertIn(source, fake_glib.removed)
        self.assertEqual(self.agent.session_cookie_state, 'absent')
        self.assertFalse(self.agent.enabled)
        self.assertTrue(self.agent.exit_failure)
        self.assertEqual({call.args[0] for call in close.call_args_list}, {91, 92})
        self.agent.loop.quit.assert_called_once()

    # Sleep transaction and timer behavior

    def test_accepted_request_true_false_with_gap_proves_resume_once(self):
        self.begin_request()
        self.agent.sleep_tx['saw_prepare_true'] = True
        self.agent.sleep_tx['saw_prepare_false'] = True
        with mock.patch.object(agent_module, 'boottime', return_value=20),                 mock.patch.object(agent_module.time, 'monotonic', return_value=10):
            self.drive_reconcile('test', preparing=False)
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')
        self.assertEqual(self.agent.suspend_request_outcome, 'proven-suspend')
        self.agent.notify.assert_called_once()

    def test_accepted_request_without_signals_settles_without_stale_reason(self):
        self.begin_request()
        with mock.patch.object(agent_module, 'boottime', return_value=6),                 mock.patch.object(agent_module.time, 'monotonic', return_value=6):
            proxy = self.drive_reconcile('test', preparing=False)
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'accepted-no-suspend')
        self.assertEqual(self.agent.suspend_request_outcome,
                         'accepted-without-observed-suspend')
        self.assertIn('accepted but no suspend observed', self.agent.timer_outcome)
        self.agent.notify.assert_called_once()
        self.assertEqual(proxy.call_count, 3)

    def test_accepted_request_with_later_clock_gap_proves_suspend(self):
        self.begin_request(reply='accepted')
        with mock.patch.object(agent_module, 'boottime', return_value=20), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=10):
            self.drive_reconcile('tick', preparing=False)
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')
        self.assertEqual(self.agent.suspend_request_outcome, 'proven-suspend')

    def test_accepted_request_with_true_and_missing_false_is_failed_preparation(self):
        self.begin_request(reply='accepted')
        self.agent.sleep_tx['saw_prepare_true'] = True
        self.agent.sleep_tx['phase'] = 'preparing'
        with mock.patch.object(agent_module, 'boottime', return_value=6), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=6):
            proxy = self.drive_reconcile('tick', preparing=False)
        self.assertEqual(self.agent.sleep_outcome, 'failed-preparation')
        self.assertEqual(self.agent.suspend_request_outcome,
                         'accepted-without-observed-suspend')
        self.assertIn('accepted but no suspend observed', self.agent.timer_outcome)
        self.agent.notify.assert_called_once()
        self.assertEqual(proxy.call_count, 3)

    def test_no_signals_then_clock_gap_is_proven_suspend(self):
        self.begin_request(reply='unknown')
        with mock.patch.object(agent_module, 'boottime', return_value=20),                 mock.patch.object(agent_module.time, 'monotonic', return_value=10):
            self.drive_reconcile('tick', preparing=None)
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')

    def test_duplicate_true_preserves_original_baseline(self):
        with mock.patch.object(agent_module, 'boottime', side_effect=[10, 15]),                 mock.patch.object(agent_module.time, 'monotonic', side_effect=[10, 10, 15, 15]):
            self.agent.observe_prepare_enter()
            baseline = self.agent.sleep_tx['gap_baseline']
            self.agent.observe_prepare_enter()
        self.assertEqual(self.agent.sleep_tx['gap_baseline'], baseline)
        self.assertTrue(self.agent.sleep_tx['saw_prepare_true'])

    def test_false_without_true_settles_conservatively_and_never_suspends(self):
        _proxy, drain = self.queue_reconcile_bus(preparing=False)
        with mock.patch.object(agent_module, 'boottime', return_value=0),                 mock.patch.object(agent_module.time, 'monotonic', return_value=0):
            self.agent.observe_prepare_exit()
            drain()
        with mock.patch.object(agent_module, 'boottime', return_value=6),                 mock.patch.object(agent_module.time, 'monotonic', return_value=6):
            proxy = self.drive_reconcile('tick', preparing=False)
        self.assertEqual(self.agent.sleep_outcome, 'uncertain-preparation')
        self.assertEqual(proxy.call_count, 3)

    def test_repeated_false_after_resolution_is_idempotent(self):
        _proxy, drain = self.queue_reconcile_bus(preparing=False)
        with mock.patch.object(agent_module, 'boottime', return_value=0), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=0):
            self.agent.observe_prepare_exit()
            drain()
        with mock.patch.object(agent_module, 'boottime', return_value=6), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=6):
            self.drive_reconcile('tick', preparing=False)
            self.agent.observe_prepare_exit()
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'uncertain-preparation')

    def test_false_then_delayed_true_converges_through_one_transaction(self):
        _proxy, drain = self.queue_reconcile_bus(preparing=False)
        with mock.patch.object(agent_module, 'boottime', return_value=0), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=0):
            self.agent.observe_prepare_exit()
            drain()
        with mock.patch.object(agent_module, 'boottime', return_value=1), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=1):
            self.agent.observe_prepare_enter()
        with mock.patch.object(agent_module, 'boottime', return_value=6), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=6):
            proxy = self.drive_reconcile('delayed-enter', preparing=False)
            self.agent.observe_prepare_exit()
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'failed-preparation')
        self.assertEqual(proxy.call_count, 3)

    def test_true_beyond_deadline_keeps_safety_latch_and_cancels_real_timer(self):
        self.agent.deadline = 100
        self.agent.timer_phase = 'running'
        with mock.patch.object(agent_module, 'boottime', return_value=0),                 mock.patch.object(agent_module.time, 'monotonic', return_value=0):
            self.agent.observe_prepare_enter()
        with mock.patch.object(agent_module, 'boottime', return_value=31),                 mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.drive_reconcile('tick', preparing=True)
        self.assertEqual(self.agent.sleep_tx['phase'], 'uncertain-blocked')
        self.assertIsNone(self.agent.deadline)
        self.assertEqual(self.agent.timer_phase, 'canceled')

    def test_uncertain_without_timer_does_not_invent_canceled_timer(self):
        self.begin_request()
        with mock.patch.object(agent_module, 'boottime', return_value=31),                 mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.drive_reconcile('tick', preparing=None)
        self.assertEqual(self.agent.sleep_tx['phase'], 'uncertain-blocked')
        self.assertEqual(self.agent.timer_phase, 'idle')

    def test_uncertain_latch_blocks_failsafe_and_direct_preflight(self):
        self.agent.sleep_tx['phase'] = 'uncertain-blocked'
        self.agent.enabled = True
        self.agent.failsafe = True
        self.agent.on_battery = True
        self.agent.battery_discharge_state = 'discharging'
        self.agent.battery_percent = 5
        self.agent.sleep_now = mock.Mock()
        self.agent.check_failsafe = agent_module.Agent.check_failsafe.__get__(self.agent)
        self.agent.check_failsafe()
        self.agent.sleep_now.assert_not_called()
        outcomes = []
        self.agent.suspend_preflight_async(lambda *result: outcomes.append(result))
        self.assertEqual(len(outcomes), 1)
        self.assertFalse(outcomes[0][0])

    def test_uncertain_latch_blocks_periodic_and_upower_failsafe_paths(self):
        self.agent.sleep_tx['phase'] = 'uncertain-blocked'
        self.agent.sleep_tx['uncertain_since'] = 0
        self.agent.enabled = True
        self.agent.failsafe = True
        self.agent.on_battery = True
        self.agent.battery_discharge_state = 'discharging'
        self.agent.battery_percent = 5
        self.agent.sleep_now = mock.Mock()
        self.agent.check_failsafe = agent_module.Agent.check_failsafe.__get__(self.agent)
        self.agent.reconcile()
        self.agent.on_properties_changed(agent_module.UPOWER, {}, [])
        self.agent.sleep_now.assert_not_called()
        self.assertEqual(self.agent.sleep_tx['phase'], 'uncertain-blocked')

    def test_unreadable_property_retains_latch_until_clock_evidence(self):
        self.begin_request(reply='unknown')
        with mock.patch.object(agent_module, 'boottime', return_value=10), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=10):
            self.drive_reconcile('before-deadline', preparing=None)
        self.assertEqual(self.agent.sleep_tx['phase'], 'request-pending')
        with mock.patch.object(agent_module, 'boottime', return_value=31), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.drive_reconcile('after-deadline', preparing=None)
        self.assertEqual(self.agent.sleep_tx['phase'], 'uncertain-blocked')
        with mock.patch.object(agent_module, 'boottime', return_value=50), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=40):
            self.drive_reconcile('clock-evidence', preparing=None)
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')

    def test_clock_gap_waits_while_logind_still_reports_preparing(self):
        self.begin_request(reply='unknown')
        with mock.patch.object(agent_module, 'boottime', return_value=40), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.drive_reconcile('clock-gap-while-preparing', preparing=True)
        self.assertEqual(self.agent.sleep_tx['phase'], 'uncertain-blocked')
        self.assertEqual(self.agent.sleep_outcome, 'preparation-state-uncertain')
        with mock.patch.object(agent_module, 'boottime', return_value=40), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.drive_reconcile('preparation-cleared', preparing=False)
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')

    def test_later_false_resolves_unreadable_latch_after_settle(self):
        self.begin_request(reply='unknown')
        self.agent.sleep_tx['phase'] = 'uncertain-blocked'
        with mock.patch.object(agent_module, 'boottime', return_value=40),                 mock.patch.object(agent_module.time, 'monotonic', return_value=40):
            self.drive_reconcile('tick', preparing=False)
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'request-outcome-unknown')

    def test_logind_owner_replacement_terminates_transaction(self):
        self.begin_request()
        self.agent.on_owner_change(agent_module.LOGIN, ':1.old', ':1.new')
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'logind-owner-lost')
        self.assertEqual(self.agent.suspend_request_outcome, 'unknown')
        self.assertEqual(self.agent.logind_owner_generation, 1)

    def test_generation_mismatch_is_resolved_before_reading_old_logind_state(self):
        self.begin_request(reply='unknown')
        self.agent.logind_owner_generation = 1
        self.agent.proxy = mock.Mock()
        self.agent.reconcile_sleep_state('generation-check')
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'logind-owner-lost')
        self.agent.proxy.assert_not_called()

    def test_logind_replacement_requires_fresh_readable_preflight(self):
        self.begin_request(reply='unknown')
        self.agent.on_owner_change(agent_module.LOGIN, ':1.old', ':1.new')
        self.assertFalse(self.drive_preflight(preparing=None)[0])
        self.assertTrue(self.drive_preflight(preparing=False)[0])

    def test_preflight_rejects_cookie_owner_mismatch_before_release(self):
        self.held_cookie()
        allowed, message, _owner = self.drive_preflight(session_owner=':1.other')
        self.assertFalse(allowed)
        self.assertIn('previous', message)

    def test_state_change_between_release_and_final_guard_prevents_suspend(self):
        self.agent.deadline = 10
        self.agent.timer_phase = 'running'
        self.allow_async_sleep_steps()
        def release(callback, **_kwargs):
            self.agent.sleep_tx['phase'] = 'preparing'
            self.agent.session_cookie_state = 'absent'
            self.agent.session_cookie = None
            callback(True)
        self.agent.release_async.side_effect = release
        login = types.SimpleNamespace(Suspend=mock.Mock())
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertTrue(self.agent.sleep_now('Countdown elapsed'))
        login.Suspend.assert_not_called()
        self.assertEqual(self.agent.timer_phase, 'consumed')

    def test_each_definite_rejection_resolves_immediately(self):
        for name in agent_module.DEFINITE_SUSPEND_REJECTIONS:
            with self.subTest(name=name):
                self.agent.sleep_tx = self.agent.idle_sleep_transaction()
                self.agent.deadline = 10
                self.agent.timer_phase = 'running'
                self.allow_async_sleep_steps()
                login = types.SimpleNamespace(
                    Suspend=mock.Mock(side_effect=FakeDBusException('denied', name=name)))
                self.agent.proxy = mock.Mock(return_value=login)
                self.agent.sleep_now('Countdown elapsed')
                self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
                self.assertEqual(self.agent.suspend_request_outcome, 'rejected')
                self.assertEqual(login.Suspend.call_count, 1)

    def test_ambiguous_errors_remain_pending_and_are_never_retried(self):
        self.configure_low_battery()
        names = ('org.freedesktop.DBus.Error.NoReply',
                 'org.freedesktop.DBus.Error.Timeout',
                 'org.freedesktop.DBus.Error.Disconnected',
                 'com.example.Unknown')
        for name in names:
            with self.subTest(name=name):
                self.agent.sleep_tx = self.agent.idle_sleep_transaction()
                self.allow_async_sleep_steps()
                login = types.SimpleNamespace(
                    Suspend=mock.Mock(side_effect=FakeDBusException('lost', name=name)))
                self.agent.proxy = mock.Mock(return_value=login)
                self.agent.sleep_now('Battery fell below the failsafe threshold')
                self.assertEqual(self.agent.sleep_tx['request_reply'], 'unknown')
                self.assertEqual(login.Suspend.call_count, 1)
                self.agent.notify.assert_called_once()
                with mock.patch.object(agent_module, 'boottime', return_value=10), \
                        mock.patch.object(agent_module.time, 'monotonic', return_value=10):
                    self.drive_reconcile('tick', preparing=False)
                self.assertEqual(login.Suspend.call_count, 1)
                self.assertEqual(self.agent.suspend_request_outcome, 'unknown')
                self.agent.notify.reset_mock()

    def test_low_battery_episode_never_retries_ambiguous_suspend(self):
        now = [0]
        monotonic_clock = mock.patch.object(agent_module.time, 'monotonic',
                                            side_effect=lambda: now[0])
        boot_clock = mock.patch.object(agent_module, 'boottime',
                                       side_effect=lambda: now[0])
        monotonic_clock.start()
        self.addCleanup(monotonic_clock.stop)
        boot_clock.start()
        self.addCleanup(boot_clock.stop)
        self.agent.enabled = True
        self.agent.failsafe = True
        self.agent.on_battery = True
        self.agent.battery_discharge_state = 'discharging'
        self.agent.battery_percent = 5
        self.allow_async_sleep_steps()
        login = types.SimpleNamespace(Suspend=mock.Mock(side_effect=FakeDBusException(
            'lost', name='org.freedesktop.DBus.Error.NoReply')))
        self.agent.proxy = mock.Mock(return_value=login)
        self.agent.check_failsafe = agent_module.Agent.check_failsafe.__get__(self.agent)

        self.agent.check_failsafe()
        now[0] = 6
        self.drive_reconcile('settled', preparing=False)
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.agent.proxy = mock.Mock(return_value=login)
        self.agent.check_failsafe()
        self.assertEqual(login.Suspend.call_count, 1)

        self.agent.enabled = False
        self.agent.check_failsafe()
        self.agent.enabled = True
        self.agent.check_failsafe()
        self.assertEqual(login.Suspend.call_count, 1)

        self.agent.battery_percent = 30
        self.agent.check_failsafe()
        self.agent.battery_percent = 5
        now[0] = 7
        self.agent.check_failsafe()
        self.assertEqual(login.Suspend.call_count, 2)

    def test_unknown_power_data_does_not_rearm_failsafe_episode(self):
        self.agent.enabled = True
        self.agent.failsafe = True
        self.agent.failsafe_triggered = True
        self.agent.on_battery = None
        self.agent.battery_discharge_state = 'unknown'
        self.agent.battery_percent = None
        self.agent.sleep_now = mock.Mock()
        self.agent.check_failsafe = agent_module.Agent.check_failsafe.__get__(self.agent)

        self.agent.check_failsafe()
        self.assertTrue(self.agent.failsafe_triggered)
        self.agent.on_battery = True
        self.agent.battery_discharge_state = 'discharging'
        self.agent.battery_percent = 5
        self.agent.check_failsafe()
        self.agent.sleep_now.assert_not_called()

        self.agent.on_battery = False
        self.agent.check_failsafe()
        self.assertFalse(self.agent.failsafe_triggered)

    def test_unknown_reply_with_false_and_no_gap_stays_unknown_not_rejected(self):
        self.begin_request(reply='unknown')
        self.agent.sleep_tx['saw_prepare_false'] = True
        with mock.patch.object(agent_module, 'boottime', return_value=6), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=6):
            self.drive_reconcile('settled', preparing=False)
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'uncertain-preparation')
        self.assertEqual(self.agent.suspend_request_outcome, 'unknown')
        self.assertIn('request outcome unknown', self.agent.timer_outcome)

    def test_unknown_reply_with_unreadable_property_becomes_safety_blocked(self):
        self.begin_request(reply='unknown')
        with mock.patch.object(agent_module, 'boottime', return_value=31), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.drive_reconcile('deadline', preparing=None)
        self.assertEqual(self.agent.sleep_tx['phase'], 'uncertain-blocked')
        self.assertEqual(self.agent.suspend_request_outcome, 'unknown')

    def test_timer_and_failsafe_defer_while_preparation_is_unresolved(self):
        self.agent.sleep_tx['phase'] = 'preparing'
        self.agent.sleep_tx['uncertain_since'] = 0
        self.agent.sleep_now = mock.Mock()
        proxy, drain = self.queue_reconcile_bus(preparing=True)
        with mock.patch.object(agent_module, 'boottime', return_value=0), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=0):
            self.agent.StartTimer(60, False, False, reply=mock.Mock(), error=mock.Mock())
            self.agent.tick()
            drain()
        self.agent.enabled = True
        self.agent.failsafe = True
        self.agent.on_battery = True
        self.agent.battery_discharge_state = 'discharging'
        self.agent.battery_percent = 5
        self.agent.check_failsafe = agent_module.Agent.check_failsafe.__get__(self.agent)
        self.agent.check_failsafe()
        self.agent.sleep_now.assert_not_called()
        self.assertEqual(self.agent.timer_phase, 'running')
        self.assertEqual(proxy.call_count, 3)

    def test_reentrant_wake_resolution_is_not_resurrected_by_method_reply(self):
        self.configure_low_battery()
        self.allow_async_sleep_steps()
        def suspend(_interactive, **kwargs):
            self.agent.resolve_sleep_transaction('proven-resume', now=10, monotonic_now=0)
            kwargs['reply_handler']()
        login = types.SimpleNamespace(Suspend=mock.Mock(side_effect=suspend))
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertTrue(self.agent.sleep_now('Battery fell below the failsafe threshold'))
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')
        self.assertEqual(self.agent.suspend_request_outcome, 'proven-suspend')

    def test_reentrant_prepare_enter_is_not_overwritten_by_method_reply(self):
        self.configure_low_battery()
        self.allow_async_sleep_steps()
        login = types.SimpleNamespace(
            Suspend=mock.Mock(side_effect=lambda _interactive, **kwargs: (
                self.agent.observe_prepare_enter(), kwargs['reply_handler']())))
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertTrue(self.agent.sleep_now('Battery fell below the failsafe threshold'))
        self.assertEqual(self.agent.sleep_tx['phase'], 'preparing')
        self.assertTrue(self.agent.sleep_tx['saw_prepare_true'])
        self.assertEqual(self.agent.sleep_tx['request_reply'], 'accepted')

    def configure_low_battery(self):
        self.agent.enabled = True
        self.agent.failsafe = True
        self.agent.on_battery = True
        self.agent.battery_discharge_state = 'discharging'
        self.agent.battery_percent = 10

    def test_conditional_timer_fresh_lid_rejection_preserves_prevention(self):
        for lid in (False, None):
            with self.subTest(lid=lid):
                a = self.agent
                a.deadline, a.timer_phase = 0, 'running'
                a.require_lid = a.require_prevention = True
                a.lid_closed = a.enabled = a.desired = True
                self.allow_async_sleep_steps()
                a.refresh_power_async.side_effect = lambda callback=None, **_kwargs: (
                    setattr(a, 'lid_closed', lid), callback(True))
                login = types.SimpleNamespace(Suspend=mock.Mock())
                a.proxy = mock.Mock(return_value=login)
                self.assertTrue(a.sleep_now('Countdown elapsed'))
                a.release_async.assert_not_called()
                login.Suspend.assert_not_called()
                self.assertTrue(a.enabled)
                self.assertTrue(a.desired)
                self.assertIsNone(a.deadline)
                self.assertEqual(a.timer_phase, 'consumed')
                self.assertIn('confirmed closed lid', a.timer_outcome)

    def test_conditional_timer_lid_change_after_release_blocks_dispatch(self):
        for moment in ('release', 'final-preflight'):
            for lid in (False, None):
                with self.subTest(moment=moment, lid=lid):
                    a = self.agent
                    a.deadline, a.timer_phase = 0, 'running'
                    a.require_lid = a.require_prevention = True
                    a.lid_closed = a.enabled = a.desired = True
                    a.sleep_tx = a.idle_sleep_transaction()
                    self.allow_async_sleep_steps()
                    def release(callback, **_kwargs):
                        a.enabled = False
                        if moment == 'release':
                            a.lid_closed = lid
                        callback(True)
                    def preflight(callback, **_kwargs):
                        if moment == 'final-preflight' and a.suspend_preflight_async.call_count == 2:
                            a.lid_closed = lid
                        callback(True, '', ':1.login')
                    a.release_async.side_effect = release
                    a.suspend_preflight_async.side_effect = preflight
                    login = types.SimpleNamespace(Suspend=mock.Mock())
                    a.proxy = mock.Mock(return_value=login)
                    self.assertTrue(a.sleep_now('Countdown elapsed'))
                    a.release_async.assert_called_once()
                    login.Suspend.assert_not_called()
                    self.assertEqual(a.refresh_power_async.call_count, 2)
                    self.assertEqual(a.timer_phase, 'consumed')
                    self.assertFalse(a.desired)
                    self.assertFalse(a.enabled)
                    self.assertIn('confirmed closed lid', a.timer_outcome)

    def test_conditional_timer_prevention_loss_before_release_blocks_dispatch(self):
        a = self.agent
        a.deadline, a.timer_phase = 0, 'running'
        a.require_prevention = True
        a.enabled = a.desired = True
        self.allow_async_sleep_steps()
        a.suspend_preflight_async.side_effect = lambda callback, **_kwargs: (
            setattr(a, 'enabled', False), callback(True, '', ':1.login'))
        login = types.SimpleNamespace(Suspend=mock.Mock())
        a.proxy = mock.Mock(return_value=login)
        self.assertTrue(a.sleep_now('Countdown elapsed'))
        a.release_async.assert_not_called()
        login.Suspend.assert_not_called()
        self.assertEqual(a.timer_phase, 'consumed')
        self.assertIn('effective prevention before release', a.timer_outcome)

    def test_conditional_timer_stable_conditions_dispatch_once_after_release(self):
        a = self.agent
        a.deadline, a.timer_phase = 0, 'running'
        a.require_lid = a.require_prevention = True
        a.lid_closed = a.enabled = a.desired = True
        self.allow_async_sleep_steps()
        def release(callback, **_kwargs):
            a.enabled = False
            callback(True)
        a.release_async.side_effect = release
        login = types.SimpleNamespace(Suspend=mock.Mock())
        a.proxy = mock.Mock(return_value=login)
        self.assertTrue(a.sleep_now('Countdown elapsed'))
        a.tick()
        login.Suspend.assert_called_once()
        self.assertEqual(login.Suspend.call_args.args, (False,))
        self.assertEqual(login.Suspend.call_args.kwargs['timeout'], 3)
        self.assertTrue(callable(login.Suspend.call_args.kwargs['reply_handler']))
        self.assertTrue(callable(login.Suspend.call_args.kwargs['error_handler']))
        self.assertIsNone(a.deadline)
        self.assertEqual(a.timer_phase, 'consumed')
        self.assertFalse(a.enabled)
        self.assertEqual(a.refresh_power_async.call_count, 2)

    def test_preflight_reads_cannot_authorize_changed_lifecycle(self):
        for read in ('property', 'owner'):
            for change in ('shutdown', 'transaction', 'generation', 'cookie'):
                if read == 'property' and change == 'cookie':
                    continue
                with self.subTest(read=read, change=change):
                    self.agent.shutting_down = False
                    self.agent.sleep_tx = self.agent.idle_sleep_transaction()
                    self.agent.session_cookie = 12
                    self.agent.session_cookie_state = 'held'
                    self.agent.session_cookie_owner = ':1.gnome'
                    self.allow_async_sleep_steps()
                    self.agent.suspend_preflight_async = mock.Mock(
                        side_effect=agent_module.Agent.suspend_preflight_async.__get__(self.agent))
                    def mutate():
                        if change == 'shutdown':
                            self.agent.shutting_down = True
                        elif change == 'transaction':
                            self.agent.observe_prepare_enter()
                        elif change == 'generation':
                            self.agent.logind_owner_generation += 1
                        else:
                            self.agent.session_cookie_state = 'release-pending'
                    login = types.SimpleNamespace(Suspend=mock.Mock())
                    pending = []
                    def proxy(_bus, _owner, _path, interface):
                        if interface == 'org.freedesktop.DBus':
                            def get_owner(name, **kwargs):
                                def deliver():
                                    if name == agent_module.SESSION and read == 'owner':
                                        mutate()
                                    kwargs['reply_handler'](':1.login' if name == agent_module.LOGIN
                                                            else ':1.gnome')
                                pending.append(deliver)
                            return types.SimpleNamespace(GetNameOwner=get_owner)
                        if interface == 'org.freedesktop.DBus.Properties':
                            def get(_name, _key, **kwargs):
                                def deliver():
                                    if read == 'property':
                                        mutate()
                                    kwargs['reply_handler'](False)
                                pending.append(deliver)
                            return types.SimpleNamespace(Get=get)
                        if interface == 'org.freedesktop.login1.Manager':
                            return login
                        raise AssertionError('Unexpected preflight interface: ' + interface)
                    self.agent.proxy = mock.Mock(side_effect=proxy)
                    self.assertTrue(self.agent.sleep_now('Countdown elapsed'))
                    self.assertEqual(len(pending), 1)
                    self.agent.release_async.assert_not_called()
                    while pending:
                        pending.pop(0)()
                    self.assertEqual(self.agent.proxy.call_count, 2 if read == 'property' else 3)
                    self.agent.release_async.assert_not_called()
                    login.Suspend.assert_not_called()

    def test_failsafe_rechecks_power_after_inhibitor_release(self):
        changes = (('on_battery', False), ('on_battery', None),
                   ('battery_discharge_state', 'unknown'),
                   ('battery_discharge_state', 'not-discharging'),
                   ('battery_percent', None), ('battery_percent', 20),
                   ('failsafe', False))
        for attr, value in changes:
            with self.subTest(attr=attr, value=value):
                self.configure_low_battery()
                self.agent.failsafe_triggered = False
                self.allow_async_sleep_steps()
                def release(callback, **_kwargs):
                    self.agent.enabled = False
                    setattr(self.agent, attr, value)
                    callback(True)
                self.agent.release_async.side_effect = release
                login = types.SimpleNamespace(Suspend=mock.Mock())
                self.agent.proxy = mock.Mock(return_value=login)
                self.assertTrue(self.agent.sleep_now('Battery fell below the failsafe threshold'))
                self.assertEqual(self.agent.refresh_power_async.call_count, 2)
                login.Suspend.assert_not_called()
                self.assertTrue(self.agent.failsafe_triggered)
                self.assertFalse(self.agent.desired)
                self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
                self.assertIn('power conditions no longer confirmed', self.agent.last_error)

    def test_failsafe_stable_power_dispatches_once_after_effective_release(self):
        self.configure_low_battery()
        self.allow_async_sleep_steps()
        def release(callback, **_kwargs):
            self.agent.enabled = False
            callback(True)
        self.agent.release_async.side_effect = release
        login = types.SimpleNamespace(Suspend=mock.Mock())
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertTrue(self.agent.sleep_now('Battery fell below the failsafe threshold'))
        self.assertFalse(self.agent.enabled)
        self.assertEqual(self.agent.refresh_power_async.call_count, 2)
        agent_module.Agent.check_failsafe(self.agent)
        login.Suspend.assert_called_once()
        self.assertEqual(login.Suspend.call_args.args, (False,))
        self.assertEqual(login.Suspend.call_args.kwargs['timeout'], 3)
        self.assertTrue(callable(login.Suspend.call_args.kwargs['reply_handler']))
        self.assertTrue(callable(login.Suspend.call_args.kwargs['error_handler']))
        self.assertTrue(self.agent.failsafe_triggered)

    def test_failsafe_power_change_during_final_logind_read_blocks_dispatch(self):
        self.configure_low_battery()
        self.allow_async_sleep_steps()
        calls = 0
        def preflight(callback, **_kwargs):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.agent.on_battery = False
            callback(True, '', ':1.login')
        self.agent.suspend_preflight_async.side_effect = preflight
        login = types.SimpleNamespace(Suspend=mock.Mock())
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertTrue(self.agent.sleep_now('Battery fell below the failsafe threshold'))
        self.assertEqual(calls, 2)
        login.Suspend.assert_not_called()
        self.assertTrue(self.agent.failsafe_triggered)
        self.assertIn('changed during final preflight', self.agent.last_error)

    def test_failsafe_last_power_refresh_events_precede_final_guard(self):
        for event in ('shutdown', 'preparing', 'unknown-preparing'):
            with self.subTest(event=event):
                self.configure_low_battery()
                self.agent.shutting_down = False
                self.allow_async_sleep_steps()
                def refresh(callback=None, **kwargs):
                    self.assertFalse(kwargs['reconcile_lid'])
                    if self.agent.refresh_power_async.call_count == 2 and event == 'shutdown':
                        self.agent.shutting_down = True
                    callback(True)
                pending = []
                def preflight(callback, **_kwargs):
                    if self.agent.suspend_preflight_async.call_count == 2:
                        agent_module.Agent.suspend_preflight_async(
                            self.agent, callback, **_kwargs)
                    else:
                        callback(True, '', ':1.login')
                def proxy(_bus, _owner, _path, interface):
                    if interface == 'org.freedesktop.DBus':
                        return types.SimpleNamespace(GetNameOwner=lambda _name, **kwargs:
                            pending.append(lambda: kwargs['reply_handler'](':1.login')))
                    if interface == 'org.freedesktop.DBus.Properties':
                        return types.SimpleNamespace(Get=lambda _name, _key, **kwargs:
                            pending.append(lambda: kwargs['reply_handler'](
                                True if event == 'preparing' else None)))
                    raise AssertionError('Final preflight passed an unreadable preparation state')
                self.agent.refresh_power_async.side_effect = refresh
                self.agent.suspend_preflight_async.side_effect = preflight
                self.agent.release_async.side_effect = lambda callback, **_kwargs: (
                    setattr(self.agent, 'enabled', False), callback(True))
                self.agent.proxy = mock.Mock(side_effect=proxy)
                self.assertTrue(self.agent.sleep_now('Battery fell below the failsafe threshold'))
                if event != 'shutdown':
                    self.assertEqual(len(pending), 1)
                    while pending:
                        pending.pop(0)()
                self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
                self.assertEqual(self.agent.suspend_preflight_async.call_count,
                                 1 if event == 'shutdown' else 2)
                self.assertEqual(self.agent.proxy.call_count,
                                 0 if event == 'shutdown' else 2)
                if event == 'preparing':
                    self.assertIn('logind is preparing for sleep', self.agent.last_error)
                elif event == 'unknown-preparing':
                    self.assertIn('D-Bus read failed', self.agent.last_error)

    def test_terminal_resolution_ignores_reentrant_prepare_signal(self):
        self.begin_request()
        self.agent.sleep_tx['saw_prepare_true'] = True
        proxy, drain = self.queue_reconcile_bus(preparing=False)
        def refresh(callback=None, **_kwargs):
            self.agent.on_prepare_sleep(False)
            callback(True)
        self.agent.refresh_power_async.side_effect = refresh
        with mock.patch.object(agent_module, 'boottime', return_value=20), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=10):
            self.agent.reconcile_sleep_state('test')
            drain()
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')
        self.assertEqual(proxy.call_count, 3)
        self.agent.notify.assert_called_once()

    def test_cancel_without_timer_is_idempotent(self):
        self.agent.timer_phase = 'idle'
        self.assertFalse(self.agent.cancel_timer())
        self.assertEqual(self.agent.timer_phase, 'idle')
        self.agent.publish.assert_not_called()

    def test_proven_suspend_consumes_overdue_active_timer_without_resuspend(self):
        self.agent.deadline = 5
        self.agent.timer_phase = 'running'
        self.agent.clock_gap = agent_module.ClockGapSample(0, 0, 0, 0)
        proxy, drain = self.queue_reconcile_bus(preparing=False)
        with mock.patch.object(agent_module, 'boottime', return_value=20),                 mock.patch.object(agent_module.time, 'monotonic', return_value=10):
            self.agent.tick()
            drain()
        self.assertIsNone(self.agent.deadline)
        self.assertEqual(self.agent.timer_phase, 'canceled')
        self.assertIn('expired during suspend', self.agent.timer_outcome)
        self.assertEqual(proxy.call_count, 3)

    def test_failsafe_rechecks_after_fresh_power_read(self):
        self.agent.enabled = True
        self.agent.failsafe = True
        self.agent.on_battery = True
        self.agent.battery_discharge_state = 'discharging'
        self.agent.battery_percent = 5
        self.agent.sleep_now = mock.Mock()
        self.agent.suspend_preflight_async = mock.Mock(side_effect=lambda callback, **_kwargs:
            callback(True, '', ':1.login'))
        def refresh(callback=None, **_kwargs):
            self.agent.on_battery = False
            callback(True)
        self.agent.refresh_power_async.side_effect = refresh
        self.agent.check_failsafe = agent_module.Agent.check_failsafe.__get__(self.agent)
        self.agent.check_failsafe()
        self.agent.suspend_preflight_async.assert_called_once()
        self.agent.refresh_power_async.assert_called_once()
        self.agent.sleep_now.assert_not_called()

    def power_devices(self, devices):
        self.agent.property = mock.Mock(side_effect=[True, True, False, 5])
        manager = types.SimpleNamespace(EnumerateDevices=lambda **_kwargs: list(range(len(devices))),
                                       GetDisplayDevice=lambda **_kwargs: '/display')
        def proxy(_bus, _name, path, _interface):
            if path == '/org/freedesktop/DBus':
                return types.SimpleNamespace(GetNameOwner=lambda *_args, **_kwargs: ':1.upower')
            if path == agent_module.UPOWER_PATH:
                return manager
            result = devices[path]
            def get_all(_interface, **_kwargs):
                if isinstance(result, Exception):
                    raise result
                return dict(Type=2, PowerSupply=True, IsPresent=True,
                            Percentage=5, State=result)
            return types.SimpleNamespace(GetAll=get_all)
        self.agent.proxy = proxy
        agent_module.Agent.refresh_power(self.agent)

    def test_upower_aggregate_requires_positive_discharge_evidence(self):
        cases = [([], 'unknown'), ([0], 'unknown'), ([6], 'unknown'),
                 ([3], 'unknown'), ([2], 'discharging'), ([1, 4, 5], 'not-discharging'),
                 ([1, 0], 'unknown'), ([2, 0], 'discharging'),
                 ([1, FakeDBusException('missing')], 'unknown'),
                 ([2, FakeDBusException('missing')], 'unknown')]
        for devices, expected in cases:
            with self.subTest(devices=devices):
                self.power_devices(devices)
                self.assertEqual(self.agent.battery_discharge_state, expected)
                self.assertEqual(self.agent.battery_discharging, expected == 'discharging')
                if any(isinstance(device, Exception) for device in devices):
                    self.assertIsNone(self.agent.battery_percent)

    def test_partially_unreadable_battery_set_cannot_trigger_or_rearm(self):
        self.agent.failsafe = True
        self.agent.enabled = True
        self.agent.sleep_now = mock.Mock()
        self.agent.check_failsafe = agent_module.Agent.check_failsafe.__get__(self.agent)
        self.power_devices([2, FakeDBusException('unreadable second battery')])
        self.agent.check_failsafe()
        self.agent.sleep_now.assert_not_called()
        self.agent.failsafe_triggered = True
        self.power_devices([1, FakeDBusException('unreadable second battery')])
        self.agent.check_failsafe()
        self.assertTrue(self.agent.failsafe_triggered)

    def test_invalid_percentages_and_upower_owner_loss_preserve_episode(self):
        self.agent.failsafe = True
        self.agent.failsafe_triggered = True
        self.agent.on_battery = True
        self.agent.battery_discharge_state = 'discharging'
        for value in (None, 'invalid', float('nan'), float('inf'), -1, 101):
            with self.subTest(value=value):
                self.agent.battery_percent = agent_module.valid_percent(value)
                self.assertIsNone(self.agent.battery_percent)
                agent_module.Agent.check_failsafe(self.agent)
                self.assertTrue(self.agent.failsafe_triggered)
        self.agent.property = mock.Mock(side_effect=FakeDBusException('UPower owner gone'))
        self.agent.refresh_power = agent_module.Agent.refresh_power.__get__(self.agent)
        self.agent.on_owner_change(agent_module.UPOWER, ':1.old', '')
        self.assertEqual(self.agent.battery_discharge_state, 'unknown')
        agent_module.Agent.check_failsafe(self.agent)
        self.assertTrue(self.agent.failsafe_triggered)

    def test_state_directory_requires_absolute_path_and_empty_uses_default(self):
        with mock.patch.dict(agent_module.os.environ, {'XDG_STATE_HOME': 'relative'}):
            with self.assertRaisesRegex(ValueError, 'absolute path'):
                agent_module.data_home()
        with mock.patch.dict(agent_module.os.environ, {'XDG_STATE_HOME': ''}):
            self.assertEqual(agent_module.data_home(), Path.home() / '.local/state/sleep-disabler')
        for unsafe in ('/tmp/bad\nstate', '/tmp/bad\tstate', '/tmp/bad\x7fstate',
                       '/tmp/bad\x85state', '/tmp/bad\u200bstate'):
            with self.subTest(unsafe=repr(unsafe)), \
                    mock.patch.dict(agent_module.os.environ, {'XDG_STATE_HOME': unsafe}):
                with self.assertRaises(ValueError):
                    agent_module.data_home()

    def test_unknown_low_battery_sequences_cannot_dispatch_twice(self):
        for ambiguous in (0, 6, [], FakeDBusException('UPower gone')):
            with self.subTest(ambiguous=ambiguous):
                self.agent.enabled = True
                self.agent.failsafe = True
                self.agent.failsafe_triggered = False
                self.agent.on_battery = True
                self.agent.battery_discharge_state = 'discharging'
                self.agent.battery_percent = 5
                self.agent.sleep_now = mock.Mock()
                self.agent.check_failsafe = agent_module.Agent.check_failsafe.__get__(self.agent)
                self.agent.suspend_preflight_async = mock.Mock(
                    side_effect=lambda callback, **_kwargs: callback(True, '', ':1.login'))
                self.agent.check_failsafe()
                self.agent.sleep_now.assert_called_once()
                if isinstance(ambiguous, Exception):
                    self.agent.property = mock.Mock(side_effect=ambiguous)
                    agent_module.Agent.refresh_power(self.agent)
                else:
                    self.power_devices(ambiguous if isinstance(ambiguous, list) else [ambiguous])
                self.agent.check_failsafe()
                self.assertTrue(self.agent.failsafe_triggered)
                self.agent.on_battery = True
                self.agent.battery_discharge_state = 'discharging'
                self.agent.battery_percent = 5
                self.agent.check_failsafe()
                self.agent.sleep_now.assert_called_once()

    def test_positive_recovery_rearms_failsafe_episode(self):
        for on_battery, percentage, discharge in (
                (False, None, 'unknown'), (True, 20, 'unknown'),
                (True, 5, 'not-discharging')):
            self.agent.failsafe = True
            self.agent.failsafe_triggered = True
            self.agent.on_battery = on_battery
            self.agent.battery_percent = percentage
            self.agent.battery_discharge_state = discharge
            agent_module.Agent.check_failsafe(self.agent)
            self.assertFalse(self.agent.failsafe_triggered)

    def test_missing_percentage_preserves_episode_and_fresh_unknown_blocks_dispatch(self):
        self.agent.enabled = True
        self.agent.failsafe = True
        self.agent.on_battery = True
        self.agent.battery_discharge_state = 'discharging'
        self.agent.battery_percent = 5
        self.agent.sleep_now = mock.Mock()
        self.agent.check_failsafe = agent_module.Agent.check_failsafe.__get__(self.agent)
        self.agent.suspend_preflight_async = mock.Mock(
            side_effect=lambda callback, **_kwargs: callback(True, '', ':1.login'))
        self.agent.check_failsafe()
        self.agent.battery_percent = None
        self.agent.check_failsafe()
        self.assertTrue(self.agent.failsafe_triggered)
        self.agent.battery_percent = 5
        self.agent.check_failsafe()
        self.agent.sleep_now.assert_called_once()
        self.agent.failsafe_triggered = False
        self.agent.sleep_now.reset_mock()
        self.agent.refresh_power_async.side_effect = lambda callback=None, **_kwargs: (
            setattr(self.agent, 'battery_discharge_state', 'unknown'), callback(True))
        self.agent.check_failsafe()
        self.agent.sleep_now.assert_not_called()

    def test_explicit_failsafe_reconfiguration_rearms_consumed_episode(self):
        self.agent.failsafe_triggered = True
        self.agent.SetFailsafe(True, 27)
        self.assertFalse(self.agent.failsafe_triggered)
        self.assertEqual(self.agent.threshold, 27)
        self.assertTrue(self.agent.failsafe)

    def test_panel_registration_is_versioned_and_sender_owned(self):
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(
            side_effect=lambda _name, **kwargs: kwargs['reply_handler'](':1.42')))
        self.agent.proxy = mock.Mock(return_value=names)
        reply, error = mock.Mock(), mock.Mock()
        def register(version, sender=':1.42'):
            self.agent.RegisterPanelRuntime(version, sender=sender, reply=reply, error=error)
        self.assertFalse(self.agent.state()['panelRuntimeRegistered'])
        for version in ('', '0', '1bad', '1.2.3.4', ' 5', '5\n'):
            with self.assertRaises(FakeDBusException):
                register(version)
        register('5')
        reply.assert_called_once_with()
        error.assert_not_called()
        self.assertEqual(self.agent.state()['panelRuntimeVersion'], '5')
        with mock.patch.object(self.agent, 'publish') as publish:
            register('5')
            publish.assert_not_called()
        self.agent.UnregisterPanelRuntime('5', sender=':1.43')
        self.agent.UnregisterPanelRuntime('4', sender=':1.42')
        self.assertTrue(self.agent.state()['panelRuntimeRegistered'])
        self.agent.on_panel_owner_change(':1.43', ':1.43', '')
        self.assertTrue(self.agent.state()['panelRuntimeRegistered'])
        self.agent.on_panel_owner_change(':1.42', ':1.42', '')
        self.assertFalse(self.agent.state()['panelRuntimeRegistered'])
        register('5')
        self.agent.UnregisterPanelRuntime('5', sender=':1.42')
        self.assertFalse(self.agent.state()['panelRuntimeRegistered'])
        self.assertEqual(self.agent.state()['agentRuntimeVersion'], '0.6.0')
        self.assertEqual(self.agent.state()['apiVersion'], 6)

    def test_panel_registration_rejects_non_shell_and_stale_owner_replies(self):
        names = types.SimpleNamespace(GetNameOwner=mock.Mock())
        self.agent.proxy = mock.Mock(return_value=names)
        reply, error = mock.Mock(), mock.Mock()
        self.agent.RegisterPanelRuntime('6', sender=':1.42', reply=reply, error=error)
        names.GetNameOwner.call_args.kwargs['reply_handler'](':1.other')
        reply.assert_not_called()
        self.assertEqual(error.call_args.args[0].get_dbus_name(), agent_module.IFACE + '.InvalidArgument')
        self.assertFalse(self.agent.state()['panelRuntimeRegistered'])

        error.reset_mock()
        self.agent.RegisterPanelRuntime('6', sender=':1.42', reply=reply, error=error)
        self.agent.on_owner_change(agent_module.SHELL, ':1.42', ':1.other')
        names.GetNameOwner.call_args.kwargs['reply_handler'](':1.42')
        reply.assert_not_called()
        error.assert_called_once()
        self.assertEqual(error.call_args.args[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        self.assertFalse(self.agent.state()['panelRuntimeRegistered'])

        self.agent.RegisterPanelRuntime('6', sender=':1.42', reply=reply, error=error)
        names.GetNameOwner.call_args.kwargs['reply_handler'](':1.42')
        self.assertTrue(self.agent.state()['panelRuntimeRegistered'])
        self.agent.on_owner_change(agent_module.SHELL, ':1.other', ':1.next')
        self.assertFalse(self.agent.state()['panelRuntimeRegistered'])

    def test_panel_registration_rejects_late_owner_success(self):
        names = types.SimpleNamespace(GetNameOwner=mock.Mock())
        self.agent.proxy = mock.Mock(return_value=names)
        reply, error = mock.Mock(), mock.Mock()
        with mock.patch.object(agent_module.time, 'monotonic', return_value=100):
            self.agent.RegisterPanelRuntime('6', sender=':1.42', reply=reply, error=error)
        owner_reply = names.GetNameOwner.call_args.kwargs['reply_handler']
        with mock.patch.object(agent_module.time, 'monotonic', return_value=104):
            owner_reply(':1.42')
        reply.assert_not_called()
        error.assert_called_once()
        self.assertEqual(error.call_args.args[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        self.assertFalse(self.agent.state()['panelRuntimeRegistered'])
        owner_reply(':1.42')
        error.assert_called_once()

    def test_panel_registration_retry_after_slow_publication_is_idempotent(self):
        a = self.agent
        names = types.SimpleNamespace(GetNameOwner=mock.Mock())
        a.proxy = mock.Mock(return_value=names)
        now = [100]
        a.publish.side_effect = lambda: now.__setitem__(0, 104)
        reply, error = mock.Mock(), mock.Mock()
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=lambda: now[0]):
            a.RegisterPanelRuntime('6', sender=':1.42', reply=reply, error=error)
            names.GetNameOwner.call_args.kwargs['reply_handler'](':1.42')
            reply.assert_not_called()
            error.assert_called_once()
            self.assertTrue(a.state()['panelRuntimeRegistered'])
            a.publish.side_effect = None
            now[0] = 105
            a.RegisterPanelRuntime('6', sender=':1.42', reply=reply, error=error)
            names.GetNameOwner.call_args.kwargs['reply_handler'](':1.42')
        reply.assert_called_once()
        error.assert_called_once()
        a.publish.assert_called_once()

    def test_panel_unregister_cancels_pending_owner_check(self):
        names = types.SimpleNamespace(GetNameOwner=mock.Mock())
        self.agent.proxy = mock.Mock(return_value=names)
        reply, error = mock.Mock(), mock.Mock()
        self.agent.RegisterPanelRuntime('6', sender=':1.42', reply=reply, error=error)
        owner_reply = names.GetNameOwner.call_args.kwargs['reply_handler']
        self.agent.UnregisterPanelRuntime('6', sender=':1.other')
        self.agent.UnregisterPanelRuntime('5', sender=':1.42')
        error.assert_not_called()
        self.agent.UnregisterPanelRuntime('6', sender=':1.42')
        self.assertEqual(error.call_args.args[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        owner_reply(':1.42')
        reply.assert_not_called()
        self.assertFalse(self.agent.state()['panelRuntimeRegistered'])

    def test_panel_unregister_async_reply_tracks_admission_deadline(self):
        a = self.agent
        reply, error = mock.Mock(), mock.Mock()
        a.panel_runtime_sender = ':1.42'
        a.panel_runtime_version = '6'
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=[100, 100]):
            a.UnregisterPanelRuntime('6', sender=':1.42', reply=reply, error=error)
        reply.assert_called_once_with()
        error.assert_not_called()
        self.assertEqual(a.panel_runtime_sender, '')
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=[100, 121]):
            a.UnregisterPanelRuntime('6', sender=':1.42', reply=reply, error=error)
        reply.assert_called_once()
        error.assert_called_once()
        self.assertEqual(error.call_args.args[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')

    def test_post_replace_settings_failure_reconciles_memory(self):
        real_write = agent_module.atomic_json
        def visible_write(path, values):
            real_write(path, values)
            raise agent_module.AtomicWriteError('directory fsync', committed=True)
        with mock.patch.object(agent_module, 'atomic_json', side_effect=visible_write):
            self.agent.persist_settings(threshold=37)
        self.assertEqual(self.agent.threshold, 37)
        self.assertEqual(json.loads(self.agent.settings_path.read_text()), self.agent.settings())
        self.assertIn('durability is uncertain', self.agent.last_error)

    def test_pre_replace_settings_failure_keeps_memory(self):
        self.agent.persist_settings(threshold=31)
        with mock.patch.object(agent_module, 'atomic_json', side_effect=
                               agent_module.AtomicWriteError('replace failed')):
            with self.assertRaises(FakeDBusException):
                self.agent.persist_settings(threshold=40)
        self.assertEqual(self.agent.threshold, 31)
        self.assertEqual(json.loads(self.agent.settings_path.read_text()), self.agent.settings())

    def test_mismatched_or_malformed_committed_settings_disable_owned_features(self):
        for content in ('{}', 'malformed JSON'):
            with self.subTest(content=content):
                self.agent.enabled = True
                self.agent.lid_mode = True
                self.agent.lid_dimming = True
                self.agent.lid_closed = True
                self.agent.brightness_record = None
                self.agent.brightness_journal_state = 'absent'
                self.agent.brightness_path.unlink(missing_ok=True)
                self.agent.brightness_adapter = FakeBrightness()
                self.agent.dim_brightness()
                self.agent.lid_fd = 99
                real_write = agent_module.atomic_json
                def write(path, values):
                    if path == self.agent.settings_path:
                        path.write_text(content)
                        raise agent_module.AtomicWriteError('uncertain committed content', committed=True)
                    return real_write(path, values)
                with mock.patch.object(agent_module, 'atomic_json', side_effect=write), \
                        mock.patch.object(agent_module.os, 'close') as close:
                    with self.assertRaises(FakeDBusException):
                        self.agent.persist_settings(threshold=40)
                self.assertIn(mock.call(99), close.call_args_list)
                self.assertIsNone(self.agent.lid_fd)
                self.assertFalse(self.agent.lid_mode)
                self.assertFalse(self.agent.lid_dimming)
                self.assertEqual(self.agent.brightness_adapter.value, 70)
                self.assertIsNone(self.agent.brightness_record)
                self.assertIn('Could not save preferences', self.agent.last_error)

    def test_atomic_write_exposes_commit_and_durability_at_each_failure(self):
        path = self.agent.settings_path
        for stage in ('file-open', 'file-write', 'file-fsync', 'replace',
                      'directory-open', 'directory-fsync', 'directory-close'):
            with self.subTest(stage=stage):
                path.write_text('{"old": true}')
                stack = __import__('contextlib').ExitStack()
                with stack:
                    if stage == 'file-open':
                        stack.enter_context(mock.patch.object(Path, 'open', side_effect=OSError(stage)))
                    elif stage == 'file-write':
                        stack.enter_context(mock.patch.object(agent_module.json, 'dump', side_effect=OSError(stage)))
                    elif stage == 'replace':
                        stack.enter_context(mock.patch.object(Path, 'replace', side_effect=OSError(stage)))
                    elif stage == 'directory-open':
                        stack.enter_context(mock.patch.object(agent_module.os, 'open', side_effect=OSError(stage)))
                    elif stage == 'directory-close':
                        real_close = agent_module.os.close
                        def close(fd):
                            real_close(fd)
                            raise OSError('close')
                        stack.enter_context(mock.patch.object(agent_module.os, 'close', side_effect=close))
                    else:
                        stack.enter_context(mock.patch.object(agent_module.os, 'fsync', side_effect=
                            [OSError(stage)] if stage == 'file-fsync' else [None, OSError(stage)]))
                    with self.assertRaises(agent_module.AtomicWriteError) as raised:
                        agent_module.atomic_json(path, {'new': True})
                committed = stage.startswith('directory-')
                self.assertEqual(raised.exception.committed, committed)
                self.assertEqual(raised.exception.durable, stage == 'directory-close')
                self.assertEqual(json.loads(path.read_text()), {'new': True} if committed else {'old': True})

    def test_uncertain_prepared_and_applied_brightness_journals(self):
        real_write = agent_module.atomic_json
        for fail_phase in ('prepared', 'set-confirmed', 'applied'):
            with self.subTest(phase=fail_phase):
                self.agent.brightness_record = None
                self.agent.brightness_journal_state = 'absent'
                self.agent.brightness_path.unlink(missing_ok=True)
                self.agent.brightness_adapter = FakeBrightness()
                def write(path, values):
                    real_write(path, values)
                    if values['phase'] == fail_phase:
                        raise agent_module.AtomicWriteError('fsync', committed=True)
                with mock.patch.object(agent_module, 'atomic_json', side_effect=write):
                    self.assertEqual(self.agent.dim_brightness(), 'pending')
                self.assertEqual(self.agent.brightness_record,
                                 json.loads(self.agent.brightness_path.read_text()))
                self.assertEqual(self.agent.brightness_adapter.writes,
                                 [] if fail_phase == 'prepared' else [0])
                self.assertTrue(self.agent.brightness_recovery_pending())

    def test_healthy_dim_is_owned_and_shutdown_restores_before_remote_release(self):
        self.agent.enabled = True
        self.agent.desired = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        self.assertEqual(self.agent.brightness_state(), 'dimmed-owned')
        self.assertFalse(self.agent.state()['brightnessRecoveryPending'])
        self.agent.session_cookie = 12
        self.agent.session_cookie_state = 'held'
        self.agent.session_cookie_owner = ':1.gnome'
        def remote_release(_cookie, **_kwargs):
            self.assertEqual(self.agent.brightness_adapter.value, 70)
            self.assertIsNone(self.agent.brightness_record)
            self.assertFalse(self.agent.enabled)
            self.assertTrue(self.agent.shutting_down)
            _kwargs['error_handler'](FakeDBusException('remote release timeout'))
        names = types.SimpleNamespace(GetNameOwner=lambda _name, **kwargs:
            kwargs['reply_handler'](':1.gnome'))
        self.agent.proxy = mock.Mock(side_effect=lambda _bus, name, *_args:
            names if name == 'org.freedesktop.DBus' else
            types.SimpleNamespace(Uninhibit=remote_release))
        self.agent.session_bus = types.SimpleNamespace(get_is_connected=lambda: True,
                                                       close=mock.Mock())
        self.agent.stop()
        self.assertFalse(fake_glib.sources)
        self.agent.stop()
        self.assertEqual(self.agent.brightness_adapter.writes, [0, 70])
        self.assertFalse(self.agent.tick())
        self.assertFalse(self.agent.reconcile())
        with self.assertRaises(FakeDBusException):
            self.agent.StartTimer(60, False, False)

    def test_repeated_dimming_enable_preserves_owned_journal_and_brightness(self):
        self.agent.enabled = True
        self.agent.lid_closed = True
        self.agent.SetLidDimming(True)
        record = json.loads(self.agent.brightness_path.read_text())
        self.assertEqual(self.agent.brightness_state(), 'dimmed-owned')
        self.assertEqual(self.agent.brightness_adapter.writes, [0])
        with mock.patch.object(agent_module, 'atomic_json', wraps=agent_module.atomic_json) as write:
            self.agent.SetLidDimming(True)
        self.assertEqual(self.agent.brightness_adapter.writes, [0])
        self.assertEqual(json.loads(self.agent.brightness_path.read_text()), record)
        self.assertEqual(write.call_count, 0)
        self.assertEqual(self.agent.brightness_state(), 'dimmed-owned')

    def test_remote_open_lid_dimming_toggle_has_no_brightness_call(self):
        a = self.agent
        a.enabled = True
        a.lid_closed = False
        a.reconcile_brightness = mock.Mock(side_effect=AssertionError('blocking reconciliation'))
        a.brightness_adapter.read = mock.Mock(side_effect=AssertionError('blocking read'))
        a.brightness_adapter.write = mock.Mock(side_effect=AssertionError('blocking write'))
        replies, failures = [], []
        a.SetLidDimming(True, reply=lambda: replies.append('on'), error=failures.append)
        a.SetLidDimming(False, reply=lambda: replies.append('off'), error=failures.append)
        self.assertEqual(replies, ['on', 'off'])
        self.assertEqual(failures, [])
        self.assertFalse(fake_glib.sources)

    def test_remote_repeated_dimming_enable_waits_for_bounded_read(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        a.SetLidDimming(True)
        record = json.loads(a.brightness_path.read_text())
        pending = []
        a.brightness_adapter.read_async = lambda callback, **_kwargs: pending.append(callback)
        replied, failed = [], []
        with mock.patch.object(agent_module, 'atomic_json', wraps=agent_module.atomic_json) as write:
            a.SetLidDimming(True, reply=lambda: replied.append(True),
                            error=lambda issue: failed.append(issue))
            self.assertEqual((replied, failed), ([], []))
            self.assertEqual(len(pending), 1)
            pending.pop()(0, None)
            self.assertEqual((replied, failed), ([True], []))
            self.assertEqual(write.call_count, 0)
        self.assertEqual(a.brightness_adapter.writes, [0])
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)

    def test_repeated_dimming_verification_counts_admission_work(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        a.SetLidDimming(True)
        now = [100]
        brightness_state = a.brightness_state
        def slow_brightness_state():
            now[0] = 121
            return brightness_state()
        a.brightness_state = slow_brightness_state
        a.brightness_adapter.read_async = mock.Mock()
        replied, failed = [], []
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=lambda: now[0]):
            a.SetLidDimming(True, reply=lambda: replied.append(True), error=failed.append)
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        a.brightness_adapter.read_async.assert_not_called()
        self.assertIsNone(a.dimming_verification)

    def test_remote_repeated_dimming_enable_timeout_retains_journal(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        a.SetLidDimming(True)
        record = json.loads(a.brightness_path.read_text())
        pending = []
        a.brightness_adapter.read_async = lambda callback: pending.append(callback)
        replied, failed = [], []
        a.SetLidDimming(True, reply=lambda: replied.append(True),
                        error=lambda issue: failed.append(issue))
        source = max(fake_glib.sources)
        fake_glib.sources[source][1]()
        pending.pop()(0, None)
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertTrue(a.brightness_recovery_pending())
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)
        self.assertEqual(a.brightness_adapter.writes, [0])

    def test_remote_repeated_dimming_enable_late_success_is_timeout(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        a.SetLidDimming(True)
        pending = []
        a.brightness_adapter.read_async = lambda callback: pending.append(callback)
        replied, failed = [], []
        a.SetLidDimming(True, reply=lambda: replied.append(True),
                        error=lambda issue: failed.append(issue))
        a.dimming_verification['deadline'] = agent_module.time.monotonic() - 1
        pending.pop()(0, None)
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertTrue(a.brightness_recovery_pending())
        self.assertEqual(a.brightness_adapter.writes, [0])
        self.assertIsNone(a.dimming_verification)

    def test_remote_repeated_dimming_enable_rejects_stale_owner(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        a.SetLidDimming(True)
        pending = []
        a.brightness_adapter.read_async = lambda callback: pending.append(callback)
        replied, failed = [], []
        a.SetLidDimming(True, reply=lambda: replied.append(True),
                        error=lambda issue: failed.append(issue))
        a.brightness_adapter = FakeBrightness(value=0)
        pending.pop()(0, None)
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertTrue(a.brightness_recovery_pending())
        self.assertEqual(a.dimming_verifications, set())

    def test_brightness_owner_loss_immediately_invalidates_pending_verification(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        a.SetLidDimming(True)
        pending = []
        a.brightness_adapter.read_async = lambda callback: pending.append(callback)
        replied, failed = [], []
        a.SetLidDimming(True, reply=lambda: replied.append(True), error=failed.append)
        with mock.patch.object(a, 'reconcile_brightness_async'):
            a.on_owner_change(agent_module.BRIGHTNESS, ':1.old', '')
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertTrue(a.brightness_recovery_pending())
        self.assertIsNone(a.dimming_verification)
        pending.pop()(0, None)
        self.assertEqual(len(failed), 1)

    def test_remote_repeated_dimming_enable_is_canceled_by_stop(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        a.SetLidDimming(True)
        pending = []
        a.brightness_adapter.read_async = lambda callback, **_kwargs: pending.append(callback)
        replied, failed = [], []
        a.SetLidDimming(True, reply=lambda: replied.append(True),
                        error=lambda issue: failed.append(issue))
        a.release_async = lambda callback, **_kwargs: callback(True)
        a.loop = types.SimpleNamespace(quit=lambda: None)
        a.stop()
        pending.pop()(0, None)
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(a.dimming_verifications, set())

    def test_remote_repeated_dimming_enable_coalesces_waiters(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        a.SetLidDimming(True)
        pending = []
        a.brightness_adapter.read_async = lambda callback: pending.append(callback)
        replied, failed = [], []
        for index in (1, 2):
            a.SetLidDimming(True, reply=lambda index=index: replied.append(index),
                            error=lambda issue: failed.append(issue))
        self.assertEqual(len(pending), 1)
        pending.pop()(0, None)
        self.assertEqual(replied, [1, 2])
        self.assertEqual(failed, [])
        self.assertIsNone(a.dimming_verification)

    def test_disable_supersedes_pending_remote_dimming_verification(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        a.SetLidDimming(True)
        pending = []
        a.brightness_adapter.read_async = lambda callback: pending.append(callback)
        replied, failed = [], []
        a.SetLidDimming(True, reply=lambda: replied.append(True),
                        error=lambda issue: failed.append(issue))
        a.SetLidDimming(False)
        pending.pop()(0, None)
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(a.brightness_adapter.value, 70)
        self.assertIsNone(a.dimming_verification)

    def test_remote_dimming_verification_rechecks_journal_after_read(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        a.SetLidDimming(True)
        pending = []
        a.brightness_adapter.read_async = lambda callback: pending.append(callback)
        replied, failed = [], []
        a.SetLidDimming(True, reply=lambda: replied.append(True),
                        error=lambda issue: failed.append(issue))
        changed = json.loads(a.brightness_path.read_text())
        changed['before'] = 60
        a.brightness_path.write_text(json.dumps(changed))
        pending.pop()(0, None)
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertTrue(a.brightness_recovery_pending())

    def test_repeated_dimming_enable_rejects_changed_output(self):
        self.agent.enabled = True
        self.agent.lid_closed = True
        self.agent.SetLidDimming(True)
        journal = self.agent.brightness_path.read_text()
        self.agent.brightness_adapter.output_identity = lambda: 'another-output'
        with self.assertRaises(FakeDBusException):
            self.agent.SetLidDimming(True)
        self.assertEqual(self.agent.brightness_adapter.writes, [0])
        self.assertEqual(self.agent.brightness_path.read_text(), journal)
        self.assertTrue(self.agent.brightness_recovery_pending())

    def test_manual_brightness_conflict_can_be_explicitly_rearmed(self):
        self.agent.enabled = True
        self.agent.lid_closed = True
        self.agent.SetLidDimming(True)
        self.agent.brightness_adapter.value = 35
        self.assertEqual(self.agent.restore_brightness(), 'manual-change-preserved')
        self.assertEqual(self.agent.brightness_state(), 'manual-change-preserved')
        with mock.patch.object(agent_module, 'atomic_json', wraps=agent_module.atomic_json) as write:
            self.agent.SetLidDimming(True)
        self.assertEqual(self.agent.brightness_adapter.writes, [0, 0])
        self.assertEqual(self.agent.brightness_state(), 'dimmed-owned')
        self.assertEqual(write.call_count, 3)  # Prepared, Set-confirmed, applied; no preference rewrite.

    def test_notification_dispatch_never_waits_for_remote_reply(self):
        bus = types.SimpleNamespace(get_is_connected=lambda: True)
        self.agent.session_bus = bus
        remote = mock.Mock()
        self.agent.proxy = mock.Mock(return_value=remote)
        agent_module.Agent.notify(self.agent, 'Summary', 'Body')
        call = remote.Notify.call_args
        self.assertEqual(call.kwargs['timeout'], 2)
        self.assertTrue(call.kwargs['reply_handler'])
        self.assertTrue(call.kwargs['error_handler'])
        self.agent.shutting_down = True
        agent_module.Agent.notify(self.agent, 'Summary', 'Body')
        remote.Notify.assert_called_once()

    def test_notification_setup_failure_does_not_interrupt_caller(self):
        self.agent.session_bus = types.SimpleNamespace(get_is_connected=lambda: True)
        self.agent.proxy = mock.Mock(side_effect=TypeError('notification proxy unavailable'))
        agent_module.Agent.notify(self.agent, 'Summary', 'Body')
        self.agent.proxy.assert_called_once()

    def test_repeated_prevention_enable_keeps_healthy_inhibitors(self):
        a = self.agent
        a.session_cookie = 7
        a.session_cookie_state = 'held'
        a.session_cookie_owner = ':1.gnome'
        a.fd = 42
        a.enabled = True
        a.proxy = mock.Mock(side_effect=AssertionError('duplicate remote inhibitor'))
        a.SetPrevention(True)
        self.assertEqual(a.session_cookie, 7)
        self.assertEqual(a.fd, 42)
        self.assertTrue(a.desired)
        a.session_manager_owner.assert_called_once()
        a.refresh_power.assert_called_once()
        a.check_failsafe.assert_called_once()

    def test_healthy_prevention_repetition_clears_only_prevention_error(self):
        a = self.agent
        a.session_cookie = 7
        a.session_cookie_state = 'held'
        a.session_cookie_owner = ':1.gnome'
        a.fd = 42
        a.enabled = True
        a.last_error = 'GNOME inhibitor ownership is unresolved; wait for recovery before enabling'
        a.SetPrevention(True)
        self.assertEqual(a.last_error, '')
        a.last_error = 'Brightness recovery pending: display unavailable'
        a.SetPrevention(True)
        self.assertEqual(a.last_error, 'Brightness recovery pending: display unavailable')

    def test_prevention_dimming_verification_marks_stale_adapter_pending(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        self.assertEqual(a.dim_brightness(), 'redimmed')
        old_adapter = a.brightness_adapter
        old_adapter.read_async = mock.Mock()
        results = []
        agent_module.Agent.reconcile_brightness_async(
            a, 'prevention-enable', results.append)
        callback = old_adapter.read_async.call_args.args[0]
        a.brightness_adapter = FakeBrightness()
        callback(0, None)
        self.assertEqual(results, ['pending'])
        self.assertTrue(a.brightness_retry_pending)
        self.assertTrue(a.brightness_recovery_pending())
        self.assertIn('adapter changed', a.brightness_error)

    def test_concurrent_prevention_enable_coalesces_owner_lookup(self):
        a = self.agent
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True,
                                              close=mock.Mock())
        names = mock.Mock()
        a.proxy = mock.Mock(return_value=names)
        first, second = [], []
        a.SetPrevention(True, reply=lambda: first.append('ok'), error=first.append)
        a.SetPrevention(True, reply=lambda: second.append('ok'), error=second.append)
        names.GetNameOwner.assert_called_once()
        names.GetNameOwner.call_args.kwargs['error_handler'](
            FakeDBusException('session manager unavailable'))
        self.assertEqual(len(first), 1)
        self.assertEqual(len(second), 1)
        self.assertIsInstance(first[0], FakeDBusException)
        self.assertIsInstance(second[0], FakeDBusException)
        self.assertIsNone(a.prevention_acquisition)

    def test_acquisition_success_preserves_unrelated_diagnostic(self):
        a = self.agent
        a.desired = True
        a.last_error = 'Could not save preferences: disk unavailable'
        a.handle_lid_change = mock.Mock()
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lambda name, **kwargs:
            kwargs['reply_handler'](':1.gnome' if name == agent_module.SESSION else ':1.login')))
        login = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            kwargs['reply_handler'](types.SimpleNamespace(take=lambda: 42))))
        gnome = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            kwargs['reply_handler'](agent_module.dbus.UInt32(7))))
        a.proxy = mock.Mock(side_effect=lambda _bus, owner, _path, _interface:
            names if owner == 'org.freedesktop.DBus' else
            login if owner == ':1.login' else gnome)
        results = []
        a.acquire_async(results.append)
        self.assertEqual(results, [None])
        self.assertTrue(a.enabled)
        self.assertEqual(a.last_error, 'Could not save preferences: disk unavailable')

        a.enabled = False
        a.fd = None
        a.session_cookie = None
        a.session_cookie_state = 'absent'
        a.acquisition_error = 'Prevention acquisition exceeded its deadline'
        a.last_error = a.acquisition_error
        results.clear()
        a.acquire_async(results.append)
        self.assertEqual(results, [None])
        self.assertEqual(a.last_error, '')

    def test_remote_healthy_prevention_repetition_rechecks_fresh_power(self):
        a = self.agent
        a.session_cookie = 7
        a.session_cookie_state = 'held'
        a.session_cookie_owner = ':1.gnome'
        a.fd = 42
        a.sleep_fd_owner = ':1.login'
        a.enabled = True
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lambda name, **kwargs:
            kwargs['reply_handler'](':1.gnome' if name == agent_module.SESSION else ':1.login')))
        a.proxy = mock.Mock(return_value=names)
        a.acquire_async = mock.Mock(side_effect=AssertionError('healthy pair was reacquired'))
        a.check_failsafe = mock.Mock(side_effect=lambda callback: callback('clear'))
        a.reconcile_brightness_async = mock.Mock(side_effect=lambda _reason, callback:
                                                 callback('unchanged'))
        replies, errors = [], []
        with mock.patch.object(agent_module.os, 'fstat', return_value=object()):
            a.SetPrevention(True, reply=lambda: replies.append('ok'), error=errors.append)
        self.assertEqual(replies, ['ok'])
        self.assertEqual(errors, [])
        self.assertEqual(names.GetNameOwner.call_count, 2)
        a.refresh_power_async.assert_called_once()
        a.check_failsafe.assert_called_once()
        a.acquire_async.assert_not_called()
        self.assertEqual(a.session_cookie, 7)
        self.assertEqual(a.fd, 42)

    def test_partial_prevention_ownership_releases_without_new_acquisition(self):
        a = self.agent
        a.session_cookie = 7
        a.session_cookie_state = 'release-pending'
        a.session_cookie_owner = ':1.gnome'
        a.acquire_async = mock.Mock(side_effect=AssertionError('partial pair was reacquired'))
        a.release_async = mock.Mock(side_effect=lambda callback: callback(False))
        replies, errors = [], []
        a.SetPrevention(True, reply=lambda: replies.append('ok'), error=errors.append)
        self.assertEqual(replies, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].get_dbus_name(), agent_module.IFACE + '.ReleasePending')
        a.release_async.assert_called_once()
        a.acquire_async.assert_not_called()
        self.assertFalse(a.desired)

    def test_shutdown_rejects_new_prevention_request_without_acquisition(self):
        a = self.agent
        a.shutting_down = True
        a.acquire_async = mock.Mock()
        with self.assertRaises(FakeDBusException) as failure:
            a.SetPrevention(True, reply=mock.Mock(), error=mock.Mock())
        self.assertEqual(failure.exception.get_dbus_name(),
                         agent_module.IFACE + '.Unavailable')
        a.acquire_async.assert_not_called()

    def test_prevention_off_wins_pending_owner_lookup(self):
        a = self.agent
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True,
                                              close=mock.Mock())
        names = mock.Mock()
        a.proxy = mock.Mock(return_value=names)
        enabled, disabled = [], []
        a.SetPrevention(True, reply=lambda: enabled.append('ok'), error=enabled.append)
        owner_reply = names.GetNameOwner.call_args.kwargs['reply_handler']
        a.SetPrevention(False, reply=lambda: disabled.append('ok'), error=disabled.append)
        self.assertEqual(disabled, ['ok'])
        self.assertEqual(len(enabled), 1)
        self.assertEqual(enabled[0].get_dbus_name(), agent_module.IFACE + '.Canceled')
        owner_reply(':1.gnome')
        names.GetNameOwner.assert_called_once()
        self.assertFalse(a.desired)
        self.assertFalse(a.enabled)
        a.session_bus.close.assert_not_called()

    def test_prevention_off_deadline_includes_canceling_pending_enable(self):
        a = self.agent
        now = [100]
        a.prevention_enable_requests.add(lambda _failure: now.__setitem__(0, 121))
        a.release_async = mock.Mock(side_effect=lambda callback: callback(True))
        replied, failed = [], []
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=lambda: now[0]):
            a.SetPrevention(False, reply=lambda: replied.append(True), error=failed.append)
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        self.assertFalse(a.desired)
        a.release_async.assert_called_once()

    def test_late_fresh_power_does_not_start_prevention_failsafe_work(self):
        a = self.agent
        power_callbacks, replied, failed = [], [], []
        a.acquire_async = lambda callback: (setattr(a, 'enabled', True), callback(None))
        a.refresh_power_async = mock.Mock(side_effect=lambda callback, **_kwargs:
                                          power_callbacks.append(callback))
        with mock.patch.object(agent_module.time, 'monotonic', return_value=100):
            a.SetPrevention(True, reply=lambda: replied.append(True), error=failed.append)
        with mock.patch.object(agent_module.time, 'monotonic', return_value=121):
            power_callbacks.pop()(True)
        a.check_failsafe.assert_not_called()
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')

    def test_late_failsafe_result_does_not_start_brightness_work(self):
        a = self.agent
        failsafe_callbacks, replied, failed = [], [], []
        a.acquire_async = lambda callback: (setattr(a, 'enabled', True), callback(None))
        a.check_failsafe = mock.Mock(side_effect=lambda callback:
                                     failsafe_callbacks.append(callback))
        a.reconcile_brightness_async = mock.Mock()
        with mock.patch.object(agent_module.time, 'monotonic', return_value=100):
            a.SetPrevention(True, reply=lambda: replied.append(True), error=failed.append)
        self.assertEqual(len(failsafe_callbacks), 1)
        with mock.patch.object(agent_module.time, 'monotonic', return_value=121):
            failsafe_callbacks.pop()('clear')
        a.reconcile_brightness_async.assert_not_called()
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')

    def test_late_healthy_prevention_owner_replies_do_not_start_fresh_power(self):
        for late_stage in ('gnome', 'login', 'owner-error'):
            with self.subTest(late_stage=late_stage):
                a = self.agent
                a.session_cookie = 7
                a.session_cookie_state = 'held'
                a.session_cookie_owner = ':1.gnome'
                a.fd = 42
                a.sleep_fd_owner = ':1.login'
                a.enabled = True
                a.desired = False
                callbacks, error_callbacks = [], []
                names = types.SimpleNamespace(GetNameOwner=mock.Mock(
                    side_effect=lambda _name, **kwargs: (
                        callbacks.append(kwargs['reply_handler']),
                        error_callbacks.append(kwargs['error_handler']))))
                a.proxy = mock.Mock(return_value=names)
                a.refresh_power_async = mock.Mock()
                a.release_async = mock.Mock()
                replied, failed = [], []
                with mock.patch.object(agent_module.os, 'fstat', return_value=object()), \
                        mock.patch.object(agent_module.time, 'monotonic', return_value=100):
                    a.SetPrevention(True, reply=lambda: replied.append(True), error=failed.append)
                    self.assertEqual(len(callbacks), 1)
                    if late_stage == 'login':
                        callbacks.pop(0)(':1.gnome')
                        self.assertEqual(len(callbacks), 1)
                with mock.patch.object(agent_module.time, 'monotonic', return_value=121):
                    if late_stage == 'owner-error':
                        error_callbacks.pop(0)(FakeDBusException('owner lost',
                            name='org.freedesktop.DBus.Error.NameHasNoOwner'))
                    else:
                        callbacks.pop(0)(':1.gnome' if late_stage == 'gnome' else ':1.login')
                self.assertEqual(names.GetNameOwner.call_count,
                                 2 if late_stage == 'login' else 1)
                a.refresh_power_async.assert_not_called()
                a.release_async.assert_not_called()
                self.assertEqual(replied, [])
                self.assertEqual(len(failed), 1)
                self.assertEqual(failed[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')

    def test_lid_mode_disable_reports_elapsed_local_budget(self):
        a = self.agent
        now = [100]
        persist = a.persist_settings
        def slow_persist(**values):
            persist(**values)
            now[0] = 121
        a.persist_settings = slow_persist
        replied, failed = [], []
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=lambda: now[0]):
            a.SetLidMode(False, reply=lambda: replied.append(True), error=failed.append)
        self.assertEqual(replied, [])
        self.assertEqual(failed[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        self.assertFalse(a.lid_mode)

    def test_lid_mode_disable_deadline_includes_superseding_pending_request(self):
        a = self.agent
        now = [100]
        a.lid_mode_request = {'enabled': True}
        a.finish_lid_mode_request = mock.Mock(side_effect=lambda *_args: now.__setitem__(0, 121))
        replied, failed = [], []
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=lambda: now[0]):
            a.SetLidMode(False, reply=lambda: replied.append(True), error=failed.append)
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        self.assertFalse(a.lid_mode)

    def test_immediate_dimming_change_reports_elapsed_local_budget(self):
        a = self.agent
        now = [100]
        persist = a.persist_settings
        def slow_persist(**values):
            persist(**values)
            now[0] = 121
        a.persist_settings = slow_persist
        replied, failed = [], []
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=lambda: now[0]):
            a.SetLidDimming(True, reply=lambda: replied.append(True), error=failed.append)
        self.assertEqual(replied, [])
        self.assertEqual(failed[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        self.assertTrue(a.lid_dimming)

    def test_expired_dimming_admission_does_not_start_brightness_write(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        now = [100]
        persist = a.persist_settings
        def slow_persist(**values):
            persist(**values)
            now[0] = 121
        a.persist_settings = slow_persist
        a.dim_brightness_async = mock.Mock()
        replied, failed = [], []
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=lambda: now[0]):
            a.SetLidDimming(True, reply=lambda: replied.append(True), error=failed.append)
        self.assertEqual(replied, [])
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        a.dim_brightness_async.assert_not_called()
        self.assertTrue(a.lid_dimming)

    def test_failsafe_change_reports_elapsed_local_budget(self):
        a = self.agent
        now = [100]
        persist = a.persist_settings
        def slow_persist(**values):
            persist(**values)
            now[0] = 121
        a.persist_settings = slow_persist
        replied, failed = [], []
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=lambda: now[0]):
            a.SetFailsafe(True, 25, reply=lambda: replied.append(True), error=failed.append)
        self.assertEqual(replied, [])
        self.assertEqual(failed[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        self.assertTrue(a.failsafe)
        self.assertEqual(a.threshold, 25)

    def test_known_cookie_cleanup_does_not_disconnect_as_unknown_acquisition(self):
        a = self.agent
        a.desired = True
        owners = iter((':1.gnome', FakeDBusException('owner check failed'), ':1.gnome'))
        def next_owner():
            result = next(owners)
            if isinstance(result, Exception):
                raise result
            return result
        a.session_manager_owner = mock.Mock(side_effect=next_owner)
        session = types.SimpleNamespace(Inhibit=mock.Mock(return_value=agent_module.dbus.UInt32(7)),
                                        Uninhibit=mock.Mock(return_value=None))
        login = types.SimpleNamespace(Inhibit=mock.Mock(return_value=types.SimpleNamespace(take=lambda: 42)))
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(
            side_effect=lambda name, **_kwargs: ':1.login' if name == agent_module.LOGIN else name))
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
                            names if name == 'org.freedesktop.DBus' else
                            login if name == ':1.login' else session)
        a.session_bus = types.SimpleNamespace(close=mock.Mock(), get_is_connected=lambda: True)
        with mock.patch.object(agent_module.os, 'close') as close:
            with self.assertRaises(FakeDBusException):
                a.acquire()
        session.Uninhibit.assert_called_once_with(7, timeout=3)
        close.assert_called_once_with(42)
        a.session_bus.close.assert_not_called()
        self.assertEqual(a.session_cookie_state, 'absent')

    def test_canceled_async_acquisition_closes_late_login_fd(self):
        a = self.agent
        a.desired = True
        results, login_replies = [], []
        def lookup(name, **kwargs):
            owner = ':1.gnome' if name == agent_module.SESSION else ':1.login'
            kwargs['reply_handler'](owner)
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lookup))
        login = types.SimpleNamespace(Inhibit=mock.Mock(
            side_effect=lambda *_args, **kwargs: login_replies.append(kwargs['reply_handler'])))
        gnome = types.SimpleNamespace(Inhibit=mock.Mock())
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
                            names if name == 'org.freedesktop.DBus' else
                            login if name == ':1.login' else gnome)
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True, close=mock.Mock())
        a.acquire_async(results.append)
        self.assertEqual(len(login_replies), 1)
        a.cancel_prevention_acquisition('off won the race')
        with mock.patch.object(agent_module.os, 'close') as close:
            login_replies.pop()(types.SimpleNamespace(take=lambda: 42))
        close.assert_called_once_with(42)
        gnome.Inhibit.assert_not_called()
        a.session_bus.close.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], FakeDBusException)
        self.assertIsNone(a.prevention_acquisition)
        self.assertIsNone(a.fd)

    def test_stop_during_login_inhibit_closes_late_fd(self):
        a = self.agent
        a.desired = True
        results, login_replies = [], []
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lambda name, **kwargs:
            kwargs['reply_handler'](':1.gnome' if name == agent_module.SESSION else ':1.login')))
        login = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            login_replies.append(kwargs['reply_handler'])))
        gnome = types.SimpleNamespace(Inhibit=mock.Mock())
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
                            names if name == 'org.freedesktop.DBus' else
                            login if name == ':1.login' else gnome)
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True, close=mock.Mock())
        a.acquire_async(results.append)
        self.assertEqual(len(login_replies), 1)
        a.stop()
        with mock.patch.object(agent_module.os, 'close') as close:
            login_replies.pop()(types.SimpleNamespace(take=lambda: 42))
        close.assert_called_once_with(42)
        gnome.Inhibit.assert_not_called()
        a.session_bus.close.assert_not_called()
        a.loop.quit.assert_called()
        self.assertEqual(len(results), 1)
        self.assertIsNone(a.fd)

    def assert_stop_during_owner_lookup(self, stage):
        a = self.agent
        a.desired = True
        results, owner_replies = [], []
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(
            side_effect=lambda _name, **kwargs:
            owner_replies.append(kwargs['reply_handler'])))
        login = types.SimpleNamespace(Inhibit=mock.Mock())
        gnome = types.SimpleNamespace(Inhibit=mock.Mock())
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
            names if name == 'org.freedesktop.DBus' else
            login if name == ':1.login' else gnome)
        a.session_bus = types.SimpleNamespace(
            get_is_connected=lambda: True, close=mock.Mock())
        a.acquire_async(results.append)
        if stage == 'login':
            owner_replies.pop()(':1.gnome')
        self.assertEqual(len(owner_replies), 1)
        a.stop()
        owner_replies.pop()(':1.gnome' if stage == 'session' else ':1.login')
        self.assertEqual(len(results), 1)
        self.assertIsNone(a.prevention_acquisition)
        self.assertIsNone(a.fd)
        login.Inhibit.assert_not_called()
        gnome.Inhibit.assert_not_called()
        a.session_bus.close.assert_not_called()
        a.loop.quit.assert_called()

    def test_stop_during_session_owner_lookup_leaves_late_reply_inert(self):
        self.assert_stop_during_owner_lookup('session')

    def test_stop_during_login_owner_lookup_leaves_late_reply_inert(self):
        self.assert_stop_during_owner_lookup('login')

    def test_canceled_async_acquisition_closes_bus_before_late_cookie(self):
        a = self.agent
        a.desired = True
        results, gnome_replies = [], []
        def lookup(name, **kwargs):
            kwargs['reply_handler'](':1.gnome' if name == agent_module.SESSION else ':1.login')
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lookup))
        login = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            kwargs['reply_handler'](types.SimpleNamespace(take=lambda: 42))))
        gnome = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            gnome_replies.append(kwargs['reply_handler'])))
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
                            names if name == 'org.freedesktop.DBus' else
                            login if name == ':1.login' else gnome)
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True, close=mock.Mock())
        with mock.patch.object(agent_module.os, 'close') as close:
            a.acquire_async(results.append)
            self.assertEqual(len(gnome_replies), 1)
            a.cancel_prevention_acquisition('off won the race')
            gnome_replies.pop()(agent_module.dbus.UInt32(7))
        close.assert_called_once_with(42)
        a.session_bus.close.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], FakeDBusException)
        self.assertIsNone(a.prevention_acquisition)
        self.assertIsNone(a.session_cookie)
        self.assertTrue(a.exit_failure)

    def prepare_unknown_acquisition_with_brightness_restore(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        a.enabled = False
        a.desired = True
        bus = types.SimpleNamespace(get_is_connected=lambda: True, close=mock.Mock())
        a.session_bus = bus
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lambda name, **kwargs:
            kwargs['reply_handler'](':1.gnome' if name == agent_module.SESSION else ':1.login')))
        login = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            kwargs['reply_handler'](types.SimpleNamespace(take=lambda: 42))))
        gnome = types.SimpleNamespace(Inhibit=mock.Mock())
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
            names if name == 'org.freedesktop.DBus' else
            login if name == ':1.login' else gnome)
        acquisition_results, restorations = [], []
        a.acquire_async(acquisition_results.append)
        self.assertEqual(gnome.Inhibit.call_count, 1)
        a.restore_brightness_async = lambda callback: restorations.append(callback)
        a.attempt_session_release_async = lambda callback: callback(True)
        return a, bus, acquisition_results, restorations

    def test_off_defers_unknown_acquisition_disconnect_until_restore(self):
        a, bus, acquisition_results, restorations = \
            self.prepare_unknown_acquisition_with_brightness_restore()
        results = []
        a.release_async(results.append)
        self.assertEqual(len(acquisition_results), 1)
        self.assertEqual(len(restorations), 1)
        bus.close.assert_not_called()
        self.assertEqual(results, [])
        restorations.pop()('restored')
        bus.close.assert_called_once()
        self.assertEqual(results, [False])

    def test_stop_defers_unknown_acquisition_disconnect_until_restore(self):
        a, bus, acquisition_results, restorations = \
            self.prepare_unknown_acquisition_with_brightness_restore()
        a.stop()
        self.assertEqual(len(acquisition_results), 1)
        self.assertEqual(len(restorations), 1)
        bus.close.assert_not_called()
        restorations.pop()('restored')
        bus.close.assert_called_once()
        a.loop.quit.assert_called()

    def test_async_known_cookie_owner_mismatch_releases_without_disconnect(self):
        a = self.agent
        a.desired = True
        results = []
        session_lookups = 0
        def lookup(name, **kwargs):
            nonlocal session_lookups
            if name == agent_module.SESSION:
                session_lookups += 1
                owner = ':1.gnome' if session_lookups == 1 else ':1.other'
            else:
                owner = ':1.login' if name == agent_module.LOGIN else ':1.gnome'
            kwargs['reply_handler'](owner)
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lookup))
        login = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            kwargs['reply_handler'](types.SimpleNamespace(take=lambda: 42))))
        gnome = types.SimpleNamespace(
            Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
                kwargs['reply_handler'](agent_module.dbus.UInt32(7))),
            Uninhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
                kwargs['reply_handler']()))
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
                            names if name == 'org.freedesktop.DBus' else
                            login if name == ':1.login' else gnome)
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True, close=mock.Mock())
        with mock.patch.object(agent_module.os, 'close') as close:
            a.acquire_async(results.append)
        close.assert_called_once_with(42)
        gnome.Uninhibit.assert_called_once()
        a.session_bus.close.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertIsInstance(results[0], FakeDBusException)
        self.assertEqual(a.session_cookie_state, 'absent')
        self.assertIsNone(a.session_cookie)

    def test_async_known_cookie_failed_cleanup_stays_release_pending(self):
        a = self.agent
        a.desired = True
        results = []
        session_lookups = 0
        def lookup(name, **kwargs):
            nonlocal session_lookups
            if name == agent_module.SESSION:
                session_lookups += 1
                owner = ':1.gnome' if session_lookups == 1 else ':1.other'
            else:
                owner = ':1.login' if name == agent_module.LOGIN else ':1.gnome'
            kwargs['reply_handler'](owner)
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lookup))
        login = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            kwargs['reply_handler'](types.SimpleNamespace(take=lambda: 42))))
        gnome = types.SimpleNamespace(
            Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
                kwargs['reply_handler'](agent_module.dbus.UInt32(7))),
            Uninhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
                kwargs['error_handler'](FakeDBusException('release failed'))))
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
                            names if name == 'org.freedesktop.DBus' else
                            login if name == ':1.login' else gnome)
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True, close=mock.Mock())
        with mock.patch.object(agent_module.os, 'close') as close:
            a.acquire_async(results.append)
        close.assert_called_once_with(42)
        gnome.Uninhibit.assert_called_once()
        a.session_bus.close.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].get_dbus_name(), agent_module.IFACE + '.ReleasePending')
        self.assertEqual(a.session_cookie, 7)
        self.assertEqual(a.session_cookie_state, 'release-pending')
        self.assertEqual(a.session_cookie_owner, ':1.gnome')

    def test_stop_after_cookie_before_owner_validation_releases_known_cookie(self):
        a = self.agent
        a.desired = True
        results, held_owner_replies = [], []
        session_lookups = 0
        def lookup(name, **kwargs):
            nonlocal session_lookups
            if name == agent_module.SESSION:
                session_lookups += 1
                if session_lookups == 2:
                    held_owner_replies.append(kwargs['reply_handler'])
                    return
                owner = ':1.gnome'
            else:
                owner = ':1.login' if name == agent_module.LOGIN else ':1.gnome'
            kwargs['reply_handler'](owner)
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lookup))
        login = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            kwargs['reply_handler'](types.SimpleNamespace(take=lambda: 42))))
        gnome = types.SimpleNamespace(
            Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
                kwargs['reply_handler'](agent_module.dbus.UInt32(7))),
            Uninhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
                kwargs['reply_handler']()))
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
                            names if name == 'org.freedesktop.DBus' else
                            login if name == ':1.login' else gnome)
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True, close=mock.Mock())
        with mock.patch.object(agent_module.os, 'close') as close:
            a.acquire_async(results.append)
            self.assertEqual(len(held_owner_replies), 1)
            a.stop()
            held_owner_replies.pop()(':1.gnome')
        close.assert_called_once_with(42)
        gnome.Uninhibit.assert_called_once()
        a.session_bus.close.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertIsNone(a.session_cookie)
        a.loop.quit.assert_called()

    def test_stop_after_gnome_confirmation_before_login_validation_releases_cookie(self):
        a = self.agent
        a.desired = True
        results, login_owner_replies = [], []
        login_lookups = 0
        def lookup(name, **kwargs):
            nonlocal login_lookups
            if name == agent_module.LOGIN:
                login_lookups += 1
                if login_lookups == 2:
                    login_owner_replies.append(kwargs['reply_handler'])
                    return
                owner = ':1.login'
            else:
                owner = ':1.gnome'
            kwargs['reply_handler'](owner)
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lookup))
        login = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            kwargs['reply_handler'](types.SimpleNamespace(take=lambda: 42))))
        gnome = types.SimpleNamespace(
            Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
                kwargs['reply_handler'](agent_module.dbus.UInt32(7))),
            Uninhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
                kwargs['reply_handler']()))
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
            names if name == 'org.freedesktop.DBus' else
            login if name == ':1.login' else gnome)
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True, close=mock.Mock())
        with mock.patch.object(agent_module.os, 'close') as close:
            a.acquire_async(results.append)
            self.assertEqual(len(login_owner_replies), 1)
            a.stop()
            login_owner_replies.pop()(':1.login')
        close.assert_called_once_with(42)
        gnome.Uninhibit.assert_called_once()
        a.session_bus.close.assert_not_called()
        self.assertEqual(len(results), 1)
        self.assertIsNone(a.session_cookie)
        a.loop.quit.assert_called()

    def test_async_malformed_cookie_closes_fd_and_connection_boundary(self):
        a = self.agent
        a.desired = True
        results = []
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lambda name, **kwargs:
            kwargs['reply_handler'](':1.gnome' if name == agent_module.SESSION else ':1.login')))
        login = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            kwargs['reply_handler'](types.SimpleNamespace(take=lambda: 42))))
        gnome = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            kwargs['reply_handler']('bad-cookie')))
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
                            names if name == 'org.freedesktop.DBus' else
                            login if name == ':1.login' else gnome)
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True, close=mock.Mock())
        with mock.patch.object(agent_module.os, 'close') as close:
            a.acquire_async(results.append)
        close.assert_called_once_with(42)
        a.session_bus.close.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        self.assertIsNone(a.session_cookie)
        self.assertTrue(a.exit_failure)

    def test_async_predispatch_owner_error_never_closes_session_boundary(self):
        a = self.agent
        a.desired = True
        results = []
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lambda _name, **kwargs:
            kwargs['error_handler'](FakeDBusException('owner unavailable'))))
        a.proxy = mock.Mock(return_value=names)
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True, close=mock.Mock())
        a.acquire_async(results.append)
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        a.session_bus.close.assert_not_called()
        self.assertIsNone(a.prevention_acquisition)
        self.assertIsNone(a.session_cookie)

    def test_async_dispatched_inhibit_error_closes_fd_and_session_boundary(self):
        a = self.agent
        a.desired = True
        results = []
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(side_effect=lambda name, **kwargs:
            kwargs['reply_handler'](':1.gnome' if name == agent_module.SESSION else ':1.login')))
        login = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            kwargs['reply_handler'](types.SimpleNamespace(take=lambda: 42))))
        gnome = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=lambda *_args, **kwargs:
            kwargs['error_handler'](FakeDBusException('remote Inhibit failed'))))
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
                            names if name == 'org.freedesktop.DBus' else
                            login if name == ':1.login' else gnome)
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True, close=mock.Mock())
        with mock.patch.object(agent_module.os, 'close') as close:
            a.acquire_async(results.append)
        close.assert_called_once_with(42)
        a.session_bus.close.assert_called_once()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].get_dbus_name(), agent_module.IFACE + '.Unavailable')
        self.assertIsNone(a.session_cookie)

    def test_malformed_cookie_reply_closes_login_fd_and_connection_boundary(self):
        a = self.agent
        a.desired = True
        session = types.SimpleNamespace(Inhibit=mock.Mock(return_value='bad-cookie'))
        login = types.SimpleNamespace(Inhibit=mock.Mock(return_value=types.SimpleNamespace(take=lambda: 42)))
        names = types.SimpleNamespace(GetNameOwner=mock.Mock(return_value=':1.login'))
        a.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
                            names if name == 'org.freedesktop.DBus' else
                            login if name == ':1.login' else session)
        a.session_bus = types.SimpleNamespace(close=mock.Mock(), get_is_connected=lambda: True)
        with mock.patch.object(agent_module.os, 'close') as close:
            with self.assertRaises(FakeDBusException):
                a.acquire()
        close.assert_called_once_with(42)
        a.session_bus.close.assert_called_once()
        self.assertTrue(a.exit_failure)

    def test_lid_diagnostic_clear_preserves_unrelated_errors(self):
        self.agent.lid_error = 'Lid lock unavailable: denied'
        self.agent.last_error = self.agent.lid_error
        self.agent.SetLidMode(False, reply=mock.Mock(), error=mock.Mock())
        self.assertEqual(self.agent.last_error, '')
        self.assertEqual(self.agent.lid_error, '')
        self.agent.last_error = 'Unrelated persistence failure'
        self.agent.set_lid_error('Lid lock unavailable: denied')
        self.agent.SetLidMode(False, reply=mock.Mock(), error=mock.Mock())
        self.assertEqual(self.agent.last_error, 'Unrelated persistence failure')
        self.assertEqual(self.agent.lid_error, '')

    def test_brightness_state_classifies_actual_work(self):
        self.assertEqual(self.agent.brightness_state(), 'idle')
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        self.assertEqual(self.agent.brightness_state(), 'dimmed-owned')
        self.agent.enabled = False
        self.assertEqual(self.agent.brightness_state(), 'restore-pending')
        self.agent.enabled = True
        self.agent.brightness_record['phase'] = 'prepared'
        self.assertEqual(self.agent.brightness_state(), 'restore-pending')
        for journal_state in ('invalid', 'unreadable'):
            self.agent.brightness_journal_state = journal_state
            self.assertEqual(self.agent.brightness_state(), journal_state)
            self.assertTrue(self.agent.brightness_recovery_pending())
        self.agent.brightness_journal_state = 'absent'
        self.agent.brightness_record = None
        self.agent.brightness_path.unlink(missing_ok=True)
        self.agent.brightness_redim_suppressed = True
        self.agent.brightness_error = 'Manual brightness change preserved'
        self.assertEqual(self.agent.brightness_state(), 'manual-change-preserved')
        self.assertFalse(self.agent.brightness_recovery_pending())

    def test_stop_attempts_restore_before_successful_or_unavailable_release(self):
        for mode in ('applied', 'prepared', 'manual', 'no-adapter', 'no-journal'):
            with self.subTest(mode=mode):
                self.agent.shutting_down = False
                self.agent.brightness_record = None
                self.agent.brightness_journal_state = 'absent'
                self.agent.brightness_path.unlink(missing_ok=True)
                self.agent.brightness_adapter = FakeBrightness()
                self.agent.enabled = True
                self.agent.lid_dimming = True
                self.agent.lid_closed = True
                if mode != 'no-journal':
                    self.agent.dim_brightness()
                if mode == 'prepared':
                    record = dict(self.agent.brightness_record, phase='prepared')
                    record.pop('written')
                    agent_module.atomic_json(self.agent.brightness_path, record)
                    self.agent.brightness_record = record
                if mode == 'manual':
                    self.agent.brightness_adapter.value = 35
                if mode == 'no-adapter':
                    self.agent.brightness_adapter = None
                self.agent.session_cookie = 12
                self.agent.session_cookie_state = 'held'
                self.agent.session_cookie_owner = ':1.gnome'
                observed = []
                def uninhibit(_cookie, **_kwargs):
                    observed.append(self.agent.brightness_record)
                    if mode in ('no-adapter', 'prepared'):
                        self.assertIsNotNone(self.agent.brightness_record)
                    else:
                        self.assertIsNone(self.agent.brightness_record)
                        self.assertEqual(self.agent.brightness_adapter.value, 35 if mode == 'manual' else 70)
                    _kwargs['reply_handler']()
                names = types.SimpleNamespace(GetNameOwner=lambda _name, **kwargs:
                    kwargs['reply_handler'](':1.gnome'))
                self.agent.proxy = mock.Mock(side_effect=lambda _bus, name, *_args:
                    names if name == 'org.freedesktop.DBus' else
                    types.SimpleNamespace(Uninhibit=uninhibit))
                self.agent.session_bus = types.SimpleNamespace(get_is_connected=lambda: True)
                self.agent.stop()
                self.assertEqual(len(observed), 1)
                self.agent.stop()

    def test_stop_waits_for_async_restore_readback_before_release(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        callbacks, events = [], []
        adapter = a.brightness_adapter
        adapter.read_async = lambda callback, **_kwargs: callbacks.append(callback)
        adapter.write_async = lambda value, callback, **_kwargs: (
            events.append(('write', value)), adapter.write(value), callback(None))
        a.release_async = lambda callback, **_kwargs: (
            events.append(('release', adapter.value)), callback(True))
        a.stop()
        self.assertEqual(events, [])
        self.assertEqual(len(callbacks), 1)
        callbacks.pop(0)(0, None)
        self.assertEqual(events, [('write', 70)])
        self.assertEqual(len(callbacks), 1)
        callbacks.pop(0)(70, None)
        self.assertEqual(events, [('write', 70), ('release', 70)])
        self.assertIsNone(a.brightness_record)
        a.loop.quit.assert_called_once()

    def test_prevention_off_waits_for_brightness_readback_before_uninhibit(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        callbacks, events, results = [], [], []
        adapter = a.brightness_adapter
        adapter.read_async = lambda callback, **_kwargs: callbacks.append(callback)
        adapter.write_async = lambda value, callback, **_kwargs: (
            events.append(('write', value)), adapter.write(value), callback(None))
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True)
        a.attempt_session_release_async = lambda callback: (
            events.append(('release', adapter.value)), callback(True))
        a.release_async(results.append)
        self.assertEqual(events, [])
        self.assertEqual(results, [])
        self.assertTrue(a.brightness_release_pending)
        callbacks.pop(0)(0, None)
        self.assertEqual(events, [('write', 70)])
        self.assertEqual(results, [])
        callbacks.pop(0)(70, None)
        self.assertEqual(events, [('write', 70), ('release', 70)])
        self.assertEqual(results, [True])
        self.assertIsNone(a.brightness_record)
        self.assertFalse(a.brightness_release_pending)

    def test_remote_dimming_enable_journals_before_async_write_and_readback(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        adapter = a.brightness_adapter
        reads, writes, replies, errors = [], [], [], []
        adapter.read_async = lambda callback, **_kwargs: reads.append(callback)
        adapter.write_async = lambda value, callback, **_kwargs: writes.append((value, callback))
        a.SetLidDimming(True, reply=lambda: replies.append(True), error=errors.append)
        self.assertEqual((len(reads), writes, replies, errors), (1, [], [], []))
        reads.pop()(70, None)
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][0], 0)
        self.assertEqual(json.loads(a.brightness_path.read_text())['phase'], 'prepared')
        adapter.value = 0
        writes.pop()[1](None)
        self.assertEqual(json.loads(a.brightness_path.read_text())['phase'], 'set-confirmed')
        reads.pop()(0, None)
        self.assertEqual(json.loads(a.brightness_path.read_text())['phase'], 'applied')
        self.assertEqual((replies, errors), ([True], []))

    def test_async_dim_loads_existing_journal_without_starting_new_write(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        record = {'schema': 3, 'identity': a.brightness_adapter.output_identity(),
                  'machine': agent_module.machine_identity(), 'before': 70,
                  'target': 0, 'phase': 'prepared'}
        a.brightness_path.write_text(json.dumps(record))
        a.brightness_record = None
        a.brightness_adapter.read_async = mock.Mock(side_effect=AssertionError('unexpected read'))
        results = []
        a.dim_brightness_async(results.append)
        self.assertEqual(results, ['pending'])
        self.assertEqual(a.brightness_record, record)
        a.brightness_adapter.read_async.assert_not_called()
        self.assertEqual(a.brightness_adapter.writes, [])

    def test_remote_dimming_disable_waits_for_inflight_dim_then_restores(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        adapter = a.brightness_adapter
        reads, writes, on_replies, on_errors, off_replies, off_errors = [], [], [], [], [], []
        adapter.read_async = lambda callback, **_kwargs: reads.append(callback)
        adapter.write_async = lambda value, callback, **_kwargs: writes.append((value, callback))
        a.SetLidDimming(True, reply=lambda: on_replies.append(True), error=on_errors.append)
        reads.pop()(70, None)
        a.SetLidDimming(False, reply=lambda: off_replies.append(True), error=off_errors.append)
        self.assertEqual((on_replies, off_replies, len(writes)), ([], [], 1))
        value, wrote = writes.pop()
        adapter.value = value
        wrote(None)
        reads.pop()(0, None)
        self.assertEqual(len(reads), 1)
        reads.pop()(0, None)
        value, restored = writes.pop()
        self.assertEqual(value, 70)
        adapter.value = value
        restored(None)
        reads.pop()(70, None)
        self.assertEqual(on_replies, [])
        self.assertEqual(len(on_errors), 1)
        self.assertEqual((off_replies, off_errors), ([True], []))
        self.assertIsNone(a.brightness_record)

    def test_stop_waits_for_inflight_dim_and_retains_uncertain_write(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        reads, writes, releases, errors = [], [], [], []
        a.brightness_adapter.read_async = lambda callback, **_kwargs: reads.append(callback)
        a.brightness_adapter.write_async = lambda value, callback, **_kwargs: (
            writes.append((value, callback)))
        a.release_async = lambda callback, **_kwargs: (
            releases.append(True), callback(True))
        a.SetLidDimming(True, reply=lambda: None, error=errors.append)
        reads.pop()(70, None)
        record = json.loads(a.brightness_path.read_text())
        a.stop()
        self.assertEqual(releases, [])
        writes[0][1](FakeDBusException('Set outcome unknown'))
        self.assertEqual(releases, [True])
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)
        self.assertEqual(record['phase'], 'prepared')
        writes[0][1](None)
        self.assertEqual(releases, [True])
        self.assertEqual(len(errors), 1)
        a.loop.quit.assert_called_once()

    def test_remote_dim_timeout_retains_prepared_journal_and_ignores_late_set(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        reads, writes, replies, errors = [], [], [], []
        a.brightness_adapter.read_async = lambda callback, **_kwargs: reads.append(callback)
        a.brightness_adapter.write_async = lambda value, callback, **_kwargs: (
            writes.append((value, callback)))
        a.SetLidDimming(True, reply=lambda: replies.append(True), error=errors.append)
        reads.pop()(70, None)
        record = json.loads(a.brightness_path.read_text())
        source = max(fake_glib.sources)
        fake_glib.sources[source][1]()
        self.assertEqual(replies, [])
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].get_dbus_name(), agent_module.IFACE + '.InvalidState')
        self.assertEqual(record['phase'], 'prepared')
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)
        writes[0][1](None)
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)
        self.assertEqual(len(writes), 1)

    def test_session_disconnect_invalidates_pending_dim_without_losing_journal(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        reads, writes = [], []
        a.brightness_adapter.read_async = lambda callback, **_kwargs: reads.append(callback)
        a.brightness_adapter.write_async = lambda value, callback, **_kwargs: (
            writes.append((value, callback)))
        a.SetLidDimming(True, reply=lambda: None, error=lambda _failure: None)
        reads.pop()(70, None)
        record = json.loads(a.brightness_path.read_text())
        source = a.brightness_dim_operation['source']
        a.on_session_bus_disconnected()
        self.assertIsNone(a.brightness_dim_operation)
        self.assertIn(source, fake_glib.removed)
        writes.pop()[1](None)
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)
        self.assertEqual(record['phase'], 'prepared')

    def test_session_disconnect_invalidates_pending_restore_before_write(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        record = json.loads(a.brightness_path.read_text())
        reads = []
        a.brightness_adapter.read_async = lambda callback, **_kwargs: reads.append(callback)
        a.brightness_adapter.write_async = mock.Mock(side_effect=AssertionError('late write'))
        a.restore_brightness_async(lambda _result: None)
        source = a.brightness_restore_operation['source']
        a.on_session_bus_disconnected()
        self.assertIsNone(a.brightness_restore_operation)
        self.assertIn(source, fake_glib.removed)
        reads.pop()(0, None)
        a.brightness_adapter.write_async.assert_not_called()
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)

    def test_late_restore_read_keeps_journal_without_new_write(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        record = json.loads(a.brightness_path.read_text())
        reads, outcomes = [], []
        a.brightness_adapter.read_async = lambda callback, **_kwargs: reads.append(callback)
        a.brightness_adapter.write_async = mock.Mock()
        clock = [100.0]
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=lambda: clock[0]):
            a.restore_brightness_async(outcomes.append)
            clock[0] = 110.0
            reads.pop()(0, None)
        self.assertEqual(outcomes, ['pending'])
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)
        a.brightness_adapter.write_async.assert_not_called()

    def test_late_restore_readback_keeps_recovery_journal(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        record = json.loads(a.brightness_path.read_text())
        reads, writes, outcomes = [], [], []
        a.brightness_adapter.read_async = lambda callback, **_kwargs: reads.append(callback)
        a.brightness_adapter.write_async = lambda value, callback, **_kwargs: (
            writes.append((value, callback)))
        clock = [100.0]
        with mock.patch.object(agent_module.time, 'monotonic', side_effect=lambda: clock[0]):
            a.restore_brightness_async(outcomes.append)
            reads.pop()(0, None)
            value, wrote = writes.pop()
            self.assertEqual(value, record['before'])
            wrote(None)
            clock[0] = 110.0
            reads.pop()(record['before'], None)
        self.assertEqual(outcomes, ['pending'])
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)

    def test_prevention_off_waits_for_inflight_async_dim_before_release(self):
        a = self.agent
        a.enabled = a.lid_closed = True
        reads, writes, releases = [], [], []
        adapter = a.brightness_adapter
        adapter.read_async = lambda callback, **_kwargs: reads.append(callback)
        adapter.write_async = lambda value, callback, **_kwargs: writes.append((value, callback))
        a.SetLidDimming(True, reply=lambda: None, error=lambda _failure: None)
        reads.pop()(70, None)
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True)
        a.attempt_session_release_async = lambda callback: (
            releases.append(adapter.value), callback(True))
        results = []
        a.release_async(results.append)
        self.assertEqual(releases, [])
        value, wrote = writes.pop()
        adapter.value = value
        wrote(None)
        reads.pop()(0, None)
        reads.pop()(0, None)
        value, restored = writes.pop()
        adapter.value = value
        restored(None)
        reads.pop()(70, None)
        self.assertEqual((releases, results), ([70], [True]))
        self.assertIsNone(a.brightness_record)

    def test_prevention_off_retains_journal_after_uncertain_restore_write(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        record = json.loads(a.brightness_path.read_text())
        writes, releases, results = [], [], []
        a.brightness_adapter.write_async = lambda value, callback, **_kwargs: (
            writes.append((value, callback)))
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True)
        a.attempt_session_release_async = lambda callback: (
            releases.append(True), callback(True))
        a.release_async(results.append)
        self.assertEqual(len(writes), 1)
        self.assertEqual(releases, [])
        writes[0][1](FakeDBusException('restore Set timed out'))
        self.assertEqual((releases, results), ([True], [True]))
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)
        writes[0][1](None)
        self.assertEqual((releases, results), ([True], [True]))
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)

    def test_prevention_off_restore_deadline_setup_failure_releases_without_orphan(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        record = json.loads(a.brightness_path.read_text())
        releases, results = [], []
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True)
        a.attempt_session_release_async = lambda callback: (
            releases.append(True), callback(True))
        with mock.patch.object(fake_glib, 'timeout_add', side_effect=OSError('source setup')), \
                mock.patch.object(agent_module.LOG, 'exception'):
            a.release_async(results.append)
        self.assertEqual((releases, results), ([True], [True]))
        self.assertIsNone(a.brightness_restore_operation)
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)

    def test_stop_joins_prevention_off_restore_and_release(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        reads, releases, off_results = [], [], []
        adapter = a.brightness_adapter
        adapter.read_async = lambda callback, **_kwargs: reads.append(callback)
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True)
        a.attempt_session_release_async = lambda callback: (
            releases.append(adapter.value), callback(True))
        a.release_async(off_results.append)
        a.stop()
        self.assertEqual((len(reads), releases, off_results), (1, [], []))
        reads.pop(0)(0, None)
        self.assertEqual((len(reads), releases), (1, []))
        reads.pop(0)(70, None)
        self.assertEqual((releases, off_results), ([70], [True]))
        a.loop.quit.assert_called_once()
        self.assertIsNone(a.brightness_record)

    def test_stop_joins_already_dispatched_prevention_release(self):
        a = self.agent
        replies, off_results = [], []
        a.session_bus = types.SimpleNamespace(get_is_connected=lambda: True)
        a.attempt_session_release_async = lambda callback: replies.append(callback)
        a.release_async(off_results.append)
        self.assertEqual(len(replies), 1)
        a.stop()
        self.assertEqual(len(replies), 1)
        self.assertTrue(a.prevention_release['force_disconnect'])
        replies.pop()(True)
        self.assertEqual(off_results, [True])
        a.loop.quit.assert_called_once()

    def test_late_restore_read_reply_cannot_remove_uncertain_journal(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        callbacks, outcomes = [], []
        a.brightness_adapter.read_async = lambda callback, **_kwargs: callbacks.append(callback)
        a.restore_brightness_async(outcomes.append)
        record = json.loads(a.brightness_path.read_text())
        pending_callback = callbacks.pop()
        # A failed read settles the operation; an old success cannot write.
        pending_callback(None, FakeDBusException('read timed out'))
        pending_callback(0, None)
        self.assertEqual(outcomes, ['pending'])
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)
        self.assertEqual(a.brightness_adapter.writes, [0])

    def test_stop_retains_journal_when_async_restore_write_is_uncertain(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        a.dim_brightness()
        record = json.loads(a.brightness_path.read_text())
        writes, releases = [], []
        a.brightness_adapter.write_async = lambda value, callback, **_kwargs: (
            writes.append((value, callback)))
        a.release_async = lambda callback, **_kwargs: (
            releases.append(True), callback(True))
        a.stop()
        self.assertEqual(len(writes), 1)
        self.assertEqual(releases, [])
        writes[0][1](FakeDBusException('restore Set timed out'))
        self.assertEqual(releases, [True])
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)
        self.assertTrue(a.brightness_recovery_pending())
        writes[0][1](None)
        self.assertEqual(releases, [True])
        self.assertEqual(json.loads(a.brightness_path.read_text()), record)

    def test_settings_and_journal_memory_match_each_directory_failure(self):
        # Caller-level fault matrix complements atomic_json's visibility test.
        from contextlib import ExitStack
        for operation in ('open', 'fsync', 'close'):
            for kind in ('settings', 'prepared', 'applied'):
                with self.subTest(operation=operation, kind=kind):
                    self.agent.brightness_record = None
                    self.agent.brightness_journal_state = 'absent'
                    self.agent.brightness_path.unlink(missing_ok=True)
                    self.agent.brightness_adapter = FakeBrightness()
                    original = getattr(agent_module.os, operation)
                    calls = 0
                    def fail(*args):
                        nonlocal calls
                        calls += 1
                        desired = 2 if kind == 'applied' else 1
                        if operation == 'fsync':
                            desired *= 2
                        if calls == desired:
                            if operation == 'close':
                                original(*args)
                            raise OSError('injected directory ' + operation)
                        return original(*args)
                    with ExitStack() as stack:
                        stack.enter_context(mock.patch.object(agent_module.os, operation, side_effect=fail))
                        if kind == 'settings':
                            self.agent.persist_settings(threshold=38)
                        else:
                            self.agent.dim_brightness()
                    if kind == 'settings':
                        self.assertEqual(json.loads(self.agent.settings_path.read_text()), self.agent.settings())
                    else:
                        self.assertEqual(json.loads(self.agent.brightness_path.read_text()),
                                         self.agent.brightness_record)
                        self.assertEqual(self.agent.brightness_adapter.writes,
                                         [0] if kind == 'applied' else [])
                        self.assertTrue(self.agent.brightness_recovery_pending())

    def test_shutdown_remote_faults_retain_evidence_and_close_local_fds(self):
        for stage in ('read', 'write', 'readback', 'owner', 'uninhibit'):
            with self.subTest(stage=stage):
                fake_glib.sources.clear()
                a = self.agent
                a.shutting_down = False
                a.exit_failure = False
                a.brightness_record = None
                a.brightness_journal_state = 'absent'
                a.brightness_retry_after = 0
                a.brightness_path.unlink(missing_ok=True)
                a.brightness_adapter = FakeBrightness()
                a.enabled = a.lid_dimming = a.lid_closed = True
                a.dim_brightness()
                a.session_cookie = 12
                a.session_cookie_state = 'held'
                a.session_cookie_owner = ':1.gnome'
                a.fd, a.lid_fd = 99, 100
                names, remote = mock.Mock(), mock.Mock()
                a.proxy = mock.Mock(side_effect=lambda _bus, owner, _path, _interface:
                                    names if owner == 'org.freedesktop.DBus' else remote)
                a.session_bus = types.SimpleNamespace(close=mock.Mock(),
                                                       get_is_connected=lambda: True)
                failure = FakeDBusException('timeout')
                with __import__('contextlib').ExitStack() as stack:
                    if stage == 'read':
                        stack.enter_context(mock.patch.object(
                            a.brightness_adapter, 'read_async',
                            side_effect=lambda callback, **_kwargs: callback(None, failure)))
                    elif stage == 'readback':
                        reads = 0
                        original_read = a.brightness_adapter.read_async
                        def read_with_failure(callback, **kwargs):
                            nonlocal reads
                            reads += 1
                            if reads == 2:
                                callback(None, failure)
                            else:
                                original_read(callback, **kwargs)
                        stack.enter_context(mock.patch.object(
                            a.brightness_adapter, 'read_async', side_effect=read_with_failure))
                    elif stage == 'write':
                        stack.enter_context(mock.patch.object(
                            a.brightness_adapter, 'write_async',
                            side_effect=lambda _value, callback, **_kwargs: callback(failure)))
                    elif stage == 'owner':
                        names.GetNameOwner.side_effect = lambda _name, **kwargs: \
                            kwargs['error_handler'](failure)
                    else:
                        remote.Uninhibit.side_effect = lambda _cookie, **kwargs: \
                            kwargs['error_handler'](failure)
                    if stage != 'owner':
                        names.GetNameOwner.side_effect = lambda _name, **kwargs: \
                            kwargs['reply_handler'](':1.gnome')
                    if stage != 'uninhibit':
                        remote.Uninhibit.side_effect = lambda _cookie, **kwargs: \
                            kwargs['reply_handler']()
                    close = stack.enter_context(mock.patch.object(agent_module.os, 'close'))
                    a.stop()
                self.assertIn(mock.call(99), close.call_args_list)
                self.assertIn(mock.call(100), close.call_args_list)
                self.assertIsNone(a.fd)
                self.assertIsNone(a.lid_fd)
                if stage in ('read', 'write', 'readback'):
                    self.assertTrue(a.brightness_path.exists())
                    self.assertTrue(a.brightness_recovery_pending())
                else:
                    self.assertEqual(a.brightness_adapter.value, 70)
                    a.session_bus.close.assert_called_once()
                self.assertFalse(fake_glib.sources)

    def test_stop_deadline_closes_connection_and_local_fds_after_stalled_restore(self):
        a = self.agent
        a.session_bus = types.SimpleNamespace(close=mock.Mock(),
                                              get_is_connected=lambda: True)
        a.fd, a.lid_fd = 99, 100
        a.restore_brightness_async = mock.Mock()
        with mock.patch.object(agent_module.os, 'close') as close:
            a.stop()
            source = a.stop_deadline_source
            self.assertIn(source, fake_glib.sources)
            fake_glib.sources[source][1]()
        self.assertTrue(a.exit_failure)
        self.assertIsNone(a.fd)
        self.assertIsNone(a.lid_fd)
        self.assertIn(mock.call(99), close.call_args_list)
        self.assertIn(mock.call(100), close.call_args_list)
        a.session_bus.close.assert_called_once()
        a.loop.quit.assert_called()

    def test_precommit_failures_preserve_settings_and_prepared_recovery_record(self):
        from contextlib import ExitStack
        for stage in ('open', 'write', 'fsync', 'replace'):
            for kind in ('settings', 'prepared', 'applied'):
                with self.subTest(stage=stage, kind=kind):
                    self.agent.brightness_record = None
                    self.agent.brightness_journal_state = 'absent'
                    self.agent.brightness_retry_pending = False
                    self.agent.brightness_path.unlink(missing_ok=True)
                    self.agent.brightness_adapter = FakeBrightness()
                    self.agent.persist_settings(threshold=31)
                    wanted = 2 if kind == 'applied' else 1
                    calls = 0
                    with ExitStack() as stack:
                        if stage == 'open':
                            original = Path.open
                            def fail(path, *args, **kwargs):
                                nonlocal calls
                                if path.suffix == '.tmp':
                                    calls += 1
                                    if calls == wanted:
                                        raise OSError('temporary open')
                                return original(path, *args, **kwargs)
                            stack.enter_context(mock.patch.object(Path, 'open', fail))
                        elif stage == 'replace':
                            original = Path.replace
                            def fail(path, *args, **kwargs):
                                nonlocal calls
                                calls += 1
                                if calls == wanted:
                                    raise OSError('replace')
                                return original(path, *args, **kwargs)
                            stack.enter_context(mock.patch.object(Path, 'replace', fail))
                        else:
                            owner = agent_module.json if stage == 'write' else agent_module.os
                            name = 'dump' if stage == 'write' else 'fsync'
                            original = getattr(owner, name)
                            if stage == 'fsync' and kind == 'applied':
                                wanted = 3  # prepared file + directory precede applied file
                            def fail(*args, **kwargs):
                                nonlocal calls
                                calls += 1
                                if calls == wanted:
                                    raise OSError(stage)
                                return original(*args, **kwargs)
                            stack.enter_context(mock.patch.object(owner, name, side_effect=fail))
                        if kind == 'settings':
                            with self.assertRaises(FakeDBusException):
                                self.agent.persist_settings(threshold=42)
                        else:
                            self.assertEqual(self.agent.dim_brightness(), 'pending')
                    if kind == 'settings':
                        self.assertEqual(self.agent.threshold, 31)
                        self.assertEqual(json.loads(self.agent.settings_path.read_text()), self.agent.settings())
                    elif kind == 'prepared':
                        self.assertFalse(self.agent.brightness_path.exists())
                        self.assertIsNone(self.agent.brightness_record)
                        self.assertEqual(self.agent.brightness_adapter.writes, [])
                    else:
                        self.assertEqual(json.loads(self.agent.brightness_path.read_text()), self.agent.brightness_record)
                        self.assertEqual(self.agent.brightness_record['phase'], 'prepared')
                        self.assertEqual(self.agent.brightness_adapter.writes, [0])
                        self.assertTrue(self.agent.brightness_recovery_pending())
    def test_state_publishes_transaction_outcomes(self):
        self.agent.sleep_tx['phase'] = 'uncertain-blocked'
        self.agent.sleep_outcome = 'logind-state-unreadable'
        self.agent.suspend_request_outcome = 'unknown'
        with mock.patch.object(agent_module, 'boottime', return_value=0):
            state = self.agent.state()
        self.assertEqual(state['sleepPhase'], 'uncertain-blocked')
        self.assertEqual(state['sleepOutcome'], 'logind-state-unreadable')
        self.assertEqual(state['suspendRequestOutcome'], 'unknown')

    def test_timer_start_preserves_unrelated_diagnostic(self):
        a = self.agent
        a.last_error = 'GNOME inhibitor release is pending; retry required'
        reply, error = mock.Mock(), mock.Mock()
        a.StartTimer(60, False, False, reply=reply, error=error)
        reply.assert_called_once_with()
        error.assert_not_called()
        self.assertEqual(a.last_error, 'GNOME inhibitor release is pending; retry required')

        a.last_error = 'Could not save preferences: previous write failed'
        a.StartTimer(120, False, False, reply=reply, error=error)
        self.assertEqual(reply.call_count, 2)
        error.assert_not_called()
        self.assertEqual(a.last_error, '')

        a.last_error = 'Could not save preferences: previous write failed'
        warning = 'Preferences saved, but directory durability is uncertain: fsync failed'
        a.persist_settings = mock.Mock(side_effect=lambda **_changes:
                                       setattr(a, 'last_error', warning))
        a.StartTimer(180, False, False, reply=reply, error=error)
        self.assertEqual(reply.call_count, 3)
        error.assert_not_called()
        self.assertEqual(a.last_error, warning)

    def test_async_timer_dispatches_once_after_release_and_final_preflight(self):
        self.agent.deadline = 10
        self.agent.timer_phase = 'running'
        self.agent.require_lid = True
        self.agent.lid_closed = True
        self.agent.enabled = self.agent.desired = True
        self.agent.last_error = 'Unrelated persistence failure'
        preflights = []

        def preflight(callback, **kwargs):
            preflights.append(kwargs)
            callback(True, '', ':1.login')

        def release(callback, **_kwargs):
            self.agent.enabled = False
            self.agent.session_cookie = None
            self.agent.session_cookie_state = 'absent'
            callback(True)

        login = types.SimpleNamespace(Suspend=mock.Mock())
        self.agent.suspend_preflight_async = mock.Mock(side_effect=preflight)
        self.agent.release_async = mock.Mock(side_effect=release)
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertTrue(self.agent.sleep_now('Countdown elapsed'))
        login.Suspend.assert_called_once()
        self.assertEqual(login.Suspend.call_args.args, (False,))
        self.assertEqual([item['require_cookie_absent'] for item in preflights],
                         [False, True])
        self.assertIsNone(self.agent.deadline)
        self.assertEqual(self.agent.timer_phase, 'consumed')
        self.assertEqual(self.agent.last_error, 'Unrelated persistence failure')
        login.Suspend.call_args.kwargs['reply_handler']()
        self.assertEqual(self.agent.suspend_request_outcome, 'accepted')
        self.assertEqual(self.agent.sleep_tx['phase'], 'request-pending')
        self.assertEqual(self.agent.last_error, 'Unrelated persistence failure')

    def test_accepted_suspend_reply_clears_only_previous_suspend_diagnostic(self):
        a = self.agent
        a.last_error = 'Suspend request rejected: previous attempt'
        tx = a.begin_sleep_request('timer', 'Countdown elapsed')
        a.finish_suspend_request(tx, 'timer')
        self.assertEqual(a.last_error, '')
        self.assertEqual(a.suspend_request_outcome, 'accepted')

        a.last_error = 'Unrelated persistence failure'
        tx = a.begin_sleep_request('timer', 'Countdown elapsed')
        a.finish_suspend_request(tx, 'timer')
        self.assertEqual(a.last_error, 'Unrelated persistence failure')

    def test_async_timer_lid_change_after_release_blocks_suspend(self):
        self.agent.deadline = 10
        self.agent.timer_phase = 'running'
        self.agent.require_lid = True
        self.agent.lid_closed = True
        self.agent.suspend_preflight_async = mock.Mock(
            side_effect=lambda callback, **_kwargs: callback(True, '', ':1.login'))

        def release(callback, **_kwargs):
            self.agent.lid_closed = False
            callback(True)

        login = types.SimpleNamespace(Suspend=mock.Mock())
        self.agent.release_async = mock.Mock(side_effect=release)
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertTrue(self.agent.sleep_now('Countdown elapsed'))
        login.Suspend.assert_not_called()
        self.assertEqual(self.agent.suspend_preflight_async.call_count, 1)
        self.assertEqual(self.agent.timer_phase, 'consumed')
        self.assertIn('confirmed closed lid', self.agent.timer_outcome)

    def test_async_unknown_suspend_reply_does_not_dispatch_twice(self):
        self.agent.deadline = 10
        self.agent.timer_phase = 'running'
        self.agent.suspend_preflight_async = mock.Mock(
            side_effect=lambda callback, **_kwargs: callback(True, '', ':1.login'))
        self.agent.release_async = mock.Mock(side_effect=lambda callback, **_kwargs: callback(True))
        login = types.SimpleNamespace(Suspend=mock.Mock(side_effect=FakeDBusException(
            'reply lost', name='org.freedesktop.DBus.Error.NoReply')))
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertTrue(self.agent.sleep_now('Countdown elapsed'))
        self.assertEqual(self.agent.suspend_request_outcome, 'unknown')
        self.assertEqual(self.agent.sleep_tx['request_reply'], 'unknown')
        self.assertFalse(self.agent.sleep_now('Countdown elapsed'))
        login.Suspend.assert_called_once()

    def test_async_unexpected_suspend_setup_error_remains_unknown(self):
        self.agent.deadline = 10
        self.agent.timer_phase = 'running'
        self.agent.suspend_preflight_async = mock.Mock(
            side_effect=lambda callback, **_kwargs: callback(True, '', ':1.login'))
        self.agent.release_async = mock.Mock(side_effect=lambda callback, **_kwargs: callback(True))
        login = types.SimpleNamespace(Suspend=mock.Mock(
            side_effect=TypeError('unexpected marshalling failure')))
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertTrue(self.agent.sleep_now('Countdown elapsed'))
        self.assertEqual(self.agent.suspend_request_outcome, 'unknown')
        self.assertEqual(self.agent.sleep_tx['request_reply'], 'unknown')
        self.assertFalse(self.agent.sleep_now('Countdown elapsed'))
        login.Suspend.assert_called_once()

    def test_async_preflight_requires_noninteractive_login_capability(self):
        for capability in ('yes', 'challenge'):
            with self.subTest(capability=capability):
                names = types.SimpleNamespace(GetNameOwner=mock.Mock(
                    side_effect=lambda _name, **kwargs: kwargs['reply_handler'](':1.login')))
                properties = types.SimpleNamespace(Get=mock.Mock(
                    side_effect=lambda _iface, _key, **kwargs: kwargs['reply_handler'](False)))
                login = types.SimpleNamespace(CanSuspend=mock.Mock(
                    side_effect=lambda **kwargs: kwargs['reply_handler'](capability)))
                interfaces = {'org.freedesktop.DBus': names,
                              'org.freedesktop.DBus.Properties': properties,
                              'org.freedesktop.login1.Manager': login}
                self.agent.proxy = mock.Mock(
                    side_effect=lambda _bus, _owner, _path, interface: interfaces[interface])
                outcomes = []
                self.agent.suspend_preflight_async(
                    lambda allowed, reason, owner: outcomes.append((allowed, reason, owner)),
                    require_cookie_absent=True)
                self.assertEqual(len(outcomes), 1)
                self.assertEqual(outcomes[0][0], capability == 'yes')
                self.assertEqual(outcomes[0][2], ':1.login' if capability == 'yes' else '')
                self.assertEqual(names.GetNameOwner.call_count, 3 if capability == 'yes' else 2)
                properties.Get.assert_called_once()
                login.CanSuspend.assert_called_once()


if __name__ == '__main__':
    unittest.main()
