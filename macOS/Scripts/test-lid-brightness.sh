#!/bin/zsh
set -euo pipefail

project_directory="${0:A:h:h}"
test_directory="$(mktemp -d "${TMPDIR:-/tmp}/sleep-disabler-brightness-tests.XXXXXX")"
test_architecture="$(/usr/bin/uname -m)"
"/Library/Developer/CommandLineTools/usr/bin/swiftc" \
  -module-cache-path "${test_directory}/module-cache" \
  -target "${test_architecture}-apple-macosx13.5" \
  -sdk "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk" \
  "${project_directory}/Sleep Disabler/LidBrightnessPolicy.swift" \
  "${project_directory}/Tests/LidBrightnessPolicyTests.swift" \
  -o "${test_directory}/lid-brightness-tests"
"${test_directory}/lid-brightness-tests"
