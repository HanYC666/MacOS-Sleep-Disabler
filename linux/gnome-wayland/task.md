# GNOME Wayland plan 0.5 implementation ledger

This ledger replaces the stale plan 0.4 completion record before plan 0.5 implementation begins. It must remain in the repository after completion. Checked implementation items require source changes plus meaningful deterministic verification. Real GNOME, distro, display hardware, `s2idle`, and `[deep]` observations remain unchecked until performed on those systems.

## Preparation and scope control

- [x] Read `planning/plan0.5.md` and map every requirement to agent, panel, service, installer, tests, documentation, review, or a live-only gate.
- [x] Inspect the worktree and preserve all existing plan 0.4 work and unrelated user changes.
- [x] Clear the stale plan 0.4 ledger and create this detailed plan 0.5 ledger before implementation edits.
- [x] Confirm plan 0.5 has no duplicate file-map entry or other scope-changing editorial defect.
- [x] Record the pre-change regression baseline: 74 Python tests and the existing Node panel harness pass; this does not verify plan 0.5.

## Agent runtime and API contract

- [x] Add immutable agent runtime version and integer API schema constants.
- [x] Publish `agentRuntimeVersion` and `apiVersion` from `GetState`.
- [x] Add `RegisterPanelRuntime(s version)` with caller unique-name binding and strict version validation.
- [x] Add `UnregisterPanelRuntime(s version)` that accepts only the owning sender and matching version.
- [x] Clear panel registration when its unique bus owner disappears and on process restart.
- [x] Publish `panelRuntimeVersion` and `panelRuntimeRegistered` truthfully.
- [x] Cover valid, malformed, foreign-sender, mismatched unregister, owner-loss, and restart behavior.

## Tri-state UPower and failsafe episode safety

- [x] Replace binary discharge evidence with `discharging`, `not-discharging`, and `unknown` classifications.
- [x] Treat UPower device state 2 as discharging and states 0/6 as unknown.
- [x] Aggregate multiple batteries conservatively; empty, partial, or conflicting evidence stays unknown.
- [x] Publish `batteryDischargeState` for diagnostics.
- [x] Centralize exact `failsafe_condition_met()` trigger evidence.
- [x] Centralize positive-only `failsafe_condition_cleared()` rearm evidence.
- [x] Preserve `failsafe_triggered` through UPower exceptions, owner loss, empty batteries, missing percentage, state 0, and state 6.
- [x] Require identical positive evidence on the fresh read immediately before suspend.
- [x] Preserve the one-request episode latch after accepted, rejected, and unknown delivery outcomes until positive recovery or explicit settings action.
- [x] Test single/multiple/partial battery sets and every ambiguous low → unknown → low sequence with exact suspend call counts.
- [x] Test permitted rearm from AC, threshold recovery, established non-discharging state, and explicit failsafe reconfiguration.

## Atomic JSON outcome coherence

- [x] Add a result/exception contract that distinguishes failure before replace from visible replacement with uncertain directory durability.
- [x] Close the directory FD on every path and retain diagnostics without using `rm`.
- [x] Reconcile settings memory from disk after post-replace directory open/fsync/close failure.
- [x] Keep old memory authoritative after verified pre-replace failure.
- [x] Reconcile brightness journal memory and state after every post-replace failure.
- [x] Refuse a new brightness dim when a prepared journal is visible but durability is uncertain.
- [x] Preserve recovery information when applied-journal durability becomes uncertain after the brightness write.
- [x] Inject temporary open/write, file fsync, replace, directory open, directory fsync, and close failures and assert disk/memory agreement.

## Truthful lid and brightness state

- [x] Add a scoped `lidError` field and stop retaining old lid failures in the general error after lid mode is disabled or succeeds.
- [x] Preserve unrelated persistence, inhibitor, sleep, and brightness errors when lid state changes.
- [x] Add a stable `brightnessState` classification.
- [x] Make healthy applied closed-lid ownership `dimmed-owned` with `brightnessRecoveryPending=false`.
- [x] Mark prepared, restore-required, redim-required, retrying, unreadable, and invalid records as recovery pending.
- [x] Publish manual-change preservation separately from failed recovery.
- [x] Keep all journal identity, machine, output, current-value, and readback checks from plan 0.4.
- [x] Test healthy dim, restore pending, redim pending, invalid/unreadable, manual conflict, and no-journal states.

## Shutdown brightness ordering

