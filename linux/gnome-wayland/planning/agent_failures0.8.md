# Agent regression failure inventory for plan 0.8

Source: the isolated pre-plan-0.8 full run of 206 tests. This is a **provisional triage**, not proof that any failing assertion can be deleted. Current instruction forbids rerunning sleep/scheduling cases. Each row must be traced through the current callback path before migration or source repair.

| # | Result | Case | Variant | Test line | Immediate failure | Provisional cause | Resolved |
| --- | --- | --- | --- | ---: | --- | --- | --- |
| 1 | ERROR | `test_logind_replacement_requires_fresh_readable_preflight` | `` | 1431 | AttributeError: 'Agent' object has no attribute 'suspend_preflight'. Did you mean: 'suspend_preflight_async'? | removed synchronous preflight call | [ ] |
| 2 | ERROR | `test_owner_loss_cancels_release_source` | `` | 1193 | AttributeError: 'object' object has no attribute 'get_is_connected' | incomplete fake session bus | [ ] |
| 3 | ERROR | `test_preflight_rejects_cookie_owner_mismatch_before_release` | `` | 1438 | AttributeError: 'Agent' object has no attribute 'suspend_preflight'. Did you mean: 'suspend_preflight_async'? | removed synchronous preflight call | [ ] |
| 4 | ERROR | `test_release_disconnects_after_three_total_attempts` | `` | 1149 | AttributeError: 'types.SimpleNamespace' object has no attribute 'get_is_connected' | incomplete fake session bus | [ ] |
| 5 | ERROR | `test_release_failure_schedules_exactly_one_five_second_source` | `` | 1121 | AttributeError: 'object' object has no attribute 'get_is_connected' | incomplete fake session bus | [ ] |
| 6 | ERROR | `test_release_retry_success_cancels_pending_state` | `` | 1133 | AttributeError: 'object' object has no attribute 'get_is_connected' | incomplete fake session bus | [ ] |
| 7 | ERROR | `test_uncertain_latch_blocks_failsafe_and_direct_preflight` | `` | 1352 | AttributeError: 'Agent' object has no attribute 'suspend_preflight'. Did you mean: 'suspend_preflight_async'? | removed synchronous preflight call | [ ] |
| 8 | FAIL | `test_accepted_request_with_true_and_missing_false_is_failed_preparation` | `` | 1262 | + failed-preparation | async observation mismatch; inspect callback trace | [ ] |
| 9 | FAIL | `test_accepted_request_without_signals_settles_without_stale_reason` | `` | 1240 | + idle | async observation mismatch; inspect callback trace | [ ] |
| 10 | FAIL | `test_ambiguous_errors_remain_pending_and_are_never_retried` | `name='org.freedesktop.DBus.Error.NoReply'` | 1486 | + unknown | async observation mismatch; inspect callback trace | [ ] |
| 11 | FAIL | `test_ambiguous_errors_remain_pending_and_are_never_retried` | `name='org.freedesktop.DBus.Error.Timeout'` | 1486 | + unknown | async observation mismatch; inspect callback trace | [ ] |
| 12 | FAIL | `test_ambiguous_errors_remain_pending_and_are_never_retried` | `name='org.freedesktop.DBus.Error.Disconnected'` | 1486 | + unknown | async observation mismatch; inspect callback trace | [ ] |
| 13 | FAIL | `test_ambiguous_errors_remain_pending_and_are_never_retried` | `name='com.example.Unknown'` | 1486 | + unknown | async observation mismatch; inspect callback trace | [ ] |
| 14 | FAIL | `test_clock_gap_waits_while_logind_still_reports_preparing` | `` | 1393 | + uncertain-blocked | async observation mismatch; inspect callback trace | [ ] |
| 15 | FAIL | `test_conditional_timer_fresh_lid_rejection_preserves_prevention` | `lid=False` | 1630 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 16 | FAIL | `test_conditional_timer_fresh_lid_rejection_preserves_prevention` | `lid=None` | 1630 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 17 | FAIL | `test_conditional_timer_lid_change_after_release_blocks_dispatch` | `moment='release', lid=False` | 1663 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 18 | FAIL | `test_conditional_timer_lid_change_after_release_blocks_dispatch` | `moment='release', lid=None` | 1663 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 19 | FAIL | `test_conditional_timer_lid_change_after_release_blocks_dispatch` | `moment='final-preflight', lid=False` | 1663 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 20 | FAIL | `test_conditional_timer_lid_change_after_release_blocks_dispatch` | `moment='final-preflight', lid=None` | 1663 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 21 | FAIL | `test_conditional_timer_prevention_loss_before_release_blocks_dispatch` | `` | 1682 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 22 | FAIL | `test_conditional_timer_stable_conditions_dispatch_once_after_release` | `` | 1701 | AssertionError: Expected 'mock' to have been called once. Called 0 times. | async observation mismatch; inspect callback trace | [ ] |
| 23 | FAIL | `test_each_definite_rejection_resolves_immediately` | `name='org.freedesktop.DBus.Error.AccessDenied'` | 1469 | + rejected | async observation mismatch; inspect callback trace | [ ] |
| 24 | FAIL | `test_each_definite_rejection_resolves_immediately` | `name='org.freedesktop.PolicyKit1.Error.NotAuthorized'` | 1469 | + rejected | async observation mismatch; inspect callback trace | [ ] |
| 25 | FAIL | `test_each_definite_rejection_resolves_immediately` | `name='org.freedesktop.login1.OperationInProgress'` | 1469 | + rejected | async observation mismatch; inspect callback trace | [ ] |
| 26 | FAIL | `test_each_definite_rejection_resolves_immediately` | `name='org.freedesktop.login1.SleepVerbNotSupported'` | 1469 | + rejected | async observation mismatch; inspect callback trace | [ ] |
| 27 | FAIL | `test_failsafe_last_power_refresh_events_precede_final_guard` | `event='shutdown'` | 1824 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 28 | FAIL | `test_failsafe_last_power_refresh_events_precede_final_guard` | `event='preparing'` | 1824 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 29 | FAIL | `test_failsafe_last_power_refresh_events_precede_final_guard` | `event='unknown-preparing'` | 1824 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 30 | FAIL | `test_failsafe_power_change_during_final_logind_read_blocks_dispatch` | `` | 1802 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 31 | FAIL | `test_failsafe_rechecks_power_after_inhibitor_release` | `attr='on_battery', value=False` | 1762 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 32 | FAIL | `test_failsafe_rechecks_power_after_inhibitor_release` | `attr='on_battery', value=None` | 1762 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 33 | FAIL | `test_failsafe_rechecks_power_after_inhibitor_release` | `attr='battery_discharge_state', value='unknown'` | 1762 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 34 | FAIL | `test_failsafe_rechecks_power_after_inhibitor_release` | `attr='battery_discharge_state', value='not-discharging'` | 1762 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 35 | FAIL | `test_failsafe_rechecks_power_after_inhibitor_release` | `attr='battery_percent', value=None` | 1762 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 36 | FAIL | `test_failsafe_rechecks_power_after_inhibitor_release` | `attr='battery_percent', value=20` | 1762 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 37 | FAIL | `test_failsafe_rechecks_power_after_inhibitor_release` | `attr='failsafe', value=False` | 1762 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 38 | FAIL | `test_failsafe_stable_power_dispatches_once_after_effective_release` | `` | 1779 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 39 | FAIL | `test_false_then_delayed_true_converges_through_one_transaction` | `` | 1317 | + idle | async observation mismatch; inspect callback trace | [ ] |
| 40 | FAIL | `test_false_without_true_settles_conservatively_and_never_suspends` | `` | 1291 | + uncertain-preparation | async observation mismatch; inspect callback trace | [ ] |
| 41 | FAIL | `test_later_false_resolves_unreadable_latch_after_settle` | `` | 1408 | + idle | async observation mismatch; inspect callback trace | [ ] |
| 42 | FAIL | `test_low_battery_episode_never_retries_ambiguous_suspend` | `` | 1512 | AssertionError: 0 != 1 | async observation mismatch; inspect callback trace | [ ] |
| 43 | FAIL | `test_missing_percentage_preserves_episode_and_fresh_unknown_blocks_dispatch` | `` | 1997 | AssertionError: False is not true | async observation mismatch; inspect callback trace | [ ] |
| 44 | FAIL | `test_preflight_reads_cannot_authorize_changed_lifecycle` | `read='property', change='shutdown'` | 1740 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 45 | FAIL | `test_preflight_reads_cannot_authorize_changed_lifecycle` | `read='property', change='transaction'` | 1740 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 46 | FAIL | `test_preflight_reads_cannot_authorize_changed_lifecycle` | `read='property', change='generation'` | 1740 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 47 | FAIL | `test_preflight_reads_cannot_authorize_changed_lifecycle` | `read='owner', change='shutdown'` | 1740 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 48 | FAIL | `test_preflight_reads_cannot_authorize_changed_lifecycle` | `read='owner', change='transaction'` | 1740 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 49 | FAIL | `test_preflight_reads_cannot_authorize_changed_lifecycle` | `read='owner', change='generation'` | 1740 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 50 | FAIL | `test_preflight_reads_cannot_authorize_changed_lifecycle` | `read='owner', change='cookie'` | 1740 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 51 | FAIL | `test_proven_suspend_consumes_overdue_active_timer_without_resuspend` | `` | 1853 | AssertionError: 5 is not None | async observation mismatch; inspect callback trace | [ ] |
| 52 | FAIL | `test_reentrant_prepare_enter_is_not_overwritten_by_method_reply` | `` | 1608 | + preparing | async observation mismatch; inspect callback trace | [ ] |
| 53 | FAIL | `test_reentrant_wake_resolution_is_not_resurrected_by_method_reply` | `` | 1597 | + proven-resume | async observation mismatch; inspect callback trace | [ ] |
| 54 | FAIL | `test_repeated_false_after_resolution_is_idempotent` | `` | 1302 | + idle | async observation mismatch; inspect callback trace | [ ] |
| 55 | FAIL | `test_state_change_between_release_and_final_guard_prevents_suspend` | `` | 1453 | AssertionError: True is not false | async observation mismatch; inspect callback trace | [ ] |
| 56 | FAIL | `test_unknown_low_battery_sequences_cannot_dispatch_twice` | `ambiguous=0` | 1960 | AssertionError: Expected 'mock' to have been called once. Called 0 times. | async observation mismatch; inspect callback trace | [ ] |
| 57 | FAIL | `test_unknown_low_battery_sequences_cannot_dispatch_twice` | `ambiguous=6` | 1960 | AssertionError: Expected 'mock' to have been called once. Called 0 times. | async observation mismatch; inspect callback trace | [ ] |
| 58 | FAIL | `test_unknown_low_battery_sequences_cannot_dispatch_twice` | `ambiguous=[]` | 1960 | AssertionError: Expected 'mock' to have been called once. Called 0 times. | async observation mismatch; inspect callback trace | [ ] |
| 59 | FAIL | `test_unknown_low_battery_sequences_cannot_dispatch_twice` | `ambiguous=FakeDBusException('UPower gone')` | 1960 | AssertionError: Expected 'mock' to have been called once. Called 0 times. | async observation mismatch; inspect callback trace | [ ] |
| 60 | FAIL | `test_unknown_reply_with_false_and_no_gap_stays_unknown_not_rejected` | `` | 1554 | + idle | async observation mismatch; inspect callback trace | [ ] |

