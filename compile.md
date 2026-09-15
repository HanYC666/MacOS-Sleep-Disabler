# Compile Guide

This project builds with Apple's Xcode Command Line Tools only. The archive script does not invoke the Xcode app or `xcodebuild`.

1. Open Terminal and navigate into the main project folder:

```bash
cd "path/to/Sleep Disabler"
```

2. Run the archive script from that project root. Do not use `sudo` and do not run it from the `Scripts` folder:

```bash
zsh Scripts/archive-native.sh
```

3. The script creates two independent native apps under `build/native-archives`. Open only the one for the Mac you are using:

```bash
# Apple Silicon
open "build/native-archives/Sleep-Disabler-arm64.app"

# Intel
open "build/native-archives/Sleep-Disabler-x86_64.app"
```

The script uses an installed development certificate when available, otherwise it creates an ad-hoc signature for local use. See [RELEASE.md](RELEASE.md) for Developer ID notarization.
