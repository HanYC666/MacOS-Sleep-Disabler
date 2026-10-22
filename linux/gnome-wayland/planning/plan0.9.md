# Plan 0.9 — manual sleep prevention and inclusive battery failsafe

Status: implementation, isolated verification, complete reinstall, administrator
guard installation, and post-login runtime verification complete. No live sleep test.

## Required result

- Prevent ordinary desktop, idle, hardware-key, suspend, hibernate, hybrid-sleep,
  and suspend-then-hibernate requests while prevention is on. Hardware sleep keys
  should lock the session. Root can always remove or bypass installed controls;
  firmware and kernel emergency actions are outside an application’s control.
- Trigger the battery failsafe at **percentage <= threshold**. Equality must not
  rearm an already consumed low-battery episode. Preserve fresh-power checks,
  release-before-suspend ordering, and one request per episode.
- Research desktop-specific controls, implement the GNOME Wayland solution,
  and retain lid stay-awake as experimental. Do not change the user’s logind
  configuration or require restarting logind or logging out for policy changes.
- Test using fake buses/settings/services. Never start a real sleep target,
  issue Suspend/Hibernate, close the lid, or activate a live timer/failsafe.
- After implementation and review, preserve recovery evidence, uninstall the
  laptop’s old managed components, reinstall every updated component, and verify
  versions, loaded extension, policy, prevention state, and disabled automation.

## Current evidence (2026-10-19)

The installed agent is 0.7.0 / API 6. Its service is enabled and active; prevention,
GNOME inhibition, and journaled hardware-key redirection report held/active, with
no agent or key-guard error. Timer is idle and failsafe is off. Battery readback
was 71%, configured threshold 90%: do not enable that failsafe during verification.
All six installed Python files and the panel JavaScript/metadata match the source
byte for byte. Following the user's fresh login, GNOME reports panel 7 enabled
and ACTIVE, and the agent reports panelRuntimeRegistered=true and version 7.
No unattended logout or Shell restart was performed.

Administrator authentication completed and the system installer exited 0, retaining
backups under `/var/lib/sleep-disabler-guard/5040d2c063e84f41bdefd1f91a29a138`.
The agent and independent readback both report the system guard verified. All
four loaded services retain their vendor ExecStart, have the exact non-ignored
guard ExecCondition, no pending reload, and no custom successful exit statuses.
A held login1 inhibitor alone would not establish this additional boundary.

This Kali installation mixes Shell 50.4-1+kali1 with gnome-session-bin 51.0-2 and
gnome-settings-daemon 51.0-1; systemd is 262-1. Shell version alone is insufficient
to select a session/power backend. Local Shell systemActions.activateSuspend calls
SessionManager.Suspend. Upstream gsm_systemd_suspend calls SuspendWithFlags with
SD_LOGIND_SKIP_INHIBITORS (1 << 4). This is an identified bypass, not a reproduced
live suspend. Upstream media-keys separately calls login1 Suspend/Hibernate; simply
adding handle-suspend-key to our inhibitor does not intercept GSD’s own shortcuts.

Sources:

