# Compile Guide

This project builds with Apple's Xcode Command Line Tools only. The archive script does not invoke the Xcode app or `xcodebuild`.

1. Open Terminal and navigate into the repository's `macOS` folder:

```bash
cd "/path/to/MacOS-Sleep-Disabler/macOS"
```

2. Run the isolated lid-lock policy tests, then the archive script from that folder. Do not use `sudo` and do not run them from the `Scripts` folder:

```bash
zsh Scripts/test-lid-lock.sh
zsh Scripts/archive-native.sh
```

The test executable contains only the transition policy: it cannot lock the screen or suspend the computer. It checks preference/prevention gating, startup, repeated closure, unknown lid readings, and baseline resets. This is separate from native UI and hardware validation. The archive includes `ContentView.swift`, `LidLockPolicy.swift`, and `Sleep_DisablerApp.swift`.

3. The script creates two independent native apps under `build/native-archives`. To install without launching, copy the matching app into Applications using Finder; quit an older copy before replacing it. To launch when ready, use only the app for your Mac:

```bash
# Apple Silicon
open "build/native-archives/Sleep-Disabler-arm64.app"

# Intel
open "build/native-archives/Sleep-Disabler-x86_64.app"
```

The script uses an installed development certificate when available, otherwise it creates an ad-hoc signature for local use. See [RELEASE.md](RELEASE.md) for Developer ID notarization.

The scheduled-sleep warning links Apple's `UserNotifications` framework; the archive script includes it. After building, allow notifications when starting a timer and select Banners for Sleep Disabler in System Settings → Notifications. Check final-minute delivery and timer cancellation as described in [RELEASE.md](RELEASE.md). Existing repository ZIPs do not yet include this source change.

Native compilation also includes `Sleep Disabler/BatteryFailsafePolicy.swift` for low-battery episode tracking and final dispatch validation. Its isolated regression checks are in `Tests/BatteryFailsafePolicyTests.swift`; compile these two files together to run the policy without invoking system power APIs.

If output files already exist, pass a new directory, such as `zsh Scripts/archive-native.sh build/lid-lock-review`. A source update does not update previously generated apps or ZIPs. After launching a new build, the optional locking switch appears under **Lid Behavior** and in the menu-bar menu; it defaults off and operates only while sleep prevention is active.