## Resolution rules

For each row: identify the original safety invariant; drive or reason through queued callbacks, absolute deadline, and owner generation; update only obsolete observation points; repair production code for a real invariant violation; record two review passes in `../task.md`. Do not claim full-suite pass while execution remains prohibited.

## Case-to-invariant crosswalk (source review, not behavior resolution)

The test line and immediate failed assertion for each numbered variant are in the inventory above. The groups below assign every row an invariant and a proposed repair or observation point. Rows 1, 3, and 7 are demonstrably obsolete *calls* to the removed synchronous method; rows 2, 4, 5, and 6 are demonstrably incomplete bus fakes. The 53 assertion failures remain **unresolved** as to final cause: their old synchronous observation points are suspect, but a genuine runtime defect is still possible until the queued behavior cases can be run. A proposed fixture migration never changes the safety invariant.

| Rows | Preserved invariant | Proposed repair / current observation point |
| --- | --- | --- |
| 1 | A logind replacement requires a fresh readable owner/property/capability preflight. | Drive `suspend_preflight_async` with queued owner/property/capability replies; reject stale owner before release or request. |
| 2 | Owner loss cancels a pending release retry without leaking the cookie boundary. | Supply a connected fake session bus and fire owner-loss before the queued release callback. |
| 3 | A cookie held by a different GNOME owner cannot authorize release. | Use callback preflight and deliver the mismatched `GetNameOwner` reply explicitly. |
| 4 | Three total failed release attempts end with a session-bus disconnect. | Give the fake `get_is_connected`, queue each owner/`Uninhibit` result, and advance retry sources exactly twice. |
| 5 | One failed release creates exactly one five-second retry source. | Use a connected fake and observe the agent source ID after the asynchronous owner and release failures. |
| 6 | A successful retry clears pending release and cancels further work. | Deliver the second `Uninhibit` success and check cookie/source state and no late retry. |
| 7 | An uncertain sleep latch blocks failsafe and direct preflight. | Invoke callback preflight under the latch; retain no-release and no-`Suspend` assertions. |
| 8–9 | Accepted request plus preparation signals or their absence settles once to the conservative outcome. | Drain owner → `PreparingForSleep` → final-owner reads before asserting `failed-preparation` or `accepted-no-suspend`. |
| 10–13 | Every ambiguous D-Bus error variant leaves the request unknown and cannot trigger a second automatic request. | Complete the async attempt prerequisites, deliver the named error, then queued reconciliation reads; assert one dispatch and an unknown latch for each error name. |
| 14 | A clock gap does not resolve while logind still says it is preparing. | Deliver a queued `PreparingForSleep=true` read before inspecting the transaction phase. |
| 15–16 | A false or unknown fresh lid value rejects a lid-required timer before dispatch. | Observe staged attempt separately from `Suspend` call count after the fresh power callback. |
| 17–20 | A lid change during release or final preflight blocks dispatch. | Inject each listed lid value at its named callback boundary; assert no `Suspend` after the final guard. |
| 21 | Lost effective prevention before release blocks a prevention-required timer. | Mutate `enabled` before the initial preflight continuation, then inspect release/dispatch calls. |
| 22 | Stable timer conditions dispatch exactly once after confirmed release. | Advance first preflight, release, and final preflight callbacks before asserting the one request. |
| 23–26 | Each named definite rejection resolves as rejected, with no retry. | Deliver each named `Suspend` error after dispatch and assert the transaction result and one call. |
| 27–29 | Shutdown, confirmed preparation, and unreadable preparation each block the final failsafe guard. | Inject the event before the queued final owner/property read; preserve distinct rejection reasons and no dispatch. |
| 30 | A power change during the final logind read blocks a failsafe request. | Mutate power while the final preflight callback is queued; inspect the final power guard and dispatch count. |
| 31–37 | Each listed unsafe or unknown post-release battery/failsafe value blocks dispatch. | Deliver a fresh post-release power snapshot with that exact value; assert no `Suspend` for every variant. |
| 38 | Stable failsafe power dispatches only once after effective release. | Advance post-release power and final preflight explicitly, then assert one request and a consumed episode. |
| 39–40 | False/true signal order converges in one transaction; false alone settles conservatively. | Queue owner/property/final-owner reads before the signal, deliver them in order, and check final classification. |
| 41 | A later false can settle an unreadable latch after the settle interval. | Deliver an unreadable read followed by a later queued false; keep the no-retry assertion. |
| 42 | A low-battery episode cannot retry after an ambiguous suspend reply. | Drive both async failsafe preflights, fresh power, dispatch, error, and reconciliation before asserting one request. |
| 43 | Missing percentage preserves episode state; fresh unknown evidence blocks dispatch. | Deliver the fresh power callback with missing percentage and inspect episode/dispatch separately. |
| 44–50 | Shutdown, transaction, owner-generation, or cookie mutation during either named preflight read invalidates authorization. | Change state immediately before the queued owner/property callback; require no release and no request for every variant. |
| 51 | Proven external suspend consumes an overdue active timer without a second request. | Install the queued reconciliation bus before tick, deliver its reads, then inspect deadline and request count. |
| 52–53 | A reentrant prepare or wake signal cannot be overwritten or resurrected by a later method reply. | Deliver the signal during the request callback path, then the late reply; assert single transaction identity. |
| 54 | Repeated false after resolution is idempotent. | Complete the first queued reconciliation, deliver another false, and assert unchanged terminal outcome. |
| 55 | Any state change between release and the final guard prevents dispatch. | Inject the change at the final preflight boundary and assert no `Suspend`. |
| 56–59 | Every listed ambiguous low-battery evidence sequence consumes one episode and dispatches at most once. | Drive asynchronous prerequisite callbacks for each exact variant; check first request and no repeated dispatch. |
| 60 | Unknown request plus false and no clock gap stays unknown, never definite rejection. | Drain the queued logind reads after the ambiguous reply and retain unknown classification. |

