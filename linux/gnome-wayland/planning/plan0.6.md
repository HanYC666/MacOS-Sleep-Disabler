# Plan 0.6 — GNOME Wayland responsiveness and remaining logical repairs

Status: **planning complete; implementation and verification not started**.

This plan follows the source-only review after plan 0.5. It covers all eight findings in that review. Implement it as one coordinated change across the agent, extension, installer, service contract, CLI, tests, and documentation. Retain the existing per-user architecture: no root helper, new privileged service, or kernel sleep-state change.

Document review establishes requirement coverage only. It does not establish that the implementation works or that every possible bug has been found. No project code or tests were executed to prepare this plan.

## 1. Complete finding map

| ID | Finding | Priority | Repair | Required evidence |
| --- | --- | --- | --- | --- |
| F1 | Ordinary synchronous D-Bus waits can delay the shutdown callback beyond systemd's stop limit. | High | §3 | Main loop remains responsive during stalled power, acquisition, notification, and preflight operations; shutdown restores before release. |
| F2 | Separate clock reads can turn a scheduling pause into false suspend evidence. | High | §4 | Inter-read pause cases never prove suspend; real clock-gap growth still can. |
| F3 | Repeated healthy prevention enable reports release pending. | Medium | §5 | Repeated and overlapping enables create no duplicate inhibitors and return truthful results. |
| F4 | Successful acquisition cleanup is misclassified as unknown acquisition. | Medium | §6 | Acquisition reply and cleanup result remain distinct under every fault sequence. |
| F5 | Installer XDG paths can differ from systemd and Shell discovery/runtime paths. | Medium | §7 | Real-path validation and generated environment contract; mismatches fail before deployment mutation. |
| F6 | Repeated dimming enable mistakes a healthy owned journal for recovery pending. | Medium | §8 | Healthy repeated enable succeeds without brightness cycling or journal replacement. |
| F7 | Panel runtime registration never retries for the same owner. | Low | §9 | Bounded registration retries recover, with no stale lifecycle effects. |
| F8 | Successful panel recovery retains an obsolete connection error. | Low | §10 | Recovery clears only superseded connection errors; action and registration failures retain their own lifetimes. |

## 2. Implementation ledger and order

When implementation is requested, replace the contents of `../task.md` with a detailed ledger **before editing implementation files**. Preserve that ledger at completion. For each subtask record implementation evidence, meaningful verification evidence, and two separate review passes before marking it complete. Keep unperformed hardware gates visibly unchecked.

Order:

1. Inventory existing state machines, external calls, public API, and prior plan safeguards.
2. Implement the asynchronous operation framework and shutdown lifecycle (§3).
3. Migrate power, brightness, acquisition/release, sleep preflight, notifications, and their callers onto that framework.
4. Repair clock evidence (§4).
5. Repair prevention idempotence and acquisition classification (§§5–6).
6. Establish installer environment and rollback contracts (§7).
7. Repair dimming semantics (§8).
8. Repair extension registration and error domains together (§§9–10).
9. Synchronize runtime versions, readiness checks, CLI, tests, service expectations, and documentation (§11).
10. Complete targeted verification, full regression checks, and both final audits (§§12–14).

Do not mark implementation complete merely because this document's coverage boxes are checked.

## 3. F1 — Responsive D-Bus operations and bounded shutdown

### 3.1 Inventory the whole blocking surface

List every outbound remote call, including calls hidden in adapters and helpers:

- UPower `Get`, `GetAll`, `EnumerateDevices`, and `GetDisplayDevice`.
- SessionManager owner lookup, `Inhibit`, and `Uninhibit`.
- login1 `Inhibit`, `CanSuspend`, `PreparingForSleep`, and `Suspend`.
- Brightness introspection, property reads, writes, and readback.
- Notifications `Notify`.
- Startup bus/owner lookups and connection establishment.

Include constructor/startup work, timer/failsafe paths, periodic reconciliation, signals, API handlers, and shutdown. Explicitly decide whether each local filesystem operation has an acceptable remaining scheduling limitation. Never advertise an unconditional wall-clock guarantee under arbitrary CPU starvation or stalled storage.

### 3.2 Use asynchronous calls and explicit operation ownership

Use dbus-python's asynchronous `reply_handler`/`error_handler` mechanism for outbound calls, with explicit transport timeouts and GLib operation deadlines. Verify its installed-version behavior and Unix-FD reply ownership during implementation; do not assume a timeout cancels execution at the remote service.

