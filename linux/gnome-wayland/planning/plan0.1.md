# Plan 0.1 — GNOME Wayland prototype repair

Status: **code changes implemented; Linux integration gates open**. Scope: `linux/gnome-wayland/` on GNOME Wayland, with Debian-family packages as the first target. This plan fixes the findings from the source-level stack review; it does not claim Linux runtime validation. Preserve the existing per-user architecture: Shell extension → session D-Bus agent → login1, GNOME SessionManager, and UPower.

## 1. Evidence and constraints

- GNOME 48's power daemon owns `org.gnome.SettingsDaemon.Power` at `/org/gnome/SettingsDaemon/Power`. Its `org.gnome.SettingsDaemon.Power.Screen` interface exposes a read/write **`Brightness` property**; its introspection XML does not declare `GetPercentage` or `SetPercentage`. The prototype currently uses the interface name as the bus destination and calls those absent methods. [GNOME 48 power source](https://github.com/GNOME/gnome-settings-daemon/blob/gnome-48/plugins/power/gsd-power-manager.c)
- Current GNOME power code uses `org.gnome.Shell.Brightness` at `/org/gnome/Shell/Brightness`; GNOME Settings checks `HasBrightnessControl` there. Obtain the exact read/write brightness signatures by inspecting the matching GNOME Shell release source and live D-Bus introspection before implementing this adapter. Do not infer a method from the service name. [Current GNOME power source](https://github.com/GNOME/gnome-settings-daemon/blob/main/plugins/power/gsd-power-manager.c), [GNOME Settings power panel](https://github.com/GNOME/gnome-control-center/blob/main/panels/power/cc-power-panel.c)
- `org.gnome.SessionManager.Inhibit` flags 4 and 8 mean suspend and idle inhibition; a successful request does not promise that every user-initiated sleep can be blocked. [GNOME SessionManager API](https://gnome.pages.gitlab.gnome.org/gnome-session/re04.html)
- `CLOCK_BOOTTIME` counts time spent suspended. login1's `PrepareForSleep(false)` identifies resume. A timer that has elapsed while suspended therefore needs an explicit resume policy. [Linux clock documentation](https://man7.org/linux/man-pages/man2/clock_gettime.2.html), [login1 API](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.login1.html)
- GNOME's `shell-version` list is a support declaration, not runtime proof. Keep 46–50 only after loading and exercising the extension on each claimed release; otherwise narrow the list and document untested releases. [GNOME extension anatomy](https://gjs.guide/extensions/overview/anatomy.html), [GNOME extension maintenance](https://gjs.guide/extensions/overview/updates-and-breakage.html)

## 2. Work order

### P0 — Make brightness capability truthful

**Current defect:** `agent.py` constants and `brightness_api()` target the wrong bus name; `refresh_brightness_capability()`, `dim_brightness()`, and `restore_brightness()` call methods absent from GNOME 48. On GNOME 49+, the old Screen interface is removed.

1. Introduce a small brightness adapter with `probe`, `read`, `write`, and `identity` operations. Keep GNOME 46–48 Screen and GNOME 49–50 Shell implementations separate. Select by **successful introspection and capability probing**, not the Shell version string alone. Probe both service ownership and the required property/method signatures. Reprobe after owner changes and resume.
2. For the legacy adapter, use bus destination `org.gnome.SettingsDaemon.Power`, object `/org/gnome/SettingsDaemon/Power`, interface `org.gnome.SettingsDaemon.Power.Screen`, and the documented `Brightness` property via `org.freedesktop.DBus.Properties.Get/Set`. Check that the property is present, writable, and has the expected integer type on the actual target release. Never call `GetPercentage`/`SetPercentage` on GNOME.
3. For the newer adapter, inspect the matching GNOME Shell release's D-Bus XML/source and a live `busctl --user introspect` result. Record the exact service, path, interface, property or method signatures, range, and how the internal display is identified. Implement only after that evidence is recorded. If multiple displays cannot be disambiguated, mark dimming unavailable rather than change the wrong output.
4. `brightnessAvailable` must mean a working **built-in-panel** read/write path, not merely that a service exists. Add a reason field such as `brightnessError` for unsupported API, no internal panel, and probe/write failure. The panel and CLI should show this state. A zero value may mean minimum brightness rather than screen off; label it accordingly until hardware confirms otherwise.

**Done when:** each adapter has a contract test against captured introspection; a GNOME 46/48 target and a GNOME 49/50 target can read, dim, and restore the built-in panel; unsupported sessions remain safely unavailable. Update `README.md` to name the selected API and the actual tested versions.

### P0 — Make brightness recovery crash safe

**Current defect:** `dim_brightness()` writes a record, changes brightness, then rewrites the record. Its exception handler can delete the only recovery record after brightness has already changed.

1. Define a journal record containing schema version, adapter/output identity, prior value, intended value, and phase (`prepared` or `applied`). Write it atomically before any brightness change. Flush and `fsync` the record and containing directory if the design promises power-loss durability; otherwise state that it only covers process crashes. Reject an invalid or foreign-output record without guessing.
2. Write the target brightness and read it back. Persist the observed applied value if it differs from the requested value. If this second write fails, **retain** the prepared record. Never delete a record merely because a write or readback failed.
3. On lid open, feature disable, clean stop, service restart, brightness-service restart, and resume: compare the current output value with the value the app actually wrote. Restore the prior value only on a match. If the user or GNOME changed brightness meanwhile, preserve that newer value and clear the record with a documented conflict outcome. If the API/output is temporarily absent, keep the record and retry when it returns.
4. Recovery must happen before accepting another dim operation. Ensure an agent crash after the hardware write but before the `applied` phase is recoverable from the `prepared` record. Record and surface pending recovery rather than silently treating dimming as ready.
5. Decide whether a separate watchdog is required to restore brightness promptly after `SIGKILL` when systemd restart is disabled. The existing service restart only gives eventual recovery; it is not a complete guarantee after logout or a failed restart. Document the chosen guarantee and add a watchdog if prompt restoration is a release requirement.

**Done when:** fault injection at each journal/write/readback/restore step leaves either the original brightness or a retained recovery record; a manual brightness change is never overwritten; agent and brightness-service crashes recover on restart.

### P0 — Prevent immediate re-suspend on resume

**Current defect:** `on_prepare_sleep(false)` calls `tick()`. If a timer expires during unrelated sleep, `tick()` immediately calls `Suspend(false)` again after wake.

**Policy for 0.1:** preserve elapsed-time semantics while awake, but **cancel an overdue timer on resume** with a visible `expired while suspended` outcome. Do not issue a second suspend on that resume. A timer that still has time remaining continues with its original `CLOCK_BOOTTIME` deadline. This policy is intentionally conservative for attended wake and `[deep]` suspend-to-RAM.

1. Track sleep entry and resume generation from `PrepareForSleep(true/false)`. On resume, reconcile UPower, lid, inhibitor, and brightness state first. Then compare the deadline with `CLOCK_BOOTTIME`; cancel if expired during sleep. Publish the outcome and clear the timer before any ordinary tick can act.
2. Guard the one-second tick against the interval between wake and the resume signal. Use a resume-pending state or another serialization rule so an overdue timer cannot race the wake callback. Define behavior if the agent was started during an existing sleep/wake transition.
3. Preserve existing condition checks: lid-open and prevention-off timers cancel. An unconditioned timer remains allowed when prevention is off while awake. All deliberate suspend paths consume the timer **before** making the login1 call; a failed suspend does not rearm it.
4. Apply the same no-loop rule to the battery failsafe: after its suspend attempt, prevention is off and the trigger is consumed until the user turns it on again. Show failures and never automatically retry a refused suspend every 15 seconds.

**Done when:** tests cover timer expiry while awake, before sleep, during sleep, at wake, after wake, condition loss, and `Suspend(false)` failure. On a `[deep]` laptop, an unrelated suspend/resume never immediately sends the machine back to sleep.

### P1 — Change lid mode without dropping main protection

**Current defect:** `SetLidMode()` calls `release()` and `acquire()`, briefly removing the working sleep and idle inhibitors. A failed reacquisition leaves prevention degraded.

1. Separate ownership of the required `sleep` FD, GNOME session cookie, and optional `handle-lid-switch` FD. With prevention enabled, changing lid mode should add or release **only** the optional lid FD. Do not touch the required locks or the brightness journal.
2. Make the change transactional: acquire the new optional FD before committing `lid_mode=true`; if acquisition fails, retain the prior mode and required locks and return a specific error. For disabling, close the lid FD and publish the final state. Decide whether saved preference means requested mode or successfully active mode, and use one meaning consistently.
3. On login1 or GNOME SessionManager owner loss, mark the corresponding required capability degraded; reacquire a complete required pair when both services are available. Keep the optional lid lock independent. Do not display `enabled=true` based only on stale local FD/cookie values after owner replacement.

**Done when:** repeated lid toggles keep exactly one required login1 sleep inhibitor and one GNOME session inhibitor throughout; optional acquisition failure leaves main prevention active; bus-owner restart produces truthful degraded/recovered state.

### P1 — Report lid support and other capabilities accurately

**Current defect:** the panel shows a `lidMode` switch, while `lidInhibitor` may be false. An acquired low-level lock also does not prove that GNOME will keep a closed laptop awake.

1. Expose separate fields for `lidRequested`, `lidLockAcquired`, and `lidOutcome` (`unverified`, `verified on this session`, `failed`, or `unavailable`). Keep `enabled` reserved for the required main inhibitor pair. Never derive `lidOutcome=verified` from lock acquisition alone.
2. Render these distinctions in the top-bar menu and CLI. Show a clear error when an optional lock is refused, when UPower cannot report lid state, or when dimming is unavailable. A disabled/disconnected agent must disable actions and show its state.
3. Document a short physical lid test on AC and battery, docked and undocked, with a dwell longer than GNOME's lid delay. Record observed results by GNOME release and laptop; do not promise universal lid support from a single successful test.

**Done when:** `lidMode=true` cannot be mistaken for proven lid prevention and failure remains visible after the menu closes/reopens.

### P1 — Make preference updates transactional

**Current defect:** setters mutate live state before `save_settings()`. An `OSError` can return a failed D-Bus call after behavior has already changed.

1. Validate proposed threshold, failsafe, lid, dimming, and timer-default values first. Build the next preference snapshot without mutating current state. Persist it atomically, then commit the in-memory setting and publish one authoritative state change. For operations with an external side effect, define an explicit prepare/apply/commit/rollback sequence; never leave an unreported partial result.
2. Translate file and D-Bus errors into stable app-specific D-Bus errors, log the underlying cause, and refresh the panel from `GetState` after a rejected toggle. Preserve the old preference and active behavior when persistence fails; if rollback itself fails, expose a degraded state that names the remaining active effect.
3. Use the same rule for `StartTimer`: if saving the last-used duration/options fails, do not silently start a timer while the call reports failure. Specify whether a read-only state directory can run with nonpersistent settings; for 0.1, reject the operation clearly.

**Done when:** permission denied, full disk, malformed existing preferences, and atomic-replace failure each leave a truthful `GetState` and matching panel/CLI result.

### P1 — Verify packaging and panel compatibility

1. Check the installer from an actual GNOME Wayland user session: dependency detection, extension discovery after copy, `graphical-session.target` activation, service restart, and CLI access. Do not report installation success if the agent failed to start. On update, avoid duplicate inhibitors and stale extension state.
2. Exercise extension lifecycle on every release retained in `metadata.json`: enable/disable/re-enable, agent absent/present/restarted, D-Bus failure, menu open during restart, and timer updates. Confirm callbacks cannot render a destroyed actor or resubscribe after disable. Keep the CLI as an independent recovery control.
3. Check that an unsupported brightness adapter leaves the main prevention switch usable. Update the README with the real supported-version matrix and diagnostics. Narrow metadata claims if any Shell release remains untested.

**Done when:** install → agent start → panel/CLI control → stop/restart → clean inhibitor release is observed on each declared GNOME release, or that release is removed from the support declaration.

## 3. Verification sequence

1. **Source/API gate:** capture release-matched GNOME 46, 48, 49, and 50 brightness introspection; confirm exact D-Bus signatures before writing adapters. Review login1 and SessionManager call signatures against current source.
2. **Deterministic agent tests:** fake D-Bus services and a fake boottime clock. Test inhibitor acquire/release and owner replacement, each brightness journal failure point, UPower unknown/stale/AC cases, timer wake races, `Suspend(false)` rejection, and preference write failures. Assert emitted state as well as side effects.
3. **GNOME Shell integration:** validate the extension in a disposable GNOME session for each metadata version. Check toggle rollback, signal subscriptions, disconnected state, and cleanup. A JavaScript syntax check alone is insufficient.
4. **Attended hardware gate:** first test main idle prevention with the lid open; then lid behavior and brightness separately. Test AC/battery, docked/undocked, manual brightness conflict, service kill/restart, and an intentional timer suspend. Record `/sys/power/mem_sleep`; repeat the wake scenarios with `[deep]` where available. Keep a way to reopen the lid and stop the user service.
5. **Release gate:** update `README.md`, `research_checklist.md`, and the support matrix with measured outcomes. Mark every feature as working, unavailable, or unverified per GNOME release and hardware. Do not call the prototype feature-complete until P0 and P1 gates pass.

## 4. File map

| File | Planned change |
| --- | --- |
| `linux/gnome-wayland/agent.py` | Brightness adapters/journal; resume-safe timer state; independent inhibitors; transactional setters; capability state. |
| `linux/gnome-wayland/extension/sleep-disabler@local/extension.js` | Show effective versus requested capabilities and recovery/error state; verify lifecycle across Shell versions. |
| `linux/gnome-wayland/extension/sleep-disabler@local/metadata.json` | List only tested GNOME Shell releases. |
| `linux/gnome-wayland/install.sh` and `sleep-disabler-gnome.service` | Check actual service start/update behavior and clear install failures. |
| `linux/gnome-wayland/ctl.py` and `README.md` | Explain capability and timer outcomes; provide recovery diagnostics. |
| `linux/research_checklist.md` | Record introspection evidence and physical test results as they are completed. |

The GNOME 49–50 per-panel brightness write contract remains a release gate: current Shell source exposes dimming and capability state, but no public read/write panel value. A future adapter needs a safe output identity and recovery design before code is changed.

## 5. Implementation record (2026-10-05)

- [x] GNOME 46–48 legacy adapter probes the `Brightness` read/write `i` property and uses `org.freedesktop.DBus.Properties.Get/Set` on the correct service. GNOME 49–50's Shell service exposes `SetDimming`, `SetAutoBrightnessTarget`, and `HasBrightnessControl`, but no public per-panel brightness read/write API in [GNOME 49](https://github.com/GNOME/gnome-shell/blob/gnome-49/js/ui/shellDBus.js) or [GNOME 50](https://github.com/GNOME/gnome-shell/blob/gnome-50/js/ui/shellDBus.js); manual lid dimming remains unavailable there.
- [x] Brightness journal has prepared/applied phases, machine identity, atomic fsynced writes, readback, retained pending records on failure, and conflict-safe restoration. Agent restart is the selected eventual-recovery mechanism; prompt recovery after disabled systemd or logout is outside the 0.1 guarantee.
- [x] Resume consumes overdue countdowns; tick detects a suspend gap before the resume signal. Timer/failsafe suspend attempts consume their triggers before calling login1.
- [x] Optional lid FD toggles independently of required inhibitors. State now distinguishes request, lock acquisition, and physical outcome, which remains unverified until attended tests.
- [x] Preference and last-used timer values persist before in-memory changes. Installer checks service and D-Bus health. Extension reconnect callbacks are guarded against stale owners and disabled actors.
- [x] Eleven local state-machine tests cover legacy probe contract, journal recovery and failure, foreign-machine record rejection, manual brightness conflict, timer resume race, failed suspend, preference failure, lid lock isolation, and owner replacement; Python, JavaScript, and shell syntax checks pass.
- [ ] Live GNOME 46–50 Shell lifecycle, install/update, release-matched introspection, and hardware tests including `[deep]` remain. Metadata still advertises the experimental test range, so release support must not be claimed yet.