- [x] Add an explicit shutdown/restored-brightness desired state without falsifying inhibitor ownership.
- [x] Block new acquire, timer, retry, and dim work once shutdown begins.
- [x] Reconcile/restore brightness before the first remote GNOME `Uninhibit` attempt.
- [x] Make later release reconciliation idempotent.
- [x] Retain the recovery journal and continue cleanup when brightness cannot be verified/restored.
- [x] Test exact ordering with successful, raising, and blocking fake `Uninhibit` calls.
- [x] Cover healthy applied dim, prepared journal, manual conflict, no adapter, no journal, forced disconnect, SIGTERM-equivalent stop, and repeated stop/release.

## Panel requested/effective semantics

- [x] Render the prevention switch from `desired` and status/icon from effective inhibitor state.
- [x] Keep `desired=true`, `enabled=false` cancellable and send `SetPrevention(false)` when switched off.
- [x] Render release-pending and reconnecting states without conflating them.
- [x] Restore switches to authoritative requested state after rejected actions.
- [x] Derive Cancel sensitivity from `timerPhase == running`, including zero displayed seconds.
- [x] Disable Cancel only after authoritative consumed/canceled/idle state.
- [x] Render healthy brightness ownership separately from recovery pending.
- [x] Render scoped lid diagnostics only while current/relevant.

## Panel connection retry and runtime handshake

- [x] Add constants for exactly three `GetState` attempts per current owner: immediate plus two bounded retries.
- [x] Keep controls disabled and show connecting/retrying status between attempts.
- [x] Establish connected state from successful current-generation `GetState` or authoritative `StateChanged`.
- [x] Cancel the sole retry source on success, current signal, disconnect, generation change, or extension disable.
- [x] Prevent stale replies, signals, and retry callbacks from changing a newer generation.
- [x] Reach stable disconnected/error state after the bounded attempts while allowing a later valid current-owner signal to recover.
- [x] Add extension runtime version consistent with metadata.
- [x] Register runtime after agent connection/owner return and unregister on extension disable when possible.
- [x] Ensure registration failure does not prevent extension disable or local cleanup.
- [x] Expand the Node harness for requested/effective states, zero timer, all retry branches, stale generations, diagnostics, brightness labels, and runtime lifecycle.

## User service and interpreter consistency

- [x] Change the unit to `Type=dbus` with `BusName=org.sleepdisabler.App`.
- [x] Add a bounded `TimeoutStartSec` while retaining the required restart and stop policies.
- [x] Point `ExecStart` at a generated installed agent launcher.
- [x] Resolve `${PYTHON:-/usr/bin/python3}` to a stable executable and validate imports with it.
- [x] Generate agent and CLI launchers that safely execute that exact interpreter.
- [x] Validate quoting for paths with spaces and shell metacharacters without evaluating environment text.
- [x] Resolve the unit directory through `${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user` for every stage/backup/install path.

## Transactional core installer

### Additional transaction requirements from the amended plan

- [x] Record the prior generated interpreter safely; prove prior API recovery using that interpreter after changing PYTHON.
- [x] Capture explicit service-state text and reject unknown, masked, static, or otherwise unrestorable state before mutation.
- [x] Persist transaction intent before each switch, retain backups, and detect interrupted transactions before a subsequent install.
- [x] Serialize installers and roll back catchable SIGINT/SIGTERM without recursive interruption.
- [x] Bound readiness with a monotonic deadline in addition to attempt count and subprocess timeouts.
- [x] Limit unit-verifier fallback to identified unsupported syntax; test timeout and real validation failure.
- [x] Read registered panel runtime and committed agent/API version from one coherent snapshot.
- [x] Classify all panel staging failures after core commit as exit 2.
- [x] Preserve backup and successful deployment when optional final backup renaming fails.
- [x] Reject relative XDG paths and verify shell/systemd escaping with actual generated launcher invocation.
- [x] Document skipped JavaScript syntax validation when an optional parser is unavailable.
- [x] Test every new branch, then trace its mutations in review pass 1 and full-stack outcomes in review pass 2.

- [x] Stage agent, CLI, launchers, and unit before replacing any active artifact.
- [x] Validate Python compile/import, launcher syntax/paths, unit structure, modes, and required files from staging.
- [x] Use `systemd-analyze --user verify` when supported with deterministic test fallback.
- [x] Capture prior artifact presence, service enabled state, active state, API health, agent version, and panel version.
- [x] Switch staged artifacts while preserving unique same-filesystem rollback copies.
- [x] Reload systemd and restore intended enable/start state.
- [x] Poll boundedly for service state, D-Bus ownership, exact staged agent runtime, compatible API schema, and callable state API.
- [x] On failure, retain failed artifacts and restore prior files, daemon state, enablement, running state, and prior API health.
- [x] Report file, enablement, running-state, and API rollback results separately.
- [x] Preserve prior disabled/inactive state rather than starting it during rollback.
- [x] Leave a failed first core install without a false enabled/healthy claim and exit 1.
- [x] Commit a verified new core before beginning panel deployment; retain it on panel-only exit 2.
- [x] Perform all cleanup through moves/retained diagnostics and never use `rm`.

