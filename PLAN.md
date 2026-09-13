# Sleep Disabler Improvement Plan

## Goals

- Ship separate native Apple Silicon and Intel `.app` bundles.
- Make the power-management, scheduling, monitoring, and UI code easier to test and maintain.
- Replace fragile battery text parsing with supported macOS APIs.
- Improve lid-state monitoring without claiming an unsupported event API is guaranteed.
- Make window mode faster to understand and operate.

## Feasibility

| Area | Status | Decision |
| --- | --- | --- |
| Apple Silicon and Intel support | Addressable | Build, sign, test, and publish separate `arm64` and `x86_64` `.app` bundles. Do not ship a universal binary. |
| Split `AppState` | Addressable | Extract focused services while retaining `AppState` as the observable UI coordinator. |
| Battery readings | Addressable | Replace `pmset -g batt` regex parsing with IOKit Power Sources data and change notifications. |
| Lid event-only detection | Partially addressable | Prototype IOKit service notifications. Keep a direct-IOKit, adaptive-poll fallback because `AppleClamshellState` is undocumented and Apple does not guarantee a public lid-change event. |
| Lid dimming on non-MacBooks | Addressable | Disable the existing control on machines without a built-in display. The control remains visible but greyed out, with an explanatory label. |
| Silent migration from the old broad `sudoers` rule | Partially addressable | The rule can be replaced automatically when elevation is already available. A password/authorization prompt is unavoidable if the only existing permission is `pmset`; that rule cannot authorize rewriting `/etc/sudoers.d`. |
| Window-mode UI | Addressable | Reorganize the existing controls around status and the primary sleep action. |

## Phase 0 — Compatibility baseline

1. Define the supported matrix: macOS 13.5+, separate Apple Silicon and Intel builds, notebooks, and desktops.
2. On both an Apple Silicon and Intel test machine, record behavior for:
   - disabling and restoring sleep;
   - scheduled sleep;
   - battery failsafe while on battery power;
   - lid-dependent scheduling and dimming, where a built-in display exists.
3. Add a diagnostic capability model (`hasInternalBattery`, `hasBuiltInDisplay`, `supportsLidState`, `supportsBrightnessDimming`) so unsupported optional features are disabled with a clear reason rather than failing silently.
4. Detect whether the lid feature can be offered by enumerating online displays and calling the public `CGDisplayIsBuiltin`. It identifies an internal display on portable systems. Use this capability—not a string match—as the enforcement gate.
5. Optionally show the user-friendly model name in diagnostics only. Do not gate on whether `hw.model` contains `MacBook`: Apple documents it as a machine-model identifier, and newer models can use identifiers such as `Mac17,3` instead. A closed-lid MacBook may temporarily report no built-in display, so preserve the successful launch-time capability result for the session.

Acceptance criteria: normal sleep control, scheduler, login item, window mode, and menu-bar mode work on both CPU architectures. The lid-dimmer toggle is visible but disabled on desktops and other machines without a built-in display, and it never affects basic functionality.

## Phase 1 — Two native release bundles

1. Create distinct Release archive configurations or release automation: one with `ARCHS = arm64`, one with `ARCHS = x86_64`.
2. Keep the deployment target at macOS 13.5 unless compatibility testing requires raising it.
3. Build, sign, notarize, zip, and publish each architecture separately with unambiguous names, for example `Sleep-Disabler-arm64.app` and `Sleep-Disabler-x86_64.app`.
4. Verify each final executable with `lipo -archs`: the Apple Silicon archive must report only `arm64`; the Intel archive must report only `x86_64`.
5. Update the README and release notes with download guidance and any lid-dimmer limitations by machine.

Risk: the private DisplayServices framework used by the dimmer is not a stable compatibility contract. Each architecture-specific release needs hardware validation, and the feature must stay separately capability-gated.

## Phase 2 — Restrict privileged `pmset` access and migrate old policy

The current managed rule permits every invocation of `/usr/bin/pmset` without a password. Replace it with a policy that permits only the exact state-changing operations the app uses. Reads such as `pmset -g custom` must run without `sudo` and therefore require no authorization rule.

1. Inventory every privileged invocation before writing policy: disabling/enabling `disablesleep`, setting `sleep` and `displaysleep` for AC and battery, and `sleepnow`.
2. Centralize those operations in `PowerManager` as typed commands. Reject every other argument combination before spawning a process.
3. Generate `/etc/sudoers.d/sleep_disabler` with a versioned header and only anchored, exact command-argument rules. Do not use a wildcard such as `/usr/bin/pmset *`.
4. Use sudoers regular expressions only after verifying the installed sudo supports them (sudo 1.9.10+). Bound numeric values to the valid `pmset` ranges. If that cannot be verified, do not fall back to a broad `pmset` rule; use a narrowly validating privileged helper instead.
5. At startup, run unprivileged state reads and verify every required restricted command with `sudo -n`. Record whether the new policy is fully usable, missing, or requires migration.
6. Check the managed file's versioned header and content through the authorized installer path. If it is the old `%admin ... NOPASSWD: /usr/bin/pmset` file, replace it atomically with the new restrictive file and validate it with `visudo -cf` before activation.
7. Migration behavior: if an administrator authorization token or cached elevation is available, perform the replacement without an additional UI interruption. If it is not available, request one standard macOS authorization prompt. It is not technically safe or possible to rewrite the protected sudoers file silently using only the old rule, because that rule grants `pmset`, not file-write privileges.
8. Treat an unreadable, altered, or invalid managed file as **not installed**. Never overwrite unrelated sudoers files. Show a repair action rather than issuing privileged commands.
9. Replace the existing startup probe (`sudo -n pmset -g`) with the exact-policy verification above, so a legacy broad policy is not mistaken for the desired restricted policy.

