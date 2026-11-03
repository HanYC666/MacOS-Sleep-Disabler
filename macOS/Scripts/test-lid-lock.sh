#!/bin/zsh
set -euo pipefail

project_directory="${0:A:h:h}"
test_directory="$(mktemp -d "${TMPDIR:-/tmp}/sleep-disabler-lid-tests.XXXXXX")"
test_architecture="$(/usr/bin/uname -m)"
# Keep the executable in the temporary directory for inspection. These tests
# contain only the transition policy and cannot lock or suspend the host.
"/Library/Developer/CommandLineTools/usr/bin/swiftc" \
  -module-cache-path "${test_directory}/module-cache" \
  -target "${test_architecture}-apple-macosx13.5" \
  -sdk "/Library/Developer/CommandLineTools/SDKs/MacOSX.sdk" \
  "${project_directory}/Sleep Disabler/LidLockPolicy.swift" \
  "${project_directory}/Tests/LidLockPolicyTests.swift" \
  -o "${test_directory}/lid-lock-tests"
"${test_directory}/lid-lock-tests"
