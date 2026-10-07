# Plan 0.4 — GNOME Wayland sleep transaction safety and lifecycle completion

Status: **implemented in local source and deterministic harnesses; live GNOME and hardware validation remains open**. Scope: fix every defect found by the post-plan-0.3 source review in one coordinated change to `linux/gnome-wayland/`. Preserve the per-user architecture, conservative inhibitor ownership rules, crash-safe brightness journal, and the open live GNOME and hardware release gates.

This plan follows [plan 0.3](plan0.3.md). Its central change is to replace loosely related sleep booleans with one explicit sleep transaction and to route every deliberate suspend request, wake reconciliation, and brightness recovery event through shared guards.

## 1. Constraints and invariants

The implementation must preserve these facts:

1. A logind `PrepareForSleep(false)` signal means sleep preparation ended; it does not by itself prove that the system suspended. A sufficient increase in `CLOCK_BOOTTIME - CLOCK_MONOTONIC` remains the proof of actual suspend. This is valid for both `s2idle` and `[deep]` because boottime includes suspended time and monotonic time does not.
2. `PreparingForSleep=true` is an active safety condition. A local reporting timeout may cancel a timer and publish an uncertain outcome, but it must not make the agent treat logind as safely awake.
3. A successful or ambiguously failed `Suspend(false)` call is a transaction that needs reconciliation. The agent must retain the request reason, start time, clock baseline, and reply classification until clock, signal, property, or logind-owner evidence resolves it.
4. A D-Bus transport error can arrive after the remote method ran. Only errors with verified rejection semantics may be called a refusal. `NoReply`, timeout, disconnect, and unrecognized errors are unknown outcomes and must never cause an automatic retry.
5. There is exactly one final guard immediately before login1 `Suspend(false)`. Every caller, including the timer and battery failsafe, must pass through it. The guard rejects the request while a previous sleep transaction is unresolved, logind reports preparation, logind state is unreadable, or this app's GNOME inhibitor release is unresolved.
6. A bounded user-visible uncertainty period and a suspend safety latch are separate concepts. The first prevents timers from hanging forever; the second remains until positive evidence makes another suspend request safe.
7. Brightness recovery must retain all plan-0.3 identity and journal checks. Reapplying closed-lid dimming must never bypass persisted-record validation or overwrite a detected manual brightness change.
8. Timer state and sleep transaction state are independent. Sleep reconciliation must not report a nonexistent timer as canceled.

## 2. P0 — Replace the sleep booleans with one transaction

### 2.1 Define the state model

Introduce one internal sleep transaction object or an equivalent group whose transitions are controlled by shared methods. It must contain at least:

- `phase`: `idle`, `requesting`, `request-pending`, `preparing`, `reconciling`, or `uncertain-blocked`;
- `origin`: `timer`, `failsafe`, or `external`;
- `reason`: the user-facing reason for an app request;
- `requested_at`: monotonic time for a dispatched app request;
- `request_reply`: `none`, `accepted`, `rejected`, or `unknown`;
- `saw_prepare_true` and `saw_prepare_false`;
- `gap_baseline`: `CLOCK_BOOTTIME - CLOCK_MONOTONIC` captured before dispatch or on the first enter signal;
- `uncertain_since`: monotonic time used for reporting deadlines;
- the logind unique-owner generation under which the transaction began.

Remove `sleeping`, `resume_pending`, `resume_pending_since`, `sleep_started`, `sleep_gap_baseline`, and `last_sleep_reason`, or make them read-only derived values during a short migration. No caller may independently clear a subset of the transaction.

Expose a concise `sleepPhase`, `sleepOutcome`, and `suspendRequestOutcome` through `GetState`. Do not expose raw timestamps. Keep `timerPhase` exclusively about an actual countdown.

### 2.2 Centralize transitions

Add small transition helpers rather than mutating transaction fields throughout the agent:

1. `begin_sleep_request(origin, reason)` captures both clocks, owner generation, and request time before the D-Bus call.
2. `observe_prepare_enter()` records the true signal without replacing an earlier request baseline or reason. A duplicate true signal is idempotent.
3. `observe_prepare_exit()` records the false signal and invokes reconciliation. A false signal without a prior true creates a conservative external transaction long enough to classify the evidence.
4. `reconcile_sleep_state(trigger)` reads the two clocks and `PreparingForSleep`, classifies the evidence, handles the uncertainty deadline, and is the only normal path back to `idle`.
5. `resolve_sleep_transaction(classification)` performs timer reconciliation, brightness/power reconciliation, notification, state cleanup, and publication exactly once.
6. `mark_sleep_uncertain_blocked(outcome)` cancels an affected overdue timer and publishes the bounded uncertain outcome while retaining the safety latch.

