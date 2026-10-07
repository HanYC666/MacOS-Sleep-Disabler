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

    def output_identity(self):
        return self.identity + ':card0-eDP-1:intel_backlight'

    def write(self, value):
        self.writes.append(value)
        self.value = value


class AgentTests(unittest.TestCase):
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
                self.assertEqual(a.brightness_record['phase'], 'prepared')
                self.assertEqual(json.loads(a.brightness_path.read_text()), a.brightness_record)
                self.assertFalse(a.state()['brightnessDimmed'])
                self.assertTrue(a.state()['brightnessRecoveryPending'])
                self.assertIn('Dim readback differs', a.brightness_error)
                result = a.restore_brightness()
                self.assertEqual(result, 'manual-change-preserved' if actual == 70 else 'pending')
                self.assertEqual(a.brightness_adapter.value, actual)

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
        remote.GetNameOwner.assert_called_once_with(agent_module.SESSION, timeout=3)
        remote.Uninhibit.assert_called_once_with(7, timeout=3)

    def test_dim_readback_exception_retains_prepared_record_without_healthy_claim(self):
        a = self.agent
        a.enabled = a.lid_dimming = a.lid_closed = True
        with mock.patch.object(a.brightness_adapter, 'read',
                               side_effect=[70, FakeDBusException('readback timed out')]):
            self.assertEqual(a.dim_brightness(), 'pending')
        self.assertEqual(a.brightness_record['phase'], 'prepared')
        self.assertEqual(json.loads(a.brightness_path.read_text()), a.brightness_record)
        self.assertFalse(a.state()['brightnessDimmed'])
        self.assertTrue(a.state()['brightnessRecoveryPending'])
        self.assertEqual(a.brightness_adapter.value, 0)
        # A later readable target can be restored safely from the prepared intent.
        self.assertEqual(a.restore_brightness(), 'restored')
        self.assertEqual(a.brightness_adapter.value, 70)

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
                mock.patch.object(agent_module.Agent, 'refresh_power'), \
                mock.patch.object(agent_module.Agent, 'reconcile_brightness'):
            agent_module.Agent()
        self.assertEqual(events[:3], [('session', False), ('system', False), ('name', 'requested')])
        for name in ('session', 'system'):
            self.assertLess(events.index((name, 'Disconnected')), events.index(('settings', 'loaded')))
        session.get_is_connected.assert_has_calls([mock.call(), mock.call()])
        system.get_is_connected.assert_has_calls([mock.call(), mock.call()])

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
        remote.Uninhibit.side_effect = disconnect
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
        remote.Uninhibit.side_effect = lambda _cookie, **_kwargs: events.append('uninhibit')
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
        a.SetLidMode(False)
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
        a.brightness_adapter = FakeBrightness()
        a.brightness_available = True
        a.timer_phase = 'idle'
        a.timer_outcome = ''
        a.deadline = None
        a.require_lid = False
        a.require_prevention = False
        a.last_timer_state = -1
        a.clock_gap = 0
        a.logind_owner_generation = 0
        a.last_prepare_signal = None
        a.sleep_tx = a.idle_sleep_transaction()
        a.sleep_outcome = 'none'
        a.suspend_request_outcome = 'none'
        a.system_bus = object()
        a.session_bus = object()
        a.session_cookie = None
        a.session_cookie_state = 'absent'
        a.session_cookie_owner = ''
        a.session_owner_generation = 0
        a.inhibitor_outcome = 'absent'
        a.release_attempts = 0
        a.release_retry_after = 0
        a.release_retry_source = 0
        a.exit_failure = False
        a.shutting_down = False
        a.panel_runtime_version = ''
        a.panel_runtime_sender = ''
        a.lid_error = ''
        a.last_error = ''
        a.enabled = False
        a.desired = False
        a.lid_mode = False
        a.lid_dimming = False
        a.lid_closed = False
        a.lid_fd = None
        a.fd = None
        a.lid_outcome = 'unverified'
        a.failsafe = False
        a.failsafe_triggered = False
        a.threshold = 20
        a.on_battery = False
        a.battery_discharging = False
        a.battery_discharge_state = 'unknown'
        a.battery_percent = None
        a.recovering = False
        a.last_timer_minutes = 30
        a.last_timer_require_lid = False
        a.last_timer_require_prevention = False
        a.session_manager_owner = mock.Mock(return_value=':1.gnome')
        a.preparing_for_sleep = mock.Mock(return_value=False)
        a.publish = mock.Mock()
        a.notify = mock.Mock()
        a.refresh_power = mock.Mock()
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
            'gap_baseline': baseline, 'uncertain_since': requested,
            'owner_generation': self.agent.logind_owner_generation,
        }
        self.agent.suspend_request_outcome = reply

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
        self.agent.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _interface:
                                     login if name == agent_module.LOGIN else gnome)

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
        self.agent.brightness_adapter.writes.clear()
        self.agent.on_owner_change(agent_module.BRIGHTNESS, '', ':1.power')
        self.assertEqual(self.agent.brightness_adapter.writes, [70, 0])

    def test_service_loss_and_return_closed_lid_ends_dimmed(self):
        self.agent.enabled = True
        self.agent.lid_dimming = True
        self.agent.lid_closed = True
        self.agent.dim_brightness()
        adapter = self.agent.brightness_adapter
        adapter.writes.clear()
        self.agent.on_owner_change(agent_module.BRIGHTNESS, ':1.old', '')
        self.agent.refresh_brightness_capability.side_effect = lambda: (
            setattr(self.agent, 'brightness_adapter', adapter),
            setattr(self.agent, 'brightness_available', True))
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
        self.agent.refresh_brightness_capability.side_effect = lambda: (
            setattr(self.agent, 'brightness_adapter', adapter),
            setattr(self.agent, 'brightness_available', True))
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

    def test_release_failure_schedules_exactly_one_five_second_source(self):
        self.held_cookie()
        gnome = types.SimpleNamespace(
            Uninhibit=mock.Mock(side_effect=FakeDBusException('timeout')))
        self.agent.proxy = mock.Mock(return_value=gnome)
        self.agent.reconcile_brightness = mock.Mock()
        self.assertFalse(self.agent.release())
        self.assertEqual(len(fake_glib.sources), 1)
        self.assertEqual(next(iter(fake_glib.sources.values()))[0], 5)
        self.agent.retry_session_release()
        self.assertEqual(len(fake_glib.sources), 1)

    def test_release_retry_success_cancels_pending_state(self):
        self.held_cookie()
        gnome = types.SimpleNamespace(
            Uninhibit=mock.Mock(side_effect=[FakeDBusException('timeout'), None]))
        self.agent.proxy = mock.Mock(return_value=gnome)
        self.agent.reconcile_brightness = mock.Mock()
        self.agent.release()
        source = self.agent.release_retry_source
        callback = fake_glib.sources[source][1]
        callback()
        self.assertEqual(self.agent.session_cookie_state, 'absent')
        self.assertEqual(self.agent.release_retry_source, 0)
        self.assertEqual(gnome.Uninhibit.call_count, 2)

    def test_release_disconnects_after_three_total_attempts(self):
        self.held_cookie()
        gnome = types.SimpleNamespace(
            Uninhibit=mock.Mock(side_effect=FakeDBusException('timeout')))
        bus = types.SimpleNamespace(close=mock.Mock())
        self.agent.session_bus = bus
        self.agent.proxy = mock.Mock(return_value=gnome)
        self.agent.reconcile_brightness = mock.Mock()
        self.agent.release()
        for _ in range(2):
            source = self.agent.release_retry_source
            fake_glib.sources[source][1]()
        self.assertEqual(gnome.Uninhibit.call_count, 3)
        bus.close.assert_called_once()
        self.assertTrue(self.agent.exit_failure)

    def test_owner_loss_cancels_release_source(self):
        self.held_cookie()
        self.agent.session_cookie_state = 'release-pending'
        self.agent.release_retry_source = fake_glib.timeout_add_seconds(5, lambda: False)
        source = self.agent.release_retry_source
        self.agent.on_owner_change(agent_module.SESSION, ':1.gnome', ':1.new')
        self.assertEqual(self.agent.session_cookie_state, 'absent')
        self.assertIn(source, fake_glib.removed)

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
        self.agent.preparing_for_sleep.return_value = False
        with mock.patch.object(agent_module, 'boottime', return_value=20),                 mock.patch.object(agent_module.time, 'monotonic', return_value=10):
            self.agent.reconcile_sleep_state('test')
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')
        self.assertEqual(self.agent.suspend_request_outcome, 'proven-suspend')
        self.agent.notify.assert_called_once()

    def test_accepted_request_without_signals_settles_without_stale_reason(self):
        self.begin_request()
        with mock.patch.object(agent_module, 'boottime', return_value=6),                 mock.patch.object(agent_module.time, 'monotonic', return_value=6):
            self.agent.reconcile_sleep_state('test')
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'accepted-no-suspend')
        self.assertEqual(self.agent.suspend_request_outcome,
                         'accepted-without-observed-suspend')
        self.assertIn('accepted but no suspend observed', self.agent.timer_outcome)
        self.agent.notify.assert_called_once()

    def test_accepted_request_with_later_clock_gap_proves_suspend(self):
        self.begin_request(reply='accepted')
        with mock.patch.object(agent_module, 'boottime', return_value=20), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=10):
            self.agent.reconcile_sleep_state('tick')
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')
        self.assertEqual(self.agent.suspend_request_outcome, 'proven-suspend')

    def test_accepted_request_with_true_and_missing_false_is_failed_preparation(self):
        self.begin_request(reply='accepted')
        self.agent.sleep_tx['saw_prepare_true'] = True
        self.agent.sleep_tx['phase'] = 'preparing'
        with mock.patch.object(agent_module, 'boottime', return_value=6), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=6):
            self.agent.reconcile_sleep_state('tick')
        self.assertEqual(self.agent.sleep_outcome, 'failed-preparation')
        self.assertEqual(self.agent.suspend_request_outcome,
                         'accepted-without-observed-suspend')
        self.assertIn('accepted but no suspend observed', self.agent.timer_outcome)
        self.agent.notify.assert_called_once()

    def test_no_signals_then_clock_gap_is_proven_suspend(self):
        self.begin_request(reply='unknown')
        self.agent.preparing_for_sleep.return_value = None
        with mock.patch.object(agent_module, 'boottime', return_value=20),                 mock.patch.object(agent_module.time, 'monotonic', return_value=10):
            self.agent.reconcile_sleep_state('tick')
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')

    def test_duplicate_true_preserves_original_baseline(self):
        self.agent.preparing_for_sleep.return_value = True
        with mock.patch.object(agent_module, 'boottime', side_effect=[10, 15]),                 mock.patch.object(agent_module.time, 'monotonic', side_effect=[10, 15]):
            self.agent.observe_prepare_enter()
            baseline = self.agent.sleep_tx['gap_baseline']
            self.agent.observe_prepare_enter()
        self.assertEqual(self.agent.sleep_tx['gap_baseline'], baseline)
        self.assertTrue(self.agent.sleep_tx['saw_prepare_true'])

    def test_false_without_true_settles_conservatively_and_never_suspends(self):
        self.agent.proxy = mock.Mock()
        with mock.patch.object(agent_module, 'boottime', return_value=0),                 mock.patch.object(agent_module.time, 'monotonic', return_value=0):
            self.agent.observe_prepare_exit()
        with mock.patch.object(agent_module, 'boottime', return_value=6),                 mock.patch.object(agent_module.time, 'monotonic', return_value=6):
            self.agent.reconcile_sleep_state('tick')
        self.assertEqual(self.agent.sleep_outcome, 'uncertain-preparation')
        self.agent.proxy.assert_not_called()

    def test_repeated_false_after_resolution_is_idempotent(self):
        with mock.patch.object(agent_module, 'boottime', return_value=0), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=0):
            self.agent.observe_prepare_exit()
        with mock.patch.object(agent_module, 'boottime', return_value=6), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=6):
            self.agent.reconcile_sleep_state('tick')
            self.agent.observe_prepare_exit()
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'uncertain-preparation')

    def test_false_then_delayed_true_converges_through_one_transaction(self):
        self.agent.proxy = mock.Mock()
        with mock.patch.object(agent_module, 'boottime', return_value=0), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=0):
            self.agent.observe_prepare_exit()
        with mock.patch.object(agent_module, 'boottime', return_value=1), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=1):
            self.agent.observe_prepare_enter()
        with mock.patch.object(agent_module, 'boottime', return_value=6), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=6):
            self.agent.reconcile_sleep_state('delayed-enter')
            self.agent.observe_prepare_exit()
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'failed-preparation')
        self.agent.proxy.assert_not_called()

    def test_true_beyond_deadline_keeps_safety_latch_and_cancels_real_timer(self):
        self.agent.deadline = 100
        self.agent.timer_phase = 'running'
        self.agent.preparing_for_sleep.return_value = True
        with mock.patch.object(agent_module, 'boottime', return_value=0),                 mock.patch.object(agent_module.time, 'monotonic', return_value=0):
            self.agent.observe_prepare_enter()
        with mock.patch.object(agent_module, 'boottime', return_value=31),                 mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.agent.reconcile_sleep_state('tick')
        self.assertEqual(self.agent.sleep_tx['phase'], 'uncertain-blocked')
        self.assertIsNone(self.agent.deadline)
        self.assertEqual(self.agent.timer_phase, 'canceled')

    def test_uncertain_without_timer_does_not_invent_canceled_timer(self):
        self.begin_request()
        self.agent.preparing_for_sleep.return_value = None
        with mock.patch.object(agent_module, 'boottime', return_value=31),                 mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.agent.reconcile_sleep_state('tick')
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
        self.assertFalse(self.agent.suspend_preflight()[0])

    def test_uncertain_latch_blocks_periodic_and_upower_failsafe_paths(self):
        self.agent.sleep_tx['phase'] = 'uncertain-blocked'
        self.agent.sleep_tx['uncertain_since'] = 0
        self.agent.preparing_for_sleep.return_value = None
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
        self.agent.preparing_for_sleep.return_value = None
        with mock.patch.object(agent_module, 'boottime', return_value=10), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=10):
            self.agent.reconcile_sleep_state('before-deadline')
        self.assertEqual(self.agent.sleep_tx['phase'], 'request-pending')
        with mock.patch.object(agent_module, 'boottime', return_value=31), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.agent.reconcile_sleep_state('after-deadline')
        self.assertEqual(self.agent.sleep_tx['phase'], 'uncertain-blocked')
        with mock.patch.object(agent_module, 'boottime', return_value=50), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=40):
            self.agent.reconcile_sleep_state('clock-evidence')
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')

    def test_clock_gap_waits_while_logind_still_reports_preparing(self):
        self.begin_request(reply='unknown')
        self.agent.preparing_for_sleep.return_value = True
        with mock.patch.object(agent_module, 'boottime', return_value=40), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.agent.reconcile_sleep_state('clock-gap-while-preparing')
        self.assertEqual(self.agent.sleep_tx['phase'], 'uncertain-blocked')
        self.assertEqual(self.agent.sleep_outcome, 'preparation-state-uncertain')
        self.agent.preparing_for_sleep.return_value = False
        with mock.patch.object(agent_module, 'boottime', return_value=40), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.agent.reconcile_sleep_state('preparation-cleared')
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')

    def test_later_false_resolves_unreadable_latch_after_settle(self):
        self.begin_request(reply='unknown')
        self.agent.sleep_tx['phase'] = 'uncertain-blocked'
        self.agent.preparing_for_sleep.return_value = False
        with mock.patch.object(agent_module, 'boottime', return_value=40),                 mock.patch.object(agent_module.time, 'monotonic', return_value=40):
            self.agent.reconcile_sleep_state('tick')
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
        self.agent.reconcile_sleep_state('generation-check')
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'logind-owner-lost')
        self.agent.preparing_for_sleep.assert_not_called()

    def test_logind_replacement_requires_fresh_readable_preflight(self):
        self.begin_request(reply='unknown')
        self.agent.on_owner_change(agent_module.LOGIN, ':1.old', ':1.new')
        self.agent.preparing_for_sleep.return_value = None
        self.assertFalse(self.agent.suspend_preflight()[0])
        self.agent.preparing_for_sleep.return_value = False
        self.assertTrue(self.agent.suspend_preflight()[0])

    def test_preflight_rejects_cookie_owner_mismatch_before_release(self):
        self.held_cookie()
        self.agent.session_manager_owner.return_value = ':1.other'
        allowed, message = self.agent.suspend_preflight()
        self.assertFalse(allowed)
        self.assertIn('previous', message)

    def test_state_change_between_release_and_final_guard_prevents_suspend(self):
        self.agent.deadline = 10
        self.agent.timer_phase = 'running'
        def release():
            self.agent.sleep_tx['phase'] = 'preparing'
            self.agent.session_cookie_state = 'absent'
            self.agent.session_cookie = None
            return True
        self.agent.release = mock.Mock(side_effect=release)
        login = types.SimpleNamespace(Suspend=mock.Mock())
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertFalse(self.agent.sleep_now('Countdown elapsed'))
        login.Suspend.assert_not_called()
        self.assertEqual(self.agent.timer_phase, 'consumed')

    def test_each_definite_rejection_resolves_immediately(self):
        for name in agent_module.DEFINITE_SUSPEND_REJECTIONS:
            with self.subTest(name=name):
                self.agent.sleep_tx = self.agent.idle_sleep_transaction()
                self.agent.deadline = 10
                self.agent.timer_phase = 'running'
                self.agent.release = mock.Mock(return_value=True)
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
                self.agent.release = mock.Mock(return_value=True)
                login = types.SimpleNamespace(
                    Suspend=mock.Mock(side_effect=FakeDBusException('lost', name=name)))
                self.agent.proxy = mock.Mock(return_value=login)
                self.agent.sleep_now('Battery fell below the failsafe threshold')
                self.assertEqual(self.agent.sleep_tx['request_reply'], 'unknown')
                self.assertEqual(login.Suspend.call_count, 1)
                self.agent.notify.assert_called_once()
                with mock.patch.object(agent_module, 'boottime', return_value=10), \
                        mock.patch.object(agent_module.time, 'monotonic', return_value=10):
                    self.agent.reconcile_sleep_state('tick')
                self.assertEqual(login.Suspend.call_count, 1)
                self.agent.notify.reset_mock()

    def test_low_battery_episode_never_retries_ambiguous_suspend(self):
        self.agent.enabled = True
        self.agent.failsafe = True
        self.agent.on_battery = True
        self.agent.battery_discharge_state = 'discharging'
        self.agent.battery_percent = 5
        self.agent.release = mock.Mock(return_value=True)
        login = types.SimpleNamespace(Suspend=mock.Mock(side_effect=FakeDBusException(
            'lost', name='org.freedesktop.DBus.Error.NoReply')))
        self.agent.proxy = mock.Mock(return_value=login)
        self.agent.check_failsafe = agent_module.Agent.check_failsafe.__get__(self.agent)

        self.agent.check_failsafe()
        with mock.patch.object(agent_module, 'boottime', return_value=6), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=6):
            self.agent.reconcile_sleep_state('settled')
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
            self.agent.reconcile_sleep_state('settled')
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'uncertain-preparation')
        self.assertEqual(self.agent.suspend_request_outcome, 'unknown')
        self.assertIn('request outcome unknown', self.agent.timer_outcome)

    def test_unknown_reply_with_unreadable_property_becomes_safety_blocked(self):
        self.begin_request(reply='unknown')
        self.agent.preparing_for_sleep.return_value = None
        with mock.patch.object(agent_module, 'boottime', return_value=31), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.agent.reconcile_sleep_state('deadline')
        self.assertEqual(self.agent.sleep_tx['phase'], 'uncertain-blocked')
        self.assertEqual(self.agent.suspend_request_outcome, 'unknown')

    def test_timer_and_failsafe_defer_while_preparation_is_unresolved(self):
        self.agent.sleep_tx['phase'] = 'preparing'
        self.agent.sleep_tx['uncertain_since'] = 0
        self.agent.preparing_for_sleep.return_value = True
        self.agent.sleep_now = mock.Mock()
        with mock.patch.object(agent_module, 'boottime', return_value=0), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=0):
            self.agent.StartTimer(60, False, False)
            self.agent.tick()
        self.agent.enabled = True
        self.agent.failsafe = True
        self.agent.on_battery = True
        self.agent.battery_discharge_state = 'discharging'
        self.agent.battery_percent = 5
        self.agent.check_failsafe = agent_module.Agent.check_failsafe.__get__(self.agent)
        self.agent.check_failsafe()
        self.agent.sleep_now.assert_not_called()
        self.assertEqual(self.agent.timer_phase, 'running')

    def test_reentrant_wake_resolution_is_not_resurrected_by_method_reply(self):
        self.configure_low_battery()
        self.agent.release = mock.Mock(return_value=True)
        def suspend(_interactive):
            self.agent.resolve_sleep_transaction('proven-resume', now=10, monotonic_now=0)
        login = types.SimpleNamespace(Suspend=mock.Mock(side_effect=suspend))
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertTrue(self.agent.sleep_now('Battery fell below the failsafe threshold'))
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')
        self.assertEqual(self.agent.suspend_request_outcome, 'proven-suspend')

    def test_reentrant_prepare_enter_is_not_overwritten_by_method_reply(self):
        self.configure_low_battery()
        self.agent.release = mock.Mock(return_value=True)
        login = types.SimpleNamespace(
            Suspend=mock.Mock(side_effect=lambda _interactive:
                              self.agent.observe_prepare_enter()))
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
                a.refresh_power.side_effect = lambda **_kwargs: setattr(a, 'lid_closed', lid)
                a.release = mock.Mock(return_value=True)
                login = types.SimpleNamespace(Suspend=mock.Mock())
                a.proxy = mock.Mock(return_value=login)
                self.assertFalse(a.sleep_now('Countdown elapsed'))
                a.release.assert_not_called()
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
                    a.refresh_power.reset_mock()
                    a.preparing_for_sleep.reset_mock()
                    def release():
                        a.enabled = False
                        if moment == 'release':
                            a.lid_closed = lid
                        return True
                    def preparing():
                        if moment == 'final-preflight' and a.preparing_for_sleep.call_count == 2:
                            a.lid_closed = lid
                        return False
                    a.release = mock.Mock(side_effect=release)
                    a.preparing_for_sleep.side_effect = preparing
                    login = types.SimpleNamespace(Suspend=mock.Mock())
                    a.proxy = mock.Mock(return_value=login)
                    self.assertFalse(a.sleep_now('Countdown elapsed'))
                    a.release.assert_called_once_with()
                    login.Suspend.assert_not_called()
                    self.assertEqual(a.refresh_power.call_args_list,
                                     [mock.call(reconcile_lid=False), mock.call(reconcile_lid=False)])
                    self.assertEqual(a.timer_phase, 'consumed')
                    self.assertFalse(a.desired)
                    self.assertFalse(a.enabled)
                    self.assertIn('confirmed closed lid', a.timer_outcome)

    def test_conditional_timer_prevention_loss_before_release_blocks_dispatch(self):
        a = self.agent
        a.deadline, a.timer_phase = 0, 'running'
        a.require_prevention = True
        a.enabled = a.desired = True
        a.preparing_for_sleep.side_effect = lambda: (setattr(a, 'enabled', False), False)[1]
        a.release = mock.Mock(return_value=True)
        login = types.SimpleNamespace(Suspend=mock.Mock())
        a.proxy = mock.Mock(return_value=login)
        self.assertFalse(a.sleep_now('Countdown elapsed'))
        a.release.assert_not_called()
        login.Suspend.assert_not_called()
        self.assertEqual(a.timer_phase, 'consumed')
        self.assertIn('effective prevention before release', a.timer_outcome)

    def test_conditional_timer_stable_conditions_dispatch_once_after_release(self):
        a = self.agent
        a.deadline, a.timer_phase = 0, 'running'
        a.require_lid = a.require_prevention = True
        a.lid_closed = a.enabled = a.desired = True
        def release():
            a.enabled = False
            return True
        a.release = mock.Mock(side_effect=release)
        login = types.SimpleNamespace(Suspend=mock.Mock())
        a.proxy = mock.Mock(return_value=login)
        self.assertTrue(a.sleep_now('Countdown elapsed'))
        a.tick()
        login.Suspend.assert_called_once_with(False)
        self.assertIsNone(a.deadline)
        self.assertEqual(a.timer_phase, 'consumed')
        self.assertFalse(a.enabled)
        self.assertEqual(a.refresh_power.call_args_list,
                         [mock.call(reconcile_lid=False), mock.call(reconcile_lid=False)])

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
                    self.agent.preparing_for_sleep.side_effect = None
                    self.agent.session_manager_owner.side_effect = None
                    def altered_read():
                        if change == 'shutdown':
                            self.agent.shutting_down = True
                        elif change == 'transaction':
                            self.agent.observe_prepare_enter()
                        elif change == 'generation':
                            self.agent.logind_owner_generation += 1
                        else:
                            self.agent.session_cookie_state = 'release-pending'
                        return False if read == 'property' else ':1.gnome'
                    callback = self.agent.preparing_for_sleep if read == 'property' else self.agent.session_manager_owner
                    callback.side_effect = altered_read
                    self.agent.release = mock.Mock(return_value=True)
                    login = types.SimpleNamespace(Suspend=mock.Mock())
                    self.agent.proxy = mock.Mock(return_value=login)
                    self.assertFalse(self.agent.sleep_now('Countdown elapsed'))
                    self.agent.release.assert_not_called()
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
                def release():
                    self.agent.enabled = False
                    setattr(self.agent, attr, value)
                    return True
                self.agent.release = mock.Mock(side_effect=release)
                login = types.SimpleNamespace(Suspend=mock.Mock())
                self.agent.proxy = mock.Mock(return_value=login)
                self.agent.refresh_power.reset_mock()
                self.assertFalse(self.agent.sleep_now('Battery fell below the failsafe threshold'))
                self.agent.refresh_power.assert_called_once_with(reconcile_lid=False)
                login.Suspend.assert_not_called()
                self.assertTrue(self.agent.failsafe_triggered)
                self.assertFalse(self.agent.desired)
                self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
                self.assertIn('power conditions no longer confirmed', self.agent.last_error)

    def test_failsafe_stable_power_dispatches_once_after_effective_release(self):
        self.configure_low_battery()
        def release():
            self.agent.enabled = False
            return True
        self.agent.release = mock.Mock(side_effect=release)
        login = types.SimpleNamespace(Suspend=mock.Mock())
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertTrue(self.agent.sleep_now('Battery fell below the failsafe threshold'))
        self.assertFalse(self.agent.enabled)
        self.agent.refresh_power.assert_called_once_with(reconcile_lid=False)
        agent_module.Agent.check_failsafe(self.agent)
        login.Suspend.assert_called_once_with(False)
        self.assertTrue(self.agent.failsafe_triggered)

    def test_failsafe_power_change_during_final_logind_read_blocks_dispatch(self):
        self.configure_low_battery()
        self.agent.release = mock.Mock(return_value=True)
        calls = 0
        def preparing():
            nonlocal calls
            calls += 1
            if calls == 2:
                self.agent.on_battery = False
            return False
        self.agent.preparing_for_sleep.side_effect = preparing
        login = types.SimpleNamespace(Suspend=mock.Mock())
        self.agent.proxy = mock.Mock(return_value=login)
        self.assertFalse(self.agent.sleep_now('Battery fell below the failsafe threshold'))
        self.assertEqual(calls, 2)
        login.Suspend.assert_not_called()
        self.assertTrue(self.agent.failsafe_triggered)
        self.assertIn('changed during final preflight', self.agent.last_error)

    def test_failsafe_last_power_refresh_events_precede_final_guard(self):
        for event in ('shutdown', 'preparing', 'unknown-preparing'):
            with self.subTest(event=event):
                self.configure_low_battery()
                self.agent.shutting_down = False
                self.agent.preparing_for_sleep.return_value = False
                self.agent.release = mock.Mock(return_value=True)
                def refresh(**kwargs):
                    self.assertEqual(kwargs, {'reconcile_lid': False})
                    if event == 'shutdown':
                        self.agent.shutting_down = True
                    else:
                        self.agent.preparing_for_sleep.return_value = True if event == 'preparing' else None
                self.agent.refresh_power.side_effect = refresh
                login = types.SimpleNamespace(Suspend=mock.Mock())
                self.agent.proxy = mock.Mock(return_value=login)
                self.assertFalse(self.agent.sleep_now('Battery fell below the failsafe threshold'))
                login.Suspend.assert_not_called()
                self.assertEqual(self.agent.sleep_tx['phase'], 'idle')

    def test_terminal_resolution_ignores_reentrant_prepare_signal(self):
        self.begin_request()
        self.agent.sleep_tx['saw_prepare_true'] = True
        self.agent.refresh_power.side_effect = lambda **_kwargs: self.agent.on_prepare_sleep(False)
        with mock.patch.object(agent_module, 'boottime', return_value=20), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=10):
            self.agent.reconcile_sleep_state('test')
        self.assertEqual(self.agent.sleep_tx['phase'], 'idle')
        self.assertEqual(self.agent.sleep_outcome, 'proven-resume')
        self.agent.notify.assert_called_once()

    def test_cancel_without_timer_is_idempotent(self):
        self.agent.timer_phase = 'idle'
        self.assertFalse(self.agent.cancel_timer())
        self.assertEqual(self.agent.timer_phase, 'idle')
        self.agent.publish.assert_not_called()

    def test_proven_suspend_consumes_overdue_active_timer_without_resuspend(self):
        self.agent.deadline = 5
        self.agent.timer_phase = 'running'
        self.agent.clock_gap = 0
        self.agent.preparing_for_sleep.return_value = False
        self.agent.proxy = mock.Mock()
        with mock.patch.object(agent_module, 'boottime', return_value=20),                 mock.patch.object(agent_module.time, 'monotonic', return_value=10):
            self.agent.tick()
        self.assertIsNone(self.agent.deadline)
        self.assertEqual(self.agent.timer_phase, 'canceled')
        self.assertIn('expired during suspend', self.agent.timer_outcome)
        self.agent.proxy.assert_not_called()

    def test_failsafe_rechecks_after_fresh_power_read(self):
        self.agent.enabled = True
        self.agent.failsafe = True
        self.agent.on_battery = True
        self.agent.battery_discharge_state = 'discharging'
        self.agent.battery_percent = 5
        self.agent.sleep_now = mock.Mock()
        self.agent.refresh_power.side_effect = lambda: setattr(self.agent, 'on_battery', False)
        self.agent.check_failsafe = agent_module.Agent.check_failsafe.__get__(self.agent)
        self.agent.check_failsafe()
        self.agent.sleep_now.assert_not_called()

    def power_devices(self, devices):
        self.agent.property = mock.Mock(side_effect=[True, True, False, 5])
        manager = types.SimpleNamespace(EnumerateDevices=lambda: list(range(len(devices))),
                                       GetDisplayDevice=lambda: '/display')
        def proxy(_bus, _name, path, _interface):
            if path == agent_module.UPOWER_PATH:
                return manager
            result = devices[path]
            def get_all(_interface):
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
                 ([2, FakeDBusException('missing')], 'discharging')]
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
                self.agent.refresh_power = mock.Mock()
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
        self.agent.check_failsafe()
        self.agent.battery_percent = None
        self.agent.check_failsafe()
        self.assertTrue(self.agent.failsafe_triggered)
        self.agent.battery_percent = 5
        self.agent.check_failsafe()
        self.agent.sleep_now.assert_called_once()
        self.agent.failsafe_triggered = False
        self.agent.sleep_now.reset_mock()
        self.agent.refresh_power.side_effect = lambda: setattr(self.agent, 'battery_discharge_state', 'unknown')
        self.agent.check_failsafe()
        self.agent.sleep_now.assert_not_called()

    def test_explicit_failsafe_reconfiguration_rearms_consumed_episode(self):
        self.agent.failsafe_triggered = True
        self.agent.SetFailsafe(True, 27)
        self.assertFalse(self.agent.failsafe_triggered)
        self.assertEqual(self.agent.threshold, 27)
        self.assertTrue(self.agent.failsafe)

    def test_panel_registration_is_versioned_and_sender_owned(self):
        self.assertFalse(self.agent.state()['panelRuntimeRegistered'])
        for version in ('', '0', '1bad', '1.2.3.4', ' 5', '5\n'):
            with self.assertRaises(FakeDBusException):
                self.agent.RegisterPanelRuntime(version, sender=':1.42')
        self.agent.RegisterPanelRuntime('5', sender=':1.42')
        self.assertEqual(self.agent.state()['panelRuntimeVersion'], '5')
        self.agent.UnregisterPanelRuntime('5', sender=':1.43')
        self.agent.UnregisterPanelRuntime('4', sender=':1.42')
        self.assertTrue(self.agent.state()['panelRuntimeRegistered'])
        self.agent.on_panel_owner_change(':1.43', ':1.43', '')
        self.assertTrue(self.agent.state()['panelRuntimeRegistered'])
        self.agent.on_panel_owner_change(':1.42', ':1.42', '')
        self.assertFalse(self.agent.state()['panelRuntimeRegistered'])
        self.agent.RegisterPanelRuntime('5', sender=':1.42')
        self.agent.UnregisterPanelRuntime('5', sender=':1.42')
        self.assertFalse(self.agent.state()['panelRuntimeRegistered'])
        self.assertEqual(self.agent.state()['agentRuntimeVersion'], '0.5.0')
        self.assertEqual(self.agent.state()['apiVersion'], 5)

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
        for fail_phase in ('prepared', 'applied'):
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
            self.assertTrue(self.agent.enabled)
            self.assertTrue(self.agent.shutting_down)
            raise FakeDBusException('remote release timeout')
        self.agent.proxy = mock.Mock(return_value=types.SimpleNamespace(Uninhibit=remote_release))
        self.agent.session_bus = types.SimpleNamespace(close=mock.Mock())
        self.agent.stop()
        self.assertFalse(fake_glib.sources)
        self.agent.stop()
        self.assertEqual(self.agent.brightness_adapter.writes, [0, 70])
        self.assertFalse(self.agent.tick())
        self.assertFalse(self.agent.reconcile())
        with self.assertRaises(FakeDBusException):
            self.agent.StartTimer(60, False, False)

    def test_lid_diagnostic_clear_preserves_unrelated_errors(self):
        self.agent.lid_error = 'Lid lock unavailable: denied'
        self.agent.last_error = self.agent.lid_error
        self.agent.SetLidMode(False)
        self.assertEqual(self.agent.last_error, '')
        self.assertEqual(self.agent.lid_error, '')
        self.agent.last_error = 'Unrelated persistence failure'
        self.agent.set_lid_error('Lid lock unavailable: denied')
        self.agent.SetLidMode(False)
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
        self.assertEqual(self.agent.brightness_state(), 'redim-pending')
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
                    if mode == 'no-adapter':
                        self.assertIsNotNone(self.agent.brightness_record)
                    else:
                        self.assertIsNone(self.agent.brightness_record)
                        self.assertEqual(self.agent.brightness_adapter.value, 35 if mode == 'manual' else 70)
                self.agent.proxy = mock.Mock(return_value=types.SimpleNamespace(Uninhibit=uninhibit))
                self.agent.stop()
                self.assertEqual(len(observed), 1)
                self.agent.stop()

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
                a.session_manager_owner = mock.Mock(return_value=':1.gnome')
                remote = types.SimpleNamespace(Uninhibit=mock.Mock())
                a.proxy = mock.Mock(return_value=remote)
                a.session_bus = types.SimpleNamespace(close=mock.Mock())
                failure = FakeDBusException('timeout')
                with __import__('contextlib').ExitStack() as stack:
                    if stage in ('read', 'readback'):
                        values = [failure, failure] if stage == 'read' else [0, failure, failure]
                        stack.enter_context(mock.patch.object(a.brightness_adapter, 'read', side_effect=values))
                    elif stage == 'write':
                        stack.enter_context(mock.patch.object(a.brightness_adapter, 'write', side_effect=failure))
                    elif stage == 'owner':
                        a.session_manager_owner.side_effect = failure
                    else:
                        remote.Uninhibit.side_effect = failure
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


if __name__ == '__main__':
    unittest.main()
