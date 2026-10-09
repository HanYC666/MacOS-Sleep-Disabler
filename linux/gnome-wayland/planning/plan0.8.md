# Plan 0.8 — reconcile the agent regression suite and close the 0.6/0.7 review

Status: **fake-service implementation, isolated verification, and SSH delivery complete; live gates remain open**. Commit `422e07b` records the 0.6/0.7 work. The pre-0.8 full run found 53 assertion failures and seven errors in 206 agent cases. After callback-fixture migration and two final fixture corrections, the isolated fake-bus agent suite passed all 212 current cases on 2026-10-09. That proves current fake-path assertions, not live GNOME or hardware behavior. The current core installer suite passed 84 cases with one skip, CLI passed five, and the GJS panel harness passed. The shell installer suite exceeded a 240-second serial limit, then all 44 cases passed when partitioned into four independent 11-case temporary-home shards; no single serial run is claimed.

The user permits **isolated fake-bus sleep and scheduling tests that cannot suspend the laptop**. The agent test module installs fake `dbus` and GLib modules before importing production code; it has no live bus constructor, and `Suspend` is a mock. Do not invoke a live `Suspend`, `systemctl suspend`, timer, or lid-close path. A separate source-only final walkthrough was completed after implementation. The user subsequently authorized SSH delivery; `origin/main` was read back at `53f688342536d48b2bd7d1faf5318582c30f0e61` after the initial push. The final ledger commit must also be verified on the remote.

## 1. Required result and evidence rules

1. Read the current code and recent Git history before edits. Map all 0.6 F1–F8 and 0.7 sections to current source, existing fixtures, and the old ledger. Treat old ledger claims as leads, not proof; inspect current state.
2. Replace `../task.md` with a detailed, persistent ledger **before implementation edits**. Record each subtask, acceptance criteria, current evidence, two independent review passes, and unperformed gates. Only check a subtask after the implementation and both passes are complete. Preserve the file after completion.
3. Classify all 60 failing/error cases by name and cause. Record a case-by-case inventory: obsolete synchronous expectation, inadequate fake bus/callback driving, genuine source defect, or uncertain. Do not skip, delete, rename, or weaken a test merely to make the suite green. Preserve its original invariant when migrating it to the asynchronous API.
4. Audit the historical 206-case suite and current test selection structurally. The user's later permission allows the complete isolated fake-bus agent suite; record its exact current count and result separately from the historical 206-case failure result. Never describe a selected subset or a live gate as covered by fake tests.
5. At completion, match each plan 0.6 and 0.7 requirement to current source and appropriate evidence. An unrun fixture or live gate stays visibly open. Do not claim the goal or the whole regression suite complete from source review alone.

## 2. Regression-test migration: retain behavior, update the harness

The agent's runtime service methods now use `async_callbacks`, and preflight is `suspend_preflight_async`; `sleep_now` reports acceptance of an asynchronous attempt rather than a synchronous final outcome. The old fixtures around lines 1100–2000 still call `suspend_preflight()`, expect immediate `Suspend`/settlement, or use `session_bus=object()` where cleanup now checks `get_is_connected()`. Migrate those fixtures to controlled callback queues and explicit GLib source advancement. Assert pre-release and final preflight separately, and assert that `Suspend(false)` is dispatched at most once only after release and fresh evidence. Use fake buses and fake timers exclusively; do not connect to a real D-Bus service.

Build reusable fake-bus helpers for owner lookups, `PreparingForSleep`, `CanSuspend`, inhibitor release, `Suspend` reply/error, and bus connection/disconnect. Each helper must model callback ordering, delayed reply, no reply, owner replacement, exception, and operation deadline where relevant. Fake time must distinguish BOOTTIME, MONOTONIC, and a fired GLib timeout. Model a queued callback as pending until explicitly delivered; do not make tests pass by calling every callback synchronously. Count and close fake FDs/cookies. Keep the fixture able to exercise direct synchronous compatibility methods only when a test explicitly targets them.

Rebuild the old scenario groups, retaining their safety assertions:

| Group | Existing failing examples | Required current-path assertions |
| --- | --- | --- |
| Release and owner cleanup | `test_release_*`, `test_owner_loss_cancels_release_source`, owner/cookie mismatch | Accurate connected fake bus; known-cookie release vs uncertain connection close; retry source count and cancellation; late replies inert; brightness restoration ordered before release. |
| Initial and final preflight | `test_logind_replacement_requires_fresh_readable_preflight`, `test_preflight_reads_cannot_authorize_changed_lifecycle`, `test_uncertain_latch_blocks_failsafe_and_direct_preflight` | Two fresh owner checks, property and `CanSuspend` handling, lifecycle/transaction/cookie invalidation, absolute parent deadline; no dispatch on stale or unreadable evidence. |
| Timer and failsafe | `test_conditional_timer_*`, `test_failsafe_*`, low-battery and missing-percentage cases | Pending attempt vs terminal result, fresh power/lid after release, timer episode consumed once, unsafe changes block dispatch, unknown evidence never retries. |
| Suspend reply and reconciliation | accepted/ambiguous/definite-rejection cases, `test_false_*`, `test_reentrant_*`, repeated false and clock-gap cases | Exactly one request, accepted/unknown/rejected classification, `PrepareForSleep` and `PreparingForSleep` ordering, owner generation, gap-proof interval, unknown latch, idempotent resolution. |
| Persistent state and UI | timer outcome and displayed state assertions | Authoritative state reflects pending/blocked/consumed and independent diagnostics without stale success claims. |

For each migrated case, compare its old assertion to the 0.6/0.7 invariant. If a current implementation genuinely violates that invariant, fix production code and add a focused regression. If its expectation is obsolete, explain the new observation point in a short test comment. A red test must not be relabeled as stale without a trace through the current callback path.