All signal, one-second tick, 15-second maintenance, logind owner-change, and deliberate-request completion paths must use these helpers.

### 2.3 Define complete reconciliation rules

Apply the following precedence in one place:

1. If the clock-gap delta is at least `SUSPEND_GAP_TOLERANCE`, classify a proven suspend after logind no longer reports active preparation. If the property is unreadable after wake evidence, resolve as proven suspend because the clock evidence is independent.
2. If `PreparingForSleep` is false and a true signal or app request exists but there is no clock gap, wait a short documented settling interval, then classify failed or uncertain preparation. This interval covers signal/property ordering without leaving the transaction indefinitely pending.
3. If `PreparingForSleep` remains true beyond 30 seconds of awake monotonic time, cancel any affected countdown with an uncertain-preparation outcome and transition to `uncertain-blocked`. Keep the safety latch set and continue polling. Do not clear the clock baseline or request evidence.
4. If `PreparingForSleep` is unreadable without clock evidence, transition to `uncertain-blocked` after the reporting deadline and retain the latch. Resolve only when the property becomes false, clock evidence appears, or the owning logind process is proven gone/replaced.
5. On logind owner loss/replacement, terminate the transaction conservatively, record an owner-loss outcome, refresh logind state under the new owner, and require a later fresh guard check before any new suspend request.
6. Missing true, missing false, missing both signals, duplicate true, false without true, and reordered callbacks must all converge through the same rules. None may issue a second suspend as part of reconciliation.

The transaction may remain safety-blocked longer than 30 seconds. The timer and UI outcome are bounded; permission to send another suspend request depends on positive state evidence.

## 3. P0 — Guard every deliberate suspend request

### 3.1 Add one final preflight

Create `suspend_preflight()` and call it inside `sleep_now()` immediately before releasing inhibitors or consuming irreversible state. It must:

1. Reject when the sleep transaction phase is anything other than `idle`.
2. Read login1 `PreparingForSleep` freshly. Require an explicit false value; true and unreadable both block the request.
3. Before release, allow `absent` or a definitely held cookie tied to the current SessionManager owner; reject `release-pending`, owner mismatch, and every unknown cookie state. After release, require `absent` before login1 is called.
4. Record a precise outcome without calling login1. Timer and failsafe callers must not implement weaker copies of this logic.

Recheck the transaction and logind state after inhibitor release and immediately before `Suspend(false)`, because release and D-Bus calls can dispatch other events. If state changed, consume the one-shot timer trigger when required, publish why suspend was not sent, and do not retry automatically.

`check_failsafe()` must test the same preflight both before and after its fresh UPower read. A UPower signal or periodic reconciliation while `uncertain-blocked` must result in zero `Suspend` calls.

### 3.2 Track the request before dispatch

Immediately before calling login1:

1. Create the transaction with origin, reason, clock baseline, monotonic request time, and logind owner generation.
2. Mark the timer trigger consumed, where applicable, before dispatch so callback reentrancy cannot send it twice.
3. On a normal reply, set `request_reply=accepted` and keep the transaction pending until reconciliation resolves it.
4. Do not clear the reason merely because the method returned. A normal reply with no signals and no clock gap must resolve after the settling interval as an accepted request with no observed suspend.

This closes the current no-signal/no-gap hole and prevents an unrelated future suspend from inheriting a stale reason.

### 3.3 Separate rejection from unknown delivery

Add a classifier for `Suspend(false)` exceptions:

- Maintain a small allowlist of error names whose login1/systemd semantics prove that suspend was rejected before it began. Verify this list against the target systemd API/source and cover every allowed name with a test.
- Treat `org.freedesktop.DBus.Error.NoReply`, timeout, disconnect, owner loss during the call, and every unrecognized error as `request_reply=unknown`.
- A proven rejection resolves the transaction as `rejected`, preserves the precise timer/failsafe outcome, and never waits for a false signal.
- An unknown result keeps the transaction and safety latch. It is reconciled from clock, signal, property, and owner evidence exactly like an accepted result.
- Never automatically repeat an unknown request. If it later produces clock evidence, report a real suspend. If it settles with no clock evidence, report an uncertain request outcome rather than “refused.”

