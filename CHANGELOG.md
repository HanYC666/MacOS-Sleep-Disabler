# Changelog

Significant changes to Sleep Disabler for macOS and Linux are listed newest first. Dates follow Git author dates. Each entry identifies its commit; related audit-only follow-ups are grouped where noted. Version labels mark historical releases or designated milestones, not every commit. The v1.4.4 and v1.4.5 milestones are assigned retrospectively here; this does not create Git tags or change application metadata.

## [2026-10-24] - v1.4.5 - Native macOS archives

Commit: `059dd10`

### Changed

- Rebuilt the separate Apple Silicon and Intel ZIPs with bundles named `Sleep Disabler.app` using Xcode Command Line Tools.
- Updated installation and release documentation with successful native builds, architecture and ad-hoc signature verification, 30 isolated policy checks, and maintainer-confirmed manual functionality testing.

## [2026-10-24] - Repository housekeeping

Commit: `19689df`

### Changed

- Removed the tracked `.DS_Store` file from Git and added a repository-wide ignore rule for Finder metadata.

## [2026-10-24] - macOS configurable lid locking

Commit: `b8d38eb`

### Added

- Added a saved, optional **Lock Screen When Lid Closes** setting for MacBooks, active only while sleep prevention is on, with a matching checked menu-bar item.
- Added isolated Swift lid-transition tests covering 30 policy checks without calling system locking or sleep APIs.

### Changed

- Grouped locking and dimming in a native **Lid Behavior** settings group with descriptive captions, system icons, and switches. The window scrolls to keep all settings accessible.
- Reused IOKit lid monitoring independently of brightness support and guarded locking against startup, unreadable readings, repeated closed-lid events, and preference or prevention changes.
- Included the lid-lock policy in native archive compilation.

## [2026-10-24] - Linux configurable lid and suspend locking

Commit: `747368d`

### Added

- Added a saved **Lock screen when lid closes** preference and a synchronized GNOME panel switch, enabled by default.

### Changed

- Made lid, manual Suspend, and guarded sleep-key locking conditional on the preference while retaining sleep prevention when locking is disabled.
- Updated agent, panel, installer, and key-guard tests for both locking modes.

## [2026-10-22] - v1.4.4 - Linux manual-sleep safeguards

Commit: `da892f6`

### Added

- Added an administrator-installed systemd sleep guard for suspend, hibernate, hybrid sleep, and suspend-then-hibernate, with protected-file and loaded-condition verification.
- Added journaled GNOME sleep-key guarding and panel interception of manual Suspend while prevention is active.

### Fixed

- Kept prevention active when its shortcut recovery journal disappears, notified the user, and retained in-memory recovery information for the running process.
- Added restoration through schema defaults when the journal remains missing, preserving intervening user edits.

## [2026-10-09] - Linux isolated verification and audit record

Commit: `38cd886`; related audit records: `07df68e` through `5ff6543`.

### Changed

- Expanded fake-bus lifecycle and failsafe fixtures and recorded isolated sleep and scheduling verification.
- Documented source audits, historical failure cases, bounded verification results, and outstanding live GNOME and hardware checks. These records do not establish physical suspend or brightness behavior.

## [2026-10-09] - Linux diagnostic retention

Commit: `71e6414`

### Fixed

- Preserved unrelated diagnostics across successful operations and added regression coverage for error ownership.

## [2026-10-09] - Linux asynchronous test migration

Commit: `ae3605c`

### Changed

- Migrated agent fixtures to asynchronous operation handling and recorded the plan 0.8 audit and remaining verification boundaries.

## [2026-10-09] - Linux lifecycle and recovery safeguards

Commit: `422e07b`

### Changed

- Added owner-checked GNOME panel registration, bounded asynchronous operations, caller admission limits, and bus-loss cleanup.
- Strengthened brightness journaling, readback, restoration, and shutdown handling for uncertain writes.
- Verified effective service and XDG paths during installation and guarded state-directory migration.
- Added final suspend capability checks after inhibitor release and expanded agent, installer, CLI, and panel tests.

## [2026-10-07] - Linux transactional installation

Commit: `02008e0`