Acceptance criteria: no installed rule permits arbitrary `pmset` arguments; the app detects the restrictive policy at startup; legacy app-managed broad rules are migrated after any authorization that macOS requires; and all normal sleep control and restoration flows continue to work.

## Phase 3 — Separate responsibilities

Create the following types and keep each independently testable through protocols:

| Type | Responsibilities |
| --- | --- |
| `PowerManager` | Read, disable, restore, and force system sleep; own `pmset` command execution and saved settings. |
| `BatteryMonitor` | Read structured battery/source state via IOKit and publish change events. |
| `LidMonitor` | Report lid state, manage the IOKit-event experiment, and own the adaptive fallback. |
| `BrightnessManager` | Capture, dim, and restore brightness only when the capability model permits it. |
| `Scheduler` | Duration, countdown, start/stop rules, cancellation conditions, and completion callback. |
| `MenuBarController` | Construct and update `NSStatusItem` / `NSMenu`. |
| `AppState` | Published UI state and orchestration of the above services; no direct shell parsing or timer ownership. |

Suggested order: extract `Scheduler` first, then `PowerManager`, then monitoring services. This keeps each behavioral change small and allows existing UI bindings to continue using `AppState` during the migration.

Acceptance criteria: `AppState` no longer directly creates `Process` instances, owns the scheduler timer, or parses battery output. Scheduler and power-restoration tests run without executing `pmset`.

## Phase 4 — Replace battery text parsing

1. Replace the `pmset -g batt` call and percent regex with `IOPSCopyPowerSourcesInfo`, `IOPSCopyPowerSourcesList`, and `IOPSGetPowerSourceDescription`.
2. Read `kIOPSCurrentCapacityKey` for percent and `kIOPSPowerSourceStateKey` to determine whether the Mac is drawing from battery power.
3. Register `IOPSNotificationCreateRunLoopSource` so the failsafe re-evaluates when battery level or power source changes, rather than only on the current 30-second refresh timer.
4. Preserve safe behavior when no internal battery is present: the failsafe should be unavailable, not treated as a zero-percent battery.
5. Add unit tests for battery, AC, charging, missing battery, and threshold-edge cases.

Acceptance criteria: battery failsafe no longer depends on localized or undocumented `pmset` output format.

## Phase 5 — Lid monitoring and dimming

Research conclusion: Apple supplies general IOKit service-interest notifications, but does not publish a stable, public API specifically for lid-state changes. The commonly used `IOPMrootDomain.AppleClamshellState` is an undocumented registry property. Existing open-source implementations combine that property with wake/sleep notifications and short-lived polling rather than trusting a single event source.

1. Replace the spawned `/usr/sbin/ioreg` process with a direct IOKit read of `IOPMrootDomain.AppleClamshellState` for initial state and fallback checks.
2. Prototype `IOServiceAddInterestNotification` for `IOPMrootDomain` on both Intel and Apple Silicon MacBooks. Capture callback type, timing, and whether it actually correlates with lid transitions.
3. Also subscribe to supported workspace/power wake and sleep notifications. On wake, refresh the lid state before restoring brightness or restarting a lid-dependent workflow.
4. Use event-driven updates if validation confirms reliable delivery. Otherwise use direct-IOKit polling only while Lid Dimmer or a lid-restricted scheduler is enabled; no shell process should be spawned every two seconds.
5. Retain the launch-time built-in-display capability. In `MainView`, leave Lid Dimmer visible but disabled with “Available on MacBooks only” on unsupported machines.
6. Put brightness handling behind `BrightnessManager` and restore brightness on every disable, termination, error, and wake path.

Acceptance criteria: the app has no recurring `ioreg` subprocess; lid-dependent features work where supported and degrade safely elsewhere.

## Phase 6 — Minimal window-mode polish

Keep the current title, status, primary sleep button, section order, dividers, dimensions, scheduler controls, and lid-dimmer layout. Do not turn the window into a card-based redesign.

1. Change only the final two action areas at the bottom of the existing window.
2. Present **Launch at Login** as a labelled settings row with a native toggle and a short secondary explanation, replacing the imperative enable/disable button label.
3. Present **Window Mode / Menu Bar Mode** as a labelled settings row with a native segmented picker or toggle. Preserve the current behavior and clearly explain that menu-bar mode hides the main window.
4. Keep the existing divider before this settings area; use subtle background grouping, standard control widths, and alignment so these two rows read as settings rather than unrelated buttons.
5. Preserve keyboard navigation, VoiceOver labels, and the current window size. Test both mode transitions and launch-at-login state refresh.

Acceptance criteria: the screen layout remains recognizably unchanged, while the bottom two actions are clearer, stateful settings controls instead of stacked command buttons.

## Release gates

- Unit tests for scheduler, capability gating, battery thresholds, and power-setting restoration.
- Manual hardware testing on one Apple Silicon Mac and one Intel Mac.
- Two release bundles are published; each executable contains only its intended architecture.
- No periodic `ioreg` subprocess or `pmset -g batt` regex remains.
- The managed sudoers file is validated, limited to required operations, and a prior broad app-managed rule migrates safely.
- Verify restoring sleep and brightness after disabling the app feature, cancelling a schedule, a low-battery trigger, and app termination.