This crosswalk covers rows 1–60 exactly once. It is a source-level migration map; it does not check any `Resolved` box or establish a full-suite pass.

Initial source migration: rows 1, 3, and 7 now call `suspend_preflight_async`, with a fake callback-only proxy for the readable/owner-mismatch cases. Rows 2, 5, and 6 now inherit a session-bus fake with `get_is_connected`; row 4's explicit bus fake has that method too. Rows 4–6 additionally drive the actual async owner lookup and `Uninhibit` callbacks through a pinned fake GNOME owner, retaining the one-source, two-attempt success, and three-attempt disconnect checks. The code compiles, but these seven cases remain unchecked because the current user instruction prohibits executing sleep-functionality tests. Their original behavioral assertions still need the full callback and retry review.

Rows 8–9 now use a queued fake logind owner → `PreparingForSleep` → final-owner read and keep their accepted-without-suspend/failed-preparation outcomes. The queue is explicitly drained by the fixture; it is never connected to live D-Bus. These rows remain unchecked until the prohibited behavior can be run or otherwise proven through the complete source audit.

Rows 14, 41, and 60 also use that queued read. They retain the original safety claims: a proven gap waits while logind reports preparing, a later false settles an unreadable latch, and an ambiguous request plus false is never recast as a definite rejection. These are source migrations only; no prohibited case was executed.

