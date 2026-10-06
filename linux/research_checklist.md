# Linux port research checklist

Started: 4 October 2026. This checklist covers the complete functional Linux port, including the user's `deep` suspend-to-RAM laptop. Mark an item `[x]` when primary evidence establishes a documented behavior or the research resolves a design question; this does **not** mean Linux code has been built or tested. Mark `[~]` when documentation establishes an API but desktop, release, or hardware behavior remains to verify. Keep `[ ]` for unresolved research. A source link and result belong beside each checked item. Do not treat a successful API call as proof of an end-to-end feature.

## A. Current app contract

- [x] A1. [App state](../Sleep%20Disabler/Sleep_DisablerApp.swift) and [power services](../Sleep%20Disabler/SystemServices.swift): persistent `pmset` timer changes, restoration, and status refresh recorded in [research §1](RESEARCH.md#1-exact-behavior-being-ported).
- [x] A2. [App state](../Sleep%20Disabler/Sleep_DisablerApp.swift): strict `<` threshold, internal battery/AC gating, release then sleep, notification recorded in [research §1](RESEARCH.md#1-exact-behavior-being-ported).
- [x] A3. [Feature services](../Sleep%20Disabler/FeatureServices.swift) and [app state](../Sleep%20Disabler/Sleep_DisablerApp.swift): 2-second fallback poll, main-display zero/restore; crash restoration is absent, so Linux design adds recovery.
- [x] A4. [App state](../Sleep%20Disabler/Sleep_DisablerApp.swift): 365d/23h/59m bounds, saved options, cancellation, and expiry sequence in [research §1](RESEARCH.md#1-exact-behavior-being-ported).
- [x] A5. Distinct effect/owner table in [research §4](RESEARCH.md#4-session-inhibition-endpoints-and-boundaries); critical battery and firmware actions cannot be promised away.

## B. Kernel and system suspend path

- [x] B1. [login1 API](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.login1.html) and [kernel PM](https://docs.kernel.org/admin-guide/pm/sleep-states.html): system bus endpoint/signatures and kernel handoff in [research §2](RESEARCH.md#2-kernel-systemd-and-deep).
- [x] B2. [Kernel sleep states](https://docs.kernel.org/admin-guide/pm/sleep-states.html): `deep` is suspend-to-RAM/S3, `s2idle` differs at kernel/firmware level; logind inhibition precedes either, as mapped in [research §2](RESEARCH.md#2-kernel-systemd-and-deep).
- [x] B3. [Kernel PM documentation](https://docs.kernel.org/power/basic-pm-debugging.html): read `state`, `mem_sleep`, optional debugfs `suspend_stats`; no sysfs writes in diagnostic probes.
- [x] B4. [login1 API](https://github.com/systemd/systemd/blob/main/man/org.freedesktop.login1.xml): method signatures, polkit outcomes, and version-gated flags in [research §2](RESEARCH.md#2-kernel-systemd-and-deep).
- [x] B5. [systemd inhibitor design](https://github.com/systemd/systemd/blob/main/docs/INHIBITOR_LOCKS.md): `PrepareForSleep(b)` is observational without a delay lock.
- [x] B6. [systemd inhibitor design](https://github.com/systemd/systemd/blob/main/docs/INHIBITOR_LOCKS.md): `block`, `delay`, `block-weak`, FD release, and low-level lid distinction.
- [x] B7. [logind.conf](https://www.freedesktop.org/software/systemd/man/latest/logind.conf.html): lid settings, ignore-inhibited, docked behavior in [research §2](RESEARCH.md#2-kernel-systemd-and-deep).
- [x] B8. [login1 API](https://github.com/systemd/systemd/blob/main/man/org.freedesktop.login1.xml): release own block and call `Suspend(false)`; [research §2](RESEARCH.md#2-kernel-systemd-and-deep) specifies failure handling.
- [~] B9. [Kernel PM](https://docs.kernel.org/power/suspend-flows.html) explains stages and state differences; actual firmware wake, critical power behavior, and resume require physical `deep`/`s2idle` tests.
- [x] B10. [login1 API](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.login1.html): logind system service versus optional user manager and read-only probes in [research §7](RESEARCH.md#7-distribution-dependencies-and-diagnostics).

## C. Power and lid input

- [x] C1. [UPower root API](https://upower.freedesktop.org/docs/UPower/): bus/path/interface, property changes, device signals, and methods in [research §3](RESEARCH.md#3-battery-and-lid-input).
- [x] C2. [UPower Device API](https://upower.freedesktop.org/docs/Device.html): physical battery selection and display aggregate caveat in [research §3](RESEARCH.md#3-battery-and-lid-input).
- [~] C3. [Debian systemd file list](https://packages.debian.org/trixie/amd64/systemd/filelist), [Debian UPower](https://packages.debian.org/trixie/upower), [Ubuntu Noble UPower](https://packages.ubuntu.com/noble/upower), and [Resolute UPower](https://packages.ubuntu.com/resolute/upower) establish package/service names; Mint/Kali installed inventory remains to probe. Hard vs recommended policy in [research §7](RESEARCH.md#7-distribution-dependencies-and-diagnostics).
- [x] C4. [UPower API](https://upower.freedesktop.org/docs/UPower/): mark failsafe unavailable on missing/stale/unknown data; owner-watch and conservative behavior defined in [research §3](RESEARCH.md#3-battery-and-lid-input).
- [~] C5. [UPower API](https://upower.freedesktop.org/docs/UPower/) provides lid property/change delivery; correctness on each machine needs a closed-lid observation.
- [x] C6. [Current app](../Sleep%20Disabler/Sleep_DisablerApp.swift) uses strict `<`; one-shot latch and final AC recheck specified in [research §3](RESEARCH.md#3-battery-and-lid-input).

## D. Generic desktop inhibition

- [x] D1. [systemd inhibitor design](https://github.com/systemd/systemd/blob/main/docs/INHIBITOR_LOCKS.md): names/modes/FD lifetime in [research §2](RESEARCH.md#2-kernel-systemd-and-deep).
- [x] D2. [Portal Inhibit](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Inhibit.html) and [Request](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Request.html): exact method, flags, Close, and Response caveat in [research §4](RESEARCH.md#4-session-inhibition-endpoints-and-boundaries).
- [~] D3. [Portal selection](https://flatpak.github.io/xdg-desktop-portal/docs/portals.conf.html) and [Debian package index](https://packages.debian.org/search?keywords=xdg-desktop-portal&suite=trixie) establish frontend/backends; selected backend on each target image must be probed.
- [x] D4. [Freedesktop idle inhibit spec](https://specifications.freedesktop.org/idle-inhibit/0.1/): session endpoint, `Inhibit`/`UnInhibit`, and idle-only scope in [research §4](RESEARCH.md#4-session-inhibition-endpoints-and-boundaries).
- [x] D5. [Wayland idle inhibit protocol](https://wayland.app/protocols/idle-inhibit-unstable-v1): tied to a visible surface; unsuitable as sole headless/tray control.
- [~] D6. [Xfce power manager source](https://github.com/xfce-mirror/xfce4-power-manager/blob/master/src/xfpm-power.c) confirms DE-controlled DPMS; global X11 fallback and restoration need target-session validation.
- [x] D7. [D-Bus specification](https://dbus.freedesktop.org/doc/dbus-specification.html) owner-change events and [logind FD lifetime](https://github.com/systemd/systemd/blob/main/docs/INHIBITOR_LOCKS.md) drive recovery in [research §4](RESEARCH.md#4-session-inhibition-endpoints-and-boundaries).
- [x] D8. Distinct effects documented by [systemd](https://github.com/systemd/systemd/blob/main/docs/INHIBITOR_LOCKS.md), [portal](https://flatpak.github.io/xdg-desktop-portal/docs/doc-org.freedesktop.portal.Inhibit.html), and [idle spec](https://specifications.freedesktop.org/idle-inhibit/0.1/); matrix in [research §4](RESEARCH.md#4-session-inhibition-endpoints-and-boundaries).

## E. Desktop-specific paths

- [~] E1. [GNOME SessionManager API](https://gnome.pages.gitlab.gnome.org/gnome-session/re04.html) gives exact call; Settings Daemon/Shell outcomes on Wayland and Xorg need target-session tests. See [research §5](RESEARCH.md#5-desktop-adapters).
- [~] E2. [Cinnamon power source](https://github.com/linuxmint/cinnamon-settings-daemon/blob/master/plugins/power/csd-power-manager.c) confirms session API use, lid lock, and 30-second safety timer; X11/experimental Wayland behavior remains hardware-dependent. See [research §5](RESEARCH.md#5-desktop-adapters).
- [~] E3. [PowerDevil header](https://github.com/KDE/powerdevil/blob/master/daemon/powerdevilpolicyagent.h) gives bit values 1 and 4 and method signatures; [source](https://github.com/KDE/powerdevil/blob/master/daemon/powerdevilpolicyagent.cpp) confirms own lid/key lock. User-policy acceptance and display-server outcomes still need tests.
- [~] E4. [Xfce XML](https://github.com/xfce-mirror/xfce4-power-manager/blob/master/src/org.freedesktop.PowerManagement.Inhibit.xml) and [source](https://github.com/xfce-mirror/xfce4-power-manager/blob/master/src/xfpm-inhibit.c) give exact session endpoint; [power source](https://github.com/xfce-mirror/xfce4-power-manager/blob/master/src/xfpm-power.c) confirms DPMS path. Wayland/target-version outcomes remain open.
- [~] E5. Adapter ordering and all physical test gates are defined in [research §5](RESEARCH.md#5-desktop-adapters); final selection awaits the actual matrix.

## F. Brightness and display

- [~] F1. [Cinnamon source](https://github.com/linuxmint/cinnamon-settings-daemon/blob/master/plugins/power/csd-power-manager.c) confirms `Screen.GetPercentage/SetPercentage`. [GNOME 48 source](https://github.com/GNOME/gnome-settings-daemon/blob/gnome-48/plugins/power/gsd-power-manager.c) confirms the `i` read/write `Brightness` property. [GNOME 49 Shell source](https://github.com/GNOME/gnome-shell/blob/gnome-49/js/ui/shellDBus.js) and [GNOME 50 Shell source](https://github.com/GNOME/gnome-shell/blob/gnome-50/js/ui/shellDBus.js) expose dimming but no public per-panel read/write brightness; the prototype safely disables that feature there. Live introspection, Plasma, and Xfce remain open.
- [~] F2. [Kernel backlight ABI](https://docs.kernel.org/gpu/backlight.html) defines nodes/scales and ambiguity; permissions/seat ownership and which node is internal need hardware inspection.
- [~] F3. [Wayland output-power protocol](https://wayland.app/protocols/wlr-output-power-management-unstable-v1) is compositor/protocol dependent; arbitrary-client access and internal-panel identity require compositor tests.
- [~] F4. [Xfce power-manager source](https://github.com/xfce-mirror/xfce4-power-manager/blob/master/src/xfpm-power.c) confirms its DPMS ownership; X11 restoration under each desktop remains a live test.
- [x] F5. Conflict-safe journal/watchdog design in [research §6](RESEARCH.md#6-brightness-countdown-startup-and-controls), based on [kernel backlight ABI](https://docs.kernel.org/gpu/backlight.html); implementation/test still required.
- [x] F6. Three separate physical assertions (continued process/network, panel luminance, external display) specified in [research §6](RESEARCH.md#6-brightness-countdown-startup-and-controls).

## G. Countdown, startup, and desktop controls

- [x] G1. [Linux clock_gettime manual](https://man7.org/linux/man-pages/man2/clock_gettime.2.html): use `CLOCK_BOOTTIME` to include suspend; restart/logout policy in [research §6](RESEARCH.md#6-brightness-countdown-startup-and-controls).
- [x] G2. [Current app timer](../Sleep%20Disabler/Sleep_DisablerApp.swift) plus serialized Linux transition sequence in [research §2](RESEARCH.md#2-kernel-systemd-and-deep) and [§6](RESEARCH.md#6-brightness-countdown-startup-and-controls).
- [x] G3. [XDG autostart spec](https://specifications.freedesktop.org/autostart-spec/latest/): paths, optional user service, single-instance policy in [research §6](RESEARCH.md#6-brightness-countdown-startup-and-controls).
- [~] G4. [GNOME GJS extension guide](https://gjs.guide/extensions/development/creating.html) confirms panel item; [Quick Settings guide](https://gjs.guide/extensions/topics/quick-settings.html) confirms toggle; [AppIndicator extension](https://extensions.gnome.org/extension/615/appindicator-support/) is optional host. Shell-version packaging/enablement must be tested.
- [~] G5. [SNI spec](https://specifications.freedesktop.org/status-notifier-item/latest/status-notifier-watcher.html) defines standard watcher/events, while [Plasma source](https://github.com/KDE/plasma-workspace/blob/master/xembed-sni-proxy/sniproxy.cpp) uses `org.kde.*`; [research §6](RESEARCH.md#6-brightness-countdown-startup-and-controls) requires probing both. Host presence needs per-desktop test.
- [x] G6. Proposed app-owned session D-Bus methods/signals, reconnect and window fallback in [research §6](RESEARCH.md#6-brightness-countdown-startup-and-controls); [GJS guide](https://gjs.guide/extensions/development/creating.html) establishes extension lifecycle.
- [~] G7. [Notification spec](https://specifications.freedesktop.org/notification-spec/latest/) defines `Notify` endpoint; target session availability/visibility during suspend remains a test.

## H. Distros, packages, security, and release

- [~] H1. [Debian 13 notes](https://www.debian.org/releases/trixie/release-notes/whats-new.html), [Ubuntu 26.04 notes](https://documentation.ubuntu.com/release-notes/26.04/summary-for-lts-users/), [Mint 22.3 notes](https://www.linuxmint.com/rel_zena_whatsnew.php), [Kali Wayland guide](https://www.kali.org/docs/general-use/wayland/) underpin the [plan matrix](IMPLEMENTATION_PLAN.md#23-distributionsession-targets); image-specific availability still needs confirmation.
- [~] H2. [Debian portal package index](https://packages.debian.org/search?keywords=xdg-desktop-portal&suite=trixie) and [Ubuntu package index](https://packages.ubuntu.com/noble/gnome/) establish names; Mint/Kali image inventory and chosen build stack remain open.
- [x] H3. [systemd inhibitor design](https://github.com/systemd/systemd/blob/main/docs/INHIBITOR_LOCKS.md) confirms noninteractive polkit checks; normal mode can be unprivileged when policy permits, not universally. See [research §2](RESEARCH.md#2-kernel-systemd-and-deep).
- [~] H4. Proposed package relationships and no-global-config rule in [research §7](RESEARCH.md#7-distribution-dependencies-and-diagnostics); exact dependencies depend on chosen implementation/runtime.
- [x] H5. Read-only commands and privacy limits listed in [research §7](RESEARCH.md#7-distribution-dependencies-and-diagnostics).
- [~] H6. Test matrix in [implementation plan §7](IMPLEMENTATION_PLAN.md#7-verification-matrix-and-acceptance-criteria) and [research §8](RESEARCH.md#8-open-proof-gates); executable harness and live runs remain open.
- [x] H7. Open uncertainties and explicit support gate in [research §8](RESEARCH.md#8-open-proof-gates); no hardware behavior promoted to verified.

## Research log

| Date | Area | Evidence/result | Remaining verification |
|---|---|---|---|
| 2026-10-04 | Checklist created | The items above define the research needed before claiming full support. | Research and testing pending. |
| 2026-10-04 | Repo/kernel/systemd/UPower/portal | Source-verified API mapping and macOS behavior recorded in [RESEARCH.md](RESEARCH.md). | Linux hardware tests, some DE API signatures, and package versions remain. |
| 2026-10-04 | Deep suspend | `deep` is S3 suspend-to-RAM; logind inhibition precedes kernel state selection. | Physical wake, lid, battery, brightness, and timer tests on `deep`. |
| 2026-10-05 | GNOME Wayland repair | Legacy brightness property and new Shell service verified in upstream source; agent journal tied to machine identity, resume guard, lid lock isolation, transactional preferences, UI state, and installer health check implemented. Eleven local state-machine tests plus syntax checks pass. | Release-matched live introspection, Shell lifecycle, Debian-family install, and attended hardware tests including `[deep]`; GNOME 49–50 manual dimming remains unavailable pending a safe per-panel API. |
| 2026-10-05 | GNOME Wayland plan 0.2 | Source changes add explicit brightness journal states/retry and a conservative single-panel identity rule; optional lid FD errors no longer unwind required inhibitors; timer wake fallback consults logind state; panel errors survive refresh; installer distinguishes an active agent from a pending extension. Local deterministic tests and static checks are recorded in `gnome-wayland/task.md`. Primary references: [login1 source](https://github.com/systemd/systemd/blob/main/man/org.freedesktop.login1.xml), [GNOME SessionManager API](https://gnome.pages.gitlab.gnome.org/gnome-session/re04.html), [GNOME extension guidance](https://gjs.guide/extensions/overview/updates-and-breakage.html), [Linux clocks](https://man7.org/linux/man-pages/man2/clock_gettime.2.html). | No live GNOME 46–50 session or Debian-family `[deep]` hardware was available here; all physical and lifecycle behavior remains unverified. |
| 2026-10-05 | GNOME Wayland plan 0.3 | The agent now retains ambiguous GNOME cookies, binds them to the issuing SessionManager owner, blocks duplicate acquisition and suspend, retries with a bounded client-disconnect fallback, and exposes pending state to the panel/CLI. Sleep completion requires boottime/monotonic evidence instead of trusting `PrepareForSleep(false)`. Brightness restoration rereads machine ID, journal contents, adapter, and output identity immediately before writing. Deterministic fault coverage and both source reviews are recorded in `gnome-wayland/task.md`. | Live cookie removal, Shell lifecycle, actual failed-sleep signaling, GNOME 46–48 panel mapping, and attended Debian-family `[deep]` behavior remain unverified. |
