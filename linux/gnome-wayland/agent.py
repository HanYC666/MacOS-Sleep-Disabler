#!/usr/bin/python3
"""Per-user GNOME Wayland prototype. No privileged helper or global setting edits."""

import json
import logging
import math
import os
from dataclasses import dataclass
from pathlib import Path
import re
import signal
import time
import xml.etree.ElementTree as ET

import dbus
import dbus.mainloop.glib
import dbus.service
from gi.repository import GLib


APP = "org.sleepdisabler.App"
PATH = "/org/sleepdisabler/App"
IFACE = "org.sleepdisabler.App1"
LOGIN = "org.freedesktop.login1"
LOGIN_PATH = "/org/freedesktop/login1"
SESSION = "org.gnome.SessionManager"
SESSION_PATH = "/org/gnome/SessionManager"
UPOWER = "org.freedesktop.UPower"
UPOWER_PATH = "/org/freedesktop/UPower"
BRIGHTNESS = "org.gnome.SettingsDaemon.Power"
BRIGHTNESS_PATH = "/org/gnome/SettingsDaemon/Power"
BRIGHTNESS_IFACE = BRIGHTNESS + ".Screen"
SHELL_BRIGHTNESS = "org.gnome.Shell.Brightness"
SHELL = "org.gnome.Shell"
SYS_CLASS = Path('/sys/class')
MAX_SECONDS = 365 * 86400 + 23 * 3600 + 59 * 60
SUSPEND_GAP_TOLERANCE = 0.5
CLOCK_SAMPLE_WIDTH_LIMIT = 0.05
RELEASE_RETRY_SECONDS = 5
RELEASE_RETRY_LIMIT = 3
RELEASE_OPERATION_TIMEOUT = 7
RECOVERY_CALL_TIMEOUT = 3
BRIGHTNESS_PROBE_TIMEOUT = 12
BRIGHTNESS_DIM_TIMEOUT = 8
BRIGHTNESS_RESTORE_TIMEOUT = 9
POWER_REFRESH_TIMEOUT = 6
ACQUISITION_TIMEOUT = 9
PUBLIC_MUTATION_TIMEOUT = 20
MAX_PUBLIC_WAITERS = 64
STOP_REMOTE_TIMEOUT = 24
SUSPEND_ATTEMPT_TIMEOUT = 50
SLEEP_RECONCILE_TIMEOUT = 9
POWER_DEVICE_CONCURRENCY = 4
NOTIFICATION_CALL_TIMEOUT = 2
SLEEP_SETTLE_SECONDS = 5
SLEEP_UNCERTAIN_SECONDS = 30
DEFINITE_SUSPEND_REJECTIONS = frozenset((
    'org.freedesktop.DBus.Error.AccessDenied',
    'org.freedesktop.PolicyKit1.Error.NotAuthorized',
    'org.freedesktop.login1.SleepVerbNotSupported',
    'org.freedesktop.login1.OperationInProgress',
))
LOG = logging.getLogger("sleep-disabler")
AGENT_RUNTIME_VERSION = "0.6.0"
API_VERSION = 6
PANEL_VERSION_RE = re.compile(r"^[1-9][0-9]*(?:\.[0-9]+){0,2}$")


class AtomicWriteError(OSError):
    """Atomic-write failure with visibility and durability information."""

    def __init__(self, message, *, committed=False, durable=False):
        super().__init__(message)
        self.committed = committed
        self.durable = durable


def boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


@dataclass(frozen=True)
class ClockGapSample:
    boot: float
    monotonic: float
    lower: float
    upper: float


def sample_clock_gap():
    """Bound the BOOTTIME minus MONOTONIC offset across one clock sample."""
    before = time.monotonic()
    boot = boottime()
    after = time.monotonic()
    if (not all(math.isfinite(value) for value in (before, boot, after)) or
            boot < before or after < before or
            after - before > CLOCK_SAMPLE_WIDTH_LIMIT):
        return None
    return ClockGapSample(boot, after, boot - after, boot - before)


def gap_proves_suspend(current, baseline):
    return (current is not None and baseline is not None and
            current.lower - baseline.upper >= SUSPEND_GAP_TOLERANCE)


def valid_percent(value):
    if type(value) not in (int, float, dbus.Double):
        return None
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 100 else None
    except (TypeError, ValueError, OverflowError):
        return None


def dbus_boolean(value):
    if not isinstance(value, (bool, dbus.Boolean)):
        raise ValueError('D-Bus boolean property is missing or malformed')
    return bool(value)


def dbus_uint32(value):
    if type(value) not in (int, dbus.UInt32) or not 0 <= value <= 0xffffffff:
        raise ValueError('D-Bus unsigned integer property is missing or malformed')
    return int(value)


def upower_device_list(value):
    if (not isinstance(value, (list, tuple)) or
            (getattr(value, 'signature', None) is not None and str(value.signature) != 'o')):
        raise ValueError('UPower device enumeration is missing or malformed')
    return list(value)