## Transactional extension installer

- [x] Validate metadata, UUID, required files, JavaScript syntax, and runtime/metadata version agreement before switch.
- [x] Preserve prior installed/enabled/active/runtime state on upgrade.
- [x] Keep a validated first install in the canonical UUID directory after discovery, enable, active-list, or runtime-proof failure.
- [x] Give truthful logout/login and re-enable instructions for retained first installs.
- [x] Restore the previous canonical extension and configured state on upgrade failure.
- [x] Retain the failed new upgrade under a diagnostic path.
- [x] Verify discovery, enabled-list, and active-list state.
- [x] Poll for the exact staged `panelRuntimeVersion`; reject blank, malformed, stale, newer, older, and timeout results.
- [x] Verify prior runtime restoration when possible; otherwise report it as unproven and require logout.
- [x] Never claim new JavaScript is active based only on disk metadata or enabled/active lists.
- [x] Preserve exit 0 for fully proven agent+panel, exit 2 for usable core with panel pending/failure, and exit 1 for core failure.

## Installer deterministic harness

- [x] Extend command stubs for enabled/active state, D-Bus readiness, exact agent/API version, exact panel runtime, delays, and failures.
- [x] Test validation failure before any core switch.
- [x] Test first core install and successful upgrade with retained previous copy.
- [x] Test missing bus ownership, missing API, delayed readiness, stale agent version, and incompatible API version.
- [x] Test full core rollback and partial rollback reporting.
- [x] Test prior disabled/inactive preservation.
- [x] Test explicit alternate interpreter usage and path quoting.
- [x] Test custom `XDG_CONFIG_HOME` unit installation and rollback.
- [x] Test first extension discovery failure retaining canonical files and successful simulated next-login discovery.
- [x] Test upgrade discovery/enable/active failures and old state restoration.
- [x] Test exact/delayed/stale/blank/malformed/timed-out panel runtime reports.
- [x] Test panel mismatch rollback and old runtime recovery proof.
- [x] Test core success plus panel exit 2 preserving the new working CLI.
- [x] Prove the harness never touches the real HOME, user manager, GNOME profile, or session bus.

## Documentation and research checklist

- [x] Update README for tri-state failsafe/rearm evidence.
- [x] Document requested versus effective panel prevention and zero-second Cancel behavior.
- [x] Document bounded panel connection retry and runtime handshake.
- [x] Document retained canonical first-install recovery and upgrade rollback/logout cases.
- [x] Document transactional core rollback and the core/panel commit boundary.
- [x] Document `Type=dbus`, bounded exact-version readiness, selected interpreter, and XDG unit path.
- [x] Document shutdown brightness ordering and atomic durability-uncertain state.
- [x] Document healthy dim ownership, actual recovery pending, and scoped lid errors.
- [x] Update `linux/research_checklist.md` with deterministic evidence while retaining every live-only limitation.

## Focused and full deterministic verification

- [x] Run focused agent failsafe tests after battery work.
- [x] Run focused persistence/brightness/stop tests after state work.
- [x] Run the Node panel harness after extension work.
- [x] Run focused isolated installer tests after installer work.
- [x] Run the complete Python discovery suite.
- [x] Run Python compile checks for agent, CLI, and test modules.
- [x] Run shell syntax, JavaScript syntax, metadata JSON, and diff whitespace checks.
- [x] Search for exactly one executable `Suspend(false)` call.
- [x] Search for forbidden binary failsafe rearm, hard-coded installed interpreter/unit path, timerRemaining-only cancellation, missing runtime proof, wrong stop ordering, and any `rm` command.

## Double-check pass 1: mutation, ownership, and transaction audit

### Additional plan amendments — each requires implementation and both reviews

- [x] Verify exact dim target readback before applied/healthy publication; test ignored, partial, matching, and unreadable values, retaining prepared recovery evidence.
- [x] Bound every shutdown D-Bus call, including hidden proxy introspection; count the complete restore/release budget and reserve time for local cleanup.
- [x] Reject missing-unit first-install normalization when orphan core artifacts or a dangling unit exist; test independent artifact absence and status 4.
- [x] Audit panel intent-record publication under interruption/corruption; retain incomplete records and refuse subsequent switching.
- [x] Resolve receipt durability as process coordination, document the limit, and audit failure outcomes.
- [x] Separate previous panel file/configuration restoration from unproven runtime recovery in every report.
- [x] Resolve graphical-session target default ordering and fix any cycle; retain live environment/logout gates.
- [x] Protect against overlapping same-owner GetState replies arriving in reverse order, including obsolete errors, signals, and lifecycle changes.
- [x] Validate installed dbus-python contract assumptions from primary sources and retain real-bus delivery gates.
- [x] Re-read truncated sleep reply/transaction/StartTimer endings and map all remaining installer fault categories to assertions.