Keep mutable agent state on the main-loop thread. Do not add uncontrolled worker threads sharing bus connections or agent fields. Preserve the existing dbus-python public service unless a separately justified migration is necessary.

Each operation owns:

- Its operation ID, lifecycle generation, relevant service unique owner, and deadline.
- Its expected state/transaction identity and exactly-once completion status.
- Its timers, continuations, and any newly received cookie or FD.
- A disposition for late success, timeout, remote error, owner replacement, and shutdown.

Cancellation invalidates future state mutation; it is not evidence that a remote request did not happen. Late FD replies must be closed. Late cookie replies must either be cleaned up against the issuing owner or remain represented as unresolved ownership until the connection boundary is closed. Late `Suspend` replies must never cause a repeat suspend.

### 3.3 Bound complete operations, not only calls

Define named constants and document their accounting:

- Ordinary remote-call timeout: at most 3 seconds.
- Power refresh total deadline: at most 6 seconds, regardless of device count.
- Acquisition/preflight total deadline: at most 9 seconds per operation, including owner checks.
- Notification timeout: at most 2 seconds; no feature waits for its result.
- Shutdown total remote-work budget: at most 24 seconds from entry into shutdown, leaving margin below `TimeoutStopSec=30`.

Every subcall uses the lesser of its normal timeout and remaining operation budget. Do not start a call after the budget expires. These bounds govern remote work under a scheduled main loop, not arbitrary host stalls.

Power refresh builds a private snapshot and commits it only if its operation and owner generations are current. Coalesce repeated refresh requests; bound concurrent per-device reads. On failure publish conservative unknown evidence, never a readable subset as aggregate charge evidence. A late old snapshot cannot overwrite new evidence.

Expose an internal refresh continuation/result so suspend preflight can explicitly await fresh evidence. Reusing a cached value while a refresh is pending must not satisfy the existing fresh-lid or battery safety checks.

### 3.4 Preserve serialization and transactional semantics

Serialize mutating inhibitor and brightness operations. Coalesce equivalent requests; order conflicting requests explicitly. `off`, stop, bus loss, and owner replacement supersede an unfinished enable. Brightness reconciliation retains one desired-state coordinator and validates its target again after every asynchronous boundary.

Converting brightness I/O to asynchronous calls must preserve journal-before-write, readback, machine/output validation, manual-change conflict handling, and uncertain-write recovery. A timeout after `Set` is an unknown write result, not permission to discard the journal or redim blindly.

Register signal handling before asynchronous startup work can acquire resources. Keep the public bus service usable for bounded status during startup; publish effective prevention only after both required inhibitors are proven. Define startup failure/cleanup behavior and remain within `TimeoutStartSec=20` or revise the bound with explicit budget evidence.

### 3.5 Public API and clients

For methods that wait on operations, use dbus-python service `async_callbacks` while preserving method signatures and final success/error meaning. Do not return success merely because work was queued. Every request receives exactly one terminal reply, including coalesced callers and shutdown cancellation.

`GetState` must read local state without initiating remote work. `StateChanged`/`TimerChanged` retain their contracts. Choose explicit CLI, extension, and installer call deadlines greater than the applicable total operation budget plus reply margin. The extension's existing 5-second action timeout cannot remain for a method legitimately allowed 9 seconds. Keep status/error fallback reads bounded too.

Account for the entire public method chain, including queue time, acquisition, power refresh, brightness reconciliation, and cleanup. Give ordinary mutating requests a total deadline of at most 20 seconds and client action calls a 25-second deadline; inner operations share the remaining request budget instead of adding independent full budgets. Admission must be bounded: equivalent calls coalesce, conflicting calls have explicit ordering, and overload gets a truthful busy error. Suspend execution is separate from bounded preflight: retain the existing asynchronous suspend reply/unknown-outcome rules rather than claiming the kernel will complete sleep within an ordinary mutation deadline. Shutdown has its own priority and budget and never waits behind the ordinary mutation queue.

### 3.6 Shutdown transition

On shutdown entry:

1. Set `shutting_down`, clear requested prevention, consume/cancel future timer work, and invalidate ordinary operation generations.
2. Stop retry sources and prevent new refresh, notification, acquisition, dim, and suspend work.
3. Drain or classify already dispatched brightness writes within the remaining budget. Preserve journals for unknown outcomes.
4. Reconcile toward restored brightness before entering normal remote inhibitor release; check the shutdown budget at every continuation.
5. Release GNOME ownership and local inhibitor FDs. If ownership remains ambiguous, close the client session-bus connection as the existing ownership boundary.
6. Quit once cleanup completes or the internal deadline expires. Retain unresolved brightness records and report limitations truthfully.

