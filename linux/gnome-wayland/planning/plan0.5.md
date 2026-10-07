# Plan 0.5 — GNOME Wayland truthfulness, deployment transactions, and final static-review repairs

Status: **local implementation, deterministic verification, both implementation audits, and the separate final source-only review completed**. This plan follows [plan 0.4](plan0.4.md) and addresses all 14 findings recorded from the complete post-plan-0.4 logical review. The retained [task ledger](../task.md) maps repairs to source and assertion evidence and records the final 169-test result. Live GNOME Shell, Debian-family installation, brightness, `s2idle`, and `[deep]` validation remains unperformed. These results do not establish production compatibility or absence of all bugs.

The work is one coordinated release because the agent state schema, panel behavior, extension runtime handshake, installer health check, service type, and isolated installer harness must agree. It retains the existing per-user design and requires no privileged helper or kernel sleep-state change. This revision also incorporates the subsequent source-review findings about mismatched dim readback and implicit shutdown call timeouts; §24 makes the remaining audit questions explicit acceptance gates.

## 1. Review findings covered by this plan

These describe the source at the preceding review, before the partial plan-0.5 repairs. They are the coverage baseline, not a claim that every defect remains present today.

### P0 findings

1. UPower state `0` (unknown) and state `6` (pending discharge) currently collapse to `battery_discharging=False`. That can look like positive recovery, clear `failsafe_triggered`, and allow a second failsafe suspend request after an ambiguous first request.
2. When prevention is requested but temporarily ineffective (`desired=true`, `enabled=false`), the panel shows an off switch. Clicking it sends `SetPrevention(true)` again, so the panel cannot cancel reconnection.
3. A validated first extension install is moved out of its canonical UUID directory when Shell has not discovered it yet. Logging out cannot make Shell discover a directory that the installer removed, contradicting the recovery instructions.
4. Upgrade checks prove that the extension UUID is enabled and active, but do not prove that GNOME Shell loaded the newly installed JavaScript instead of retaining the old imported module.
5. `agent.py`, `ctl.py`, the CLI wrapper, and the systemd unit are overwritten before the new core is validated or health checked. A bad update destroys the prior working core even though extension files have rollback handling.
6. `Type=simple` plus `systemctl is-active` has a readiness race: the unit can be active before the process owns `org.sleepdisabler.App`, and the installer performs only one immediate API call.
7. Clean stop can wait in GNOME `Uninhibit` while a healthy closed-lid dim remains applied. The first stop reconciliation still sees effective prevention enabled, and the later brightness restoration can be delayed until most of `TimeoutStopSec=30` has elapsed.

### P1 findings

8. The user unit path is hard-coded to `~/.config/systemd/user` and ignores `XDG_CONFIG_HOME`.
9. The installer validates `$PYTHON`, while the installed unit and CLI wrapper execute `/usr/bin/python3`. Validation therefore does not prove that the installed interpreter has `dbus` and `gi`.
10. `atomic_json()` can replace the destination and then raise on parent-directory `fsync`. Callers treat that as a failed write and leave in-memory state behind the file that is already visible on disk.
11. A still-owned overdue countdown publishes zero remaining seconds, causing the panel to disable **Cancel countdown** even though `CancelTimer` is still meaningful.
12. Turning failed lid mode off can leave stale lid failure text in the general error/status output.
13. After the initial `GetState` call fails, the panel has no explicit bounded retry for the current owner. Recovery depends on a later state signal or name-owner cycle.
14. `brightnessRecoveryPending=true` currently means any brightness journal exists. A valid, intentionally applied closed-lid dim therefore looks like failed recovery even when no restoration should presently occur.

## 2. Non-negotiable invariants

1. There remains exactly one executable login1 `Suspend(false)` call, protected by the plan-0.4 preflight and sleep transaction.
2. Unknown UPower information never triggers failsafe sleep and never supplies the positive evidence needed to rearm a consumed failsafe episode.
3. One low-battery episode can dispatch at most one request, including a request with unknown D-Bus delivery. Only explicit user reconfiguration or positively observed recovery can rearm it.
4. Panel switches represent the user's requested settings. Status text and icons separately represent effective acquisition, degradation, release, and recovery.
5. The installer never reports a new extension version active unless the running extension code reports that exact runtime version.
6. A validated first install that merely needs Shell discovery stays in the canonical UUID directory across logout/login.
7. A failed core upgrade restores the previous core files, unit contents, enabled state, running state, and API usability when a prior healthy install existed.
8. The interpreter validated during installation is the interpreter used by both the installed agent and installed CLI.
9. On shutdown, brightness enters a restore-required desired state before any potentially blocking remote `Uninhibit` call.
10. After an atomic replace, memory and published state must acknowledge the destination content even if directory durability could not be confirmed.
11. A healthy applied dim is reported as owned/dimmed, not as recovery failure. A journal that needs an action or has an unresolved error is reported as recovery pending.
12. Existing plan-0.4 sleep reconciliation, inhibitor ownership, journal identity validation, manual-change preservation, and `CLOCK_BOOTTIME - CLOCK_MONOTONIC` handling remain intact for both `s2idle` and `[deep]`.

## 3. P0 — Make failsafe battery evidence tri-state

### 3.1 Represent discharge evidence explicitly

Replace `battery_discharging` as a two-state boolean with an explicit internal classification such as:

- `discharging`: every condition required to treat the battery as actively draining has positive evidence;
- `not-discharging`: the nonempty relevant battery set is completely readable and every battery has an established charging/charged/pending-charge state; AC and percentage recovery are separate failsafe-clearing evidence, not battery discharge-state values;
- `unknown`: no positively discharging battery is known and UPower is unavailable, there is no usable system battery, enumeration is partial, a state is `0` (unknown) or `6` (pending discharge), or a needed property cannot be read. A known discharging battery takes precedence for discharge classification under the aggregation rules below, while percentage completeness is checked separately.

Use the UPower `Device.State` values deliberately. State `2` is discharging; `1` (charging), `4` (fully charged), and `5` (pending charge) provide non-discharging evidence. States `0` (unknown), `3` (empty), and `6` (pending discharge), plus unrecognized values, remain unknown for this safety decision. An empty battery is not positive evidence of recovery. Do not infer recovery from “state is not 2.” Publish a string field such as `batteryDischargeState` so the CLI and tests can observe all three classifications. Keep any compatibility boolean only as a derived value and never use it for rearming.

For multiple batteries, derive a conservative aggregate:

1. If any usable power-supply battery is positively discharging, classify the aggregate as discharging.
2. Otherwise, if the nonempty system battery set is completely readable and every battery has a positively non-discharging state, classify it as not-discharging.
3. Otherwise classify it as unknown.

An empty or partially unreadable battery set is not positive recovery evidence. If any enumerated system battery cannot be read, publish the aggregate percentage as unknown rather than presenting the readable subset as the complete battery charge. A known discharging device can still establish the aggregate discharge classification, but unknown aggregate percentage prevents both the low-charge trigger and percentage-based rearm. Positive AC evidence remains a separate valid clearing condition.

### 3.2 Separate trigger evidence from rearm evidence

Create pure helpers or equivalent centralized predicates:

- `failsafe_condition_met()`: failsafe enabled, prevention effective, `OnBattery is True`, discharge state exactly `discharging`, a valid percentage exists, and percentage is strictly below the threshold;
- `failsafe_condition_cleared()`: positive evidence of at least one safe clearing condition: failsafe explicitly disabled/reconfigured, `OnBattery is False`, valid percentage at/above threshold, or `OnBattery is True` with valid percentage and discharge state exactly `not-discharging`;
- unknown values return false from both predicates.

`check_failsafe()` may clear `failsafe_triggered` only through `failsafe_condition_cleared()` or an explicit `SetFailsafe` action. A refresh exception, UPower owner loss, state `0`, state `6`, missing percentage, or missing batteries preserves the latch. The fresh read immediately before suspend must require the same exact positive trigger evidence.

### 3.3 Preserve the latch across ambiguous sequences

Inhibitor release can wait on D-Bus after the initial battery refresh. For a failsafe request, refresh UPower again after confirmed release with lid reconciliation disabled, then require the same positive power evidence before the final suspend preflight. Factor the power-only predicate from the effective-prevention trigger: release intentionally sets `enabled=false`, so the final check must not require an inhibitor that has just been removed. AC, unknown data, threshold recovery, or failsafe disablement during release must prevent dispatch. Preserve the consumed episode latch; do not reacquire prevention or retry automatically. Test each change during release, stable low power with exactly one dispatch, and shutdown/preparation delivered during the last refresh. The final suspend preflight must follow this refresh.

Cover these sequences without a second `Suspend` call:

1. Low/discharging request → unknown D-Bus result → state `0` → low/discharging again.
2. Low/discharging request → UPower exception → service return still low/discharging.
3. Low/discharging request → state `6` → state `2` still below threshold.
4. Low/discharging request → no batteries observed temporarily → same low battery returns.
5. Low/discharging request → percentage unknown → same low percentage returns.

Rearm only after positive AC, threshold recovery, a positively non-discharging battery state with valid context, or an explicit settings action. Test rearming separately for each permitted path.

