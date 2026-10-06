#!/usr/bin/python3
"""Per-user GNOME Wayland prototype. No privileged helper or global setting edits."""

import json
import logging
import math
import os
from pathlib import Path
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
LOG = logging.getLogger("sleep-disabler")


def boottime():
    return time.clock_gettime(time.CLOCK_BOOTTIME)


def valid_percent(value):
    try:
        number = float(value)
        return number if math.isfinite(number) and 0 <= number <= 100 else None
    except (TypeError, ValueError):
        return None


def data_home():
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / "sleep-disabler"


def machine_identity():
    identity = Path("/etc/machine-id").read_text(encoding="ascii").strip()
    if not identity:
        raise ValueError("Machine identity is unavailable")
    return identity


def atomic_json(path, values):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(values, stream)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class LegacyBrightness:
    identity = "gnome-settings-daemon:built-in-panel"

    def __init__(self, agent):
        self.agent = agent

    def properties(self):
        return self.agent.proxy(self.agent.session_bus, BRIGHTNESS, BRIGHTNESS_PATH,
                                "org.freedesktop.DBus.Properties")

    def probe(self):
        xml = str(self.agent.proxy(self.agent.session_bus, BRIGHTNESS, BRIGHTNESS_PATH,
                                   "org.freedesktop.DBus.Introspectable").Introspect())
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
        value = int(self.properties().Get(BRIGHTNESS_IFACE, "Brightness"))
        if not 0 <= value <= 100:
            raise ValueError("No usable built-in panel brightness")
        return value

    def write(self, value):
        self.properties().Set(BRIGHTNESS_IFACE, "Brightness", dbus.Int32(value))