### Added

- Added staged core and panel deployment with readiness checks, rollback, retained backups, installation locking, and interrupted-transaction records.

### Changed

- Strengthened sleep transaction preflight, failsafe power rechecks, brightness recovery, and panel state synchronization.
- Expanded isolated deployment and runtime tests and documented separate agent and panel installation outcomes.

## [2026-10-06] - Shared project documentation

Commit: `90c4eee`

### Changed

- Restored a shared root README with platform guidance and AGPL-3.0 licensing information.

## [2026-10-06] - Linux inhibition and suspend safeguards

Commit: `0938b69`

### Changed

- Tracked GNOME inhibitor ownership and uncertain releases explicitly, preventing suspend while inhibitor release is unresolved.
- Added identity-checked brightness recovery, conservative brightness capability detection, and suspend/resume clock reconciliation.
- Expanded state-machine tests and distinguished experimental GNOME compatibility from verified hardware support.

## [2026-10-05] - Linux GNOME Wayland prototype

Commit: `d1b829e`

### Added

- Added a Python agent, D-Bus CLI, GNOME Shell panel extension, and per-user systemd installation for the experimental Linux implementation.
- Added logind and GNOME inhibitors, UPower battery/lid monitoring, countdown scheduling, battery failsafe, and optional brightness handling.

### Changed

- Reorganized the repository into `macOS/` and `linux/` platform directories.

## [2026-09-15] - 1.4.3

Commit: `af6ff84`

### Fixed

- Fixed menu-bar window actions failing to reopen a closed main window or show it after switching to another desktop.

## [2026-09-15] - 1.4.2

Commit: `6c7632b`

### Changed

- Updated the separate single-architecture Apple Silicon and Intel release archives to build exclusively with Xcode Command Line Tools.
- Added startup detection and automatic migration of the recognized legacy app-managed `pmset` permission rule.

### Fixed

- Preserved the proven `v1.3.0` sleep-button route so disabling sleep no longer depends on capturing both active and inactive power-source sections.
- Fixed restricted-permission verification after administrator authorization and added an explanation before the macOS password prompt.
- Prevented the main window from briefly appearing at launch when Menu Bar mode was saved.
- Persisted the Lid-Closed Dimmer setting to the preferences file immediately and restored its boolean value at startup.

## [2026-09-15] - macOS Command Line Tools build procedure

Commit: `3c5373d`

### Changed

- Updated native archive compilation and related instructions to use Xcode Command Line Tools without the Xcode application.

## [2026-09-14] - 1.4.1

Commit: `dadb481`

### Added

- Added separate native Apple Silicon and Intel release archive automation.
- Added structured IOKit battery monitoring and power-source change notifications.
- Added capability-gated lid dimmer availability and native settings controls for login and app mode.
- Added automatic release-mode detection for notarized Developer ID archives or locally runnable development/ad-hoc archives.
- Added persisted ownership tracking and a complete-restoration-baseline requirement for app-managed sleep settings.

### Changed

- Split power management, battery monitoring, lid monitoring, brightness, scheduling, privilege policy, and menu-bar behavior out of `AppState`.
- Replaced the app-managed unrestricted `pmset` sudoers rule with a validated, versioned policy limited to the commands the app uses.
- Replaced the recurring `ioreg` subprocess with direct IOKit lid reads, interest notifications, workspace power notifications, and conditional fallback monitoring.
- Updated the Battery Power Failsafe to use the system-level IOKit power-provider value and re-evaluate immediately when its settings change.
- Updated Lid-Closed Dimmer to resolve optional DisplayServices symbols at runtime and stay disabled when they are unavailable.
- Updated native archives to verify their single architecture, sign exported bundles, and staple notarization before packaging when distribution credentials are available.

### Fixed

- Fixed partially failed sleep-disable operations by rolling settings back to their captured baseline.
- Fixed optional private brightness symbols preventing the application from linking.

## [2026-09-07] - 1.3.0

Commit: `6df7221`

### Added