Rows 15–22, 23–26, 52–53, and 55 have initial callback-path source migrations. The timer cases now distinguish accepted attempt staging from eventual `Suspend(false)` dispatch and observe fresh power, first/final preflight, and release callbacks. The definite-rejection and reentrant method-reply cases route through the same fake prerequisites. Assertions for no dispatch, exactly one dispatch, and request-local outcome remain. These rows are **not resolved** until each full callback trace and prohibited behavior test can be validated.

Rows 27–38 have initial callback-path source migrations for fresh failsafe power, release-time changes, final-preflight changes, and shutdown/preparation changes before the final guard. Their original no-dispatch or one-dispatch assertions remain. The `preparing` and unreadable variants were initially modeled by injected rejected preflight results; the follow-up below replaces that shortcut with a real queued property read. No case was executed.

Follow-up source migration for rows 27–29: the last-power-refresh fixture now invokes the real final `suspend_preflight_async` after a fake successful initial pass. A queued fake login1 owner reply then delivers `PreparingForSleep=true` or an unreadable value to the production parser, and the test checks the corresponding rejection reason and absence of any later proxy call. The shutdown variant still stops before the final preflight. This closes the fixture-equivalence concern by source inspection only; these rows remain unchecked until behavior execution is permitted.

Rows 10–13, 42–43, and 56–59 have initial source migrations for the asynchronous failsafe admission and ambiguous-reply episode. The fake failsafe evaluation now supplies both preflight callbacks and fresh power, and the ambiguous request settles through a queued logind read. The unknown outcome and no-repeat assertions remain. The initial fake callbacks still need a queued-order review; behavior execution remains prohibited.