Notifications and `timerOutcome` must distinguish `request rejected`, `request outcome unknown`, `accepted but no suspend observed`, and `resumed after proven suspend`.

## 4. P0 — Make wake and brightness desired state agree

### 4.1 Separate journal loading, restoration, and desired-state application

Refactor `recover_brightness()` so loading a valid journal does not unconditionally restore it. Add one coordinator such as `reconcile_brightness(trigger)` that decides whether the current desired state is dimmed or restored and then calls the existing validated write paths.

The desired dim condition remains:

```
enabled and lid_dimming and lid_closed is True
```

The coordinator must return explicit results such as `unchanged`, `restored`, `redimmed`, `manual-change-preserved`, `pending`, or `invalid`. Callers use the result rather than guessing from `brightness_record is None`.

Use this decision table so periodic recovery cannot accidentally become an unconditional brightness writer:

| Journal and desired state | Action |
| --- | --- |
| Valid journal, restored brightness desired | Validate persisted record, machine, adapter/output, and current value; restore only on a match. |
| Valid journal, dimmed brightness desired, wake or owner-return trigger | Validate and restore, then create a fresh journal and redim only after successful restoration. |
| Valid journal, dimmed brightness desired, ordinary periodic trigger | Retry a pending failed operation; do not cycle a healthy applied dim through restore/redim every 15 seconds. |
| No journal, dimmed brightness desired, eligible state transition | Dim once on lid close, explicit enable, startup reconciliation, or successful post-wake restoration. |
| No journal, dimmed brightness desired, ordinary periodic trigger | Do nothing unless an explicitly recorded retry is pending. |
| Manual/current-value conflict | Preserve the current value and suppress redim until a new lid-close transition or explicit dimming re-enable. |
| Invalid/unverifiable journal or unavailable adapter | Make no brightness write, retain the record and diagnostic, and retry only according to the existing recovery policy. |

### 4.2 Apply ordering on wake

For every resolved wake or failed preparation:

1. Refresh UPower/lid state.
2. Refresh the brightness adapter and load any journal without writing.
3. Run validated restoration where the existing recovery contract requires it.
4. If restoration succeeded and the current desired condition still requires a closed-lid dim, create a fresh validated journal and dim again before publishing final state.
5. If restoration detects a manual/current-value conflict, preserve that value, clear or retain the journal according to the existing verified conflict rules, and suppress redimming for that event.
6. If validation or a write fails, keep the journal and do not attempt a second brightness write through a different path.

This ordering guarantees that an ordinary still-closed wake ends dimmed while retaining the plan-0.3 rule that a proven manual change is not overwritten.

### 4.3 Use the same coordinator for every trigger

Route these events through `reconcile_brightness(trigger)`:

- lid close and lid open;
- dimming enable/disable;
- prevention enable/disable;
- proven wake, failed preparation, and uncertain reconciliation completion;
- agent startup after lid state is known;
- brightness-service owner loss and return;
- periodic recovery;
- clean service stop.

On brightness-service return while the lid is still closed, do not leave the panel restored. Resolve the old journal through the shared validation gate and reapply dimming only after a verified restoration or a clearly safe no-record state. Preserve manual-conflict suppression across periodic reconciliation so the next 15-second callback cannot overwrite the value that was just preserved. A new lid-close transition or explicit dimming re-enable may begin a new dim attempt.

Reorder startup so lid/power state is known before desired brightness state is applied. Retain the invariant that unsupported GNOME 49–50 brightness APIs do not affect main sleep prevention.

## 5. P1 — Make retry and diagnostic timing truthful

### 5.1 Give GNOME release retry its own scheduler

Do not depend on the 15-second maintenance callback for a five-second retry constant.

1. When `mark_release_pending()` runs, schedule a one-shot GLib timeout for `RELEASE_RETRY_SECONDS` and retain its source ID.
2. At the callback, verify the cookie state and owner generation again, attempt exactly one release, and either resolve, schedule the next retry, or disconnect after `RELEASE_RETRY_LIMIT` total attempts.
3. Cancel the timeout on successful release, owner loss, session-bus disconnect, and clean shutdown.
4. Keep periodic reconciliation as a defensive backstop, but it must not add retries earlier than the dedicated deadline or create duplicate scheduled callbacks.
5. Document the exact bound. With five-second spacing and three total attempts including the initial attempt, fallback occurs around ten seconds after the first ambiguous result, plus event-loop scheduling delay.