def data_home():
    value = str(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
    if not value.isprintable():
        raise ValueError('XDG_STATE_HOME contains a non-printable character')
    path = Path(value)
    if not path.is_absolute():
        raise ValueError('XDG_STATE_HOME must be an absolute path')
    return path / "sleep-disabler"


def machine_identity():
    identity = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    if not identity:
        raise ValueError("Machine identity is unavailable")
    return identity


def atomic_json(path, values):
    temporary = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(values, stream)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    except OSError as error:
        raise AtomicWriteError(str(error), committed=False, durable=False) from error
    directory = None
    durable = False
    try:
        directory = os.open(path.parent, os.O_RDONLY)
        os.fsync(directory)
        durable = True
    except OSError as error:
        raise AtomicWriteError(str(error), committed=True, durable=durable) from error
    finally:
        if directory is not None:
            try:
                os.close(directory)
            except OSError as error:
                raise AtomicWriteError(str(error), committed=True, durable=durable) from error


class LegacyBrightness:
    identity = "gnome-settings-daemon:built-in-panel"

    def __init__(self, agent, owner=BRIGHTNESS):
        self.agent = agent
        self.owner = owner

    def properties(self):
        return self.agent.proxy(self.agent.session_bus, self.owner, BRIGHTNESS_PATH,
                                "org.freedesktop.DBus.Properties")

    def probe_async(self, callback, timeout_provider=lambda: RECOVERY_CALL_TIMEOUT,
                    current=lambda: True):
        def introspected(xml):
            if not current():
                return
            try:
                root = ET.fromstring(str(xml))
                prop = root.find("./interface[@name='%s']/property[@name='Brightness']" % BRIGHTNESS_IFACE)
                if prop is None or prop.get('type') != 'i' or prop.get('access') != 'readwrite':
                    raise ValueError('GNOME Screen.Brightness read/write property is absent')
                self.output_identity()
                self.read_async(lambda _level, failure: callback(failure),
                                timeout=timeout_provider())
            except (OSError, ValueError, ET.ParseError, dbus.DBusException, TimeoutError) as failure:
                callback(failure)

        self.agent.proxy(self.agent.session_bus, self.owner, BRIGHTNESS_PATH,
                         'org.freedesktop.DBus.Introspectable').Introspect(
            timeout=timeout_provider(), reply_handler=introspected,
            error_handler=callback)

    def output_identity(self):
        # The legacy D-Bus property addresses the laptop panel, but does not
        # identify a connector. Restrict it to an unambiguous local topology.
        backlights = sorted((SYS_CLASS / 'backlight').glob('*'))
        connectors = sorted(path for path in (SYS_CLASS / 'drm').glob('card*-*')
                            if path.name.split('-', 1)[-1].split('-', 1)[0] in ('eDP', 'LVDS', 'DSI')
                            and (path / 'status').read_text().strip() == 'connected')
        if len(backlights) != 1 or len(connectors) != 1:
            raise ValueError('Built-in output identity is ambiguous')
        return '%s:%s:%s' % (self.identity, connectors[0].name, backlights[0].name)

    def read(self):
        raw = self.properties().Get(BRIGHTNESS_IFACE, "Brightness", timeout=RECOVERY_CALL_TIMEOUT)
        if not isinstance(raw, int) or isinstance(raw, (bool, dbus.Boolean)):
            raise ValueError("No usable built-in panel brightness")
        value = int(raw)
        if not 0 <= value <= 100:
            raise ValueError("No usable built-in panel brightness")
        return value

    def read_async(self, callback, timeout=RECOVERY_CALL_TIMEOUT):
        def received(value):
            try:
                if not isinstance(value, int) or isinstance(value, (bool, dbus.Boolean)):
                    raise ValueError("No usable built-in panel brightness")
                level = int(value)
                if not 0 <= level <= 100:
                    raise ValueError("No usable built-in panel brightness")
            except (TypeError, ValueError) as error:
                callback(None, error)
                return
            callback(level, None)

        self.properties().Get(BRIGHTNESS_IFACE, "Brightness",
                              timeout=timeout,
                              reply_handler=received,
                              error_handler=lambda error: callback(None, error))

    def write(self, value):
        self.properties().Set(BRIGHTNESS_IFACE, "Brightness", dbus.Int32(value), timeout=RECOVERY_CALL_TIMEOUT)

    def write_async(self, value, callback, timeout=RECOVERY_CALL_TIMEOUT):
        self.properties().Set(BRIGHTNESS_IFACE, 'Brightness', dbus.Int32(value),
                              timeout=timeout, reply_handler=lambda: callback(None),
                              error_handler=callback)


class Agent(dbus.service.Object):
    def __init__(self):
        try:
            self._initialize()
        except Exception:
            for attr in ('session_bus', 'system_bus'):
                bus = getattr(self, attr, None)
                if bus is not None:
                    try:
                        bus.close()
                    except Exception:
                        LOG.exception('%s close failed after startup error', attr)
            raise

    def _initialize(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.session_bus = dbus.SessionBus()
        self.session_bus.set_exit_on_disconnect(False)
        self.system_bus = dbus.SystemBus()
        self.system_bus.set_exit_on_disconnect(False)
        self.bus_name = dbus.service.BusName(APP, self.session_bus, do_not_queue=True)
        super().__init__(self.bus_name, PATH)
        self.loop = GLib.MainLoop()
        self.fd = None
        self.sleep_fd_owner = ''
        self.lid_fd = None
        self.lid_acquisition = None
        self.lid_mode_request = None
        self.session_cookie = None
        self.session_cookie_state = 'absent'
        self.session_cookie_owner = ''
        self.session_owner_generation = 0
        self.shell_owner_generation = 0
        self.inhibitor_outcome = 'absent'
        self.release_attempts = 0
        self.release_retry_after = 0
        self.release_retry_source = 0
        self.session_release_operation = None
        self.prevention_release = None
        self.prevention_acquisition = None
        self.exit_failure = False
        self.enabled = False
        self.desired = False
        self.lid_mode = False
        self.lid_dimming = False
        self.failsafe = False
        self.failsafe_triggered = False
        self.failsafe_evaluation = None
        self.suspend_attempt = None
        self.threshold = 20
        self.last_timer_minutes = 30
        self.last_timer_require_lid = False
        self.last_timer_require_prevention = False
        self.deadline = None
        self.require_lid = False
        self.require_prevention = False
        self.last_error = ""
        self.lid_error = ""
        self.timer_outcome = ""
        self.clock_gap = sample_clock_gap()
        self.logind_owner_generation = 0
        self.last_prepare_signal = None
        self.sleep_tx = self.idle_sleep_transaction()
        self.sleep_reconcile_operation = None
        self.suspend_preflight_operation = None
        self.sleep_outcome = 'none'
        self.suspend_request_outcome = 'none'
        self.timer_phase = 'idle'
        self.lid_closed = None
        self.on_battery = None
        self.battery_percent = None
        self.battery_discharge_state = 'unknown'
        self.battery_discharging = False
        self.power_generation = 0
        self.power_refresh = None
        self.power_last_known_lid = None
        self.timer_start = None
        self.brightness_record = None
        self.brightness_adapter = None
        self.brightness_available = False
        self.brightness_probe_generation = 0
        self.brightness_probe_operation = None
        self.brightness_dim_operation = None
        self.brightness_restore_operation = None
        self.brightness_error = ""
        self.brightness_journal_state = 'absent'
        self.brightness_retry_after = 0
        self.brightness_redim_suppressed = False
        self.brightness_retry_pending = False
        self.brightness_release_pending = False
        self.dimming_verifications = set()
        self.dimming_verification = None
        self.lid_outcome = "unverified"
        self.recovering = False
        self.shutting_down = False
        self.stop_deadline_source = 0
        self.panel_runtime_version = ''
        self.panel_runtime_sender = ''
        self.panel_registration_cancellers = set()
        self.prevention_enable_requests = set()
        self.prevention_disable_requests = set()
        self.dimming_disable_requests = set()
        self.last_timer_state = -1
        self.settings_path = data_home() / "preferences.json"
        self.brightness_path = data_home() / "brightness-recovery.json"
        self.watch_bus_disconnects()
        self.check_startup_connections()
        self.load_settings()
        self.system_bus.add_signal_receiver(self.on_properties_changed,
            signal_name="PropertiesChanged", dbus_interface="org.freedesktop.DBus.Properties",
            bus_name=UPOWER)
        self.system_bus.add_signal_receiver(self.on_device_change,
            signal_name="DeviceAdded", dbus_interface=UPOWER, bus_name=UPOWER)
        self.system_bus.add_signal_receiver(self.on_device_change,
            signal_name="DeviceRemoved", dbus_interface=UPOWER, bus_name=UPOWER)
        self.system_bus.add_signal_receiver(self.on_prepare_sleep,
            signal_name="PrepareForSleep", dbus_interface="org.freedesktop.login1.Manager",
            bus_name=LOGIN)
        self.system_bus.add_signal_receiver(self.on_owner_change,
            signal_name="NameOwnerChanged", dbus_interface="org.freedesktop.DBus",
            arg0=LOGIN)
        self.system_bus.add_signal_receiver(self.on_owner_change,
            signal_name="NameOwnerChanged", dbus_interface="org.freedesktop.DBus",
            arg0=UPOWER)
        self.session_bus.add_signal_receiver(self.on_owner_change,
            signal_name="NameOwnerChanged", dbus_interface="org.freedesktop.DBus",
            arg0=SESSION)
        self.session_bus.add_signal_receiver(self.on_owner_change,
            signal_name="NameOwnerChanged", dbus_interface="org.freedesktop.DBus",
            arg0=BRIGHTNESS)
        self.session_bus.add_signal_receiver(self.on_owner_change,
            signal_name="NameOwnerChanged", dbus_interface="org.freedesktop.DBus",
            arg0=SHELL)
        self.session_bus.add_signal_receiver(self.on_panel_owner_change,
            signal_name="NameOwnerChanged", dbus_interface="org.freedesktop.DBus")
        self.check_startup_connections()
        tick_source = 0
        reconcile_source = 0
        try:
            tick_source = GLib.timeout_add_seconds(1, self.tick)
            if not tick_source:
                raise RuntimeError('GLib did not attach the one-second agent tick')
            reconcile_source = GLib.timeout_add_seconds(15, self.reconcile)
            if not reconcile_source:
                raise RuntimeError('GLib did not attach periodic reconciliation')
            self.refresh_brightness_capability('startup')
            self.refresh_power_async()
            self.reconcile_brightness_async('startup')
        except Exception:
            # A probe may already have queued D-Bus callbacks or a brightness
            # write. Invalidate them before closing their transport, and keep
            # any durable recovery journal for the next startup.
            self.shutting_down = True
            def cleanup(label, action):
                try:
                    action()
                except Exception:
                    LOG.exception('%s cleanup failed after startup error', label)
            for source in (reconcile_source, tick_source):
                if source:
                    cleanup('GLib source', lambda source=source: GLib.source_remove(source))
            cleanup('Brightness probe', self.cancel_brightness_probe)
            cleanup('Power refresh', self.cancel_power_refresh)
            for attr in ('brightness_dim_operation', 'brightness_restore_operation'):
                operation = getattr(self, attr)
                if operation is not None:
                    setattr(self, attr, None)
                    cleanup(attr, lambda operation=operation: GLib.source_remove(operation['source']))
            raise
        LOG.info("Agent started; prevention is off")

    def proxy(self, bus, name, path, interface):
        return dbus.Interface(bus.get_object(name, path, introspect=False), interface)

    def property(self, bus, name, path, interface, key):
        return self.proxy(bus, name, path, "org.freedesktop.DBus.Properties").Get(
            interface, key, timeout=RECOVERY_CALL_TIMEOUT)

    def session_manager_owner(self):
        return str(self.proxy(self.session_bus, 'org.freedesktop.DBus',
                              '/org/freedesktop/DBus', 'org.freedesktop.DBus').GetNameOwner(
                                  SESSION, timeout=RECOVERY_CALL_TIMEOUT))

    def watch_bus_disconnects(self):
        # libdbus bus defaults can exit without running Python cleanup.
        for bus, handler in ((self.session_bus, self.on_session_bus_disconnected),
                             (self.system_bus, self.on_system_bus_disconnected)):
            bus.add_signal_receiver(handler, signal_name='Disconnected',
                dbus_interface='org.freedesktop.DBus.Local',
                path='/org/freedesktop/DBus/Local')

    def check_startup_connections(self):
        # A disconnect can occur before its local signal is dispatched. Check
        # explicitly before startup work and before entering the main loop.
        if not self.session_bus.get_is_connected():
            self.on_session_bus_disconnected()
        if not self.system_bus.get_is_connected():
            self.on_system_bus_disconnected()
        if self.exit_failure:
            raise dbus.DBusException('Bus disconnected during agent startup',
                                     name=IFACE + '.Unavailable')

    @staticmethod
    def validate_settings(values):
        if not isinstance(values, dict):
            raise ValueError("preferences must be an object")
        threshold = values.get("threshold", 20)
        minutes = values.get("timer_minutes", 30)
        if type(threshold) is not int or not 1 <= threshold <= 99:
            raise ValueError("invalid threshold")
        if type(minutes) is not int or not 1 <= minutes <= MAX_SECONDS // 60:
            raise ValueError("invalid timer duration")
        for key in ("failsafe", "lid_mode", "lid_dimming",
                    "timer_require_lid", "timer_require_prevention"):
            if type(values.get(key, False)) is not bool:
                raise ValueError("invalid " + key)
        return {
            "threshold": threshold, "failsafe": values.get("failsafe", False),
            "lid_mode": values.get("lid_mode", False),
            "lid_dimming": values.get("lid_dimming", False),
            "timer_minutes": minutes,
            "timer_require_lid": values.get("timer_require_lid", False),
            "timer_require_prevention": values.get("timer_require_prevention", False),
        }

    def apply_settings(self, values):
        values = self.validate_settings(values)
        self.threshold = values["threshold"]
        self.failsafe = values["failsafe"]
        self.lid_mode = values["lid_mode"]
        self.lid_dimming = values["lid_dimming"]
        self.last_timer_minutes = values["timer_minutes"]
        self.last_timer_require_lid = values["timer_require_lid"]
        self.last_timer_require_prevention = values["timer_require_prevention"]

    def load_settings(self):
        try:
            self.apply_settings(json.loads(self.settings_path.read_text()))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, TypeError) as error:
            self.last_error = "Preferences could not be loaded; defaults are active: " + str(error)

    def settings(self):
        return {"threshold": self.threshold, "failsafe": self.failsafe,
                  "lid_mode": self.lid_mode, "lid_dimming": self.lid_dimming,
                  "timer_minutes": self.last_timer_minutes,
                  "timer_require_lid": self.last_timer_require_lid,
                  "timer_require_prevention": self.last_timer_require_prevention}

    def persist_settings(self, **changes):
        values = self.settings()
        values.update(changes)
        try:
            atomic_json(self.settings_path, values)
        except OSError as error:
            if isinstance(error, AtomicWriteError) and error.committed:
                try:
                    persisted = self.validate_settings(json.loads(self.settings_path.read_text()))
                    if persisted == self.validate_settings(values):
                        self.apply_settings(persisted)
                        self.last_error = "Preferences saved, but directory durability is uncertain: " + str(error)
                        self.publish()
                        return
                    self.apply_settings(persisted)
                except (OSError, ValueError, TypeError, json.JSONDecodeError):
                    self.apply_settings({})
                # A mismatched/unreadable committed file changes authoritative
                # settings even though the action will raise. Reconcile disabled
                # features here, since the method caller's success path is skipped.
                if not self.lid_mode:
                    self.close_lid()
                    self.lid_outcome = 'unavailable'
                    self.set_lid_error('')
                self.reconcile_brightness_async('settings-reload')
            self.last_error = "Could not save preferences: " + str(error)
            self.publish()
            LOG.error("Could not save preferences: %s", error)
            raise dbus.DBusException("Could not save preferences: " + str(error),
                                     name=IFACE + ".PersistenceError")
        for key, value in changes.items():
            setattr(self, {"lid_mode": "lid_mode", "lid_dimming": "lid_dimming",
                           "timer_minutes": "last_timer_minutes",
                           "timer_require_lid": "last_timer_require_lid",
                           "timer_require_prevention": "last_timer_require_prevention"}.get(key, key), value)

    def has_brightness_journal(self):
        if self.brightness_record is not None or self.brightness_journal_state in (
                'pending', 'unreadable', 'invalid'):
            return True
        try:
            return self.brightness_path.exists()
        except OSError:
            return True

    def cancel_brightness_probe(self):
        self.brightness_probe_generation += 1
        operation = self.brightness_probe_operation
        self.brightness_probe_operation = None
        if operation is not None:
            GLib.source_remove(operation['source'])

    def refresh_brightness_capability(self, trigger=None):
        """Probe the legacy property without waiting for D-Bus on the main loop."""
        self.cancel_brightness_probe()
        if self.shutting_down:
            return
        generation = self.brightness_probe_generation
        operation = {'source': 0, 'deadline': time.monotonic() + BRIGHTNESS_PROBE_TIMEOUT}

        def remaining_call_timeout():
            remaining = operation['deadline'] - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Brightness probe timed out')
            return min(RECOVERY_CALL_TIMEOUT, remaining)

        def finish(adapter, failure):
            if self.brightness_probe_operation is not operation or \
                    generation != self.brightness_probe_generation:
                return
            if failure is None and time.monotonic() >= operation['deadline']:
                failure = TimeoutError('Brightness probe timed out')
            self.brightness_probe_operation = None
            if operation['source']:
                GLib.source_remove(operation['source'])
            if failure is None:
                self.brightness_adapter = adapter
                self.brightness_available = True
                if not self.has_brightness_journal():
                    self.brightness_error = ''
            else:
                self.brightness_adapter = None
                self.brightness_available = False
                self.brightness_error = 'No verified built-in-panel read/write API (GNOME 49+ exposes dimming only)'
                LOG.debug('Brightness probe unavailable: %s', failure)
            try:
                self.publish()
            except Exception:
                LOG.exception('Brightness probe state publication failed')
            if trigger is not None:
                self.reconcile_brightness_async(trigger)

        def owner_checked(adapter, expected, owner):
            if str(owner) != expected:
                finish(None, ValueError('Brightness service owner changed during probe'))
            else:
                finish(adapter, None)

        def owner_received(owner):
            if self.brightness_probe_operation is not operation:
                return
            expected = str(owner)
            adapter = LegacyBrightness(self, expected)

            def probed(failure):
                if self.brightness_probe_operation is not operation:
                    return
                if failure is not None:
                    finish(None, failure)
                    return
                try:
                    names.GetNameOwner(BRIGHTNESS, timeout=remaining_call_timeout(),
                        reply_handler=lambda current: owner_checked(adapter, expected, current),
                        error_handler=lambda error: finish(None, error))
                except (dbus.DBusException, OSError, ValueError, TimeoutError) as error:
                    finish(None, error)

            try:
                adapter.probe_async(probed, remaining_call_timeout,
                                    lambda: self.brightness_probe_operation is operation)
            except (dbus.DBusException, OSError, ValueError, ET.ParseError, TimeoutError) as error:
                finish(None, error)

        try:
            operation['source'] = GLib.timeout_add(BRIGHTNESS_PROBE_TIMEOUT * 1000,
                lambda: (finish(None, TimeoutError('Brightness probe timed out')), False)[1])
            if not operation['source']:
                raise RuntimeError('Brightness probe deadline source unavailable')
        except Exception as failure:
            self.brightness_probe_operation = operation
            finish(None, failure)
            return
        self.brightness_probe_operation = operation
        try:
            names = self.proxy(self.session_bus, 'org.freedesktop.DBus',
                               '/org/freedesktop/DBus', 'org.freedesktop.DBus')
            names.GetNameOwner(BRIGHTNESS, timeout=remaining_call_timeout(),
                               reply_handler=owner_received,
                               error_handler=lambda error: finish(None, error))
        except (dbus.DBusException, OSError, ValueError, TimeoutError) as error:
            finish(None, error)

    def validate_brightness_record(self, record, verify_output=False):
        if (not isinstance(record, dict) or record.get("schema") not in (2, 3) or
                not isinstance(record.get('identity'), str) or
                not record['identity'].startswith(LegacyBrightness.identity + ':') or
                not isinstance(record.get('machine'), str) or
                record.get("phase") not in ("prepared", "set-confirmed", "applied") or
                (record.get("phase") == "set-confirmed" and record.get("schema") != 3) or
                type(record.get("before")) is not int or not 0 <= record["before"] <= 100 or
                type(record.get("target")) is not int or not 0 <= record["target"] <= 100 or
                (record.get("phase") == "applied" and
                 (type(record.get("written")) is not int or not 0 <= record["written"] <= 100))):
            raise ValueError("Invalid brightness recovery record")
        if record['machine'] != machine_identity():
            raise ValueError("Foreign brightness recovery record")
        if verify_output:
            if self.brightness_adapter is None:
                raise OSError("Brightness adapter is unavailable")
            if record['identity'] != self.brightness_adapter.output_identity():
                raise ValueError("Brightness output changed")
        return record

    def mark_brightness_validation_error(self, error):
        if isinstance(error, OSError):
            self.brightness_journal_state = 'unreadable'
            self.brightness_retry_after = time.monotonic() + 15
            self.brightness_error = "Cannot verify brightness recovery identity: " + str(error)
        else:
            self.brightness_journal_state = 'invalid'
            self.brightness_error = str(error) + '; record retained for manual attention'
        self.publish()

    def recover_brightness(self):
        """Load and validate the journal without changing brightness."""
        if time.monotonic() < self.brightness_retry_after:
            return 'pending'
        try:
            record = json.loads(self.brightness_path.read_text())
        except FileNotFoundError:
            self.brightness_journal_state = 'absent'
            self.brightness_record = None
            return 'unchanged'
        except OSError as error:
            self.brightness_journal_state = 'unreadable'
            self.brightness_retry_after = time.monotonic() + 15
            self.brightness_error = "Unreadable brightness recovery record: " + str(error)
            self.publish()
            return 'pending'
        except ValueError as error:
            self.brightness_journal_state = 'invalid'
            self.brightness_error = "Invalid brightness recovery record: " + str(error)
            self.publish()
            return 'invalid'
        try:
            self.validate_brightness_record(record)
        except (OSError, ValueError) as error:
            self.mark_brightness_validation_error(error)
            return 'invalid' if self.brightness_journal_state == 'invalid' else 'pending'
        self.brightness_record = record
        self.brightness_journal_state = 'pending'
        self.brightness_retry_after = 0
        self.brightness_retry_pending = True
        return 'loaded'

    def dim_brightness_async(self, callback):
        """Journal and verify one bounded dim write without blocking D-Bus."""
        operation = self.brightness_dim_operation
        if operation is not None:
            if len(operation['waiters']) >= MAX_PUBLIC_WAITERS:
                callback('pending')
                return
            operation['waiters'].append(callback)
            return
        if not self.brightness_desired_dimmed():
            callback('unchanged')
            return
        if self.brightness_restore_operation is not None:
            callback('pending')
            return
        if self.brightness_record is None and self.has_brightness_journal():
            self.recover_brightness()
            # An existing record needs its own recovery decision before any
            # new write, even when the journal appeared since admission.
            callback('pending')
            return
        if self.has_brightness_journal() or self.brightness_adapter is None:
            callback('pending')
            return
        adapter = self.brightness_adapter
        operation = {'source': 0, 'deadline': time.monotonic() + BRIGHTNESS_DIM_TIMEOUT,
                     'waiters': [callback], 'adapter': adapter, 'record': None}

        def finish(result):
            if self.brightness_dim_operation is not operation:
                return
            self.brightness_dim_operation = None
            GLib.source_remove(operation['source'])
            if not self.brightness_desired_dimmed() and self.brightness_record is not None:
                try:
                    self.restore_brightness_async(lambda _restored: None)
                except Exception:
                    LOG.exception('Superseded dim restoration failed')
            for waiter in operation['waiters']:
                try:
                    waiter(result)
                except Exception:
                    LOG.exception('Brightness dim continuation failed')

        def pending(failure):
            if self.brightness_dim_operation is not operation:
                return
            self.brightness_error = ('Brightness recovery pending: ' if self.brightness_record is not None
                                     else 'Could not dim built-in panel: ') + str(failure)
            self.brightness_retry_pending = True
            try:
                self.publish()
            except Exception:
                LOG.exception('Brightness dim failure publication failed')
            finish('pending')

        def remaining():
            value = operation['deadline'] - time.monotonic()
            if value <= 0:
                raise TimeoutError('Brightness dim exceeded its deadline')
            return min(RECOVERY_CALL_TIMEOUT, value)

        def journal_unchanged():
            record = operation['record']
            if record is None or self.brightness_record != record:
                raise ValueError('Brightness journal changed during dim')
            persisted = json.loads(self.brightness_path.read_text())
            if persisted != record:
                raise ValueError('Brightness journal changed on disk during dim')
            if self.brightness_adapter is not adapter:
                raise ValueError('Brightness adapter changed during dim')
            self.validate_brightness_record(persisted, verify_output=True)

        def readback(level, failure):
            if self.brightness_dim_operation is not operation:
                return
            if time.monotonic() >= operation['deadline']:
                pending(TimeoutError('Brightness dim exceeded its deadline'))
                return
            if failure is not None:
                pending(failure)
                return
            try:
                journal_unchanged()
                if level != 0:
                    raise ValueError('Dim readback differs from requested brightness')
                applied = dict(operation['record'], phase='applied', written=0)
                atomic_json(self.brightness_path, applied)
                operation['record'] = applied
                self.brightness_record = applied
                self.brightness_error = ''
                self.brightness_retry_pending = False
            except AtomicWriteError as error:
                if error.committed:
                    self.recover_committed_brightness(applied)
                pending(error)
                return
            except (OSError, ValueError, dbus.DBusException) as error:
                pending(error)
                return
            try:
                self.publish()
            except Exception:
                LOG.exception('Brightness dim success publication failed')
            finish('redimmed')

        def wrote(failure):
            if self.brightness_dim_operation is not operation:
                return
            if time.monotonic() >= operation['deadline']:
                pending(TimeoutError('Brightness dim exceeded its deadline'))
                return
            if failure is not None:
                pending(failure)
                return
            try:
                journal_unchanged()
                confirmed = dict(operation['record'], phase='set-confirmed')
                atomic_json(self.brightness_path, confirmed)
                operation['record'] = confirmed
                self.brightness_record = confirmed
                adapter.read_async(readback, timeout=remaining())
            except AtomicWriteError as error:
                if error.committed:
                    self.recover_committed_brightness(confirmed)
                pending(error)
            except (OSError, ValueError, dbus.DBusException, TimeoutError) as error:
                pending(error)

        def current(level, failure):
            if self.brightness_dim_operation is not operation:
                return
            if time.monotonic() >= operation['deadline']:
                pending(TimeoutError('Brightness dim exceeded its deadline'))
                return
            if failure is not None:
                pending(failure)
                return
            if type(level) is not int or not 0 <= level <= 100:
                pending(ValueError('Brightness read returned an invalid value'))
                return
            if not self.brightness_desired_dimmed() or self.brightness_adapter is not adapter:
                finish('unchanged')
                return
            if level == 0:
                finish('unchanged')
                return
            try:
                if self.has_brightness_journal():
                    raise ValueError('Brightness journal appeared during dim')
                record = {'schema': 3, 'identity': adapter.output_identity(),
                          'machine': machine_identity(), 'before': level,
                          'target': 0, 'phase': 'prepared'}
                atomic_json(self.brightness_path, record)
                operation['record'] = record
                self.brightness_record = record
                self.brightness_journal_state = 'pending'
                adapter.write_async(0, wrote, timeout=remaining())
            except AtomicWriteError as error:
                if error.committed:
                    self.recover_committed_brightness(record)
                pending(error)
            except (OSError, ValueError, dbus.DBusException, TimeoutError) as error:
                pending(error)

        try:
            operation['source'] = GLib.timeout_add(BRIGHTNESS_DIM_TIMEOUT * 1000,
                lambda: (pending(TimeoutError('Brightness dim exceeded its deadline')), False)[1])
            if not operation['source']:
                raise RuntimeError('GLib did not attach the brightness dim deadline')
        except Exception as failure:
            self.brightness_error = 'Could not dim built-in panel: ' + str(failure)
            self.brightness_retry_pending = True
            callback('pending')
            return
        self.brightness_dim_operation = operation
        try:
            adapter.read_async(current, timeout=remaining())
        except (OSError, ValueError, dbus.DBusException, TimeoutError) as error:
            pending(error)

    def dim_brightness(self):
        if self.brightness_dim_operation is not None:
            return 'pending'
        if self.shutting_down:
            return 'unchanged'
        if self.brightness_record is None and self.has_brightness_journal():
            self.recover_brightness()
            # A resolved manual conflict must not trigger a new dim in the
            # same lid event.
            return 'pending'
        if self.has_brightness_journal() or not self.brightness_adapter:
            return 'pending' if self.has_brightness_journal() else 'invalid'
        try:
            before = self.brightness_adapter.read()
            if before == 0:
                return 'unchanged'
            record = {"schema": 3, "identity": self.brightness_adapter.output_identity(),
                      "machine": machine_identity(),
                      "before": before, "target": 0, "phase": "prepared"}
            try:
                atomic_json(self.brightness_path, record)
            except AtomicWriteError as error:
                if error.committed:
                    self.recover_committed_brightness(record)
                    self.brightness_retry_pending = True
                    self.brightness_error = "Brightness journal durability is uncertain; dim was not applied: " + str(error)
                    self.publish()
                    return 'pending'
                raise
            self.brightness_record = record
            self.brightness_journal_state = 'pending'
            self.brightness_adapter.write(0)
            confirmed = dict(record, phase='set-confirmed')
            try:
                atomic_json(self.brightness_path, confirmed)
                self.brightness_record = confirmed
            except AtomicWriteError as error:
                if error.committed:
                    self.recover_committed_brightness(confirmed)
                self.brightness_retry_pending = True
                self.brightness_error = 'Confirmed brightness journal durability is uncertain: ' + str(error)
                self.publish()
                return 'pending'
            record = confirmed
            written = self.brightness_adapter.read()
            if written != record['target']:
                raise ValueError('Dim readback differs from requested brightness; recovery journal retained')
            applied = dict(record, written=written, phase="applied")
            try:
                atomic_json(self.brightness_path, applied)
                self.brightness_record = applied
            except AtomicWriteError as error:
                if not error.committed:
                    raise
                self.recover_committed_brightness(applied)
                self.brightness_retry_pending = True
                self.brightness_error = "Applied brightness journal durability is uncertain: " + str(error)
                self.publish()
                return 'pending'
            self.brightness_error = ""
            self.brightness_retry_pending = False
            self.publish()
            return 'redimmed'
        except (dbus.DBusException, OSError, ValueError) as error:
            LOG.warning("Could not dim built-in panel: %s", error)
            self.brightness_error = ("Brightness recovery pending: " if self.brightness_record is not None
                                     else "Could not dim built-in panel: ") + str(error)
            self.brightness_retry_pending = True
            self.publish()
            return 'pending'

    def recover_committed_brightness(self, expected):
        """A visible replace must be reconciled from disk before reporting it."""
        try:
            persisted = json.loads(self.brightness_path.read_text())
            self.validate_brightness_record(persisted)
            self.brightness_record = persisted
            if persisted != expected:
                raise ValueError('Brightness recovery record changed after replace')
            self.brightness_journal_state = 'pending'
        except (OSError, ValueError, TypeError) as error:
            self.brightness_record = None
            self.mark_brightness_validation_error(error)

    def restore_brightness(self):
        record = self.brightness_record
        if record is None:
            return 'unchanged'
        if self.brightness_adapter is None:
            self.brightness_error = "Brightness recovery pending: brightness service unavailable"
            self.brightness_retry_pending = True
            self.publish()
            return 'pending'
        try:
            persisted = json.loads(self.brightness_path.read_text())
            if persisted != record:
                raise ValueError('Brightness recovery record changed on disk')
            self.validate_brightness_record(persisted, verify_output=True)
            if record['phase'] == 'prepared':
                # The Set may still execute after a timeout or crash. Reading
                # the old/target value cannot prove that no late write remains.
                self.brightness_error = 'Brightness write outcome uncertain; prepared journal retained'
                self.brightness_retry_pending = True
                self.publish()
                return 'pending'
            current = self.brightness_adapter.read()
            expected = record.get("written", record["target"])
            if current == expected:
                self.brightness_adapter.write(record["before"])
                if self.brightness_adapter.read() != record["before"]:
                    raise ValueError("Restore readback differs from saved brightness")
                self.brightness_error = ""
            elif record['phase'] == 'set-confirmed':
                self.brightness_error = 'Brightness recovery ambiguous; journal retained'
                self.brightness_retry_pending = True
                self.publish()
                return 'pending'
            else:
                self.brightness_error = "Manual brightness change preserved"
                result = 'manual-change-preserved'
                self.brightness_redim_suppressed = True
            if current == expected:
                result = 'restored'
            self.brightness_path.unlink(missing_ok=True)
            self.brightness_record = None
            self.brightness_journal_state = 'resolved'
            self.brightness_retry_pending = False
            self.publish()
            return result
        except dbus.DBusException as error:
            LOG.warning("Brightness recovery pending: %s", error)
            self.brightness_error = "Brightness recovery pending: " + str(error)
            self.brightness_retry_pending = True
            self.publish()
            return 'pending'
        except (OSError, ValueError, json.JSONDecodeError) as error:
            LOG.warning("Brightness recovery identity check failed: %s", error)
            self.mark_brightness_validation_error(error)
            self.brightness_retry_pending = isinstance(error, OSError)
            return 'invalid' if self.brightness_journal_state == 'invalid' else 'pending'

    def restore_brightness_async(self, callback):
        """Restore through bounded callbacks while retaining uncertain journals."""
        existing = self.brightness_restore_operation
        if existing is not None:
            if len(existing['waiters']) >= MAX_PUBLIC_WAITERS:
                callback('pending')
                return
            existing['waiters'].append(callback)
            return
        record = self.brightness_record
        adapter = self.brightness_adapter
        if record is None:
            callback('unchanged')
            return
        if adapter is None:
            self.brightness_error = 'Brightness recovery pending: brightness service unavailable'
            self.brightness_retry_pending = True
            try:
                self.publish()
            except Exception:
                LOG.exception('Brightness unavailable state publication failed')
            callback('pending')
            return
        operation = {'source': 0, 'deadline': time.monotonic() + BRIGHTNESS_RESTORE_TIMEOUT,
                     'waiters': [callback], 'record': record, 'adapter': adapter}

        def publish_restoration():
            try:
                self.publish()
            except Exception:
                LOG.exception('Brightness restore state publication failed')

        def finish(result):
            if self.brightness_restore_operation is not operation:
                return
            self.brightness_restore_operation = None
            GLib.source_remove(operation['source'])
            for waiter in operation['waiters']:
                try:
                    waiter(result)
                except Exception:
                    LOG.exception('Brightness restore completion failed')

        def pending(failure):
            if self.brightness_restore_operation is not operation:
                return
            self.brightness_retry_pending = True
            self.brightness_error = 'Brightness recovery pending: ' + str(failure)
            publish_restoration()
            finish('pending')

        def validated():
            if self.brightness_restore_operation is not operation:
                return False
            if time.monotonic() >= operation['deadline']:
                pending(TimeoutError('Brightness restoration exceeded its deadline'))
                return False
            try:
                persisted = json.loads(self.brightness_path.read_text())
                if persisted != record:
                    raise ValueError('Brightness recovery record changed on disk')
                if self.brightness_adapter is not adapter:
                    raise ValueError('Brightness adapter changed during restoration')
                self.validate_brightness_record(persisted, verify_output=True)
            except (OSError, ValueError) as failure:
                try:
                    self.mark_brightness_validation_error(failure)
                except Exception:
                    LOG.exception('Brightness restore validation publication failed')
                self.brightness_retry_pending = True
                finish('invalid' if self.brightness_journal_state == 'invalid' else 'pending')
                return False
            if time.monotonic() >= operation['deadline']:
                pending(TimeoutError('Brightness restoration exceeded its deadline'))
                return False
            return True

        def call_timeout():
            remaining = operation['deadline'] - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('Brightness restoration exceeded its deadline')
            return min(RECOVERY_CALL_TIMEOUT, remaining)

        def restored_read(level, failure):
            if self.brightness_restore_operation is not operation:
                return
            if failure is not None:
                pending(failure)
            elif not validated():
                return
            elif level != record['before']:
                pending(ValueError('Restore readback differs from saved brightness'))
            else:
                self.brightness_error = ''
                resolve('restored')

        def wrote(failure):
            if self.brightness_restore_operation is not operation:
                return
            if failure is not None:
                pending(failure)
            elif validated():
                try:
                    adapter.read_async(restored_read, timeout=call_timeout())
                except (dbus.DBusException, OSError, ValueError, TimeoutError) as error:
                    pending(error)

        def resolve(result):
            if not validated():
                return
            try:
                self.brightness_path.unlink(missing_ok=True)
            except OSError as failure:
                try:
                    self.mark_brightness_validation_error(failure)
                except Exception:
                    LOG.exception('Brightness restore unlink publication failed')
                finish('pending')
                return
            self.brightness_record = None
            self.brightness_journal_state = 'resolved'
            self.brightness_retry_pending = False
            publish_restoration()
            finish(result)

        def current_read(level, failure):
            if self.brightness_restore_operation is not operation:
                return
            if failure is not None:
                pending(failure)
                return
            if not validated():
                return
            expected = record.get('written', record['target'])
            if level == expected:
                try:
                    adapter.write_async(record['before'], wrote, timeout=call_timeout())
                except (dbus.DBusException, OSError, ValueError, TimeoutError) as error:
                    pending(error)
            elif record['phase'] == 'set-confirmed':
                pending(ValueError('Brightness recovery is ambiguous'))
            else:
                self.brightness_error = 'Manual brightness change preserved'
                self.brightness_redim_suppressed = True
                resolve('manual-change-preserved')

        try:
            operation['source'] = GLib.timeout_add(BRIGHTNESS_RESTORE_TIMEOUT * 1000,
                lambda: (pending(TimeoutError('Brightness restoration exceeded its deadline')),
                         False)[1])
            if not operation['source']:
                raise RuntimeError('GLib did not attach the brightness restore deadline')
        except Exception as setup_error:
            self.brightness_error = 'Brightness recovery pending: ' + str(setup_error)
            self.brightness_retry_pending = True
            publish_restoration()
            callback('pending')
            return
        self.brightness_restore_operation = operation
        if not validated():
            return
        if record['phase'] == 'prepared':
            pending(ValueError('Brightness write outcome is uncertain; prepared journal retained'))
            return
        try:
            adapter.read_async(current_read, timeout=call_timeout())
        except (dbus.DBusException, OSError, ValueError, TimeoutError) as error:
            pending(error)

    def brightness_desired_dimmed(self):
        return (not self.shutting_down and not self.brightness_release_pending and
                self.enabled and self.lid_dimming and
                self.lid_closed is True)

    def brightness_state(self):
        if self.brightness_journal_state == 'unreadable':
            return 'unreadable'
        if self.brightness_journal_state == 'invalid':
            return 'invalid'
        record = self.brightness_record
        if record is None:
            if self.has_brightness_journal():
                return 'restore-pending'
            if self.brightness_retry_pending and self.brightness_desired_dimmed():
                return 'redim-pending'
            if self.brightness_redim_suppressed and self.brightness_error == 'Manual brightness change preserved':
                return 'manual-change-preserved'
            return 'idle'
        if (record.get('phase') == 'applied' and self.brightness_desired_dimmed() and
                not self.brightness_retry_pending):
            return 'dimmed-owned'
        if record.get('phase') == 'prepared':
            return 'restore-pending'
        return 'restore-pending'

    def brightness_recovery_pending(self):
        return self.brightness_state() in (
            'restore-pending', 'redim-pending', 'unreadable', 'invalid')

    def verify_owned_dimming(self):
        """Confirm an applied journal still describes this output and write."""
        record = self.brightness_record
        if record is None or self.brightness_adapter is None:
            return False
        try:
            persisted = json.loads(self.brightness_path.read_text())
            if persisted != record:
                raise ValueError('Brightness recovery record changed on disk')
            self.validate_brightness_record(persisted, verify_output=True)
            if self.brightness_adapter.read() != record['written']:
                raise ValueError('Brightness changed after the recorded dim')
            return True
        except (dbus.DBusException, OSError, ValueError, KeyError) as error:
            self.brightness_retry_pending = True
            self.brightness_error = 'Brightness ownership needs recovery: ' + str(error)
            self.publish()
            return False

    def reconcile_brightness(self, trigger):
        """Apply the desired brightness state through the journal validation gate."""
        if self.brightness_dim_operation is not None:
            return 'pending'
        result = 'unchanged'
        if self.brightness_record is None and self.brightness_journal_state != 'invalid':
            result = self.recover_brightness()
        wants_dim = self.brightness_desired_dimmed()
        wake_like = trigger in ('wake', 'failed-preparation', 'uncertain-resolution',
                                'owner-loss', 'owner-return', 'startup')
        if self.brightness_record is not None:
            retrying = self.brightness_retry_pending
            healthy_dim = (self.brightness_record.get('phase') == 'applied' and wants_dim and
                           not wake_like and not self.brightness_retry_pending)
            if healthy_dim:
                if trigger != 'prevention-enable' or self.verify_owned_dimming():
                    return 'unchanged'
            result = self.restore_brightness()
            if result != 'restored':
                return result
            if wants_dim and (wake_like or (trigger == 'periodic' and retrying)) and \
                    not self.brightness_redim_suppressed:
                return self.dim_brightness()
            return result
        if wants_dim and not self.brightness_redim_suppressed:
            eligible = trigger in ('lid-close', 'dimming-enable', 'prevention-enable',
                                   'owner-return', 'startup')
            if eligible or (trigger == 'periodic' and self.brightness_retry_pending):
                return self.dim_brightness()
        return result

    def reconcile_brightness_async(self, trigger, callback=None):
        """Reconcile event/background brightness through the async coordinator."""
        done = callback if callback is not None else lambda _result: None
        if self.brightness_dim_operation is not None:
            if trigger == 'lid-open':
                if len(self.brightness_dim_operation['waiters']) < MAX_PUBLIC_WAITERS:
                    self.brightness_dim_operation['waiters'].append(
                        lambda _result: self.reconcile_brightness_async(trigger, done))
                else:
                    done('pending')
            elif trigger == 'prevention-enable':
                if len(self.brightness_dim_operation['waiters']) < MAX_PUBLIC_WAITERS:
                    self.brightness_dim_operation['waiters'].append(
                        lambda result: (done(result) if result == 'pending' else
                                        self.reconcile_brightness_async(trigger, done)))
                else:
                    done('pending')
            else:
                done('pending')
            return
        result = 'unchanged'
        if self.brightness_record is None and self.brightness_journal_state != 'invalid':
            result = self.recover_brightness()
        wants_dim = self.brightness_desired_dimmed()
        wake_like = trigger in ('wake', 'failed-preparation', 'uncertain-resolution',
                                'owner-loss', 'owner-return', 'startup')
        if self.brightness_record is not None:
            retrying = self.brightness_retry_pending
            healthy_dim = (self.brightness_record.get('phase') == 'applied' and wants_dim and
                           not wake_like and not retrying)
            if healthy_dim:
                if trigger == 'prevention-enable':
                    record = self.brightness_record
                    adapter = self.brightness_adapter
                    if adapter is None:
                        done('pending')
                        return

                    def checked(level, failure):
                        if self.shutting_down or self.brightness_record != record or \
                                self.brightness_adapter is not adapter or \
                                not self.brightness_desired_dimmed():
                            if (not self.shutting_down and self.brightness_record == record and
                                    self.brightness_adapter is not adapter and
                                    self.brightness_desired_dimmed()):
                                self.brightness_retry_pending = True
                                self.brightness_error = ('Brightness ownership needs recovery: '
                                                         'adapter changed')
                                try:
                                    self.publish()
                                except Exception:
                                    LOG.exception('Brightness verification publication failed')
                            done('pending')
                            return
                        try:
                            if failure is not None:
                                raise ValueError(str(failure))
                            persisted = json.loads(self.brightness_path.read_text())
                            if persisted != record:
                                raise ValueError('Brightness recovery record changed on disk')
                            self.validate_brightness_record(persisted, verify_output=True)
                            if level != record['written']:
                                raise ValueError('Brightness changed after the recorded dim')
                        except Exception as verification_error:
                            self.brightness_retry_pending = True
                            self.brightness_error = ('Brightness ownership needs recovery: ' +
                                                     str(verification_error))
                            try:
                                self.publish()
                            except Exception:
                                LOG.exception('Brightness verification publication failed')
                            done('pending')
                            return
                        done('unchanged')

                    try:
                        adapter.read_async(checked, timeout=RECOVERY_CALL_TIMEOUT)
                    except Exception as verification_error:
                        checked(None, verification_error)
                    return
                done('unchanged')
                return

            def restored(outcome):
                if (outcome == 'restored' and self.brightness_desired_dimmed() and
                        (wake_like or (trigger == 'periodic' and retrying)) and
                        not self.brightness_redim_suppressed):
                    self.dim_brightness_async(done)
                else:
                    done(outcome)

            self.restore_brightness_async(restored)
            return
        if wants_dim and not self.brightness_redim_suppressed:
            eligible = trigger in ('lid-close', 'dimming-enable', 'prevention-enable',
                                   'owner-return', 'startup')
            if eligible or (trigger == 'periodic' and self.brightness_retry_pending):
                self.dim_brightness_async(done)
                return
        done(result)

    def refresh_power(self, reconcile_lid=True):
        # In-process compatibility path; exported D-Bus operations use the
        # bounded asynchronous refresh. Supersede older background reads.
        self.cancel_power_refresh()
        old_lid = (self.lid_closed if self.lid_closed is not None else
                   self.power_last_known_lid)
        complete = False
        try:
            names = self.proxy(self.system_bus, 'org.freedesktop.DBus',
                               '/org/freedesktop/DBus', 'org.freedesktop.DBus')
            owner = str(names.GetNameOwner(UPOWER, timeout=RECOVERY_CALL_TIMEOUT))
            self.on_battery = dbus_boolean(self.property(self.system_bus, owner, UPOWER_PATH, UPOWER, "OnBattery"))
            present = dbus_boolean(self.property(self.system_bus, owner, UPOWER_PATH, UPOWER, "LidIsPresent"))
            self.lid_closed = dbus_boolean(self.property(self.system_bus, owner, UPOWER_PATH, UPOWER, "LidIsClosed")) if present else None
            devices = upower_device_list(self.proxy(self.system_bus, owner, UPOWER_PATH, UPOWER).EnumerateDevices(
                timeout=RECOVERY_CALL_TIMEOUT))
            batteries = []
            for path in devices:
                props = self.proxy(self.system_bus, owner, path, "org.freedesktop.DBus.Properties").GetAll(
                    "org.freedesktop.UPower.Device", timeout=RECOVERY_CALL_TIMEOUT)
                kind = dbus_uint32(props['Type'])
                supply = dbus_boolean(props['PowerSupply'])
                device_present = dbus_boolean(props['IsPresent'])
                if kind == 2 and supply and device_present:
                    batteries.append(props)
            states = []
            for props in batteries:
                state = dbus_uint32(props['State'])
                if state == 2:
                    states.append('discharging')
                elif state in (1, 4, 5):
                    states.append('not-discharging')
                else:
                    states.append('unknown')
            if 'discharging' in states:
                self.battery_discharge_state = 'discharging'
            elif states and all(state == 'not-discharging' for state in states):
                self.battery_discharge_state = 'not-discharging'
            else:
                self.battery_discharge_state = 'unknown'
            self.battery_discharging = self.battery_discharge_state == 'discharging'
            if len(batteries) == 1:
                self.battery_percent = valid_percent(batteries[0].get("Percentage"))
            elif len(batteries) > 1:
                display_path = self.proxy(self.system_bus, owner, UPOWER_PATH, UPOWER).GetDisplayDevice(
                    timeout=RECOVERY_CALL_TIMEOUT)
                self.battery_percent = valid_percent(self.property(self.system_bus, owner, display_path,
                    "org.freedesktop.UPower.Device", "Percentage"))
            else:
                self.battery_percent = None
            if batteries and self.battery_percent is None:
                raise ValueError('UPower battery percentage is missing or malformed')
            if str(names.GetNameOwner(UPOWER, timeout=RECOVERY_CALL_TIMEOUT)) != owner:
                raise ValueError('UPower owner changed during refresh')
            complete = True
        except (dbus.DBusException, OSError, TypeError, ValueError, KeyError, OverflowError) as error:
            LOG.warning("UPower unavailable: %s", error)
            self.on_battery = None
            self.lid_closed = None
            self.battery_percent = None
            self.battery_discharge_state = 'unknown'
            self.battery_discharging = False
        if complete:
            self.power_last_known_lid = self.lid_closed
        if self.lid_mode and self.lid_closed is None and self.lid_fd is not None:
            self.lid_outcome = "unverified; lid state unavailable"
        if old_lid != self.lid_closed:
            if old_lid is False and self.lid_closed is True:
                self.brightness_redim_suppressed = False
            if reconcile_lid:
                self.handle_lid_change()
        self.publish()

    def cancel_power_refresh(self):
        self.power_generation += 1
        operation = self.power_refresh
        self.power_refresh = None
        if operation is not None:
            GLib.source_remove(operation['source'])
            for callback in operation['waiters']:
                try:
                    callback(False)
                except Exception:
                    LOG.exception('Canceled power refresh continuation failed')

    def invalidate_power_snapshot(self):
        if self.lid_closed is not None:
            self.power_last_known_lid = self.lid_closed
        changed = (self.on_battery is not None or self.lid_closed is not None or
                   self.battery_percent is not None or
                   self.battery_discharge_state != 'unknown')
        self.on_battery = None
        self.lid_closed = None
        self.battery_percent = None
        self.battery_discharge_state = 'unknown'
        self.battery_discharging = False
        if self.lid_mode and self.lid_fd is not None:
            self.lid_outcome = 'unverified; lid state unavailable'
        if changed:
            self.publish()

    def power_changed(self):
        if self.shutting_down:
            return
        self.invalidate_power_snapshot()
        if self.power_refresh is not None:
            self.power_refresh['restart_needed'] = True
            self.power_refresh['check_failsafe'] = True
        else:
            self.refresh_power_async(check_failsafe=True)

    def finish_power_refresh(self, operation, snapshot):
        if self.power_refresh is not operation:
            return
        self.power_refresh = None
        if operation['source']:
            GLib.source_remove(operation['source'])
        restart = operation['restart_needed'] and not self.shutting_down
        if restart:
            snapshot = None
        old_lid = (self.lid_closed if self.lid_closed is not None else
                   self.power_last_known_lid)
        if snapshot is None:
            self.on_battery = None
            self.lid_closed = None
            self.battery_percent = None
            self.battery_discharge_state = 'unknown'
            self.battery_discharging = False
        else:
            (self.on_battery, self.lid_closed, self.battery_percent,
             self.battery_discharge_state) = snapshot
            self.battery_discharging = self.battery_discharge_state == 'discharging'
            self.power_last_known_lid = self.lid_closed
        if self.lid_mode and self.lid_closed is None and self.lid_fd is not None:
            self.lid_outcome = 'unverified; lid state unavailable'
        snapshot_lid = self.lid_closed
        reconcile_lid_change = old_lid != snapshot_lid and operation['reconcile_lid']
        if old_lid != self.lid_closed:
            if old_lid is False and self.lid_closed is True:
                self.brightness_redim_suppressed = False
        try:
            self.publish()
        except Exception:
            LOG.exception('Power refresh publication failed')
        for callback in operation['waiters']:
            try:
                callback(snapshot is not None)
            except Exception:
                LOG.exception('Power refresh continuation failed')
        if reconcile_lid_change and self.lid_closed == snapshot_lid and not self.shutting_down:
            try:
                self.handle_lid_change()
            except Exception:
                LOG.exception('Power refresh lid reconciliation failed')
        if snapshot is not None and operation['check_failsafe'] and not self.shutting_down:
            try:
                self.check_failsafe()
            except Exception:
                LOG.exception('Power refresh failsafe evaluation failed')
        if restart and self.power_refresh is None:
            self.refresh_power_async(check_failsafe=True)

    def refresh_power_async(self, callback=None, reconcile_lid=True, check_failsafe=False,
                            deadline=None):
        """Coalesce UPower reads and commit only a complete current-owner snapshot."""
        if self.shutting_down:
            if callback is not None:
                callback(False)
            return
        if deadline is not None and time.monotonic() >= deadline:
            if callback is not None:
                callback(False)
            return
        if self.power_refresh is not None:
            self.power_refresh['reconcile_lid'] |= reconcile_lid
            self.power_refresh['check_failsafe'] |= check_failsafe
            if callback is not None:
                if len(self.power_refresh['waiters']) >= MAX_PUBLIC_WAITERS:
                    callback(False)
                elif deadline is not None and deadline < self.power_refresh['deadline']:
                    waiter = {'done': False, 'source': 0}

                    def complete_waiter(success):
                        if waiter['done']:
                            return
                        waiter['done'] = True
                        if waiter['source']:
                            GLib.source_remove(waiter['source'])
                        try:
                            callback(success and time.monotonic() < deadline)
                        except Exception:
                            LOG.exception('UPower refresh continuation failed')

                    try:
                        waiter['source'] = GLib.timeout_add(
                            max(1, int((deadline - time.monotonic()) * 1000)),
                            lambda: (complete_waiter(False), GLib.SOURCE_REMOVE)[1])
                        if not waiter['source']:
                            raise RuntimeError('GLib did not attach the UPower waiter deadline')
                    except Exception as failure:
                        LOG.warning('UPower waiter deadline setup failed: %s', failure)
                        complete_waiter(False)
                    else:
                        self.power_refresh['waiters'].append(complete_waiter)
                else:
                    self.power_refresh['waiters'].append(callback)
            return
        started = time.monotonic()
        operation_deadline = started + POWER_REFRESH_TIMEOUT
        if deadline is not None:
            operation_deadline = min(operation_deadline, deadline)
        if operation_deadline <= started:
            if callback is not None:
                callback(False)
            return
        self.power_generation += 1
        operation = {'generation': self.power_generation, 'owner': '',
                     'started': started,
                     'deadline': operation_deadline,
                     'source': 0, 'waiters': [] if callback is None else [callback],
                     'reconcile_lid': reconcile_lid, 'check_failsafe': check_failsafe,
                     'values': {}, 'devices': [],
                     'next_device': 0, 'pending_devices': 0, 'batteries': [],
                     'validating_owner': False, 'restart_needed': False}
        try:
            operation['source'] = GLib.timeout_add(
                max(1, int((operation_deadline - time.monotonic()) * 1000)),
                lambda: (self.finish_power_refresh(operation, None), GLib.SOURCE_REMOVE)[1])
            if not operation['source']:
                raise RuntimeError('GLib did not attach the UPower snapshot deadline')
        except Exception as failure:
            LOG.warning('UPower snapshot deadline setup failed: %s', failure)
            self.power_refresh = operation
            self.finish_power_refresh(operation, None)
            return
        self.power_refresh = operation

        def live():
            return (self.power_refresh is operation and
                    operation['generation'] == self.power_generation and
                    not self.shutting_down and time.monotonic() < operation['deadline'])

        def fail(error):
            if live():
                LOG.warning('UPower snapshot unavailable: %s', error)
                self.finish_power_refresh(operation, None)

        def call(path, interface, method, args, done):
            if not live():
                return
            try:
                remote = self.proxy(self.system_bus, operation['owner'], path, interface)
                remaining = operation['deadline'] - time.monotonic()
                if remaining <= 0:
                    self.finish_power_refresh(operation, None)
                    return
                getattr(remote, method)(
                    *args, timeout=min(RECOVERY_CALL_TIMEOUT, remaining),
                    reply_handler=lambda value: done(value) if live() else None,
                    error_handler=fail)
            except (dbus.DBusException, OSError, TypeError, ValueError) as error:
                fail(error)

        def commit_if_complete():
            if not live() or operation['pending_devices'] or operation['next_device'] < len(operation['devices']):
                return
            if operation['validating_owner']:
                return
            operation['validating_owner'] = True

            values = operation['values']
            batteries = operation['batteries']
            try:
                states = []
                for props in batteries:
                    state = dbus_uint32(props['State'])
                    states.append('discharging' if state == 2 else
                                  'not-discharging' if state in (1, 4, 5) else 'unknown')
                discharge = ('discharging' if 'discharging' in states else
                             'not-discharging' if states and all(s == 'not-discharging' for s in states)
                             else 'unknown')
                on_battery = dbus_boolean(values['OnBattery'])
                lid = dbus_boolean(values['LidIsClosed']) if dbus_boolean(values['LidIsPresent']) else None
                if len(batteries) > 1:
                    def got_display_percentage(value):
                        percent = valid_percent(value)
                        if percent is None:
                            fail('UPower display battery percentage is missing or malformed')
                            return
                        validate_owner((on_battery, lid, percent, discharge))

                    def got_display(path):
                        call(path, 'org.freedesktop.DBus.Properties', 'Get',
                             ('org.freedesktop.UPower.Device', 'Percentage'),
                             got_display_percentage)
                    call(UPOWER_PATH, UPOWER, 'GetDisplayDevice', (), got_display)
                else:
                    percent = valid_percent(batteries[0].get('Percentage')) if batteries else None
                    if batteries and percent is None:
                        fail('UPower battery percentage is missing or malformed')
                        return
                    validate_owner((on_battery, lid, percent, discharge))
            except (TypeError, ValueError, KeyError, AttributeError) as error:
                fail(error)

        def validate_owner(snapshot):
            if not live():
                return
            try:
                names = self.proxy(self.system_bus, 'org.freedesktop.DBus',
                                   '/org/freedesktop/DBus', 'org.freedesktop.DBus')
                remaining = operation['deadline'] - time.monotonic()
                if remaining <= 0:
                    self.finish_power_refresh(operation, None)
                    return
                names.GetNameOwner(UPOWER, timeout=min(RECOVERY_CALL_TIMEOUT, remaining),
                    reply_handler=lambda owner: commit_for_owner(owner, snapshot) if live() else None,
                    error_handler=fail)
            except (dbus.DBusException, OSError, TypeError, ValueError) as error:
                fail(error)

        def commit_for_owner(owner, snapshot):
            if str(owner) != operation['owner']:
                fail('UPower owner changed during refresh')
                return
            self.finish_power_refresh(operation, snapshot)

        def pump_devices():
            if not live():
                return
            while (operation['pending_devices'] < POWER_DEVICE_CONCURRENCY and
                   operation['next_device'] < len(operation['devices'])):
                path = operation['devices'][operation['next_device']]
                operation['next_device'] += 1
                operation['pending_devices'] += 1
                def received(props):
                    if not live():
                        return
                    try:
                        kind = dbus_uint32(props['Type'])
                        supply = dbus_boolean(props['PowerSupply'])
                        present = dbus_boolean(props['IsPresent'])
                        if kind == 2 and supply and present:
                            operation['batteries'].append(props)
                    except (TypeError, ValueError, AttributeError, KeyError) as error:
                        fail(error)
                        return
                    operation['pending_devices'] -= 1
                    pump_devices()
                call(path, 'org.freedesktop.DBus.Properties', 'GetAll',
                     ('org.freedesktop.UPower.Device',), received)
            commit_if_complete()

        def got_devices(devices):
            if not live():
                return
            try:
                operation['devices'] = upower_device_list(devices)
            except (TypeError, ValueError) as error:
                fail(error)
                return
            pump_devices()

        def read_property(index=0):
            if not live():
                return
            keys = ('OnBattery', 'LidIsPresent', 'LidIsClosed')
            if index == len(keys):
                call(UPOWER_PATH, UPOWER, 'EnumerateDevices', (), got_devices)
                return
            key = keys[index]
            if key == 'LidIsClosed':
                try:
                    if not dbus_boolean(operation['values']['LidIsPresent']):
                        read_property(index + 1)
                        return
                except (KeyError, ValueError) as error:
                    fail(error)
                    return
            call(UPOWER_PATH, 'org.freedesktop.DBus.Properties', 'Get', (UPOWER, key),
                 lambda value: (operation['values'].__setitem__(key, value), read_property(index + 1)))

        try:
            names = self.proxy(self.system_bus, 'org.freedesktop.DBus',
                               '/org/freedesktop/DBus', 'org.freedesktop.DBus')
            remaining = operation['deadline'] - time.monotonic()
            if remaining <= 0:
                self.finish_power_refresh(operation, None)
                return
            names.GetNameOwner(UPOWER, timeout=min(RECOVERY_CALL_TIMEOUT, remaining),
                reply_handler=lambda owner: (operation.__setitem__('owner', str(owner)),
                                             read_property()) if live() else None,
                error_handler=fail)
        except (dbus.DBusException, OSError, TypeError, ValueError) as error:
            fail(error)

    def handle_lid_change(self):
        if self.lid_closed is False:
            if self.deadline and self.require_lid:
                self.cancel_timer("Timer canceled because the lid opened")
            self.reconcile_brightness_async('lid-open')
        elif self.lid_closed is True:
            self.reconcile_brightness_async('lid-close')

    def acquire(self):
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        if self.session_cookie_state != 'absent' or self.session_cookie is not None:
            message = 'GNOME inhibitor release is pending; cannot acquire a duplicate inhibitor'
            self.last_error = message
            self.publish()
            raise dbus.DBusException(message, name=IFACE + '.ReleasePending')
        gnome_call_started = False
        cookie_received = False
        try:
            owner_before = self.session_manager_owner()
            names = self.proxy(self.system_bus, 'org.freedesktop.DBus',
                               '/org/freedesktop/DBus', 'org.freedesktop.DBus')
            login_owner = str(names.GetNameOwner(LOGIN, timeout=RECOVERY_CALL_TIMEOUT))
            login = self.proxy(self.system_bus, login_owner, LOGIN_PATH,
                               "org.freedesktop.login1.Manager")
            fd = login.Inhibit("sleep", "Sleep Disabler", "Keep this session awake", "block",
                               timeout=RECOVERY_CALL_TIMEOUT).take()
            if type(fd) is not int or fd < 0:
                raise ValueError('login1 returned an invalid sleep inhibitor FD')
            self.fd = fd
            self.sleep_fd_owner = login_owner
            gnome = self.proxy(self.session_bus, owner_before, SESSION_PATH, SESSION)
            gnome_call_started = True
            cookie = gnome.Inhibit(APP, dbus.UInt32(0), "Keep this session active",
                                   dbus.UInt32(12), timeout=RECOVERY_CALL_TIMEOUT)
            self.session_cookie = dbus_uint32(cookie)
            cookie_received = True
            self.session_cookie_owner = owner_before
            self.session_cookie_state = 'held'
            self.inhibitor_outcome = 'held'
            owner_after = self.session_manager_owner()
            if owner_after != owner_before:
                raise dbus.DBusException('GNOME SessionManager changed during inhibitor acquisition')
            if str(names.GetNameOwner(LOGIN, timeout=RECOVERY_CALL_TIMEOUT)) != login_owner:
                raise dbus.DBusException('login1 changed during inhibitor acquisition')
            self.session_owner_generation += 1
            self.inhibitor_outcome = 'held'
            self.release_attempts = 0
            self.enabled = True
            self.last_error = ""
            if self.lid_mode:
                self.retry_lid_lock()
            self.handle_lid_change()
            self.publish()
            return
        except (dbus.DBusException, OSError, TypeError, ValueError, OverflowError) as error:
            failure = str(error)
            self.last_error = failure
            released = self.release()
            self.last_error = failure if released else (
                'GNOME inhibitor release uncertain: ' + failure + '; cleanup: ' + self.last_error)
            if gnome_call_started and not cookie_received:
                self.desired = False
                self.disconnect_session_bus(
                    'GNOME inhibitor acquisition failed with an unknown remote outcome')
            raise dbus.DBusException(self.last_error, name=IFACE + ".Unavailable")

    def cancel_prevention_acquisition(self, reason):
        operation = self.prevention_acquisition
        if operation is not None:
            operation['fail'](dbus.DBusException(reason, name=IFACE + '.Canceled'))

    def acquire_async(self, callback):
        """Acquire the two inhibitors under one deadline, retaining late-resource ownership."""
        if self.shutting_down or not self.session_bus.get_is_connected():
            callback(dbus.DBusException('Agent is unavailable', name=IFACE + '.Unavailable'))
            return
        if self.prevention_acquisition is not None:
            if len(self.prevention_acquisition['waiters']) >= MAX_PUBLIC_WAITERS:
                callback(dbus.DBusException('Too many pending prevention acquisitions',
                                            name=IFACE + '.Busy'))
            else:
                self.prevention_acquisition['waiters'].append(callback)
            return
        if (self.prevention_release is not None or self.session_cookie_state != 'absent' or
                self.session_cookie is not None or self.fd is not None or self.enabled):
            callback(dbus.DBusException('GNOME inhibitor ownership is unresolved',
                                        name=IFACE + '.ReleasePending'))
            return
        operation = {'waiters': [callback], 'deadline': time.monotonic() + ACQUISITION_TIMEOUT,
                     'source': 0, 'fd': None, 'cookie': None, 'session_owner': '',
                     'login_owner': '', 'gnome_dispatched': False,
                     'session_generation': self.session_owner_generation,
                     'login_generation': self.logind_owner_generation}
        def close_fd(fd):
            if type(fd) is int and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass

        def finish_waiters(failure):
            for waiter in operation['waiters']:
                try:
                    waiter(failure)
                except Exception:
                    LOG.exception('Prevention acquisition continuation failed')

        def fail(failure):
            if self.prevention_acquisition is not operation:
                return
            self.prevention_acquisition = None
            GLib.source_remove(operation['source'])
            close_fd(operation['fd'])
            operation['fd'] = None
            message = str(failure)
            cancelled = self.dbus_error_name(failure) == IFACE + '.Canceled'
            if not cancelled:
                self.last_error = message
            if operation['cookie'] is not None:
                if not self.session_bus.get_is_connected():
                    self.clear_session_cookie('session-bus-disconnected')
                    finish_waiters(dbus.DBusException(message, name=IFACE + '.Unavailable'))
                    return
                # A received cookie is known ownership even if owner validation
                # failed. Keep its issuing owner and await confirmed cleanup.
                self.session_cookie = operation['cookie']
                self.session_cookie_owner = operation['session_owner']
                self.session_cookie_state = 'held'
                self.inhibitor_outcome = 'held'
                def cleaned(released):
                    if not released:
                        self.last_error = ('GNOME inhibitor release uncertain: ' + message)
                    else:
                        self.last_error = '' if cancelled else message
                    finish_waiters(dbus.DBusException(message if cancelled else self.last_error,
                        name=IFACE + ('.Unavailable' if released else '.ReleasePending')))
                self.release_async(cleaned)
                return
            if operation['gnome_dispatched']:
                # A timed-out/failed Inhibit can still have installed a cookie
                # whose value never reached this process. If a release already
                # owns brightness restoration, let it close the bus afterward.
                self.desired = False
                if self.prevention_release is not None:
                    self.prevention_release['unknown_acquisition'] = True
                    self.prevention_release['force_disconnect'] = True
                finish_waiters(dbus.DBusException(message, name=IFACE + '.Unavailable'))
                if self.prevention_release is None and self.session_bus.get_is_connected():
                    self.disconnect_session_bus(
                        'GNOME inhibitor acquisition ended with an unknown remote outcome')
                return
            self.inhibitor_outcome = 'absent' if cancelled else 'failed'
            if not self.shutting_down:
                try:
                    self.publish()
                except Exception:
                    LOG.exception('Prevention acquisition failure publication failed')
            finish_waiters(dbus.DBusException(message, name=IFACE + '.Unavailable'))

        operation['fail'] = fail
        try:
            operation['source'] = GLib.timeout_add(ACQUISITION_TIMEOUT * 1000, lambda: (
                fail(TimeoutError('Prevention acquisition exceeded its deadline')),
                GLib.SOURCE_REMOVE)[1])
            if not operation['source']:
                raise RuntimeError('GLib did not attach the acquisition deadline source')
        except Exception as setup_error:
            callback(dbus.DBusException('Prevention acquisition deadline setup failed: ' +
                                        str(setup_error), name=IFACE + '.Unavailable'))
            return
        self.prevention_acquisition = operation
        self.inhibitor_outcome = 'acquiring'
        try:
            self.publish()
        except Exception:
            LOG.exception('Prevention acquisition-start publication failed')

        def live():
            return (self.prevention_acquisition is operation and not self.shutting_down and
                    self.desired and self.session_owner_generation == operation['session_generation'] and
                    self.logind_owner_generation == operation['login_generation'] and
                    time.monotonic() < operation['deadline'])

        def remaining():
            return min(RECOVERY_CALL_TIMEOUT, operation['deadline'] - time.monotonic())

        def lookup(bus, name, done):
            if not live():
                fail(dbus.DBusException('Prevention acquisition was superseded',
                                        name=IFACE + '.Canceled'))
                return
            try:
                timeout = remaining()
                if timeout <= 0:
                    raise TimeoutError('Prevention acquisition exceeded its deadline')
                self.proxy(bus, 'org.freedesktop.DBus', '/org/freedesktop/DBus',
                           'org.freedesktop.DBus').GetNameOwner(
                    name, timeout=timeout, reply_handler=done, error_handler=fail)
            except Exception as error:
                fail(error)

        def session_confirmed(owner):
            if not live():
                return
            if str(owner) != operation['session_owner']:
                fail(dbus.DBusException('GNOME SessionManager changed during acquisition',
                                        name=IFACE + '.Unavailable'))
                return
            def login_confirmed(login_owner):
                if not live():
                    return
                if str(login_owner) != operation['login_owner']:
                    fail(dbus.DBusException('login1 changed during acquisition',
                                            name=IFACE + '.Unavailable'))
                    return
                self.prevention_acquisition = None
                GLib.source_remove(operation['source'])
                self.fd = operation['fd']
                self.sleep_fd_owner = operation['login_owner']
                operation['fd'] = None
                self.session_cookie = operation['cookie']
                self.session_cookie_owner = operation['session_owner']
                self.session_cookie_state = 'held'
                self.inhibitor_outcome = 'held'
                self.session_owner_generation += 1
                self.release_attempts = 0
                self.enabled = True
                self.last_error = ''
                try:
                    if self.lid_mode:
                        self.retry_lid_lock()
                    self.handle_lid_change()
                    self.publish()
                except Exception:
                    LOG.exception('Post-acquisition reconciliation failed')
                finish_waiters(None)
            lookup(self.system_bus, LOGIN, login_confirmed)

        def cookie_received(cookie):
            try:
                cookie = dbus_uint32(cookie)
            except ValueError:
                if self.prevention_acquisition is operation:
                    fail(ValueError('GNOME inhibitor returned a malformed cookie'))
                return
            if self.prevention_acquisition is not operation:
                # Cancellation already closed the session connection; do not
                # attach a late cookie to a newer operation.
                return
            operation['cookie'] = cookie
            if not live():
                fail(dbus.DBusException('Prevention acquisition was superseded',
                                        name=IFACE + '.Canceled'))
                return
            lookup(self.session_bus, SESSION, session_confirmed)

        def login_received(returned):
            try:
                fd = returned.take()
            except (AttributeError, OSError, TypeError) as error:
                if self.prevention_acquisition is operation:
                    fail(error)
                return
            if type(fd) is not int or fd < 0:
                if self.prevention_acquisition is operation:
                    fail(ValueError('login1 returned an invalid sleep inhibitor FD'))
                return
            if not live():
                close_fd(fd)
                if self.prevention_acquisition is operation:
                    fail(dbus.DBusException('Prevention acquisition was superseded',
                                            name=IFACE + '.Canceled'))
                return
            operation['fd'] = fd
            try:
                timeout = remaining()
                if timeout <= 0:
                    raise TimeoutError('Prevention acquisition exceeded its deadline')
                gnome = self.proxy(self.session_bus, operation['session_owner'],
                                   SESSION_PATH, SESSION)
                flags = dbus.UInt32(0)
                modes = dbus.UInt32(12)
                operation['gnome_dispatched'] = True
                gnome.Inhibit(APP, flags, 'Keep this session active', modes, timeout=timeout,
                              reply_handler=cookie_received, error_handler=fail)
            except Exception as error:
                fail(error)

        def login_resolved(owner):
            if not live():
                return
            operation['login_owner'] = str(owner)
            try:
                timeout = remaining()
                if timeout <= 0:
                    raise TimeoutError('Prevention acquisition exceeded its deadline')
                self.proxy(self.system_bus, operation['login_owner'], LOGIN_PATH,
                           'org.freedesktop.login1.Manager').Inhibit(
                    'sleep', 'Sleep Disabler', 'Keep this session awake', 'block',
                    timeout=timeout, reply_handler=login_received, error_handler=fail)
            except Exception as error:
                fail(error)

        def session_resolved(owner):
            if not live():
                return
            operation['session_owner'] = str(owner)
            lookup(self.system_bus, LOGIN, login_resolved)

        lookup(self.session_bus, SESSION, session_resolved)

    def finish_lid_acquisition(self, operation, fd=None, failure=None):
        if self.lid_acquisition is not operation:
            if type(fd) is int and fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return
        self.lid_acquisition = None
        GLib.source_remove(operation['source'])
        if fd is None:
            fd = operation.pop('fd', None)
        if failure is None and time.monotonic() >= operation['deadline']:
            failure = dbus.DBusException('login1 lid acquisition exceeded its deadline',
                                         name=IFACE + '.Unavailable')
        if (failure is None and (self.shutting_down or not self.enabled or
                operation['generation'] != self.logind_owner_generation)):
            failure = dbus.DBusException('login1 changed during lid acquisition',
                                         name=IFACE + '.Unavailable')
        if failure is None and (type(fd) is not int or fd < 0):
            failure = dbus.DBusException('login1 returned an invalid lid inhibitor FD',
                                         name=IFACE + '.Unavailable')
        if failure is None:
            self.lid_fd = fd
            self.lid_outcome = 'unverified'
            self.set_lid_error('')
        elif type(fd) is int and fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        for callback in operation['waiters']:
            try:
                callback(failure)
            except Exception:
                LOG.exception('Lid acquisition continuation failed')

    def cancel_lid_acquisition(self, reason):
        if self.lid_acquisition is not None:
            self.finish_lid_acquisition(self.lid_acquisition, failure=dbus.DBusException(
                reason, name=IFACE + '.Unavailable'))

    def acquire_lid_async(self, callback):
        if self.shutting_down or not self.enabled:
            callback(dbus.DBusException('Prevention is unavailable for lid lock',
                                        name=IFACE + '.Unavailable'))
            return
        if self.lid_fd is not None:
            callback(None)
            return
        if self.lid_acquisition is not None:
            if len(self.lid_acquisition['waiters']) >= MAX_PUBLIC_WAITERS:
                callback(dbus.DBusException('Too many pending lid acquisition requests',
                                            name=IFACE + '.Busy'))
                return
            self.lid_acquisition['waiters'].append(callback)
            return
        operation = {'generation': self.logind_owner_generation, 'waiters': [callback],
                     'source': 0, 'fd': None, 'deadline': time.monotonic() + ACQUISITION_TIMEOUT}
        try:
            operation['source'] = GLib.timeout_add(ACQUISITION_TIMEOUT * 1000, lambda: (
                self.finish_lid_acquisition(operation, failure=dbus.DBusException(
                    'login1 lid acquisition timed out', name=IFACE + '.Unavailable')),
                GLib.SOURCE_REMOVE)[1])
            if not operation['source']:
                raise RuntimeError('Lid acquisition deadline source unavailable')
        except Exception as failure:
            callback(failure)
            return
        self.lid_acquisition = operation

        def remaining():
            return min(RECOVERY_CALL_TIMEOUT, operation['deadline'] - time.monotonic())

        def failed(failure):
            self.finish_lid_acquisition(operation, failure=failure)

        def owner_confirmed(owner):
            if self.lid_acquisition is not operation:
                return
            if str(owner) != operation['owner']:
                failed(dbus.DBusException('login1 owner changed during lid acquisition',
                                          name=IFACE + '.Unavailable'))
                return
            self.finish_lid_acquisition(operation)

        def received(returned):
            try:
                fd = returned.take()
            except (AttributeError, OSError, TypeError) as take_error:
                self.finish_lid_acquisition(operation, failure=take_error)
                return
            if self.lid_acquisition is not operation:
                self.finish_lid_acquisition(operation, fd=fd)
                return
            operation['fd'] = fd
            try:
                timeout = remaining()
                if timeout <= 0:
                    raise TimeoutError('login1 lid owner check exceeded its deadline')
                names.GetNameOwner(LOGIN, timeout=timeout, reply_handler=owner_confirmed,
                                   error_handler=failed)
            except (dbus.DBusException, OSError, TypeError, ValueError, TimeoutError) as owner_error:
                failed(owner_error)

        def owner_resolved(owner):
            if self.lid_acquisition is not operation:
                return
            if not str(owner).startswith(':'):
                failed(dbus.DBusException('login1 returned no unique owner',
                                          name=IFACE + '.Unavailable'))
                return
            operation['owner'] = str(owner)
            try:
                timeout = remaining()
                if timeout <= 0:
                    raise TimeoutError('login1 lid acquisition exceeded its deadline')
                login = self.proxy(self.system_bus, operation['owner'], LOGIN_PATH,
                                   'org.freedesktop.login1.Manager')
                login.Inhibit('handle-lid-switch', 'Sleep Disabler', 'Test lid stay-awake', 'block',
                              timeout=timeout, reply_handler=received, error_handler=failed)
            except (dbus.DBusException, OSError, TypeError, ValueError, TimeoutError) as inhibit_error:
                failed(inhibit_error)

        try:
            names = self.proxy(self.system_bus, 'org.freedesktop.DBus',
                               '/org/freedesktop/DBus', 'org.freedesktop.DBus')
            timeout = remaining()
            if timeout <= 0:
                raise TimeoutError('login1 lid owner lookup exceeded its deadline')
            names.GetNameOwner(LOGIN, timeout=timeout, reply_handler=owner_resolved,
                               error_handler=failed)
        except (dbus.DBusException, OSError, TypeError, ValueError, TimeoutError) as owner_error:
            failed(owner_error)

    def set_lid_error(self, message):
        previous = self.lid_error
        self.lid_error = message
        if not self.last_error or self.last_error == previous:
            self.last_error = message

    def retry_lid_lock(self):
        if (self.shutting_down or not (self.enabled and self.lid_mode) or
                self.lid_fd is not None or self.lid_acquisition is not None):
            return
        def finished(error):
            if self.shutting_down or not (self.enabled and self.lid_mode):
                return
            if error is None:
                self.set_lid_error('')
            else:
                self.lid_outcome = 'failed'
                message = 'Lid lock unavailable: ' + str(error)
                if self.lid_error != message:
                    LOG.warning('%s', message)
                self.set_lid_error(message)
            self.publish()
        self.acquire_lid_async(finished)

    def close_lid(self):
        self.cancel_lid_acquisition('Lid lock no longer requested')
        if self.lid_fd is not None:
            try:
                os.close(self.lid_fd)
            except OSError as error:
                self.last_error = "Could not close lid lock: " + str(error)
                LOG.error(self.last_error)
            self.lid_fd = None

    def clear_session_cookie(self, outcome):
        self.cancel_release_retry()
        operation = getattr(self, 'session_release_operation', None)
        if operation is not None:
            self.session_release_operation = None
            GLib.source_remove(operation['source'])
        self.session_cookie = None
        self.session_cookie_state = 'absent'
        self.session_cookie_owner = ''
        self.inhibitor_outcome = outcome
        self.release_attempts = 0
        self.release_retry_after = 0
        if self.last_error.startswith('GNOME inhibitor release uncertain:'):
            self.last_error = ''
        if operation is not None:
            for callback in operation['waiters']:
                try:
                    callback(True)
                except Exception:
                    LOG.exception('GNOME release continuation failed')

    def cancel_release_retry(self):
        source = getattr(self, 'release_retry_source', 0)
        if source:
            try:
                GLib.source_remove(source)
            except (AttributeError, TypeError):
                pass
            self.release_retry_source = 0

    def schedule_release_retry(self):
        if (self.shutting_down or getattr(self, 'release_retry_source', 0) or
                self.session_release_operation is not None or
                self.session_cookie_state != 'release-pending'):
            return
        try:
            source = GLib.timeout_add_seconds(RELEASE_RETRY_SECONDS, self.on_release_retry)
            if not source:
                raise RuntimeError('GLib did not attach the inhibitor-release retry source')
            self.release_retry_source = source
        except Exception as setup_error:
            self.disconnect_session_bus('GNOME inhibitor retry could not be scheduled: ' +
                                        str(setup_error))

    def on_release_retry(self):
        self.release_retry_source = 0
        if self.shutting_down:
            return False
        if self.session_cookie_state != 'release-pending':
            return False
        if self.session_release_operation is not None:
            return False
        def finished(released):
            if self.shutting_down:
                return
            if not released and self.release_attempts >= RELEASE_RETRY_LIMIT:
                self.disconnect_session_bus(
                    'GNOME inhibitor release remained uncertain after bounded retries')
            else:
                self.schedule_release_retry()
                self.publish()
        self.attempt_session_release_async(finished)
        return False

    def finish_session_release(self, operation, error=None, outcome=None):
        if self.session_release_operation is not operation:
            return
        self.session_release_operation = None
        GLib.source_remove(operation['source'])
        if outcome is not None:
            self.clear_session_cookie(outcome)
            released = True
        else:
            released = self.mark_release_pending(error)
        for callback in operation['waiters']:
            try:
                callback(released)
            except Exception:
                LOG.exception('GNOME release continuation failed')

    def attempt_session_release_async(self, callback):
        if self.session_cookie is None or self.session_cookie_state == 'absent':
            callback(True)
            return
        if self.session_release_operation is not None:
            if len(self.session_release_operation['waiters']) >= MAX_PUBLIC_WAITERS:
                callback(False)
                return
            self.session_release_operation['waiters'].append(callback)
            return
        operation = {'cookie': self.session_cookie, 'owner': self.session_cookie_owner,
                     'waiters': [callback],
                     'deadline': time.monotonic() + RELEASE_OPERATION_TIMEOUT, 'source': 0}
        try:
            operation['source'] = GLib.timeout_add(
                RELEASE_OPERATION_TIMEOUT * 1000,
                lambda: (self.finish_session_release(operation, error=TimeoutError(
                    'GNOME inhibitor release exceeded its deadline')), GLib.SOURCE_REMOVE)[1])
            if not operation['source']:
                raise RuntimeError('GLib did not attach the release deadline source')
        except Exception as setup_error:
            callback(self.mark_release_pending(setup_error))
            return
        self.session_release_operation = operation

        def live():
            return (self.session_release_operation is operation and
                    self.session_cookie == operation['cookie'] and
                    self.session_cookie_owner == operation['owner'] and
                    time.monotonic() < operation['deadline'])

        def failed(error):
            if self.session_release_operation is operation:
                self.finish_session_release(operation, error=error)

        def lookup_failed(error):
            if self.session_release_operation is not operation:
                return
            if self.dbus_error_name(error) == 'org.freedesktop.DBus.Error.NameHasNoOwner':
                self.finish_session_release(operation, outcome='owner-lost')
            else:
                failed(error)

        def released():
            if live():
                self.finish_session_release(operation, outcome='released')

        def owner_resolved(owner):
            if not live():
                return
            if str(owner) != operation['owner']:
                failed(ValueError('GNOME inhibitor issuing owner could not be verified'))
                return
            try:
                remaining = operation['deadline'] - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('GNOME inhibitor release exceeded its deadline')
                self.proxy(self.session_bus, operation['owner'], SESSION_PATH, SESSION).Uninhibit(
                    dbus.UInt32(operation['cookie']),
                    timeout=min(RECOVERY_CALL_TIMEOUT, remaining),
                    reply_handler=released, error_handler=failed)
            except (dbus.DBusException, OSError, TypeError, ValueError, TimeoutError) as error:
                failed(error)

        try:
            names = self.proxy(self.session_bus, 'org.freedesktop.DBus',
                               '/org/freedesktop/DBus', 'org.freedesktop.DBus')
            remaining = operation['deadline'] - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('GNOME inhibitor release exceeded its deadline')
            names.GetNameOwner(operation['owner'], timeout=min(RECOVERY_CALL_TIMEOUT, remaining),
                               reply_handler=owner_resolved, error_handler=lookup_failed)
        except (dbus.DBusException, OSError, TypeError, ValueError, TimeoutError) as error:
            lookup_failed(error)

    def attempt_session_release(self):
        if self.session_cookie is None or self.session_cookie_state == 'absent':
            return True
        if self.session_release_operation is not None:
            return False
        try:
            current_owner = str(self.proxy(self.session_bus, 'org.freedesktop.DBus',
                '/org/freedesktop/DBus', 'org.freedesktop.DBus').GetNameOwner(
                    self.session_cookie_owner, timeout=RECOVERY_CALL_TIMEOUT))
        except dbus.DBusException as error:
            if self.dbus_error_name(error) == 'org.freedesktop.DBus.Error.NameHasNoOwner':
                self.clear_session_cookie('owner-lost')
                return True
            return self.mark_release_pending(error)
        if current_owner != self.session_cookie_owner:
            return self.mark_release_pending(ValueError('GNOME inhibitor issuing owner could not be verified'))
        try:
            self.proxy(self.session_bus, self.session_cookie_owner, SESSION_PATH, SESSION).Uninhibit(
                dbus.UInt32(self.session_cookie), timeout=RECOVERY_CALL_TIMEOUT)
        except dbus.DBusException as error:
            return self.mark_release_pending(error)
        self.clear_session_cookie('released')
        return True

    def mark_release_pending(self, error):
        self.session_cookie_state = 'release-pending'
        self.inhibitor_outcome = 'release-uncertain'
        self.release_attempts += 1
        self.release_retry_after = time.monotonic() + RELEASE_RETRY_SECONDS
        self.last_error = 'GNOME inhibitor release uncertain: ' + str(error)
        LOG.warning('%s', self.last_error)
        if self.release_attempts < RELEASE_RETRY_LIMIT:
            self.schedule_release_retry()
        return False

    def disconnect_session_bus(self, reason):
        self.shutting_down = True
        self.cancel_release_retry()
        self.cancel_stop_deadline()
        self.inhibitor_outcome = 'disconnect-requested'
        self.last_error = reason
        try:
            self.publish()
        except Exception:
            LOG.exception('Session-bus disconnect state publication failed')
        LOG.error('%s; closing the session bus to release the GNOME inhibitor', reason)
        try:
            self.session_bus.close()
            self.clear_session_cookie('disconnect-completed')
        except Exception as error:
            self.inhibitor_outcome = 'disconnect-failed'
            LOG.error('Session-bus disconnect failed: %s', error)
        for attr in ('lid_fd', 'fd'):
            fd = getattr(self, attr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, None)
        self.sleep_fd_owner = ''
        self.enabled = False
        self.desired = False
        self.exit_failure = True
        if getattr(self, 'loop', None) is not None:
            self.loop.quit()

    def on_session_bus_disconnected(self, *_args):
        """Drop local resources and exit so systemd can reconnect a fresh agent."""
        self.shutting_down = True
        self.cancel_stop_deadline()
        self.cancel_sleep_reconcile()
        self.cancel_suspend_attempt('Session bus disconnected during suspend attempt')
        self.cancel_failsafe_evaluation()
        self.cancel_suspend_preflight('Session bus disconnected during suspend preflight')
        for cancel in tuple(self.prevention_enable_requests):
            cancel(dbus.DBusException('Session bus disconnected', name=IFACE + '.Unavailable'))
        for cancel in tuple(self.prevention_disable_requests):
            cancel(dbus.DBusException('Session bus disconnected', name=IFACE + '.Unavailable'))
        self.cancel_brightness_probe()
        for cancel in tuple(self.dimming_verifications):
            cancel(dbus.DBusException('Session bus disconnected', name=IFACE + '.Unavailable'))
        for complete in tuple(self.dimming_disable_requests):
            complete('pending')
        # The transport cannot deliver the pending public replies. Retain any
        # durable journal and make late Get/Set callbacks inert before exit.
        for attr in ('brightness_dim_operation', 'brightness_restore_operation'):
            operation = getattr(self, attr)
            if operation is not None:
                setattr(self, attr, None)
                GLib.source_remove(operation['source'])
        try:
            self.cancel_prevention_acquisition('Session bus disconnected')
        except Exception:
            LOG.exception('Prevention acquisition cancellation failed after session bus loss')
        if self.lid_mode_request is not None:
            try:
                self.finish_lid_mode_request(self.lid_mode_request, dbus.DBusException(
                    'Session bus disconnected', name=IFACE + '.Unavailable'))
            except Exception:
                LOG.exception('Lid mode request cancellation failed after session bus loss')
        try:
            self.cancel_lid_acquisition('Session bus disconnected')
        except Exception:
            LOG.exception('Lid acquisition cancellation failed after session bus loss')
        for cancel in tuple(self.panel_registration_cancellers):
            try:
                cancel(dbus.DBusException('Session bus disconnected', name=IFACE + '.Unavailable'))
            except Exception:
                LOG.exception('Panel registration cancellation failed after session bus loss')
        if self.timer_start is not None:
            GLib.source_remove(self.timer_start['source'])
            self.timer_start = None
        self.cancel_power_refresh()
        # No signal publication or brightness traffic through the lost bus.
        if self.deadline is not None:
            self.deadline = None
            self.require_lid = False
            self.require_prevention = False
            self.timer_phase = 'canceled'
            self.timer_outcome = 'Timer canceled because the session bus disconnected'
            self.last_timer_state = -1
        self.cancel_release_retry()
        self.clear_session_cookie('session-bus-disconnected')
        for attr in ('lid_fd', 'fd'):
            fd = getattr(self, attr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, None)
        self.sleep_fd_owner = ''
        self.enabled = False
        self.desired = False
        self.last_error = 'Session bus disconnected; exiting for service restart'
        self.exit_failure = True
        if getattr(self, 'loop', None) is not None:
            self.loop.quit()

    def on_system_bus_disconnected(self, *_args):
        # Session brightness and GNOME release may still be available. Stop
        # restores before remote release and drops stale login1 inhibitor FDs.
        self.exit_failure = True
        self.last_error = 'System bus disconnected; exiting for service restart'
        self.stop()

    def release(self, force_disconnect=False, brightness_reconciled=False):
        # An off request must attempt to restore the panel while GNOME's
        # inhibitor is still held. Stop performs its own restoration first.
        if self.prevention_release is not None:
            self.last_error = 'GNOME inhibitor release is already in progress'
            return False
        if self.lid_mode_request is not None:
            self.finish_lid_mode_request(self.lid_mode_request, dbus.DBusException(
                'Prevention was released during lid mode request', name=IFACE + '.Canceled'))
        self.cancel_lid_acquisition('Prevention was released')
        if not brightness_reconciled and self.session_bus.get_is_connected():
            self.brightness_release_pending = True
            try:
                self.reconcile_brightness('prevention-disable')
            except Exception as error:
                LOG.exception('Brightness restoration failed during prevention release')
                self.brightness_error = 'Brightness restoration failed: ' + str(error)
        try:
            released = self.attempt_session_release()
        except (dbus.DBusException, OSError, TypeError, ValueError) as error:
            released = self.mark_release_pending(error)
        finally:
            for attr in ("lid_fd", "fd"):
                fd = getattr(self, attr)
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    setattr(self, attr, None)
            self.sleep_fd_owner = ''
            self.enabled = False
            self.brightness_release_pending = False
        self.publish()
        if not released and force_disconnect:
            self.disconnect_session_bus('GNOME inhibitor release remained uncertain during shutdown')
        return released

    def release_async(self, callback, *, force_disconnect=False, brightness_reconciled=False):
        """Restore first, then finish GNOME release without blocking the main loop."""
        if self.prevention_release is not None:
            if not force_disconnect and len(self.prevention_release['waiters']) >= MAX_PUBLIC_WAITERS:
                callback(False)
                return
            self.prevention_release['waiters'].append(callback)
            self.prevention_release['force_disconnect'] |= force_disconnect
            return
        operation = {'waiters': [callback], 'force_disconnect': force_disconnect,
                     'unknown_acquisition': False, 'done': False}
        self.prevention_release = operation
        try:
            self.cancel_prevention_acquisition('Prevention was released during acquisition')
        except Exception:
            LOG.exception('Prevention acquisition cancellation failed during release')
        if self.lid_mode_request is not None:
            try:
                self.finish_lid_mode_request(self.lid_mode_request, dbus.DBusException(
                    'Prevention was released during lid mode request', name=IFACE + '.Canceled'))
            except Exception:
                LOG.exception('Lid mode request cancellation failed during prevention release')
        try:
            self.cancel_lid_acquisition('Prevention was released')
        except Exception:
            LOG.exception('Lid acquisition cancellation failed during prevention release')
        needs_brightness_restore = (not brightness_reconciled and
                                    self.session_bus.get_is_connected())
        if needs_brightness_restore:
            self.brightness_release_pending = True
        # Once cleanup begins, the old pair must not be reported as healthy
        # or reused by a concurrent enable request.
        self.enabled = False
        if not self.shutting_down:
            try:
                self.publish()
            except Exception:
                LOG.exception('Prevention release-start publication failed')

        def finished(released):
            if self.prevention_release is not operation or operation['done']:
                return
            operation['done'] = True
            self.prevention_release = None
            released = released and not operation['unknown_acquisition']
            for attr in ('lid_fd', 'fd'):
                fd = getattr(self, attr)
                if fd is not None:
                    try:
                        os.close(fd)
                    except OSError:
                        pass
                    setattr(self, attr, None)
            self.sleep_fd_owner = ''
            self.enabled = False
            self.brightness_release_pending = False
            if not self.shutting_down:
                try:
                    self.publish()
                except Exception:
                    LOG.exception('Prevention release publication failed')
            if (not released and operation['force_disconnect'] and
                    self.session_bus.get_is_connected()):
                self.disconnect_session_bus('GNOME inhibitor release remained uncertain during shutdown')
            for waiter in operation['waiters']:
                try:
                    waiter(released)
                except Exception:
                    LOG.exception('Prevention release continuation failed')

        def begin_remote_release(_brightness_result='unchanged'):
            if self.prevention_release is not operation or operation['done']:
                return
            try:
                self.attempt_session_release_async(finished)
            except Exception as failure:
                LOG.exception('Asynchronous GNOME release setup failed')
                finished(self.mark_release_pending(failure))

        if needs_brightness_restore:
            def restore_after_dim(_dim_result='unchanged'):
                if self.prevention_release is not operation or operation['done']:
                    return
                try:
                    if self.brightness_record is None and self.brightness_journal_state != 'invalid':
                        self.recover_brightness()
                    self.restore_brightness_async(begin_remote_release)
                except Exception as failure:
                    LOG.exception('Brightness restoration failed during prevention release')
                    self.brightness_error = 'Brightness restoration failed: ' + str(failure)
                    begin_remote_release('pending')
            try:
                if self.brightness_dim_operation is not None:
                    self.brightness_dim_operation['waiters'].append(restore_after_dim)
                else:
                    restore_after_dim()
            except Exception as failure:
                LOG.exception('Brightness restoration failed during prevention release')
                self.brightness_error = 'Brightness restoration failed: ' + str(failure)
                begin_remote_release('pending')
        else:
            begin_remote_release()

    def retry_session_release(self):
        """Defensive backstop; the one-shot source owns normal retry timing."""
        if self.session_cookie_state != 'release-pending':
            return
        if self.session_release_operation is not None:
            return
        if getattr(self, 'release_retry_source', 0):
            return
        if time.monotonic() < self.release_retry_after:
            self.schedule_release_retry()
            return
        self.on_release_retry()

    def failsafe_power_condition_met(self):
        return (self.failsafe and self.on_battery is True and
                self.battery_discharge_state == 'discharging' and
                self.battery_percent is not None and self.battery_percent < self.threshold)

    def failsafe_condition_met(self):
        return self.enabled and self.failsafe_power_condition_met()

    def failsafe_condition_cleared(self):
        if not self.failsafe or self.on_battery is False:
            return True
        if self.battery_percent is None:
            return False
        return (self.battery_percent >= self.threshold or
                (self.on_battery is True and
                 self.battery_discharge_state == 'not-discharging'))

    def cancel_failsafe_evaluation(self):
        evaluation = self.failsafe_evaluation
        if evaluation is not None:
            evaluation['finish']('unavailable')

    def check_failsafe(self, callback=None):
        if self.failsafe_condition_cleared():
            self.failsafe_triggered = False
        if not self.failsafe_condition_met():
            if callback is not None:
                callback('clear' if self.failsafe_condition_cleared() else 'unavailable')
            return
        if (self.shutting_down or self.failsafe_triggered or
                self.sleep_tx['phase'] != 'idle' or self.suspend_attempt is not None):
            if callback is not None:
                callback('unavailable')
            return
        if self.failsafe_evaluation is not None:
            if callback is not None:
                if len(self.failsafe_evaluation['waiters']) >= MAX_PUBLIC_WAITERS:
                    callback('unavailable')
                    return
                self.failsafe_evaluation['waiters'].append(callback)
            return
        evaluation = {'waiters': [] if callback is None else [callback]}
        self.failsafe_evaluation = evaluation

        def live():
            return (self.failsafe_evaluation is evaluation and not self.shutting_down and
                    not self.failsafe_triggered and self.sleep_tx['phase'] == 'idle' and
                    self.failsafe_condition_met())

        def finish(outcome='unavailable'):
            if self.failsafe_evaluation is evaluation:
                self.failsafe_evaluation = None
                for waiter in evaluation['waiters']:
                    try:
                        waiter(outcome)
                    except Exception:
                        LOG.exception('Failsafe evaluation continuation failed')

        evaluation['finish'] = finish

        def ended():
            finish('clear' if not self.failsafe_condition_met() else 'unavailable')

        def final_preflight(allowed, _reason, _owner):
            if not live() or not allowed:
                ended()
                return
            self.failsafe_triggered = True
            try:
                self.sleep_now('Battery fell below the failsafe threshold')
            finally:
                finish('triggered')

        def fresh_power(available):
            if not live() or not available:
                ended()
                return
            try:
                self.suspend_preflight_async(final_preflight)
            except Exception:
                ended()
                LOG.exception('Final failsafe preflight setup failed')

        def initial_preflight(allowed, _reason, _owner):
            if not live() or not allowed:
                ended()
                return
            # The first preflight can take several seconds. Its power snapshot
            # cannot authorize the later irreversible request.
            try:
                self.cancel_power_refresh()
                self.refresh_power_async(callback=fresh_power, reconcile_lid=False)
            except Exception:
                ended()
                LOG.exception('Fresh failsafe power read setup failed')

        try:
            self.suspend_preflight_async(initial_preflight)
        except Exception:
            ended()
            LOG.exception('Initial failsafe preflight setup failed')

    def finish_suspend_preflight(self, operation, allowed, reason='', login_owner=''):
        if self.suspend_preflight_operation is not operation:
            return
        self.suspend_preflight_operation = None
        GLib.source_remove(operation['source'])
        for callback in operation['waiters']:
            try:
                callback(allowed, reason, login_owner if allowed else '')
            except Exception:
                LOG.exception('Suspend preflight continuation failed')

    def cancel_suspend_preflight(self, reason):
        operation = self.suspend_preflight_operation
        if operation is not None:
            self.finish_suspend_preflight(operation, False, reason)

    def suspend_preflight_async(self, callback, require_cookie_absent=False, deadline=None,
                                fresh_after=None):
        """Read fresh logind state and verify GNOME ownership under one deadline."""
        if deadline is not None and time.monotonic() >= deadline:
            callback(False, 'suspend preflight parent deadline elapsed', '')
            return
        if (self.shutting_down or self.sleep_tx['phase'] != 'idle' or
                self.prevention_acquisition is not None):
            callback(False, 'agent or sleep lifecycle is unavailable', '')
            return
        existing = self.suspend_preflight_operation
        if existing is not None:
            if existing['require_cookie_absent'] == require_cookie_absent:
                if fresh_after is not None and existing.get('started', float('-inf')) < fresh_after:
                    callback(False, 'pending suspend preflight predates this attempt', '')
                elif deadline is not None and deadline < existing['deadline']:
                    callback(False, 'pending suspend preflight exceeds parent deadline', '')
                elif len(existing['waiters']) >= MAX_PUBLIC_WAITERS:
                    callback(False, 'too many pending suspend preflights', '')
                else:
                    existing['waiters'].append(callback)
            else:
                callback(False, 'another suspend preflight is in progress', '')
            return
        started = time.monotonic()
        operation_deadline = started + ACQUISITION_TIMEOUT
        if deadline is not None:
            operation_deadline = min(operation_deadline, deadline)
        operation = {'source': 0, 'started': started, 'deadline': operation_deadline,
                     'waiters': [callback], 'require_cookie_absent': require_cookie_absent,
                     'login_generation': self.logind_owner_generation,
                     'session_generation': self.session_owner_generation,
                     'cookie': self.session_cookie,
                     'cookie_owner': self.session_cookie_owner,
                     'cookie_state': self.session_cookie_state}
        try:
            remaining_ms = int((operation_deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise TimeoutError('suspend preflight parent deadline elapsed')
            operation['source'] = GLib.timeout_add(remaining_ms,
                lambda: (self.finish_suspend_preflight(operation, False,
                         'suspend preflight exceeded its deadline'), GLib.SOURCE_REMOVE)[1])
            if not operation['source']:
                raise RuntimeError('GLib did not attach the suspend preflight deadline')
        except Exception as failure:
            callback(False, 'suspend preflight deadline setup failed: ' + str(failure), '')
            return
        self.suspend_preflight_operation = operation

        def live():
            return (self.suspend_preflight_operation is operation and not self.shutting_down and
                    self.prevention_acquisition is None and
                    self.sleep_tx['phase'] == 'idle' and
                    self.logind_owner_generation == operation['login_generation'] and
                    self.session_owner_generation == operation['session_generation'] and
                    self.session_cookie == operation['cookie'] and
                    self.session_cookie_owner == operation['cookie_owner'] and
                    self.session_cookie_state == operation['cookie_state'] and
                    time.monotonic() < operation['deadline'])

        def current():
            if not live():
                self.finish_suspend_preflight(operation, False,
                    'inhibitor or agent lifecycle changed during suspend preflight')
                return False
            return True

        def call_timeout():
            remaining = operation['deadline'] - time.monotonic()
            if remaining <= 0:
                raise TimeoutError('suspend preflight exceeded its deadline')
            return min(RECOVERY_CALL_TIMEOUT, remaining)

        def failed(failure):
            if self.suspend_preflight_operation is operation:
                self.finish_suspend_preflight(operation, False,
                    'suspend preflight D-Bus read failed: ' + str(failure))

        def confirm_login_owner(login_owner):
            if not current():
                return
            def confirmed(owner):
                if not current():
                    return
                if str(owner) != login_owner:
                    self.finish_suspend_preflight(operation, False,
                        'login1 owner changed during suspend preflight')
                    return
                if operation['cookie_state'] != 'absent':
                    # This pass only proves it is safe to release our own
                    # inhibitor. Check capability after release below.
                    self.finish_suspend_preflight(operation, True, '', login_owner)
                    return
                def capability_received(capability):
                    if not current():
                        return
                    if str(capability) != 'yes':
                        self.finish_suspend_preflight(operation, False,
                            'login1 cannot suspend without interaction: ' + str(capability))
                        return
                    def final_owner(owner_after):
                        if not current():
                            return
                        if str(owner_after) != login_owner:
                            self.finish_suspend_preflight(operation, False,
                                'login1 owner changed after suspend capability check')
                        else:
                            self.finish_suspend_preflight(operation, True, '', login_owner)
                    try:
                        self.proxy(self.system_bus, 'org.freedesktop.DBus',
                                   '/org/freedesktop/DBus', 'org.freedesktop.DBus').GetNameOwner(
                            LOGIN, timeout=call_timeout(), reply_handler=final_owner,
                            error_handler=failed)
                    except Exception as failure:
                        failed(failure)
                try:
                    self.proxy(self.system_bus, login_owner, LOGIN_PATH,
                               'org.freedesktop.login1.Manager').CanSuspend(
                        timeout=call_timeout(), reply_handler=capability_received,
                        error_handler=failed)
                except Exception as failure:
                    failed(failure)
            try:
                self.proxy(self.system_bus, 'org.freedesktop.DBus',
                           '/org/freedesktop/DBus', 'org.freedesktop.DBus').GetNameOwner(
                    LOGIN, timeout=call_timeout(), reply_handler=confirmed,
                    error_handler=failed)
            except Exception as failure:
                failed(failure)

        def session_owner_received(owner, login_owner):
            if not current():
                return
            if str(owner) != operation['cookie_owner']:
                self.finish_suspend_preflight(operation, False,
                    'GNOME inhibitor belongs to a previous SessionManager owner')
                return
            confirm_login_owner(login_owner)

        def preparing_received(value, login_owner):
            if not current():
                return
            try:
                preparing = dbus_boolean(value)
            except ValueError as failure:
                failed(failure)
                return
            if preparing:
                self.finish_suspend_preflight(operation, False, 'logind is preparing for sleep')
                return
            if require_cookie_absent:
                if operation['cookie_state'] != 'absent' or operation['cookie'] is not None:
                    self.finish_suspend_preflight(operation, False,
                        'GNOME inhibitor release is not confirmed')
                else:
                    confirm_login_owner(login_owner)
                return
            if operation['cookie_state'] == 'absent' and operation['cookie'] is None:
                confirm_login_owner(login_owner)
                return
            if (operation['cookie_state'] != 'held' or operation['cookie'] is None or
                    not operation['cookie_owner']):
                self.finish_suspend_preflight(operation, False,
                    'GNOME inhibitor ownership is unresolved')
                return
            try:
                self.proxy(self.session_bus, 'org.freedesktop.DBus',
                           '/org/freedesktop/DBus', 'org.freedesktop.DBus').GetNameOwner(
                    SESSION, timeout=call_timeout(),
                    reply_handler=lambda owner: session_owner_received(owner, login_owner),
                    error_handler=failed)
            except Exception as failure:
                failed(failure)

        def login_owner_received(owner):
            if not current():
                return
            login_owner = str(owner)
            try:
                self.proxy(self.system_bus, login_owner, LOGIN_PATH,
                           'org.freedesktop.DBus.Properties').Get(
                    'org.freedesktop.login1.Manager', 'PreparingForSleep',
                    timeout=call_timeout(),
                    reply_handler=lambda value: preparing_received(value, login_owner),
                    error_handler=failed)
            except Exception as failure:
                failed(failure)

        try:
            self.proxy(self.system_bus, 'org.freedesktop.DBus',
                       '/org/freedesktop/DBus', 'org.freedesktop.DBus').GetNameOwner(
                LOGIN, timeout=call_timeout(), reply_handler=login_owner_received,
                error_handler=failed)
        except Exception as failure:
            failed(failure)

    @staticmethod
    def dbus_error_name(error):
        getter = getattr(error, 'get_dbus_name', None)
        if getter:
            try:
                return str(getter())
            except Exception:
                pass
        return str(getattr(error, 'name', '') or '')

    def classify_suspend_error(self, error):
        return 'rejected' if self.dbus_error_name(error) in DEFINITE_SUSPEND_REJECTIONS else 'unknown'

    def cancel_suspend_attempt(self, reason):
        operation = self.suspend_attempt
        if operation is not None:
            operation['finish'](reason)

    def sleep_now(self, reason):
        """Consume one automatic trigger and stage its bounded async request."""
        if self.shutting_down or self.sleep_tx['phase'] != 'idle' or self.suspend_attempt is not None:
            return False
        origin = 'timer' if reason == 'Countdown elapsed' else 'failsafe'
        timer_require_lid = origin == 'timer' and self.require_lid
        timer_require_prevention = origin == 'timer' and self.require_prevention
        if origin == 'failsafe':
            self.failsafe_triggered = True
        else:
            self.consume_timer('Countdown elapsed while awake; evaluating suspend')
        operation_started = time.monotonic()
        operation = {'source': 0, 'started': operation_started,
                     'deadline': operation_started + SUSPEND_ATTEMPT_TIMEOUT,
                     'origin': origin,
                     'login_generation': self.logind_owner_generation,
                     'session_generation': self.session_owner_generation,
                     'released': False}

        def finish(why=''):
            if self.suspend_attempt is not operation:
                return
            self.suspend_attempt = None
            GLib.source_remove(operation['source'])
            if not why or self.shutting_down or self.sleep_tx['phase'] != 'idle':
                return
            prefix = ('Suspend not requested after inhibitor release: ' if operation['released']
                      else 'Suspend not requested: ')
            self.last_error = prefix + why
            if origin == 'timer':
                self.timer_outcome = 'Countdown consumed; ' + self.last_error.lower()
            self.notify('Sleep Disabler', self.last_error)
            self.publish()

        operation['finish'] = finish
        try:
            operation['source'] = GLib.timeout_add(SUSPEND_ATTEMPT_TIMEOUT * 1000,
                lambda: (finish('suspend attempt exceeded its deadline'), GLib.SOURCE_REMOVE)[1])
            if not operation['source']:
                raise RuntimeError('GLib did not attach the suspend attempt deadline')
        except Exception as failure:
            self.last_error = 'Suspend not requested: suspend attempt deadline unavailable: ' + str(failure)
            if origin == 'timer':
                self.timer_outcome = 'Countdown consumed; ' + self.last_error.lower()
            self.publish()
            return False
        self.suspend_attempt = operation

        def live():
            return (self.suspend_attempt is operation and not self.shutting_down and
                    self.sleep_tx['phase'] == 'idle' and
                    self.logind_owner_generation == operation['login_generation'] and
                    self.session_owner_generation == operation['session_generation'] and
                    time.monotonic() < operation['deadline'])

        def require_live():
            if live():
                return True
            finish('agent, owner, or sleep lifecycle changed during suspend attempt')
            return False

        def start_preflight(callback, require_cookie_absent=False):
            try:
                self.suspend_preflight_async(callback, require_cookie_absent=require_cookie_absent,
                                             deadline=operation['deadline'],
                                             fresh_after=operation['started'])
            except Exception as failure:
                finish('suspend preflight setup failed: ' + str(failure))

        def start_power(callback):
            try:
                self.cancel_power_refresh()
                self.refresh_power_async(callback=callback, reconcile_lid=False,
                                         deadline=operation['deadline'])
            except Exception as failure:
                finish('fresh power read setup failed: ' + str(failure))

        def dispatch(login_owner):
            if not require_live():
                return
            if origin == 'failsafe' and not self.failsafe_power_condition_met():
                finish('failsafe power conditions changed during final preflight')
                return
            if timer_require_lid and self.lid_closed is not True:
                finish('timer requires a confirmed closed lid')
                return
            LOG.info('Requesting suspend: %s', reason)
            self.last_error = ''
            finish()
            transaction = self.begin_sleep_request(origin, reason)
            try:
                self.publish()
            except Exception:
                LOG.exception('Suspend request publication failed')
            try:
                self.proxy(self.system_bus, login_owner, LOGIN_PATH,
                           'org.freedesktop.login1.Manager').Suspend(
                    False, timeout=RECOVERY_CALL_TIMEOUT,
                    reply_handler=lambda: self.finish_suspend_request(transaction, origin),
                    error_handler=lambda error: self.finish_suspend_request(transaction, origin, error))
            except Exception as failure:
                # A transport or marshalling failure can arrive after the
                # request was handed to D-Bus. Keep its outcome unknown.
                self.finish_suspend_request(transaction, origin, failure)

        def final_preflight(allowed, why, login_owner):
            if not require_live():
                return
            if not allowed:
                finish(why)
            else:
                dispatch(login_owner)

        def after_release_power(available):
            if not require_live():
                return
            if not available:
                finish('fresh power state is unavailable')
            elif origin == 'failsafe' and not self.failsafe_power_condition_met():
                finish('failsafe power conditions no longer confirmed')
            elif timer_require_lid and self.lid_closed is not True:
                finish('timer requires a confirmed closed lid')
            else:
                start_preflight(final_preflight, require_cookie_absent=True)

        def released(confirmed):
            if not require_live():
                return
            if not confirmed:
                finish('GNOME inhibitor release is uncertain')
                return
            operation['released'] = True
            if origin == 'failsafe' or timer_require_lid:
                # Remote release may have outlived the prior power evidence.
                start_power(after_release_power)
            else:
                start_preflight(final_preflight, require_cookie_absent=True)

        def first_preflight(allowed, why, _login_owner):
            if not require_live():
                return
            if not allowed:
                finish(why)
            elif timer_require_lid and self.lid_closed is not True:
                finish('timer requires a confirmed closed lid')
            elif timer_require_prevention and not self.enabled:
                finish('timer requires effective prevention before release')
            elif origin == 'failsafe' and not self.failsafe_condition_met():
                finish('failsafe power conditions no longer confirmed')
            else:
                self.desired = False
                try:
                    self.release_async(released)
                except Exception as failure:
                    finish('GNOME inhibitor release setup failed: ' + str(failure))

        def first_power(available):
            if not require_live():
                return
            if not available:
                finish('fresh power state is unavailable')
            elif origin == 'failsafe' and not self.failsafe_condition_met():
                finish('failsafe power conditions no longer confirmed')
            elif timer_require_lid and self.lid_closed is not True:
                finish('timer requires a confirmed closed lid')
            else:
                start_preflight(first_preflight)

        if origin == 'failsafe' or timer_require_lid:
            start_power(first_power)
        else:
            start_preflight(first_preflight)
        return True

    def finish_suspend_request(self, transaction, origin, failure=None):
        # A sleep signal or owner replacement may have resolved this request
        # before login1 reports its method result. Never infer a second request.
        if self.shutting_down or self.sleep_tx is not transaction:
            return
        if failure is None:
            if not self.record_sleep_request_reply(transaction, 'accepted'):
                return
            if origin == 'timer':
                self.timer_outcome = 'Countdown consumed; suspend request accepted'
            self.publish()
            return
        classification = self.classify_suspend_error(failure)
        if not self.record_sleep_request_reply(transaction, classification):
            return
        self.last_error = ('Suspend request rejected: ' if classification == 'rejected'
                           else 'Suspend request outcome unknown: ') + str(failure)
        LOG.error(self.last_error)
        self.notify("Sleep Disabler", self.last_error)
        if classification == 'rejected':
            self.resolve_sleep_transaction('rejected')
        else:
            self.publish()

    def notify(self, summary, body):
        if self.shutting_down:
            return
        try:
            if not self.session_bus.get_is_connected():
                return
            self.proxy(self.session_bus, "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications", "org.freedesktop.Notifications").Notify(
                "Sleep Disabler", dbus.UInt32(0), "", summary, body,
                dbus.Array([], signature="s"), dbus.Dictionary({}, signature="sv"), dbus.Int32(5000),
                timeout=NOTIFICATION_CALL_TIMEOUT, reply_handler=lambda _id: None,
                error_handler=lambda error: LOG.debug('Notification unavailable: %s', error))
        except Exception as error:
            # Notifications are advisory; a local proxy/setup failure must
            # never interrupt inhibitor cleanup or sleep-state reconciliation.
            LOG.debug('Notification dispatch unavailable: %s', error)

    def cancel_timer(self, reason=""):
        if self.deadline is None:
            return False
        self.deadline = None
        self.require_lid = False
        self.require_prevention = False
        if reason:
            self.timer_outcome = reason
        self.timer_phase = 'canceled'
        self.last_timer_state = -1
        self.publish()
        return True

    def consume_timer(self, outcome):
        if self.deadline is None:
            return False
        self.deadline = None
        self.require_lid = False
        self.require_prevention = False
        self.timer_phase = 'consumed'
        self.timer_outcome = outcome
        self.last_timer_state = -1
        return True

    def idle_sleep_transaction(self):
        return {'phase': 'idle', 'origin': '', 'reason': '', 'requested_at': None,
                'request_reply': 'none', 'saw_prepare_true': False,
                'saw_prepare_false': False, 'gap_baseline': None,
                'uncertain_since': None, 'owner_generation': self.logind_owner_generation
                if hasattr(self, 'logind_owner_generation') else 0}

    def retain_clock_baseline(self, sample):
        """Keep an older valid interval while new suspend evidence is ambiguous."""
        if (sample is not None and (self.clock_gap is None or
                sample.upper < self.clock_gap.lower)):
            self.clock_gap = sample

    def begin_sleep_request(self, origin, reason):
        sample = sample_clock_gap()
        monotonic_now = sample.monotonic if sample is not None else time.monotonic()
        self.sleep_tx = {'phase': 'requesting', 'origin': origin, 'reason': reason,
                         'requested_at': monotonic_now, 'request_reply': 'none',
                         'saw_prepare_true': False, 'saw_prepare_false': False,
                         'gap_baseline': sample,
                         'uncertain_since': monotonic_now,
                         'owner_generation': self.logind_owner_generation}
        return self.sleep_tx

    def record_sleep_request_reply(self, transaction, reply):
        """Apply a method reply only to the transaction that issued the call."""
        if self.sleep_tx is not transaction or transaction['request_reply'] != 'none':
            return False
        transaction['request_reply'] = reply
        if transaction['phase'] == 'requesting' and reply != 'rejected':
            transaction['phase'] = 'request-pending'
        self.suspend_request_outcome = reply
        return True

    def observe_prepare_enter(self):
        if self.sleep_tx['phase'] == 'resolving':
            return
        self.last_prepare_signal = True
        if self.sleep_tx['phase'] == 'idle':
            sample = sample_clock_gap()
            monotonic_now = sample.monotonic if sample is not None else time.monotonic()
            self.sleep_tx = {'phase': 'preparing', 'origin': 'external', 'reason': '',
                             'requested_at': None, 'request_reply': 'none',
                             'saw_prepare_true': True, 'saw_prepare_false': False,
                             'gap_baseline': sample,
                             'uncertain_since': monotonic_now,
                             'owner_generation': self.logind_owner_generation}
        else:
            self.sleep_tx['saw_prepare_true'] = True
            self.sleep_tx['phase'] = 'preparing'
            if self.sleep_tx['uncertain_since'] is None:
                self.sleep_tx['uncertain_since'] = time.monotonic()
        self.publish()

    def observe_prepare_exit(self):
        if self.sleep_tx['phase'] == 'resolving':
            self.last_prepare_signal = False
            return
        if self.sleep_tx['phase'] == 'idle' and self.last_prepare_signal is False:
            return
        self.last_prepare_signal = False
        if self.sleep_tx['phase'] == 'idle':
            monotonic_now = time.monotonic()
            self.sleep_tx = {'phase': 'reconciling', 'origin': 'external', 'reason': '',
                             'requested_at': None, 'request_reply': 'none',
                             'saw_prepare_true': False, 'saw_prepare_false': True,
                             'gap_baseline': self.clock_gap,
                             'uncertain_since': monotonic_now,
                             'owner_generation': self.logind_owner_generation}
        else:
            self.sleep_tx['saw_prepare_false'] = True
            self.sleep_tx['phase'] = 'reconciling'
        self.reconcile_sleep_state('prepare-exit')

    def mark_sleep_uncertain_blocked(self, outcome):
        self.sleep_tx['phase'] = 'uncertain-blocked'
        self.sleep_outcome = outcome
        if self.deadline is not None:
            self.cancel_timer('Countdown canceled because sleep state remained uncertain')
        else:
            self.publish()

    def resolve_sleep_transaction(self, classification, now=None, monotonic_now=None):
        if self.sleep_tx['phase'] in ('idle', 'resolving'):
            return
        self.cancel_sleep_reconcile()
        tx = self.sleep_tx
        tx['phase'] = 'resolving'
        reason = tx['reason']
        self.sleep_outcome = classification
        if classification == 'rejected':
            self.suspend_request_outcome = 'rejected'
            if tx['origin'] == 'timer':
                self.timer_outcome = 'Countdown consumed; suspend request rejected'
        elif classification == 'accepted-no-suspend':
            self.suspend_request_outcome = 'accepted-without-observed-suspend'
            if tx['origin'] == 'timer':
                self.timer_outcome = 'Countdown consumed; request accepted but no suspend observed'
            self.notify('Sleep Disabler', 'Suspend request was accepted but no suspend was observed')
        elif classification == 'request-outcome-unknown':
            self.suspend_request_outcome = 'unknown'
            if tx['origin'] == 'timer':
                self.timer_outcome = 'Countdown consumed; suspend request outcome unknown'
        elif classification in ('failed-preparation', 'uncertain-preparation') and \
                tx['request_reply'] == 'accepted':
            self.suspend_request_outcome = 'accepted-without-observed-suspend'
            if tx['origin'] == 'timer':
                self.timer_outcome = 'Countdown consumed; request accepted but no suspend observed'
            self.notify('Sleep Disabler', 'Suspend request was accepted but no suspend was observed')
        elif classification in ('failed-preparation', 'uncertain-preparation') and \
                tx['request_reply'] == 'unknown':
            self.suspend_request_outcome = 'unknown'
            if tx['origin'] == 'timer':
                self.timer_outcome = 'Countdown consumed; suspend request outcome unknown'
        elif classification == 'proven-resume':
            if tx['origin'] in ('timer', 'failsafe'):
                self.suspend_request_outcome = 'proven-suspend'
            if reason:
                self.notify('Sleep Disabler', 'Resumed after: ' + reason)
        elif classification == 'logind-owner-lost' and tx['request_reply'] != 'none':
            self.suspend_request_outcome = 'unknown'
            if tx['origin'] == 'timer':
                self.timer_outcome = 'Countdown consumed; suspend request outcome unknown'
        trigger = 'wake' if classification == 'proven-resume' else (
            'uncertain-resolution' if ('uncertain' in classification or
                                       classification == 'logind-owner-lost')
            else 'failed-preparation')

        def after_power(_available):
            if self.sleep_tx is not tx or self.shutting_down:
                return
            try:
                self.refresh_brightness_capability(trigger)
            except Exception:
                LOG.exception('Brightness capability refresh failed after sleep resolution')
            if self.deadline is not None and boottime() >= self.deadline:
                labels = {
                    'proven-resume': 'Countdown expired during suspend; no second suspend requested',
                    'failed-preparation': 'Countdown expired during failed sleep preparation; no suspend requested',
                }
                self.cancel_timer(labels.get(classification,
                                  'Countdown canceled after uncertain sleep outcome; no suspend requested'))
            elif self.deadline is not None:
                self.timer_conditions_hold()
            self.retain_clock_baseline(sample_clock_gap())
            self.last_prepare_signal = False
            self.sleep_tx = self.idle_sleep_transaction()
            try:
                self.publish()
            except Exception:
                LOG.exception('Sleep resolution publication failed')

        try:
            self.cancel_power_refresh()
            self.refresh_power_async(callback=after_power, reconcile_lid=False)
        except Exception:
            LOG.exception('Power refresh setup failed after sleep resolution')
            after_power(False)

    def cancel_sleep_reconcile(self):
        operation = self.sleep_reconcile_operation
        if operation is not None:
            self.sleep_reconcile_operation = None
            GLib.source_remove(operation['source'])

    def timer_conditions_hold(self):
        if self.require_lid and self.lid_closed is not True:
            self.cancel_timer('Timer canceled because the lid is not closed after sleep preparation')
            return False
        if self.require_prevention and not self.enabled:
            self.cancel_timer('Timer canceled because prevention is off after sleep preparation')
            return False
        return True

    def reconcile_sleep_state(self, trigger):
        if self.sleep_tx['phase'] == 'resolving':
            return False
        sample = sample_clock_gap()
        monotonic_now = sample.monotonic if sample is not None else time.monotonic()
        if self.sleep_tx['phase'] == 'idle':
            if not gap_proves_suspend(sample, self.clock_gap):
                # A complete downward shift cannot prove suspend from the old
                # baseline; establish a new trustworthy baseline.
                self.retain_clock_baseline(sample)
                return False
            self.sleep_tx = {'phase': 'reconciling', 'origin': 'external', 'reason': '',
                             'requested_at': None, 'request_reply': 'none',
                             'saw_prepare_true': False, 'saw_prepare_false': False,
                             'gap_baseline': self.clock_gap, 'uncertain_since': monotonic_now,
                             'owner_generation': self.logind_owner_generation}
        tx = self.sleep_tx
        if tx['owner_generation'] != self.logind_owner_generation:
            self.resolve_sleep_transaction('logind-owner-lost')
            return True
        operation = self.sleep_reconcile_operation
        if operation is not None:
            if operation['tx'] is not tx:
                self.cancel_sleep_reconcile()
            else:
                if gap_proves_suspend(sample, tx['gap_baseline']):
                    operation['proven_sample'] = sample
                return False
        operation = {'tx': tx, 'source': 0,
                     'deadline': time.monotonic() + SLEEP_RECONCILE_TIMEOUT,
                     'generation': self.logind_owner_generation,
                     'proven_sample': sample if gap_proves_suspend(sample, tx['gap_baseline']) else None}

        def complete(preparing, owner_lost=False):
            if self.sleep_reconcile_operation is not operation:
                return
            self.cancel_sleep_reconcile()
            if self.shutting_down or self.sleep_tx is not tx:
                return
            if owner_lost or operation['generation'] != self.logind_owner_generation:
                self.resolve_sleep_transaction('logind-owner-lost')
                return
            if time.monotonic() >= operation['deadline']:
                preparing = None
            current = sample_clock_gap()
            proven = current if gap_proves_suspend(current, tx['gap_baseline']) else \
                operation['proven_sample']
            if proven is not None and preparing is not True:
                # A valid prior interval still proves a resume when this
                # callback's sample is unavailable or ambiguous.
                self.clock_gap = proven
                self.resolve_sleep_transaction('proven-resume')
                return
            current_monotonic = current.monotonic if current is not None else time.monotonic()
            since = tx['uncertain_since'] if tx['uncertain_since'] is not None else current_monotonic
            elapsed = current_monotonic - since
            if preparing is True:
                tx['phase'] = 'preparing' if elapsed < SLEEP_UNCERTAIN_SECONDS else 'uncertain-blocked'
                if elapsed >= SLEEP_UNCERTAIN_SECONDS:
                    self.mark_sleep_uncertain_blocked('preparation-state-uncertain')
                return
            if preparing is None:
                if elapsed >= SLEEP_UNCERTAIN_SECONDS:
                    self.mark_sleep_uncertain_blocked('logind-state-unreadable')
                return
            if tx['gap_baseline'] is None or current is None:
                # A later clock cannot reconstruct a missing request baseline;
                # an invalid current sample cannot establish absence of sleep.
                if elapsed >= SLEEP_UNCERTAIN_SECONDS:
                    self.mark_sleep_uncertain_blocked('clock-evidence-unavailable')
                return
            tx['phase'] = 'reconciling'
            if elapsed < SLEEP_SETTLE_SECONDS:
                return
            if tx['saw_prepare_true']:
                classification = 'failed-preparation'
            elif tx['saw_prepare_false']:
                classification = 'uncertain-preparation'
            elif tx['request_reply'] == 'accepted':
                classification = 'accepted-no-suspend'
            elif tx['request_reply'] == 'unknown':
                classification = 'request-outcome-unknown'
            else:
                classification = 'uncertain-preparation'
            self.resolve_sleep_transaction(classification)

        def remaining():
            value = operation['deadline'] - time.monotonic()
            if value <= 0:
                raise TimeoutError('logind preparation read exceeded its deadline')
            return min(RECOVERY_CALL_TIMEOUT, value)

        def failed(failure):
            LOG.debug('Cannot read logind sleep state: %s', failure)
            complete(None)

        def final_owner(expected, current):
            if self.sleep_reconcile_operation is not operation:
                return
            if str(current) != expected:
                complete(None, owner_lost=True)
            else:
                complete(operation['preparing'])

        def received(value, owner):
            if self.sleep_reconcile_operation is not operation:
                return
            try:
                operation['preparing'] = dbus_boolean(value)
                self.proxy(self.system_bus, 'org.freedesktop.DBus',
                           '/org/freedesktop/DBus', 'org.freedesktop.DBus').GetNameOwner(
                    LOGIN, timeout=remaining(),
                    reply_handler=lambda current: final_owner(owner, current),
                    error_handler=failed)
            except Exception as failure:
                failed(failure)

        def owner_received(owner):
            if self.sleep_reconcile_operation is not operation:
                return
            try:
                self.proxy(self.system_bus, str(owner), LOGIN_PATH,
                           'org.freedesktop.DBus.Properties').Get(
                    'org.freedesktop.login1.Manager', 'PreparingForSleep',
                    timeout=remaining(),
                    reply_handler=lambda value: received(value, str(owner)),
                    error_handler=failed)
            except Exception as failure:
                failed(failure)

        try:
            operation['source'] = GLib.timeout_add(SLEEP_RECONCILE_TIMEOUT * 1000,
                lambda: (complete(None), GLib.SOURCE_REMOVE)[1])
            if not operation['source']:
                raise RuntimeError('GLib did not attach the logind preparation deadline')
        except Exception as failure:
            LOG.warning('Logind preparation read could not start: %s', failure)
            since = tx['uncertain_since']
            if since is not None and time.monotonic() - since >= SLEEP_UNCERTAIN_SECONDS:
                self.mark_sleep_uncertain_blocked('logind-state-unreadable')
            return False
        self.sleep_reconcile_operation = operation
        try:
            self.proxy(self.system_bus, 'org.freedesktop.DBus',
                       '/org/freedesktop/DBus', 'org.freedesktop.DBus').GetNameOwner(
                LOGIN, timeout=remaining(), reply_handler=owner_received,
                error_handler=failed)
        except Exception as failure:
            failed(failure)
        return False

    def tick(self):
        if self.shutting_down:
            return False
        now = boottime()
        self.reconcile_sleep_state('tick')
        if self.deadline is not None:
            if self.sleep_tx['phase'] != 'idle':
                return True
            if self.require_lid and self.lid_closed is not True:
                self.cancel_timer("Timer canceled because the lid is not closed")
            elif self.require_prevention and not self.enabled:
                self.cancel_timer("Timer canceled because prevention is off")
            elif now >= self.deadline:
                self.sleep_now("Countdown elapsed")
            else:
                remaining = math.ceil(self.deadline - now)
                displayed_minute = math.ceil(remaining / 60)
                if displayed_minute != self.last_timer_state:
                    self.last_timer_state = displayed_minute
                    try:
                        self.TimerChanged(dbus.UInt64(remaining))
                    except dbus.DBusException as error:
                        LOG.warning('Timer signal delivery failed: %s', error)
        return True

    def reconcile(self):
        if self.shutting_down:
            return False
        self.reconcile_sleep_state('periodic')
        self.refresh_power_async(check_failsafe=True)
        self.refresh_brightness_capability('periodic')
        if self.brightness_journal_state == 'invalid':
            try:
                if not self.brightness_path.exists():
                    self.brightness_journal_state = 'absent'
                    self.brightness_error = ''
            except OSError:
                pass
        self.reconcile_brightness_async('periodic')
        self.retry_session_release()
        if self.desired and self.suspend_attempt is None and not self.enabled and not self.recovering and \
                self.session_cookie_state == 'absent':
            self.recovering = True
            try:
                self.acquire_async(lambda _failure: setattr(self, 'recovering', False))
            except Exception:
                self.recovering = False
                LOG.exception('Background prevention acquisition setup failed')
        self.retry_lid_lock()
        return True

    def on_properties_changed(self, interface, changed, invalidated, **_kwargs):
        if self.shutting_down:
            return
        if interface in (UPOWER, "org.freedesktop.UPower.Device"):
            self.power_changed()

    def on_device_change(self, *_args):
        if self.shutting_down:
            return
        self.power_changed()

    def on_prepare_sleep(self, entering):
        if self.shutting_down:
            return
        self.cancel_sleep_reconcile()
        if entering:
            self.cancel_suspend_attempt('logind began preparing for sleep')
            self.observe_prepare_enter()
            self.cancel_suspend_preflight('logind began preparing for sleep')
        else:
            self.observe_prepare_exit()

    def on_owner_change(self, name, old, new):
        if self.shutting_down:
            return
        if name == BRIGHTNESS:
            self.brightness_adapter = None
            self.brightness_available = False
            if self.brightness_record is not None:
                self.brightness_retry_pending = True
            if self.dimming_verification is not None:
                self.dimming_verification['finish'](dbus.DBusException(
                    'Brightness owner changed during verification', name=IFACE + '.Canceled'))
        if name == BRIGHTNESS and new:
            self.refresh_brightness_capability('owner-return')
        elif name == BRIGHTNESS:
            self.cancel_brightness_probe()
            self.brightness_error = "Brightness service disconnected"
            self.reconcile_brightness_async('owner-loss')
        elif name == UPOWER:
            self.cancel_suspend_attempt('UPower owner changed during suspend attempt')
            self.cancel_power_refresh()
            self.invalidate_power_snapshot()
            self.power_last_known_lid = None
            self.refresh_power_async(check_failsafe=True)
        elif name == SHELL:
            self.shell_owner_generation += 1
            for cancel in tuple(self.panel_registration_cancellers):
                cancel(dbus.DBusException('GNOME Shell changed during registration',
                                          name=IFACE + '.Unavailable'))
            if self.panel_runtime_sender and new != self.panel_runtime_sender:
                self.panel_runtime_sender = ''
                self.panel_runtime_version = ''
                self.publish()
        elif name in (LOGIN, SESSION):
            if name == LOGIN and old != new:
                self.set_lid_error('')
            if name == LOGIN and old != new:
                self.logind_owner_generation += 1
                self.cancel_suspend_attempt('login1 changed during suspend attempt')
                self.cancel_suspend_preflight('login1 changed during suspend preflight')
                self.cancel_prevention_acquisition('login1 changed during prevention acquisition')
                self.cancel_lid_acquisition('login1 changed during lid acquisition')
                if self.sleep_tx['phase'] != 'idle':
                    self.resolve_sleep_transaction('logind-owner-lost')
                self.last_prepare_signal = None
            if name == SESSION and old != new:
                self.session_owner_generation += 1
                self.cancel_suspend_attempt('GNOME SessionManager changed during suspend attempt')
                self.cancel_suspend_preflight('GNOME SessionManager changed during suspend preflight')
                self.cancel_prevention_acquisition('GNOME SessionManager changed during acquisition')
                if old and old == self.session_cookie_owner:
                    if not self.enabled:
                        # Losing a well-known name does not prove the issuing
                        # unique connection is gone. Retry cleanup to that owner.
                        self.session_cookie_state = 'release-pending'
                        self.inhibitor_outcome = 'release-uncertain'
                        if self.prevention_release is None:
                            self.schedule_release_retry()
                        self.publish()
            if old and old != new and self.enabled:
                if name == LOGIN:
                    # logind dropped its old inhibitor state with its owner.
                    self.lid_outcome = 'failed' if self.lid_mode else 'unavailable'
                def owner_loss_released(_released):
                    if self.shutting_down:
                        return
                    if self.session_cookie_state != 'release-pending':
                        self.last_error = name + ' disconnected; retrying'
                    self.publish()
                    if new and self.desired and self.session_cookie_state == 'absent':
                        GLib.idle_add(self.reconcile_once)
                self.release_async(owner_loss_released)
            elif new and self.desired and not self.enabled:
                GLib.idle_add(self.reconcile_once)
            elif new and name == LOGIN and self.enabled and self.lid_mode:
                GLib.idle_add(self.reconcile_once)

    def on_panel_owner_change(self, name, old, new):
        if self.shutting_down:
            return
        if (self.panel_runtime_sender and name == self.panel_runtime_sender and old and not new):
            self.panel_runtime_sender = ''
            self.panel_runtime_version = ''
            self.publish()

    def reconcile_once(self):
        self.reconcile()
        return False

    def state(self):
        remaining = max(0, math.ceil(self.deadline - boottime())) if self.deadline is not None else 0
        values = {
            "agentRuntimeVersion": dbus.String(AGENT_RUNTIME_VERSION),
            "apiVersion": dbus.UInt32(API_VERSION),
            "stateHome": dbus.String(str(data_home().parent)),
            "panelRuntimeVersion": dbus.String(self.panel_runtime_version),
            "panelRuntimeRegistered": dbus.Boolean(bool(self.panel_runtime_sender)),
            "enabled": dbus.Boolean(self.enabled),
            "desired": dbus.Boolean(self.desired),
            "gnomeInhibitorState": dbus.String(self.session_cookie_state),
            "gnomeReleasePending": dbus.Boolean(self.session_cookie_state == 'release-pending'),
            "inhibitorOutcome": dbus.String(self.inhibitor_outcome),
            "sessionOwnerGeneration": dbus.UInt64(self.session_owner_generation),
            "lidMode": dbus.Boolean(self.lid_mode),
            "lidRequested": dbus.Boolean(self.lid_mode),
            "lidLockAcquired": dbus.Boolean(self.lid_fd is not None),
            "lidOutcome": dbus.String(self.lid_outcome),
            "lidError": dbus.String(self.lid_error),
            "lidInhibitor": dbus.Boolean(self.lid_fd is not None),
            "lidDimming": dbus.Boolean(self.lid_dimming),
            "lidClosed": dbus.String("unknown" if self.lid_closed is None else ("yes" if self.lid_closed else "no")),
            "brightnessDimmed": dbus.Boolean(self.brightness_record is not None and
                                             self.brightness_record.get("phase") == "applied"),
            "brightnessAvailable": dbus.Boolean(self.brightness_available),
            "brightnessError": dbus.String(self.brightness_error),
            "brightnessJournalState": dbus.String(self.brightness_journal_state),
            "brightnessState": dbus.String(self.brightness_state()),
            "brightnessRecoveryPending": dbus.Boolean(self.brightness_recovery_pending()),
            "failsafe": dbus.Boolean(self.failsafe),
            "threshold": dbus.UInt32(self.threshold),
            "onBattery": dbus.String("unknown" if self.on_battery is None else ("yes" if self.on_battery else "no")),
            "batteryPercent": dbus.Double(self.battery_percent if self.battery_percent is not None else -1),
            "batteryDischargeState": dbus.String(self.battery_discharge_state),
            "timerRemaining": dbus.UInt64(remaining),
            "timerOutcome": dbus.String(self.timer_outcome),
            "timerPhase": dbus.String(self.timer_phase),
            "timerRequireLid": dbus.Boolean(self.require_lid),
            "timerRequirePrevention": dbus.Boolean(self.require_prevention),
            "timerDefaultMinutes": dbus.UInt32(self.last_timer_minutes),
            "timerDefaultRequireLid": dbus.Boolean(self.last_timer_require_lid),
            "timerDefaultRequirePrevention": dbus.Boolean(self.last_timer_require_prevention),
            "sleepPhase": dbus.String(self.sleep_tx['phase']),
            "sleepOutcome": dbus.String(self.sleep_outcome),
            "suspendRequestOutcome": dbus.String(self.suspend_request_outcome),
            "error": dbus.String(self.last_error),
        }
        return dbus.Dictionary(values, signature="sv")

    def publish(self):
        state = self.state()
        try:
            self.StateChanged(state)
        except dbus.DBusException as error:
            # A transport failure must not interrupt committed state or cleanup.
            LOG.warning('State signal delivery failed: %s', error)

    @dbus.service.method(IFACE, in_signature="", out_signature="a{sv}")
    def GetState(self):
        return self.state()

    @dbus.service.method(IFACE, in_signature="s", out_signature="", sender_keyword="sender",
                         async_callbacks=('reply', 'error'))
    def RegisterPanelRuntime(self, version, sender=None, reply=None, error=None):
        version = str(version)
        sender = str(sender or '')
        if not PANEL_VERSION_RE.fullmatch(version) or not sender.startswith(':'):
            raise dbus.DBusException('Invalid panel runtime registration',
                                     name=IFACE + '.InvalidArgument')
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        if len(self.panel_registration_cancellers) >= MAX_PUBLIC_WAITERS:
            raise dbus.DBusException('Too many pending panel registrations',
                                     name=IFACE + '.Busy')
        generation = self.shell_owner_generation
        operation = {'done': False, 'source': 0,
                     'deadline': time.monotonic() + 3.5}

        def finish(failure=None, owner=None):
            if operation['done']:
                return
            if failure is None and time.monotonic() >= operation['deadline']:
                failure = dbus.DBusException('GNOME Shell owner check timed out',
                                             name=IFACE + '.Unavailable')
            operation['done'] = True
            self.panel_registration_cancellers.discard(finish)
            GLib.source_remove(operation['source'])
            if failure is None and (self.shutting_down or generation != self.shell_owner_generation):
                failure = dbus.DBusException('GNOME Shell changed during registration',
                                             name=IFACE + '.Unavailable')
            if failure is None and str(owner) != sender:
                failure = dbus.DBusException('Panel runtime sender is not the current GNOME Shell owner',
                                             name=IFACE + '.InvalidArgument')
            if failure is None and (self.panel_runtime_version != version or
                                    self.panel_runtime_sender != sender):
                self.panel_runtime_version = version
                self.panel_runtime_sender = sender
                try:
                    self.publish()
                except Exception:
                    LOG.exception('Panel registration state publication failed')
            if failure is None and time.monotonic() >= operation['deadline']:
                failure = dbus.DBusException('GNOME Shell owner check timed out',
                                             name=IFACE + '.Unavailable')
            try:
                if failure is None:
                    reply()
                else:
                    error(failure)
            except Exception as callback_error:
                LOG.warning('Panel registration reply could not be delivered: %s', callback_error)

        try:
            operation['source'] = GLib.timeout_add(3500, lambda: (
                finish(dbus.DBusException('GNOME Shell owner check timed out',
                                          name=IFACE + '.Unavailable')), GLib.SOURCE_REMOVE)[1])
            if not operation['source']:
                raise RuntimeError('Panel registration deadline source unavailable')
        except Exception as failure:
            error(dbus.DBusException(str(failure), name=IFACE + '.Unavailable'))
            return
        finish.sender = sender
        finish.version = version
        self.panel_registration_cancellers.add(finish)
        try:
            names = self.proxy(self.session_bus, 'org.freedesktop.DBus',
                               '/org/freedesktop/DBus', 'org.freedesktop.DBus')
            names.GetNameOwner(SHELL, timeout=RECOVERY_CALL_TIMEOUT,
                               reply_handler=lambda owner: finish(owner=owner), error_handler=finish)
        except (dbus.DBusException, OSError, TypeError, ValueError) as owner_error:
            finish(owner_error)

    @dbus.service.method(IFACE, in_signature="s", out_signature="", sender_keyword="sender",
                         async_callbacks=('reply', 'error'))
    def UnregisterPanelRuntime(self, version, sender=None, reply=None, error=None):
        deadline = time.monotonic() + PUBLIC_MUTATION_TIMEOUT
        for cancel in tuple(self.panel_registration_cancellers):
            if (getattr(cancel, 'sender', None) == str(sender or '') and
                    getattr(cancel, 'version', None) == str(version)):
                cancel(dbus.DBusException('Panel runtime unregistered during owner check',
                                         name=IFACE + '.Unavailable'))
        if (str(sender or '') != self.panel_runtime_sender or
                str(version) != self.panel_runtime_version):
            if reply is not None:
                if time.monotonic() >= deadline:
                    error(dbus.DBusException('Panel unregister exceeded its deadline',
                                             name=IFACE + '.Unavailable'))
                else:
                    reply()
            return
        self.panel_runtime_version = ''
        self.panel_runtime_sender = ''
        self.publish()
        if reply is not None:
            if time.monotonic() >= deadline:
                error(dbus.DBusException('Panel unregister exceeded its deadline',
                                         name=IFACE + '.Unavailable'))
            else:
                reply()

    @dbus.service.method(IFACE, in_signature="b", out_signature="",
                         async_callbacks=('reply', 'error'))
    def SetPrevention(self, enabled, reply=None, error=None):
        admitted = time.monotonic()
        admission_deadline = admitted + PUBLIC_MUTATION_TIMEOUT
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        if enabled:
            if self.suspend_attempt is not None:
                raise dbus.DBusException('An automatic suspend attempt is in progress',
                                         name=IFACE + '.Busy')
            if reply is not None:
                if len(self.prevention_enable_requests) >= MAX_PUBLIC_WAITERS:
                    raise dbus.DBusException('Too many pending prevention requests',
                                             name=IFACE + '.Busy')
                request = {'done': False, 'source': 0,
                           'admitted': admitted,
                           'deadline': admission_deadline}

                def finish(failure=None):
                    if request['done']:
                        return
                    if failure is None and time.monotonic() >= request['deadline']:
                        failure = dbus.DBusException('Prevention enable exceeded its deadline',
                                                     name=IFACE + '.Unavailable')
                    request['done'] = True
                    self.prevention_enable_requests.discard(finish)
                    if request['source']:
                        GLib.source_remove(request['source'])
                    try:
                        if failure is None:
                            reply()
                        else:
                            error(failure)
                    except Exception as callback_error:
                        LOG.warning('Prevention enable reply could not be delivered: %s',
                                    callback_error)

                try:
                    request['source'] = GLib.timeout_add(max(1, int(
                        (admission_deadline - time.monotonic()) * 1000)),
                        lambda: (finish(dbus.DBusException('Prevention enable exceeded its deadline',
                            name=IFACE + '.Unavailable')), GLib.SOURCE_REMOVE)[1])
                    if not request['source']:
                        raise RuntimeError('Prevention enable deadline source unavailable')
                except Exception as failure:
                    finish(failure)
                    return
                self.prevention_enable_requests.add(finish)

                def after_failsafe(outcome):
                    if request['done']:
                        return
                    if time.monotonic() >= request['deadline']:
                        finish(dbus.DBusException('Prevention enable exceeded its deadline',
                                                  name=IFACE + '.Unavailable'))
                        return
                    if self.shutting_down or not self.desired or not self.enabled:
                        finish(dbus.DBusException('Prevention enable was superseded',
                                                  name=IFACE + '.Canceled'))
                        return
                    if outcome != 'clear' or self.failsafe_condition_met():
                        finish(dbus.DBusException('Failsafe conditions could not be cleared',
                                                  name=IFACE + '.Unavailable'))
                        return
                    def after_brightness(result):
                        if request['done']:
                            return
                        if self.shutting_down or not self.desired or not self.enabled:
                            finish(dbus.DBusException('Prevention enable was superseded',
                                                      name=IFACE + '.Canceled'))
                            return
                        if result in ('pending', 'invalid') or self.brightness_recovery_pending():
                            finish(dbus.DBusException('Prevention is enabled, but brightness recovery is pending',
                                                      name=IFACE + '.InvalidState'))
                            return
                        try:
                            if self.last_error.startswith((
                                    'GNOME inhibitor ownership is unresolved;',
                                    'GNOME inhibitor release is pending;')):
                                self.last_error = ''
                                self.publish()
                        except Exception as failure:
                            finish(failure)
                            return
                        finish()

                    try:
                        self.reconcile_brightness_async('prevention-enable', after_brightness)
                    except Exception as failure:
                        finish(failure)

                def fresh_power(available):
                    if request['done']:
                        return
                    if time.monotonic() >= request['deadline']:
                        finish(dbus.DBusException('Prevention enable exceeded its deadline',
                                                  name=IFACE + '.Unavailable'))
                        return
                    if self.shutting_down or not self.desired or not self.enabled:
                        finish(dbus.DBusException('Prevention enable was superseded',
                                                  name=IFACE + '.Canceled'))
                        return
                    if not available:
                        finish(dbus.DBusException('Fresh power state is unavailable',
                                                  name=IFACE + '.Unavailable'))
                        return
                    try:
                        self.check_failsafe(callback=after_failsafe)
                    except Exception as failure:
                        finish(failure)

                def acquired(failure):
                    if request['done']:
                        return
                    if failure is not None:
                        finish(failure)
                    elif self.shutting_down or not self.desired:
                        finish(dbus.DBusException('Prevention enable was superseded',
                                                  name=IFACE + '.Canceled'))
                    else:
                        try:
                            pending = self.power_refresh
                            if (pending is not None and
                                    pending['started'] < request['admitted']):
                                # This read predates the request. Wait for it to
                                # finish, then start a new current-owner read.
                                def after_older_refresh(_available):
                                    if not request['done']:
                                        try:
                                            self.refresh_power_async(callback=fresh_power,
                                                                     deadline=request['deadline'])
                                        except Exception as power_error:
                                            finish(power_error)
                                if len(pending['waiters']) >= MAX_PUBLIC_WAITERS:
                                    finish(dbus.DBusException('Too many pending power refresh requests',
                                                              name=IFACE + '.Busy'))
                                else:
                                    pending['waiters'].append(after_older_refresh)
                            else:
                                self.refresh_power_async(callback=fresh_power,
                                                         deadline=request['deadline'])
                        except Exception as power_error:
                            finish(power_error)

                try:
                    fd_live = self.fd is not None and os.fstat(self.fd) is not None
                except (OSError, ValueError, TypeError):
                    fd_live = False
                healthy = (self.session_cookie_state == 'held' and
                           self.session_cookie is not None and self.session_cookie_owner and
                           fd_live and self.sleep_fd_owner and self.enabled and
                           self.prevention_release is None and
                           self.prevention_acquisition is None)
                if time.monotonic() >= request['deadline']:
                    finish(dbus.DBusException('Prevention enable exceeded its deadline',
                                              name=IFACE + '.Unavailable'))
                    return
                if ((self.enabled or self.session_cookie_state != 'absent' or
                     self.session_cookie is not None or self.fd is not None or
                     self.prevention_release is not None) and not healthy):
                    def partial_pair_released(released):
                        finish(dbus.DBusException('GNOME inhibitor ownership was unresolved',
                            name=IFACE + ('.Unavailable' if released else '.ReleasePending')))
                    try:
                        self.release_async(partial_pair_released)
                    except Exception as cleanup_error:
                        LOG.exception('Partial GNOME inhibitor cleanup failed')
                        finish(dbus.DBusException(str(cleanup_error),
                                                   name=IFACE + '.ReleasePending'))
                    return
                self.desired = True
                if healthy:
                    owner = self.session_cookie_owner
                    generation = self.session_owner_generation
                    login_owner = self.sleep_fd_owner
                    login_generation = self.logind_owner_generation
                    sleep_fd = self.fd
                    def release_stale_owner():
                        # The old cookie belongs to the issuing unique owner,
                        # even if the well-known name is now absent or replaced.
                        def cleaned(released):
                            finish(dbus.DBusException('Prevention inhibitor owner changed',
                                name=IFACE + ('.Unavailable' if released else '.ReleasePending')))
                        try:
                            self.release_async(cleaned)
                        except Exception as cleanup_error:
                            LOG.exception('Stale GNOME inhibitor cleanup failed')
                            finish(dbus.DBusException(str(cleanup_error),
                                                       name=IFACE + '.ReleasePending'))

                    def verified_login(current):
                        if request['done']:
                            return
                        if time.monotonic() >= request['deadline']:
                            finish(dbus.DBusException('Prevention enable exceeded its deadline',
                                                      name=IFACE + '.Unavailable'))
                            return
                        if self.shutting_down or not self.desired:
                            finish(dbus.DBusException('Prevention enable was superseded',
                                                       name=IFACE + '.Canceled'))
                            return
                        try:
                            still_open = os.fstat(sleep_fd) is not None
                        except (OSError, ValueError, TypeError):
                            still_open = False
                        if str(current) != login_owner or not still_open:
                            release_stale_owner()
                            return
                        if (self.session_owner_generation != generation or
                                self.logind_owner_generation != login_generation or
                                self.session_cookie_owner != owner or
                                self.sleep_fd_owner != login_owner or
                                self.session_cookie_state != 'held' or self.fd != sleep_fd or
                                not self.enabled):
                            release_stale_owner()
                        else:
                            acquired(None)
                    def verified(current):
                        if request['done']:
                            return
                        if time.monotonic() >= request['deadline']:
                            finish(dbus.DBusException('Prevention enable exceeded its deadline',
                                                      name=IFACE + '.Unavailable'))
                            return
                        if self.shutting_down or not self.desired:
                            finish(dbus.DBusException('Prevention enable was superseded',
                                                       name=IFACE + '.Canceled'))
                            return
                        if str(current) != owner:
                            release_stale_owner()
                            return
                        try:
                            remaining = request['deadline'] - time.monotonic()
                            if remaining <= 0:
                                raise TimeoutError('Prevention enable exceeded its deadline')
                            self.proxy(self.system_bus, 'org.freedesktop.DBus',
                                       '/org/freedesktop/DBus', 'org.freedesktop.DBus').GetNameOwner(
                                LOGIN, timeout=min(RECOVERY_CALL_TIMEOUT, remaining),
                                reply_handler=verified_login, error_handler=verify_failed)
                        except Exception as failure:
                            verify_failed(failure)
                    def verify_failed(failure):
                        if request['done']:
                            return
                        if time.monotonic() >= request['deadline']:
                            finish(dbus.DBusException('Prevention enable exceeded its deadline',
                                                      name=IFACE + '.Unavailable'))
                            return
                        if self.dbus_error_name(failure) == 'org.freedesktop.DBus.Error.NameHasNoOwner':
                            release_stale_owner()
                        else:
                            finish(failure)
                    try:
                        remaining = request['deadline'] - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError('Prevention enable exceeded its deadline')
                        self.proxy(self.session_bus, 'org.freedesktop.DBus',
                                   '/org/freedesktop/DBus', 'org.freedesktop.DBus').GetNameOwner(
                            SESSION, timeout=min(RECOVERY_CALL_TIMEOUT, remaining),
                            reply_handler=verified, error_handler=verify_failed)
                    except Exception as failure:
                        verify_failed(failure)
                else:
                    try:
                        self.acquire_async(acquired)
                    except Exception as failure:
                        finish(failure)
                return
            if (self.enabled or self.session_cookie_state != 'absent' or
                    self.session_cookie is not None or self.fd is not None):
                healthy = (self.session_cookie_state == 'held' and self.session_cookie is not None
                           and self.session_cookie_owner and self.fd is not None and self.enabled)
                if healthy:
                    try:
                        healthy = self.session_manager_owner() == self.session_cookie_owner
                    except dbus.DBusException:
                        healthy = False
                if not healthy:
                    message = 'GNOME inhibitor ownership is unresolved; wait for recovery before enabling'
                    self.last_error = message
                    self.publish()
                    raise dbus.DBusException(message, name=IFACE + '.ReleasePending')
            self.desired = True
            if not self.enabled:
                self.acquire()
            self.refresh_power()
            self.check_failsafe()
            if self.enabled and self.last_error.startswith((
                    'GNOME inhibitor ownership is unresolved;',
                    'GNOME inhibitor release is pending;')):
                self.last_error = ''
                self.publish()
        else:
            if reply is not None and len(self.prevention_disable_requests) >= MAX_PUBLIC_WAITERS:
                raise dbus.DBusException('Too many pending prevention releases',
                                         name=IFACE + '.Busy')
            if reply is not None and self.prevention_release is not None and \
                    len(self.prevention_release['waiters']) >= MAX_PUBLIC_WAITERS:
                raise dbus.DBusException('Too many pending release continuations',
                                         name=IFACE + '.Busy')
            self.desired = False
            self.cancel_suspend_attempt('Prevention was released during suspend attempt')
            self.cancel_failsafe_evaluation()
            for cancel in tuple(self.prevention_enable_requests):
                cancel(dbus.DBusException('Prevention was released during enable',
                                          name=IFACE + '.Canceled'))
            if self.deadline and self.require_prevention:
                self.cancel_timer("Timer canceled because prevention is off")
            if reply is None:
                # Direct in-process callers retain the historical synchronous
                # behavior; D-Bus callers always provide async callbacks.
                self.cancel_prevention_acquisition('Prevention was released during acquisition')
                if not self.release():
                    raise dbus.DBusException(self.last_error, name=IFACE + '.ReleasePending')
                return
            request = {'done': False, 'source': 0,
                       'deadline': admission_deadline}
            def finish_disable(failure=None):
                if request['done']:
                    return
                if failure is None and time.monotonic() >= request['deadline']:
                    failure = dbus.DBusException('Prevention disable exceeded its deadline',
                                                 name=IFACE + '.Unavailable')
                request['done'] = True
                self.prevention_disable_requests.discard(finish_disable)
                if request['source']:
                    GLib.source_remove(request['source'])
                try:
                    if failure is None:
                        reply()
                    else:
                        error(failure)
                except Exception as callback_error:
                    LOG.warning('Prevention disable reply could not be delivered: %s', callback_error)
            try:
                remaining_ms = int((admission_deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    raise TimeoutError('Prevention disable exceeded its deadline')
                request['source'] = GLib.timeout_add(remaining_ms,
                    lambda: (finish_disable(dbus.DBusException(
                        'Prevention disable exceeded its deadline', name=IFACE + '.Unavailable')),
                        GLib.SOURCE_REMOVE)[1])
                if not request['source']:
                    raise RuntimeError('Prevention disable deadline source unavailable')
            except Exception as setup_error:
                finish_disable(dbus.DBusException(str(setup_error), name=IFACE + '.Unavailable'))
                try:
                    self.release_async(lambda _confirmed: None)
                except Exception:
                    LOG.exception('Prevention cleanup failed after disable deadline setup failure')
                return
            self.prevention_disable_requests.add(finish_disable)
            def released(confirmed):
                if confirmed:
                    finish_disable()
                else:
                    finish_disable(dbus.DBusException(self.last_error,
                                                      name=IFACE + '.ReleasePending'))
            try:
                self.release_async(released)
            except Exception as release_error:
                LOG.exception('Prevention cleanup failed during disable')
                finish_disable(dbus.DBusException(str(release_error), name=IFACE + '.Unavailable'))
            return
        if enabled:
            self.reconcile_brightness('prevention-enable')
        if reply is not None:
            reply()

    def finish_lid_mode_request(self, operation, failure=None):
        if self.lid_mode_request is not operation:
            return
        if failure is None and time.monotonic() >= operation['deadline']:
            failure = dbus.DBusException('Lid mode request exceeded its deadline',
                                         name=IFACE + '.Unavailable')
        self.lid_mode_request = None
        GLib.source_remove(operation['source'])
        if failure is not None:
            self.cancel_lid_acquisition('Lid mode request ended')
        for reply, error in operation['waiters']:
            try:
                if failure is None:
                    reply()
                else:
                    error(failure)
            except Exception as callback_error:
                LOG.warning('Lid mode reply could not be delivered: %s', callback_error)

    @dbus.service.method(IFACE, in_signature="b", out_signature="",
                         async_callbacks=('reply', 'error'))
    def SetLidMode(self, enabled, reply=None, error=None):
        admission_deadline = time.monotonic() + PUBLIC_MUTATION_TIMEOUT
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        enabled = bool(enabled)
        if self.lid_mode_request is not None:
            if enabled and self.lid_mode_request['enabled']:
                if len(self.lid_mode_request['waiters']) >= MAX_PUBLIC_WAITERS:
                    raise dbus.DBusException('Too many pending lid mode requests',
                                             name=IFACE + '.Busy')
                self.lid_mode_request['waiters'].append((reply, error))
                return
            self.finish_lid_mode_request(self.lid_mode_request, dbus.DBusException(
                'Lid mode request was superseded', name=IFACE + '.Canceled'))
        if not enabled:
            try:
                self.persist_settings(lid_mode=False)
                self.close_lid()
                self.lid_outcome = 'unavailable'
                self.set_lid_error('')
                self.publish()
            except Exception as failure:
                error(failure)
                return
            if time.monotonic() >= admission_deadline:
                error(dbus.DBusException('Lid mode request exceeded its deadline',
                                         name=IFACE + '.Unavailable'))
            else:
                reply()
            return
        operation = {'enabled': True, 'waiters': [(reply, error)], 'source': 0,
                     'deadline': admission_deadline}
        try:
            remaining_ms = int((admission_deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise TimeoutError('Lid mode request exceeded its deadline')
            operation['source'] = GLib.timeout_add(remaining_ms, lambda: (
                self.finish_lid_mode_request(operation, dbus.DBusException(
                    'Lid mode request exceeded its deadline', name=IFACE + '.Unavailable')),
                GLib.SOURCE_REMOVE)[1])
            if not operation['source']:
                raise RuntimeError('Lid mode deadline source unavailable')
        except Exception as failure:
            if error is not None:
                error(failure)
            else:
                raise
            return
        self.lid_mode_request = operation
        had_lid_fd = self.lid_fd is not None

        def acquired(failure):
            if self.lid_mode_request is not operation:
                return
            if failure is None and (self.shutting_down or time.monotonic() >= operation['deadline']):
                failure = dbus.DBusException('Lid mode request expired', name=IFACE + '.Unavailable')
            if failure is not None:
                self.lid_outcome = 'failed'
                self.set_lid_error('Lid lock unavailable: ' + str(failure))
                try:
                    self.publish()
                except Exception:
                    LOG.exception('Lid acquisition failure publication failed')
                self.finish_lid_mode_request(operation, failure)
                return
            try:
                self.persist_settings(lid_mode=True)
            except Exception as persistence_error:
                if not had_lid_fd:
                    self.close_lid()
                    self.lid_outcome = 'unavailable'
                self.finish_lid_mode_request(operation, persistence_error)
                return
            self.set_lid_error('')
            if self.lid_closed is None:
                self.lid_outcome = 'unverified; lid state unavailable'
            try:
                self.publish()
            except Exception:
                LOG.exception('Lid mode publication failed after settings commit')
            self.finish_lid_mode_request(operation)

        if self.enabled and self.lid_fd is None:
            self.acquire_lid_async(acquired)
        else:
            acquired(None)

    @dbus.service.method(IFACE, in_signature="b", out_signature="",
                         async_callbacks=('reply', 'error'))
    def SetLidDimming(self, enabled, reply=None, error=None):
        admission_deadline = time.monotonic() + PUBLIC_MUTATION_TIMEOUT
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        enabled = bool(enabled)
        if reply is not None and not enabled and \
                len(self.dimming_disable_requests) >= MAX_PUBLIC_WAITERS:
            raise dbus.DBusException('Too many pending dimming disable requests',
                                     name=IFACE + '.Busy')
        if reply is not None and not enabled and self.brightness_dim_operation is not None and \
                len(self.brightness_dim_operation['waiters']) >= MAX_PUBLIC_WAITERS:
            raise dbus.DBusException('Too many pending brightness operations',
                                     name=IFACE + '.Busy')

        completed = {'done': False, 'source': 0, 'timed_out': False,
                     'deadline': admission_deadline}
        def finished_async(result):
            if completed['done']:
                return
            if time.monotonic() >= completed['deadline']:
                completed['timed_out'] = True
            completed['done'] = True
            self.dimming_disable_requests.discard(finished_async)
            if completed['source']:
                GLib.source_remove(completed['source'])
                completed['source'] = 0
            try:
                if completed['timed_out']:
                    error(dbus.DBusException('Dimming request exceeded its deadline',
                                             name=IFACE + '.Unavailable'))
                elif self.shutting_down or self.lid_dimming != enabled:
                    error(dbus.DBusException('Dimming request was superseded',
                                             name=IFACE + '.Canceled'))
                elif result in ('pending', 'invalid') or self.brightness_recovery_pending():
                    error(dbus.DBusException('Brightness recovery is pending',
                                             name=IFACE + '.InvalidState'))
                else:
                    try:
                        self.publish()
                    except Exception:
                        LOG.exception('Dimming completion publication failed')
                    reply()
            except Exception:
                LOG.exception('Dimming reply could not be delivered')

        if reply is not None and enabled and self.brightness_dim_operation is not None:
            if len(self.brightness_dim_operation['waiters']) >= MAX_PUBLIC_WAITERS:
                raise dbus.DBusException('Too many pending dimming requests',
                                         name=IFACE + '.Busy')
            if not self.lid_dimming:
                self.persist_settings(lid_dimming=True)
            if time.monotonic() >= admission_deadline:
                finished_async('pending')
                return
            self.brightness_redim_suppressed = False
            self.dim_brightness_async(finished_async)
            return
        if not enabled and self.dimming_verification is not None:
            self.dimming_verification['finish'](dbus.DBusException(
                'Dimming verification was superseded', name=IFACE + '.Canceled'))
        if (reply is not None and enabled and self.lid_dimming and
                self.brightness_state() == 'dimmed-owned'):
            # A repeated enable only needs to prove that the existing journal
            # still owns the current level. Keep the D-Bus read off the loop.
            record = self.brightness_record
            adapter = self.brightness_adapter
            if self.dimming_verification is not None:
                operation = self.dimming_verification
                if operation['record'] == record and operation['adapter'] is adapter:
                    if len(operation['waiters']) >= MAX_PUBLIC_WAITERS:
                        raise dbus.DBusException('Too many pending dimming verifications',
                                                 name=IFACE + '.Busy')
                    operation['waiters'].append((reply, error))
                    return
                operation['finish'](dbus.DBusException(
                    'Brightness ownership changed during verification',
                    name=IFACE + '.Canceled'))
            operation = {'done': False, 'source': 0, 'record': record,
                         'deadline': admission_deadline,
                         'adapter': adapter, 'waiters': [(reply, error)]}

            def finish(failure=None):
                if operation['done']:
                    return
                if failure is None and time.monotonic() >= operation['deadline']:
                    failure = dbus.DBusException('Brightness verification exceeded its deadline',
                                                 name=IFACE + '.Unavailable')
                operation['done'] = True
                if self.dimming_verification is operation:
                    self.dimming_verification = None
                self.dimming_verifications.discard(finish)
                GLib.source_remove(operation['source'])
                for waiter_reply, waiter_error in operation['waiters']:
                    try:
                        if failure is None:
                            waiter_reply()
                        else:
                            waiter_error(failure)
                    except Exception as callback_error:
                        LOG.warning('Dimming enable reply could not be delivered: %s', callback_error)

            def checked(level, failure):
                if operation['done']:
                    return
                if time.monotonic() >= operation['deadline']:
                    failure = TimeoutError('Brightness verification timed out')
                if (not self.shutting_down and self.brightness_adapter is not adapter and
                        self.brightness_record == record and self.brightness_desired_dimmed()):
                    self.brightness_retry_pending = True
                    self.brightness_error = 'Brightness ownership needs recovery: adapter changed'
                    try:
                        self.publish()
                    except Exception:
                        LOG.exception('Brightness adapter-change publication failed')
                if (self.shutting_down or not self.lid_dimming or
                        not self.brightness_desired_dimmed() or
                        self.brightness_adapter is not adapter or
                        self.brightness_record != record):
                    finish(dbus.DBusException('Dimming verification was superseded',
                                              name=IFACE + '.Canceled'))
                    return
                if failure is None:
                    try:
                        persisted = json.loads(self.brightness_path.read_text())
                        if persisted != record:
                            raise ValueError('Brightness recovery record changed on disk')
                        self.validate_brightness_record(persisted, verify_output=True)
                    except (OSError, ValueError) as journal_error:
                        failure = journal_error
                if failure is None and level != record['written']:
                    failure = ValueError('Brightness changed after the recorded dim')
                if failure is not None:
                    self.brightness_retry_pending = True
                    self.brightness_error = 'Brightness ownership needs recovery: ' + str(failure)
                    try:
                        self.publish()
                    except Exception:
                        LOG.exception('Dimming verification failure publication failed')
                    finish(dbus.DBusException('Brightness recovery is pending',
                                              name=IFACE + '.InvalidState'))
                    return
                finish()

            try:
                remaining_ms = int((admission_deadline - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    raise TimeoutError('Brightness verification exceeded its deadline')
                operation['source'] = GLib.timeout_add(remaining_ms,
                    lambda: (checked(None, TimeoutError('Brightness verification timed out')),
                             False)[1])
                if not operation['source']:
                    raise RuntimeError('GLib did not attach the dimming verification deadline')
            except Exception as setup_error:
                error(dbus.DBusException('Brightness verification could not start: ' +
                                         str(setup_error), name=IFACE + '.Unavailable'))
                return
            operation['finish'] = finish
            self.dimming_verification = operation
            self.dimming_verifications.add(finish)
            try:
                persisted = json.loads(self.brightness_path.read_text())
                if persisted != record:
                    raise ValueError('Brightness recovery record changed on disk')
                self.validate_brightness_record(persisted, verify_output=True)
                if adapter is None:
                    raise ValueError('Brightness adapter is unavailable')
                adapter.read_async(checked)
            except (OSError, ValueError, dbus.DBusException) as failure:
                checked(None, failure)
            return
        if enabled and self.has_brightness_journal():
            if reply is not None:
                # The healthy repeated-enable case was verified above. Any
                # other existing journal needs recovery, not a blocking read.
                raise dbus.DBusException('Brightness recovery is pending',
                                         name=IFACE + '.InvalidState')
            if self.brightness_state() != 'dimmed-owned' or not self.verify_owned_dimming():
                raise dbus.DBusException("Brightness recovery is pending", name=IFACE + ".InvalidState")
        if enabled and not self.brightness_available:
            raise dbus.DBusException(self.brightness_error or "Built-in brightness is unavailable",
                                     name=IFACE + ".Unavailable")
        if self.lid_dimming != bool(enabled):
            self.persist_settings(lid_dimming=bool(enabled))
        if enabled:
            self.brightness_redim_suppressed = False
        if reply is not None and enabled and self.brightness_desired_dimmed() and \
                self.brightness_record is None and not self.has_brightness_journal():
            if time.monotonic() >= admission_deadline:
                finished_async('pending')
                return
            self.dim_brightness_async(finished_async)
            return
        if reply is not None and not enabled and (self.brightness_dim_operation is not None or
                                                  self.brightness_record is not None or
                                                  self.has_brightness_journal()):
            self.dimming_disable_requests.add(finished_async)
            def dimming_deadline():
                completed['source'] = 0
                completed['timed_out'] = True
                finished_async('pending')
                return GLib.SOURCE_REMOVE
            try:
                remaining_ms = int((completed['deadline'] - time.monotonic()) * 1000)
                if remaining_ms <= 0:
                    dimming_deadline()
                else:
                    completed['source'] = GLib.timeout_add(remaining_ms, dimming_deadline)
                    if not completed['source']:
                        raise RuntimeError('Dimming disable deadline source unavailable')
            except Exception as setup_error:
                self.dimming_disable_requests.discard(finished_async)
                completed['done'] = True
                error(dbus.DBusException(str(setup_error), name=IFACE + '.Unavailable'))
            def restore_after_dim(_result='unchanged'):
                try:
                    if self.brightness_record is None and self.brightness_journal_state != 'invalid':
                        self.recover_brightness()
                    self.restore_brightness_async(finished_async)
                except Exception as failure:
                    self.brightness_error = 'Brightness recovery pending: ' + str(failure)
                    finished_async('pending')
            if self.brightness_dim_operation is not None:
                self.brightness_dim_operation['waiters'].append(restore_after_dim)
            else:
                restore_after_dim()
            return
        if reply is None:
            self.reconcile_brightness('dimming-enable' if enabled else 'dimming-disable')
            self.publish()
        else:
            try:
                self.publish()
            except Exception:
                LOG.exception('Dimming state publication failed after settings commit')
            if time.monotonic() >= completed['deadline']:
                error(dbus.DBusException('Dimming request exceeded its deadline',
                                         name=IFACE + '.Unavailable'))
            else:
                reply()

    @dbus.service.method(IFACE, in_signature="bu", out_signature="",
                         async_callbacks=('reply', 'error'))
    def SetFailsafe(self, enabled, threshold, reply=None, error=None):
        deadline = time.monotonic() + PUBLIC_MUTATION_TIMEOUT
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        if not 1 <= int(threshold) <= 99:
            raise dbus.DBusException("Threshold must be 1–99", name=IFACE + ".InvalidArgument")
        self.persist_settings(failsafe=bool(enabled), threshold=int(threshold))
        self.failsafe_triggered = False
        if self.suspend_attempt is not None and self.suspend_attempt['origin'] == 'failsafe':
            self.cancel_suspend_attempt('Failsafe settings changed during suspend attempt')
        self.cancel_failsafe_evaluation()
        self.cancel_suspend_preflight('Failsafe settings changed')
        self.cancel_power_refresh()
        try:
            self.refresh_power_async(check_failsafe=True)
        except Exception:
            LOG.exception('Failsafe power refresh setup failed')
        try:
            self.publish()
        except Exception:
            LOG.exception('Failsafe settings publication failed')
        if reply is not None:
            if time.monotonic() >= deadline:
                error(dbus.DBusException('Failsafe request exceeded its deadline',
                                         name=IFACE + '.Unavailable'))
            else:
                reply()

    def finish_timer_start(self, operation, failure=None):
        if self.timer_start is not operation:
            return
        if failure is None and time.monotonic() >= operation['deadline']:
            failure = dbus.DBusException('Timer start exceeded its deadline',
                                         name=IFACE + '.Unavailable')
        self.timer_start = None
        GLib.source_remove(operation['source'])
        for reply, error in operation['waiters']:
            try:
                if failure is None:
                    reply()
                else:
                    error(failure)
            except Exception as callback_error:
                LOG.warning('Timer start reply could not be delivered: %s', callback_error)

    @dbus.service.method(IFACE, in_signature="tbb", out_signature="",
                         async_callbacks=('reply', 'error'))
    def StartTimer(self, seconds, require_lid, require_prevention, reply=None, error=None):
        admission_deadline = time.monotonic() + PUBLIC_MUTATION_TIMEOUT
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        if self.suspend_attempt is not None:
            raise dbus.DBusException('An automatic suspend attempt is in progress',
                                     name=IFACE + '.Busy')
        seconds = int(seconds)
        if not 60 <= seconds <= MAX_SECONDS or seconds % 60:
            raise dbus.DBusException("Duration must be 1 minute to 365d 23h 59m in whole minutes",
                                     name=IFACE + ".InvalidArgument")
        request = (seconds, bool(require_lid), bool(require_prevention))
        if self.timer_start is not None:
            if self.timer_start['request'] == request:
                if len(self.timer_start['waiters']) >= MAX_PUBLIC_WAITERS:
                    raise dbus.DBusException('Too many pending timer requests',
                                             name=IFACE + '.Busy')
                self.timer_start['waiters'].append((reply, error))
                return
            raise dbus.DBusException('Another timer start is pending', name=IFACE + '.Busy')
        operation = {'request': request, 'waiters': [(reply, error)], 'source': 0,
                     'deadline': admission_deadline}
        try:
            remaining_ms = int((admission_deadline - time.monotonic()) * 1000)
            if remaining_ms <= 0:
                raise TimeoutError('Timer start exceeded its deadline')
            operation['source'] = GLib.timeout_add(
                remaining_ms,
                lambda: (self.finish_timer_start(operation, dbus.DBusException(
                    'Timer start exceeded its deadline', name=IFACE + '.Unavailable')),
                    GLib.SOURCE_REMOVE)[1])
            if not operation['source']:
                raise RuntimeError('Timer start deadline source unavailable')
        except Exception as failure:
            if error is not None:
                error(failure)
            else:
                raise
            return
        self.timer_start = operation

        def fresh_power(available):
            if self.timer_start is not operation:
                return
            if self.shutting_down or not available or time.monotonic() >= operation['deadline']:
                self.finish_timer_start(operation, dbus.DBusException(
                    'Fresh power state is unavailable', name=IFACE + '.Unavailable'))
                return
            if require_lid and self.lid_closed is not True:
                failure = dbus.DBusException('Close the lid before starting this timer',
                                             name=IFACE + '.InvalidState')
            elif require_prevention and not self.enabled:
                failure = dbus.DBusException('Enable prevention before starting this timer',
                                             name=IFACE + '.InvalidState')
            else:
                failure = None
            if failure is not None:
                self.finish_timer_start(operation, failure)
                return
            try:
                self.persist_settings(timer_minutes=seconds // 60,
                                      timer_require_lid=bool(require_lid),
                                      timer_require_prevention=bool(require_prevention))
                if time.monotonic() >= operation['deadline']:
                    raise TimeoutError('Timer start exceeded its deadline')
                self.deadline = boottime() + seconds
                fresh_sample = sample_clock_gap()
                self.retain_clock_baseline(fresh_sample)
                self.timer_phase = 'running'
                self.last_timer_state = -1
                self.require_lid = bool(require_lid)
                self.require_prevention = bool(require_prevention)
                self.timer_outcome = ''
                self.last_error = ''
            except Exception as failure:
                self.finish_timer_start(operation, failure)
                return
            try:
                self.publish()
            except Exception as publication_error:
                LOG.warning('Timer started but state publication failed: %s', publication_error)
            self.finish_timer_start(operation)

        # The caller needs post-admission evidence, never a previously queued
        # background read whose observations might predate this request.
        try:
            self.cancel_power_refresh()
            self.refresh_power_async(callback=fresh_power, reconcile_lid=False,
                                     deadline=operation['deadline'])
        except Exception as failure:
            self.finish_timer_start(operation, failure)

    @dbus.service.method(IFACE, in_signature="", out_signature="",
                         async_callbacks=('reply', 'error'))
    def CancelTimer(self, reply=None, error=None):
        deadline = time.monotonic() + PUBLIC_MUTATION_TIMEOUT
        if self.timer_start is not None:
            self.finish_timer_start(self.timer_start, dbus.DBusException(
                'Timer start was canceled', name=IFACE + '.Canceled'))
        if self.suspend_attempt is not None and self.suspend_attempt['origin'] == 'timer':
            self.cancel_suspend_attempt('Automatic suspend attempt was canceled')
        self.cancel_timer()
        if reply is not None:
            if time.monotonic() >= deadline:
                error(dbus.DBusException('Timer cancel exceeded its deadline',
                                         name=IFACE + '.Unavailable'))
            else:
                reply()

    @dbus.service.signal(IFACE, signature="a{sv}")
    def StateChanged(self, state):
        pass

    @dbus.service.signal(IFACE, signature="t")
    def TimerChanged(self, remaining):
        pass

    def cancel_stop_deadline(self):
        source = self.stop_deadline_source
        if source:
            self.stop_deadline_source = 0
            GLib.source_remove(source)

    def finish_stop(self):
        self.cancel_stop_deadline()
        self.loop.quit()

    def stop_deadline_expired(self):
        self.stop_deadline_source = 0
        self.exit_failure = True
        self.disconnect_session_bus('Shutdown exceeded its 24-second remote-work deadline; '
                                    'brightness recovery evidence remains on disk')
        for attr in ('lid_fd', 'fd'):
            fd = getattr(self, attr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, None)
        self.finish_stop()
        return GLib.SOURCE_REMOVE

    def stop(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        self.desired = False
        try:
            self.stop_deadline_source = GLib.timeout_add(
                STOP_REMOTE_TIMEOUT * 1000, self.stop_deadline_expired)
            if not self.stop_deadline_source:
                raise RuntimeError('GLib did not attach the shutdown deadline source')
        except Exception:
            LOG.exception('Shutdown deadline could not be installed')
            self.stop_deadline_expired()
            return
        def cancel_for_stop(label, action):
            try:
                action()
            except Exception:
                LOG.exception('%s cancellation failed during shutdown', label)

        cancel_for_stop('Sleep reconciliation', self.cancel_sleep_reconcile)
        cancel_for_stop('Suspend attempt',
                        lambda: self.cancel_suspend_attempt('Agent is shutting down'))
        cancel_for_stop('Failsafe evaluation', self.cancel_failsafe_evaluation)
        cancel_for_stop('Suspend preflight',
                        lambda: self.cancel_suspend_preflight('Agent is shutting down'))
        unavailable = dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        for cancel in tuple(self.prevention_enable_requests):
            cancel_for_stop('Prevention enable', lambda cancel=cancel: cancel(unavailable))
        for cancel in tuple(self.prevention_disable_requests):
            cancel_for_stop('Prevention disable', lambda cancel=cancel: cancel(unavailable))
        cancel_for_stop('Brightness probe', self.cancel_brightness_probe)
        for cancel in tuple(self.dimming_verifications):
            cancel_for_stop('Dimming verification', lambda cancel=cancel: cancel(unavailable))
        for complete in tuple(self.dimming_disable_requests):
            cancel_for_stop('Dimming disable', lambda complete=complete: complete('pending'))
        if self.lid_mode_request is not None:
            try:
                self.finish_lid_mode_request(self.lid_mode_request, dbus.DBusException(
                    'Agent is shutting down', name=IFACE + '.Unavailable'))
            except Exception:
                LOG.exception('Lid mode request cancellation failed during shutdown')
        try:
            self.cancel_lid_acquisition('Agent is shutting down')
        except Exception:
            LOG.exception('Lid acquisition cancellation failed during shutdown')
        for cancel in tuple(self.panel_registration_cancellers):
            try:
                cancel(dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable'))
            except Exception:
                LOG.exception('Panel registration cancellation failed during shutdown')
        def release_after_brightness(_result):
            try:
                self.release_async(lambda _released: self.finish_stop(), force_disconnect=True,
                                   brightness_reconciled=True)
            except Exception:
                LOG.exception('Asynchronous shutdown release failed')
                self.finish_stop()

        if self.timer_start is not None:
            cancel_for_stop('Timer start',
                            lambda: self.finish_timer_start(self.timer_start, unavailable))
        cancel_for_stop('Power refresh', self.cancel_power_refresh)
        cancel_for_stop('Timer', self.cancel_timer)
        cancel_for_stop('Release retry', self.cancel_release_retry)
        if self.prevention_release is not None:
            # Ordinary off already owns restore-before-release. Join its
            # completion now; joining only the shared brightness callback
            # could run after off has finished and issue a second Uninhibit.
            try:
                self.release_async(lambda _released: self.finish_stop(), force_disconnect=True,
                                   brightness_reconciled=True)
            except Exception:
                LOG.exception('Asynchronous shutdown release join failed')
                self.finish_stop()
            return
        if self.prevention_acquisition is not None:
            # release_async cancels the pending call and restores brightness
            # before closing a connection with an unknown GNOME outcome.
            try:
                self.release_async(lambda _released: self.finish_stop(), force_disconnect=True)
            except Exception:
                LOG.exception('Asynchronous shutdown acquisition release failed')
                self.finish_stop()
            return
        def restore_after_dim(_result='unchanged'):
            try:
                if self.brightness_record is None and self.brightness_journal_state != 'invalid':
                    self.recover_brightness()
                self.restore_brightness_async(release_after_brightness)
            except Exception as error:
                LOG.exception('Brightness restoration failed during shutdown')
                self.brightness_error = 'Brightness restoration failed: ' + str(error)
                release_after_brightness('pending')

        if self.brightness_dim_operation is not None:
            self.brightness_dim_operation['waiters'].append(restore_after_dim)
        else:
            restore_after_dim()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.environ.get("XDG_SESSION_TYPE", "wayland") != "wayland":
        LOG.warning("This prototype is intended for GNOME Wayland")
    lifecycle = {'agent': None, 'stop_requested': False}
    def request_stop():
        agent = lifecycle['agent']
        if agent is None:
            lifecycle['stop_requested'] = True
        else:
            agent.stop()
        return GLib.SOURCE_CONTINUE
    for signum in (signal.SIGTERM, signal.SIGINT):
        source = GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signum, request_stop)
        if not source:
            raise RuntimeError('GLib did not attach the agent shutdown signal')
    agent = Agent()
    lifecycle['agent'] = agent
    if lifecycle['stop_requested']:
        def finish_pending_stop():
            agent.stop()
            return GLib.SOURCE_REMOVE
        GLib.idle_add(finish_pending_stop)
    agent.loop.run()
    if agent.exit_failure:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