Bus loss must not make restoration call an unavailable bus or accidentally redim. A second stop is idempotent. Ensure unfinished acquisition callbacks cannot install an inhibitor after shutdown or leak returned resources.

Acceptance: a stalled ordinary remote call never prevents the stop transition from being dispatched. Restoration is attempted before remote release whenever its bus and budget are available. Recovery remains eventual rather than guaranteed after unavailable services, logout, forced kill, or stalled storage.

## 4. F2 — Suspend evidence with clock uncertainty

Create one shared clock-sampling helper. Read `MONOTONIC` before BOOTTIME and again after BOOTTIME:

```text
m_before <= monotonic_at_boottime <= m_after
gap_lower = boottime_sample - m_after
gap_upper = boottime_sample - m_before
sample_width = m_after - m_before
```

Require finite ordered values. Reject samples wider than a named sampling limit, initially 50 ms. An invalid or delayed sample is unavailable evidence, not zero gap or suspend proof.

Store interval baselines rather than scalar differences. Prove suspend only when:

```text
current.gap_lower - baseline.gap_upper >= SUSPEND_GAP_TOLERANCE
```

Retain the existing 0.5-second suspend threshold unless a separate justified change is made. Do not update a valid baseline with an invalid sample. During ambiguous overlapping intervals, retain the earlier trustworthy baseline until evidence is resolved; do not progressively erase potential suspend growth. Validate baseline update rules for repeated samples and accumulated uncertainty.

Use this helper consistently in construction, timer start, request dispatch, external preparation entry/exit, reconciliation, and transaction resolution. Remove independent raw clock subtraction in those paths. BOOTTIME remains the timer deadline clock; MONOTONIC remains the operation/uncertainty deadline clock.

When a request has no trustworthy pre-request baseline, mark gap proof unavailable for that transaction. A later first valid sample cannot retrospectively prove its suspend. Preserve conservative signal/logind resolution and unknown-outcome blocking; lack of gap proof must never authorize another automatic request.

Tests must cover pauses before/after the middle read, 0.7-second false-positive reproduction, repeated invalid samples, opposite-order legacy scenarios, exact threshold boundaries, real suspend growth, startup without a baseline, invalid request baseline, overlapping intervals, and `PreparingForSleep=True` delaying classification. Verify no false wake notification, timer cancellation, brightness wake cycle, or unknown-request resolution.

## 5. F3 — Idempotent prevention control

Define the enable decision table explicitly:

| State | `SetPrevention(True)` result |
| --- | --- |
| Healthy held GNOME cookie, current issuing owner, live login1 FD, effective prevention true | Success; preserve ownership; no new inhibitors. |
| Equivalent acquisition pending | Join the existing result; no duplicate request. |
| Release pending, unresolved acquisition, or inconsistent partial ownership | Truthful typed error or defined recovery continuation; never duplicate acquisition. |
| Ownership absent and agent available | Start one acquisition. |
| Shutdown or unavailable required bus | `Unavailable`; no acquisition. |

Keep requested state, effective state, and pending operation distinct. Repeated enable must not introduce or preserve a fabricated release-pending error. Track error origin so a successful prevention operation clears its superseded prevention error without deleting unrelated brightness, lid, or durability diagnostics.

Retain the existing fresh power/failsafe evaluation on successful enable; a repeated `on` is not permission to bypass a newly unsafe battery condition. Verify `on → on`, two simultaneous enables, `on → off` during acquisition, enable during genuine release uncertainty, owner replacement, and partial ownership.

## 6. F4 — Acquisition outcome separate from cleanup outcome

Represent acquisition stages explicitly: not dispatched, dispatched/reply unknown, cookie received with issuing owner, ownership validated, cleanup confirmed, cleanup unresolved, and owner lost. Do not infer whether a cookie was received from its current cleared value.

Bind the call to the resolved unique SessionManager owner so the issuing owner is known even if its well-known name changes. If the reply returns a cookie but subsequent validation fails, clean up that known cookie against the correct owner. Confirmed release means a known acquisition was cleaned up; it does not require the unknown-acquisition disconnect path.

A genuinely unknown dispatched acquisition still requires the existing conservative connection-boundary cleanup. If known-cookie cleanup is uncertain, retain explicit release-pending state and bounded recovery; do not silently discard it. Close login1 FDs on every unsuccessful path, including late replies.