Publish attempt count and next-action state only if they materially help diagnostics; do not expose misleading exact countdown values.

### 5.2 Keep timer phase semantically valid

Define the permitted timer transitions independently of sleep state:

- `idle -> running` on `StartTimer`;
- `running -> consumed` before its one deliberate request;
- `running -> canceled` only when a real active countdown is canceled;
- `consumed` and `canceled -> running` only through a new `StartTimer`;
- startup with no persisted timer is `idle`.

The sleep uncertainty path may set a timer outcome and `canceled` only if a deadline existed. If no timer existed, leave `timerPhase` unchanged. `CancelTimer` with no active timer must be idempotent and must not invent a past countdown.

## 6. P1 — Complete panel connection-state handling

Replace the boolean helper's mixed meanings with an explicit local connection state: `disconnected`, `connecting`, or `connected`.

1. `enable()` and name disappearance set `disconnected`, show the warning icon, and disable every action.
2. Name appearance sets `connecting`, disables actions, subscribes to signals, and requests `GetState`.
3. A successful authoritative `GetState` calls the connected-state path before rendering per-action sensitivity.
4. A valid `StateChanged` received before the initial reply may also establish connected state for the current generation.
5. Call failure returns to disconnected state while preserving a visible action/connection error.
6. Owner-generation checks and cancellables must continue preventing stale callbacks from enabling controls after disconnect or extension disable.
7. `_render()` may decide feature-specific sensitivity only after the extension is connected. Dimming and cancel controls retain their existing capability/deadline rules.

Extend the panel harness to exercise disconnected → connecting → connected → disconnected transitions, a stale prior-generation reply, initial `GetState` failure, early `StateChanged`, and action failure followed by authoritative refresh. Live Shell validation remains mandatory because the Node harness cannot prove GNOME lifecycle compatibility.

## 7. P1 — Make extension installation recoverable

Change the extension part of `install.sh` into a staged transaction while leaving the already validated agent and CLI usable if panel installation fails.

1. Capture whether the extension was installed, enabled, and active before changing it.
2. Copy new extension files to a staging directory and validate metadata JSON, UUID, required files, and any available non-live syntax/static checks before touching the active directory.
3. Preserve the old extension directory as a rollback copy, then switch the staged directory into the canonical UUID path. Do not discard the rollback copy until the new extension has passed enable and enabled-list checks.
4. Disable/re-enable only when required to load changed files. Do not disable an existing working extension before staging and validation succeed.
5. If enable, discovery, enabled-list, or active-list verification fails, restore the old directory and its previous enabled state. Report separately whether rollback succeeded and whether a logout is needed for Shell discovery.
6. If no previous extension existed, retain the failed staged/new installation for diagnosis or move it aside safely; do not claim rollback to a working panel.
7. Keep exit status `2` for panel-only failure and confirm in the message that the user service and CLI remain available.

The implementation must follow repository filesystem policy and must not use the `rm` command. Use safe staging/rename/rollback operations and the repository-approved disposal mechanism where cleanup is required.

## 8. Deterministic test matrix

### 8.1 Sleep transaction and final guard

Add tests for all of the following, asserting phase, outcome, timer state, retained/cleared reason, and exact `Suspend` call count:

1. Accepted request, true/false signals, and a qualifying clock gap: one request and one proven resume notification.
2. Accepted request with neither signal, no gap, and `PreparingForSleep=false`: bounded accepted-but-no-suspend resolution and no stale reason.
3. Accepted request with neither signal and a later clock gap: proven suspend with no second request.
4. Accepted request with true but missing false: resolve when the property becomes false.
5. True signal with `PreparingForSleep=true` beyond 30 seconds: timer canceled if present, phase `uncertain-blocked`, safety latch retained.
6. Run both periodic reconcile and UPower callbacks in that state with the failsafe armed: zero suspend requests.
7. Transition the property from true to false after the reporting deadline: exactly one resolution and no request during the transition.
8. Property read failures before and after the deadline: no suspend until false, clock evidence, or owner change resolves safety.
9. Missing both signals with property read failure followed by logind owner replacement: conservative owner-loss resolution and a fresh preflight requirement.
10. Duplicate true, false without true, false before delayed callbacks, and repeated false: idempotent resolution.
11. StartTimer and SetFailsafe attempts while preparation is true or unreadable: precise rejection/deferral and zero suspend calls.
12. A state change dispatched between inhibitor release and the final pre-call guard: no suspend call.