Rows 44–50 now invoke the real `suspend_preflight_async` with fake login1/GNOME owner and property callbacks. Each variant mutates shutdown, transaction, owner generation, or cookie state immediately before the selected callback and retains the no-release/no-`Suspend` assertion. This is a source-only migration; callback timing and all seven variants still need full validation.

Rows 39–40, 51, and 54 now install the queued owner/property/final-owner bus **before** the triggering signal or tick, then drain it explicitly. This preserves the initial unresolved phase, the later settle, and overdue timer consumption without asserting that no logind read occurs. The original no-resuspend and classification checks remain; the cases are unexecuted.

## Structural selection audit, 2026-10-09

AST inspection of `tests/test_agent.py` finds 211 uniquely named `AgentTests.test_*` methods. The historical 206-case result predates five added regressions. Nine current agent methods have been executed in separately selected nonscheduling runs, all passing: the two notification-only cases plus `test_upower_property_evidence_rejects_malformed_values`, `test_brightness_async_read_validates_reply_and_uses_short_timeout`, `test_brightness_sync_read_rejects_coercible_malformed_types`, `test_state_directory_requires_absolute_path_and_empty_uses_default`, `test_post_replace_settings_failure_reconciles_memory`, `test_pre_replace_settings_failure_keeps_memory`, and `test_atomic_write_exposes_commit_and_durability_at_each_failure`. The seven newly selected methods use the fake agent constructed with `__new__`, a temporary directory, and mocked remote brightness calls; they validate conversion or local persistence and do not invoke timers or sleep paths. The other 202 methods remain **unselected**, including three new diagnostic regressions for acquisition, timer start, and accepted suspend reply. This is conservative: some cover pure data conversion, installer-adjacent state, or brightness recovery and could be selected later after a case-by-case side-effect audit; the timer, failsafe, preflight, suspend, wake, lid-close, and scheduling cases are prohibited now. This count is a structural inventory, not a test result or a claim that all 202 unselected methods exercise sleep.

