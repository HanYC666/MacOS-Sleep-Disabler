# Changelog

All notable changes to Sleep Disabler are documented here. Versions without an existing Git tag are chronological patch versions inferred from the commit history.

## [2026-09-15] - 1.4.3

### Fixed

- Fixed menu-bar window actions failing to reopen a closed main window or show it after switching to another desktop.

## [2026-09-15] - 1.4.2

### Changed

- Updated the separate single-architecture Apple Silicon and Intel release archives to build exclusively with Xcode Command Line Tools.
- Added startup detection and automatic migration of the recognized legacy app-managed `pmset` permission rule.

### Fixed

- Preserved the proven `v1.3.0` sleep-button route so disabling sleep no longer depends on capturing both active and inactive power-source sections.
- Fixed restricted-permission verification after administrator authorization and added an explanation before the macOS password prompt.
- Prevented the main window from briefly appearing at launch when Menu Bar mode was saved.
- Persisted the Lid-Closed Dimmer setting to the preferences file immediately and restored its boolean value at startup.

## [2026-09-14] - 1.4.1

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

## [2026-08-27] - 1.2.1

### Changed

- Updated the README with the measured memory footprint and clarified that macOS 13.5 or later is required.
- Linked the Command Line Tools build guide from the source-build instructions.

## [2026-08-27] - 1.2.0

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

## [2026-08-18] - 1.0.3

### Changed

- Reorganized the README into a concise product overview, requirements, installation, permissions, build instructions, and technical behavior reference.
- Clarified the manual `sudoers` setup flow and documented the exact `pmset` commands used to disable and restore sleep.
- Standardized the README title and disclosure language.

## [2026-08-15] - 1.0.2

### Changed

- Added README guidance for long-running unsupervised, training, and agentic workloads.

## [2026-08-15] - 1.0.1

### Added

- Added an AI-use disclosure to the README.

## [2026-05-30] - 1.0.0

### Added

- Initial release of Sleep Disabler for macOS.
- SwiftUI application with a standard window mode and a persistent menu-bar mode.
- Controls to disable and re-enable system sleep and display sleep through `pmset`.
- Launch-at-login support through `SMAppService`.
- Persisted application mode through `UserDefaults`.
- Automatic check for the required password-free `pmset` permission rule, with a guided installation option.
- Xcode project, macOS app assets, bundle configuration, and source-build documentation.