## 4. P0 — Make panel controls represent requested state and remain recoverable

### 4.1 Requested prevention versus effective prevention

In `_render()`:

1. Set the prevention toggle from `desired`, not `enabled`.
2. Continue using `enabled`, inhibitor state, and `gnomeReleasePending` for status and icon selection.
3. With `desired=true` and `enabled=false`, render the switch on, show the existing reconnecting/degraded warning, and keep it sensitive when no release operation forbids action. Clicking it off must call `SetPrevention(false)` and cancel reconnection.
4. With `desired=false` and a release pending, render the switch off and show release pending. Disable repeat toggles only while the release state makes a new request invalid.
5. On an action failure, refresh authoritative state and return the visual switch to the agent's requested state.

Do not overload one boolean to represent preference, effective ownership, and transition state.

### 4.2 Cancel an overdue but still-owned timer

Derive cancel sensitivity from authoritative timer ownership, primarily `timerPhase == "running"`, instead of `timerRemaining > 0`. A countdown can remain running between its deadline and transaction reconciliation while the rounded remaining value is zero. `TimerChanged(0)` must update the label without erasing the phase from the most recent state snapshot. Cancel stays enabled until a `StateChanged` update moves the phase to `consumed`, `canceled`, or `idle`.

### 4.3 Add bounded current-owner `GetState` retry

Add a retry state that belongs to the current owner generation:

1. Attempt `GetState` immediately after name appearance.
2. If it fails before any authoritative `StateChanged`, schedule at most two more attempts at documented delays, for example one and two seconds.
3. Keep controls disabled and show “connecting/retrying” during those attempts.
4. A successful reply or valid current-generation state signal cancels the retry source and establishes connected state.
5. After the final failure, show a stable disconnected/error state. A later valid state signal for the same current owner may still recover it.
6. Owner disappearance, generation change, extension disable, or successful connection cancels any scheduled retry. There can be only one retry source.
7. Stale callbacks and retry sources from old generations cannot alter state or sensitivity. Keep the generation counter monotonic across `disable()`/`enable()` on the same extension instance; resetting it can make old callbacks match a new lifecycle. Bind action replies and signals to the unique agent owner as well as the lifecycle generation, and prevent an older `GetState` reply from overwriting a newer authoritative state signal.

The exact attempt count and delay must be constants covered by the Node harness, not implicit GLib behavior.

Handle synchronous exceptions from `Gio.DBusConnection.call` as well as errors from `call_finish`. A synchronous `GetState` send failure consumes one of the same three bootstrap attempts; it must not escape the lifecycle callback or create an extra retry source. A synchronous action failure shows its diagnostic and requests authoritative state without changing the requested setting. Runtime registration/unregistration failure must still allow local teardown. Increment the state freshness serial for `TimerChanged` as well as `StateChanged`, so an older `GetState` response cannot overwrite a newer countdown. Test failure, recovery, owner replacement, and disable/re-enable for each path.

### 4.4 Remove stale lid diagnostics

Give lid errors domain-specific ownership, preferably a published `lidError` field. Set it when lid inhibitor acquisition or verification fails. Clear it when:

- lid mode is explicitly disabled;
- a later acquisition succeeds;
- the relevant owner loss changes the diagnostic to a new current result.

The panel shows lid diagnostics only while lid mode is requested or while a current lid action itself failed. Disabling lid mode must not leave “Lid lock unavailable” in the general status. Avoid clearing unrelated agent, sleep, persistence, or brightness errors.

## 5. P0 — Prove which extension code is running

### 5.1 Add a runtime version handshake

Give the extension source a constant runtime version that is bumped with each deployable extension revision. Keep it consistent with `metadata.json`'s numeric/string version through a static installer check.

Add exact methods such as `RegisterPanelRuntime(s version)` and `UnregisterPanelRuntime(s version)`. Use the D-Bus sender keyword to bind a registration to the caller's unique session-bus name and the current agent process. Reject an empty or malformed version, ignore an unregister from another sender/version, and clear the registration if that unique bus owner disappears. Publish at least:

- `panelRuntimeVersion`;
- `panelRuntimeRegistered`;
- optionally the agent-side time/generation needed for diagnostics, without exposing unstable internal timestamps as API guarantees.

The extension registers from `enable()` after its panel objects and agent watch are initialized, and unregisters from `disable()` when possible. It must also register again after the agent owner returns because an agent restart forgets prior reports. Registration failure affects installer proof and status reporting but must not break the user's ability to disable the extension.

An agent restart begins with no panel registration. This is essential during the transition from plan 0.4 because an already loaded old extension cannot satisfy the new handshake. A stale extension module can report only its compiled old version. Enabled/active list membership and on-disk metadata are supporting checks, not substitutes for this runtime report.

### 5.2 Verify the handshake during install

After enable/re-enable:

1. Verify discovery, enabled-list membership, and active-list membership as today.
2. Poll one coherent agent `GetState` snapshot for a bounded interval until `panelRuntimeRegistered` is exactly true and `panelRuntimeVersion` exactly equals the staged extension's expected version. Also require the committed agent runtime/API version; a version string alone, an unregistered report, or a replaced incompatible agent cannot prove deployment success.
3. Treat blank, older, newer, malformed, or timed-out reports as “new JavaScript not proven loaded.”
4. For an upgrade with a previous extension, enter rollback and restore the previous directory and previous enabled/active state. If the old runtime can be restored, verify its expected old version when it is available in old metadata; otherwise report that runtime restoration remains unproven and require logout.
5. If Shell cannot unload the old module cleanly, report that logout/login is required. Never label the new code active from enabled/active lists alone.

The isolated installer harness must model an old runtime report, a new runtime report, no report, delayed report, stale report after re-enable, and rollback to the old report.

Registration is a cooperative lifecycle/version report, not authentication of GNOME Shell: another same-user bus client can call the method. Document that boundary. Bind reports to a live unique sender, clear them on its loss, and test mismatched-sender unregister and owner replacement. Do not make a security claim from a version handshake. Read the registered flag and version together rather than through separate field calls that can observe different owners or snapshots.

Validate the original D-Bus field types before converting the snapshot to JSON. In particular, string `"false"` must not become registered through `bool()`, and a string, fractional number, or boolean must not become an accepted integer API version through `int()`. Runtime versions require strings and registration requires a boolean. If legacy snapshots omit new fields, use explicit documented defaults only for that absence; a present malformed field fails the probe. Apply the same validation to core readiness, panel readiness, and rollback proof. Tests must exercise the probe conversion boundary, not merely inject already-normalized JSON.

## 6. P0 — Keep validated first installs discoverable

Distinguish failures before and after the staged directory becomes a valid canonical install:

1. Metadata, UUID, required-file, syntax, and runtime-version consistency failures occur before switching and may retain the invalid staging directory under a diagnostic name.
2. Once a validated first install has been moved to `$XDG_DATA_HOME/gnome-shell/extensions/sleep-disabler@local`, a discovery, enable, active-list, or runtime-handshake failure must leave that directory in place.
3. On that first-install path, exit `2`, state that the service/CLI are usable, and give truthful recovery steps: log out/in, run `gnome-extensions enable sleep-disabler@local`, and rerun the installer or inspect runtime status.
4. Do not claim rollback when no previous extension existed. Report configured enablement separately from active/runtime proof, including enabled-but-unproven, disabled, and unknown outcomes; do not infer that the extension is disabled merely because loaded runtime proof failed. If enabled state is ambiguous, report it separately without removing the canonical files.
5. On upgrade, continue retaining the failed new copy under a diagnostic name while restoring the previous canonical directory.

Tests must prove that a first discovery failure leaves both `metadata.json` and `extension.js` in the canonical UUID directory and that a simulated next session can discover and enable it.

## 7. P0 — Make the core installation transactional

### 7.1 Define the core transaction boundary

Treat these artifacts as one versioned core deployment:

- installed `agent.py`;
- installed `ctl.py`;
- the agent launcher;
- `~/.local/bin/sleep-disablerctl`;
- the user unit under the resolved systemd user-unit directory.

Stage all new files before replacing any active file. Validate from staging:

1. both Python modules compile with the selected interpreter;
2. the selected interpreter imports `dbus` and `gi`;
3. launchers are syntactically valid and reference the selected absolute interpreter and intended installed paths;
4. the unit contains the intended `Type`, `BusName`, `ExecStart`, restart policy, and timeouts in their correct sections; `CollectMode=inactive-or-failed` belongs in `[Unit]`, not `[Service]`, and the structural fallback must reject the wrong section;
5. `systemd-analyze --user verify` is used when supported, with a deterministic structural fallback in the isolated harness;
6. required modes and files are present.

Give the agent its own immutable release/runtime version plus an integer D-Bus API schema version in `GetState`. The installer reads the expected values from the staged source or a single validated version manifest. Core health requires the exact new agent runtime version and a compatible API schema, not merely any process responding on the well-known name. Keep the agent and panel versions distinct so a partially updated deployment is diagnosable.

No service restart, active-file move, or daemon reload occurs before staging validation succeeds.