Preserve user intent only where safe and supported by the recovery state machine. Any retry after confirmed cleanup must be bounded, generation checked, and blocked by `off`, shutdown, failsafe, or owner loss. Report the actual failed validation and cleanup disposition rather than claiming an unknown outcome unnecessarily.

Required faults: pre-dispatch failure; GNOME error with unknown result; cookie reply followed by owner-check failure and successful release; same with uncertain release; owner replacement; malformed reply; late cookie/FD after cancellation; and shutdown during each stage.

## 7. F5 — Explicit XDG environment and discovery contract

### 7.1 Selected policy

Support custom paths only when their discovery can be established. Do not globally import installer variables into the user manager, restart Shell, or modify unrelated session environment.

Resolve default/empty XDG values consistently and require absolute effective paths. Derive one installation contract containing program/CLI destinations, unit destination, extension destination, and agent state home. Retain existing managed-path type checks and transactional rollback.

### 7.2 Unit discovery

Before changing deployment files, query the actual user manager's unit search paths using supported systemd interfaces, such as `org.freedesktop.systemd1.Manager.UnitPath` with a bounded command fallback only after verifying its format. Require the selected unit directory to be in the manager's effective search path. Do not treat the invoking shell's XDG variable as proof.

Reject an unsearchable custom config destination with a precise preflight error explaining that the running session must use the intended configuration path. This plan deliberately does not create external-unit links or modify manager search paths.

After deployment and reload, inspect the service's loaded `FragmentPath` and require it to resolve to the managed unit. A same-named unit from a higher-priority path must not satisfy readiness. Include this check in rollback verification for an existing installation.

### 7.3 Agent state environment

Generate an explicit `Environment=` assignment for the selected `XDG_STATE_HOME` in the unit, using tested systemd directive quoting/escaping rather than shell quoting. Escape percent specifiers and handle spaces, quotes, backslashes, and non-ASCII; reject newline, carriage return, NUL, or other values that cannot be represented safely under the chosen renderer.

Extend the service validator to validate the generated environment assignment. The agent, settings, journals, diagnostics, and documentation must all refer to the same state home. Add the effective state home to bounded readiness evidence and require it to match the installation contract; coordinate any API/schema version change.

Do not relocate old journals silently when the selected state home changes. Detect an existing installation's state-home contract; reject a path change before mutation if it could abandon a recovery record. Define a documented attended migration procedure that preserves and reconciles old records before changing the contract.

For installations predating an exported state-home field or explicit unit environment, inspect the prior service's effective environment and bounded same-user process evidence where available. A missing field is not proof of the default path. If the previous location cannot be established, fail the state-path transition conservatively and retain the old deployment; document the recovery/migration steps.

### 7.4 Shell discovery

Establish the running Shell's effective data home before replacing extension files. Resolve Shell's session-bus unique owner and same-user PID through D-Bus, then read only the relevant XDG variable from `/proc/<pid>/environ` when permitted. Do not log the full environment. Check owner/PID generation before and after the read; retry a bounded owner change or fail preflight conservatively.

Verify on target GNOME versions that this observation represents Shell's extension discovery roots. Existing-extension path information, where available through GNOME's supported extension tooling/interface, provides an additional check against shadowed UUIDs.

If Shell environment cannot be established, do not guess custom discovery roots. Fail with a precise pending/unsupported-environment result before changing artifacts. Document this restriction rather than accepting a path and later claiming it was discovered. A known default path still requires the existing post-install active-runtime proof.

Compare selected and observed effective data homes. Reject mismatch before core or panel mutation. Do not copy the extension to two directories as a workaround. Retain bounded logout/login guidance for a matching path that Shell has not yet discovered.

### 7.5 Verification

Add realistic test fixtures for manager unit-path and loaded-fragment results and Shell owner/PID/environment observations. Existing stubs proving file placement are insufficient.

Cover default and empty values, matching custom session paths, installer-only overrides, inaccessible `/proc`, changing Shell owner, shadowed units/extensions, unsupported query results, unsafe directive values, selected state propagation, state-home changes with outstanding recovery, readiness mismatch, and rollback restoring the prior environment and fragment contract. All installer test commands remain isolated from the developer's real services.

## 8. F6 — Idempotent dimming with truthful journal state

Use the coordinator's classified brightness state, not journal existence alone, to decide whether enable is permitted.