- Added Sleep Scheduler, which can force the Mac to sleep after a configurable days, hours, and minutes countdown.
- Added persistent scheduler duration settings and restored them automatically at launch.
- Added a menu-bar quick action to start the last configured scheduler duration.
- Added a live remaining-time row to the menu-bar menu while a scheduler is active.
- Added an optional closed-lid condition that prevents starting the scheduler with an open lid and cancels an active scheduler when the lid opens.
- Added an optional sleep-disabled condition that only permits scheduling while Sleep Disabler is active and cancels the scheduler when normal sleep is restored.
- Added scheduler documentation to the README.

### Changed

- Updated forced-sleep handling to restore disabled sleep settings before issuing `pmset sleepnow`.
- Rebuilt the distributable `Sleep Disabler.app` bundle with the scheduler changes.

### Fixed

- Enabled continuous press-and-hold tracking for the native scheduler steppers.
- Fixed the scheduler control layout so numeric-width changes do not move the stepper arrows.

## [2026-08-27] - macOS requirements documentation

Commit: `0475759`

### Changed

- Updated the README with the measured memory footprint and clarified that macOS 13.5 or later is required.
- Linked the Command Line Tools build guide from the source-build instructions.

## [2026-08-27] - 1.2.0

Commit: `804506c`

### Added

- Added Battery Power Failsafe, including a persisted on/off setting and a configurable 1-99% battery threshold.
- Added periodic battery-state monitoring that detects discharge on battery power.
- Added a critical-battery notification window and a forced system-sleep action when the failsafe threshold is reached.
- Added Lid-Closed Dimmer, which uses `ioreg` to detect the clamshell state and `DisplayServices` to set main-display brightness to zero while sleep is disabled.
- Added restoration of the previous display brightness when the lid is opened or the dimmer is no longer applicable.
- Added a native `NSStepper` control for adjusting the battery failsafe threshold.
- Added a Command Line Tools build guide and a distributable app bundle to the release.
- Added complete application metadata to the bundle plist, including name, executable, identifier, version, and macOS minimum version.

### Changed

- Reworked the main window into distinct sleep-control, battery-failsafe, lid-dimmer, login-item, and mode-selection sections.
- Reworked menu-bar mode with controls for the lid dimmer alongside sleep, window, mode, login-item, and quit actions.
- Changed sleep-state management to capture the current AC and battery `sleep` and `displaysleep` values before disabling sleep.
- Changed restoration to apply the saved AC and battery settings instead of resetting all machines to hardcoded defaults.
- Changed `pmset` calls to run non-interactively through `sudo -n`.
- Enlarged and stabilized the main window, including hiding instead of destroying it when closed.
- Updated app reopening so menu-bar mode can present the existing window without creating a duplicate.
- Reworked the README around installation, permissions, build, and runtime behavior.

### Fixed

- Prevented periodic sleep-state refreshes from overwriting an in-progress manual sleep toggle.
- Restored the correct activation policy when showing, hiding, or reopening the app window.
- Excluded the battery-failsafe notification window from normal window hiding and lookup.
- Kept the app's original power-management settings available for restoration after disabling sleep.

## [2026-08-18] - macOS documentation reorganization

Commit: `98152ff`

### Changed

- Reorganized the README into a concise product overview, requirements, installation, permissions, build instructions, and technical behavior reference.
- Clarified the manual `sudoers` setup flow and documented the exact `pmset` commands used to disable and restore sleep.
- Standardized the README title and disclosure language.

## [2026-08-15] - Workload guidance

Commit: `8cc9e00`

### Changed

- Added README guidance for long-running unsupervised, training, and agentic workloads.

## [2026-08-15] - AI-use disclosure

Commit: `7c82952`

### Added

- Added an AI-use disclosure to the README.

## [2026-05-30] - 1.0.0

Commit: `c5c0494`

### Added

- Initial release of Sleep Disabler for macOS.
- SwiftUI application with a standard window mode and a persistent menu-bar mode.
- Controls to disable and re-enable system sleep and display sleep through `pmset`.
- Launch-at-login support through `SMAppService`.
- Persisted application mode through `UserDefaults`.
- Automatic check for the required password-free `pmset` permission rule, with a guided installation option.
- Xcode project, macOS app assets, bundle configuration, and source-build documentation.