### 7.2 Capture and restore prior state

Before switching, capture independently:

- whether each prior artifact exists;
- whether the service is enabled;
- whether it is active;
- whether the prior API responds;
- the prior extension runtime version for later panel rollback proof.

Move prior artifacts to unique rollback locations on the same filesystem, move staged artifacts into place, reload the user manager, restore the intended enablement, and restart/start the service. Do not use `rm`; use `mv`, `bin`, or retained diagnostic paths throughout.

If new core readiness fails:

1. stop the failed new service if needed;
2. move failed new artifacts to diagnostic paths;
3. restore every prior artifact;
4. run `daemon-reload`;
5. restore the previous enabled/disabled state;
6. restore the previous active/inactive state rather than unconditionally starting it;
7. if the previous service was active and API-healthy, require it to become API-healthy again;
8. report separately whether file rollback, enablement rollback, running-state rollback, and API rollback succeeded.

If no previous core existed, retain useful failed files for diagnosis, leave no false enabled/healthy claim, and exit `1`. A panel-only failure after a healthy core transaction remains exit `2`.

### 7.3 Order core and panel commits

Complete and verify the core transaction first because the new runtime handshake depends on the new API. Only then begin the extension transaction. After the core is proven healthy, a panel failure does not roll the core back; the CLI remains usable and the installer exits `2`. Document this commit boundary explicitly.

Close the interruption window between helper readiness/commit and the shell assigning its `core_committed` flag. Publish an atomic per-invocation commit receipt only after exact core readiness, with signals suppressed during publication. Flush/fsync the prepared file before renaming it; receipt preparation/publication failure must enter core rollback. The shell's signal and helper-failure paths consult the published receipt so interruption after commit reports exit `2` and preserves the committed core. Refuse a pre-existing receipt path before starting the deployment, retain receipts for inspection, and document their contents/location. A receipt coordinates these processes; it does not make the deployment crash-atomic. Test interruption before readiness, receipt preparation failure, and SIGTERM immediately after helper success but before shell assignment, asserting service state and exit classification.

### 7.4 Close transaction failure and interruption gaps

1. Preserve the prior launcher's interpreter identity separately from the newly selected interpreter. Probe prior API health and rollback readiness through a validated prior interpreter when available. Changing `PYTHON` must not turn a healthy old service into a false “prior API unavailable” result. Never execute arbitrary launcher text to discover its interpreter; use a narrowly parsed generated launcher or a deployment manifest. If the prior interpreter cannot be determined safely, report prior API recovery as unproven rather than claiming success.
2. Capture prior service state as known enabled/disabled and active/inactive, or explicitly unknown. A command timeout, unavailable user manager, masked/static unit, or unexpected response must not silently become ordinary disabled/inactive. Before mutation, either establish a supported restorable state or stop with a precise diagnostic. Record actual installed artifact presence independently of service queries.
3. Track each move before the next mutation so a failure between backup and replacement still restores that target. Rollback attempts all dimensions even when one fails; reports distinguish failed, restored, and not required/unproven. Verify actual enablement/running state after restoration instead of relying only on successful commands.
4. Handle catchable SIGINT/SIGTERM during the mutation window with the same rollback policy; shell interruption after core commit follows the panel transaction policy. Prevent a second interruption from recursively entering rollback. SIGKILL or power loss cannot execute a handler: retain a transaction record/backups that identify incomplete switches, and detect them on the next install before overwriting rollback evidence. Do not promise that several separate filesystem moves are crash-atomic.
5. Route every panel-stage failure after core commit through exit `2`, including directory creation, `mktemp`, file copy/mode, validation subprocess, switch, and GNOME command failures. A bare `set -e` exit must not misclassify a usable committed core as a failed core install.
6. A failed optional backup-renaming/diagnostic housekeeping operation after proven deployment must retain its existing backup and print a warning. It must not undo a healthy deployment or misreport core readiness. A final readiness failure is a deployment failure and follows the panel policy.
7. Use bounded subprocess timeouts and an overall monotonic readiness deadline. Document both retry count/delays and worst-case duration including API calls and systemctl operations; ten attempts do not imply a ten-second bound. Rollback readiness has its own finite deadline.
8. Restrict the `systemd-analyze` fallback to a positively identified unsupported command/option. Generic “not supported” text can describe an invalid unit and must not suppress a real validation failure. Test supported success, unsupported verifier, invalid unit, and verifier timeout separately.
9. Decide JavaScript syntax-validation requirements explicitly: use an available parser, or document syntax validation as skipped and require loaded runtime proof for success. Do not claim JavaScript syntax was checked when Node was absent. Missing optional Node must not become an undocumented Debian runtime dependency.
10. Validate `XDG_CONFIG_HOME`, `XDG_DATA_HOME`, and `XDG_STATE_HOME` as absolute when supplied, or reject invalid relative values before mutation with a clear message. Unset/empty values use their documented defaults. Validate state-path resolution in the agent as well as the installer, and isolate all three variables in the harness. Quote generated shell paths and escape systemd command-path syntax independently, including spaces, quotes, backslashes, `%` specifiers, and `$` expansion. Test behavior, not only launcher string contents.

### 7.5 Serialize deployment and preserve interrupted-install evidence

Use one per-user installer lock for the entire core-and-panel deployment, including capture of prior state and final runtime proof. Taking separate locks for the two phases permits another installer to replace the core between panel checks. Validate an inherited lock descriptor against the expected lock file, keep it open through child completion, and forward catchable interruption to the deployment child so it can perform rollback. A concurrent invocation must fail clearly before switching files.

Persist a transaction intent record before switching artifacts. Include target, stage, backup, prior presence, prior service state, and enough version/interpreter information to interpret rollback. Record backup/replacement intent before the corresponding rename so interruption immediately after a rename does not lose the restoration path. Test interruption at both sides of every move.

Archive completed records only after the corresponding deployment or rollback outcome is established. A partially restored transaction keeps its original record and backups. The next invocation detects that record and stops before another switch. Detection is not automatic crash recovery: document that distinction and give an inspection procedure identifying the retained paths and independently unproven file/service/API dimensions. Do not instruct the user to discard the record or retry blindly.

Never move a previous extension backup into an existing canonical directory: directory-to-directory `mv` can nest the backup instead of restoring it. If retaining the failed canonical copy fails, preserve both that copy and the old backup, retain the record, and report rollback incomplete. Diagnostic archival failure after successful deployment is only a warning; failed restoration is a deployment failure.

## 8. P0 — Remove the service readiness race and interpreter mismatch

### 8.1 Make D-Bus ownership part of service readiness

Change the unit to:

- `Type=dbus`;
- `BusName=org.sleepdisabler.App`;
- an explicit bounded `TimeoutStartSec` long enough for normal session-bus startup but short enough for installer rollback;
- the existing restart and stop policies unless testing identifies a concrete conflict.

With `Type=dbus`, systemd does not consider startup complete until the process owns the declared name. The installer must still perform bounded API polling because ownership alone does not prove that `GetState` is callable and compatible. Poll service state plus `GetState` until the exact staged agent runtime version and compatible API schema respond, or until a documented deadline, rather than making one immediate call.

The agent must continue requesting the name with `do_not_queue=True`; duplicate agents fail instead of waiting behind the active owner. Tests must cover delayed ownership, service active without an API response, process exit before ownership, timeout, and eventual success.

### 8.2 Use the interpreter that was validated

Resolve `${PYTHON:-/usr/bin/python3}` to a stable executable path before staging. Generate an installed agent launcher and CLI launcher that execute that exact interpreter with safely quoted paths. Point `ExecStart` at the installed agent launcher. Do not validate one interpreter and silently install `/usr/bin/python3` commands.

Validate the generated launchers under paths containing spaces and shell metacharacters in the isolated harness. The launchers must not evaluate command substitutions or environment-provided code. On reinstall with a different explicit `$PYTHON`, the transaction updates both launchers together.

### 8.3 Honor the XDG user-unit path

Resolve the unit directory as:

```text
${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user
```

Use the same resolved path for staging, backup, install, diagnostics, and tests. Keep the program/CLI locations documented; do not silently claim full XDG relocation for paths that remain under `~/.local`.

## 9. P0 — Restore brightness before remote inhibitor release on stop

### 9.1 Add an explicit shutdown desired state

Introduce `shutting_down` or an equivalent explicit target override. At the start of `stop()`:

1. mark shutdown/restored-brightness desired state;
2. prevent any new acquire, timer, retry, or dim operation;
3. cancel the countdown and local retry sources;
4. refresh/load enough journal state to perform the existing identity-safe restoration;
5. attempt brightness restoration;
6. only then call GNOME `Uninhibit` or any other potentially blocking remote release;
7. close local inhibitor FDs, publish/record final diagnostics where the bus is still usable, and quit.

Do not falsify `enabled` merely to force restoration while inhibitors are still held. The brightness desired-state helper should explicitly return restored during shutdown. `release()` may perform an idempotent second reconciliation, but it cannot be the first opportunity to restore.

### 9.2 Bound and test shutdown ordering