Implementation entries below have now been reconciled against source, deterministic assertions, and both completed audits. Historical snapshots retain their original counts and limitations; the final evidence section supersedes them. Checked source/contract work does not certify installed package behavior or physical results; those remain in the live gates.

### Findings and repair subtasks from this pass

- [x] Disable libdbus automatic exit on both buses and register explicit system-bus disconnect cleanup/restart; test handler setup and brightness-before-release/FD cleanup, document the upstream contract, and review twice.
- [x] Audit and repair disconnect-policy timing before startup traffic, initialized handler state, and repeated/simultaneous/stop-time disconnects; assert idempotent cleanup without reacquire/redim and review twice.

- [x] Validate raw D-Bus readiness field types before JSON conversion; exercise malformed strings, booleans, fractional API versions, and legacy missing fields at the probe boundary, then review twice.
- [x] Validate explicit and state-derived CLI thresholds before unsigned conversion; test invalid input without bus mutation and valid endpoint dispatch, then review twice.
- [x] Verify `CollectMode` placement in `[Unit]` and reject `[Service]` placement during staging; record source/test evidence after both reviews.
- [x] Verify synchronous panel bootstrap/action/runtime send failures and countdown freshness; align callback indentation and review lifecycle cleanup twice.
- [x] Establish disconnected signal-publication behavior; prevent transport failure from interrupting cleanup or committed-state reconciliation, test the failure path, and review twice.

- [x] Close the core-ready → shell-commit interruption window with an atomic per-invocation receipt; keep signals suppressed while publishing it, and roll back if receipt preparation fails.
- [x] Verify SIGTERM after core commit but before shell assignment exits 2 and preserves the active, enabled core; verify stale receipt paths cannot be reused.
- [x] Report retained first-install enablement separately from unproven active runtime.
- [x] Treat charge percentage from a partially unreadable battery set as unknown; verify it cannot trigger or rearm failsafe.
- [x] Reject relative XDG_STATE_HOME and use the default for an empty value; isolate that variable in the installer harness.
- [x] Exercise partial file, running-state, and API rollback failures independently, preserving transaction records and reports.
- [x] Verify the installer lock remains held during panel activation and custom XDG unit paths restore after failed upgrades.
- [x] Document retained-record inspection, commit receipts, and repaired battery uncertainty semantics.

- [x] Trace every battery/discharge assignment and failsafe latch write.
- [x] Trace every failsafe entry and the sole suspend call.
- [x] Trace prevention render/action pairs and timer phase/cancel sensitivity.
- [x] Trace every panel retry source, generation guard, and runtime registration transition.
- [x] Trace every lid/brightness state and atomic-write outcome transition.
- [x] Trace core/extension staging, switching, rollback, and diagnostic retention paths.
- [x] Trace every interpreter reference, unit path, and readiness decision.
- [x] Trace every stop/release path and prove restored brightness intent precedes remote uninhibit.
- [x] Fix every pass-1 finding and rerun affected plus full checks.

## Double-check pass 2: independent complete-stack audit

- [x] Trace installer environment → core stage/commit/readiness/rollback.
- [x] Trace extension stage → first install/upgrade → Shell discovery → exact runtime proof/rollback.
- [x] Trace graphical session service start, D-Bus API, CLI, and Shell connection/retry.
- [x] Trace logind/GNOME inhibitors, lid mode, UPower, failsafe, timer, and deliberate suspend.
- [x] Trace accepted/rejected/unknown request, signals, clock gap, `s2idle`, `[deep]`, and wake.
- [x] Trace brightness journal, dim, wake/redim, owner changes, manual conflict, stop, and crash recovery.
- [x] Trace all agent/Shell/UPower/logind owner loss and replacement paths.
- [x] Cross-check README recovery instructions, paths, exit codes, and support claims against source.
- [x] Map every plan 0.5 coverage item to source plus test evidence or an explicit live gate.
- [x] Inspect final diff for stale claims, unrelated edits, unsupported support statements, secrets, and forbidden commands.
- [x] Fix every pass-2 finding and rerun affected plus full checks.

## Final requested static logical review after implementation

