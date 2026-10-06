# GNOME Wayland prototype

This is a per-user prototype of Sleep Disabler for GNOME Wayland on Debian-family distributions. It uses a Python agent plus a GNOME Shell extension. The agent starts with sleep prevention **off** on every fresh process start. It does not edit `logind.conf`, GNOME power settings, kernel sleep state, or system files. The code has passed local state-machine tests but has not yet been run in a Linux GNOME session.

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

No build step or root helper is needed. The installer copies files into `~/.local`, enables a per-user `systemd` service, and asks GNOME to enable the extension. To update after copying new source files, run `sh ./install.sh` again. Installation starts prevention off. Exit status `0` means the agent API responded and Shell reports the extension active; `2` means the agent works but the top-bar extension is pending; `1` means the agent install failed. Even status `0` requires a visual panel check. `~/.local/bin/sleep-disablerctl` works if the extension fails to load.

## First test

```sh
~/.local/bin/sleep-disablerctl status
~/.local/bin/sleep-disablerctl on
~/.local/bin/sleep-disablerctl status
systemd-inhibit --list
~/.local/bin/sleep-disablerctl off
```

The second status should show `enabled: true`. `systemd-inhibit --list` should show a Sleep Disabler `sleep` block while on and no Sleep Disabler block after off. A clean off state has `desired: false`, `enabled: false`, `gnomeInhibitorState: absent`, and `gnomeReleasePending: false`. If GNOME does not confirm `Uninhibit`, off is accepted as the requested state but the call reports an error and status shows release pending. The agent retries only against the same SessionManager owner. After three ambiguous results it closes its session-bus connection and exits so GNOME drops client-owned cookies; the user service then restarts it with prevention off. Test GNOME's normal idle blank and automatic suspend with short temporary timeouts, then restore your usual timeouts. Manual Sleep may prompt or override an inhibitor depending on GNOME policy; do not use manual Sleep as the only idle test.

The top-bar menu can toggle prevention, experimental lid mode, dimming, the battery failsafe, and start/cancel 30-minute, 1-hour, or 2-hour countdowns. The CLI exposes the threshold, arbitrary whole-minute duration, and timer conditions:

```sh
~/.local/bin/sleep-disablerctl failsafe on 20
~/.local/bin/sleep-disablerctl failsafe off
~/.local/bin/sleep-disablerctl timer 45 --prevention-on
~/.local/bin/sleep-disablerctl cancel-timer
~/.local/bin/sleep-disablerctl lid on
~/.local/bin/sleep-disablerctl dim on
```

**A timer or failsafe can request real suspend.** Start with the switch and idle tests; use a short timer only when ready for the laptop to sleep. The failsafe checks for a present system battery, `OnBattery`, discharging state, and a percentage strictly **below** the threshold. Unknown data does not trigger sleep. It releases this app's inhibitors before calling login1 `Suspend(false)`. If this app's GNOME cookie is still held or its release is uncertain, the trigger is consumed, suspend is not called, and the pending-release recovery path runs without rearming the trigger.

Lid stay-awake is experimental. The agent requests a low-level logind `handle-lid-switch` lock, but GNOME may own lid policy and still suspend. Test this with the machine attended, on AC and battery, with and without an external monitor; keep it closed longer than 30 seconds. `lidRequested`, `lidLockAcquired`, and `lidOutcome` distinguish the preference, FD acquisition, and physical verification. The agent never claims physical verification automatically.

On GNOME 46–48, dimming probes the `org.gnome.SettingsDaemon.Power.Screen` **`Brightness` property** at service `org.gnome.SettingsDaemon.Power`. It enables writing only when Linux reports exactly one connected internal DRM connector (`eDP`, `LVDS`, or `DSI`) and one backlight device. The recovery journal records those identities with `/etc/machine-id` before writing zero. Immediately before restoration, the agent rereads the journal and `/etc/machine-id`, requires the persisted record to equal the in-memory record, rechecks the adapter/output identity, and restores only if the current value still equals what it wrote. A changed or unreadable identity leaves brightness and the journal untouched. This is a conservative single-panel rule, not proof that every GNOME/hardware combination maps the legacy property to the same physical panel; physical dim/restore remains a release gate. The API read/compare/write sequence is not atomic. A copied, malformed, or older schema-1 journal is retained for manual attention. Zero is the API's minimum brightness value; whether it blanks the panel depends on hardware. A retained valid record is retried after agent restart, brightness-service restart, lid opening, feature disable, resume, and periodic reconciliation. A manual brightness change is preserved. The service has `Restart=on-failure` and a 30-second normal stop timeout, so recovery is attempted while the user manager and service are available; this does not guarantee prompt restoration after logout, systemd failure, or a disabled service. `brightnessJournalState`, `brightnessRecoveryPending`, and `brightnessError` expose unresolved recovery. If a pre-0.2 journal remains, restore brightness manually with GNOME controls and inspect the record before removing it; the agent will not apply an unverified old record.

