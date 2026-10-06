# GNOME Wayland plan 0.3 implementation ledger

This is the persistent implementation ledger for `planning/plan0.3.md`. A checked implementation item means the local source and deterministic tests cover it. Live GNOME Wayland and physical `[deep]` hardware gates remain unchecked until actually observed. Do not delete this ledger after completion.

## Preparation and baseline

- [x] Replace the previous task ledger with plan 0.3 subtasks before changing implementation files.
- [x] Read plan 0.3, the current agent, panel, CLI, tests, documentation, and all relevant release/wake/brightness call sites.
- [x] Run the existing local test and static-check baseline before implementation.

## GNOME inhibitor cookie lifecycle

- [x] Add explicit cookie states (`absent`, `held`, `release-pending`) and bind every cookie to the GNOME SessionManager owner generation that issued it.
- [x] Publish desired, effective, pending-release, and inhibitor outcome fields consistently through `GetState` and `StateChanged`.
- [x] Retain the cookie and owner after an ambiguous `Uninhibit` failure; publish a persistent diagnostic instead of falsely reporting a fully released state.
- [x] Retry pending release only against the same GNOME owner; invalidate without sending the old numeric cookie after owner loss/replacement.
- [x] Prevent acquisition and duplicate GNOME inhibitors while an old cookie release remains pending.
- [x] Add a bounded recovery policy for repeated ambiguous release failures and record whether session-bus disconnect/controlled shutdown was requested or observed.
- [x] Audit normal off, failed-acquire rollback, owner replacement, deliberate suspend, stop, and signal cleanup so none silently strands or discards a cookie.
- [x] Ensure unrelated refreshes do not clear the pending-release diagnostic.

## Deliberate suspend safety

- [x] Make timer/failsafe sleep consume its trigger once and release owned inhibitors before requesting suspend.
- [x] Block login1 `Suspend(false)` whenever the GNOME cookie is held or release-pending.
- [x] Publish and notify the precise “suspend not requested: inhibitor release uncertain” outcome and initiate bounded recovery without immediate resuspend.
- [x] Preserve optional lid inhibitor state and required-pair truth through failed releases and retries.

## Suspend/wake classification

- [x] Capture boottime-minus-monotonic evidence at sleep-entry/reconciliation boundaries and define a conservative suspend-gap tolerance.
- [x] Treat `PrepareForSleep(false)` as preparation completion and require clock-gap evidence before classifying a real suspend/resume.
- [x] Distinguish proven suspend, failed preparation, and uncertain/missing-signal outcomes in `timerOutcome` and internal phase.
- [x] Consume overdue timers after every completed/failed/uncertain preparation without issuing a second suspend; use “expired during suspend” only for proven suspend.
- [x] Preserve unexpired timers only while current lid/prevention conditions remain satisfied.
- [x] Keep deliberate suspend refusal outcomes from being overwritten or rearmed by a later false signal.
- [x] Bound missing true/false signals and login1 state read failures; reset all sleep flags and clock baselines after reconciliation.

## Brightness restore identity

- [x] Factor one schema-2 validation path for machine ID, adapter type, output identity, phase, and numeric brightness values.
- [x] Use that path both while loading the journal and immediately before every restoration write.
- [x] Reread the persisted journal and machine ID at write time and require it to match the authorized in-memory record.
- [x] Preserve the record and make zero writes/deletions for unreadable machine ID, changed machine, schema, adapter, output, or on-disk record.
- [x] Preserve compare-before-write behavior for manual/GNOME brightness changes and retry write/cleanup failures.
- [x] Confirm every restore trigger reaches the same gate: lid open, dimming off, prevention off, stop, restart, owner change, reconcile, and wake.

## Panel, CLI, and documentation

- [x] Display pending GNOME release distinctly in the GNOME panel while retaining action errors across refreshes.
- [x] Ensure CLI status/action output exposes the same requested/effective/pending semantics.
- [x] Update README and Linux research checklist with implemented semantics, limitations, and unchanged live validation gates.

## Deterministic tests and local verification

- [x] Test release success, refusal/timeout, retry success, remotely executed/lost reply, owner replacement with cookie reuse, duplicate-acquire prevention, and bounded fallback.
- [x] Test that no suspend call occurs while GNOME release is uncertain and no trigger is rearmed.
- [x] Test proven suspend, failed preparation without a clock gap, overdue delayed preparation, false without true, duplicate/reordered/missing signals, unexpired continuation, lost conditions, and bounded uncertainty.
- [x] Test machine ID changing after load, temporarily unreadable ID and recovery, replaced on-disk record, output change, manual brightness change, and successful matching restoration.
- [x] Run Python agent tests, Node panel harness, Python compile, shell syntax, JavaScript syntax, JSON parsing where applicable, and `git diff --check`.

