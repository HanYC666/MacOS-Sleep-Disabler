# Native release procedure

Sleep Disabler ships two separate apps, not a universal binary:

- `Sleep-Disabler-arm64.app` for Apple Silicon Macs.
- `Sleep-Disabler-x86_64.app` for Intel Macs.

Run the archive script from the project root. Do not run it with `sudo` and do
not run it from inside the `Scripts` directory.

```zsh
cd "/path/to/Sleep Disabler"
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