GNOME 49–50's `org.gnome.Shell.Brightness` service has `SetDimming` and `HasBrightnessControl`, but no public per-panel read/write brightness property. The agent leaves manual lid dimming unavailable there rather than guessing an output or overwriting GNOME's dimming state. Main sleep prevention remains usable. Dimming must be tested separately from lid stay-awake, since a suspended machine cannot run the agent. [GNOME 49 Shell implementation](https://github.com/GNOME/gnome-shell/blob/gnome-49/js/ui/shellDBus.js), [GNOME 50 Shell implementation](https://github.com/GNOME/gnome-shell/blob/gnome-50/js/ui/shellDBus.js)

For your `[deep]` machine, record `cat /sys/power/mem_sleep` before tests. `[deep]` selects suspend-to-RAM after logind accepts a sleep request; it does not change the inhibitor call. `PrepareForSleep(false)` can follow either a completed or failed sleep attempt, so the agent calls an interval a real suspend only when `CLOCK_BOOTTIME - CLOCK_MONOTONIC` grows by at least 0.5 seconds. An overdue countdown is canceled with a distinct proven-suspend, failed-preparation, or uncertain-wake `timerOutcome`, and never requests a second suspend. An unexpired countdown keeps its original `CLOCK_BOOTTIME` deadline only while its lid/prevention conditions still hold. Missing signals are reconciled against logind's `PreparingForSleep` property and the two-clock gap, with a bounded uncertain state. Test these cases and brightness restoration after a deliberate suspend. Do not change the kernel sleep setting for this prototype.

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

Stopping the service closes its inhibitor FDs and asks GNOME to release its cookie. If GNOME's reply is ambiguous during shutdown, the agent closes its session-bus connection, which is the ownership boundary for that cookie. The normal service auto-restarts on failure, so a crash or bounded ordinary-off recovery also releases client-owned inhibitors and then runs brightness recovery. If an automatic restart does not happen, `systemctl --user restart sleep-disabler-gnome.service` asks the agent to process its recovery record. To disable future starts, run `systemctl --user disable sleep-disabler-gnome.service`.

## Implementation notes

- System suspend: `org.freedesktop.login1.Manager.Inhibit("sleep", …, "block")` held as a Unix FD. GNOME idle/display: `org.gnome.SessionManager.Inhibit` with flags `4|8`. Both are required before `enabled` becomes true. GNOME cookies have explicit absent, held, and release-pending states bound to the SessionManager unique owner that issued them; aggregate `IsInhibited` is never treated as ownership proof. [login1](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.login1.html), [GNOME SessionManager](https://gnome.pages.gitlab.gnome.org/gnome-session/re04.html)
- Battery/lid: UPower system bus properties and change signals, with a 15-second reconciliation read. [UPower](https://upower.freedesktop.org/docs/UPower/)
- Timer: `CLOCK_BOOTTIME` includes time suspended. Running timers are process-scoped; a service restart cancels them. Preferences survive restart. The agent consumes timer and failsafe triggers before requesting suspend, including when login1 rejects the request. [Linux clock_gettime](https://man7.org/linux/man-pages/man2/clock_gettime.2.html)
- Top bar: the extension adds a native panel button, calls the app's session D-Bus API, and shows disconnected state if the agent stops. The agent remains independent of GNOME Shell. [GNOME extension guide](https://gjs.guide/extensions/overview/anatomy.html)

GNOME 46–50 are listed in extension metadata only as an experimental prototype test range. No version has passed a live Shell lifecycle or attended hardware test, so none is claimed supported. The matrix is: 46–48 main inhibition, lid lock, timer, and brightness **unverified**; 49–50 main inhibition, lid lock, and timer **unverified**, manual dimming **unavailable**. Debian, Ubuntu, Mint, and Kali GNOME Wayland installs and `[deep]` hardware are **unverified**. If the agent fails to start, the installer exits with an error; if Shell has not discovered or activated the extension, it exits `2` and leaves the CLI available. A failed extension discovery may require a logout/login before enabling the copied extension. See [task.md](task.md) for the outstanding live gates.