- Already enabled with a healthy `dimmed-owned` record: succeed without restore/redim, new write, journal replacement, or false recovery error.
- Genuine recovery pending, invalid identity, uncertain write, or malformed record: retain conservative rejection/recovery rules; do not relabel as healthy.
- Normal enable with no unresolved journal: persist and reconcile once.
- Repeated explicit enable after manual conflict: preserve the documented intentional rearm semantics; allow a fresh guarded reconciliation only when journal/identity validation permits it.
- Disable during an asynchronous dim: set the restored target and resolve any late write through the same coordinator.

Preserve unavailable-adapter errors, settings durability handling, and manual-change protection. Verify repeated enable while dimmed, open-lid enable, recovery pending, manual conflict rearm, identity mismatch, persistence failure, and enable/disable races. UI and CLI must agree with `brightnessRecoveryPending`.

## 9. F7 — Bounded runtime registration recovery

Replace `_runtimeRegistrationStarted` with explicit idle/pending/succeeded/exhausted registration state, attempt count, retry source, and lifecycle generation. Keep registration separate from state bootstrap.

Use three total attempts, with 1- and 2-second delays after failure. A synchronous send failure and an asynchronous failure follow the same bounded policy. Successful registration stops retries. Exhaustion reports registration failure without disabling otherwise valid state/control functionality.

Capture the unique agent owner, generation, connection, and cancellable for each attempt. Reset on a new owner or disable/re-enable. Remove timers and invalidate callbacks on disable/disconnect. A timeout may have registered remotely; retries must therefore be idempotent for the same sender/version. Check and preserve this behavior in the agent's `RegisterPanelRuntime` method.

Runtime registration and installer proof remain separate: disk version, configured enablement, and active extension are not substitutes for the exact registered runtime. Recheck installer readiness timing against the complete retry envelope. An obsolete registration callback cannot show an error, schedule work, or mark the new owner registered.

Tests: immediate success; sync failure; async failure; timeout after remote success; second/third-attempt success; exhaustion; recovery of GetState without registration; owner change during retry/callback; disable; re-enable; and unregister ordering.

## 10. F8 — Error domains and recovery

Maintain separate extension fields for connection/bootstrap error, runtime-registration error, and user-action error. Compose status deterministically without hiding agent-reported diagnostics.

Authoritative current-owner `GetState` success or `StateChanged` clears the connection/bootstrap error and marks controls connected. It does not clear action or registration errors. `TimerChanged` alone is not full bootstrap recovery. Registration success clears only its error. Successful actions clear only the action error they supersede, using an action serial so an older callback cannot clear a newer failure.

Owner replacement and disable reset lifecycle-local errors. A late reply from an old generation cannot clear or introduce any current error. An obsolete state reply cannot clear a newer state failure merely because it succeeded remotely.

Test retry exhaustion followed by late authoritative state, successful refresh, simultaneous independent errors, registration recovery, successful/failed actions in reverse callback order, stale replies, and disable/re-enable. Assert displayed text and stored error state, not only switch sensitivity.

## 11. Cross-stack integration and documentation

Update `agent.py`, `ctl.py`, `extension/.../extension.js`, `core_install.py`, `install.sh`, the service template, and relevant tests as required. Update metadata/runtime constants only through a coherent versioning decision; installer expectations and extension/agent runtime proof must match the shipped revision.

Preserve public method signatures when asynchronous replies suffice. Any new exported state field or changed response semantics requires an explicit API compatibility decision, tests, and readiness parser changes where needed. Do not hide incompatibility behind an unchanged version.

Update README with:

- Repeated enable semantics and manual-conflict rearm behavior.
- Operation and client deadlines, conservative timeout outcomes, and shutdown limitations.
- Interval-based suspend evidence and unavailable-baseline behavior.
- Supported XDG/session contract, effective state location, migration restriction, and preflight errors.
- Registration retry behavior and independent panel errors.
- Honest distinction between deterministic verification and live platform support.

Retain all prior safeguards: dual inhibitor proof; unique-owner cookie tracking; bounded release recovery; restore-before-release; journal identity validation; settings durability diagnostics; unknown battery evidence; one-shot timer consumption; fresh conditional preflight; unknown suspend blocking; transactional deployment; and exact runtime readiness.

## 12. Verification matrix

Implementation verification is authorized only when implementation is requested. For this planning task, these remain future requirements.