## 3. Re-audit production behavior while migrating

Trace `Agent.__init__` through bus construction, signal registration, startup probes, `GetState`, every callback-backed public method, inhibitor ownership, brightness journal, power snapshot, timer/failsafe, preflight, request, wake reconciliation, stop, and restart. Recheck the following 0.6/0.7 invariants at each callback boundary:

- Every remote call uses a bounded asynchronous path in live runtime; direct compatibility helpers cannot leak into a service method, timer, signal, startup, or stop callback. Parent deadlines include queue and local admission time, and expired callbacks start no new dependent work.
- Public mutations send exactly one reply; coalesced requests share the result; conflicts have truthful Busy/Unavailable outcomes; stop supersedes ordinary work and retains a path to brightness restoration and inhibitor cleanup.
- UPower aggregate evidence is all-or-unknown with current unique owner and fresh lid/power before automatic sleep. `CanSuspend` must return `yes` for a noninteractive `Suspend(false)` request. A remote timeout is an unknown side-effect outcome, never proof of cancellation.
- GNOME cookie and login1 FD ownership are explicit; late resources are released/closed; unresolved acquisition or release closes the relevant session-bus boundary only after any available brightness restoration.
- Brightness journal is durable before a Set, records confirmed vs uncertain writes, and is cleared only after identity and physical readback proof. Repeated healthy enable has zero extra brightness writes; manual conflict and output changes remain conservative.
- Clock-gap samples use interval bounds and request-local baselines; a paused inter-read cannot prove suspend. Unknown suspend/reconciliation evidence blocks automatic retry and retains truthful state.
- Shell registration authenticates current owner, retries within its envelope, and cannot publish after disable/replacement. Connection, action, registration, and agent diagnostic errors clear only on their own authoritative evidence.
- Installer preflight proves running manager and Shell discovery paths, active prior state home, unit `FragmentPath`, rollback target, and exact runtime readiness. The service, CLI, extension, and README agree on API/version/deadlines.

Any discovered defect gets a minimal production repair, corresponding regression source coverage, and ledger evidence. Avoid changing the architectural contract to fit an old fixture.

## 4. Lid and monitor behavior: separate the claims

`HandleLidSwitch=ignore` is a system-wide `logind.conf` setting, whereas a `handle-lid-switch` inhibitor is a per-process temporary lock; neither proves panel power or Mutter monitor-layout behavior. The project must not silently edit system-wide logind policy. GNOME Shell/Mutter may reconfigure the internal connector on lid close, and firmware/backlight hardware may darken the panel regardless of software brightness. The user's reported Kali Wayland 60 fps but incomplete/glitchy closed-lid rendering is a hardware/session observation, not evidence that all machines behave that way. Research official logind and Mutter documentation; document which claims are supported and which require attended hardware observation. Keep dimming optional for the core stay-awake function and preserve safe journal recovery where dimming is available. Do not simulate a real lid close or sleep to close this gate.

## 5. Permitted verification and two review passes

1. Run source/AST checks, Python compilation, shell syntax, diff whitespace, and isolated fake-service suites. The user now permits the complete fake-bus agent suite, including timer/failsafe/suspend simulation, provided it cannot connect to live D-Bus or suspend the laptop. Run installer, CLI, core installer, and GJS harnesses only with fake services and no live deployment. Record each completed count and any bounded timeout separately.
2. Probe API availability read-only: local dbus-python/GLib/GJS signatures and, if useful, `Introspect`/`Get`/owner queries against safe endpoints. Do not call mutating API methods or set logind policy. Distinguish host API existence from target Kali/GNOME behavior.
3. Pass 1: requirement → code → fixture/source evidence for every 0.6/0.7/0.8 row; check old failure manifest against migrated cases and compare exact result semantics. Resolve all source-level findings.
4. Pass 2: an independent adversarial walkthrough from installer → running service → Shell panel → user mutation → power/lid changes → automatic attempt → wake/unknown state → shutdown/rollback. Check callback reorder, timeout just before source firing, owner replacement, stale resources, and partial writes. Fake sleep/scheduling tests are permitted; live behavior is not. Record new findings and repeat both passes for affected paths.
5. Final separate **no-code-execution** logical review of the complete stack from the top. Read the final files, not only diffs. Record exact limitations. Leave live sleep, physical lid/panel, and target-distro gates open. Verify SSH delivery before marking the goal achieved.

## 6. Delivery and completion checklist

- [x] Current code and history inventoried; all 60 old failure/error variants mapped to current passing fake cases. Historical source/fixture causation was not bisected.
- [x] `../task.md` replaced before implementation edits and retained with granular checked/unchecked subtasks and two review passes.
- [x] Old fixtures migrated without weakening safety invariants; concrete source defects found during migration fixed and covered.
- [x] Plans 0.6 and 0.7 rechecked requirement by requirement against final source.
- [x] Permitted isolated verification passes; fake sleep/scheduling coverage and all live gates are honestly distinguished.
- [x] Full source-only stack review found no remaining concrete logical defect; live proof remains open.
- [x] Requested commits were pushed to the configured SSH remote after the user lifted the local-only preference; `origin/main` returned `53f688342536d48b2bd7d1faf5318582c30f0e61` after the initial push. The final ledger commit is subject to a second remote hash check.

Source context: [systemd login1 D-Bus API](https://www.freedesktop.org/software/systemd/man/latest/org.freedesktop.login1.html), [logind lid configuration](https://www.freedesktop.org/software/systemd/man/latest/logind.conf.html), [systemd inhibitor locks](https://github.com/systemd/systemd/blob/main/docs/INHIBITOR_LOCKS.md), and [Mutter overview](https://gnome.pages.gitlab.gnome.org/mutter/).