### 8.2 Suspend reply classification

Test:

1. Every allowlisted definite rejection error.
2. `NoReply`, timeout, disconnect, and an unknown custom error.
3. Unknown reply followed by a clock gap.
4. Unknown reply followed by false/no gap.
5. Unknown reply with both signals absent and property false after settling.
6. Unknown reply that stays property-unknown beyond the reporting deadline.

Assert that no unknown outcome is automatically retried or labeled refused, and that later evidence can still classify an actual suspend.

### 8.3 Brightness desired-state matrix

Cover:

1. Wake with lid closed, prevention enabled, dimming enabled, and a valid applied journal: final brightness is dimmed and the replacement journal remains valid.
2. The same wake with the lid open: one verified restoration and no redim.
3. Brightness service loss/return while still closed: verified restoration/reapplication ends dimmed.
4. Service return after lid opened: restore only.
5. Manual brightness conflict during wake or service recovery: preserve the manual value and suppress periodic redim.
6. Machine/output/persisted-record mismatch during the restore/redim sequence: no unauthorized write and retained diagnostic record.
7. Restore succeeds but redim write or applied-journal update fails: the journal remains sufficient for recovery.
8. Startup with a closed lid: power state is read first and final desired brightness is applied once.
9. GNOME 49–50/no adapter: no brightness writes and prevention remains usable.

### 8.4 Retry, timer, panel, and installer

Add deterministic coverage for:

- release attempts at the dedicated five-second deadlines, no duplicate GLib sources, cancellation on resolution/owner loss/stop, and fallback after the documented number of attempts;
- uncertainty with no timer leaving `timerPhase` unchanged;
- canceling a real timer versus an idempotent no-timer cancel;
- every panel connection transition and stale callback listed in §6;
- installer staging validation, upgrade success, enable failure with successful rollback, rollback failure reporting, first-install panel failure, and preservation of the service/CLI result.

Prefer extracting small pure shell helpers or using a temporary HOME and command stubs for installer tests. Tests must not interact with the developer's actual GNOME profile or user service.

## 9. Implementation sequence for one coordinated change

1. Replace `task.md` with a persistent plan-0.4 implementation ledger before editing implementation files. Include every section below, two independent review passes, and the live gates; do not clear it after completion.
2. Establish the new sleep transaction model and convert signals/tick to shared transitions without changing brightness, panel, or installer behavior yet.
3. Add the final suspend preflight and request reply classifier. Convert timer and failsafe callers, then add the entire sleep matrix before continuing.
4. Refactor brightness loading and desired-state reconciliation. Convert every listed trigger and add the brightness matrix.
5. Add the dedicated release scheduler and correct timer phase transitions.
6. Implement the three-state extension connection lifecycle and expand the panel harness.
7. Implement staged extension installation and its isolated command-stub tests.
8. Update `README.md`, `task.md`, and `linux/research_checklist.md` so they describe actual behavior and keep all unperformed live checks open.
9. Run the focused tests after each area, then the full deterministic suite and static checks once. Do not broaden execution beyond what is needed to establish the acceptance matrix.
10. Perform two source review passes after tests pass:
    - Pass 1 traces every state transition and all call sites for `Suspend`, `PreparingForSleep`, brightness writes, release retry scheduling, panel sensitivity, and extension directory replacement.
    - Pass 2 starts at installer/service startup and follows panel/CLI → agent → inhibitors → timer/failsafe → sleep signals → wake/brightness → owner changes → stop/restart. Map every coverage box in §12 to a test or an explicit live gate.

## 10. File map