Use the existing conservative journal/current-value checks. If brightness cannot be verified or restored, retain the journal and proceed with inhibitor cleanup; shutdown must not hang indefinitely trying brightness writes. Test exact call order with a fake `Uninhibit` that blocks or raises: the brightness restore/write attempt must occur before entry into the fake remote call. Also cover no journal, manual conflict, unavailable brightness service, release pending, forced disconnect, SIGTERM, and direct `stop()`.

`TimeoutStopSec=30` remains an outer systemd bound, not the mechanism that schedules restoration.

### 9.3 Bound each recovery call explicitly

Use one documented recovery-call timeout, initially three seconds, on brightness introspection, property reads/writes, SessionManager owner lookup, and `Uninhibit`. Do not rely on dbus-python's implicit timeout. Owner lookup must use a proxy method that accepts an explicit timeout. Trace every helper reached by `stop()` to identify hidden proxy/introspection traffic; either bound those calls as well or disable automatic proxy introspection when the method contract is already known.

Count calls for each shutdown branch, including the idempotent second reconciliation in `release()`. Two read/write/read restoration attempts plus owner lookup and `Uninhibit` account for at most eight explicit calls, or 24 seconds at three seconds each, only if no additional remote calls occur. This is a remote-call budget, not a hard wall-clock guarantee: journal I/O, adapter introspection, identity checks, scheduling, and local cleanup must be accounted for separately. If the complete trace exceeds the 30-second service bound, remove duplicate work or introduce a shared monotonic recovery deadline that reserves time for FD cleanup. Never increase the service bound just to hide unbounded work.

Verify timeout keywords on the actual adapter/owner/release methods with fakes, and inject timeout exceptions at each call. Every failed branch retains unresolved journals, reaches local FD cleanup, and avoids reacquisition/redimming. Live elapsed-time and physical restoration remain §19 gates.

## 10. P1 — Make atomic JSON commit outcomes coherent

### 10.1 Distinguish pre-commit and post-commit failures

Refactor `atomic_json()` to expose whether `temporary.replace(path)` occurred. Use a small result type or custom exception with at least:

- `committed=false`: failure occurred before replacement, so the old destination remains authoritative;
- `committed=true, durable=false`: replacement is visible, but parent-directory `fsync` failed, so crash durability is uncertain;
- normal return: replacement and required durability steps succeeded.

Always close the directory FD. Retain or safely move a temporary file when helpful for diagnosis; never use `rm`.

### 10.2 Reconcile each caller after a post-commit failure

For settings:

- re-read and validate the destination after a committed/durability-uncertain result;
- if it equals the requested values, update in-memory settings to match it and publish a durability warning;
- if it does not match or cannot be read, reload authoritative disk state/defaults and return a persistence error without leaving memory claiming another value;
- reconcile the features affected by the reloaded settings before publishing the error: close an owned lid inhibitor if lid mode is now off, clear only its scoped diagnostic, and run identity-safe brightness reconciliation. An action may raise before its usual success-path cleanup, so that cleanup cannot be the only route that applies reloaded settings. Preserve shutdown and sleep-transaction safeguards during this reconciliation.

For brightness journals:

- synchronize `brightness_record` and `brightness_journal_state` with the exact destination content after replacement;
- if the prepared journal is visible but durability is uncertain, do not begin a new dim write; retain a truthful pending diagnostic;
- if the applied journal replacement becomes durability-uncertain after brightness was written, preserve the in-memory record and immediately keep recovery pending; never behave as though no record exists;
- restoration and journal cleanup continue through identity, current-value, and readback validation.

Inject failures at temporary open/write, file `fsync`, replace, directory open, directory `fsync`, and close. Assert both disk and memory after every point.

## 11. P1 — Publish truthful per-feature state

### 11.1 Separate healthy dim ownership from recovery work

Keep `brightnessDimmed` as the effective applied state and redefine `brightnessRecoveryPending` to mean an action is actually needed or a recovery error is unresolved. Add a stable field such as `brightnessState` with values along these lines:

- `idle`: no journal and no owned dim;
- `dimmed-owned`: valid applied journal, current desired state is dimmed, no retry/error is pending;
- `restore-pending`: desired state is restored but the owned journal has not been safely resolved;
- `redim-pending`: wake/owner recovery requires restore then redim;
- `manual-change-preserved`;
- `unreadable` or `invalid`.

Exact names may be tightened during implementation, but every published value must have one defined meaning. A healthy `dimmed-owned` state sets `brightnessRecoveryPending=false`. Prepared journals, failed writes, desired restoration with a retained applied journal, unreadable/invalid journals, and deferred restore/redim work set it true. The panel displays “brightness dimmed” for healthy ownership and reserves “recovery pending” for real pending work.

After requesting dimming, require a successful readback exactly matching the recorded target before writing an `applied` journal or publishing `brightnessDimmed=true`/`dimmed-owned`. A successful setter return alone is insufficient. Ignored writes, partial changes, invalid readback, and readback exceptions retain the `prepared` journal and a pending diagnostic. Preserve the original brightness and target in that journal. Recovery may resolve an unchanged original value without writing; an unexpected intermediate value follows the existing ambiguity/manual-change policy and must not be overwritten merely to make the feature appear successful.

Test at least original=70/target=0 with readback 70, 5, 0, and an exception. Only verified 0 may become applied/healthy; unchanged 70 resolves conservatively and intermediate 5 remains unresolved without speculative writes. Include applied-journal commit/durability failure after a verified write.

### 11.2 Keep domain errors scoped

Publish `lidError` and use existing `brightnessError`, timer outcome, sleep outcome, and inhibitor outcome for their domains. Reserve the general `error` for a current cross-cutting/action error. Clear a domain error only when its own feature is disabled, succeeds, or is superseded. Tests must prove that fixing/disabling lid mode does not erase a brightness or persistence error and that stale lid text disappears.

### 11.3 CLI input and publication failure review

Validate a supplied failsafe threshold as an integer in `1–99` before constructing `dbus.UInt32` or making a mutation call. Invalid negative, zero, 100, oversized, and non-integer inputs must produce an ordinary argument error without a traceback or settings change; valid endpoints 1 and 99 remain accepted. Validate a threshold obtained from state before unsigned conversion too, reporting malformed state clearly. Add `ctl.py` and meaningful CLI assertions to the implementation/evidence map.

Trace every state publication during owner loss, shutdown, and an API mutation. If signal publication raises because the bus has disconnected, it must not abort brightness restoration, local FD cleanup, or persistence reconciliation, and it must not turn a committed action into an ambiguous unrecorded state change. Determine whether the existing D-Bus layer already guarantees each case; record that evidence or implement a narrowly scoped publication-error policy. Preserve genuine programming errors instead of hiding every exception. This is an audit requirement, not a claim that every existing publication call is defective.

Explicitly disable libdbus automatic exit on both agent connections with `set_exit_on_disconnect(False)` and install local `Disconnected` handlers. Session-bus loss cannot restore through an unavailable session service: retain its journal, close local FDs, and exit with failure for restart. System-bus loss can still restore through a surviving session bus: use the shutdown restoration-before-release path, close stale local FDs, and exit with failure. Test the real setup method and both callback outcomes; keep actual bus reconnection/restart under the live gate. Do not broaden exception handling to hide signal-construction or memory-allocation errors.

Audit initialization as well as the running main loop: set the disconnect policy as soon as each connection exists, before startup calls that may dispatch D-Bus traffic. Install cleanup handlers only once their required local state is initialized, and define failure behavior if startup disconnects before normal handler registration finishes. Trace repeated and simultaneous system/session disconnects and disconnect during `stop()`; cleanup must remain idempotent, must not reacquire inhibitors or apply a new dim, and must not depend on publishing through a lost connection. Test the ordering and local-resource outcome separately from the still-open real-bus delivery gate.

## 12. Deterministic verification matrix

### 12.1 Agent failsafe tests

Add tests for:

1. each relevant UPower state, especially `0`, `2`, and `6`;
2. UPower exception and owner loss;
3. empty, single, multiple-consistent, multiple-mixed, and partially unreadable battery sets;
4. missing and invalid percentage;
5. every ambiguous sequence in §3.3 with exact `Suspend` call count of one;
6. positive rearm by AC, percentage recovery, established non-discharging state, and explicit setting action;
7. no rearm from unknown data;
8. the fresh pre-suspend read changing from discharging to unknown.

### 12.2 Panel harness tests

Add tests for:

1. `desired=true/enabled=false` renders on, remains cancellable, and sends `SetPrevention(false)`;
2. effective on, effective off, reconnecting, and release-pending combinations;
3. rejected toggle refreshes to authoritative requested state;
4. running timer with `timerRemaining=0` keeps Cancel enabled;
5. consumed/canceled/idle timer disables Cancel;
6. immediate `GetState` success;
7. first failure then retry success;
8. all attempts fail with exact attempt count and stable error;
9. early current-generation `StateChanged` cancels retry;
10. disconnect/disable cancels retry;
11. stale callbacks and timers cannot reconnect a newer generation, including disable/re-enable of the same instance; a newer signal cannot be overwritten by an older state reply;
12. healthy dim versus recovery-pending text;
13. lid failure then disable/success clears only lid diagnostics;
14. runtime register/unregister calls across enable, disable, and agent owner replacement, including synchronous send failures that must not prevent local teardown.

