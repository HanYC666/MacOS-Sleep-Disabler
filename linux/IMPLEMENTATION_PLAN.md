# Linux functional port: research and implementation plan

Research snapshot: 4 October 2026. This is a plan for the behavior of the current macOS app, not a UI design. The target is a per-user desktop application for Debian-family systems. The exact OS and desktop API ledger is in [RESEARCH.md](RESEARCH.md); evidence status and unfinished validation are tracked in [research_checklist.md](research_checklist.md). Package names, desktop behavior, and release defaults must be checked again when implementation begins.

## 1. What must be reproduced

The [project README](../README.md) and `Sleep_DisablerApp.swift` define these behaviors:

1. A switch that prevents automatic system sleep and automatic display sleep, with an accurate on/off state and restoration when turned off or when the app exits.
2. A battery failsafe: while the switch is on, if an internal battery is discharging and its percentage falls **below** a chosen threshold, stop inhibiting and request immediate sleep; notify the user.
3. A laptop lid feature: with the switch on, allow the machine to remain awake when the lid closes, and dim the built-in panel to its minimum or off state; restore the prior brightness when the lid opens.
4. A sleep countdown of days, hours, and minutes, with optional conditions “lid must remain closed” and “sleep prevention must remain active.” The countdown can be canceled, and its last duration/options survive app restart. At expiry it requests sleep.
5. Refresh the observed state, start at login if selected, and expose actions from a desktop integration point. Those are implementation requirements even though the UI layout is outside scope.

The macOS implementation edits persistent `pmset` settings. Linux has separate owners for system sleep, desktop idle behavior, lid actions, and brightness. The Linux port should reproduce the **observable behavior**, primarily with process-scoped inhibitors. It should not claim that it has changed system policy, or that every sleep path is blocked, unless that has been verified in the active session.

### Behavioral vocabulary

| Behavior | Owner commonly involved | Required result |
|---|---|---|
| Idle system suspend | Desktop power manager and/or `systemd-logind` | No automatic suspend while enabled |
| Idle display blank/dim/off | Desktop power manager, compositor, X11 DPMS | Display remains on while enabled and lid open |
| Idle screen lock | Desktop session/locker | Measure and document separately; do not promise bypass of a security policy |
| Lid close suspend | Desktop power manager or `logind` | Machine stays awake when this feature is enabled |
| Explicit Sleep action | User's desktop or another app | A `sleep` block can also block manual Sleep; document whether the desktop offers an override, and provide a clear way to turn prevention off |
| Critical battery/thermal shutdown | Firmware, kernel, desktop safety policy | Never promise to override |

The main switch and lid feature need separate capability indicators. For example, “idle sleep prevented” must not be shown as “lid close supported” when a desktop ignores the inhibitor.

## 2. Research findings that drive the design

### 2.1 The shared Linux layer