## Double check pass 1: branch and ownership trace

- [x] Trace every GNOME cookie transition and each release caller, including error persistence and owner-generation handling.
- [x] Trace every sleep event ordering and clock branch, checking exact deadline, phase, outcome, and suspend-call count.
- [x] Trace every brightness restore caller to the shared persisted-record and live-identity validation gate.
- [x] Fix every discrepancy found in pass 1 and rerun relevant checks.

## Double check pass 2: acceptance mapping and full stack

- [x] Independently map every plan 0.3 acceptance case to a deterministic test or an explicit live gate.
- [x] Trace installer → user service → session D-Bus agent → GNOME panel/CLI → inhibitors → timer/wake → brightness recovery → stop/restart.
- [x] Inspect the complete scoped diff for regressions, stale fields, misleading support claims, and accidental user-work changes.
- [x] Rerun the full local verification suite after all pass-2 fixes and record exact results below.

## Live validation gates

- [ ] Disposable GNOME Wayland session: verify normal off, service stop, bounded disconnect fallback, owner changes, panel/CLI state, and GNOME/logind inhibitor disappearance.
- [ ] GNOME 46–48 Debian-family hardware: verify the intended built-in panel, journal recovery, manual brightness conflict, stop/restart, docked/undocked, and AC/battery behavior.
- [ ] Attended `[deep]` hardware: verify true wake versus failed sleep and prove an overdue timer never issues a second suspend.
- [ ] GNOME 49–50: keep manual dimming unavailable unless a safe supported API is separately proven.

## Final verification record

Pass 1 traced all cookie, clock, timer, and brightness branches. It found that a SessionManager `NameOwnerChanged` loss was still being treated as ambiguous until a replacement appeared. The callback now uses that owner-loss signal as direct proof that the old client-owned cookie is gone, never sends the numeric cookie to the replacement, closes the required logind FD, and preserves the `owner-lost` outcome. The 50 agent tests, panel harness, and diff check passed after the fix.

Pass 2 independently traced the installed stack and the plan acceptance matrix. It found that an old `released` or `owner-lost` diagnostic could suppress the safety disconnect when a later GNOME `Inhibit` call had an unknown result. Acquisition fallback now depends only on whether the current call produced a usable cookie. A regression test starts with a stale `released` outcome. A second test covers the SessionManager owner changing while `Inhibit` is in flight; the agent does not send the ambiguous cookie to either owner and closes its client connection so GNOME removes any inhibitor created by that call. Timer tests were also tightened to assert the exact outcome and phase in the suspend, failed-preparation, uncertain-wake, condition-loss, and bounded-reconciliation branches.

### Plan 0.3 acceptance mapping