- [x] Perform a fresh top-to-bottom logical review without executing project code, independent of the two implementation audits.
- [x] Start from installer inputs and follow every success, failure, retry, rollback, logout, stop, restart, and owner-change branch.
- [x] Follow panel and CLI actions through D-Bus state, inhibitors, UPower, timer/failsafe, suspend transaction, wake, and brightness recovery.
- [x] Check Debian-family paths, systemd semantics, GNOME Shell lifecycle assumptions, Wayland boundaries, `s2idle`, and `[deep]` invariants.
- [x] Record every newly found logical issue and implement/fix it before goal completion.
- [x] Repeat deterministic checks after any final-review fix: not required because this separate final review found no additional confirmed defect and made no project-code change.
- [x] Record the final logical-review conclusion and any truly live-only residual risks.

## Live validation gates — remain open until physically observed

- [ ] Disposable GNOME Wayland: first install/logout discovery, visual panel, retry, runtime upgrade, and rollback.
- [ ] Debian, Ubuntu, Linux Mint, and Kali: dependencies, XDG paths, user manager, graphical target, logout/login, and restart.
- [ ] GNOME Shell 46–50: lifecycle and runtime handshake.
- [ ] GNOME 46–48 hardware: intended-panel dim/restore across lid, owner restart, docking, external displays, AC, and battery.
- [ ] GNOME 49–50: keep brightness no-write unless a safe public per-output read/write API is proven.
- [ ] Real UPower/logind/SessionManager owner loss and state 0/2/6 transitions.
- [ ] Clean stop while physically dimmed restores promptly before inhibitor release completes.
- [ ] Attended `s2idle` and `[deep]`: success/failure, countdown, failsafe, signal ordering, and final brightness.

## Final evidence record

- [x] Record exact test counts and outputs after all fixes.
- [x] Record static-search evidence and both completed review passes.
- [x] Record the separate final no-execution logical review and its disposition.
- [x] Confirm all implementation items are checked and all unperformed live gates remain unchecked.

## Historical progress evidence — superseded by final evidence below

- Agent slice: 86 deterministic tests pass, including tri-state battery aggregation, episode retention, positive recovery, version/sender lifecycle, atomic failure stages, journal commit ambiguity, shutdown-before-release, scoped lid errors, raw publication failures, and session/system disconnect setup and outcomes. Bus initialization timing and repeated/simultaneous disconnect review remain open.
- Panel slice: Node harness passes requested/effective rendering, zero-second running timer cancellation, runtime registration/unregistration, three state attempts, retry cancellation, and stale-generation/state protections.
- Installer slice: 36 isolated installer tests and 17 core-helper tests pass. The core now uses `core_install.py`; import/compile/staging checks precede switching, exact agent/API readiness gates core commit, and prior files/service state/API roll back on failure. First validated extension installs remain canonical; panel runtime proof and upgrade recovery are tested. This passing suite does not establish that the complete planned fault matrix has been audited.
- CLI slice: 4 isolated threshold-validation tests pass without a real session bus.
- Latest full discovery result: `python3 -m unittest discover -s linux/gnome-wayland/tests -q` completed with **143 tests in 74.286s, OK** (session 34065): 86 agent + 17 core-helper + 36 installer + 4 CLI tests.
- Python compile, shell syntax, JavaScript syntax, panel harness, and diff whitespace checks passed during implementation. Full fault-matrix coverage, two review passes, and the independent final static review remain open; no unchecked implementation/review subtask is completed merely by this suite result.
- No live GNOME, Debian-family distro, brightness hardware, `s2idle`, or `[deep]` validation has been performed.

### Continuation repairs and evidence (2026-10-07)

- [x] Preserve due timer conditions through consumption; refresh required lid before and after release, check final-preflight changes, and require prevention immediately before deliberate release. Test rejection/success ordering and exact dispatch counts; review twice.
- [x] Repair post-release failsafe freshness: separate power-only evidence from effective-prevention trigger, refresh without lid work after release, then apply the final shutdown/logind/cookie guard.
- [x] Verify release-time AC, unknown discharge/percentage, threshold recovery, and disablement block dispatch without retry; stable low power dispatches exactly once; reentrant shutdown/preparation blocks dispatch. Review source and assertions twice.
- [x] Reject symlink or wrong-type managed core destinations before staging; verify dangling and valid symlinks retain their original targets and no deployment commands run; review rejection twice.
- [x] Recheck shutdown/transaction/owner generation after preflight D-Bus reads; a read can deliver lifecycle changes, so stale false state must not authorize suspend. Verify property-read and owner-read reentrancy twice.
- Combined snapshot before the post-release failsafe repair: session 10069, **156 tests in 85.663s, OK** (93 agent + 22 core-helper + 37 installer + 4 CLI). This does not certify subsequent changes or completed reviews.

