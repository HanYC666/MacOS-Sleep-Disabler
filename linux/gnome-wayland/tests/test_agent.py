"""State-machine tests that do not need a GNOME session or real D-Bus."""

import importlib.util
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


dbus = types.ModuleType("dbus")
dbus.DBusException = FakeDBusException
dbus.Int32 = int
dbus.UInt32 = int
dbus.UInt64 = int
dbus.Boolean = bool
dbus.Double = float
dbus.String = str
dbus.Dictionary = lambda value, signature=None: value
dbus.service = types.ModuleType("dbus.service")
dbus.service.Object = object
dbus.service.method = lambda *_args, **_kwargs: lambda function: function
dbus.service.signal = lambda *_args, **_kwargs: lambda function: function
dbus.mainloop = types.ModuleType("dbus.mainloop")
dbus.mainloop.glib = types.ModuleType("dbus.mainloop.glib")
gi = types.ModuleType("gi")
gi.repository = types.ModuleType("gi.repository")
gi.repository.GLib = types.SimpleNamespace(idle_add=mock.Mock())
sys.modules.update({
    "dbus": dbus, "dbus.service": dbus.service,
    "dbus.mainloop": dbus.mainloop, "dbus.mainloop.glib": dbus.mainloop.glib,
    "gi": gi, "gi.repository": gi.repository,
})
spec = importlib.util.spec_from_file_location("sleep_disabler_agent", Path(__file__).parents[1] / "agent.py")
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
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        identity_patch = mock.patch.object(agent_module, "machine_identity", return_value="test-machine")
        identity_patch.start()
        self.addCleanup(identity_patch.stop)
        self.agent = agent_module.Agent.__new__(agent_module.Agent)
        self.agent.brightness_path = Path(self.directory.name) / "recovery.json"
        self.agent.settings_path = Path(self.directory.name) / "preferences.json"
        self.agent.brightness_record = None
        self.agent.brightness_error = ""
        self.agent.brightness_journal_state = 'absent'
        self.agent.brightness_retry_after = 0
        self.agent.brightness_adapter = FakeBrightness()
        self.agent.timer_phase = 'idle'
        self.agent.resume_pending_since = None
        self.agent.sleep_started = None
        self.agent.sleep_gap_baseline = None
        self.agent.clock_gap = 0
        self.agent.system_bus = object()
        self.agent.session_bus = object()
        self.agent.session_cookie = None
        self.agent.session_cookie_state = 'absent'
        self.agent.session_cookie_owner = ''
        self.agent.session_owner_generation = 0
        self.agent.inhibitor_outcome = 'absent'
        self.agent.release_attempts = 0
        self.agent.release_retry_after = 0
        self.agent.exit_failure = False
        self.agent.last_error = ''
        self.agent.session_manager_owner = mock.Mock(return_value=':1.gnome')
        self.agent.preparing_for_sleep = mock.Mock(return_value=False)
        self.agent.publish = mock.Mock()

    def test_dim_and_restore_after_restart(self):
        self.agent.dim_brightness()
        self.assertEqual(self.agent.brightness_adapter.value, 0)
        self.assertTrue(self.agent.brightness_path.exists())
        self.agent.brightness_record = None
        self.agent.recover_brightness()
        self.assertEqual(self.agent.brightness_adapter.value, 70)
        self.assertFalse(self.agent.brightness_path.exists())

    def test_legacy_probe_requires_signed_writable_property(self):
        xml = "<node><interface name='org.gnome.SettingsDaemon.Power.Screen'>" \
              "<property name='Brightness' type='i' access='readwrite'/>" \
              "</interface></node>"
        properties = types.SimpleNamespace(Get=mock.Mock(return_value=60))
        introspection = types.SimpleNamespace(Introspect=mock.Mock(return_value=xml))
        self.agent.session_bus = object()
        self.agent.proxy = mock.Mock(side_effect=lambda _bus, name, path, interface:
                                     introspection if interface == "org.freedesktop.DBus.Introspectable"
                                     else properties)
        adapter = agent_module.LegacyBrightness(self.agent)
        with mock.patch.object(adapter, 'output_identity', return_value='test-output'):
            adapter.probe()
        self.assertEqual(adapter.read(), 60)
        self.assertEqual(self.agent.proxy.call_args_list[0].args[1:3],
                         (agent_module.BRIGHTNESS, agent_module.BRIGHTNESS_PATH))
        introspection.Introspect.return_value = xml.replace("readwrite", "read")
        with self.assertRaises(ValueError):
            adapter.probe()

    def test_output_identity_requires_one_connected_internal_panel_and_backlight(self):
        root = Path(self.directory.name)
        (root / 'backlight' / 'intel_backlight').mkdir(parents=True)
        panel = root / 'drm' / 'card0-eDP-1'
        panel.mkdir(parents=True)
        (panel / 'status').write_text('connected')
        adapter = agent_module.LegacyBrightness(self.agent)
        with mock.patch.object(agent_module, 'SYS_CLASS', root):
            self.assertEqual(adapter.output_identity(),
                             FakeBrightness.identity + ':card0-eDP-1:intel_backlight')
            (root / 'backlight' / 'acpi_video0').mkdir()
            with self.assertRaises(ValueError):
                adapter.output_identity()

    def test_manual_change_is_preserved(self):
        self.agent.dim_brightness()
        self.agent.brightness_adapter.value = 35
        self.agent.restore_brightness()
        self.assertEqual(self.agent.brightness_adapter.value, 35)
        self.assertFalse(self.agent.brightness_path.exists())

    def test_manual_change_is_not_redimmed_by_same_recovery_event(self):
        self.agent.dim_brightness()
        self.agent.brightness_adapter.value = 35
        self.agent.brightness_record = None
        self.agent.dim_brightness()
        self.assertEqual(self.agent.brightness_adapter.value, 35)
        self.assertFalse(self.agent.brightness_path.exists())

    def test_foreign_machine_record_is_not_restored(self):
        self.agent.dim_brightness()
        record = agent_module.json.loads(self.agent.brightness_path.read_text())
        record["machine"] = "other-machine"
        agent_module.atomic_json(self.agent.brightness_path, record)
        self.agent.brightness_record = None
        self.agent.recover_brightness()
        self.assertEqual(self.agent.brightness_adapter.value, 0)
        self.assertTrue(self.agent.brightness_path.exists())
        self.assertIn("foreign", self.agent.brightness_error.lower())

    def test_transient_record_read_retries_without_losing_journal(self):
        self.agent.dim_brightness()
        self.agent.brightness_record = None
        original = Path.read_text
        failures = 0

        def intermittent(path, *args, **kwargs):
            nonlocal failures
            if path == self.agent.brightness_path and failures == 0:
                failures += 1
                raise PermissionError('temporarily denied')
            return original(path, *args, **kwargs)

        with mock.patch.object(Path, 'read_text', intermittent):
            self.agent.recover_brightness()
        self.assertEqual(self.agent.brightness_journal_state, 'unreadable')
        self.assertTrue(self.agent.brightness_path.exists())
        self.agent.brightness_retry_after = 0
        self.agent.recover_brightness()
        self.assertEqual(self.agent.brightness_adapter.value, 70)
        self.assertFalse(self.agent.brightness_path.exists())

    def test_machine_identity_failure_retries(self):
        self.agent.dim_brightness()
        self.agent.brightness_record = None
        with mock.patch.object(agent_module, 'machine_identity', side_effect=OSError('offline')):
            self.agent.recover_brightness()
        self.assertEqual(self.agent.brightness_journal_state, 'unreadable')
        self.agent.brightness_retry_after = 0
        self.agent.recover_brightness()
        self.assertEqual(self.agent.brightness_adapter.value, 70)

    def test_output_identity_mismatch_blocks_restore_and_new_dim(self):
        self.agent.dim_brightness()
        self.agent.brightness_adapter.output_identity = lambda: FakeBrightness.identity + ':card1-eDP-1:other'
        self.agent.restore_brightness()
        self.assertEqual(self.agent.brightness_adapter.value, 0)
        self.assertTrue(self.agent.brightness_path.exists())
        self.assertEqual(self.agent.brightness_journal_state, 'invalid')
        self.agent.brightness_record = None
        self.agent.dim_brightness()
        self.assertEqual(self.agent.brightness_adapter.writes, [0])

    def test_invalid_record_is_preserved(self):
        self.agent.brightness_path.write_text('{bad json')
        self.agent.recover_brightness()
        self.assertEqual(self.agent.brightness_journal_state, 'invalid')
        self.assertTrue(self.agent.brightness_path.exists())

    def test_adapter_absence_then_return_restores_loaded_record(self):
        self.agent.dim_brightness()
        self.agent.brightness_record = None
        adapter = self.agent.brightness_adapter
        self.agent.brightness_adapter = None
        self.agent.recover_brightness()
        self.assertEqual(self.agent.brightness_journal_state, 'pending')
        self.agent.brightness_adapter = adapter
        self.agent.restore_brightness()
        self.assertEqual(adapter.value, 70)

    def test_periodic_reconcile_reloads_record_after_transient_failure(self):
        self.agent.dim_brightness()
        self.agent.brightness_record = None
        self.agent.brightness_journal_state = 'unreadable'
        self.agent.brightness_retry_after = 0
        self.agent.refresh_power = mock.Mock()
        self.agent.refresh_brightness_capability = mock.Mock()
        self.agent.retry_lid_lock = mock.Mock()
        self.agent.check_failsafe = mock.Mock()
        self.agent.enabled = False
        self.agent.desired = False
        self.agent.lid_dimming = False
        self.agent.lid_closed = False
        self.agent.reconcile()
        self.assertEqual(self.agent.brightness_adapter.value, 70)
        self.assertFalse(self.agent.brightness_path.exists())

    def test_failed_applied_journal_keeps_prepared_record(self):
        writer = agent_module.atomic_json
        calls = 0

        def fail_second(path, record):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("disk full")
            writer(path, record)

        with mock.patch.object(agent_module, "atomic_json", side_effect=fail_second):
            self.agent.dim_brightness()
        self.assertEqual(self.agent.brightness_adapter.value, 0)
        self.assertTrue(self.agent.brightness_path.exists())
        self.assertEqual(self.agent.brightness_record["phase"], "prepared")
        self.agent.restore_brightness()
        self.assertEqual(self.agent.brightness_adapter.value, 70)

    def test_timer_expired_during_proven_sleep_is_consumed(self):
        self.agent.deadline = 90
        self.agent.require_lid = False
        self.agent.require_prevention = False
        self.agent.timer_outcome = ""
        self.agent.last_sleep_reason = ""
        self.agent.last_timer_state = -1
        self.agent.clock_gap = 0
        self.agent.sleeping = False
        self.agent.resume_pending = False
        self.agent.last_sleep_reason = ''
        self.agent.refresh_power = mock.Mock()
        self.agent.refresh_brightness_capability = mock.Mock()
        self.agent.recover_brightness = mock.Mock()
        self.agent.restore_brightness = mock.Mock()
        self.agent.sleep_now = mock.Mock()
        self.agent.tick = mock.Mock()
        with mock.patch.object(agent_module, "boottime", side_effect=[80, 100]), \
                mock.patch.object(agent_module.time, "monotonic", side_effect=[80, 80]):
            self.agent.on_prepare_sleep(True)
            self.agent.on_prepare_sleep(False)
        self.assertIsNone(self.agent.deadline)
        self.assertEqual(self.agent.timer_outcome,
                         'Countdown expired during suspend; no second suspend requested')
        self.assertEqual(self.agent.timer_phase, 'canceled')
        self.agent.sleep_now.assert_not_called()

    def test_tick_detects_unannounced_resume_before_suspend_request(self):
        self.agent.deadline = 90
        self.agent.last_sleep_reason = ''
        self.agent.require_lid = False
        self.agent.require_prevention = False
        self.agent.clock_gap = 0
        self.agent.sleeping = False
        self.agent.resume_pending = False
        self.agent.refresh_power = mock.Mock()
        self.agent.refresh_brightness_capability = mock.Mock()
        self.agent.recover_brightness = mock.Mock()
        self.agent.restore_brightness = mock.Mock()
        self.agent.sleep_now = mock.Mock()
        with mock.patch.object(agent_module, "boottime", return_value=100), \
                mock.patch.object(agent_module.time, "monotonic", return_value=80):
            self.agent.tick()
        self.assertFalse(self.agent.resume_pending)
        self.assertIsNone(self.agent.deadline)
        self.assertEqual(self.agent.timer_outcome,
                         'Countdown expired during suspend; no second suspend requested')
        self.assertEqual(self.agent.timer_phase, 'canceled')
        self.agent.sleep_now.assert_not_called()

    def timer_fixture(self, deadline=100):
        self.agent.deadline = deadline
        self.agent.require_lid = False
        self.agent.require_prevention = False
        self.agent.last_timer_state = -1
        self.agent.timer_outcome = ''
        self.agent.last_sleep_reason = ''
        self.agent.sleeping = False
        self.agent.resume_pending = False
        self.agent.clock_gap = 0
        self.agent.refresh_power = mock.Mock()
        self.agent.refresh_brightness_capability = mock.Mock()
        self.agent.recover_brightness = mock.Mock()
        self.agent.restore_brightness = mock.Mock()
        self.agent.sleep_now = mock.Mock()
        self.agent.TimerChanged = mock.Mock()

    def test_awake_deadline_requests_suspend_once(self):
        self.timer_fixture()
        with mock.patch.object(agent_module, 'boottime', return_value=100), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=100):
            self.agent.tick()
        self.agent.sleep_now.assert_called_once_with('Countdown elapsed')

    def test_sub_tolerance_clock_drift_does_not_claim_suspend(self):
        self.timer_fixture()
        with mock.patch.object(agent_module, 'boottime', return_value=100.2), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=100):
            self.agent.tick()
        self.assertEqual(self.agent.deadline, 100)
        self.assertFalse(self.agent.resume_pending)
        self.agent.sleep_now.assert_called_once_with('Countdown elapsed')

    def test_missing_resume_signal_uses_logind_state(self):
        self.timer_fixture(deadline=200)
        with mock.patch.object(agent_module, 'boottime', return_value=90):
            self.agent.on_prepare_sleep(True)
        self.agent.preparing_for_sleep.return_value = False
        with mock.patch.object(agent_module, 'boottime', return_value=120), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=100):
            self.agent.tick()
        self.assertFalse(self.agent.resume_pending)
        self.assertFalse(self.agent.sleeping)
        self.assertEqual(self.agent.deadline, 200)
        self.assertEqual(self.agent.timer_outcome, '')
        self.assertEqual(self.agent.timer_phase, 'running')
        self.agent.sleep_now.assert_not_called()

    def test_duplicate_enter_signal_does_not_restart_sleep_baseline(self):
        self.timer_fixture(deadline=200)
        with mock.patch.object(agent_module, 'boottime', side_effect=[90, 95]):
            self.agent.on_prepare_sleep(True)
            self.agent.on_prepare_sleep(True)
        self.assertEqual(self.agent.sleep_started, 90)
        self.assertEqual(self.agent.timer_phase, 'entering-sleep')

    def test_resume_signal_without_enter_signal_never_resuspends(self):
        self.timer_fixture(deadline=100)
        with mock.patch.object(agent_module, 'boottime', return_value=110), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=100):
            self.agent.on_prepare_sleep(False)
        self.assertIsNone(self.agent.deadline)
        self.agent.sleep_now.assert_not_called()

    def test_long_unannounced_sleep_preserves_unexpired_timer(self):
        self.timer_fixture(deadline=200)
        with mock.patch.object(agent_module, 'boottime', return_value=120), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=20):
            self.agent.tick()
        self.assertEqual(self.agent.deadline, 200)
        self.assertFalse(self.agent.resume_pending)
        self.assertEqual(self.agent.timer_outcome, '')
        self.assertEqual(self.agent.timer_phase, 'running')
        self.agent.sleep_now.assert_not_called()

    def test_unknown_logind_state_with_clock_evidence_is_suspend(self):
        self.timer_fixture()
        self.agent.preparing_for_sleep.return_value = None
        with mock.patch.object(agent_module, 'boottime', return_value=120), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=100):
            self.agent.tick()
        self.assertIsNone(self.agent.deadline)
        self.assertEqual(self.agent.timer_outcome,
                         'Countdown expired during suspend; no second suspend requested')
        self.assertEqual(self.agent.timer_phase, 'canceled')
        self.agent.sleep_now.assert_not_called()

    def test_condition_loss_cancels_before_deadline(self):
        self.timer_fixture()
        self.agent.require_lid = True
        self.agent.lid_closed = False
        with mock.patch.object(agent_module, 'boottime', return_value=90), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=90):
            self.agent.tick()
        self.assertIsNone(self.agent.deadline)
        self.assertEqual(self.agent.timer_outcome,
                         'Timer canceled because the lid is not closed')
        self.assertEqual(self.agent.timer_phase, 'canceled')
        self.agent.sleep_now.assert_not_called()

    def test_persistence_failure_does_not_change_preferences(self):
        self.agent.threshold = 20
        self.agent.failsafe = False
        self.agent.lid_mode = False
        self.agent.lid_dimming = False
        self.agent.last_timer_minutes = 30
        self.agent.last_timer_require_lid = False
        self.agent.last_timer_require_prevention = False
        with mock.patch.object(agent_module, "atomic_json", side_effect=OSError("read only")):
            with self.assertRaises(FakeDBusException):
                self.agent.persist_settings(threshold=10)
        self.assertEqual(self.agent.threshold, 20)

    def test_failed_suspend_consumes_timer_and_prevention(self):
        self.agent.deadline = 90
        self.agent.require_lid = True
        self.agent.require_prevention = True
        self.agent.desired = True
        self.agent.last_error = ""
        self.agent.last_sleep_reason = ""
        self.agent.system_bus = object()
        self.agent.release = mock.Mock()
        self.agent.notify = mock.Mock()
        self.agent.proxy = mock.Mock(return_value=types.SimpleNamespace(
            Suspend=mock.Mock(side_effect=FakeDBusException("denied"))))
        self.agent.sleep_now("Countdown elapsed")
        self.assertIsNone(self.agent.deadline)
        self.assertFalse(self.agent.desired)
        self.assertFalse(self.agent.require_prevention)
        self.assertIn("denied", self.agent.last_error)
        self.agent.release.assert_called_once()

    def test_session_owner_replacement_marks_required_pair_down(self):
        self.agent.enabled = True
        self.agent.desired = True
        self.agent.session_cookie = 42
        self.agent.session_cookie_state = 'held'
        self.agent.session_cookie_owner = ':1.2'
        self.agent.last_error = ""
        def release():
            self.agent.enabled = False
            self.agent.clear_session_cookie('owner-lost')
        self.agent.release = mock.Mock(side_effect=release)
        self.agent.reconcile_once = mock.Mock()
        with mock.patch.object(agent_module.GLib, "idle_add") as idle_add:
            self.agent.on_owner_change(agent_module.SESSION, ":1.2", ":1.3")
        self.assertIsNone(self.agent.session_cookie)
        self.assertFalse(self.agent.enabled)
        self.agent.release.assert_called_once()
        idle_add.assert_called_once_with(self.agent.reconcile_once)

    def test_lid_toggle_keeps_required_inhibitors(self):
        self.agent.enabled = True
        self.agent.lid_mode = False
        self.agent.lid_fd = None
        self.agent.lid_closed = False
        self.agent.last_error = ""
        self.agent.release = mock.Mock()
        self.agent.acquire = mock.Mock()
        self.agent.acquire_lid = mock.Mock(side_effect=lambda: setattr(self.agent, "lid_fd", 8))
        self.agent.persist_settings = mock.Mock(side_effect=lambda **kw: setattr(self.agent, "lid_mode", kw["lid_mode"]))
        self.agent.SetLidMode(True)
        self.assertTrue(self.agent.lid_mode)
        self.agent.release.assert_not_called()
        self.agent.acquire.assert_not_called()

    def test_optional_lid_fd_take_error_keeps_required_pair(self):
        self.agent.fd = None
        self.agent.lid_fd = None
        self.agent.session_cookie = None
        self.agent.enabled = False
        self.agent.lid_mode = True
        self.agent.last_error = ''
        self.agent.handle_lid_change = mock.Mock()
        sleep_fd = types.SimpleNamespace(take=mock.Mock(return_value=11))
        lid_fd = types.SimpleNamespace(take=mock.Mock(side_effect=OSError('fd transfer failed')))
        login = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=[sleep_fd, lid_fd, lid_fd]))
        gnome = types.SimpleNamespace(Inhibit=mock.Mock(return_value=7))
        self.agent.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _iface:
                                     login if name == agent_module.LOGIN else gnome)
        self.agent.session_bus = object()
        self.agent.acquire()
        self.assertTrue(self.agent.enabled)
        self.assertEqual(self.agent.fd, 11)
        self.assertEqual(self.agent.session_cookie, 7)
        self.assertIsNone(self.agent.lid_fd)
        self.assertEqual(self.agent.lid_outcome, 'failed')
        self.assertIn('fd transfer failed', self.agent.last_error)
        login.Inhibit.assert_called_with('handle-lid-switch', 'Sleep Disabler',
                                         'Test lid stay-awake', 'block')
        self.agent.retry_lid_lock()
        self.assertEqual(login.Inhibit.call_count, 3)

    def test_unknown_gnome_acquire_outcome_disconnects_before_any_retry(self):
        self.agent.fd = None
        self.agent.lid_fd = None
        self.agent.enabled = False
        self.agent.desired = True
        # A prior cookie outcome must not influence this call's unknown result.
        self.agent.inhibitor_outcome = 'released'
        self.agent.lid_mode = False
        self.agent.restore_brightness = mock.Mock()
        self.agent.handle_lid_change = mock.Mock()
        self.agent.loop = types.SimpleNamespace(quit=mock.Mock())
        bus = types.SimpleNamespace(close=mock.Mock())
        self.agent.session_bus = bus
        login = types.SimpleNamespace(Inhibit=mock.Mock(return_value=types.SimpleNamespace(
            take=mock.Mock(return_value=11))))
        gnome = types.SimpleNamespace(Inhibit=mock.Mock(side_effect=FakeDBusException('lost reply')))
        self.agent.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _iface:
                                     login if name == agent_module.LOGIN else gnome)
        with mock.patch.object(agent_module.os, 'close'):
            with self.assertRaises(FakeDBusException):
                self.agent.acquire()
        bus.close.assert_called_once()
        self.agent.loop.quit.assert_called_once()
        self.assertFalse(self.agent.desired)
        self.assertTrue(self.agent.exit_failure)
        gnome.Inhibit.assert_called_once()

    def test_owner_change_during_acquire_disconnects_unknown_cookie_owner(self):
        self.agent.fd = None
        self.agent.lid_fd = None
        self.agent.enabled = False
        self.agent.desired = True
        self.agent.lid_mode = False
        self.agent.restore_brightness = mock.Mock()
        self.agent.handle_lid_change = mock.Mock()
        self.agent.loop = types.SimpleNamespace(quit=mock.Mock())
        bus = types.SimpleNamespace(close=mock.Mock())
        self.agent.session_bus = bus
        self.agent.session_manager_owner = mock.Mock(
            side_effect=[':1.old', ':1.new', ':1.new'])
        login = types.SimpleNamespace(Inhibit=mock.Mock(return_value=types.SimpleNamespace(
            take=mock.Mock(return_value=11))))
        gnome = types.SimpleNamespace(Inhibit=mock.Mock(return_value=7), Uninhibit=mock.Mock())
        self.agent.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _iface:
                                     login if name == agent_module.LOGIN else gnome)
        with mock.patch.object(agent_module.os, 'close'):
            with self.assertRaises(FakeDBusException):
                self.agent.acquire()
        gnome.Uninhibit.assert_not_called()
        bus.close.assert_called_once()
        self.assertFalse(self.agent.desired)
        self.assertTrue(self.agent.exit_failure)

    def test_optional_lid_retry_does_not_reacquire_required_pair(self):
        self.agent.enabled = True
        self.agent.lid_mode = True
        self.agent.lid_fd = None
        self.agent.fd = 11
        self.agent.session_cookie = 7
        self.agent.session_cookie_state = 'held'
        self.agent.session_cookie_owner = ':1.gnome'
        self.agent.last_error = 'Lid lock unavailable: first failure'
        self.agent.acquire_lid = mock.Mock(side_effect=lambda: setattr(self.agent, 'lid_fd', 12))
        self.agent.retry_lid_lock()
        self.assertEqual(self.agent.fd, 11)
        self.assertEqual(self.agent.session_cookie, 7)
        self.assertEqual(self.agent.lid_fd, 12)
        self.assertEqual(self.agent.last_error, '')

    def test_optional_lid_dbus_refusal_is_reported_without_releasing_pair(self):
        self.agent.enabled = True
        self.agent.lid_mode = True
        self.agent.lid_fd = None
        self.agent.fd = 11
        self.agent.session_cookie = 7
        self.agent.last_error = ''
        self.agent.acquire_lid = mock.Mock(side_effect=FakeDBusException('not authorized'))
        self.agent.retry_lid_lock()
        self.assertTrue(self.agent.enabled)
        self.assertEqual((self.agent.fd, self.agent.session_cookie), (11, 7))
        self.assertEqual(self.agent.lid_outcome, 'failed')
        self.assertIn('not authorized', self.agent.last_error)

    def test_lid_preference_failure_rolls_back_optional_fd(self):
        self.agent.enabled = True
        self.agent.lid_mode = False
        self.agent.lid_fd = None
        self.agent.lid_closed = False
        self.agent.acquire_lid = mock.Mock(side_effect=lambda: setattr(self.agent, 'lid_fd', 12))
        self.agent.close_lid = mock.Mock(side_effect=lambda: setattr(self.agent, 'lid_fd', None))
        self.agent.persist_settings = mock.Mock(side_effect=FakeDBusException('read only'))
        with self.assertRaises(FakeDBusException):
            self.agent.SetLidMode(True)
        self.assertFalse(self.agent.lid_mode)
        self.assertIsNone(self.agent.lid_fd)
        self.agent.close_lid.assert_called_once()

    def test_dimming_can_be_disabled_when_api_is_unavailable(self):
        self.agent.lid_dimming = True
        self.agent.brightness_available = False
        self.agent.brightness_record = None
        self.agent.brightness_journal_state = 'unreadable'
        self.agent.persist_settings = mock.Mock(side_effect=lambda **kw:
            setattr(self.agent, 'lid_dimming', kw['lid_dimming']))
        self.agent.restore_brightness = mock.Mock()
        self.agent.SetLidDimming(False)
        self.assertFalse(self.agent.lid_dimming)
        self.agent.restore_brightness.assert_called_once()

    def test_login_owner_replacement_marks_required_pair_down(self):
        self.agent.enabled = True
        self.agent.desired = True
        self.agent.lid_mode = True
        self.agent.session_cookie = 42
        self.agent.lid_fd = 8
        self.agent.release = mock.Mock(side_effect=lambda: setattr(self.agent, 'enabled', False))
        self.agent.reconcile_once = mock.Mock()
        with mock.patch.object(agent_module.GLib, 'idle_add') as idle_add:
            self.agent.on_owner_change(agent_module.LOGIN, ':1.2', ':1.3')
        self.assertFalse(self.agent.enabled)
        self.assertEqual(self.agent.lid_outcome, 'failed')
        self.agent.release.assert_called_once()
        idle_add.assert_called_once_with(self.agent.reconcile_once)

    def test_release_restores_brightness_and_closes_both_fds(self):
        self.agent.fd = 11
        self.agent.lid_fd = 12
        self.agent.session_cookie = 7
        self.agent.session_cookie_state = 'held'
        self.agent.session_cookie_owner = ':1.gnome'
        self.agent.enabled = True
        self.agent.session_bus = object()
        self.agent.restore_brightness = mock.Mock()
        gnome = types.SimpleNamespace(Uninhibit=mock.Mock())
        self.agent.proxy = mock.Mock(return_value=gnome)
        with mock.patch.object(agent_module.os, 'close') as close:
            self.agent.release()
        self.agent.restore_brightness.assert_called_once()
        self.assertEqual({call.args[0] for call in close.call_args_list}, {11, 12})
        gnome.Uninhibit.assert_called_once_with(7)
        self.assertFalse(self.agent.enabled)

    def cookie_fixture(self, uninhibit):
        self.agent.fd = 11
        self.agent.lid_fd = None
        self.agent.session_cookie = 7
        self.agent.session_cookie_state = 'held'
        self.agent.session_cookie_owner = ':1.gnome'
        self.agent.enabled = True
        self.agent.desired = False
        self.agent.last_error = ''
        self.agent.restore_brightness = mock.Mock()
        gnome = types.SimpleNamespace(Uninhibit=uninhibit)
        self.agent.proxy = mock.Mock(return_value=gnome)
        return gnome

    def test_failed_release_is_visible_and_retains_owned_cookie(self):
        gnome = self.cookie_fixture(mock.Mock(side_effect=FakeDBusException('timeout')))
        with mock.patch.object(agent_module.os, 'close'):
            released = self.agent.release()
        self.assertFalse(released)
        self.assertFalse(self.agent.enabled)
        self.assertEqual(self.agent.session_cookie, 7)
        self.assertEqual(self.agent.session_cookie_state, 'release-pending')
        self.assertEqual(self.agent.inhibitor_outcome, 'release-uncertain')
        self.assertIn('timeout', self.agent.last_error)
        gnome.Uninhibit.assert_called_once_with(7)

    def test_pending_release_retries_same_owner_and_resolves(self):
        uninhibit = mock.Mock(side_effect=[FakeDBusException('lost reply'), None])
        self.cookie_fixture(uninhibit)
        with mock.patch.object(agent_module.os, 'close'):
            self.agent.release()
        self.agent.release_retry_after = 0
        self.agent.retry_session_release()
        self.assertIsNone(self.agent.session_cookie)
        self.assertEqual(self.agent.session_cookie_state, 'absent')
        self.assertEqual(self.agent.inhibitor_outcome, 'released')
        self.assertEqual(uninhibit.call_count, 2)
        self.assertEqual(self.agent.last_error, '')

    def test_pending_release_blocks_duplicate_acquisition(self):
        self.agent.session_cookie = 7
        self.agent.session_cookie_state = 'release-pending'
        self.agent.session_cookie_owner = ':1.gnome'
        self.agent.last_error = ''
        self.agent.proxy = mock.Mock()
        with self.assertRaises(FakeDBusException):
            self.agent.SetPrevention(True)
        self.assertFalse(getattr(self.agent, 'desired', False))
        self.agent.proxy.assert_not_called()

    def test_off_reports_pending_release_after_accepting_requested_state(self):
        self.agent.desired = True
        self.agent.deadline = None
        self.agent.release = mock.Mock(return_value=False)
        self.agent.last_error = 'GNOME inhibitor release uncertain: timeout'
        with self.assertRaises(FakeDBusException):
            self.agent.SetPrevention(False)
        self.assertFalse(self.agent.desired)
        self.agent.release.assert_called_once()

    def test_owner_replacement_invalidates_old_cookie_without_reusing_it(self):
        self.agent.session_cookie = 7
        self.agent.session_cookie_state = 'release-pending'
        self.agent.session_cookie_owner = ':1.old'
        self.agent.enabled = False
        self.agent.desired = False
        self.agent.last_error = 'GNOME inhibitor release uncertain: timeout'
        self.agent.proxy = mock.Mock()
        self.agent.on_owner_change(agent_module.SESSION, ':1.old', ':1.new')
        self.assertIsNone(self.agent.session_cookie)
        self.assertEqual(self.agent.inhibitor_outcome, 'owner-lost')
        self.agent.proxy.assert_not_called()
        self.agent.fd = None
        self.agent.lid_fd = None
        self.agent.lid_mode = False
        self.agent.handle_lid_change = mock.Mock()
        self.agent.session_manager_owner.return_value = ':1.new'
        login = types.SimpleNamespace(Inhibit=mock.Mock(return_value=types.SimpleNamespace(
            take=mock.Mock(return_value=11))))
        gnome = types.SimpleNamespace(Inhibit=mock.Mock(return_value=7), Uninhibit=mock.Mock())
        self.agent.proxy = mock.Mock(side_effect=lambda _bus, name, _path, _iface:
                                     login if name == agent_module.LOGIN else gnome)
        self.agent.acquire()
        self.assertEqual(self.agent.session_cookie, 7)
        self.assertEqual(self.agent.session_cookie_owner, ':1.new')
        gnome.Uninhibit.assert_not_called()

    def test_repeated_release_failure_closes_bus_and_requests_restart(self):
        self.cookie_fixture(mock.Mock(side_effect=FakeDBusException('timeout')))
        bus = types.SimpleNamespace(close=mock.Mock())
        self.agent.session_bus = bus
        self.agent.session_manager_owner = mock.Mock(return_value=':1.gnome')
        self.agent.loop = types.SimpleNamespace(quit=mock.Mock())
        with mock.patch.object(agent_module.os, 'close'):
            self.agent.release()
        for _ in range(agent_module.RELEASE_RETRY_LIMIT - 1):
            self.agent.release_retry_after = 0
            self.agent.retry_session_release()
        bus.close.assert_called_once()
        self.agent.loop.quit.assert_called_once()
        self.assertTrue(self.agent.exit_failure)
        self.assertEqual(self.agent.inhibitor_outcome, 'disconnect-completed')

    def test_shutdown_release_failure_disconnects_without_waiting_for_retry(self):
        self.cookie_fixture(mock.Mock(side_effect=FakeDBusException('timeout')))
        bus = types.SimpleNamespace(close=mock.Mock())
        self.agent.session_bus = bus
        self.agent.session_manager_owner = mock.Mock(return_value=':1.gnome')
        self.agent.loop = types.SimpleNamespace(quit=mock.Mock())
        with mock.patch.object(agent_module.os, 'close'):
            released = self.agent.release(force_disconnect=True)
        self.assertFalse(released)
        bus.close.assert_called_once()
        self.assertEqual(self.agent.inhibitor_outcome, 'disconnect-completed')

    def test_sleep_is_not_requested_while_release_is_uncertain(self):
        self.cookie_fixture(mock.Mock(side_effect=FakeDBusException('timeout')))
        self.agent.deadline = 100
        self.agent.require_lid = False
        self.agent.require_prevention = False
        self.agent.timer_outcome = ''
        self.agent.last_sleep_reason = ''
        self.agent.notify = mock.Mock()
        login = types.SimpleNamespace(Suspend=mock.Mock())
        original_proxy = self.agent.proxy
        self.agent.proxy = mock.Mock(side_effect=lambda bus, name, path, iface:
            login if name == agent_module.LOGIN else original_proxy(bus, name, path, iface))
        with mock.patch.object(agent_module.os, 'close'):
            self.agent.sleep_now('Countdown elapsed')
        login.Suspend.assert_not_called()
        self.assertIsNone(self.agent.deadline)
        self.assertIn('not requested', self.agent.timer_outcome)

    def test_restore_rereads_machine_identity_before_write(self):
        self.agent.dim_brightness()
        with mock.patch.object(agent_module, 'machine_identity', return_value='other-machine'):
            self.agent.restore_brightness()
        self.assertEqual(self.agent.brightness_adapter.writes, [0])
        self.assertEqual(self.agent.brightness_adapter.value, 0)
        self.assertTrue(self.agent.brightness_path.exists())
        self.assertEqual(self.agent.brightness_journal_state, 'invalid')

    def test_restore_retries_after_machine_identity_is_temporarily_unreadable(self):
        self.agent.dim_brightness()
        with mock.patch.object(agent_module, 'machine_identity', side_effect=OSError('offline')):
            self.agent.restore_brightness()
        self.assertEqual(self.agent.brightness_adapter.writes, [0])
        self.assertTrue(self.agent.brightness_path.exists())
        self.assertEqual(self.agent.brightness_journal_state, 'unreadable')
        self.agent.restore_brightness()
        self.assertEqual(self.agent.brightness_adapter.writes, [0, 70])
        self.assertFalse(self.agent.brightness_path.exists())

    def test_restore_rejects_record_replaced_after_load(self):
        self.agent.dim_brightness()
        changed = agent_module.json.loads(self.agent.brightness_path.read_text())
        changed['before'] = 65
        agent_module.atomic_json(self.agent.brightness_path, changed)
        self.agent.restore_brightness()
        self.assertEqual(self.agent.brightness_adapter.writes, [0])
        self.assertTrue(self.agent.brightness_path.exists())
        self.assertIn('changed on disk', self.agent.brightness_error)

    def test_failed_preparation_does_not_claim_suspend(self):
        self.timer_fixture(deadline=100)
        self.agent.tick = mock.Mock()
        with mock.patch.object(agent_module, 'boottime', side_effect=[90, 110]), \
                mock.patch.object(agent_module.time, 'monotonic', side_effect=[90, 110]):
            self.agent.on_prepare_sleep(True)
            self.agent.on_prepare_sleep(False)
        self.assertIsNone(self.agent.deadline)
        self.assertEqual(self.agent.timer_phase, 'canceled')
        self.assertEqual(self.agent.timer_outcome,
                         'Countdown expired during failed sleep preparation; no suspend requested')
        self.agent.sleep_now.assert_not_called()

    def test_false_without_true_is_uncertain_and_never_resuspends(self):
        self.timer_fixture(deadline=100)
        with mock.patch.object(agent_module, 'boottime', return_value=110), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=110):
            self.agent.on_prepare_sleep(False)
        self.assertIsNone(self.agent.deadline)
        self.assertEqual(self.agent.timer_outcome,
                         'Countdown canceled after uncertain wake; no suspend requested')
        self.assertEqual(self.agent.timer_phase, 'canceled')
        self.agent.sleep_now.assert_not_called()

    def test_unexpired_timer_is_canceled_when_condition_is_lost_during_preparation(self):
        self.timer_fixture(deadline=200)
        self.agent.require_lid = True
        self.agent.lid_closed = False
        self.agent.tick = mock.Mock()
        with mock.patch.object(agent_module, 'boottime', side_effect=[90, 110]), \
                mock.patch.object(agent_module.time, 'monotonic', side_effect=[90, 110]):
            self.agent.on_prepare_sleep(True)
            self.agent.on_prepare_sleep(False)
        self.assertIsNone(self.agent.deadline)
        self.assertEqual(self.agent.timer_outcome,
                         'Timer canceled because the lid is not closed after sleep preparation')
        self.assertEqual(self.agent.timer_phase, 'canceled')
        self.agent.sleep_now.assert_not_called()

    def test_failed_suspend_outcome_survives_later_false_signal(self):
        self.timer_fixture(deadline=100)
        self.agent.sleep_now = types.MethodType(agent_module.Agent.sleep_now, self.agent)
        self.agent.release = mock.Mock(return_value=True)
        self.agent.notify = mock.Mock()
        self.agent.proxy = mock.Mock(return_value=types.SimpleNamespace(
            Suspend=mock.Mock(side_effect=FakeDBusException('denied'))))
        self.agent.sleep_now('Countdown elapsed')
        self.assertEqual(self.agent.timer_outcome, 'Countdown suspend refused')
        with mock.patch.object(agent_module, 'boottime', return_value=110), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=110):
            self.agent.on_prepare_sleep(False)
        self.assertEqual(self.agent.timer_outcome, 'Countdown suspend refused')
        self.assertIsNone(self.agent.deadline)

    def test_bounded_uncertain_sleep_state_resets_all_flags(self):
        self.timer_fixture(deadline=200)
        self.agent.preparing_for_sleep.return_value = True
        self.agent.sleeping = True
        self.agent.resume_pending = True
        self.agent.resume_pending_since = 0
        self.agent.sleep_started = 1
        self.agent.sleep_gap_baseline = 0
        with mock.patch.object(agent_module, 'boottime', return_value=100), \
                mock.patch.object(agent_module.time, 'monotonic', return_value=31):
            self.agent.tick()
        self.assertIsNone(self.agent.deadline)
        self.assertFalse(self.agent.sleeping)
        self.assertFalse(self.agent.resume_pending)
        self.assertIsNone(self.agent.sleep_started)
        self.assertIsNone(self.agent.sleep_gap_baseline)
        self.assertEqual(self.agent.timer_outcome,
                         'Countdown canceled because wake state remained uncertain')
        self.assertEqual(self.agent.timer_phase, 'canceled')


if __name__ == "__main__":
    unittest.main()