class Agent(dbus.service.Object):
    def __init__(self):
        dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
        self.session_bus = dbus.SessionBus()
        self.system_bus = dbus.SystemBus()
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
        self.exit_failure = False
        self.enabled = False
        self.desired = False
        self.lid_mode = False
        self.lid_dimming = False
        self.failsafe = False
        self.threshold = 20
        self.last_timer_minutes = 30
        self.last_timer_require_lid = False
        self.last_timer_require_prevention = False
        self.deadline = None
        self.require_lid = False
        self.require_prevention = False
        self.last_error = ""
        self.last_sleep_reason = ""
        self.timer_outcome = ""
        self.sleeping = False
        self.resume_pending = False
        self.clock_gap = boottime() - time.monotonic()
        self.resume_pending_since = None
        self.sleep_started = None
        self.sleep_gap_baseline = None
        self.timer_phase = 'idle'
        self.lid_closed = None
        self.on_battery = None
        self.battery_percent = None
        self.battery_discharging = False
        self.brightness_record = None
        self.brightness_adapter = None
        self.brightness_available = False
        self.brightness_error = ""
        self.brightness_journal_state = 'absent'
        self.brightness_retry_after = 0
        self.lid_outcome = "unverified"
        self.recovering = False
        self.last_timer_state = -1
        self.settings_path = data_home() / "preferences.json"
        self.brightness_path = data_home() / "brightness-recovery.json"
        self.load_settings()
        self.refresh_brightness_capability()
        self.recover_brightness()
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
        self.refresh_power()
        GLib.timeout_add_seconds(1, self.tick)
        GLib.timeout_add_seconds(15, self.reconcile)
        LOG.info("Agent started; prevention is off")

    def proxy(self, bus, name, path, interface):
        return dbus.Interface(bus.get_object(name, path, introspect=False), interface)

    def property(self, bus, name, path, interface, key):
        return self.proxy(bus, name, path, "org.freedesktop.DBus.Properties").Get(interface, key)

    def session_manager_owner(self):
        return str(self.session_bus.get_name_owner(SESSION))

    def load_settings(self):
        try:
            values = json.loads(self.settings_path.read_text())
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
            self.threshold = threshold
            self.failsafe = values.get("failsafe", False)
            self.lid_mode = values.get("lid_mode", False)
            self.lid_dimming = values.get("lid_dimming", False)
            self.last_timer_minutes = minutes
            self.last_timer_require_lid = values.get("timer_require_lid", False)
            self.last_timer_require_prevention = values.get("timer_require_prevention", False)
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
        if time.monotonic() < self.brightness_retry_after:
            return
        try:
            record = json.loads(self.brightness_path.read_text())
        except FileNotFoundError:
            self.brightness_journal_state = 'absent'
            self.brightness_record = None
            return
        except OSError as error:
            self.brightness_journal_state = 'unreadable'
            self.brightness_retry_after = time.monotonic() + 15
            self.brightness_error = "Unreadable brightness recovery record: " + str(error)
            self.publish()
            return
        except ValueError as error:
            self.brightness_journal_state = 'invalid'
            self.brightness_error = "Invalid brightness recovery record: " + str(error)
            self.publish()
            return
        try:
            self.validate_brightness_record(record)
        except (OSError, ValueError) as error:
            self.mark_brightness_validation_error(error)
            return
        self.brightness_record = record
        self.brightness_journal_state = 'pending'
        self.brightness_retry_after = 0
        self.restore_brightness()

    def dim_brightness(self):
        if self.brightness_record is None and self.has_brightness_journal():
            self.recover_brightness()
            # A resolved manual conflict must not trigger a new dim in the
            # same lid event.
            return
        if self.has_brightness_journal() or not self.brightness_adapter:
            return
        try:
            before = self.brightness_adapter.read()
            if before == 0:
                return
            record = {"schema": 2, "identity": self.brightness_adapter.output_identity(),
                      "machine": machine_identity(),
                      "before": before, "target": 0, "phase": "prepared"}
            atomic_json(self.brightness_path, record)
            self.brightness_record = record
            self.brightness_journal_state = 'pending'
            self.brightness_adapter.write(0)
            applied = dict(record, written=self.brightness_adapter.read(), phase="applied")
            atomic_json(self.brightness_path, applied)
            self.brightness_record = applied
            self.brightness_error = ""
            self.publish()
        except (dbus.DBusException, OSError, ValueError) as error:
            LOG.warning("Could not dim built-in panel: %s", error)
            self.brightness_error = ("Brightness recovery pending: " if self.brightness_record is not None
                                     else "Could not dim built-in panel: ") + str(error)
            self.publish()

    def restore_brightness(self):
        record = self.brightness_record
        if record is None or self.brightness_adapter is None:
            return
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
                return
            else:
                self.brightness_error = "Manual brightness change preserved"
            self.brightness_path.unlink(missing_ok=True)
            self.brightness_record = None
            self.brightness_journal_state = 'resolved'
            self.publish()
        except dbus.DBusException as error:
            LOG.warning("Brightness recovery pending: %s", error)
            self.brightness_error = "Brightness recovery pending: " + str(error)
            self.publish()
        except (OSError, ValueError, json.JSONDecodeError) as error:
            LOG.warning("Brightness recovery identity check failed: %s", error)
            self.mark_brightness_validation_error(error)

    def refresh_power(self):
        old_lid = self.lid_closed
        try:
            self.on_battery = bool(self.property(self.system_bus, UPOWER, UPOWER_PATH, UPOWER, "OnBattery"))
            present = bool(self.property(self.system_bus, UPOWER, UPOWER_PATH, UPOWER, "LidIsPresent"))
            self.lid_closed = bool(self.property(self.system_bus, UPOWER, UPOWER_PATH, UPOWER, "LidIsClosed")) if present else None
            devices = self.proxy(self.system_bus, UPOWER, UPOWER_PATH, UPOWER).EnumerateDevices()
            batteries = []
            for path in devices:
                try:
                    props = self.proxy(self.system_bus, UPOWER, path, "org.freedesktop.DBus.Properties").GetAll("org.freedesktop.UPower.Device")
                    if int(props.get("Type", 0)) == 2 and bool(props.get("PowerSupply", False)) and bool(props.get("IsPresent", False)):
                        batteries.append(props)
                except dbus.DBusException:
                    continue
            self.battery_discharging = bool(batteries) and any(int(p.get("State", 0)) == 2 for p in batteries)
            if len(batteries) == 1:
                self.battery_percent = valid_percent(batteries[0].get("Percentage"))
            elif len(batteries) > 1:
                display_path = self.proxy(self.system_bus, UPOWER, UPOWER_PATH, UPOWER).GetDisplayDevice()
                self.battery_percent = valid_percent(self.property(self.system_bus, UPOWER, display_path,
                    "org.freedesktop.UPower.Device", "Percentage"))
            else:
                self.battery_percent = None
        except dbus.DBusException as error:
            LOG.warning("UPower unavailable: %s", error)
            self.on_battery = None
            self.lid_closed = None
            self.battery_percent = None
            self.battery_discharging = False
        if self.lid_mode and self.lid_closed is None and self.lid_fd is not None:
            self.lid_outcome = "unverified; lid state unavailable"
        if old_lid != self.lid_closed:
            self.handle_lid_change()
        self.publish()

    def handle_lid_change(self):
        if self.lid_closed is False:
            self.restore_brightness()
            if self.deadline and self.require_lid:
                self.cancel_timer("Timer canceled because the lid opened")
        elif self.lid_closed is True and self.enabled and self.lid_dimming:
            self.dim_brightness()

    def acquire(self):
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
        if self.lid_fd is not None:
            return
        login = self.proxy(self.system_bus, LOGIN, LOGIN_PATH, "org.freedesktop.login1.Manager")
        self.lid_fd = login.Inhibit("handle-lid-switch", "Sleep Disabler",
                                   "Test lid stay-awake", "block").take()
        self.lid_outcome = "unverified"

    def retry_lid_lock(self):
        if not (self.enabled and self.lid_mode) or self.lid_fd is not None:
            return
        try:
            self.acquire_lid()
            if self.last_error.startswith('Lid lock unavailable:'):
                self.last_error = ''
            self.publish()
        except (dbus.DBusException, OSError) as error:
            self.lid_outcome = 'failed'
            message = 'Lid lock unavailable: ' + str(error)
            if self.last_error != message:
                LOG.warning('%s', message)
            self.last_error = message
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
        self.session_cookie = None
        self.session_cookie_state = 'absent'
        self.session_cookie_owner = ''
        self.inhibitor_outcome = outcome
        self.release_attempts = 0
        self.release_retry_after = 0
        if self.last_error.startswith('GNOME inhibitor release uncertain:'):
            self.last_error = ''

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
                dbus.UInt32(self.session_cookie))
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
        return False

    def disconnect_session_bus(self, reason):
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

    def release(self, force_disconnect=False):
        self.restore_brightness()
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
        self.publish()
        if not released and force_disconnect:
            self.disconnect_session_bus('GNOME inhibitor release remained uncertain during shutdown')
        return released

    def retry_session_release(self):
        if self.session_cookie_state != 'release-pending':
            return
        if time.monotonic() < self.release_retry_after:
            return
        if self.attempt_session_release():
            self.publish()
        elif self.release_attempts >= RELEASE_RETRY_LIMIT:
            self.disconnect_session_bus('GNOME inhibitor release remained uncertain after bounded retries')
        else:
            self.publish()

    def check_failsafe(self):
        if not (self.enabled and not self.sleeping and not self.resume_pending and
                self.failsafe and self.on_battery is True and
                self.battery_discharging and self.battery_percent is not None and
                self.battery_percent < self.threshold):
            return
        # Read fresh data immediately before the irreversible sleep request.
        self.refresh_power()
        if not (self.enabled and self.on_battery is True and self.battery_discharging and
                self.battery_percent is not None and self.battery_percent < self.threshold):
            return
        self.sleep_now("Battery fell below the failsafe threshold")

    def sleep_now(self, reason):
        if reason == 'Countdown elapsed':
            self.timer_outcome = 'Countdown elapsed while awake; suspend requested'
            self.timer_phase = 'consumed'
        self.deadline = None
        self.require_lid = False
        self.require_prevention = False
        self.desired = False
        if not self.release():
            self.last_sleep_reason = ''
            self.last_error = 'Suspend not requested: GNOME inhibitor release is uncertain'
            if reason == 'Countdown elapsed':
                self.timer_outcome = 'Countdown consumed; suspend not requested because inhibitor release is uncertain'
            self.notify('Sleep Disabler', self.last_error)
            self.publish()
            return
        LOG.info("Requesting suspend: %s", reason)
        self.last_sleep_reason = reason
        self.last_error = ""
        self.publish()
        try:
            self.proxy(self.system_bus, LOGIN, LOGIN_PATH, "org.freedesktop.login1.Manager").Suspend(False)
        except dbus.DBusException as error:
            self.last_error = "Suspend failed: " + str(error)
            if reason == 'Countdown elapsed':
                self.timer_outcome = 'Countdown suspend refused'
            self.last_sleep_reason = ""
            LOG.error(self.last_error)
            self.notify("Sleep Disabler", self.last_error)
            self.publish()

    def notify(self, summary, body):
        try:
            self.proxy(self.session_bus, "org.freedesktop.Notifications",
                "/org/freedesktop/Notifications", "org.freedesktop.Notifications").Notify(
                "Sleep Disabler", dbus.UInt32(0), "", summary, body,
                dbus.Array([], signature="s"), dbus.Dictionary({}, signature="sv"), dbus.Int32(5000))
        except dbus.DBusException:
            pass

    def cancel_timer(self, reason=""):
        self.deadline = None
        self.require_lid = False
        self.require_prevention = False
        if reason:
            self.timer_outcome = reason
        self.timer_phase = 'canceled'
        self.last_timer_state = -1
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

    def finish_wake(self, classification, now=None, monotonic_now=None):
        now = boottime() if now is None else now
        monotonic_now = time.monotonic() if monotonic_now is None else monotonic_now
        self.refresh_power()
        self.refresh_brightness_capability()
        if self.brightness_record is None and self.brightness_journal_state != 'invalid':
            self.recover_brightness()
        if self.brightness_record is not None:
            self.restore_brightness()
        if self.deadline is not None and now >= self.deadline:
            outcomes = {
                'suspended': 'Countdown expired during suspend; no second suspend requested',
                'failed': 'Countdown expired during failed sleep preparation; no suspend requested',
                'uncertain': 'Countdown canceled after uncertain wake; no suspend requested',
            }
            self.cancel_timer(outcomes[classification])
        elif self.deadline is not None and self.timer_conditions_hold():
            self.timer_phase = 'running'
        self.sleeping = False
        self.resume_pending = False
        self.resume_pending_since = None
        self.sleep_started = None
        self.sleep_gap_baseline = None
        self.clock_gap = now - monotonic_now
        if self.last_sleep_reason and classification == 'suspended':
            self.notify('Sleep Disabler', 'Resumed after: ' + self.last_sleep_reason)
            self.last_sleep_reason = ''
        elif classification != 'suspended':
            self.last_sleep_reason = ''
        self.publish()

    def tick(self):
        now = boottime()
        monotonic_now = time.monotonic()
        gap = now - monotonic_now
        gap_delta = gap - (self.sleep_gap_baseline if self.sleep_gap_baseline is not None
                           else self.clock_gap)
        jumped = gap_delta >= SUSPEND_GAP_TOLERANCE
        if jumped or self.sleeping or self.resume_pending:
            if jumped and not self.resume_pending:
                self.resume_pending = True
                self.resume_pending_since = monotonic_now
                self.timer_phase = 'reconciling' if self.deadline is not None else self.timer_phase
            preparing = self.preparing_for_sleep()
            if preparing is False:
                classification = 'suspended' if jumped else ('failed' if self.sleep_started is not None
                                                              else 'uncertain')
                self.finish_wake(classification, now, monotonic_now)
            elif preparing is None and (jumped or (self.resume_pending_since is not None and
                    monotonic_now - self.resume_pending_since >= 5)):
                self.finish_wake('suspended' if jumped else 'uncertain', now, monotonic_now)
            elif preparing is True and self.resume_pending_since is not None and \
                    monotonic_now - self.resume_pending_since >= 30:
                if self.deadline is not None:
                    self.cancel_timer('Countdown canceled because wake state remained uncertain')
                self.resume_pending = False
                self.resume_pending_since = None
                self.sleeping = False
                self.sleep_started = None
                self.sleep_gap_baseline = None
                self.clock_gap = gap
                self.timer_phase = 'canceled'
            elif preparing is True:
                self.sleeping = True
                if self.resume_pending_since is None:
                    self.resume_pending_since = monotonic_now
        else:
            self.clock_gap = gap
        if self.deadline is not None:
            if self.sleeping or self.resume_pending:
                return True
            if self.require_lid and self.lid_closed is not True:
                self.cancel_timer("Timer canceled because the lid is not closed")
            elif self.require_prevention and not self.enabled:
                self.cancel_timer("Timer canceled because prevention is off")
            elif now >= self.deadline:
                preparing = self.preparing_for_sleep()
                if preparing is True or preparing is None:
                    self.cancel_timer('Countdown canceled because sleep state is uncertain')
                    return True
                self.sleep_now("Countdown elapsed")
            else:
                remaining = math.ceil(self.deadline - now)
                displayed_minute = math.ceil(remaining / 60)
                if displayed_minute != self.last_timer_state:
                    self.last_timer_state = displayed_minute
                    self.TimerChanged(dbus.UInt64(remaining))
        return True

    def reconcile(self):
        self.refresh_power()
        self.refresh_brightness_capability()
        if self.brightness_journal_state == 'invalid':
            try:
                if not self.brightness_path.exists():
                    self.brightness_journal_state = 'absent'
                    self.brightness_error = ''
            except OSError:
                pass
        if self.brightness_record is None and self.brightness_journal_state != 'invalid':
            self.recover_brightness()
        if self.brightness_record is not None and not (self.enabled and self.lid_dimming and self.lid_closed):
            self.restore_brightness()
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
            if self.sleeping:
                return
            self.sleeping = True
            self.resume_pending = True
            monotonic_now = time.monotonic()
            boot_now = boottime()
            self.resume_pending_since = monotonic_now
            self.sleep_started = boot_now
            self.sleep_gap_baseline = boot_now - monotonic_now
            if self.deadline is not None:
                self.timer_phase = 'entering-sleep'
        else:
            boot_now = boottime()
            monotonic_now = time.monotonic()
            baseline = self.sleep_gap_baseline if self.sleep_gap_baseline is not None else self.clock_gap
            suspended = boot_now - monotonic_now - baseline >= SUSPEND_GAP_TOLERANCE
            classification = 'suspended' if suspended else ('failed' if self.sleep_started is not None
                                                              else 'uncertain')
            self.finish_wake(classification, boot_now, monotonic_now)
            self.tick()

    def on_owner_change(self, name, old, new):
        if name in (BRIGHTNESS, SHELL_BRIGHTNESS) and new:
            self.refresh_brightness_capability()
            self.recover_brightness()
        elif name in (BRIGHTNESS, SHELL_BRIGHTNESS):
            self.brightness_adapter = None
            self.brightness_available = False
            self.brightness_error = "Brightness service disconnected"
            self.publish()
        elif name == UPOWER:
            self.refresh_power()
        elif name in (LOGIN, SESSION):
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

    def reconcile_once(self):
        self.reconcile()
        return False

    def state(self):
        remaining = max(0, math.ceil(self.deadline - boottime())) if self.deadline is not None else 0
        values = {
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
            "lidInhibitor": dbus.Boolean(self.lid_fd is not None),
            "lidDimming": dbus.Boolean(self.lid_dimming),
            "lidClosed": dbus.String("unknown" if self.lid_closed is None else ("yes" if self.lid_closed else "no")),
            "brightnessDimmed": dbus.Boolean(self.brightness_record is not None and
                                             self.brightness_record.get("phase") == "applied"),
            "brightnessAvailable": dbus.Boolean(self.brightness_available),
            "brightnessError": dbus.String(self.brightness_error),
            "brightnessJournalState": dbus.String(self.brightness_journal_state),
            "brightnessRecoveryPending": dbus.Boolean(self.has_brightness_journal()),
            "failsafe": dbus.Boolean(self.failsafe),
            "threshold": dbus.UInt32(self.threshold),
            "onBattery": dbus.String("unknown" if self.on_battery is None else ("yes" if self.on_battery else "no")),
            "batteryPercent": dbus.Double(self.battery_percent if self.battery_percent is not None else -1),
            "timerRemaining": dbus.UInt64(remaining),
            "timerOutcome": dbus.String(self.timer_outcome),
            "timerPhase": dbus.String(self.timer_phase),
            "timerRequireLid": dbus.Boolean(self.require_lid),
            "timerRequirePrevention": dbus.Boolean(self.require_prevention),
            "timerDefaultMinutes": dbus.UInt32(self.last_timer_minutes),
            "timerDefaultRequireLid": dbus.Boolean(self.last_timer_require_lid),
            "timerDefaultRequirePrevention": dbus.Boolean(self.last_timer_require_prevention),
            "error": dbus.String(self.last_error),
        }
        return dbus.Dictionary(values, signature="sv")

    def publish(self):
        self.StateChanged(self.state())

    @dbus.service.method(IFACE, in_signature="", out_signature="a{sv}")
    def GetState(self):
        return self.state()

    @dbus.service.method(IFACE, in_signature="b", out_signature="")
    def SetPrevention(self, enabled):
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

    @dbus.service.method(IFACE, in_signature="b", out_signature="")
    def SetLidMode(self, enabled):
        enabled = bool(enabled)
        acquired = False
        if enabled and self.enabled and self.lid_fd is None:
            try:
                self.acquire_lid()
                acquired = True
            except (dbus.DBusException, OSError) as error:
                self.lid_outcome = "failed"
                self.last_error = "Lid lock unavailable: " + str(error)
                self.publish()
                raise dbus.DBusException(self.last_error, name=IFACE + ".Unavailable")
        try:
            self.persist_settings(lid_mode=enabled)
        except dbus.DBusException:
            if acquired:
                self.close_lid()
                self.lid_outcome = 'unavailable'
            raise
        if not enabled:
            self.close_lid()
            self.lid_outcome = "unavailable"
        elif self.lid_closed is None:
            self.lid_outcome = "unverified; lid state unavailable"
        self.publish()

    @dbus.service.method(IFACE, in_signature="b", out_signature="")
    def SetLidDimming(self, enabled):
        if enabled and self.has_brightness_journal():
            raise dbus.DBusException("Brightness recovery is pending", name=IFACE + ".InvalidState")
        if enabled and not self.brightness_available:
            raise dbus.DBusException(self.brightness_error or "Built-in brightness is unavailable",
                                     name=IFACE + ".Unavailable")
        self.persist_settings(lid_dimming=bool(enabled))
        if not enabled:
            self.restore_brightness()
        elif self.enabled and self.lid_closed:
            self.dim_brightness()
        self.publish()

    @dbus.service.method(IFACE, in_signature="bu", out_signature="")
    def SetFailsafe(self, enabled, threshold):
        if not 1 <= int(threshold) <= 99:
            raise dbus.DBusException("Threshold must be 1–99", name=IFACE + ".InvalidArgument")
        self.persist_settings(failsafe=bool(enabled), threshold=int(threshold))
        self.refresh_power()
        self.check_failsafe()
        self.publish()

    @dbus.service.method(IFACE, in_signature="tbb", out_signature="")
    def StartTimer(self, seconds, require_lid, require_prevention):
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
        self.sleep_gap_baseline = None
        self.sleeping = False
        self.resume_pending = False
        self.resume_pending_since = None
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
        self.desired = False
        self.cancel_timer()
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