- Repaired missing-unit normalization: `service_snapshot` now receives program/CLI paths and accepts status 4 only with independently absent artifacts, including absence of dangling unit symlinks. Both install snapshot and rollback verification use the same policy.
- Repaired overlapping same-owner panel snapshots: every request increments the same freshness serial as signals. Older successful replies and errors cannot supersede a newer request/result. The Node harness passes reverse-reply and obsolete-error cases alongside existing signal/lifecycle coverage.
- Removed the explicit `After=graphical-session.target` dependency; staging now verifies the intended PartOf/WantedBy relationship and rejects conflicting explicit target ordering even without systemd-analyze. This avoids opposite ordering when target default dependencies apply; actual target definitions and GNOME lifecycle remain live gates.
- Repaired dangling retained-record checks in shell and helper before deployment, including shell commit receipts. Core-helper regression verifies no staging/systemctl mutation; the panel test now covers valid, truncated, empty, and dangling-symlink records before core switching.
- Clarified panel rollback output: restored files/configuration and list state do not certify runtime restoration, which is reported independently.
- Added failed dim-readback recovery evidence and a test that actual method proxies disable automatic introspection. Existing explicit three-second brightness/owner/release timeouts remain intact.
- Full snapshot before the retained-record/test additions: `python3 -m unittest discover -s linux/gnome-wayland/tests -q`, session 20487, **153 tests in 85.842s, OK**. This supersedes the historical 143-test result only for that snapshot.
- Latest focused results after subsequent repairs: agent **93 tests in 0.089s, OK**; core helper **22 tests in 0.689s, OK**; retained-panel-record test **1 test in 0.553s, OK** with four record variants; Node panel harness passed. Latest combined full discovery has not yet been run after these additions.
- `git diff --check`, `sh -n`, and ES-module syntax checks passed before the last retained-record additions; repeat final checks after source settles.
- Fresh upstream systemd fetch: sandbox DNS unavailable; escalated read-only curl was not executed because automatic approval review exceeded its retry limit with HTTP 429. This was a review-service failure, not a determination that the command was unsafe. No bypass attempted. Fresh remote verification remains unavailable; target-system definitions remain live gates.

### Plan §24 dispositions — implementation audits completed

| Audit question | Current disposition and evidence | Remaining work |
| --- | --- | --- |
| Missing unit/orphan artifacts | Reject orphan managed artifacts and dangling units before switch; core-helper absence/orphan/symlink matrix passes. | Source and assertions checked in both audits; real manager remains a live gate. |
| Panel intent interrupted while writing | Exclusive canonical creation/fsync before switch. Partial/published SIGTERM cases retain a blocking record; valid/truncated/empty/symlink retry cases stop before core deployment. Automatic recovery is not promised. | Both audits completed; real power-loss behavior is not certified. |
| Receipt durability | Process coordination only; file fsync and rename precede shell recognition. Preparation/rename failures roll back; post-helper interruption exits 2 and preserves the core. | Both audits completed; no directory-durability/power-loss guarantee. |
| Files restored/runtime unproven | File/configuration/list state and exact runtime proof have separate reports; mismatch/first-install matrices verify their outcomes. Archival settles the file transaction only. | Both audits completed; real Shell reload remains a live gate. |
| Graphical target ordering | Removed opposite explicit After dependency; validator enforces PartOf/WantedBy and rejects conflicting ordering. | Both audits completed; actual target definitions/environment/logout stay unobserved. Fresh fetch unavailable as recorded above. |
| Overlapping replies | Requests/signals share a freshness serial per generation; reverse replies, obsolete errors, signals, owner change, and teardown assertions pass. | Both audits completed; real Shell lifecycle remains a live gate. |
| dbus-python contracts | Explicit policies, connectivity checks, timeout arguments, and introspect=False source/assertions checked; upstream mirror evidence retained in plan §23. | Both audits completed; installed bindings and actual disconnect delivery remain live gates. |
| Truncated source ranges | Complete functions re-read, including reply/transaction/StartTimer endings. Per-artifact move/interruption, parser, prior-state/interpreter, and runtime fault assertions inspected. | Both audits completed; final static review recorded separately below. |

The maximum explicit shutdown remote-call trace is first restore Get/Set/Get (9s), owner lookup/Uninhibit (6s), and optional release reconciliation Get/Set/Get (9s): 24s. `Agent.proxy` uses introspect=False; stop does not probe a new adapter. Identity/journal I/O and scheduling are additional, and physical elapsed time is not verified. Genuine programming/allocation failures remain visible rather than being swallowed as transport failures.

