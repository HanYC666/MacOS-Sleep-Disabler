# Native release procedure

Sleep Disabler ships two separate native app bundles, never a universal app:

- `Sleep-Disabler-arm64.app` for Apple Silicon.
- `Sleep-Disabler-x86_64.app` for Intel Macs.

From the project root, run `zsh Scripts/archive-native.sh` (do not use `sudo`).
The script uses project-relative source and plist paths, so running it from the
`Scripts` directory will fail. It uses only the Command Line Tools Swift compiler
and macOS SDK to compile one `arm64` and one `x86_64` bundle, then produces
separately named `.app` and `.zip` artifacts. It does not require or invoke the
Xcode app or `xcodebuild`.

```zsh
cd "/path/to/Sleep Disabler"
zsh Scripts/archive-native.sh
```

The script selects its release mode before building:

- **Notarized distribution:** Set `NOTARYTOOL_PROFILE` to a working notarytool
  keychain profile. The machine must also have a **Developer ID Application**
  identity. The script preflights that profile with Apple, signs using hardened
  runtime and a secure timestamp, submits each app, staples its ticket, validates
  it, and only then creates the distribution ZIP.
- **Local development:** Without `NOTARYTOOL_PROFILE`, the script signs with an
  installed Apple Development or Mac Development identity. With no usable Apple
  identity it creates an ad-hoc-signed bundle for local use. These artifacts are
  intentionally not represented as notarized distribution builds.

If a profile is supplied but cannot authenticate, or a Developer ID identity is
missing, the script stops instead of silently creating a non-notarized release.
It also refuses to overwrite an existing output bundle or ZIP. Before
publication, the script itself verifies with `lipo -archs` that each executable
reports only its intended architecture.

Test normal sleep control, restoration, scheduler completion, battery failsafe,
login-item state, and both app modes on one Mac from each architecture. Test
lid dimming only on a notebook with a built-in display; the feature remains
disabled on desktops.
