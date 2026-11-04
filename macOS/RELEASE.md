# Native release procedure

Sleep Disabler ships two separate apps, not a universal binary:

- `Sleep-Disabler-arm64.app` for Apple Silicon Macs.
- `Sleep-Disabler-x86_64.app` for Intel Macs.

These are the archive script's output names. For distribution, rename each app to `Sleep Disabler.app` in a separate staging directory, then compress it as `Sleep-Disabler-arm64.zip` or `Sleep-Disabler-x86_64.zip` respectively. Both distributed ZIPs contain `Sleep Disabler.app`; keep each app paired with its verified architecture.

Run the archive script from the repository's `macOS` directory. Do not run it with `sudo` and do
not run it from inside the `Scripts` directory.

```zsh
cd "/path/to/MacOS-Sleep-Disabler/macOS"
zsh Scripts/test-lid-lock.sh
zsh Scripts/test-lid-brightness.sh
zsh Scripts/archive-native.sh
```

The script uses only Apple's Xcode Command Line Tools. It does not invoke the
Xcode app or `xcodebuild`.

For an ordinary development Mac, the output is signed with an available Apple
Development or Mac Development certificate. If neither exists, the script uses
an ad-hoc signature so the apps can run locally.

For an official release, install a Developer ID Application certificate and set
`NOTARYTOOL_PROFILE` to a notarytool keychain profile that can authenticate with
Apple:

```zsh
NOTARYTOOL_PROFILE="profile-name" zsh Scripts/archive-native.sh
```

The script verifies the identity and profile before building, signs with the
hardened runtime, notarizes each app separately, staples each ticket, and then
creates the final ZIP files. It also verifies that each executable contains only
its requested architecture.

## Validation status

The current repository ZIPs include the battery-failsafe repeat protection. Isolated policy tests and both native builds passed. In an attended session, verify that resuming below the threshold does not repeat sleep, reconnecting AC during the restoration delay cancels queued failsafe sleep, and a new low-battery episode can trigger after AC or battery recovery. Explicit failsafe settings changes rearm the policy and invalidate queued requests. Unknown power data cannot authorize sleep or rearm the episode; app restart resets in-memory episode tracking. These hardware paths have not yet been manually verified for this patch.

The current ZIPs also include the scheduled-sleep warning. Banner delivery has not been manually verified. Test a two-minute timer: allow notification permission, confirm one Sleep Disabler warning in the final minute in foreground and menu-bar mode, then cancel the timer before expiry. Also check a one-minute timer and denied notifications. macOS notification style and Focus settings control visibility; the warning does not postpone sleep or alter the immediate battery failsafe.

Both repository ZIPs have been rebuilt with the window and lid-dimmer repairs using Xcode Command Line Tools. The builds passed single-architecture and local ad-hoc signature verification. An empty SwiftUI `Settings` scene in the first window repair created a separate blank window; the current build uses an AppKit launch lifecycle and hosts the existing SwiftUI controls only inside the requested main window. A live launch with Menu Bar mode saved confirmed accessory activation and only menu bar window records, with no main window. The lid dimmer now keeps the pre-close value during a 30-second reopen settling period and verifies the actual brightness before accepting a new baseline. Isolated repeated-cycle checks passed. The earlier v1.4.5 archive was manually tested by the maintainer; the later fixes require a new attended check. In Menu Bar mode, confirm there is no startup window or Dock/Command-Tab presence, and that **Show Window** works after closing and across desktops. With prevention and the lid dimmer active on a MacBook, confirm brightness returns to its open-lid value after repeated close/open cycles, including a rapid reopen and a cycle lasting more than 30 seconds. This archive update is recorded in the shared [changelog](../CHANGELOG.md).

Use the following checklist for subsequent releases:

- For subsequent builds, run the isolated policy tests and compile both architectures. `LidLockPolicy.swift` and `LidBrightnessPolicy.swift` must be included alongside the app and view sources; the archive script already does this. These tests cannot change brightness, lock, or suspend the host.
- On a MacBook, inspect **Lid Behavior** in light and dark appearance, check caption wrapping and scrolling at the smallest window size, and verify that the window and checked menu-bar item stay synchronized.
- Verify the default-off preference survives enabling, disabling, and relaunching. Confirm that unavailable lid monitoring or the private `SACLockScreenImmediate` symbol disables the control without preventing launch, and that locking works with the dimmer off.
- In an attended hardware session, verify one lock per open-to-closed transition while prevention is active and the option is on; verify no app-requested lock with the option off or prevention off. Startup or enabling the option with an already-closed lid must not lock. Native macOS automatic-lock policy may still apply independently.
- Complete the existing signing/notarization and architecture checks before publishing rebuilt ZIPs. A Linux syntax parse does not establish Swift type correctness, private API behavior, or native appearance.

Actual closure testing can lock the session, and testing with prevention off can allow ordinary system sleep. Keep it separate from isolated validation and run it only in an attended session where those outcomes are intended. The macOS option controls lid locking; it does not reroute manual Sleep actions.