## Completed implementation audit evidence (2026-10-07)

Pass 1 traced mutable state, sole suspend dispatch, FD/cookie ownership, disk replacement outcomes, every installer switch/rollback, interpreter/unit resolution, and panel sources/generations. Pass 2 independently followed installer inputs through deployment, service startup, Shell/CLI API, battery/lid/inhibitors, timer/failsafe, sleep replies/signals/clocks, brightness recovery, stop, restart, and owner replacement. Complete source functions were read; test names alone were not treated as assertions. The continuation repairs (power freshness, preflight lifecycle, destination types, and conditional timer freshness) were revisited in both passes against their failure and success assertions.

### Original findings: requirement → source → assertion mapping

| Findings | Source reviewed in both passes | Deterministic evidence |
| --- | --- | --- |
| 1: Unknown battery rearm | `refresh_power`, `failsafe_condition_met/cleared`, `check_failsafe`, `sleep_now` | Aggregate states, partial battery, unknown-low sequences, positive recovery, explicit configuration, release/preflight changes; exact Suspend counts. |
| 2: Requested prevention | Panel `_render`, prevention toggle, `_call`; `SetPrevention` | Desired-on/effective-off renders on, sends false when canceled, and rejected actions return to authoritative state. |
| 3: First canonical install | Shell `rollback_panel`, initial switch | First discovery/enable/list/runtime failure retains canonical files; simulated next-login discovery; enablement is separately reported. |
| 4: Loaded runtime proof | Registration API, panel lifecycle, `STATE_SCRIPT`, `ready`, shell runtime/rollback checks | Exact/delayed/blank/malformed/older/newer/unregistered runtime reports, old-runtime rollback, owner loss and disable. |
| 5: Core rollback | Helper stage/snapshot/intent/install/rollback; shell lock/receipt | Every core move and both interruption sides, first failure, successful upgrade, partial file/enable/running/API rollback, retained records. |
| 6: Readiness race | `Type=dbus`, `ready`, typed `state` probe | Missing state/ownership, stale version, incompatible API, delay, total deadline and rollback assertions. |
| 7: Stop order | `stop`, shutdown dim predicate, `release`, explicit timeouts | Restore before remote uninhibit; success, timeout, read/write/readback, owner lookup and release faults, local FD cleanup, repeat/disconnect. |
| 8–9: Paths/interpreters | XDG validation, selected launchers, unit escaping, prior interpreter | Custom config install/rollback, relative config/data/state rejection, literal launcher execution, changed interpreter rollback. |
| 10: Atomic visibility | `atomic_json`, settings/journal reconciliation | Open/write/file-fsync/replace/directory-open/fsync/close injections; memory equals visible disk, uncertain journal blocks unsafe dim. |
| 11: Zero-second Cancel | Panel timer phase/signal/render, agent timer state | Running at zero remains cancelable; idle/canceled/consumed disables Cancel; newer countdown supersedes old snapshot. |
| 12: Scoped lid errors | `set_lid_error`, setters/owner changes, panel diagnostics | Disabled/successful lid clears only its error; unrelated persistence/brightness text remains. |
| 13: Bootstrap retry | `_refresh`, `_stateFailed`, retry cancellation/generation | Exactly three attempts including synchronous failure, signals/replies, exhaustion, owner change, disable, reverse replies/obsolete errors. |
| 14: Healthy dim state | `brightness_state`, recovery predicate, panel render | Applied/healthy, prepared, restore/redim, invalid/unreadable, idle and manual conflict states; panel labels. |

### Amendments and preserved invariants

- Plan §7.4's ten transaction requirements: reviewed in helper/shell source and isolated helper/installer assertions. Unit-verifier unsupported syntax differs from actual failure/timeout; optional Node absence is reported; final backup archival cannot undo proven deployment. Lock identity and lock retention during panel activation are asserted. No real HOME, user manager, GNOME profile or bus is used by the deterministic harness.
- Plan §12.6's 14 rows: each mapped to the existing agent, CLI, panel, helper or installer assertion matrix. Raw readiness types are checked before coercion; CLI thresholds reject invalid values without dispatch; publication transport failures retain committed state/cleanup; explicit disconnect policy precedes startup traffic. Exact dim readback, timeout budgets and partial rollback reports were traced separately.
- Plan §24's eight decisions: dispositions recorded above. Managed destination symlink/type matrix preserves all original entries before staging. Preflight reads cannot authorize changed lifecycle/generation/cookie state.
- Conditional countdown amendment: snapshots flags before consumption, checks fresh lid before/after release and after final preflight; prevention is required before deliberate release. Cached/fresh lid rejection preserves prevention; post-release rejection leaves it off without retry; stable conditions dispatch once. Four tests include two initial lid states and four release/preflight changes.
- Plan-0.4 regression: sole `Suspend(False)` site, accepted/rejected/unknown one-shot handling, persistent uncertainty latch, owner-bound cookie/retry, machine/output/journal/current-value gates, and manual conflict preservation remain covered. Boottime/monotonic simulations apply to `s2idle` and `[deep]`; they are not hardware proof.