| File | Required change |
| --- | --- |
| `agent.py` | Explicit sleep transaction; bounded reporting plus persistent safety latch; final suspend preflight; accepted/rejected/unknown request outcomes; desired-state brightness coordinator; dedicated GNOME release retry scheduler; truthful timer phase. |
| `extension/sleep-disabler@local/extension.js` | Disconnected/connecting/connected lifecycle and authoritative action sensitivity. |
| `install.sh` | Stage, validate, switch, verify, and roll back extension updates without disabling a working copy prematurely. |
| `ctl.py` | Continue generic state output; add wording only if needed for the new sleep/request fields and action errors. |
| `tests/test_agent.py` | Full transaction, failsafe guard, ambiguous reply, brightness wake/reconnect, retry timing, and timer-phase matrices. |
| `tests/test_extension.mjs` | Connection lifecycle, stale callbacks, and action-error behavior. |
| installer test file or harness | Isolated command-stub tests for staging and rollback without touching a real session. |
| `README.md` | Document new outcomes, persistent uncertainty guard, retry bound, brightness wake behavior, rollback behavior, and remaining live limits. |
| `task.md`, `linux/research_checklist.md` | Persistent implementation evidence, acceptance mapping, and unverified live gates. |

## 11. Live validation gates

These cannot be closed by deterministic tests or source review:

1. In a disposable GNOME Wayland session on supported Shell versions, verify the extension's disconnected/connecting/connected states, reload, owner replacement, action rollback, disable/enable, staged upgrade, and installer rollback.
2. Inspect GNOME and logind inhibitor state after normal off, service stop, ambiguous-release fallback, agent restart, SessionManager replacement, and logind replacement.
3. On Debian, Ubuntu, Linux Mint, and Kali where available, verify user `graphical-session.target`, session environment, dependencies, extension discovery, logout/login, and restart behavior. Record exact distro, systemd, GNOME, and Shell versions.
4. On GNOME 46–48 hardware, test lid-open and lid-closed wake, brightness-service restart, agent restart, manual brightness conflict, docked/undocked, external displays, AC/battery, and unsupported topology. Confirm writes affect only the intended built-in panel.
5. Keep manual brightness unavailable on GNOME 49–50 unless a safe public read/write API and output identity are separately proven.
6. On attended hardware, run both available `s2idle` and `[deep]` cases. Verify successful suspend, failed preparation, missing/delayed signals where reproducible, overdue countdown handling, low-battery failsafe, and final closed-lid brightness. Keep a direct recovery path to reopen the lid and stop the service.

## 12. Coverage audit

The checked boxes mean the defect is covered by this plan, not implemented.

- [x] Retain a safety latch after the 30-second uncertain-preparation reporting deadline; block timer/failsafe suspend until positive resolution — §§2.3, 3.1, 8.1.
- [x] Track a successful suspend request when both signals and clock evidence are absent; bound it and clear its reason correctly — §§2–3, 8.1.
- [x] Distinguish definite `Suspend(false)` rejection from an unknown remote outcome and never retry ambiguity automatically — §3.3, §8.2.
- [x] Reconcile wake brightness with the current closed-lid desired state without bypassing journal identity checks or overwriting a manual change — §4, §8.3.
- [x] Apply the same brightness logic after brightness-service owner return, startup, periodic recovery, and every existing restoration trigger — §§4.2–4.3.
- [x] Make five-second GNOME release retries actually use a dedicated five-second scheduler and prevent duplicate retry sources — §5.1, §8.4.
- [x] Prevent uncertain sleep reconciliation from setting `timerPhase=canceled` when no countdown exists — §5.2, §8.4.
- [x] Replace the extension's never-used connected=true path with tested disconnected/connecting/connected transitions — §6, §8.4.
- [x] Stage and validate extension updates, preserve prior state, and roll back enable/discovery failures — §7, §8.4.
- [x] Recheck all affected timer, failsafe, signal, owner-change, stop/restart, panel, CLI, installer, brightness, `s2idle`, and `[deep]` paths in two independent passes — §§9–11.

## 13. Completion criteria

Plan 0.4 is complete only when:

1. Every deterministic case in §8 has a meaningful assertion and passes.
2. No path can call login1 `Suspend(false)` while logind preparation is true, unreadable, or locally unresolved.
3. Every dispatched suspend request reaches exactly one terminal outcome and cannot leak its reason into another transaction.
4. A closed-lid wake with active dimming ends in the intended dimmed state unless a documented validation failure or manual conflict forbids a write.
5. Release retry timing, timer phase, panel connectivity, and installer rollback match their published states.
6. Both post-implementation source passes find no unmapped mutation or call site.
7. All live-only items remain visibly unchecked until observed on real GNOME Wayland sessions and hardware; deterministic tests must not be presented as proof of physical `[deep]`, lid, brightness, or Shell behavior.