- `systemd-logind` exposes `Inhibit(what, who, why, mode)` and returns a file descriptor whose lifetime owns the inhibitor. Relevant `what` values are `sleep`, `idle`, and `handle-lid-switch`; `block` prevents an action, while `delay` only postpones it. A `sleep` block does not itself guarantee that a desktop will keep the display lit. [systemd Inhibitor Locks](https://www.freedesktop.org/software/systemd/man/latest/systemd-inhibit.html), [login1 D-Bus API](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.login1.html)
- `logind`'s lid handling has its own rules. `LidSwitchIgnoreInhibited=yes` is the documented default for high-level inhibitors, while a low-level `handle-lid-switch` inhibitor delegates lid handling to the desktop that holds it. Thus a plain `sleep` inhibitor is insufficient evidence of lid support. [logind.conf](https://www.freedesktop.org/software/systemd/man/latest/logind.conf.html)
- The XDG Inhibit portal has `Suspend` (flag 4) and `Idle` (flag 8) requests. The actual backend depends on the desktop and portal configuration. A successful D-Bus request is not proof that all power actions are inhibited; test effects, especially lid closure. Close the portal request when the mode ends. [Portal Inhibit API](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Inhibit.html), [portal configuration](https://flatpak.github.io/xdg-desktop-portal/docs/portals.conf.html)
- `org.freedesktop.ScreenSaver.Inhibit` is a cross-desktop idle inhibition API. Its specification explicitly excludes suspend inhibition, so pair it with a verified system-sleep mechanism. Its effect on lock and dim behavior depends on the session implementation. [Freedesktop Idle Inhibition Service](https://specifications.freedesktop.org/idle-inhibit/0.1/)
- Wayland's `zwp_idle_inhibit_manager_v1` binds an inhibitor to a **visible surface**; compositors may stop honoring it when the surface is hidden. A background/tray application must not rely on this as its only idle prevention mechanism. [Wayland idle-inhibit protocol](https://gitlab.freedesktop.org/wayland/wayland-protocols/-/blob/main/unstable/idle-inhibit/idle-inhibit-unstable-v1.xml)
- UPower exposes `OnBattery`, `LidIsClosed`, `LidIsPresent`, and battery device properties through D-Bus. It can provide signals instead of constant polling. The display device is an aggregate, so select the internal power source carefully and validate the percentage/charging state before a failsafe action. [UPower API](https://upower.freedesktop.org/docs/UPower.html), [UPower client](https://upower.freedesktop.org/docs/UpClient.html)
- The kernel backlight interface has multiple device candidates, a device-specific `max_brightness`, and platform-dependent effects for `brightness=0` or `bl_power`. It does not provide a universal, unprivileged way for a desktop app to turn off just the internal panel. [Linux backlight interface](https://docs.kernel.org/gpu/backlight.html)

**Suspend state on laptops such as the user's:** read `/sys/power/state` and `/sys/power/mem_sleep` at runtime. A bracketed `[deep]` means `mem` enters suspend-to-RAM (typically ACPI S3); `[s2idle]` means suspend-to-idle. The login1 inhibitor is checked before either kernel state, so the same high-level app design applies. Entry, wake, lid, battery drain, and panel restoration must be tested on each mode because firmware and wake sources differ. Do not write a new `mem_sleep` selection or claim that an inhibitor fixes S3 firmware behavior. [Kernel sleep states](https://docs.kernel.org/admin-guide/pm/sleep-states.html), [kernel suspend flow](https://docs.kernel.org/power/suspend-flows.html).

**Services needed:** `systemd-logind.service` on the system bus is required for the baseline `Inhibit` and `Suspend(false)` path. `upower.service` on the system bus is required for the specified failsafe/lid data path. `xdg-desktop-portal.service` plus one functioning selected backend is required only if the portal adapter is chosen. GNOME/Cinnamon session manager, PowerDevil, or Xfce power manager must be running for their respective native adapter. A `systemd --user` unit is **not** required for login1 inhibition; it is only a possible launch/supervision choice. GNOME Shell and an enabled compatible extension are required for a native GNOME top-bar control. See [RESEARCH.md §7](RESEARCH.md#7-distribution-dependencies-and-diagnostics).

### 2.2 Desktop-specific ownership

| Desktop | Working hypothesis and required check |
|---|---|
| GNOME | `org.gnome.SessionManager.Inhibit` supports suspend (4) and idle (8). Use it as the preferred GNOME adapter if portal behavior is incomplete, and verify idle dim, idle suspend, and lid action independently. [GNOME SessionManager API](https://gnome.pages.gitlab.gnome.org/gnome-session/re04.html) |
| Cinnamon | `cinnamon-session` exposes the GNOME SessionManager bus name/API, while `csd-power` handles idle and can take its **own** low-level `handle-lid-switch` lock. Its source checks session inhibitor flags for idle; lid behavior needs physical testing. Some current Mint reports describe lid-related interactions with logind inhibitors. Do not assume that another `handle-lid-switch` lock changes what Cinnamon does. [Cinnamon power source](https://github.com/linuxmint/cinnamon-settings-daemon/blob/master/plugins/power/csd-power-manager.c), [Cinnamon session source](https://github.com/linuxmint/cinnamon-session/blob/master/cinnamon-session/csm-manager.c), [lid issue](https://github.com/linuxmint/cinnamon/issues/13219) |
| Plasma | PowerDevil has separate controls for dimming/display-off, suspend, and lid actions, and its policy agent coordinates inhibitors and power events. Verify the standard portal/ScreenSaver calls in both Wayland and X11 sessions; inspect PowerDevil policy only where observed behavior differs. [PowerDevil handbook](https://docs.kde.org/stable_kf6/en/powerdevil/kcontrol/powerdevil/index.html), [policy agent source](https://github.com/KDE/powerdevil/blob/master/daemon/powerdevilpolicyagent.cpp) |
| Xfce | `xfce4-power-manager` coordinates system power, DPMS on X11, and lid handling. Check which inhibitor APIs are actually present in each supported Xfce release; use presentation mode as a diagnostic comparator, not a setting to silently alter. Xfce Wayland support needs separate validation and should not be inferred from X11 results. [Xfce power manager](https://docs.xfce.org/xfce/xfce4-power-manager/start), [Xfce lid/DPMS report](https://gitlab.xfce.org/xfce/xfce4-power-manager/-/issues/265) |

### 2.3 Distribution/session targets

These are test targets, not assumptions used to choose code paths. Probe the running session via `XDG_SESSION_TYPE`, `XDG_CURRENT_DESKTOP`, `loginctl`, and available D-Bus services. `DISPLAY` may exist under XWayland in a Wayland session and does **not** mean that the session is X11.

| Distribution/release | Primary sessions to test | Notes |
|---|---|---|
| Debian 13 | GNOME Wayland; Plasma Wayland and X11; Xfce X11 | Debian lists GNOME 48, Plasma 6.3, and Xfce 4.20. Test optional desktops on the same base to separate distro effects from desktop effects. [Debian 13 release notes](https://www.debian.org/releases/trixie/release-notes/whats-new.html) |
| Ubuntu 24.04 LTS | GNOME Wayland and GNOME on Xorg | Older LTS baseline and X11 coverage. Probe actual session choice; do not assume every installation offers both. |
| Ubuntu 26.04 LTS | GNOME Wayland | Ubuntu documents a Wayland-only GNOME session; XWayland compatibility does not provide X11-wide power control. [Ubuntu 26.04 notes](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/) |
| Linux Mint 22.3 | Cinnamon X11; Cinnamon Wayland experimental; Xfce X11 | Cinnamon is the main Mint target. Gate Wayland support by real testing, because Mint describes its Wayland session as experimental in this release. [Mint 22.3 notes](https://www.linuxmint.com/rel_zena_whatsnew.php) |
| Kali rolling, current image | Xfce X11; GNOME Wayland; Plasma Wayland and optional X11 | Kali documents Xfce/X11, GNOME/Wayland, and Plasma's default Wayland path. In live/VM installs, battery and lid tests may be unavailable. [Kali Wayland guide](https://www.kali.org/docs/general-use/wayland/), [Kali FAQ](https://www.kali.org/faq/) |

Add Kubuntu/Xubuntu and additional Debian derivatives to the regression set after their matching Plasma/Xfce baselines work. “All mainstream distros” is a package and support policy, not a distinct power API for each distro: support any systemd + UPower desktop exposing the validated mechanisms, and state a capability shortfall when it does not.

## 3. Architecture

### 3.1 Runtime probes and capability report

At process start and after session-bus reconnection:

1. Read OS ID/version from `/etc/os-release` for diagnostics and packaging only.
2. Determine active user session and type using logind session properties; compare `XDG_SESSION_TYPE` and `XDG_CURRENT_DESKTOP`. Handle nested sessions, remote sessions, and missing variables explicitly.
3. Probe system D-Bus for login1 and UPower. Probe session D-Bus for portal, GNOME/Cinnamon SessionManager, Freedesktop ScreenSaver, and any desktop-specific service selected for validation.
4. Query portal backend selection where available; record owner names/versions for diagnostics. Avoid selecting a backend only from a distro label.
5. Enumerate batteries, lid presence, and brightness controls. Distinguish internal panel from external monitors. Do not enable lid dimming on devices without a reported lid and a tested built-in brightness path.
6. Report independent capabilities: `systemIdleSuspend`, `displayIdle`, `lidStayAwake`, `internalPanelDim`, `batteryFailsafe`, `scheduledSuspend`. Each can be `verified`, `availableButUnverified`, `unsupported`, or `temporarilyUnavailable`, with an explanatory reason.

An API returning success establishes `availableButUnverified` until behavior tests establish `verified` for a desktop/session/version family. Production code can use runtime API probes, but release claims require the matrix tests in §7.

### 3.2 Inhibitor manager

Build one owner for every acquired inhibition. It tracks D-Bus request paths/cookies and logind file descriptors; acquisition is idempotent and release is ordered. On session bus loss or owner change, mark the relevant capability failed and retry with bounded backoff. On process termination, close only this app's requests; process exit must naturally release all process-owned locks.

Proposed selection order, subject to observed behavior:

1. For **system suspend**, take a `login1` `sleep` block for the active user session. Verify that normal idle sleep is prevented. Desktop-native suspend inhibition may be additionally required to prevent a desktop from attempting a suspend and showing errors repeatedly.
2. For **desktop idle and display**, request the session's native inhibitor: GNOME/Cinnamon SessionManager flags `Idle` and `Suspend`, otherwise XDG portal `Idle` and `Suspend`, otherwise Freedesktop ScreenSaver `Inhibit` plus login1 `sleep`. Hold one coherent set per selected adapter; avoid duplicate inhibitor storms.
3. For **lid**, test whether the selected desktop respects those requests. If logind is handling the lid, a `handle-lid-switch` block can prevent logind's response. If GNOME, Cinnamon, Plasma, or Xfce already owns low-level lid handling, an app lock cannot replace their policy; use a documented desktop API if one exists, otherwise mark lid support unavailable until an opt-in setting strategy is implemented and tested.
4. Do not use `systemd-inhibit` as a persistent subprocess. The application should call the D-Bus API directly so lifetime and error handling are clear.

Take all required inhibitors before showing “On.” If one required path fails, release newly acquired locks and show partial capability or an error. Never keep claiming full sleep prevention after a bus disconnect. No `sudoers` rule or root daemon is needed for the normal inhibitor-based mode **if the active local session is permitted by the installed polkit policy**; login1 `Inhibit` checks authorization without prompting, so report denial as a capability failure. [systemd inhibitor design](https://github.com/systemd/systemd/blob/main/docs/INHIBITOR_LOCKS.md).

### 3.3 State model

Use explicit states: `off`, `enabling`, `on(capabilities)`, `degraded(reason)`, `disabling`, `sleepRequested`, `error`. Persist **preferences** (threshold, timer defaults, launch-at-login), not a stale “on” boolean. A fresh login should start off unless an explicit “resume prevention at login” preference is added and clearly presented.

- Enable: probe → acquire idle/suspend locks → verify acquisition → begin UPower monitoring → optionally arm lid mode → publish state.
- Disable or quit: cancel conditional timer if required → stop lid operation → restore brightness if still owned → release session locks → release login1 locks → publish off.
- Failsafe or timer expiry: cancel timer → restore panel state → release **this app's** inhibitors → call `login1.Suspend(false)` (or desktop-supported equivalent if necessary) → report rejection or authorization failure; do not automatically reacquire inhibition without a deliberate policy.
- External suspend/resume: handle login1 `PrepareForSleep`; refresh battery, lid, brightness, bus owners, and capability state after resume. Do not silently overwrite a user brightness change.

Use single-threaded event sequencing or a serialized state actor to avoid timer, battery, and lid events racing each other. Record diagnostic events without battery serial numbers, usernames, or other unnecessary personal data.

## 4. Feature design details

### 4.1 Battery failsafe

Subscribe to UPower property changes, and take a periodic fallback sample in case signals are missed. Trigger only when there is a present internal battery, it is discharging/on battery, the percentage is valid, and `percentage < configuredThreshold` while mode is active. A battery that first appears below the threshold after enable should trigger promptly. AC changes must be re-read before requesting sleep to avoid a stale event causing a surprise suspend.

If multiple batteries exist, use UPower's display device where it accurately represents the system and cross-check present physical batteries. Treat unknown percentage, missing UPower, a UPS, and a VM battery as separate cases. If battery data is unavailable, warn that the failsafe is unavailable; do not invent a value or trigger sleep. Rate-limit duplicate events. On threshold crossing, release all app-owned inhibitors first, request sleep, then show a notification if the session resumes or sleep fails. Hardware or desktop emergency battery policy may act earlier.

### 4.2 Lid-closed stay-awake and dimming

Treat lid prevention and dimming as separate features with separate checks. UPower's `LidIsPresent` and `LidIsClosed` provide primary state; reconcile on resume and after bus reconnect. Require a real laptop test: close the lid for longer than any desktop safety delay, with external displays connected and disconnected, on AC and battery.

Preferred dimming order:

1. Desktop-supported, per-user brightness API for the **internal** panel, if available and verified for that desktop/session.
2. Kernel backlight control only via an appropriately scoped mechanism if user access is supported; first identify the correct internal-panel device and test its scale. Do not recursively change every `/sys/class/backlight` node or grant broad write access.
3. If neither works, disable dimming capability while keeping any verified lid awake capability available.

Capture the exact prior brightness and device identity immediately before dimming. On open/disable/quit, restore only if the current value still equals the value this app wrote; if the user or desktop has changed it, preserve that newer value. Handle a panel disappearing/reappearing on dock/undock. Brightness zero may still leave a lit screen on some hardware; report “dimmed to minimum” unless testing confirms off. On Wayland, do not attempt an X11 `xrandr`/DPMS call through XWayland as a system-wide solution.

A process-owned brightness write can outlive a crash. Before enabling this feature, implement a small user-session watchdog or equivalent desktop-managed restore path: store the device, prior value, written value, and app bus owner; if that owner vanishes, restore only when the current value still equals the written value. Also reconcile this record at next login and after power-manager restart. A simple exit handler is insufficient for `SIGKILL` or a process crash.

If a desktop-owned lid action cannot be inhibited through supported APIs, there are two choices: expose “lid mode unavailable” for that combination, or build a separate **opt-in** desktop-policy adapter. Such an adapter would snapshot the exact AC/battery lid settings, write a do-nothing action, and restore only if values still match what the app wrote. It must cover daemon restarts, logout/crash recovery, concurrent settings edits, and the desktop's critical battery safety path. This is later work, not an assumed foundation for first release. For Cinnamon specifically, inspect `csd-power` lid safety timer and verify the closed-lid external-monitor case before claiming success. [Cinnamon power source](https://github.com/linuxmint/cinnamon-settings-daemon/blob/master/plugins/power/csd-power-manager.c), [recent lid report](https://github.com/linuxmint/cinnamon-settings-daemon/issues/466)

### 4.3 Countdown and deliberate sleep

Allow 0–365 days, 0–23 hours, and 0–59 minutes; reject total zero and overflow. Save the last chosen duration and options. The running countdown is process-scoped, matching the macOS app; do not promise it survives logout/reboot. Record an absolute deadline and recalculate remaining time from a clock that handles suspend/resume as intended, rather than subtracting one second per callback. Define wall-clock changes and DST behavior in tests; using a monotonic/boottime deadline for an elapsed-duration timer avoids manual clock changes affecting it.

At start, fail if the “lid closed only” option is selected while lid state is open/unknown; fail if “sleep disabled only” is selected while prevention is off/degraded. Cancel if the lid opens or if prevention is turned off respectively. At expiry, release own inhibitors, then call `login1.Suspend(false)`. Check `CanSuspend`, D-Bus error, and other programs' inhibitors. Never use an interactive authorization bypass or force flag without explicit user action. On failure, show a clear error and leave prevention off; a failed sleep request must not silently re-arm an expired timer.

### 4.4 Startup and desktop presence

Use the XDG autostart desktop entry in the user's config directory for launch at login. Keep only one app instance per user session, with a D-Bus name or equivalent. [XDG Autostart Specification](https://specifications.freedesktop.org/autostart/0.5/)

#### GNOME top bar: what is actually possible

The GNOME top bar is owned by **GNOME Shell**, not by ordinary app windows. Stock GNOME stopped displaying application status icons by default. Therefore a Linux app cannot simply put a persistent item beside GNOME's system icons using GTK, Qt, an X11 tray call, or a Wayland protocol. GNOME's own extension template offers an “Indicator” that adds a top-bar icon, and its example uses `Main.panel.addToStatusArea(...)`; this code runs **inside a GNOME Shell extension**. This applies to GNOME on both Wayland and X11. [GNOME status-icon migration](https://wiki.gnome.org/Initiatives%282f%29StatusIconMigration/FAQ.html), [GNOME extension indicator guide](https://gjs.guide/extensions/development/creating.html)

There are two viable ways for Sleep Disabler to appear there:

| Route | How it works | Fit for this project | Constraint |
|---|---|---|---|
| Native GNOME Shell extension | A small GJS extension creates a panel button/menu and talks to the separate Sleep Disabler process over the session D-Bus | Best way to guarantee a dedicated GNOME top-bar control **when the extension is enabled**; can show on/off/degraded state and timer text | Separate installation/enablement, GNOME Shell version maintenance, extension can be disabled or fail independently |
| StatusNotifierItem/AppIndicator | The app publishes a standard status item; an already-enabled host extension renders it in GNOME's panel | Reuses one indicator implementation across Plasma/Cinnamon/Xfce and Ubuntu configurations | Stock GNOME has no built-in host; indicator is invisible if the host extension is absent or disabled; menu/label behavior varies |

The [StatusNotifierItem specification](https://specifications.freedesktop.org/status-notifier-item/latest/status-notifier-item.html) defines the app-side session-bus item/watcher model using `org.freedesktop.*`; [Plasma's implementation](https://github.com/KDE/plasma-workspace/blob/master/xembed-sni-proxy/sniproxy.cpp) uses `org.kde.*`, which common GNOME AppIndicator hosts also use. Probe both and speak the active host's interface. Ubuntu's [AppIndicator and KStatusNotifierItem extension](https://extensions.gnome.org/extension/615/appindicator-support/) supplies a GNOME host and documents icon/menu integration. Debian 13 has a [`gnome-shell-extension-appindicator` package](https://packages.debian.org/trixie/gnome/gnome-shell-extension-appindicator); Ubuntu 24.04 has a package, while Ubuntu 26.04 lists it as provided by `gnome-shell-ubuntu-extensions` in the [Ubuntu package index](https://packages.ubuntu.com/search?keywords=gnome-shell-extension-appindicator). **Package presence is not proof that the extension is enabled in the current session.** Check the watcher's `IsStatusNotifierHostRegistered` property and verify that the icon actually appears. An absent host means the AppIndicator route cannot provide a top-bar icon. [GNOME status-icon guidance](https://wiki.gnome.org/Initiatives/StatusIconMigration/Guidelines)

**Recommended design:** keep the power-control process independent of GNOME Shell. Implement a StatusNotifierItem for desktops with an active host. Offer a small optional GNOME Shell extension for a predictable GNOME top-bar button, controlled by the user. The extension should only present state/actions and call the proposed session D-Bus API (`GetState`, `SetPrevention`, `StartTimer`, `CancelTimer`, `OpenWindow`) specified with signatures in [RESEARCH.md §6](RESEARCH.md#6-brightness-countdown-startup-and-controls); the power inhibitors and timer must live in the application, not in GNOME Shell. Authenticate that calls come from the active local user session and avoid any unrestricted privileged method. Subscribe to state changes so the button updates without polling every second except while a countdown is visible. If the service disappears, show unavailable and disable actions until it returns. Destroy the panel actor, disconnect signals, and cancel pending calls when the extension is disabled.

Package the extension as an optional GNOME-specific component in its own UUID directory; GNOME supports user and system extension locations. Declare and test the GNOME Shell versions actually supported, rather than claiming all future versions. Debian 13 GNOME 48, Ubuntu 24.04 GNOME 46, Ubuntu 26.04 GNOME 50, and Kali's current GNOME session need separate checks. GNOME extension review guidance forbids claiming future versions without testing. Installation should offer a clear way to enable the extension; do not silently change a user's extension settings. [Ubuntu 26.04 GNOME 50 notes](https://documentation.ubuntu.com/release-notes/26.04/changes-since-previous-interim/), [GNOME extension installation](https://help.gnome.org/system-admin-guide/extensions.html), [extension metadata/version guidance](https://gjs.guide/extensions/overview/anatomy.html), [extension review guidance](https://gjs.guide/extensions/review-guidelines/review-guidelines.html)

The top-bar item must never be the only way to disable sleep prevention. Always provide a normal application launcher/window, and ensure a second launch raises that window. If no indicator host or GNOME extension is active, background operation remains usable and its state is visible from the app window. Test that notifications are supplemental, not the sole control surface.

## 5. Implementation structure and milestones

Suggested implementation is a native Linux process using GLib/GIO D-Bus + UPower bindings, or another language with reliable Unix FD passing and session/system bus support. Choose the language after a small API spike; UI toolkit choice should not force the core power logic. Keep adapters behind narrow interfaces:

```text
LinuxApp
 ├─ SessionProbe           # session type, desktop, service owners, capabilities
 ├─ InhibitorManager       # login1 fd, portal request, session/ScreenSaver cookie
 │   ├─ GnomeCinnamonAdapter
 │   ├─ PortalAdapter
 │   ├─ ScreenSaverAdapter
 │   └─ LogindAdapter
 ├─ PowerMonitor           # UPower batteries, AC, lid
 ├─ BrightnessController   # internal panel only, conflict-safe restore/watchdog
 ├─ SleepCoordinator       # failsafe, timer, deliberate Suspend call
 ├─ PreferenceStore        # threshold, countdown defaults, startup preference
 └─ DesktopFrontend        # UI and desktop integration, separate from core
```

| Milestone | Deliverables | Exit gate |
|---|---|---|
| M0: API spike | Small CLI to probe session and acquire/release each candidate inhibitor; log D-Bus owners and errors | Real GNOME, Cinnamon, Plasma, Xfce sessions mapped; no assumptions based solely on docs |
| M1: Core switch | Inhibitor manager, state machine, clean quit/bus loss recovery | Idle suspend/display tests pass on the first target session; manual suspend behavior documented |
| M2: Desktop coverage | Per-desktop adapter selection and fallback, live capability report | All tier-1 session combinations in §7 pass core switch tests or show an accurate unsupported state |
| M3: Battery and timer | UPower monitor, threshold action, countdown conditions, sleep request | Deterministic unit tests plus battery/laptop integration tests; no sleep with unknown battery state |
| M4: Lid and brightness | Lid routing tests, internal-panel adapter, safe restore | Closed-lid real-hardware tests pass; unsupported combinations explicitly reported |
| M5: Desktop integration | StatusNotifierItem, optional GNOME Shell extension, app launcher/window, autostart | GNOME top-bar item works when enabled; missing/disabled host leaves full control available in the app window |
| M6: Packaging and release | `.deb` package, diagnostics, docs, CI | Install/upgrade/uninstall and test matrix complete on Debian-family targets |

Do not bundle privileged file edits into M1. If a specific desktop cannot meet a required behavior with user-level APIs, document the gap, implement the opt-in adapter as its own milestone, and rerun all restoration tests.

## 6. Development probes and diagnostics

Run these **read-only** probes on representative machines before writing desktop adapters. Examples are investigative, not instructions for users to modify their system:

```sh
cat /etc/os-release
loginctl show-session "$XDG_SESSION_ID" -p Type -p Desktop -p Active -p Remote
busctl --system list
busctl --user list
busctl --system introspect org.freedesktop.login1 /org/freedesktop/login1
busctl --system introspect org.freedesktop.UPower /org/freedesktop/UPower
systemd-inhibit --list
upower -e
```

Record `XDG_SESSION_TYPE` and `XDG_CURRENT_DESKTOP` separately, plus portal backend, session manager, PowerDevil/`csd-power`/`xfce4-power-manager` presence, lid owner, and available backlight device names. Use a temporary, short timeout for experimental inhibitors. Observe `journalctl` and `loginctl` for actual sleep transitions. Do not close a real laptop lid during unattended CI; physical tests need a person and a recovery method.

For each desktop/session, answer empirically:

1. Which API stops idle system suspend? Which stops dim, blank, DPMS, and screensaver activation?
2. Does manual Sleep still work? Does a blocked manual Sleep surface an error?
3. Which component handles lid close? Does the app's inhibitor override it on AC and battery, docked and undocked?
4. Can the internal backlight be dimmed and restored as an ordinary user? Does it also affect external outputs?
5. What happens when D-Bus, the desktop power daemon, or the app restarts?

## 7. Verification matrix and acceptance criteria

### 7.1 Session matrix

Tier 1 is needed before a broad Debian-family support claim:

| Distro | Desktop | Display server | Main tests |
|---|---|---|---|
| Debian 13 | GNOME | Wayland | All core and laptop tests |
| Debian 13 | Plasma | Wayland, X11 | Core; lid and display behavior on both |
| Debian 13 | Xfce | X11 | Core; X11 DPMS and lid |
| Ubuntu 24.04 LTS | GNOME | Wayland, Xorg | Core and regression |
| Ubuntu 26.04 LTS | GNOME | Wayland | Core and regression |
| Mint 22.3 | Cinnamon | X11 | All core and laptop tests; 30+ second closed-lid dwell |
| Mint 22.3 | Xfce | X11 | Core and regression |
| Kali current | Xfce | X11 | Core, live/installed distinction |
| Kali current | GNOME | Wayland | Core and regression |
| Kali current | Plasma | Wayland, X11 if offered | Core and regression |

Tier 2: Mint Cinnamon Wayland experimental, Xfce Wayland builds, Kubuntu/Xubuntu, Debian derivatives, NVIDIA proprietary-driver laptops, hybrid GPUs, and remote sessions. A failure on an experimental session should be disclosed by capability status; it must not undermine the tested X11/Wayland combinations.

### 7.2 Scenarios per supported session

- Idle: set the desktop's dim/display-off/suspend timeouts to short known values. Compare normal behavior with app off, then enabled. Verify no automatic system suspend or display-off for at least twice the configured timeout; check lock behavior separately.
- Inhibitor lifecycle: enable/disable repeatedly; verify exactly one app-owned lock set and none after disable, quit, crash, or logout. Restart session bus/power daemon where safe; state must degrade or recover truthfully.
- Lid: close for longer than desktop delay on AC and battery, with and without external monitor, then reopen. Check power state, built-in panel, external monitor, prior brightness restoration, and manual brightness changes while closed.
- Battery: test on a real laptop at a safe threshold above current battery, then below it; verify strict `<` comparison, AC gating, UPower disconnect, multi-battery/unknown cases, and no repeated sleep loop after resume. Avoid deep battery discharge to create a test case.
- Suspend-state coverage: record `/sys/power/mem_sleep` with `[deep]` selected and repeat core idle, explicit timer sleep, closed-lid dwell, brightness restoration, and resume. Repeat with `[s2idle]` where the firmware exposes it. Verify that the app releases its own block before a deliberate suspend and that `PrepareForSleep(false)` is observed after resume. An S3 wake failure is a hardware/firmware result, not proof the D-Bus inhibitor was absent.
- Countdown: zero/maximum duration, canceled timer, lid opening, prevention turned off, manual clock change, suspend/resume, other app inhibitor, authorization failure, and session logout.
- Security and policy: confirm user lock/screen-lock expectations, explicit desktop Sleep, critical battery handling, and no root privilege for normal use.
- Packaging: clean install, update, uninstall, startup toggle, duplicate launch, missing portal/UPower, missing panel integration, and service disabled.
- GNOME panel: test stock GNOME with no indicator extension, with AppIndicator enabled, and with the dedicated extension enabled; verify button/menu state and actions on Wayland and X11 where offered. Disable/re-enable the extension and kill/restart the app while the menu is open; no stale button, frozen Shell, duplicate item, or inaccessible off switch. Repeat after GNOME Shell upgrade and in Ubuntu's customized session.

### 7.3 Release gates

1. On every Tier-1 session, the switch must prevent **both** idle system suspend and idle display-off, or the app must state exactly which capability is unavailable before enabling. A release may support a session partially, but marketing must not claim full parity for it.
2. Battery failsafe and countdown must release all app-owned inhibitors before a sleep request and clearly report failure. No failsafe action may occur on AC or unknown battery data.
3. Lid mode may be labeled supported only after physical lid-close tests on that desktop/session. Dimming may be labeled supported only after internal-panel-only and conflict-safe restore tests.
4. After disable, quit, or crash, no changed desktop setting, stale inhibitor, or stuck brightness value may remain. If an opt-in persistent setting adapter is added, test crash recovery and concurrent user changes separately.
5. A package must install and run without administrator authorization for the normal feature set. If a capability later needs privilege, constrain it to that operation and give the user a clear opt-in path.
6. GNOME top-bar support must be described as conditional on an enabled compatible Shell extension. The application must remain controllable from its launcher/window when extensions are unavailable.

## 8. Risks and open decisions

| Risk | Consequence | Mitigation/decision gate |
|---|---|---|
| Desktop-owned lid action ignores ordinary inhibitors | Lid closes and suspends despite “On” | Treat lid as separately verified; spike per desktop; opt-in policy adapter only if safe |
| Portal backend accepts a request but does not enforce it | False success UI | Behavioral tests and capability state; fallback to native session API |
| Wayland background process cannot use surface idle inhibitor | Display still blanks | Use session/portal inhibition; no invisible-surface trick |
| X11 screensaver, compositor, and DPMS disagree | Display or lock behavior varies | Test each behavior; use desktop integration before global `xset` changes |
| Brightness API differs by hardware or requires permission | Glowing closed panel or failed restore | Internal-panel discovery, per-device tests, disable unsupported dimming |
| Other app/system inhibitor prevents scheduled sleep | Timer expires but system stays awake | Inspect/report error; never force past another app's inhibitor silently |
| System policy forbids inhibitors or suspend | Capability unavailable | Surface policy error; no blanket `sudo` workaround |
| Distro/desktop updates alter policy | Regression after upgrade | Runtime probes and versioned regression matrix |

**Decisions after M0:** choose implementation language/toolkit; settle the exact GNOME/Cinnamon/Plasma/Xfce adapter selection from observed results; decide whether unsupported lid combinations justify a separately maintained opt-in settings adapter; decide whether the product promises “display stays lit” or “display remains available but security lock may still activate” for each desktop. The unresolved checklist entries are explicit research gates, not permission to silently omit those behaviors.

The feasibility judgement is: core idle prevention, battery monitoring, and a countdown are practical on the named distros. Exact lid-closed and internal-panel behavior across every desktop/display-server/hardware combination is the expensive part. Full parity should be claimed **per tested capability and session**, not by a single distro-wide checkbox.
