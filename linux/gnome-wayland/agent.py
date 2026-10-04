#!/usr/bin/python3
"""Per-user GNOME Wayland prototype. No privileged helper or global setting edits."""

import json
import logging
import math
import os
from pathlib import Path
import signal
import time

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
BRIGHTNESS = "org.gnome.SettingsDaemon.Power.Screen"
BRIGHTNESS_PATH = "/org/gnome/SettingsDaemon/Power"
MAX_SECONDS = 365 * 86400 + 23 * 3600 + 59 * 60
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
        self.lid_closed = None
        self.on_battery = None
        self.battery_percent = None
        self.battery_discharging = False
        self.brightness_before = None
        self.brightness_written = None
        self.brightness_available = False
        self.recovering = False
        self.last_timer_state = -1
        self.settings_path = data_home() / "preferences.json"
        self.brightness_path = data_home() / "brightness-recovery.json"
        self.load_settings()
        self.recover_brightness()
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
        self.refresh_power()
        GLib.timeout_add_seconds(1, self.tick)
        GLib.timeout_add_seconds(15, self.reconcile)
        LOG.info("Agent started; prevention is off")

    def proxy(self, bus, name, path, interface):
        return dbus.Interface(bus.get_object(name, path, introspect=False), interface)

    def property(self, bus, name, path, interface, key):
        return self.proxy(bus, name, path, "org.freedesktop.DBus.Properties").Get(interface, key)

    def load_settings(self):
        try:
            values = json.loads(self.settings_path.read_text())
            self.threshold = max(1, min(99, int(values.get("threshold", 20))))
            self.failsafe = bool(values.get("failsafe", False))
            self.lid_mode = bool(values.get("lid_mode", False))
            self.lid_dimming = bool(values.get("lid_dimming", False))
            self.last_timer_minutes = max(1, min(MAX_SECONDS // 60, int(values.get("timer_minutes", 30))))
            self.last_timer_require_lid = bool(values.get("timer_require_lid", False))
            self.last_timer_require_prevention = bool(values.get("timer_require_prevention", False))
        except (OSError, ValueError, TypeError):
            pass

    def save_settings(self):
        self.settings_path.parent.mkdir(parents=True, exist_ok=True)
        values = {"threshold": self.threshold, "failsafe": self.failsafe,
                  "lid_mode": self.lid_mode, "lid_dimming": self.lid_dimming,
                  "timer_minutes": self.last_timer_minutes,
                  "timer_require_lid": self.last_timer_require_lid,
                  "timer_require_prevention": self.last_timer_require_prevention}
        temporary = self.settings_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(values))
        temporary.replace(self.settings_path)

    def brightness_api(self):
        return self.proxy(self.session_bus, BRIGHTNESS, BRIGHTNESS_PATH, BRIGHTNESS)

    def refresh_brightness_capability(self):
        try:
            percent = int(self.brightness_api().GetPercentage())
            self.brightness_available = 0 <= percent <= 100
        except (dbus.DBusException, ValueError):
            self.brightness_available = False
        self.publish()

    def recover_brightness(self):
        try:
            record = json.loads(self.brightness_path.read_text())
            self.brightness_before = int(record["before"])
            self.brightness_written = int(record["written"])
            self.restore_brightness()
        except (OSError, ValueError, KeyError, TypeError):
            return

    def dim_brightness(self):
        if self.brightness_before is not None:
            return
        try:
            api = self.brightness_api()
            before = int(api.GetPercentage())
            if not 0 <= before <= 100:
                return
            record = {"before": before, "written": 0}
            self.brightness_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.brightness_path.with_suffix(".tmp")
            temporary.write_text(json.dumps(record))
            temporary.replace(self.brightness_path)
            api.SetPercentage(dbus.UInt32(0))
            try:
                written = int(api.GetPercentage())
            except (dbus.DBusException, ValueError):
                written = 0
            if not 0 <= written <= 100:
                written = 0
            record["written"] = written
            temporary.write_text(json.dumps(record))
            temporary.replace(self.brightness_path)
            self.brightness_before = before
            self.brightness_written = written
            self.publish()
        except (dbus.DBusException, OSError, ValueError) as error:
            LOG.warning("Could not dim built-in panel: %s", error)
            self.brightness_path.unlink(missing_ok=True)

    def restore_brightness(self):
        if self.brightness_before is None:
            return
        try:
            api = self.brightness_api()
            if int(api.GetPercentage()) == self.brightness_written:
                api.SetPercentage(dbus.UInt32(self.brightness_before))
            self.brightness_path.unlink(missing_ok=True)
            self.brightness_before = None
            self.brightness_written = None
            self.publish()
        except (dbus.DBusException, OSError, ValueError) as error:
            LOG.warning("Brightness recovery pending: %s", error)

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
        try:
            login = self.proxy(self.system_bus, LOGIN, LOGIN_PATH, "org.freedesktop.login1.Manager")
            self.fd = login.Inhibit("sleep", "Sleep Disabler", "Keep this session awake", "block").take()
            gnome = self.proxy(self.session_bus, SESSION, SESSION_PATH, SESSION)
            self.session_cookie = int(gnome.Inhibit(APP, dbus.UInt32(0), "Keep this session active", dbus.UInt32(12)))
            if self.lid_mode:
                try:
                    self.lid_fd = login.Inhibit("handle-lid-switch", "Sleep Disabler", "Test lid stay-awake", "block").take()
                except dbus.DBusException as error:
                    LOG.warning("Lid inhibitor unavailable: %s", error)
            self.enabled = True
            self.last_error = ""
            self.handle_lid_change()
            self.publish()
            return
        except (dbus.DBusException, OSError) as error:
            self.last_error = str(error)
            self.release()
            raise dbus.DBusException(self.last_error, name=IFACE + ".Unavailable")

    def release(self):
        self.restore_brightness()
        if self.session_cookie is not None:
            try:
                self.proxy(self.session_bus, SESSION, SESSION_PATH, SESSION).Uninhibit(dbus.UInt32(self.session_cookie))
            except dbus.DBusException as error:
                LOG.warning("GNOME session inhibitor release failed: %s", error)
            self.session_cookie = None
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

    def check_failsafe(self):
        if not (self.enabled and self.failsafe and self.on_battery is True and
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
        self.deadline = None
        self.desired = False
        self.release()
        LOG.info("Requesting suspend: %s", reason)
        self.last_sleep_reason = reason
        self.last_error = ""
        self.publish()
        try:
            self.proxy(self.system_bus, LOGIN, LOGIN_PATH, "org.freedesktop.login1.Manager").Suspend(False)
        except dbus.DBusException as error:
            self.last_error = "Suspend failed: " + str(error)
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
            self.last_error = reason
        self.publish()

    def tick(self):
        if self.deadline is not None:
            if self.require_lid and self.lid_closed is not True:
                self.cancel_timer("Timer canceled because the lid is not closed")
            elif self.require_prevention and not self.enabled:
                self.cancel_timer("Timer canceled because prevention is off")
            elif boottime() >= self.deadline:
                self.sleep_now("Countdown elapsed")
            else:
                remaining = math.ceil(self.deadline - boottime())
                displayed_minute = math.ceil(remaining / 60)
                if displayed_minute != self.last_timer_state:
                    self.last_timer_state = displayed_minute
                    self.TimerChanged(dbus.UInt64(remaining))
        return True

    def reconcile(self):
        self.refresh_power()
        self.refresh_brightness_capability()
        if self.brightness_before is not None and not (self.enabled and self.lid_dimming and self.lid_closed):
            self.restore_brightness()
        if self.desired and not self.enabled and not self.recovering:
            self.recovering = True
            try:
                self.acquire()
            except dbus.DBusException:
                pass
            self.recovering = False
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
        if not entering:
            self.refresh_power()
            self.refresh_brightness_capability()
            self.tick()
            if self.last_sleep_reason:
                self.notify("Sleep Disabler", "Resumed after: " + self.last_sleep_reason)
                self.last_sleep_reason = ""

    def on_owner_change(self, name, old, new):
        if name == BRIGHTNESS and new:
            self.recover_brightness()
            self.refresh_brightness_capability()
        elif name == BRIGHTNESS:
            self.brightness_available = False
            self.publish()
        elif name == UPOWER:
            self.refresh_power()
        elif name in (LOGIN, SESSION):
            if old and not new and self.enabled:
                self.release()
                self.last_error = name + " disconnected; retrying"
                self.publish()
            elif new and self.desired and not self.enabled:
                GLib.idle_add(self.reconcile_once)

    def reconcile_once(self):
        self.reconcile()
        return False

    def state(self):
        remaining = max(0, math.ceil(self.deadline - boottime())) if self.deadline is not None else 0
        values = {
            "enabled": dbus.Boolean(self.enabled),
            "desired": dbus.Boolean(self.desired),
            "lidMode": dbus.Boolean(self.lid_mode),
            "lidInhibitor": dbus.Boolean(self.lid_fd is not None),
            "lidDimming": dbus.Boolean(self.lid_dimming),
            "lidClosed": dbus.String("unknown" if self.lid_closed is None else ("yes" if self.lid_closed else "no")),
            "brightnessDimmed": dbus.Boolean(self.brightness_before is not None),
            "brightnessAvailable": dbus.Boolean(self.brightness_available),
            "failsafe": dbus.Boolean(self.failsafe),
            "threshold": dbus.UInt32(self.threshold),
            "onBattery": dbus.String("unknown" if self.on_battery is None else ("yes" if self.on_battery else "no")),
            "batteryPercent": dbus.Double(self.battery_percent if self.battery_percent is not None else -1),
            "timerRemaining": dbus.UInt64(remaining),
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
            self.desired = True
            if not self.enabled:
                self.acquire()
            self.refresh_power()
            self.check_failsafe()
        else:
            self.desired = False
            self.release()
            if self.deadline and self.require_prevention:
                self.cancel_timer("Timer canceled because prevention is off")

    @dbus.service.method(IFACE, in_signature="b", out_signature="")
    def SetLidMode(self, enabled):
        self.lid_mode = bool(enabled)
        self.save_settings()
        if self.enabled:
            self.release()
            self.acquire()
        self.publish()

    @dbus.service.method(IFACE, in_signature="b", out_signature="")
    def SetLidDimming(self, enabled):
        if enabled and not self.brightness_available:
            raise dbus.DBusException("GNOME does not expose the tested screen brightness API",
                                     name=IFACE + ".Unavailable")
        self.lid_dimming = bool(enabled)
        self.save_settings()
        if not enabled:
            self.restore_brightness()
        elif self.enabled and self.lid_closed:
            self.dim_brightness()
        self.publish()

    @dbus.service.method(IFACE, in_signature="bu", out_signature="")
    def SetFailsafe(self, enabled, threshold):
        if not 1 <= int(threshold) <= 99:
            raise dbus.DBusException("Threshold must be 1–99", name=IFACE + ".InvalidArgument")
        self.failsafe = bool(enabled)
        self.threshold = int(threshold)
        self.save_settings()
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
        self.deadline = boottime() + seconds
        self.last_timer_state = -1
        self.require_lid = bool(require_lid)
        self.require_prevention = bool(require_prevention)
        self.last_timer_minutes = seconds // 60
        self.last_timer_require_lid = self.require_lid
        self.last_timer_require_prevention = self.require_prevention
        self.save_settings()
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
        self.release()
        self.loop.quit()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if os.environ.get("XDG_SESSION_TYPE", "wayland") != "wayland":
        LOG.warning("This prototype is intended for GNOME Wayland")
    agent = Agent()
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, lambda: (agent.stop(), False)[1])
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, lambda: (agent.stop(), False)[1])
    agent.loop.run()


if __name__ == "__main__":
    main()