### 12.3 Installer transaction tests

Extend the isolated temporary-HOME harness to cover:

1. staging validation fails before active core files move;
2. successful first core install;
3. successful core upgrade with old diagnostic backup retained;
4. new service never owns the bus name;
5. bus name appears but API never responds;
6. delayed API readiness succeeds within the bound;
7. failed core health check restores old files, unit, enabled state, active state, and API;
8. rollback failure reports each failed dimension truthfully;
9. previously disabled/inactive service remains disabled/inactive after rollback;
10. explicit non-default `$PYTHON` is present in both launchers and is actually invoked;
11. interpreter/import validation mismatch cannot occur;
12. custom `XDG_CONFIG_HOME` receives and restores the unit;
13. generated path quoting resists spaces and shell metacharacters;
14. first extension discovery failure keeps the validated canonical directory;
15. a simulated next login discovers/enables that retained directory;
16. upgrade discovery/enable/active failure restores the old extension;
17. exact new runtime report succeeds;
18. old, blank, malformed, delayed-within-bound, and timed-out runtime reports;
19. enabled/active new disk files with old loaded runtime cannot report success;
20. runtime mismatch rollback restores and, where possible, proves the old runtime;
21. core success plus panel failure exits `2` and leaves the new healthy CLI available;
22. no test touches the developer's actual systemd user manager, GNOME profile, or session bus.

The core cases must include a responding stale agent version and an incompatible API schema; both fail new-core readiness and enter rollback.

Add explicit cases for §7.4: selected versus prior interpreter, unknown/masked service state, failure at every switch move, catchable interruption, detection of retained interrupted transactions, independent partial rollback failures, panel staging errors after core commit, backup housekeeping failure, readiness deadline exhaustion, verifier fallback classification, missing Node, relative XDG rejection, and shell/systemd path expansion. Runtime reports must also include newer versions and `registered=false` with an otherwise matching version. Verify retained backups and canonical paths as well as exit codes and messages.

Also cover §7.5: concurrent invocation during the panel phase, inherited descriptor validation, interruption immediately after backup rename, retained partial records blocking a subsequent switch, and failure to retain the failed canonical extension without nesting the old backup. Check that completed-record archival does not disguise a partial rollback.

### 12.4 Persistence and shutdown tests

Add fault injection for every atomic-write stage listed in §10. Verify destination JSON, temporary/diagnostic artifact, in-memory settings, brightness record, published state, and allowed brightness writes.

For committed settings whose reread is mismatched or malformed, begin with both an owned lid FD and an applied dim. Assert reloaded settings, correct FD cleanup, scoped-error preservation, and safe brightness reconciliation even though the action returns a persistence error.

Add stop-order tests for healthy applied dim, prepared journal, manual conflict, no adapter, no journal, blocking/failed `Uninhibit`, forced disconnect, and repeated stop/release. Assert restoration is attempted before the first remote uninhibit and never bypasses identity/current-value checks.

### 12.5 Regression suite

Retain all meaningful plan-0.4 coverage for:

- one guarded `Suspend(false)` call;
- accepted/rejected/unknown sleep request outcomes;
- persistent uncertain-preparation latch;
- timer one-shot behavior;
- SessionManager owner-bound cookie release;
- release retry timing;
- brightness identity and manual-change checks;
- no speculative GNOME 49–50 brightness writes;
- `s2idle` and `[deep]` clock-gap simulation.

Update old assertions whose semantics intentionally change, especially `battery_discharging`, prevention toggle rendering, first-install failure placement, core rollback, and `brightnessRecoveryPending`. Do not weaken them simply to fit new output.

### 12.6 Follow-up review fault matrix

| Follow-up issue | Required assertion |
| --- | --- |
| Partially unreadable battery enumeration | Aggregate percentage is unknown; a readable low subset cannot trigger or rearm; known discharge classification does not substitute for percentage. |
| Core commit versus shell interruption | Receipt exists only after proven readiness; receipt failure restores prior core; post-helper/pre-assignment SIGTERM exits 2 and retains the healthy core; stale receipt is rejected before mutation. |
| First panel install enablement reporting | Enabled-but-unproven, disabled, and unknown are reported separately while canonical validated files remain present. |
| State path validation | Empty/unset defaults work; relative config/data/state paths fail before mutation; direct agent state-path resolution rejects relative values; harness state paths remain isolated. |
| Unit directive placement | `CollectMode` in `[Service]` fails staging even when the external verifier is unavailable; the valid `[Unit]` placement succeeds. |
| Synchronous panel sends and countdown freshness | Exactly three bootstrap attempts on synchronous send failure; action failure refreshes state; registration failure allows teardown; newer `TimerChanged` supersedes an older `GetState` reply. |
| Malformed readiness types | Raw string `"false"`, string/fractional/boolean API versions, and non-string versions cannot normalize into successful proof; missing legacy fields follow only the documented absence policy. |
| CLI threshold conversion | Negative, zero, 100, oversized, non-integer, and malformed-state thresholds do not dispatch; 1 and 99 do; invalid input returns a readable error without traceback. |
| Disconnected state publication | Establish the actual signal failure contract; any repaired publication failure cannot prevent restore, FD cleanup, or committed-settings reconciliation. |
| Automatic bus exit and system-bus loss | Both connections disable automatic exit before startup traffic and install handlers with initialized cleanup state; session loss retains journal/cleans FDs; system loss attempts restoration before release and exits for restart; startup, repeated, simultaneous, and stop-time disconnects cannot reacquire or redim. |
| Partial rollback and retained evidence | Independent file, enablement, running-state, and API failures remain separately reported; records/backups are preserved and a subsequent install stops; custom XDG unit rollback restores the original path. |
| Whole deployment lock | Concurrent installation during actual panel activation fails before switching; wrong inherited descriptor/file identity is rejected. |
| Mismatched dim readback | Ignored/partial writes and unreadable readback retain the prepared journal; only exact target readback permits applied/healthy state; recovery preserves unexpected values. |
| Recovery timeout budget | Adapter, owner lookup, and release calls use explicit finite timeouts; hidden proxy calls and duplicate reconciliation are included in the complete shutdown budget; timeout branches retain evidence and close local FDs. |

Tests for this matrix supplement §§12.1–12.5; they do not replace the original 14-finding coverage or certify live GNOME behavior.

## 13. Documentation and task ledger

The required initial replacement of `linux/gnome-wayland/task.md` with a detailed plan-0.5 ledger has already happened. Preserve that ledger and extend it for the additional requirements in this revision. The ledger must retain:

- every implementation section in this plan;
- focused test tasks;
- full deterministic/static checks;
- two independent post-implementation review passes;
- explicit unchecked live gates;
- final exact command/output evidence.

Do not clear the ledger after completion. Mark an implementation item complete only after its code, meaningful deterministic assertions, and both review passes for that item are complete. Record partial progress separately while those checks remain open. Replace or annotate stale plan-0.4 claims so no checked box implies the newly found defects are already fixed.

Update `README.md` and `linux/research_checklist.md` to document:

1. tri-state UPower and positive failsafe rearm evidence;
2. requested versus effective prevention UI;
3. timer cancel semantics at zero remaining;
4. bounded panel API retry;
5. exact runtime version proof and logout-required outcomes;
6. canonical first-install recovery;
7. transactional core rollback and its commit boundary with the panel;
8. `Type=dbus`, bounded readiness, selected interpreter, and `XDG_CONFIG_HOME`;
9. brightness-before-uninhibit stop ordering;
10. atomic durability-uncertain diagnostics;
11. healthy dim ownership versus pending recovery;
12. lid diagnostic clearing;
13. all live GNOME/distro/hardware limits;
14. retained transaction records, per-invocation commit receipts, their inspection paths, independent rollback dimensions, and the absence of automatic crash recovery;
15. unknown aggregate percentage for partial battery reads, all three XDG variables/defaults, and readable CLI validation errors.
16. explicit bus disconnect policy, initialization ordering, session versus system loss, retained brightness recovery, and restart limits;
17. verified dim readback, prepared-journal retention after ignored/partial writes, explicit recovery timeouts, and the distinction between the remote-call budget and elapsed shutdown time;
18. the final dispositions and supporting evidence for each §24 audit question.

## 14. Implementation sequence

