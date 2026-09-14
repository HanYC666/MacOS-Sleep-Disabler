#!/bin/zsh
set -euo pipefail

# Creates two independently compiled, single-architecture bundles. It selects
# notarized Developer ID distribution only when this Mac has both a Developer ID
# Application identity and a verified notarytool keychain profile. Otherwise it
# produces a local-development-signed bundle (or an ad-hoc local bundle).

project_path="Sleep Disabler.xcodeproj"
scheme="Sleep Disabler"
output_directory="${1:-build/native-archives}"
notary_profile="${NOTARYTOOL_PROFILE:-}"

die() {
  print -u2 -- "error: $*"
  exit 1
}

first_identity_matching() {
  local label="$1"
  /usr/bin/security find-identity -v -p codesigning 2>/dev/null \
    | /usr/bin/sed -n "s/.*\"\(${label}: [^\"]*\)\".*/\1/p" \
    | /usr/bin/sed -n '1p'
}

developer_id_identity="$(first_identity_matching 'Developer ID Application')"
local_identity="$(first_identity_matching 'Apple Development')"
if [[ -z "${local_identity}" ]]; then
  local_identity="$(first_identity_matching 'Mac Development')"
fi

release_mode="local"
signing_identity="${local_identity}"

if [[ -n "${developer_id_identity}" && -n "${notary_profile}" ]]; then
  command -v xcrun >/dev/null || die "Xcode is required for notarytool."
  # This is a read-only credentials/service preflight. A configured profile is
  # not enough on its own: it must successfully authenticate with Apple.
  if xcrun notarytool history --keychain-profile "${notary_profile}" >/dev/null 2>&1; then
    release_mode="notarized"
    signing_identity="${developer_id_identity}"
  else
    die "Developer ID identity found, but NOTARYTOOL_PROFILE '${notary_profile}' could not authenticate with Apple's notary service."
  fi
elif [[ -n "${notary_profile}" && -z "${developer_id_identity}" ]]; then
  die "NOTARYTOOL_PROFILE is set, but no Developer ID Application identity is installed."
fi

if [[ "${release_mode}" == "notarized" ]]; then
  print -- "Release mode: Developer ID signing and Apple notarization."
else
  if [[ -n "${signing_identity}" ]]; then
    print -- "Release mode: local development signing with ${signing_identity}."
  else
    signing_identity="-"
    print -- "Release mode: ad-hoc local signing (no Apple code-signing identity found)."
  fi
fi

sign_bundle() {
  local app_path="$1"
  if [[ "${release_mode}" == "notarized" ]]; then
    /usr/bin/codesign --force --deep --options runtime --timestamp --sign "${signing_identity}" "${app_path}"
  else
    /usr/bin/codesign --force --deep --timestamp=none --sign "${signing_identity}" "${app_path}"
  fi
  /usr/bin/codesign --verify --deep --strict --verbose=2 "${app_path}"
}

notarize_bundle() {
  local app_path="$1"
  local architecture="$2"
  local upload_zip="${output_directory}/Sleep-Disabler-${architecture}-notary-upload.zip"

  # Apple accepts a ZIP upload, but tickets are stapled to the app before the
  # final distribution ZIP is made.
  /usr/bin/ditto -c -k --keepParent "${app_path}" "${upload_zip}"
  xcrun notarytool submit "${upload_zip}" --keychain-profile "${notary_profile}" --wait
  xcrun stapler staple "${app_path}"
  xcrun stapler validate "${app_path}"
}

archive_one() {
  local architecture="$1"
  local archive_path="${output_directory}/Sleep-Disabler-${architecture}.xcarchive"
  local archive_app="${archive_path}/Products/Applications/Sleep Disabler.app"
  local release_app="${output_directory}/Sleep-Disabler-${architecture}.app"
  local release_zip="${output_directory}/Sleep-Disabler-${architecture}.zip"

  [[ ! -e "${release_app}" ]] || die "Refusing to overwrite existing ${release_app}; choose a new output directory."
  [[ ! -e "${release_zip}" ]] || die "Refusing to overwrite existing ${release_zip}; choose a new output directory."

  xcodebuild archive \
    -project "${project_path}" \
    -scheme "${scheme}" \
    -configuration Release \
    -destination 'generic/platform=macOS' \
    -archivePath "${archive_path}" \
    ARCHS="${architecture}" \
    ONLY_ACTIVE_ARCH=NO \
    CODE_SIGNING_ALLOWED=NO

  [[ -d "${archive_app}" ]] || die "Archive did not contain ${archive_app}."
  /usr/bin/ditto "${archive_app}" "${release_app}"
  sign_bundle "${release_app}"

  local built_architectures
  built_architectures="$(lipo -archs "${release_app}/Contents/MacOS/Sleep Disabler")"
  [[ "${built_architectures}" == "${architecture}" ]] || die "Expected only ${architecture}, got: ${built_architectures}."

  if [[ "${release_mode}" == "notarized" ]]; then
    notarize_bundle "${release_app}" "${architecture}"
  fi

  /usr/bin/ditto -c -k --keepParent "${release_app}" "${release_zip}"
  print -- "Created ${release_app} and ${release_zip} (${architecture})."
}

mkdir -p "${output_directory}"
archive_one arm64
archive_one x86_64