Source trace for rows 44–50: `sleep_now` stages the attempt, initial `suspend_preflight_async` snapshots login/session owner generations, cookie identity/state, and `sleep_tx.phase`. The test mutates one of these before the property or GNOME owner callback; `current()` rejects it before calling the first-preflight continuation. The staged `sleep_now` return is therefore expected to be true while `release_async` and `Suspend(false)` remain uncalled. This preserves the old no-dispatch invariant. A queued-callback behavior run is still required to prove fixture execution.

Follow-up for rows 44–50: owner and property replies are now queued until the fixture explicitly delivers them. It asserts one pending read immediately after admission and exactly two proxy calls for a property-stage mutation or three for a GNOME-owner-stage mutation. The mutation therefore occurs across a real pending callback boundary in the fake rather than inline inside the initiating call. The no-release/no-dispatch assertions remain. No behavior case was run.

Source trace for rows 4–6: `release_async` calls `attempt_session_release_async`, which pins the issuing unique GNOME owner, reads its current owner, then invokes `Uninhibit` with a seven-second operation deadline. A timeout marks release pending and installs one five-second retry source; a second successful reply clears the cookie, while the third failure closes the session-bus boundary. The fake now supplies `get_is_connected` and both callbacks. The tests still manually invoke the retry callback; the fake GLib source map does not automatically remove a fired source, so its source count is not faithful after a manual firing. The assertions inspect the agent's retry-source identifier and call counts instead. Behavior remains unverified.