| Layer | Required verification |
| --- | --- |
| Operation framework | Virtual scheduling/deadlines, one terminal reply, stale callback rejection, coalescing, cancellation, late resource cleanup. |
| Power/preflight | Stalled multi-device refresh, bounded aggregate work, conservative partial data, fresh evidence after release, owner-generation races. |
| Shutdown | Stop while every ordinary remote call is stalled; restoration order; late brightness writes; budget exhaustion; bus loss; repeated stop. |
| Clock evidence | Inter-read preemption without suspend; genuine growth; unusable baselines; no unintended timer/wake effects. |
| Prevention/acquisition | Healthy repeated enable; concurrent callers; known-cookie cleanup versus unknown acquisition; every resource fault path. |
| Dimming | Healthy journal versus recovery; no gratuitous cycling; conflict rearm; asynchronous enable/disable; durability faults. |
| Installer | Actual discovery contracts modeled, safe unit environment rendering, exact fragment/state readiness, mismatch preflight, rollback. |
| Extension | Registration retry lifecycle, independent errors, authoritative recovery, obsolete callbacks, client timeout alignment. |
| Regression | Existing Python and Node suites plus meaningful new behavioral assertions; never substitute test counts for coverage. |

Use controllable fake remote replies and clocks; do not make deterministic tests rely on wall-clock sleeps. Tests must assert observable behavior, resource lifetime, ordering, deadlines, and absence of duplicate writes/requests. Full regression verification follows targeted fixes; repeat broader runs only to resolve remaining risk or a required gate.

## 13. Live release gates

Keep these unchecked until actually performed on Linux:

- [ ] Debian, Ubuntu, Linux Mint, and Kali with an installed GNOME Wayland session: install/update/rollback, user unit discovery, CLI, panel activation, and effective environment.
- [ ] Representative experimental GNOME 46–50 range: extension lifecycle, remote-call behavior, and registration proof. Preserve the documented absence of manual brightness support on 49–50 unless separately verified and implemented.
- [ ] Default session paths and a session launched with matching custom XDG paths; installer-only overrides rejected before mutation.
- [ ] Attended single-panel brightness tests: close/open lid, manual changes, stalled remote services, stop while dimmed, recovery after restart, and logout limitations.
- [ ] Both `s2idle` and `[deep]` hardware where available: real suspend, failed preparation, resume, timer behavior, and uncertainty classification.
- [ ] External/manual suspend while prevention is active: truthful outcomes and no duplicate automatic requests.

`deep` uses the same inhibitor APIs. Do not change `/sys/power/mem_sleep`; preserve the laptop's configured sleep mode. Record actual distro, Shell, systemd, kernel, hardware, and observed mode for every live result. Mint/Kali entries refer specifically to GNOME sessions, not their usual alternative desktops.

Fix identified high-priority logical faults before laptop trials. Live testing then resolves platform/hardware uncertainty; it is not a substitute for known fixes. A new concrete safety fault discovered during implementation or testing must be addressed before advancing the relevant gate.

## 14. Two final implementation audits

### Pass 1 — Requirement to source to evidence

Trace F1–F8 through actual code, behavioral assertions, documentation, and readiness contracts. Check every asynchronous operation's timeout, total budget, terminal reply, owner/generation guard, resource cleanup, and shutdown disposition. Search for remaining implicit synchronous remote calls and raw clock-gap subtraction. Read complete affected functions, including previously truncated ranges.

### Pass 2 — Adversarial complete-stack walkthrough

Independently walk installation → startup → panel/CLI → enable → lid/dim → timer/failsafe → suspend/resume → disable → shutdown → restart/update/rollback. Inject timeout, callback reordering, service-owner change, repeated requests, manual brightness changes, environment mismatch, and unavailable clock samples at every boundary. Verify prior safeguards still hold and no retry can duplicate a suspend, inhibitor, dim, or API reply.

Completion requires all eight repairs and both audits, sufficient deterministic verification, retained detailed ledger, and explicit reporting of still-unperformed live gates. Do not claim all distro/hardware support from local tests.

## 15. Planning coverage checks

- [x] Document pass 1: all eight review findings have their own repair section and required failure-case evidence; the coverage table maps every finding.
- [x] Document pass 2: reviewed cross-component effects on service budgets, asynchronous API/client deadlines, late resources, clocks, journal recovery, XDG discovery/rollback, runtime proof, error lifetimes, versioning, prior safeguards, and `deep`/`s2idle` gates.
- [ ] Implementation complete.
- [ ] Implementation audit pass 1 complete.
- [ ] Implementation audit pass 2 complete.
- [ ] Live release gates complete.