1. Confirm the existing persistent plan-0.5 ledger covers every requirement, and add missing subtasks before resuming implementation. Its initial replacement is already complete.
2. Add tri-state battery classification and failsafe predicates, then complete the focused failsafe matrix.
3. Add per-feature lid/brightness state truthfulness and atomic-write commit outcomes, then complete persistence tests.
4. Add shutdown desired state and brightness-before-uninhibit ordering, then complete stop-order tests.
5. Add agent runtime/API version fields plus the panel-runtime registration API and published state.
6. Update the extension for requested/effective prevention, timer ownership sensitivity, bounded connection retry, diagnostics, brightness labels, and runtime registration. Expand the Node harness immediately.
7. Refactor installation into staged core and panel transactions. Generate interpreter-consistent launchers, honor `XDG_CONFIG_HOME`, change the unit to `Type=dbus`, and add bounded API polling.
8. Fix first-install canonical retention and add exact runtime handshake verification/rollback.
9. Expand the isolated installer harness across the complete §12.3 matrix.
10. Update documentation and the research checklist with actual behavior and remaining live gates.
11. Run focused suites, then one full deterministic/static validation pass.
12. Perform review pass 1 and fix every discrepancy; rerun affected and full checks.
13. Perform independent review pass 2 and fix every discrepancy; rerun affected and full checks.
14. Perform a separate fresh top-to-bottom static logical review without executing project code, after the two implementation review passes. Trace success, failure, interruption, retry, rollback, stop, restart, and owner replacement through the entire stack. Record and fix every finding; run affected checks after fixes, then repeat the static reasoning for the repaired paths.
15. Record exact final evidence in the retained `task.md`. Leave every unperformed live gate unchecked. Resume from the existing ledger; do not clear it a second time merely because this plan was amended.

## 15. File map

| File | Required changes |
| --- | --- |
| `agent.py` | Agent/API version state; tri-state discharge evidence; positive failsafe rearm; panel runtime registration/state; scoped lid error; truthful brightness state; atomic commit outcome handling; explicit shutdown brightness target and stop ordering; narrowly scoped signal transport handling and explicit bus disconnect lifecycle from initialization through stop. |
| `ctl.py` | Validate failsafe thresholds before unsigned conversion; handle malformed state without a traceback; retain JSON output and use the generated selected-interpreter launcher. Add an internal verification command only if the installer cannot safely parse `status`. |
| `sleep-disabler-gnome.service` or staged unit template | `Type=dbus`, `BusName=org.sleepdisabler.App`, bounded start timeout, installed agent launcher. |
| `extension/sleep-disabler@local/extension.js` | Runtime registration; requested-state prevention toggle; overdue cancel sensitivity; bounded `GetState` retry; scoped lid/brightness messages. |
| `extension/sleep-disabler@local/metadata.json` | Runtime/deployment version consistent with extension source. |
| `install.sh` | Transactional core staging/rollback; selected interpreter; XDG unit path; D-Bus/API readiness polling; canonical first-install retention; exact loaded-runtime proof; truthful rollback reporting. |
| `core_install.py` | Core staging/validation/commit/rollback orchestration; prior-interpreter capture; bounded coherent API probes; unit-path escaping; interruption/recovery records and independent rollback reporting. |
| `tests/test_agent.py` | Battery, persistence, shutdown ordering, diagnostic state, and runtime-registration matrices plus regression coverage. |
| `tests/test_extension.mjs` | Requested/effective UI, timer-at-zero, retries, stale generation, diagnostics, brightness labels, and runtime lifecycle. |
| `tests/test_install.py` | Core transaction, service readiness, interpreter/XDG paths, first-login recovery, loaded-runtime proof, and rollback matrices. |
| `tests/test_core_install.py` | Isolated per-artifact switch/interruption failures, partial rollback, deadline enforcement, verifier classification, and executable launcher/path checks. |
| `tests/test_ctl.py` | Isolated threshold parsing/conversion failures, absence of mutation on invalid input, malformed state, and valid endpoint dispatch. |
| `README.md` | User-visible behavior, install/update/recovery rules, and live limitations. |
| `task.md`, `linux/research_checklist.md` | New implementation evidence and still-open live gates. |

## 16. Double-check pass 1 — mutation, ownership, and transaction audit

After tests pass, perform a source-level audit that traces:

1. every assignment to battery/discharge state and every write to `failsafe_triggered`;
2. every route to `check_failsafe()` and the sole `Suspend(false)` call;
3. every prevention toggle render/action pair across requested/effective/release states;
4. every timer phase mutation and panel Cancel sensitivity decision;
5. every panel retry source creation, cancellation, callback, and generation guard;
6. every lid error set/clear and every brightness state transition;
7. every extension runtime register/unregister/report and installer comparison;
8. every core/extension stage, switch, backup, rollback, and retained diagnostic path;
9. every interpreter reference and every systemd unit path calculation;
10. every service readiness decision;
11. every atomic JSON replacement and caller reaction to pre/post-commit failure;
12. every stop/release path, proving brightness restore intent precedes remote uninhibit;
13. connection creation, disconnect policy, handler registration, signal publication, and cleanup under startup/repeated/simultaneous/stop-time bus loss.

Fix all findings and rerun affected plus full deterministic checks.

## 17. Double-check pass 2 — top-to-bottom stack audit

Independently trace this complete stack without relying on pass-1 conclusions:

1. installer environment and dependency validation;
2. core staging, validation, commit, daemon reload, enable/start, D-Bus ownership, API readiness, and rollback;
3. extension staging, first-install versus upgrade behavior, discovery, enable/active state, runtime handshake, and rollback/logout recovery;
4. graphical-session service start and clean/failed stop;
5. agent session/system bus ownership and state publication;
6. Shell connection, retries, requested controls, effective status, and stale callback handling;
7. CLI actions and JSON diagnostics;
8. login1/GNOME inhibitors, lid mode, UPower events, timer, and failsafe;
9. deliberate suspend, ambiguous reply, prepare signals, clock-gap proof, and resume for both `s2idle` and `[deep]`;
10. brightness dim, journal persistence, wake, manual conflict, owner return, shutdown restoration, and crash recovery;
11. all service/Shell/UPower/logind owner-loss and replacement paths;
12. README recovery instructions against the actual installed paths and exit statuses.

Map every plan checkbox to source plus a meaningful deterministic assertion or an explicit live gate. Inspect the final diff for stale plan-0.4 claims, unrelated changes, unsupported support statements, accidental secret/path exposure, and any `rm` command.

## 18. Static and deterministic completion checks

At minimum, after both review passes:

```text
python3 -m unittest discover -s linux/gnome-wayland/tests -q
node linux/gnome-wayland/tests/test_extension.mjs
python3 -m py_compile linux/gnome-wayland/agent.py linux/gnome-wayland/ctl.py linux/gnome-wayland/core_install.py linux/gnome-wayland/tests/test_agent.py linux/gnome-wayland/tests/test_install.py linux/gnome-wayland/tests/test_core_install.py linux/gnome-wayland/tests/test_ctl.py
sh -n linux/gnome-wayland/install.sh
node --input-type=module --check < linux/gnome-wayland/extension/sleep-disabler@local/extension.js
python3 -m json.tool linux/gnome-wayland/extension/sleep-disabler@local/metadata.json
git diff --check
```

Also use focused searches to prove:

- exactly one executable `Suspend(false)` call;
- no binary “not discharging means recovered” rearm path remains;
- no hard-coded installed `/usr/bin/python3` conflicts with selected-interpreter behavior;
- no hard-coded `~/.config/systemd/user` path bypasses `XDG_CONFIG_HOME`;
- no panel cancel decision depends only on positive remaining seconds;
- no installer success path omits exact runtime-version proof;
- no stop path reaches remote `Uninhibit` before setting restored brightness intent;
- no `rm` command exists in implementation or test helpers.

## 19. Live validation gates

These stay unchecked until observed on the named systems:

1. Disposable GNOME Wayland session: first install needing logout, next-login discovery, visual panel presence, `GetState` retry, disable/enable, and exact runtime upgrade/rollback behavior.
2. Debian, Ubuntu, Linux Mint, and Kali: dependency packages, custom/default XDG paths, user manager behavior, `Type=dbus` readiness, graphical-session target, logout/login, and restart.
3. GNOME Shell 46–50: panel lifecycle and runtime handshake. GNOME 46–48 brightness remains separately unverified; GNOME 49–50 stays no-write unless a safe API is proven.
4. Real SessionManager/logind/UPower owner loss and replacement, including UPower states `0`, `2`, and `6` when observable.
5. GNOME 46–48 hardware: intended internal-panel dim/restore, closed/open lid, service restart, manual conflict, docks, external monitors, AC, and battery.
6. Clean service stop while dimmed: verify physical brightness restores promptly before inhibitor release completes or times out.
7. Attended `s2idle` and `[deep]`: successful and failed suspend, timer, failsafe, missing/delayed signals where reproducible, and final brightness state.

Do not change `/sys/power/mem_sleep` as part of this prototype. Record the machine's existing selection and test available states only with the user controlling the hardware test.

## 20. Coverage audit

The checked boxes below mean this document includes the issue; they do not mean it has been implemented.

