# Plan 0.3 — GNOME Wayland inhibitor release, wake classification, and brightness identity

Status: **implemented in the local source and deterministic harnesses; live GNOME and hardware validation remains open**. Scope: the per-user GNOME Wayland prototype in `linux/gnome-wayland/`. This follows [plan 0.2](plan0.2.md) and the subsequent top-to-bottom source review. Preserve the existing architecture and the open GNOME 46–50 and Debian-family hardware release gates.

## 1. Verified API constraints and current behavior

- GNOME SessionManager `Inhibit` returns a cookie that the same client should pass to `Uninhibit`; disconnecting that client from the session bus also removes its inhibitor. `IsInhibited(flags)` reports whether **any** matching inhibitor exists, so it cannot establish that this app's cookie was released. `GetInhibitors()` exposes inhibitor objects and metadata, but does not directly map an `Inhibit` cookie to an object path. [GNOME manager API](https://gnome.pages.gitlab.gnome.org/gnome-session/re04.html), [GNOME inhibitor API](https://gnome.pages.gitlab.gnome.org/gnome-session/re06.html).
- A logind `Inhibit` FD releases its lock when closed. `PrepareForSleep(false)` follows either completed sleep **or a failed sleep attempt**. `PreparingForSleep` is true only between the two signals and does not emit property-change notifications. Therefore the false signal proves the preparation interval ended, not that the machine slept. [systemd inhibitor design](https://github.com/systemd/systemd/blob/main/docs/INHIBITOR_LOCKS.md), [login1 API](https://github.com/systemd/systemd/blob/main/man/org.freedesktop.login1.xml).
- `CLOCK_BOOTTIME` includes suspend time and `CLOCK_MONOTONIC` excludes it. A positive change in their difference can provide evidence of suspend; signal order alone cannot. On a `[deep]` machine, the timer must still cancel an overdue countdown after an unrelated suspend without immediately suspending again. [Linux clock documentation](https://man7.org/linux/man-pages/man2/clock_gettime.2.html).
- Current `release()` forgets the GNOME cookie even if `Uninhibit` fails, then reports `enabled=false`; `sleep_now()` proceeds to `Suspend(false)` after that release. Current `on_prepare_sleep(false)` always calls `finish_wake(True)`, which can report “expired while suspended” after a failed attempt. Current `restore_brightness()` checks output identity and brightness, but does not reread `/etc/machine-id` immediately before writing. See `agent.py` and the existing tests.

## 2. P0 — Make GNOME inhibitor release truthful and recoverable

### 2.1 Model requested, held, and uncertain release separately

1. Introduce an explicit GNOME cookie lifecycle, for example `absent`, `held`, and `release-pending`, tied to the GNOME SessionManager **unique owner generation** in which the cookie was obtained. Keep the cookie while a same-owner `Uninhibit` call has an unknown outcome. A method timeout may occur after the remote method has run, so neither a successful local call nor a timeout alone establishes the opposite state.
2. On `SetPrevention(false)`, service stop, required-inhibitor rollback, or deliberate suspend: close this app's logind sleep/lid FDs as appropriate, attempt `Uninhibit`, and publish `desired=false` immediately. If the GNOME release fails ambiguously, publish an explicit `gnomeReleasePending`/`inhibitorOutcome` and error; do **not** present the combined state as fully off or silently discard the cookie. The panel and CLI status must distinguish “requested off, GNOME release uncertain” from both fully on and fully off.
3. Retry a pending cookie only while the same GNOME owner still exists. Prevent a new prevention acquisition from overwriting that cookie or creating a duplicate GNOME inhibitor; either resolve release first or return a precise pending-state error. If the old owner disappears, mark that owner's cookie gone and invalidate it before acquiring under the new owner. Never send an old cookie to a replacement owner, where numeric cookies can be reused.
4. Resolve a retry only after a successful same-owner `Uninhibit`, an independently verified no-such-cookie response whose semantics have been confirmed on the target GNOME release, or loss of the cookie's owning session-bus connection/owner. Do not use aggregate `IsInhibited(12)` as proof that this app's cookie is gone; other applications may hold identical flags. Do not use matching app ID alone as a unique-cookie proof.
5. Define a bounded recovery path for repeated ambiguity: surface the error during retries, then close the agent's owning session-bus connection or terminate the agent in a controlled way so GNOME drops the client-owned cookie. Ensure this path works for an ordinary `off` request and for service stop. If the service is expected to restart, use its existing restart policy deliberately and return with prevention off. Record which outcome was observed rather than claiming that a bus disconnect already happened merely because it was requested.

### 2.2 Keep deliberate suspend and panel state consistent

1. `sleep_now()` must not call login1 `Suspend(false)` while this app may still hold the GNOME suspend cookie. Consume the timer/failsafe trigger once as today; if release cannot be established, publish “suspend not requested: inhibitor release uncertain,” notify the user, and take the recovery path above. Do not loop into an immediate second suspend after recovery.
2. Make `GetState`, `StateChanged`, the GNOME panel, and `sleep-disablerctl status` use the same requested/effective/release-pending meanings. Decide whether the existing `enabled` key means “required pair definitely held” and keep it consistent; add a separate pending field rather than making `enabled=false` mean “all locks definitely gone.” Preserve the optional lid lock's independent requested/acquired status.
3. Inspect all release call sites: `SetPrevention(false)`, failed `acquire()`, login1/GNOME owner replacement, `sleep_now()`, `stop()`, and signal-driven cleanup. Stop/restart must not leave a known cookie stranded in a still-connected agent. A callback that failed to release must not clear its diagnostic error on a later unrelated refresh.

**Acceptance:** Inject a successful release, a D-Bus refusal, timeout/unknown reply, release that executed remotely but lost its reply, retry success, GNOME owner replacement with a reused numeric cookie, and repeated failure followed by client disconnect. Assert no duplicate inhibitor acquisition, no false “fully off” status, no old-cookie call against a new owner, and no `Suspend(false)` while the release is uncertain. In a live GNOME session, verify this app's inhibitor disappears after normal off, service stop, and the fallback disconnect; inspect both GNOME and logind inhibitor views rather than interpreting an aggregate flag as ownership.

## 3. P0 — Distinguish completed suspend from failed preparation

1. Give the sleep interval an explicit outcome source: observed `PrepareForSleep(true)`, observed `PrepareForSleep(false)`, the boottime-minus-monotonic baseline before/after, and current login1 `PreparingForSleep`. Use the existing `sleep_started` only if it contributes to classification; otherwise remove it. Keep the current bounded missing-signal fallback.
2. Treat `PrepareForSleep(false)` as **end of preparation**. Classify “suspended and resumed” only when the two-clock gap supplies sufficient evidence of actual suspend. If the gap is absent or below a defensible tolerance, classify the interval as failed/uncertain; never label its deadline “expired while suspended.” The signal by itself is not proof. A missing `true` signal or duplicate/reordered signals must also remain conservative.
3. If an overdue countdown is observed after a proven suspend, cancel it as “expired during suspend; no second suspend.” If an overdue countdown follows failed or uncertain preparation, cancel it with a separate outcome and no immediate `Suspend(false)`. Preserve an unexpired countdown after either outcome when the timer conditions still hold; recheck lid and prevention conditions before continuing. Reset `sleeping`, `resume_pending`, and clock baselines on every completed reconciliation, including the bounded fallback.
4. Keep the existing deliberate timer/failsafe one-shot behavior. A rejected `Suspend(false)` and a later `PrepareForSleep(false)` must not overwrite the more precise “suspend refused” outcome or rearm the timer. Separate the agent's own deliberate sleep request from unrelated sleep events so notifications do not claim a successful resume after a failed request.
5. Check the 30-second uncertain-state path and `PreparingForSleep` read failure: a countdown must neither hang forever nor fire as soon as the machine becomes available. Record “uncertain wake/failed sleep” when evidence cannot distinguish them. Do not infer physical `[deep]` behavior from simulated clock tests.

**Acceptance:** Deterministic tests cover a true/false pair with a measured suspend gap, a true/false pair after a failed sleep with no gap, a delayed preparation that crosses the timer deadline without sleeping, failed `Suspend(false)` followed by false, false without true, duplicate/reordered events, missing false, missing both signals, unexpired countdown continuation, lost lid/prevention conditions, and bounded uncertain state. Each case asserts the exact `timerOutcome`, phase, deadline, and number of suspend calls. An attended `[deep]` run confirms that a wake after the deadline does not issue a second suspend.

## 4. P0 — Revalidate brightness identity at the restoration write

1. Factor a single record-validation routine for schema 2, machine ID, adapter type, and stored output identity. Use it both when loading the journal and immediately before `restore_brightness()` writes the saved value. Recheck the persisted record against the in-memory record before writing, so a replaced/edited journal cannot silently authorize a stale write. Keep the existing strict connector/backlight rule; it remains a topology guard, not physical proof of GNOME's target panel.
2. If `/etc/machine-id` is temporarily unreadable, leave the record and current brightness unchanged, mark recovery pending/unreadable, and retry with the existing bounded cadence. If the machine ID differs, mark the record foreign/invalid, retain it for manual attention, and block further dimming. Apply the same no-write/no-delete rule to a changed schema, adapter, output identity, or persisted record. Never use the in-memory identity alone as authority after a failed verification.
3. After successful validation, retain the existing compare-before-write rule: restore only when current brightness equals the value the app applied. Preserve later manual/GNOME changes, and keep failed writes or journal cleanup retryable. Document that read/compare/write through the legacy property is not atomic and that physical panel mapping still needs a live test.
4. Check every invocation of restoration: lid open, dimming disabled, prevention off, service stop, agent restart, adapter owner change, reconciliation, and wake. All must reach the same validation gate. `GetState` and the panel must show a retained journal independently of current brightness API availability.

**Acceptance:** Tests change machine ID **after** a valid record is loaded; simulate an unreadable ID during restore followed by recovery; replace the on-disk record after load; change output identity; and simulate a manual brightness change. Assert zero writes and no record deletion on any failed identity check, then one restoration on a matching record. On GNOME 46–48 hardware, inspect the live API and verify dim/restore on the intended built-in panel, including service stop and restart; keep GNOME 49–50 manual dimming unavailable unless a separately proven safe API exists.

## 5. Integration and release sequence

1. Implement inhibitor lifecycle and expose pending status first. Review all `release()` callers, then test off → on, failed acquisition rollback, owner change, deliberate sleep, and service stop. Confirm that pending release cannot be masked by a later UI refresh.
2. Implement timer classification using both clocks and logind state. Add the failed-preparation cases before changing the live wake path. Preserve the conservative no-second-suspend policy.
3. Implement shared brightness validation and route every restore through it. Review crash/journal states and manually altered records. Do not remove a journal to make a test pass.
4. Run focused agent and panel tests plus static checks. Perform two independent source-level passes: (a) trace each failure/owner/clock branch; (b) map every acceptance point below to a test or an explicit live gate. Update `task.md` with implementation subtasks and mark them only after each pass.
5. On a disposable GNOME Wayland session, verify GNOME SessionManager inhibitor lifecycle, panel/CLI pending state, service stop/restart, and true versus failed sleep outcomes. On attended Debian-family hardware, verify the built-in brightness identity and `[deep]` timer behavior. Record distro, GNOME version, display server, service owner changes, and results in `README.md` and `linux/research_checklist.md`. Keep the plan 0.2 GNOME 46–50, AC/battery, and docked/undocked gates open until actually observed.
6. Only then update supported-version claims and release status. The current macOS source-level environment cannot certify GNOME Shell lifecycle, physical panel mapping, inhibitor removal, or `[deep]` behavior.

## 6. File map

| File | Change |
| --- | --- |
| `agent.py` | Cookie lifecycle and owner generation; release-pending state; deliberate-suspend guard; clock-backed sleep classification; shared brightness identity validation. |
| `extension/sleep-disabler@local/extension.js` | Show uncertain release distinctly and keep action errors visible through refresh/reconnect. |
| `ctl.py` | Surface new state and precise errors through `status` and action results if current generic JSON output is insufficient. |
| `tests/test_agent.py`, `tests/test_extension.mjs` | Release/owner/fallback fault matrix, failed-sleep clock matrix, restore-time identity changes, and visible pending state. |
| `README.md`, `linux/research_checklist.md`, `task.md` | Actual semantics, evidence, open hardware/session gates, and two-pass implementation record. |

## 7. Coverage audit

The boxes below mean the issue is **covered by this plan**, not implemented.

- [x] Failed GNOME `Uninhibit` must retain the cookie and expose uncertain release rather than reporting fully off — §2.1.
- [x] Retry only against the cookie's owner generation; do not create duplicate inhibitors or treat aggregate `IsInhibited` as proof — §2.1.
- [x] Bound unresolved release with a client-disconnect path; cover ordinary off, rollback, service stop, and deliberate suspend — §§2.1–2.2.
- [x] Do not call `Suspend(false)` while the app's own GNOME suspend inhibitor may remain — §2.2.
- [x] `PrepareForSleep(false)` can follow failed sleep; require clock evidence before saying “expired while suspended” — §3.
- [x] Keep overdue timers one-shot, unexpired timers conditional, and missing/duplicate signals bounded, including `[deep]` — §3.
- [x] Recheck machine ID and persisted journal immediately before a brightness restore write; preserve records on failure — §4.
- [x] Route every restore trigger through that validation and retain manual-change and output guards — §4.
- [x] Add fault tests, panel/CLI truth checks, GNOME session tests, attended brightness and `[deep]` gates, and two source review passes — §§2–5.