- [GNOME session backend](https://github.com/GNOME/gnome-session/blob/main/gnome-session/gsm-systemd.c)
- [GNOME media-key handling](https://github.com/GNOME/gnome-settings-daemon/blob/master/plugins/media-keys/gsd-media-keys-manager.c)
- [login1 block and bypass semantics](https://github.com/systemd/systemd/blob/main/man/org.freedesktop.login1.xml)
- [systemd ExecCondition behavior](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html)

## Implementation and checks

- [x] Change trigger to <= and positive percentage recovery to >. Update reason
  to include equality. Fake agent suite: 218 tests pass. Boundary coverage includes
  fractional percentages, equality across repeated ticks, recovery above threshold,
  and final post-release dispatch at equality. Historical pre-0.9 plans retain
  their original strict-below language; this plan supersedes that boundary.
- [x] Add read-only sleep_guard.py candidate and seven fake-bus decision tests.
  It recognizes this app’s exact strong sleep inhibitor and never requests sleep.
  Exit 1 means skip systemd’s sleep service, 0 means allow normal policy, and 255
  means unknown state blocks execution. Seven isolated tests pass.
- [x] Verify the guard as an ExecCondition in isolated service execution with a
  harmless ExecStart marker, including blocked, absent, unknown, and release cases.
  Four real user-manager marker cases passed; no system sleep unit was started.
- [x] Install the guard root-owned with drop-ins for systemd-suspend.service,
  systemd-hibernate.service, systemd-hybrid-sleep.service, and
  systemd-suspend-then-hibernate.service. Preserve vendor ExecStart and other
  conditions. Do not modify sleep.target or run sleep units. Verify loaded merged
  configuration. Existing unrelated drop-ins and failure/rollback must be handled.
  This boundary is needed because a desktop can deliberately skip logind locks.
  It automatically permits our timer/failsafe only after our sleep FD is released.
- [x] Add reversible GNOME hardware-key mapping to screen lock while prevention
  is on. Include user and static suspend/hibernate bindings; assess power key
  mapping. Journal original user values before writes, restore only owned values,
  preserve intervening user edits, recover after process crash, and verify writable
  schemas. Test partial writes, failed persistence, recovery, and off/suspend cleanup.
  Eleven in-memory settings cases pass. Agent failure-path tests additionally prove
  known-cookie/FD cleanup on mapping failure and restoration after release errors.
- [x] Redirect Shell’s ordinary suspend action to lock while prevention is on,
  restoring only our own hook on extension disable. Avoid coupling enforcement to
  the extension remaining enabled on the lock screen; the service guard remains.
  GJS harness checks on/off, owner replacement, disconnect, disable, preservation
  of later extension overrides, and lock-without-suspend, in both scheduling modes.
- [x] Report guard availability accurately; held inhibitors alone must not claim
  complete manual-sleep coverage. Consider mixed GNOME component versions.
  Background file/loaded-unit readback expires after 35 seconds. Missing/changed
  helper, missing/ignored condition, pending reload, timeout, and custom success
  exit status cannot claim verification. The panel reports unverified coverage.
- [x] Update runtime versions, installer dependency/staging/rollback paths, CLI,
  README, panel wording, and removal instructions for all added components.
- [x] Run affected isolated suites and compile/syntax checks. Review requirements
  against final code twice, including startup, concurrent enable/off, owner loss,
  external PrepareForSleep, release failure, scheduler/failsafe release, recovery,
  crash, extension disable, and uninstall/reinstall.
- [x] Replace the laptop installation with preserved preferences/recovery records;
  confirm exact loaded versions, service and panel registration, policy readback,
  held guards, no timer, and failsafe off. No actual sleep test is authorized.

### Reinstall and final verification evidence

- The clean user reinstall completed with original preferences and recovery
  records preserved under
  `~/.local/state/sleep-disabler/reinstall-0e9a64d1fbbf49048b222a86591d3715`.
  `before.json` and `after.json` retain the state snapshots. Managed core, CLI,
  service, and extension were archived before replacement. The temporary awake
  keeper was released only after the new core held prevention; it is inactive.
- The first attempt safely restored the old installation after revealing that
  systemd 262 reports a missing unit as `inactive` with exit code 4. The installer
  now accepts that combination only when enablement also reports not-found and
  every managed unit artifact is absent. Existing-unit ambiguity still fails.
- Python agent suite: 218 passed; core installer: 85 run, 84 passed and one
  sandbox-dependent systemd-analyze check skipped; shell installer: 44 passed;
  guard/settings/root-installer/status suites: 33 passed. GJS lifecycle and
  suspend-to-lock checks passed with scheduling both present and omitted.
  Python compile, shell syntax, and `git diff --check` passed. The GJS harness
  must run from `linux/gnome-wayland`, as documented by its relative fixture path.
- Final source review covered key journal ownership and recovery, partial
  acquisition cleanup, release failures, equality/rearm boundaries, asynchronous
  guard readback, panel hook restoration, system guard transaction rollback,
  loaded-unit verification, and staging all added modules. These checks and fake
  tests do not prove physical hardware sleep behavior.
- Post-login verification: the authenticated installer finished, protected helper
  and all four drop-ins match this agent, loaded merged configuration matches,
  and panel 7 is ACTIVE and registered with agent 0.7.0 / API 6. Prevention was
  re-enabled after the fresh process start; service enablement is retained.
- The installed read-only guard returned 0 while prevention was off and 1 after
  enabling prevention. These were direct helper executions, never sleep-service
  starts. Actual GNOME settings show all six sleep/power arrays empty and their
  previous effective bindings merged into screen lock; the recovery journal
  exactly preserves each original user override. The real strong login1 sleep
  inhibitor is present. Evidence snapshots are retained at
  `/tmp/sleep-disabler-post-login-keys-before.json` and
  `/tmp/sleep-disabler-post-login-state.json`.
- The previously sandbox-blocked `systemd-analyze --user verify` check also
  passed against the installed unit after authentication. This supplements the
  historical core-suite skip rather than changing its recorded result.
- Timer and failsafe remain off, sleepPhase is idle, and no sleep request was
  sent. The existing logind configuration was left alone. Lid stay-awake and
  physical sleep-button/suspend behavior remain experimental or untested; they
  need an attended hardware test, outside the authorized no-sleep verification.

Administrator access was used only for installing the system service guard and
reloading its configuration. It did not start sleep units or restart logind.

## Missing-journal follow-up (2026-10-22)

The running agent now retains shortcut ownership in memory if the recovery file
disappears, keeps prevention on, and notifies once per missing-file episode. It
leaves the file absent until release so the user can restore it. Release uses a
restored journal when available, or journals resets to installed distribution
defaults for still-owned settings otherwise. User edits remain preserved and
failed resets remain retryable. This fallback is process-local; losing both the
file and the process loses the recovery information.

The updated core was installed with an independently verified temporary
sleep/lid inhibitor held throughout replacement. All six installed Python files
match source. Prevention was enabled and the temporary inhibitor released; the
new agent's MainPID then independently matched a real strong sleep inhibitor.
GNOME inhibitor/key redirection/system guard are held/active/verified; timer and
failsafe are off. Existing experimental lid mode was preserved. Panel 7 remains
installed and enabled, currently inactive in the locked session. No sleep request
was issued. Verification: 219 fake agent tests, 37 guard/settings/installer/status
tests, and the GJS panel lifecycle harness passed.

## Desktop comparison

| Desktop | Controls and implications |
| --- | --- |
| GNOME | SessionManager inhibition plus strong logind sleep lock cover cooperative policy. GNOME Session can request skip-inhibitors; GSD owns media keys separately. Local schemas expose user and static key arrays. Keep core service enforcement independent of panel lifecycle. |
| KDE Plasma | PowerDevil tracks logind sleep inhibitors and maps them to policy restrictions. Desktop actions remain distinct from idle policy; a GNOME Shell hook/settings adapter is not portable. [PowerDevil source](https://github.com/KDE/powerdevil/blob/master/daemon/powerdevilpolicyagent.cpp) |
| Xfce | xfce4-power-manager normally owns lid and power/suspend/hibernate keys using logind inhibition. Per-function xfconf options can delegate them to logind. Presentation/idle behavior alone does not guarantee manual sleep prevention. [Official FAQ](https://docs.xfce.org/xfce/xfce4-power-manager/faq) |
| Cinnamon | Its own settings daemon and power schema retain desktop-specific button actions, including button-suspend. Do not write GNOME keys as a Cinnamon adapter. [Media keys](https://github.com/linuxmint/cinnamon-settings-daemon/blob/master/plugins/media-keys/csd-media-keys-manager.c), [power schema](https://github.com/linuxmint/cinnamon-settings-daemon/blob/master/data/org.cinnamon.settings-daemon.plugins.power.gschema.xml.in) |
| Sway/wlroots | Idle inhibition and explicit compositor key commands are separate. swayidle before-sleep is a bounded delay to lock, not a sleep veto. A system-service boundary can protect systemd-mediated sleep, but GNOME key redirection cannot. [swayidle manual](https://github.com/swaywm/swayidle/blob/master/swayidle.1.scd) |

These comparisons guide the GNOME implementation; they are not claims of tested
support for other desktops. Lid behavior and physical panel power remain separate
and experimental. A root process writing kernel power state or bypassing the
systemd services cannot be vetoed by this application.