- [x] Unknown and pending-discharge UPower states cannot rearm failsafe — §§3, 12.1, 16.
- [x] Reconnecting prevention can be canceled from the panel — §4.1, §12.2.
- [x] Validated first installs remain in the canonical directory for logout discovery — §6, §12.3.
- [x] Upgrade success proves the exact new JavaScript runtime, not only enabled/active UUID state — §5, §12.3.
- [x] Agent, CLI, launchers, and unit update transactionally with prior-core rollback — §7, §12.3.
- [x] Service readiness requires D-Bus ownership plus bounded exact agent-runtime/API response — §§7.1, 8.1, 12.3.
- [x] Stop requests brightness restoration before potentially blocking GNOME uninhibit — §9, §12.4.
- [x] User unit installation honors `XDG_CONFIG_HOME` — §8.3, §12.3.
- [x] Installed agent and CLI use the interpreter that passed dependency validation — §8.2, §12.3.
- [x] Atomic JSON post-replace failures keep disk, memory, and diagnostics coherent — §10, §12.4.
- [x] An overdue still-running timer remains cancelable at zero displayed seconds — §4.2, §12.2.
- [x] Disabling/recovering lid mode clears only stale lid diagnostics — §§4.4, 11.2, 12.2.
- [x] Initial panel state failure has bounded same-owner retries with cancellation/generation guards — §4.3, §12.2.
- [x] Healthy intentional dim ownership is distinct from failed/pending recovery — §11.1, §§12.2 and 12.4.
- [x] Existing sleep transaction, inhibitor ownership, timer one-shot, brightness identity, GNOME 49–50 no-write, `s2idle`, and `[deep]` invariants receive regression and two-pass review — §§2, 12.5, 16–19.
- [x] Documentation, stale ledger claims, recovery guidance, and live-only support claims are corrected — §§13, 17, 19.
- [x] Prior-interpreter recovery, unknown service states, partial moves, interruptions, post-core exit classification, bounded probes, parser/verifier limits, and path expansion have explicit requirements and fault coverage — §§7.4, 12.3.
- [x] Registered panel runtime and exact committed agent/API are checked in one coherent snapshot; cooperative proof limits are explicit — §5.2.
- [x] Existing implementation progress is retained, the helper is included in the file/check map, and a separate final no-execution logical review follows both review passes — opening status, §§14–15, 18, 21.
- [x] Same-instance extension lifecycle generations, signal/reply ordering, and synchronous runtime-send failures have explicit verification — §§4.3, 12.2.
- [x] Settings reread failures reconcile owned lid and brightness resources before returning an action error — §§10.2, 12.4.
- [x] One lock spans both deployment phases; retained transaction recovery limits and directory nesting hazards are explicit — §§7.5, 12.3.
- [x] Core commit receipts close the helper-to-shell signal classification gap, including publication failure and stale receipts — §§7.3, 12.6.
- [x] Partial battery reads withhold aggregate percentage; first-install enablement is reported separately from runtime proof — §§3.1, 6, 12.6.
- [x] Config/data/state XDG validation, correct unit sections, synchronous panel sends, and countdown reply freshness have explicit requirements — §§4.3, 7.1, 7.4, 12.6.
- [x] Readiness types are validated before conversion; CLI unsigned conversion and disconnected publication receive source/fault checks — §§5.2, 11.3, 12.6.
- [x] Explicit session/system bus disconnect policy covers initialization ordering, repeated/simultaneous loss, stop-time cleanup, journal retention, and restart — §§11.3, 12.6, 16–19.
- [x] Ignored/partial dim writes cannot be published as healthy without exact target readback — §§11.1, 12.6.
- [x] Explicit recovery timeouts include hidden calls and a complete shutdown budget — §§9.3, 12.6.
- [x] Remaining audit questions have explicit decisions, verification requirements, and completion gates — §24.

## 21. Completion criteria

Plan 0.5 is complete only when:

1. Every issue in §20 maps to implemented source and a meaningful passing deterministic assertion or remains an explicitly unchecked live gate.
2. The failsafe latch cannot rearm from any unknown battery observation.
3. The panel can cancel requested-but-ineffective prevention and can cancel every still-running timer.
4. A first install remains discoverable after logout, and an upgrade cannot claim success without the exact loaded runtime report.
5. A failed core upgrade restores the prior working core and prior service state, with truthful partial-rollback reporting.
6. The service readiness decision proves D-Bus name ownership, the exact staged agent runtime/API version, and API response using the same interpreter installed in both launchers.
7. Stop establishes restored brightness intent and attempts safe restoration before the first remote uninhibit.
8. Atomic-write fault injection never leaves published memory silently contradicting a replaced destination.
9. Healthy dim ownership, pending recovery, lid failure, general failure, requested prevention, and effective prevention are distinguishable in state and panel output.
10. Both review passes and the separate final no-execution logical review are complete, all resulting fixes are retested, exact evidence is recorded in the retained `task.md`, and every real GNOME/distro/hardware gate remains unchecked until actually observed.
11. Every §7.4 failure/interruption case has a defined outcome and meaningful deterministic evidence; partial rollback or unproven runtime recovery is never labeled restored.
12. Every §12.6 follow-up finding has source and verification evidence; unresolved publication-contract questions are explicitly resolved before claiming completion.
13. Every §24 audit question has a recorded disposition supported by source/contracts and a meaningful assertion where applicable; unresolved questions cannot be represented as completed fixes. Exact dim readback and the complete shutdown timeout budget are verified.

## 22. Document completeness check

This is an audit of the plan's coverage, not certification that implementation or live validation has finished. Both document passes used the 14 recorded review findings and the current source/handoff to check that each issue has a repair, verification requirement, and completion gate.

| Recorded finding | Repair | Verification |
| --- | --- | --- |
| 1. Unknown discharge evidence rearms failsafe | §3 | §12.1; mutation trace in §16 |
| 2. Reconnecting prevention cannot be canceled | §4.1 | §12.2 items 1–3 |
| 3. First install disappears before logout discovery | §6 | §12.3 items 14–15 |
| 4. Old loaded extension accepted as new | §5 | §12.3 items 17–20; coherent registration checks |
| 5. Core overwritten without rollback | §7 | §12.3 items 1–3, 7–9; §7.4 failure matrix |
| 6. Active service accepted before usable API | §8.1 | §12.3 items 4–7; stale runtime/API tests |
| 7. Stop restores brightness after blocking uninhibit | §9 | §12.4; stop-path trace in §16 |
| 8. User-unit path ignores XDG config | §8.3 | §12.3 item 12; relative-path rejection |
| 9. Validated and installed interpreters differ | §8.2 | §12.3 items 10–13; prior-interpreter rollback |
| 10. Atomic replace succeeds but memory stays old | §10 | §12.4; every write-stage failure |
| 11. Zero-second running countdown cannot be canceled | §4.2 | §12.2 items 4–5 |
| 12. Disabled lid mode leaves stale diagnostics | §§4.4, 11.2 | §12.2 item 13; agent error-preservation assertions |
| 13. Initial panel snapshot failure never retries | §4.3 | §12.2 items 6–11; late same-owner recovery |
| 14. Healthy owned dim reported as recovery failure | §11.1 | §§12.2 item 12, 12.4 |

- [x] Document pass 1: all 14 findings map to repair and verification; original safety invariants remain in §§2 and 12.5.
- [x] Document pass 2: installation/rollback edge cases from the handoff are explicit, source/helper/test maps are complete, and status distinguishes partial implementation from verified completion.
- [x] The scope retains GNOME Wayland, Debian/Ubuntu/Mint/Kali validation, GNOME 46–50 limits, and both `s2idle` and `[deep]`; no physical validation is claimed.
- [x] Follow-up document pass 1: rechecked every numbered finding against the repair/verification table; all 14 remain covered after amendment.
- [x] Follow-up document pass 2: checked lifecycle re-entry, settings reload side effects, whole-install locking, move interruption windows, retained-record handling, directory restoration, file maps, and acceptance criteria. These are plan-coverage checks, not completed implementation reviews.
- [x] Handoff coverage pass 1: mapped every additional repair and audit candidate to §§3.1, 4.3, 5.2, 6, 7.1, 7.3–7.5, 11.3, and the explicit §12.6 matrix.
- [x] Handoff coverage pass 2: rechecked original findings 1–14 against the table, checked follow-up fault assertions and documentation/file/completion maps, and retained unchecked live gates. No project code was executed for these document checks.
- [x] Final document pass 1: reread every repair and verification section against the original 14-finding table and the follow-up matrix; clarified bus initialization/disconnect ordering and added it to the file, documentation, and review maps.
- [x] Final document pass 2: independently rechecked findings 1–14, all 12 follow-up matrix rows, implementation sequence, both reviews, final static review, and live gates. These checked boxes certify plan coverage only; they do not certify completed implementation reviews.

## 23. Primary references and research boundaries

Use these existing primary references when implementation needs an API or platform semantic checked. The main reference table is inherited; the additional D-Bus source inspection below was performed during implementation. No live introspection is claimed.

