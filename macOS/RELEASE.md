# Native release procedure

Sleep Disabler ships two separate apps, not a universal binary:

- `Sleep-Disabler-arm64.app` for Apple Silicon Macs.
- `Sleep-Disabler-x86_64.app` for Intel Macs.

Run the archive script from the repository's `macOS` directory. Do not run it with `sudo` and do
not run it from inside the `Scripts` directory.

```zsh
cd "/path/to/MacOS-Sleep-Disabler/macOS"
zsh Scripts/test-lid-lock.sh
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

## Lid-lock release validation

The current lid-lock implementation is unreleased source; the existing ZIPs do not contain it. Before replacing release downloads:

- Run the isolated policy tests and compile both architectures. `LidLockPolicy.swift` must be included alongside the app and view sources; the archive script already does this. These tests cannot lock or suspend the host.
- On a MacBook, inspect **Lid Behavior** in light and dark appearance, check caption wrapping and scrolling at the smallest window size, and verify that the window and checked menu-bar item stay synchronized.
- Verify the default-off preference survives enabling, disabling, and relaunching. Confirm that unavailable lid monitoring or the private `SACLockScreenImmediate` symbol disables the control without preventing launch, and that locking works with the dimmer off.
- In an attended hardware session, verify one lock per open-to-closed transition while prevention is active and the option is on; verify no app-requested lock with the option off or prevention off. Startup or enabling the option with an already-closed lid must not lock. Native macOS automatic-lock policy may still apply independently.
- Complete the existing signing/notarization and architecture checks before publishing rebuilt ZIPs. A Linux syntax parse does not establish Swift type correctness, private API behavior, or native appearance.

Actual closure testing can lock the session, and testing with prevention off can allow ordinary system sleep. Keep it separate from isolated validation and run it only in an attended session where those outcomes are intended. The macOS option controls lid locking; it does not reroute manual Sleep actions.