| Plan case | Deterministic evidence or remaining live gate |
| --- | --- |
| Successful GNOME release | `test_release_restores_brightness_and_closes_both_fds` checks `Uninhibit`, cookie removal, both FDs, effective state, and brightness restoration. |
| Refusal, timeout, or unknown release reply | `test_failed_release_is_visible_and_retains_owned_cookie` checks retained cookie/owner, `release-pending`, effective off, and the persistent error. |
| Ambiguous release followed by retry success | `test_pending_release_retries_same_owner_and_resolves` checks the same-owner retry and transition to `released`. |
| Ambiguous release that may have executed remotely | The same lost-reply injection plus `test_repeated_release_failure_closes_bus_and_requests_restart` covers both safe resolutions: later confirmation or bounded client disconnect. No aggregate inhibitor query is used as ownership proof. |
| Owner replacement and numeric cookie reuse | `test_owner_replacement_invalidates_old_cookie_without_reusing_it` checks that the old number is never sent to the replacement before a new acquisition. |
| Unknown acquisition reply and owner change during acquisition | `test_unknown_gnome_acquire_outcome_disconnects_before_any_retry` and `test_owner_change_during_acquire_disconnects_unknown_cookie_owner` check client disconnect, controlled failure, prevention off, and no unsafe retry/`Uninhibit`. |
| Duplicate acquisition and false fully-off state | `test_pending_release_blocks_duplicate_acquisition`, `test_off_reports_pending_release_after_accepting_requested_state`, and the panel harness check the requested/effective/pending split. |
| Bounded ordinary-off and service-stop recovery | `test_repeated_release_failure_closes_bus_and_requests_restart` and `test_shutdown_release_failure_disconnects_without_waiting_for_retry` check the bounded and immediate disconnect paths. Live inhibitor disappearance remains a gate below. |
| No suspend while release is uncertain | `test_sleep_is_not_requested_while_release_is_uncertain` checks a consumed trigger and zero login1 `Suspend` calls. |
| Proven true/false suspend across a deadline | `test_timer_expired_during_proven_sleep_is_consumed` checks the clock gap, exact outcome, canceled phase, cleared deadline, and zero second suspend requests. |
| Failed/delayed preparation across a deadline | `test_failed_preparation_does_not_claim_suspend` checks the exact failed-preparation outcome, canceled phase/deadline, and zero suspend requests. |
| Failed deliberate suspend followed by false | `test_failed_suspend_outcome_survives_later_false_signal` checks one-shot consumption and preserves `Countdown suspend refused`. |
| False without true and reordered/duplicate signals | `test_false_without_true_is_uncertain_and_never_resuspends`, `test_resume_signal_without_enter_signal_never_resuspends`, and `test_duplicate_enter_signal_does_not_restart_sleep_baseline` cover conservative classification and baseline stability. |
| Missing false, missing both signals, and logind read failure | `test_missing_resume_signal_uses_logind_state`, `test_tick_detects_unannounced_resume_before_suspend_request`, `test_long_unannounced_sleep_preserves_unexpired_timer`, and `test_unknown_logind_state_with_clock_evidence_is_suspend` cover clock/logind reconciliation without a second suspend. |
| Unexpired timer and lost conditions | `test_missing_resume_signal_uses_logind_state` and `test_long_unannounced_sleep_preserves_unexpired_timer` check continuation; `test_condition_loss_cancels_before_deadline` and `test_unexpired_timer_is_canceled_when_condition_is_lost_during_preparation` check exact cancellation outcomes. |
| Bounded uncertain state | `test_bounded_uncertain_sleep_state_resets_all_flags` checks the exact outcome, canceled phase/deadline, and reset sleep baselines/flags. |
| Restore-time machine ID changes and temporary failure | `test_restore_rereads_machine_identity_before_write` and `test_restore_retries_after_machine_identity_is_temporarily_unreadable` check zero unsafe restore writes, retained journal, then one verified restore. |
| Replaced journal, changed output, and manual brightness change | `test_restore_rejects_record_replaced_after_load`, `test_output_identity_mismatch_blocks_restore_and_new_dim`, and `test_manual_change_is_preserved` check no stale write/deletion and conflict preservation. |
| Matching restore and restart recovery | `test_dim_and_restore_after_restart` and `test_adapter_absence_then_return_restores_loaded_record` check the shared validation path and successful restoration. Physical panel mapping remains a gate below. |

### Full-stack trace

1. `install.sh` checks GNOME Wayland and Python D-Bus/GI, copies the agent, CLI, service, and extension, enables/restarts the user service, verifies both service activity and `GetState`, then distinguishes active, enabled-but-inactive, undiscovered, and unavailable extension states.
2. `sleep-disabler-gnome.service` starts the per-user agent with `Restart=on-failure`; the agent owns `org.sleepdisabler.App` on the session bus and starts with prevention off.
3. The extension and CLI call the same D-Bus methods and read the same state dictionary. The panel keeps action errors through refresh, displays pending GNOME release separately, and disables reacquisition while pending. Failed CLI actions print the reachable current state.
4. Enabling obtains the required logind sleep FD and GNOME cookie before `enabled=true`; optional lid locking stays independent. Cookie release is tied to the issuing unique owner, blocks duplicates, and ends in confirmed release, owner loss, or bounded client disconnect.
5. Timer/failsafe triggers are consumed before release and suspend. Wake classification uses the boottime/monotonic gap plus signals/logind state, preserves only eligible unexpired timers, and never issues a second suspend after an overdue or uncertain interval.
6. Every brightness restore entry reaches `restore_brightness`, which rereads the persisted record and machine ID, validates adapter/output identity, compares current brightness, and preserves the journal on validation/write failure.
7. Normal stop cancels the timer, restores brightness if safely possible, releases/invalidates inhibitors, and immediately disconnects the session bus if GNOME release is ambiguous. Bounded ordinary recovery exits with failure so systemd restarts the agent in desired-off state.

### Final local results

- `python3 -m unittest discover -s linux/gnome-wayland/tests -q`: **52 tests passed**.
- `node linux/gnome-wayland/tests/test_extension.mjs`: **passed**.
- Python compile, POSIX shell syntax, JavaScript syntax, metadata JSON parse, and `git diff --check`: **all passed**.
- Complete scoped diff reviewed: changes are limited to the GNOME Wayland implementation, its plans/tests/docs, and the Linux research checklist; support claims still identify all live GNOME, Debian-family, brightness mapping, and `[deep]` work as unverified.