### Final deterministic and static verification

- Complete repaired suite: `python3 -m unittest discover -s linux/gnome-wayland/tests -q`, session 37133: **169 tests in 94.426s, OK** (103 agent + 24 core-helper + 38 installer + 4 CLI).
- Snapshot immediately before conditional timer amendment: session 21685: **165 tests in 89.064s, OK**. Historical session 82807 passed **162 tests in 87.221s**; earlier counts above remain historical only.
- Focused timer-amended agent suite: **103 tests in 0.096s, OK**. Inherited focused core helper: **24 tests in 0.545s, OK**. Interrupted panel intent publication test passed both partial and published cases.
- `node linux/gnome-wayland/tests/test_extension.mjs`: **Panel connection-state harness passed**.
- In-memory Python compilation passed for all seven agent/CLI/helper/test files (equivalent syntax check without importing them or creating bytecode); metadata parses with UUID `sleep-disabler@local`, version 5, Shell entries 46–50.
- `sh -n`, JS ES-module syntax, and `git diff --check` passed. Source searches found exactly one executable Suspend at `agent.py`'s shared transaction; no `rm` command in implementation/tests; no installed-interpreter or unit-path bypass; Cancel follows phase and rearm follows positive evidence.
- No live distro, Shell, display hardware, firmware, real-bus, `s2idle` or `[deep]` result is inferred from these checks. No commit or deployment was performed.


## Separate final source-only logical review (2026-10-07)

This review followed the final repaired suite and both implementation audits. It used source reads, searches, and diff inspection only: no agent, installer, CLI, project tests, or syntax tooling was executed during this review. Reading existing test logs is evidence inspection, not a new run.

- Deployment: traced environment/dependencies/interpreter and XDG inputs, inherited lock, supported destination types, staging, explicit prior manager state, durable intent, all artifact moves, readiness, receipt boundary, signal handling, rollback dimensions, retained records, and the optional panel phase.
- Panel and CLI: traced Shell status-area insertion, unique-owner subscriptions, ordered snapshots/signals, three initial attempts, requested/effective controls, zero-second cancellation, synchronous/asynchronous failures, version registration, teardown, and CLI validation before unsigned conversion.
- Agent: traced startup disconnect policy and initialized cleanup state, settings visibility/durability outcomes, GNOME cookie and logind FD ownership, lid state, conservative UPower evidence and positive episode rearm, required timer conditions, fresh post-release observations, lifecycle preflight, and the single Suspend dispatch.
- Sleep and recovery: traced accepted/rejected/unknown replies, transaction identity, owner generations, prepare signals, monotonic/boottime gap, external sleep, unresolved-state blocking, consumed countdowns, wake reconciliation, and no automatic second suspend. Neither `s2idle` nor `[deep]` requires changing the configured kernel sleep state; actual firmware/clock/signal behavior remains a hardware gate.
- Brightness and shutdown: traced adapter/topology identity, prepared/applied journal, exact dim readback, disk reconciliation, manual-conflict preservation, restore/redim, unavailable service, stop-before-uninhibit ordering, bounded remote calls, FD cleanup, disconnect, and restart recovery. The 24-second explicit remote-call budget excludes filesystem/scheduling delay and is not a measured shutdown guarantee.
- Cross-stack contracts: rechecked unit PartOf/WantedBy/Type=dbus, dependency assumptions, installed launcher paths, runtime/API agreement, exit codes, recovery instructions, original 14 findings, 14 follow-up fault categories, eight audit decisions, and all continuation amendments.

Conclusion: no additional confirmed logical defect was found in these reviewed paths. No further code repair or test rerun was required after this review; the final 169-test result above remains the applicable repaired snapshot. This is a bounded source review, not a guarantee that every bug is absent. All eight live checklist entries remain open, including installed D-Bus delivery, actual user-manager/Shell lifecycle, Debian/Ubuntu/Mint/Kali, real panel mapping, and attended `s2idle`/`[deep]` trials. Fresh upstream fetch remained unavailable as recorded in historical evidence.

Final reconciliation: implementation and both audit checklists are checked against the recorded source/assertion evidence; live gates are unchecked. The ledger has been retained, not cleared. No commit or deployment was performed.
