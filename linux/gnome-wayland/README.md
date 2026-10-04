# GNOME Wayland prototype

This is a per-user prototype of Sleep Disabler for GNOME Wayland on Debian-family distributions. It uses a Python agent plus a GNOME Shell extension. The agent starts with sleep prevention **off** on every fresh process start. It does not edit `logind.conf`, GNOME power settings, kernel sleep state, or system files. This prototype has not yet been run on Linux; your machine's results are the first integration gate.

## Install on the GNOME Wayland machine

From this directory, in a GNOME Wayland terminal:

```sh
sudo apt install python3-dbus python3-gi
sh ./install.sh
```

If the top-bar icon does not appear immediately, log out and back in, then run:

```sh
gnome-extensions enable sleep-disabler@local
```

No build step or root helper is needed. The installer copies files into `~/.local`, enables a per-user `systemd` service, and asks GNOME to enable the extension. To update after copying new source files, run `sh ./install.sh` again. Installation starts prevention off. `~/.local/bin/sleep-disablerctl` works even if the extension fails to load.

## First test

```sh
~/.local/bin/sleep-disablerctl status
~/.local/bin/sleep-disablerctl on
~/.local/bin/sleep-disablerctl status
systemd-inhibit --list
~/.local/bin/sleep-disablerctl off
```

The second status should show `enabled: true`. `systemd-inhibit --list` should show a Sleep Disabler `sleep` block while on and no Sleep Disabler block after off. Test GNOME's normal idle blank and automatic suspend with short temporary timeouts, then restore your usual timeouts. Manual Sleep may prompt or override an inhibitor depending on GNOME policy; do not use manual Sleep as the only idle test.

The top-bar menu can toggle prevention, experimental lid mode, dimming, the battery failsafe, and start/cancel 30-minute, 1-hour, or 2-hour countdowns. The CLI exposes the threshold, arbitrary whole-minute duration, and timer conditions:

```sh
~/.local/bin/sleep-disablerctl failsafe on 20
~/.local/bin/sleep-disablerctl failsafe off
~/.local/bin/sleep-disablerctl timer 45 --prevention-on
~/.local/bin/sleep-disablerctl cancel-timer
~/.local/bin/sleep-disablerctl lid on
~/.local/bin/sleep-disablerctl dim on
```

**A timer or failsafe can request real suspend.** Start with the switch and idle tests; use a short timer only when ready for the laptop to sleep. The failsafe checks for a present system battery, `OnBattery`, discharging state, and a percentage strictly **below** the threshold. Unknown data does not trigger sleep. It releases this app's inhibitors before calling login1 `Suspend(false)`.

Lid stay-awake is explicitly experimental. The agent requests a low-level logind `handle-lid-switch` lock, but GNOME may own lid policy and still suspend. Test this with the machine attended, on AC and battery, with and without an external monitor; keep it closed longer than 30 seconds. `lidInhibitor: true` proves acquisition only, not that GNOME obeys it. Dimming uses GNOME's `org.gnome.SettingsDaemon.Power.Screen` service **only if exposed on your version**. It records the prior percentage, writes zero, and restores only when the current value still matches what it wrote. A recovery record is checked when the systemd service restarts. GNOME versions without that API will log a warning and leave brightness alone. Dimming must be tested separately from lid stay-awake, since a suspended machine cannot run the agent.

For your `[deep]` machine, record `cat /sys/power/mem_sleep` before tests. `[deep]` selects suspend-to-RAM after logind accepts a sleep request; it does not change the inhibitor call. Test resume and brightness restoration after a deliberate timer suspend. Do not change the kernel sleep setting for this prototype.

## Feedback to send

Run these after a test (they are read-only):

```sh
echo "$XDG_SESSION_TYPE $XDG_CURRENT_DESKTOP"
gnome-shell --version
cat /sys/power/mem_sleep
~/.local/bin/sleep-disablerctl status
systemctl --user status sleep-disabler-gnome.service --no-pager
journalctl --user -u sleep-disabler-gnome.service -b --no-pager -n 80
gnome-extensions info sleep-disabler@local
```

Please tell me separately whether (1) idle display blanking stopped, (2) idle system suspend stopped, (3) lid-close suspend stopped, (4) built-in brightness dimmed/restored, and (5) countdown/failsafe sleep succeeded. Avoid posting battery serial numbers or unrelated `systemd-inhibit --list` entries.

## Stop or recover

```sh
~/.local/bin/sleep-disablerctl off
~/.local/bin/sleep-disablerctl cancel-timer
systemctl --user stop sleep-disabler-gnome.service
gnome-extensions disable sleep-disabler@local
```

Stopping the service closes its inhibitor FDs. The normal service auto-restarts on failure, so a crash should also release FDs and then run brightness recovery. If an automatic restart does not happen, `systemctl --user restart sleep-disabler-gnome.service` asks the agent to process its recovery record. To disable future starts, run `systemctl --user disable sleep-disabler-gnome.service`.

## Implementation notes

- System suspend: `org.freedesktop.login1.Manager.Inhibit("sleep", …, "block")` held as a Unix FD. GNOME idle/display: `org.gnome.SessionManager.Inhibit` with flags `4|8`. Both are required before `enabled` becomes true. [login1](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.login1.html), [GNOME SessionManager](https://gnome.pages.gitlab.gnome.org/gnome-session/re04.html)
- Battery/lid: UPower system bus properties and change signals, with a 15-second reconciliation read. [UPower](https://upower.freedesktop.org/docs/UPower/)
- Timer: `CLOCK_BOOTTIME` includes time suspended. Running timers are process-scoped; a service restart cancels them. Preferences survive restart. [Linux clock_gettime](https://man7.org/linux/man-pages/man2/clock_gettime.2.html)
- Top bar: the extension adds a native panel button, calls the app's session D-Bus API, and shows disconnected state if the agent stops. The agent remains independent of GNOME Shell. [GNOME extension guide](https://gjs.guide/extensions/overview/anatomy.html)

GNOME 46–50 are listed in extension metadata so the prototype can be tried on the target releases. That listing is a test range, not a claim that all these versions have passed live tests.
