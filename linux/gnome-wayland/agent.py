#!/usr/bin/python3
"""Per-user GNOME Wayland prototype. No privileged helper or global setting edits."""

import json
import logging
import math
import os
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
SYS_CLASS = Path('/sys/class')
MAX_SECONDS = 365 * 86400 + 23 * 3600 + 59 * 60
SUSPEND_GAP_TOLERANCE = 0.5
RELEASE_RETRY_SECONDS = 5
RELEASE_RETRY_LIMIT = 3
RECOVERY_CALL_TIMEOUT = 3
SLEEP_SETTLE_SECONDS = 5
SLEEP_UNCERTAIN_SECONDS = 30
DEFINITE_SUSPEND_REJECTIONS = frozenset((
    'org.freedesktop.DBus.Error.AccessDenied',
    'org.freedesktop.PolicyKit1.Error.NotAuthorized',
    'org.freedesktop.login1.SleepVerbNotSupported',
    'org.freedesktop.login1.OperationInProgress',
))
LOG = logging.getLogger("sleep-disabler")
AGENT_RUNTIME_VERSION = "0.5.0"
API_VERSION = 5
PANEL_VERSION_RE = re.compile(r"^[1-9][0-9]*(?:\.[0-9]+){0,2}$")


class AtomicWriteError(OSError):
    """Atomic-write failure with visibility and durability information."""

    def __init__(self, message, *, committed=False, durable=False):
        super().__init__(message)
        self.committed = committed
        self.durable = durable


def boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def valid_percent(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 100 else None
    except (TypeError, ValueError):
        return None


def data_home():
    path = Path(os.environ.get("XDG_STATE_HOME") or Path.home() / ".local/state")
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

    def __init__(self, agent):
        self.agent = agent

    def properties(self):
        return self.agent.proxy(self.agent.session_bus, BRIGHTNESS, BRIGHTNESS_PATH,
                                "org.freedesktop.DBus.Properties")

    def probe(self):
        xml = str(self.agent.proxy(self.agent.session_bus, BRIGHTNESS, BRIGHTNESS_PATH,
                                   "org.freedesktop.DBus.Introspectable").Introspect(timeout=RECOVERY_CALL_TIMEOUT))
        root = ET.fromstring(xml)
        prop = root.find("./interface[@name='%s']/property[@name='Brightness']" % BRIGHTNESS_IFACE)
        if prop is None or prop.get("type") != "i" or prop.get("access") != "readwrite":
            raise ValueError("GNOME Screen.Brightness read/write property is absent")
        self.read()
        self.output_identity()

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
        value = int(self.properties().Get(BRIGHTNESS_IFACE, "Brightness", timeout=RECOVERY_CALL_TIMEOUT))
        if not 0 <= value <= 100:
            raise ValueError("No usable built-in panel brightness")
        return value

    def write(self, value):
        self.properties().Set(BRIGHTNESS_IFACE, "Brightness", dbus.Int32(value), timeout=RECOVERY_CALL_TIMEOUT)


class Agent(dbus.service.Object):
    def __init__(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.session_bus = dbus.SessionBus()
        self.session_bus.set_exit_on_disconnect(False)
        self.system_bus = dbus.SystemBus()
        self.system_bus.set_exit_on_disconnect(False)
        self.bus_name = dbus.service.BusName(APP, self.session_bus, do_not_queue=True)
        super().__init__(self.bus_name, PATH)
        self.loop = GLib.MainLoop()
        self.fd = None
        self.lid_fd = None
        self.session_cookie = None
        self.session_cookie_state = 'absent'
        self.session_cookie_owner = ''
        self.session_owner_generation = 0
        self.inhibitor_outcome = 'absent'
        self.release_attempts = 0
        self.release_retry_after = 0
        self.release_retry_source = 0
        self.exit_failure = False
        self.enabled = False
        self.desired = False
        self.lid_mode = False
        self.lid_dimming = False
        self.failsafe = False
        self.failsafe_triggered = False
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
        self.clock_gap = boottime() - time.monotonic()
        self.logind_owner_generation = 0
        self.last_prepare_signal = None
        self.sleep_tx = self.idle_sleep_transaction()
        self.sleep_outcome = 'none'
        self.suspend_request_outcome = 'none'
        self.timer_phase = 'idle'
        self.lid_closed = None
        self.on_battery = None
        self.battery_percent = None
        self.battery_discharge_state = 'unknown'
        self.battery_discharging = False
        self.brightness_record = None
        self.brightness_adapter = None
        self.brightness_available = False
        self.brightness_error = ""
        self.brightness_journal_state = 'absent'
        self.brightness_retry_after = 0
        self.brightness_redim_suppressed = False
        self.brightness_retry_pending = False
        self.lid_outcome = "unverified"
        self.recovering = False
        self.shutting_down = False
        self.panel_runtime_version = ''
        self.panel_runtime_sender = ''
        self.last_timer_state = -1
        self.settings_path = data_home() / "preferences.json"
        self.brightness_path = data_home() / "brightness-recovery.json"
        self.watch_bus_disconnects()
        self.check_startup_connections()
        self.load_settings()
        self.refresh_brightness_capability()
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
            arg0=SHELL_BRIGHTNESS)
        self.session_bus.add_signal_receiver(self.on_panel_owner_change,
            signal_name="NameOwnerChanged", dbus_interface="org.freedesktop.DBus")
        self.refresh_power()
        self.reconcile_brightness('startup')
        self.check_startup_connections()
        GLib.timeout_add_seconds(1, self.tick)
        GLib.timeout_add_seconds(15, self.reconcile)
        LOG.info("Agent started; prevention is off")

    def proxy(self, bus, name, path, interface):
        return dbus.Interface(bus.get_object(name, path, introspect=False), interface)

    def property(self, bus, name, path, interface, key):
        return self.proxy(bus, name, path, "org.freedesktop.DBus.Properties").Get(interface, key)

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
                self.reconcile_brightness('settings-reload')
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

    def refresh_brightness_capability(self):
        try:
            adapter = LegacyBrightness(self)
            adapter.probe()
            self.brightness_adapter = adapter
            self.brightness_available = True
            if not self.has_brightness_journal():
                self.brightness_error = ""
        except (dbus.DBusException, OSError, ValueError, ET.ParseError) as error:
            self.brightness_adapter = None
            self.brightness_available = False
            self.brightness_error = "No verified built-in-panel read/write API (GNOME 49+ exposes dimming only)"
            LOG.debug("Brightness probe unavailable: %s", error)
        self.publish()

    def validate_brightness_record(self, record, verify_output=False):
        if (not isinstance(record, dict) or record.get("schema") != 2 or
                not isinstance(record.get('identity'), str) or
                not record['identity'].startswith(LegacyBrightness.identity + ':') or
                not isinstance(record.get('machine'), str) or
                record.get("phase") not in ("prepared", "applied") or
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

    def dim_brightness(self):
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
            record = {"schema": 2, "identity": self.brightness_adapter.output_identity(),
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
            written = self.brightness_adapter.read()
            if written != record['target']:
                raise ValueError('Dim readback differs from requested brightness; prepared journal retained')
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
            current = self.brightness_adapter.read()
            expected = record.get("written", record["target"])
            if current == expected:
                self.brightness_adapter.write(record["before"])
                if self.brightness_adapter.read() != record["before"]:
                    raise ValueError("Restore readback differs from saved brightness")
                self.brightness_error = ""
            elif record["phase"] == "prepared" and current != record["before"]:
                self.brightness_error = "Brightness recovery ambiguous; record retained"
                self.publish()
                self.brightness_retry_pending = True
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

    def brightness_desired_dimmed(self):
        return (not self.shutting_down and self.enabled and self.lid_dimming and
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
        if self.brightness_desired_dimmed() and record.get('phase') == 'prepared':
            return 'redim-pending'
        return 'restore-pending'

    def brightness_recovery_pending(self):
        return self.brightness_state() in (
            'restore-pending', 'redim-pending', 'unreadable', 'invalid')

    def reconcile_brightness(self, trigger):
        """Apply the desired brightness state through the journal validation gate."""
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

    def refresh_power(self, reconcile_lid=True):
        old_lid = self.lid_closed
        try:
            self.on_battery = bool(self.property(self.system_bus, UPOWER, UPOWER_PATH, UPOWER, "OnBattery"))
            present = bool(self.property(self.system_bus, UPOWER, UPOWER_PATH, UPOWER, "LidIsPresent"))
            self.lid_closed = bool(self.property(self.system_bus, UPOWER, UPOWER_PATH, UPOWER, "LidIsClosed")) if present else None
            devices = self.proxy(self.system_bus, UPOWER, UPOWER_PATH, UPOWER).EnumerateDevices()
            batteries = []
            unreadable_battery = False
            for path in devices:
                try:
                    props = self.proxy(self.system_bus, UPOWER, path, "org.freedesktop.DBus.Properties").GetAll("org.freedesktop.UPower.Device")
                    if int(props.get("Type", 0)) == 2 and bool(props.get("PowerSupply", False)) and bool(props.get("IsPresent", False)):
                        batteries.append(props)
                except dbus.DBusException:
                    unreadable_battery = True
            states = []
            for props in batteries:
                state = int(props.get("State", 0))
                if state == 2:
                    states.append('discharging')
                elif state in (1, 4, 5):
                    states.append('not-discharging')
                else:
                    states.append('unknown')
            if 'discharging' in states:
                self.battery_discharge_state = 'discharging'
            elif states and not unreadable_battery and all(state == 'not-discharging' for state in states):
                self.battery_discharge_state = 'not-discharging'
            else:
                self.battery_discharge_state = 'unknown'
            self.battery_discharging = self.battery_discharge_state == 'discharging'
            if unreadable_battery:
                # A readable subset is not aggregate charge evidence. In
                # particular, a high subset percentage cannot rearm an episode.
                self.battery_percent = None
            elif len(batteries) == 1:
                self.battery_percent = valid_percent(batteries[0].get("Percentage"))
            elif len(batteries) > 1:
                display_path = self.proxy(self.system_bus, UPOWER, UPOWER_PATH, UPOWER).GetDisplayDevice()
                self.battery_percent = valid_percent(self.property(self.system_bus, UPOWER, display_path,
                    "org.freedesktop.UPower.Device", "Percentage"))
            else:
                self.battery_percent = None
        except (dbus.DBusException, OSError, TypeError, ValueError) as error:
            LOG.warning("UPower unavailable: %s", error)
            self.on_battery = None
            self.lid_closed = None
            self.battery_percent = None
            self.battery_discharge_state = 'unknown'
            self.battery_discharging = False
        if self.lid_mode and self.lid_closed is None and self.lid_fd is not None:
            self.lid_outcome = "unverified; lid state unavailable"
        if old_lid != self.lid_closed:
            if old_lid is False and self.lid_closed is True:
                self.brightness_redim_suppressed = False
            if reconcile_lid:
                self.handle_lid_change()
        self.publish()

    def handle_lid_change(self):
        if self.lid_closed is False:
            self.reconcile_brightness('lid-open')
            if self.deadline and self.require_lid:
                self.cancel_timer("Timer canceled because the lid opened")
        elif self.lid_closed is True:
            self.reconcile_brightness('lid-close')

    def acquire(self):
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        if self.session_cookie_state != 'absent' or self.session_cookie is not None:
            message = 'GNOME inhibitor release is pending; cannot acquire a duplicate inhibitor'
            self.last_error = message
            self.publish()
            raise dbus.DBusException(message, name=IFACE + '.ReleasePending')
        gnome_call_started = False
        try:
            owner_before = self.session_manager_owner()
            login = self.proxy(self.system_bus, LOGIN, LOGIN_PATH, "org.freedesktop.login1.Manager")
            self.fd = login.Inhibit("sleep", "Sleep Disabler", "Keep this session awake", "block").take()
            gnome = self.proxy(self.session_bus, SESSION, SESSION_PATH, SESSION)
            gnome_call_started = True
            self.session_cookie = int(gnome.Inhibit(APP, dbus.UInt32(0), "Keep this session active", dbus.UInt32(12)))
            self.session_cookie_owner = owner_before
            self.session_cookie_state = 'held'
            self.inhibitor_outcome = 'held'
            owner_after = self.session_manager_owner()
            if owner_after != owner_before:
                raise dbus.DBusException('GNOME SessionManager changed during inhibitor acquisition')
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
        except (dbus.DBusException, OSError) as error:
            self.last_error = str(error)
            self.release()
            if gnome_call_started and self.session_cookie is None:
                self.desired = False
                self.disconnect_session_bus(
                    'GNOME inhibitor acquisition failed with an unknown remote outcome')
            raise dbus.DBusException(self.last_error, name=IFACE + ".Unavailable")

    def acquire_lid(self):
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        if self.lid_fd is not None:
            return
        login = self.proxy(self.system_bus, LOGIN, LOGIN_PATH, "org.freedesktop.login1.Manager")
        self.lid_fd = login.Inhibit("handle-lid-switch", "Sleep Disabler",
                                   "Test lid stay-awake", "block").take()
        self.lid_outcome = "unverified"
        self.set_lid_error('')

    def set_lid_error(self, message):
        previous = self.lid_error
        self.lid_error = message
        if not self.last_error or self.last_error == previous:
            self.last_error = message

    def retry_lid_lock(self):
        if self.shutting_down or not (self.enabled and self.lid_mode) or self.lid_fd is not None:
            return
        try:
            self.acquire_lid()
            self.set_lid_error('')
            self.publish()
        except (dbus.DBusException, OSError) as error:
            self.lid_outcome = 'failed'
            message = 'Lid lock unavailable: ' + str(error)
            if self.lid_error != message:
                LOG.warning('%s', message)
            self.set_lid_error(message)
            self.publish()

    def close_lid(self):
        if self.lid_fd is not None:
            try:
                os.close(self.lid_fd)
            except OSError as error:
                self.last_error = "Could not close lid lock: " + str(error)
                LOG.error(self.last_error)
            self.lid_fd = None

    def clear_session_cookie(self, outcome):
        self.cancel_release_retry()
        self.session_cookie = None
        self.session_cookie_state = 'absent'
        self.session_cookie_owner = ''
        self.inhibitor_outcome = outcome
        self.release_attempts = 0
        self.release_retry_after = 0
        if self.last_error.startswith('GNOME inhibitor release uncertain:'):
            self.last_error = ''

    def cancel_release_retry(self):
        source = getattr(self, 'release_retry_source', 0)
        if source:
            try:
                GLib.source_remove(source)
            except (AttributeError, TypeError):
                pass
            self.release_retry_source = 0

    def schedule_release_retry(self):
        if self.shutting_down or getattr(self, 'release_retry_source', 0) or self.session_cookie_state != 'release-pending':
            return
        self.release_retry_source = GLib.timeout_add_seconds(
            RELEASE_RETRY_SECONDS, self.on_release_retry)

    def on_release_retry(self):
        self.release_retry_source = 0
        if self.shutting_down:
            return False
        if self.session_cookie_state != 'release-pending':
            return False
        if self.attempt_session_release():
            self.publish()
        elif self.release_attempts >= RELEASE_RETRY_LIMIT:
            self.disconnect_session_bus('GNOME inhibitor release remained uncertain after bounded retries')
        else:
            self.schedule_release_retry()
            self.publish()
        return False

    def attempt_session_release(self):
        if self.session_cookie is None or self.session_cookie_state == 'absent':
            return True
        try:
            current_owner = self.session_manager_owner()
        except dbus.DBusException as error:
            return self.mark_release_pending(error)
        if current_owner != self.session_cookie_owner:
            self.clear_session_cookie('owner-lost')
            return True
        try:
            self.proxy(self.session_bus, SESSION, SESSION_PATH, SESSION).Uninhibit(
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
        self.cancel_release_retry()
        self.inhibitor_outcome = 'disconnect-requested'
        self.last_error = reason
        self.publish()
        LOG.error('%s; closing the session bus to release the GNOME inhibitor', reason)
        try:
            self.session_bus.close()
            self.clear_session_cookie('disconnect-completed')
        except (dbus.DBusException, OSError, AttributeError) as error:
            self.inhibitor_outcome = 'disconnect-failed'
            LOG.error('Session-bus disconnect failed: %s', error)
        self.exit_failure = True
        if getattr(self, 'loop', None) is not None:
            self.loop.quit()

    def on_session_bus_disconnected(self, *_args):
        """Drop local resources and exit so systemd can reconnect a fresh agent."""
        self.shutting_down = True
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

    def release(self, force_disconnect=False):
        released = self.attempt_session_release()
        for attr in ("lid_fd", "fd"):
            fd = getattr(self, attr)
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
                setattr(self, attr, None)
        self.enabled = False
        self.reconcile_brightness('prevention-disable')
        self.publish()
        if not released and force_disconnect:
            self.disconnect_session_bus('GNOME inhibitor release remained uncertain during shutdown')
        return released

    def retry_session_release(self):
        """Defensive backstop; the one-shot source owns normal retry timing."""
        if self.session_cookie_state != 'release-pending':
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

    def check_failsafe(self):
        if self.failsafe_condition_cleared():
            self.failsafe_triggered = False
        if not self.failsafe_condition_met():
            return
        if self.shutting_down or self.failsafe_triggered or self.sleep_tx['phase'] != 'idle':
            return
        if not self.suspend_preflight(require_cookie_absent=False)[0]:
            return
        # Read fresh data immediately before the irreversible sleep request.
        self.refresh_power()
        if not self.failsafe_condition_met():
            return
        if not self.suspend_preflight(require_cookie_absent=False)[0]:
            return
        self.failsafe_triggered = True
        self.sleep_now("Battery fell below the failsafe threshold")

    def suspend_preflight(self, require_cookie_absent=False):
        if self.shutting_down:
            return False, 'agent is shutting down'
        if self.sleep_tx['phase'] != 'idle':
            return False, 'a prior sleep transaction is unresolved'
        generation = self.logind_owner_generation
        preparing = self.preparing_for_sleep()
        if self.shutting_down or self.sleep_tx['phase'] != 'idle' or generation != self.logind_owner_generation:
            return False, 'agent lifecycle changed during suspend preflight'
        if preparing is not False:
            return False, ('logind is preparing for sleep' if preparing is True else
                           'logind sleep state is unreadable')
        state = self.session_cookie_state
        if require_cookie_absent:
            if state != 'absent' or self.session_cookie is not None:
                return False, 'GNOME inhibitor release is not confirmed'
            return True, ''
        if state == 'absent' and self.session_cookie is None:
            return True, ''
        if state != 'held' or self.session_cookie is None or not self.session_cookie_owner:
            return False, 'GNOME inhibitor ownership is unresolved'
        try:
            owner = self.session_manager_owner()
        except dbus.DBusException:
            return False, 'GNOME SessionManager owner is unreadable'
        if (self.shutting_down or self.sleep_tx['phase'] != 'idle' or
                generation != self.logind_owner_generation or
                self.session_cookie_state != 'held' or self.session_cookie is None):
            return False, 'inhibitor or agent lifecycle changed during suspend preflight'
        if owner != self.session_cookie_owner:
            return False, 'GNOME inhibitor belongs to a previous SessionManager owner'
        return True, ''

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

    def sleep_now(self, reason):
        origin = 'timer' if reason == 'Countdown elapsed' else 'failsafe'
        timer_require_lid = origin == 'timer' and self.require_lid
        timer_require_prevention = origin == 'timer' and self.require_prevention
        if origin == 'failsafe':
            self.failsafe_triggered = True
        if origin == 'timer':
            self.consume_timer('Countdown elapsed while awake; evaluating suspend')
            if timer_require_lid:
                self.refresh_power(reconcile_lid=False)
        allowed, why = self.suspend_preflight(require_cookie_absent=False)
        if allowed and timer_require_lid and self.lid_closed is not True:
            allowed, why = False, 'timer requires a confirmed closed lid'
        if allowed and timer_require_prevention and not self.enabled:
            allowed, why = False, 'timer requires effective prevention before release'
        if not allowed:
            self.last_error = 'Suspend not requested: ' + why
            if origin == 'timer':
                self.timer_outcome = 'Countdown consumed; ' + self.last_error.lower()
            self.notify('Sleep Disabler', self.last_error)
            self.publish()
            return False
        self.desired = False
        if not self.release():
            self.last_error = 'Suspend not requested: GNOME inhibitor release is uncertain'
            if origin == 'timer':
                self.timer_outcome = 'Countdown consumed; suspend not requested because inhibitor release is uncertain'
            self.notify('Sleep Disabler', self.last_error)
            self.publish()
            return False
        if origin == 'failsafe':
            # Release can wait while AC/battery state changes. Prevention is
            # intentionally off now; require power evidence without enabled.
            self.refresh_power(reconcile_lid=False)
            if not self.failsafe_power_condition_met():
                self.last_error = 'Suspend not requested after inhibitor release: failsafe power conditions no longer confirmed'
                self.notify('Sleep Disabler', self.last_error)
                self.publish()
                return False
        elif timer_require_lid:
            self.refresh_power(reconcile_lid=False)
        allowed, why = self.suspend_preflight(require_cookie_absent=True)
        if allowed and timer_require_lid and self.lid_closed is not True:
            allowed, why = False, 'timer requires a confirmed closed lid'
        if not allowed:
            self.last_error = 'Suspend not requested after inhibitor release: ' + why
            if origin == 'timer':
                self.timer_outcome = 'Countdown consumed; ' + self.last_error.lower()
            self.notify('Sleep Disabler', self.last_error)
            self.publish()
            return False
        if origin == 'failsafe' and not self.failsafe_power_condition_met():
            # The final logind read can also deliver a power/settings signal.
            self.last_error = 'Suspend not requested: failsafe power conditions changed during final preflight'
            self.notify('Sleep Disabler', self.last_error)
            self.publish()
            return False
        LOG.info("Requesting suspend: %s", reason)
        self.last_error = ""
        transaction = self.begin_sleep_request(origin, reason)
        self.publish()
        try:
            self.proxy(self.system_bus, LOGIN, LOGIN_PATH, "org.freedesktop.login1.Manager").Suspend(False)
        except dbus.DBusException as error:
            classification = self.classify_suspend_error(error)
            if not self.record_sleep_request_reply(transaction, classification):
                return self.sleep_outcome == 'proven-resume'
            self.last_error = ('Suspend request rejected: ' if classification == 'rejected'
                               else 'Suspend request outcome unknown: ') + str(error)
            LOG.error(self.last_error)
            self.notify("Sleep Disabler", self.last_error)
            if classification == 'rejected':
                self.resolve_sleep_transaction('rejected')
            else:
                self.publish()
            return False
        if not self.record_sleep_request_reply(transaction, 'accepted'):
            return self.sleep_outcome == 'proven-resume'
        if origin == 'timer':
            self.timer_outcome = 'Countdown consumed; suspend request accepted'
        self.publish()
        return True

    def notify(self, summary, body):
        try:
            self.proxy(self.session_bus, "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications", "org.freedesktop.Notifications").Notify(
                "Sleep Disabler", dbus.UInt32(0), "", summary, body,
                dbus.Array([], signature="s"), dbus.Dictionary({}, signature="sv"), dbus.Int32(5000))
        except dbus.DBusException:
            pass

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

    def begin_sleep_request(self, origin, reason):
        monotonic_now = time.monotonic()
        boot_now = boottime()
        self.sleep_tx = {'phase': 'requesting', 'origin': origin, 'reason': reason,
                         'requested_at': monotonic_now, 'request_reply': 'none',
                         'saw_prepare_true': False, 'saw_prepare_false': False,
                         'gap_baseline': boot_now - monotonic_now,
                         'uncertain_since': monotonic_now,
                         'owner_generation': self.logind_owner_generation}
        return self.sleep_tx

    def record_sleep_request_reply(self, transaction, reply):
        """Apply a method reply only to the transaction that issued the call."""
        if self.sleep_tx is not transaction:
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
            monotonic_now = time.monotonic()
            self.sleep_tx = {'phase': 'preparing', 'origin': 'external', 'reason': '',
                             'requested_at': None, 'request_reply': 'none',
                             'saw_prepare_true': True, 'saw_prepare_false': False,
                             'gap_baseline': boottime() - monotonic_now,
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
        now = boottime() if now is None else now
        monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
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
        self.refresh_power(reconcile_lid=False)
        self.refresh_brightness_capability()
        trigger = 'wake' if classification == 'proven-resume' else (
            'uncertain-resolution' if ('uncertain' in classification or
                                       classification == 'logind-owner-lost')
            else 'failed-preparation')
        self.reconcile_brightness(trigger)
        if self.deadline is not None and now >= self.deadline:
            labels = {
                'proven-resume': 'Countdown expired during suspend; no second suspend requested',
                'failed-preparation': 'Countdown expired during failed sleep preparation; no suspend requested',
            }
            self.cancel_timer(labels.get(classification,
                              'Countdown canceled after uncertain sleep outcome; no suspend requested'))
        elif self.deadline is not None:
            self.timer_conditions_hold()
        self.clock_gap = now - monotonic_now
        self.last_prepare_signal = False
        self.sleep_tx = self.idle_sleep_transaction()
        self.publish()

    def preparing_for_sleep(self):
        try:
            return bool(self.property(self.system_bus, LOGIN, LOGIN_PATH,
                                      'org.freedesktop.login1.Manager', 'PreparingForSleep'))
        except dbus.DBusException as error:
            LOG.debug('Cannot read logind sleep state: %s', error)
            return None

    def timer_conditions_hold(self):
        if self.require_lid and self.lid_closed is not True:
            self.cancel_timer('Timer canceled because the lid is not closed after sleep preparation')
            return False
        if self.require_prevention and not self.enabled:
            self.cancel_timer('Timer canceled because prevention is off after sleep preparation')
            return False
        return True

    def reconcile_sleep_state(self, trigger):
        now = boottime()
        monotonic_now = time.monotonic()
        gap = now - monotonic_now
        if self.sleep_tx['phase'] == 'idle':
            if gap - self.clock_gap < SUSPEND_GAP_TOLERANCE:
                self.clock_gap = gap
                return False
            self.sleep_tx = {'phase': 'reconciling', 'origin': 'external', 'reason': '',
                             'requested_at': None, 'request_reply': 'none',
                             'saw_prepare_true': False, 'saw_prepare_false': False,
                             'gap_baseline': self.clock_gap, 'uncertain_since': monotonic_now,
                             'owner_generation': self.logind_owner_generation}
        tx = self.sleep_tx
        if tx['owner_generation'] != self.logind_owner_generation:
            self.resolve_sleep_transaction('logind-owner-lost', now, monotonic_now)
            return True
        baseline = tx['gap_baseline'] if tx['gap_baseline'] is not None else self.clock_gap
        jumped = gap - baseline >= SUSPEND_GAP_TOLERANCE
        preparing = self.preparing_for_sleep()
        if jumped and preparing is not True:
            self.resolve_sleep_transaction('proven-resume', now, monotonic_now)
            return True
        since = tx['uncertain_since'] if tx['uncertain_since'] is not None else monotonic_now
        elapsed = monotonic_now - since
        if preparing is True:
            tx['phase'] = 'preparing' if elapsed < SLEEP_UNCERTAIN_SECONDS else 'uncertain-blocked'
            if elapsed >= SLEEP_UNCERTAIN_SECONDS:
                self.mark_sleep_uncertain_blocked('preparation-state-uncertain')
            return False
        if preparing is None:
            if elapsed >= SLEEP_UNCERTAIN_SECONDS:
                self.mark_sleep_uncertain_blocked('logind-state-unreadable')
            return False
        tx['phase'] = 'reconciling'
        if elapsed < SLEEP_SETTLE_SECONDS:
            return False
        if tx['saw_prepare_true']:
            classification = 'failed-preparation'
        elif tx['saw_prepare_false']:
            classification = 'uncertain-preparation'
        elif tx['request_reply'] == 'accepted':
            classification = 'accepted-no-suspend'
        elif tx['request_reply'] == 'unknown':
            classification = 'request-outcome-unknown'
        elif tx['saw_prepare_true']:
            classification = 'failed-preparation'
        else:
            classification = 'uncertain-preparation'
        self.resolve_sleep_transaction(classification, now, monotonic_now)
        return True

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
        self.refresh_power()
        self.refresh_brightness_capability()
        if self.brightness_journal_state == 'invalid':
            try:
                if not self.brightness_path.exists():
                    self.brightness_journal_state = 'absent'
                    self.brightness_error = ''
            except OSError:
                pass
        self.reconcile_brightness('periodic')
        self.retry_session_release()
        if self.desired and not self.enabled and not self.recovering and \
                self.session_cookie_state == 'absent':
            self.recovering = True
            try:
                self.acquire()
            except dbus.DBusException:
                pass
            self.recovering = False
        self.retry_lid_lock()
        self.check_failsafe()
        return True

    def on_properties_changed(self, interface, changed, invalidated, **_kwargs):
        if interface in (UPOWER, "org.freedesktop.UPower.Device"):
            self.refresh_power()
            self.check_failsafe()

    def on_device_change(self, *_args):
        self.refresh_power()
        self.check_failsafe()

    def on_prepare_sleep(self, entering):
        if entering:
            self.observe_prepare_enter()
        else:
            self.observe_prepare_exit()

    def on_owner_change(self, name, old, new):
        if name in (BRIGHTNESS, SHELL_BRIGHTNESS) and new:
            self.refresh_brightness_capability()
            self.reconcile_brightness('owner-return')
        elif name in (BRIGHTNESS, SHELL_BRIGHTNESS):
            self.brightness_adapter = None
            self.brightness_available = False
            self.brightness_error = "Brightness service disconnected"
            self.reconcile_brightness('owner-loss')
        elif name == UPOWER:
            self.refresh_power()
        elif name in (LOGIN, SESSION):
            if name == LOGIN and old != new:
                self.set_lid_error('')
            if name == LOGIN and old != new:
                self.logind_owner_generation += 1
                if self.sleep_tx['phase'] != 'idle':
                    self.resolve_sleep_transaction('logind-owner-lost')
                self.last_prepare_signal = None
            if name == SESSION and old != new:
                self.session_owner_generation += 1
                if old and old == self.session_cookie_owner:
                    # NameOwnerChanged proves the issuing GNOME process is gone;
                    # its client-owned cookie cannot belong to the replacement.
                    self.clear_session_cookie('owner-lost')
                    if not self.enabled:
                        self.publish()
            if old and old != new and self.enabled:
                if name == LOGIN:
                    # logind dropped its old inhibitor state with its owner.
                    self.lid_outcome = 'failed' if self.lid_mode else 'unavailable'
                self.release()
                if self.session_cookie_state != 'release-pending':
                    self.last_error = name + " disconnected; retrying"
                self.publish()
            if new and self.desired and not self.enabled:
                GLib.idle_add(self.reconcile_once)
            elif new and name == LOGIN and self.enabled and self.lid_mode:
                GLib.idle_add(self.reconcile_once)

    def on_panel_owner_change(self, name, old, new):
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

    @dbus.service.method(IFACE, in_signature="s", out_signature="", sender_keyword="sender")
    def RegisterPanelRuntime(self, version, sender=None):
        version = str(version)
        sender = str(sender or '')
        if not PANEL_VERSION_RE.fullmatch(version) or not sender.startswith(':'):
            raise dbus.DBusException('Invalid panel runtime registration',
                                     name=IFACE + '.InvalidArgument')
        self.panel_runtime_version = version
        self.panel_runtime_sender = sender
        self.publish()

    @dbus.service.method(IFACE, in_signature="s", out_signature="", sender_keyword="sender")
    def UnregisterPanelRuntime(self, version, sender=None):
        if (str(sender or '') != self.panel_runtime_sender or
                str(version) != self.panel_runtime_version):
            return
        self.panel_runtime_version = ''
        self.panel_runtime_sender = ''
        self.publish()

    @dbus.service.method(IFACE, in_signature="b", out_signature="")
    def SetPrevention(self, enabled):
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        if enabled:
            if self.session_cookie_state != 'absent':
                message = 'GNOME inhibitor release is pending; wait for recovery before enabling'
                self.last_error = message
                self.publish()
                raise dbus.DBusException(message, name=IFACE + '.ReleasePending')
            self.desired = True
            if not self.enabled:
                self.acquire()
            self.refresh_power()
            self.check_failsafe()
        else:
            self.desired = False
            released = self.release()
            if self.deadline and self.require_prevention:
                self.cancel_timer("Timer canceled because prevention is off")
            if not released:
                raise dbus.DBusException(self.last_error, name=IFACE + '.ReleasePending')
        self.reconcile_brightness('prevention-enable' if enabled else 'prevention-disable')

    @dbus.service.method(IFACE, in_signature="b", out_signature="")
    def SetLidMode(self, enabled):
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        enabled = bool(enabled)
        acquired = False
        if enabled and self.enabled and self.lid_fd is None:
            try:
                self.acquire_lid()
                acquired = True
            except (dbus.DBusException, OSError) as error:
                self.lid_outcome = "failed"
                self.set_lid_error("Lid lock unavailable: " + str(error))
                self.publish()
                raise dbus.DBusException(self.lid_error, name=IFACE + ".Unavailable")
        try:
            self.persist_settings(lid_mode=enabled)
        except dbus.DBusException:
            if acquired:
                self.close_lid()
                self.lid_outcome = 'unavailable'
            raise
        self.set_lid_error('')
        if not enabled:
            self.set_lid_error('')
            self.close_lid()
            self.lid_outcome = "unavailable"
        elif self.lid_closed is None:
            self.lid_outcome = "unverified; lid state unavailable"
        else:
            self.set_lid_error('')
        self.publish()

    @dbus.service.method(IFACE, in_signature="b", out_signature="")
    def SetLidDimming(self, enabled):
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        if enabled and self.has_brightness_journal():
            raise dbus.DBusException("Brightness recovery is pending", name=IFACE + ".InvalidState")
        if enabled and not self.brightness_available:
            raise dbus.DBusException(self.brightness_error or "Built-in brightness is unavailable",
                                     name=IFACE + ".Unavailable")
        self.persist_settings(lid_dimming=bool(enabled))
        if enabled:
            self.brightness_redim_suppressed = False
        self.reconcile_brightness('dimming-enable' if enabled else 'dimming-disable')
        self.publish()

    @dbus.service.method(IFACE, in_signature="bu", out_signature="")
    def SetFailsafe(self, enabled, threshold):
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        if not 1 <= int(threshold) <= 99:
            raise dbus.DBusException("Threshold must be 1–99", name=IFACE + ".InvalidArgument")
        self.persist_settings(failsafe=bool(enabled), threshold=int(threshold))
        self.failsafe_triggered = False
        self.refresh_power()
        self.check_failsafe()
        self.publish()

    @dbus.service.method(IFACE, in_signature="tbb", out_signature="")
    def StartTimer(self, seconds, require_lid, require_prevention):
        if self.shutting_down:
            raise dbus.DBusException('Agent is shutting down', name=IFACE + '.Unavailable')
        seconds = int(seconds)
        if not 60 <= seconds <= MAX_SECONDS or seconds % 60:
            raise dbus.DBusException("Duration must be 1 minute to 365d 23h 59m in whole minutes",
                                     name=IFACE + ".InvalidArgument")
        self.refresh_power()
        if require_lid and self.lid_closed is not True:
            raise dbus.DBusException("Close the lid before starting this timer", name=IFACE + ".InvalidState")
        if require_prevention and not self.enabled:
            raise dbus.DBusException("Enable prevention before starting this timer", name=IFACE + ".InvalidState")
        self.persist_settings(timer_minutes=seconds // 60,
                              timer_require_lid=bool(require_lid),
                              timer_require_prevention=bool(require_prevention))
        self.deadline = boottime() + seconds
        self.clock_gap = boottime() - time.monotonic()
        self.timer_phase = 'running'
        self.last_timer_state = -1
        self.require_lid = bool(require_lid)
        self.require_prevention = bool(require_prevention)
        self.timer_outcome = ""
        self.last_error = ""
        self.publish()

    @dbus.service.method(IFACE, in_signature="", out_signature="")
    def CancelTimer(self):
        self.cancel_timer()

    @dbus.service.signal(IFACE, signature="a{sv}")
    def StateChanged(self, state):
        pass

    @dbus.service.signal(IFACE, signature="t")
    def TimerChanged(self, remaining):
        pass

    def stop(self):
        if self.shutting_down:
            return
        self.shutting_down = True
        self.desired = False
        self.cancel_timer()
        self.cancel_release_retry()
        self.reconcile_brightness('stop')
        self.release(force_disconnect=True)
        self.loop.quit()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.environ.get("XDG_SESSION_TYPE", "wayland") != "wayland":
        LOG.warning("This prototype is intended for GNOME Wayland")
    agent = Agent()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: (agent.stop(), False)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: (agent.stop(), False)[1])
    agent.loop.run()
    if agent.exit_failure:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
