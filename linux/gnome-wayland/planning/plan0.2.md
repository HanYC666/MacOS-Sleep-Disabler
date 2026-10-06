# Plan 0.2 — GNOME Wayland stack repair and release gates

Status: **implemented and locally checked; live Linux release gates remain open**. Scope: the current per-user GNOME Wayland prototype in `linux/gnome-wayland/`. This follows [Plan 0.1](plan0.1.md) and the 2026-10-05 source-only stack review. The extension → session D-Bus agent → login1/GNOME SessionManager/UPower architecture remains. Implementation details and two review passes are recorded in [task.md](../task.md). No live Linux result is implied.

## 1. Evidence and operating rules

- The main prevention pair is a login1 `sleep` block FD plus `org.gnome.SessionManager.Inhibit` flags `4|8` (suspend and idle). The optional login1 `handle-lid-switch` FD controls logind's low-level lid handling; acquiring it does not prove GNOME will keep a closed laptop awake. [GNOME SessionManager API](https://gnome.pages.gitlab.gnome.org/gnome-session/re04.html), [systemd inhibitor documentation](https://www.freedesktop.org/software/systemd/man/250/systemd-inhibit.html).
- `CLOCK_BOOTTIME` includes time spent suspended; `CLOCK_MONOTONIC` does not. login1's `PrepareForSleep` signals are the primary suspend/resume events. The timer policy remains: if its deadline passed during an unrelated suspend, cancel it at wake; if it has time left, continue it. This includes a laptop using `[deep]` suspend-to-RAM. [Linux clock documentation](https://www.man7.org/linux/man-pages/man2/clock_gettime.2.html), [login1 API](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.login1.html).
- GNOME 46–48's legacy `Screen.Brightness` property needs release-matched introspection. GNOME 49–50's Shell brightness interface has `SetDimming` but no public per-panel read/write brightness value in the inspected source. Keep manual lid dimming unavailable on those releases until a safe output-specific and recoverable API is proven. [GNOME 48 power source](https://github.com/GNOME/gnome-settings-daemon/blob/gnome-48/plugins/power/gsd-power-manager.c), [GNOME 49 Shell source](https://github.com/GNOME/gnome-shell/blob/gnome-49/js/ui/shellDBus.js), [GNOME 50 Shell source](https://github.com/GNOME/gnome-shell/blob/gnome-50/js/ui/shellDBus.js).
- GNOME describes `metadata.json`'s `shell-version` entries as versions tested and officially supported. Static JavaScript syntax and agent tests do not establish that support. [GNOME extension guidance](https://gjs.guide/extensions/overview/updates-and-breakage.html).

## 2. P0 — Close safety and state-machine gaps

### 2.1 Retry brightness recovery after transient record-access failures

**Current path:** `recover_brightness()` leaves `brightness_record=None` when journal reading or `/etc/machine-id` verification fails. `reconcile()` reprobes brightness but attempts restoration only when `brightness_record` is already set. A valid journal loaded while the adapter is absent is already retained in memory; the missing retry is for a record that could not yet be loaded or validated.

1. Give the journal an explicit state: absent, valid/pending, transiently unreadable, invalid/foreign, or resolved. Expose this in `GetState` without treating an unreadable record as a successful dim. Keep the on-disk journal intact on every transient failure.
2. When a journal exists and no valid record is in memory, retry loading it after successful adapter probe, brightness service owner change, resume, and periodic reconciliation. Retry transient I/O and temporarily unavailable machine identity with a bounded cadence and useful log messages. Do not repeatedly attempt to restore a structurally invalid or foreign-machine record; report it as requiring manual attention.
3. Always validate schema, machine identity, adapter identity, and the actual internal output identity before writing during recovery. The current machine ID plus the generic `built-in-panel` label is not a proven output identity. Determine whether the legacy API exposes a stable connector/backlight identity; if it does not, define a conservative single-panel identity rule and reject new dimming whenever later recovery cannot verify the same output. Never create a journal that the recovery path would necessarily refuse to apply.
4. Once the adapter and record are both available, compare current brightness with the recorded applied value. Restore only on a match; preserve a later manual/GNOME change and clear a resolved conflict. A failed restore or journal cleanup remains visible and retryable. Never start another dim while any unresolved journal exists.

**Acceptance:** tests cover initial read permission failure followed by success, missing then restored machine identity, adapter absence then return, a malformed/foreign record, manual brightness conflict, repeated reconciliation, and process restart. The record is never silently discarded or applied to an unverified output.

### 2.2 Keep the required inhibitor pair when the optional lid lock fails

**Current path:** `acquire()` catches only `dbus.DBusException` around `acquire_lid()`. An `OSError` from taking its Unix FD reaches the outer handler and `release()` closes the already acquired required pair. `SetLidMode()` already catches both exception classes.

1. Isolate every optional lid acquisition failure, including D-Bus refusal, FD transfer/`take()` errors, and local FD errors, from required sleep/idle acquisition. Keep `enabled=true` only if the required pair remains held. Set `lidOutcome=failed`, `lidLockAcquired=false`, and a durable error that names the optional failure.
2. Define `lid_mode` as the user's persisted request and `lid_fd` as the effective lock. If the request remains enabled while the optional FD is absent, retry only that FD when login1 returns or a bounded reconciliation runs. Do not close/reacquire the required pair for this retry. Clear the failure only after an effective retry or explicit disable.
3. Recheck owner-loss handling so a stale login1 FD or GNOME session cookie cannot leave `enabled=true`. Preserve the current transactional behavior of `SetLidMode()` and verify rollback if preference persistence fails after a newly acquired optional FD.

**Acceptance:** fault injection for optional D-Bus refusal, `.take()` `OSError`, persistence failure, and login1 owner replacement shows exactly one required sleep FD and one GNOME session cookie throughout each optional failure/retry. State reports request and effective lock separately.

### 2.3 Make resume handling bounded and safe when events are missing

**Current path:** `tick()` sets `resume_pending` after a boottime/monotonic gap greater than two seconds. Only `PrepareForSleep(false)` clears it. A missing resume signal can freeze a countdown; a shorter suspend may evade the gap fallback.

1. Model timer phases explicitly: running awake, entering sleep, reconciling after wake, consumed, and canceled. Record the baseline of both clocks when the timer starts and when a sleep signal arrives. Do not rely on a two-second threshold as the sole fallback. Evaluate clock differences and login1's preparing-for-sleep state when a deadline is due or a wake is suspected.
2. Treat `PrepareForSleep(true/false)` as authoritative when received, but make the fallback self-clearing: reconcile UPower, required inhibitors, and the timer once wake is established, then leave `resume_pending`. If the signal never arrives, a bounded fallback must cancel an overdue or ambiguous countdown instead of hanging forever or issuing an immediate second suspend. Preserve an unexpired countdown after wake when its state is known.
3. Consume a deliberate timer/failsafe trigger before `Suspend(false)`; never rearm it automatically after refusal or resume. Publish distinct outcomes for elapsed while awake, expired during suspend, condition lost, suspend refused, and uncertain/missed wake event. Avoid labeling a deadline as “expired while suspended” when the only evidence is an ambiguous clock/event sequence.
4. Inspect startup during a sleep/wake transition and duplicate or reordered signals. The agent starts with no active timer today, but its sleep state still must not produce a false immediate suspend if persistence is added later.

**Acceptance:** deterministic clock/event tests cover normal awake expiry; sleep shorter and longer than two seconds; expiry during sleep; wake before and after the tick; missing `PrepareForSleep(false)`; missing both signals; duplicate signals; unexpired timer continuation; lid/prevention condition loss; and rejected `Suspend(false)`. On `[deep]` hardware, an unrelated wake never immediately triggers a second suspend.

## 3. P1 — Keep panel and installation results truthful

### 3.1 Preserve rejected-action errors and provide a way to turn dimming off

1. After a rejected D-Bus action, refresh `GetState` to restore the authoritative toggle, but keep a separate panel error until dismissal, a later successful action, or a clearly defined timeout. Do not let `_render()` immediately overwrite an error that is absent from agent state. Apply the same behavior to preference write failures, unavailable brightness, and agent reconnects; discard stale errors from a previous D-Bus owner generation.
2. When `lidDimming=true` and brightness becomes unavailable, allow the panel to call `SetLidDimming(false)`. Disable only the transition **into** an unavailable capability. Show `brightnessRecoveryPending` independently from `brightnessAvailable`; the CLI remains a recovery path.
3. Keep the distinction between requested lid mode, acquired low-level lid lock, and physically verified lid behavior. Do not mark `lidOutcome=verified` from FD acquisition alone. Check menu open/close and rejected-toggle behavior on live Shell versions.

**Acceptance:** a failed preference write rolls the switch back while its error remains readable; an unavailable dimming capability can be switched off; agent restart cannot replay an old error or render a destroyed menu.

### 3.2 Distinguish agent installation from top-bar installation

1. Keep the existing agent service-active and CLI D-Bus checks. If `gnome-extensions enable` fails or Shell has not discovered the copied extension, report **agent installed, top-bar control pending**, with a nonzero or explicitly partial installer result. Do not print a blanket “Installed” success or tell the user the menu is ready in that branch.
2. On a live GNOME Wayland session, verify extension discovery, enablement, visible panel item, CLI control, `graphical-session.target` activation, update/restart behavior, and inhibitor release on service stop. Check that `TimeoutStopSec=5` permits the promised clean brightness restore on target systems; if it does not, adjust the unit or narrow the guarantee. A journal plus `Restart=on-failure` gives eventual recovery only while the user manager and service restart are available.
3. Keep unsupported GNOME 49–50 manual dimming visibly unavailable while leaving main prevention usable. Do not silently substitute Shell `SetDimming`: its public D-Bus interface does not expose the per-panel prior value needed by the journal.

**Acceptance:** installer output and exit status identify three separate results: agent usable, top-bar usable, and top-bar pending/failed. Service stop/update is observed without duplicate inhibitors or an unreported dark panel.

### 3.3 Align version claims with measured evidence

1. For each GNOME Shell version in `metadata.json`, run enable → disable → enable, agent absent → present → restarted, D-Bus call rejection, menu open during restart, and timer signal updates in a disposable session. Record distro, GNOME version, session type, result, and evidence in `README.md` and `linux/research_checklist.md`.
2. Retain in `shell-version` only versions that passed those live checks. Until any version passes, call 46–50 an **experimental test range** in prose, but do not present the metadata list as verified support at release. If metadata must remain broad for testing, mark the package as a prototype and block a supported release.
3. Record brightness separately: GNOME 46–48 legacy API must pass release-matched introspection and physical dim/restore tests; GNOME 49–50 stays unavailable unless an independently verified output-specific API and recovery path is implemented. Do not make full feature parity a prerequisite for marking the main inhibitor feature working on a version.

**Acceptance:** every retained metadata entry has a live Shell result, and every feature in the support matrix is marked working, unavailable, or unverified. No release claim relies on syntax checks alone.

## 4. Verification and release order

1. **Source review:** confirm the exact legacy brightness property and login1/GNOME inhibitor signatures for the target distro versions. Capture live introspection before enabling physical dimming. Review panel callback ordering and journal state transitions.
2. **Deterministic tests:** add focused tests for sections 2.1–2.3 and state/signal assertions. Test panel error retention and toggle sensitivity with a Shell-compatible harness where practical; do not use a JavaScript syntax check as a lifecycle substitute.
3. **Disposable GNOME sessions:** test each intended metadata version, agent service lifecycle, installer partial result, and CLI fallback. Record which checks were run; do not infer one release from another.
4. **Attended hardware:** test lid open main prevention, then lid-close behavior on AC/battery and docked/undocked, then dim/restore, manual brightness conflict, agent kill/restart, and timer/failsafe suspend. Record `/sys/power/mem_sleep` and repeat the wake cases with `[deep]` where available. Keep the lid and CLI available for recovery.
5. **Release decision:** update the README support matrix, checklist, and metadata from measured results. Main inhibition, optional lid handling, brightness, timer, and installer/top-bar availability receive separate outcomes.

## 5. File map

| File | Planned change |
| --- | --- |
| `agent.py` | Journal retry/classification and output identity; optional FD isolation/retry; bounded resume state machine and timer outcomes. |
| `extension/sleep-disabler@local/extension.js` | Durable rejected-action error, authoritative rollback, and dim-off access when capability disappears. |
| `install.sh`, `sleep-disabler-gnome.service` | Partial top-bar result, service stop/restore gate, update behavior. |
| `extension/sleep-disabler@local/metadata.json` | Only live-tested Shell versions for supported releases. |
| `tests/test_agent.py` | Fault injection and clock/event matrices; state and signal assertions. |
| `ctl.py`, `README.md`, `linux/research_checklist.md` | Recovery/status wording, diagnostics, observed support matrix and evidence. |

## 6. Coverage audit of the source-only review

These boxes mean the issue is **included in this plan**, not fixed in code.

- [x] Transient journal read/identity failure and missing periodic recovery retry — §2.1.
- [x] Foreign/ambiguous output safety and journal conflict behavior — §2.1.
- [x] Optional lid FD `OSError` must not release required inhibitors; optional retry and truthful effective state — §2.2.
- [x] Missing resume signal, short suspend, permanent `resume_pending`, and no immediate re-suspend on `[deep]` — §2.3.
- [x] Rejected panel action error must remain visible after authoritative refresh — §3.1.
- [x] Dimming must remain switchable off when brightness capability disappears — §3.1.
- [x] Acquired lid lock must not be reported as physically verified lid behavior — §§3.1, 4.
- [x] Agent health and top-bar enablement must have distinct installer outcomes — §3.2.
- [x] Service stop/restart and brightness recovery guarantee must be checked — §3.2.
- [x] GNOME 46–50 metadata claims require live lifecycle evidence; GNOME 49–50 manual dimming stays unavailable pending a safe API — §§3.2–3.3.
- [x] Debian-family GNOME Wayland, AC/battery, docked/undocked, and `[deep]` hardware gates remain explicit — §4.