| Contract | Primary reference | Required use |
| --- | --- | --- |
| Service readiness and user-unit paths | [systemd.service](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html), [systemd.unit](https://www.freedesktop.org/software/systemd/man/latest/systemd.unit.html) | Verify `Type=dbus`, `BusName`, timeouts, user-unit lookup, and supported target-system versions. |
| Generated command escaping | [systemd command lines](https://www.freedesktop.org/software/systemd/man/latest/systemd.service.html#Command%20Lines) | Check escaping separately from shell quoting; preserve literal paths and arguments. |
| Battery properties | [UPower Device API](https://upower.freedesktop.org/docs/Device.html) | Check state enum, percentage validity, and conservative treatment of unavailable properties. |
| D-Bus identity/lifecycle | [D-Bus specification](https://dbus.freedesktop.org/doc/dbus-specification.html) | Bind callbacks and reports to unique owners; clear reports on owner loss. |
| GNOME extension lifecycle | [GJS extension anatomy](https://gjs.guide/extensions/overview/anatomy.html), [updates and breakage](https://gjs.guide/extensions/overview/updates-and-breakage.html) | Check enable/disable cleanup and avoid assuming that replacing files reloads imported code. |
| Inhibitor acquisition/release | [login1 API](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.login1.html), [GNOME SessionManager API](https://gnome.pages.gitlab.gnome.org/gnome-session/re04.html) | Preserve FD/cookie ownership and noninteractive suspend/release semantics. |
| Suspend state and clock accounting | [kernel sleep states](https://docs.kernel.org/admin-guide/pm/sleep-states.html), [clock_gettime](https://man7.org/linux/man-pages/man2/clock_gettime.2.html) | Preserve `s2idle`/`deep` handling and suspend-inclusive elapsed-time logic. |

If a target version contradicts an assumed contract, record the discrepancy, adjust the implementation and tests, and repeat both relevant audit traces. Package availability, Shell hot-reload behavior, real brightness mapping, and firmware sleep/resume stay subject to §19.

The inherited research record additionally inspected upstream [systemd v255 `systemctl-is-enabled.c`](https://github.com/systemd/systemd/blob/v255/src/systemctl/systemctl-is-enabled.c) and [v257](https://github.com/systemd/systemd/blob/v257/src/systemctl/systemctl-is-enabled.c): a missing unit reports `not-found` with status 4. Preserve this first-install case rather than interpreting every nonzero `is-enabled` result as unknown or disabled. Accept it only with independently absent prior unit/artifacts as required by the transaction snapshot policy, and test that combination. This is prior upstream-source evidence from the handoff, not a fresh network check in this document pass or proof for every target systemd version.

Known logical defects should be fixed before attended laptop trials. Trials then address the remaining GNOME, distro, and hardware uncertainties; they do not replace the deterministic or static completion checks.

Implementation source inspection also read the upstream dbus-python mirror's [signal decorator](https://github.com/posborne/dbus-python/blob/master/dbus/decorators.py), [connection bindings](https://github.com/posborne/dbus-python/blob/master/_dbus_bindings/conn-methods.c), and [public bus wrapper](https://github.com/posborne/dbus-python/blob/master/dbus/_dbus.py). The decorator constructs and sends a signal without a reply; the binding's send failure is an allocation failure, distinct from a method-call disconnected exception. Its disconnect-setting documentation warns that bus defaults may call `_exit` without cleanup. The implementation therefore sets the disconnect policy explicitly, catches only D-Bus exceptions around publication, and preserves programming/allocation failures. This mirror inspection establishes why defaults must not be assumed; installed Debian-family package versions and actual disconnect delivery still require live verification.

## 24. Remaining source-audit decisions before completion

### Conditional countdown dispatch amendment

Snapshot a due timer's condition flags before consuming the one-shot. For a closed-lid timer, refresh UPower without lid side effects before preflight and again after confirmed inhibitor release; unknown/open lid blocks dispatch. Recheck the locally observed lid after the final logind read, which can deliver a lid-change signal. The prevention condition requires effective prevention immediately before intentional release; deliberate release itself must not invalidate an otherwise eligible timer. Rejected conditions before release preserve prevention; rejected conditions afterward leave prevention off, without retry or reacquire. Publish a consumed outcome explaining the failed condition. Verify cached-closed/fresh-open, unknown lid, release-time open/unknown, final-preflight lid change, prevention loss before release, and stable closed-lid success with exact Suspend counts. Keep unconditional timer behavior and the single Suspend site intact.

Managed core destinations must have supported types before staging: program directory, regular CLI file, regular unit file, or absent. Reject symlinks (including dangling ones) and other types before mutation. The transaction's `exists()` bookkeeping cannot safely preserve a dangling prior link; do not silently overwrite it. Verify all three paths with valid/dangling links and wrong types; preserve the original entries and perform no deployment commands.

Preflight D-Bus reads are lifecycle boundaries too. Recheck shutdown, unresolved transaction, and logind generation after reading `PreparingForSleep`; recheck them plus held cookie ownership after SessionManager owner lookup. A reentrant shutdown, prepare event, owner replacement, or cookie change invalidates that observation and must reject dispatch. Test changes during each read, not only changes between release and preflight.

These are audit candidates from the continuation, not additional confirmed defects. Resolve each during implementation rather than assuming an existing passing test settles it. Add the disposition and evidence to the retained ledger.

| Question | Required decision or repair | Verification and acceptance |
| --- | --- | --- |
| Missing unit with orphan core artifacts | Apply §23's first-install exception only when the prior unit and all managed core artifacts are independently absent. If program/CLI artifacts exist with `not-found`, stop before mutation with a precise unsupported/incomplete-prior-install diagnostic. A separate documented recovery workflow may handle this case later. | Cover `not-found` status 4 with total absence, orphan program, orphan CLI, dangling unit symlink, and unavailable manager; none may be silently normalized into a healthy prior installation. |
| Transaction record interrupted while being written | Decide whether the panel record must use the core's temporary-write/fsync/rename/directory-fsync pattern. A partial record may safely block deployment, but must remain inspectable and must never be treated as a completed/archiveable transaction. | Interrupt before/after publication; corrupt/truncated/symlink records stop the next install before switching; preserve paths and explain manual inspection. |
| Receipt durability versus process coordination | Retain the explicitly limited process-coordination contract in §7.3. If durable receipt publication is required, include directory fsync and define its post-rename failure outcome. Never infer power-loss atomicity from atomic rename alone. | Inject preparation, rename, and any required directory-fsync failures; check core commit, rollback, shell exit classification, and retained evidence agree. |
| Old panel files restored but old runtime unproven | Keep file restoration, configured enablement, active-list membership, and exact runtime proof as separate outcomes. An archived transaction can mean the file switch finished, but cannot imply runtime restoration. Use wording such as “previous files and configured state restored; runtime unproven; logout required.” | Old/new/no/malformed runtime reports after rollback produce truthful independent dimensions; no generic success line contradicts the unresolved runtime outcome. |
| Graphical-session target ordering | Check the installed unit against `systemd.unit` target default dependencies and actual GNOME session activation. Determine whether `After=graphical-session.target` plus installation under that target creates conflicting ordering, whether `PartOf` supplies intended logout cleanup, and whether session environment reaches the user manager. Repair ordering if the contract requires it. | Record source/manual reasoning, validate the rendered unit when supported, and retain actual GNOME target activation/logout/environment as a live gate. Do not claim absence of ordering cycles solely from a mocked systemctl result. |
| Two overlapping same-owner state replies | Give refresh requests monotonically ordered identities or coalesce them so an older reply cannot overwrite a newer accepted reply even without an intervening signal. Preserve existing owner/generation and `StateChanged`/`TimerChanged` freshness checks. | Deliver two replies in reverse order, with and without signals, success/error combinations, owner replacement, and disable/re-enable; newest accepted state remains authoritative and retries remain bounded. |
| Installed dbus-python contracts | Verify support for `set_exit_on_disconnect`, `get_is_connected`, local disconnect delivery, explicit method timeouts, and automatic proxy introspection against the dependency contract. A fake invocation proves the local call shape, not installed-package behavior. | Record relevant primary API/source evidence; keep real target package and disconnect delivery unchecked until observed. Constructor failures before resource acquisition and startup loss after acquisition have defined cleanup outcomes. |
| Completeness of the final read-through | Re-read any previously truncated source ranges, especially suspend reply/transaction resolution and `StartTimer` endings. Check every installer fault category against meaningful assertions, including malformed/newer runtime, unavailable parser, unknown service state, changed prior interpreter, and both sides of every backup/replacement move. | Requirement-to-source-to-evidence mapping covers complete functions and every artifact. Historical test counts remain historical; record a new full result only after actually running the final repaired suite. |

### Final amendment coverage checks

- [x] Amendment pass 1: original findings 1–14 still map through §22; the two additional confirmed findings each have repair text, fault assertions, documentation requirements, and completion gates in §§9.3, 11.1, 12.6, 13, and 21.
- [x] Amendment pass 2: checked the eight audit decisions against deployment, runtime, service, panel, and D-Bus boundaries; each has a required disposition and verification criterion. The follow-up matrix now has 14 rows. Unperformed implementation reviews and live tests remain unverified; these checks certify document coverage only.
- [x] Completion coverage pass 1: rechecked original findings 1–14, all 14 follow-up fault rows, the ten §7.4 transaction requirements, eight §24 dispositions, and the power freshness, preflight lifecycle, managed destination type, and conditional countdown amendments against the retained ledger.
- [x] Completion coverage pass 2: independently checked repair, verification, documentation, file, and completion maps after the final source-only review; all recorded points remain covered. Implementation audit evidence is in `task.md`; §19 remains entirely unperformed. No project code was executed for these final document checks.
