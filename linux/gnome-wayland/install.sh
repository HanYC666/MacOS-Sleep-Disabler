#!/bin/sh
set -eu

base_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
extension_id='sleep-disabler@local'
extension_target="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$extension_id"
program_target="$HOME/.local/lib/sleep-disabler-gnome"
bin_target="$HOME/.local/bin"
python_bin=${PYTHON:-/usr/bin/python3}
case "${XDG_CONFIG_HOME:-$HOME/.config}" in
    /*) ;;
    *) printf '%s\n' 'XDG_CONFIG_HOME must be an absolute path.' >&2; exit 1 ;;
esac
case "${XDG_DATA_HOME:-$HOME/.local/share}" in
    /*) ;;
    *) printf '%s\n' 'XDG_DATA_HOME must be an absolute path.' >&2; exit 1 ;;
esac
case "${XDG_STATE_HOME:-$HOME/.local/state}" in
    /*) ;;
    *) printf '%s\n' 'XDG_STATE_HOME must be an absolute path.' >&2; exit 1 ;;
esac

case ":${XDG_CURRENT_DESKTOP:-}:" in
    *GNOME*|*gnome*) ;;
    *) printf '%s\n' 'Run this installer from a GNOME desktop session.' >&2; exit 1 ;;
esac
if [ "${XDG_SESSION_TYPE:-}" != wayland ]; then
    printf '%s\n' 'Run this installer from your GNOME Wayland desktop session.' >&2
    exit 1
fi
if ! "$python_bin" -c 'import dbus, gi' >/dev/null 2>&1; then
    printf '%s\n' 'Install dependencies first: sudo apt install python3-dbus python3-gi' >&2
    exit 1
fi
python_bin=$(command -v "$python_bin") || {
    printf '%s\n' 'Selected Python interpreter was not found.' >&2
    exit 1
}
case "$python_bin" in
    /*) ;;
    *) python_bin="$PWD/$python_bin" ;;
esac
if [ "${1:-}" != '--under-install-lock' ]; then
    exec "$python_bin" "$base_dir/core_install.py" --python "$python_bin" --run-installer
fi
if [ -z "${SLEEP_DISABLER_INSTALL_LOCK_FD:-}" ]; then
    printf '%s\n' 'Installer lock descriptor is missing.' >&2
    exit 1
fi
core_committed=false
core_commit_receipt="$HOME/.local/lib/.sleep-disabler-core-commit.$$"
if [ -e "$core_commit_receipt" ] || [ -L "$core_commit_receipt" ]; then
    printf '%s\n' "Prior commit receipt retained at $core_commit_receipt; no deployment started." >&2
    exit 1
fi
panel_switch_started=false
handle_interrupt() {
    trap '' INT TERM
    if [ -f "$core_commit_receipt" ]; then core_committed=true; fi
    if [ "$core_committed" != true ]; then
        printf '%s\n' 'Installation interrupted before core commit; inspect core rollback diagnostics.' >&2
        exit 1
    fi
    if [ "$panel_switch_started" = true ]; then
        rollback_panel 'Panel installation interrupted after core commit.'
    fi
    printf '%s\n' 'Panel staging interrupted; the core and CLI remain usable. Staging files are retained.' >&2
    exit 2
}
trap handle_interrupt INT TERM
panel_record="$(dirname -- "$extension_target")/.sleep-disabler-panel-transaction.json"
if [ -e "$panel_record" ] || [ -L "$panel_record" ]; then
    printf '%s\n' "Unfinished panel transaction retained at $panel_record; inspect its paths before retrying. No core or panel files were switched." >&2
    exit 1
fi
previous_panel_runtime=$("$python_bin" "$base_dir/core_install.py" --python "$python_bin" --state-field panelRuntimeVersion 2>/dev/null) || previous_panel_runtime=''
if ! "$python_bin" "$base_dir/core_install.py" --python "$python_bin" --commit-receipt "$core_commit_receipt"; then
    if [ -f "$core_commit_receipt" ]; then
        printf '%s\n' 'Core commit proven, but installer completion was interrupted; the core and CLI remain usable.' >&2
        exit 2
    fi
    exit 1
fi
core_committed=true
extension_parent=$(dirname -- "$extension_target")
if ! install -d "$extension_parent"; then
    printf '%s\n' 'Top-bar directory creation failed; the core and CLI remain usable.' >&2
    exit 2
fi

if ! stage=$(mktemp -d "$extension_parent/.sleep-disabler-stage.XXXXXX"); then
    printf '%s\n' 'Top-bar staging failed; the core and CLI remain usable.' >&2
    exit 2
fi
failed_copy="$extension_parent/.sleep-disabler-failed.$(date +%s).$$"

panel_fail_before_switch() {
    message=$1
    if ! mv "$stage" "$failed_copy"; then
        printf '%s\n' 'Top-bar staging cleanup failed; inspect the staging directory.' >&2
    fi
    printf '%s\n' "$message" >&2
    printf '%s\n' 'The user service and CLI remain installed and usable.' >&2
    exit 2
}

if ! install -m 644 "$base_dir/extension/$extension_id/metadata.json" "$stage/metadata.json" ||
        ! install -m 644 "$base_dir/extension/$extension_id/extension.js" "$stage/extension.js"; then
    panel_fail_before_switch 'Top-bar control pending: staged file copy failed.'
fi

if [ ! -f "$stage/metadata.json" ] || [ ! -f "$stage/extension.js" ]; then
    panel_fail_before_switch 'Top-bar control pending: staged extension is incomplete.'
fi
if ! expected_panel_runtime=$("$python_bin" - "$stage/metadata.json" "$extension_id" "$stage/extension.js" <<'PYJSON'
import json
import re
import sys
with open(sys.argv[1], encoding='utf-8') as stream:
    metadata = json.load(stream)
if metadata.get('uuid') != sys.argv[2]:
    raise SystemExit('extension UUID does not match its installation directory')
if not isinstance(metadata.get('shell-version'), list) or not metadata['shell-version']:
    raise SystemExit('shell-version must be a non-empty list')
version = metadata.get('version')
source = open(sys.argv[3], encoding='utf-8').read()
match = re.search(r"^const PANEL_RUNTIME_VERSION = '([1-9][0-9]*)';$", source, re.M)
if type(version) is not int or version < 1 or not match or str(version) != match[1]:
    raise SystemExit('metadata/runtime version mismatch')
print(version)
PYJSON
)
then
    panel_fail_before_switch 'Top-bar control pending: staged metadata validation failed.'
fi
if command -v node >/dev/null 2>&1 && ! node --input-type=module --check < "$stage/extension.js" >/dev/null 2>&1; then
    panel_fail_before_switch 'Top-bar control pending: staged extension JavaScript is invalid.'
fi
if ! command -v node >/dev/null 2>&1; then
    printf '%s\n' 'Node unavailable: JavaScript syntax precheck skipped; loaded runtime proof remains required.' >&2
fi
if ! command -v gnome-extensions >/dev/null 2>&1; then
    panel_fail_before_switch 'Top-bar control pending: gnome-extensions tool is unavailable.'
fi
gnome_extensions() {
    "$python_bin" "$base_dir/core_install.py" --python "$python_bin" --gnome "$@"
}

list_has_extension() {
    while IFS= read -r item; do
        [ "$item" = "$extension_id" ] && return 0
    done
    return 1
}

previous_installed=false
previous_enabled=false
previous_active=false
[ -d "$extension_target" ] && previous_installed=true
if [ "$previous_installed" = true ] && [ -z "$previous_panel_runtime" ]; then
    previous_panel_runtime=$("$python_bin" - "$extension_target/metadata.json" <<'PYVERSION'
import json
import sys
with open(sys.argv[1], encoding='utf-8') as stream:
    version = json.load(stream).get('version')
if type(version) is not int or version < 1:
    raise SystemExit(1)
print(version)
PYVERSION
    ) || previous_panel_runtime=''
fi
if ! enabled_list=$(gnome_extensions list --enabled 2>/dev/null); then
    panel_fail_before_switch 'Top-bar control pending: GNOME could not report the prior enabled state.'
fi
if printf '%s\n' "$enabled_list" | list_has_extension; then
    previous_enabled=true
fi
if ! active_list=$(gnome_extensions list --active 2>/dev/null); then
    panel_fail_before_switch 'Top-bar control pending: GNOME could not report the prior active state.'
fi
if printf '%s\n' "$active_list" | list_has_extension; then
    previous_active=true
fi

backup="$extension_parent/.sleep-disabler-rollback.${stage##*.}.$$"
archive_panel_record() {
    outcome=$1
    if [ -e "$panel_record" ] && ! mv "$panel_record" "$panel_record.$outcome.$$"; then
        printf '%s\n' "Panel transaction completed; its record remains at $panel_record." >&2
    fi
}

rollback_panel() {
    trap '' INT TERM
    problem=$1
    if [ "$previous_installed" != true ]; then
        printf '%s\n' "$problem" >&2
        if [ ! -d "$extension_target" ] && [ -d "$stage" ]; then
            mv "$stage" "$extension_target" 2>/dev/null || true
        fi
        if [ -d "$extension_target" ]; then
            printf '%s\n' 'Validated first extension install retained in its canonical UUID directory.' >&2
            archive_panel_record 'first-install-pending'
        else
            printf '%s\n' 'Canonical first installation could not be completed; staging and transaction paths are retained.' >&2
        fi
        if retained_enabled=$(gnome_extensions list --enabled 2>/dev/null); then
            if printf '%s\n' "$retained_enabled" | list_has_extension; then
                printf '%s\n' 'Retained extension is configured enabled; active runtime remains unproven.' >&2
            else
                printf '%s\n' 'Retained extension is not configured enabled; active runtime remains unproven.' >&2
            fi
        else
            printf '%s\n' 'Retained extension enablement is unknown; active runtime remains unproven.' >&2
        fi
        printf '%s\n' 'The user service and CLI remain installed and usable.' >&2
        printf '%s\n' 'Log out and in, then run: gnome-extensions enable sleep-disabler@local' >&2
        printf '%s\n' 'Rerun this installer to verify the exact loaded runtime.' >&2
        exit 2
    fi
    rollback_ok=true
    gnome_extensions disable "$extension_id" >/dev/null 2>&1 || true
    if [ -d "$backup" ]; then
        if [ -d "$extension_target" ] && ! mv "$extension_target" "$failed_copy"; then rollback_ok=false; fi
        # mv into an existing directory nests the backup instead of restoring it.
        if [ -e "$extension_target" ] || ! mv "$backup" "$extension_target"; then rollback_ok=false; fi
    fi
    if [ "$rollback_ok" = true ]; then
        if [ "$previous_enabled" = true ]; then
            gnome_extensions enable "$extension_id" >/dev/null 2>&1 || rollback_ok=false
        else
            gnome_extensions disable "$extension_id" >/dev/null 2>&1 || true
        fi
    fi
    if restored_enabled=$(gnome_extensions list --enabled 2>/dev/null); then
        if [ "$previous_enabled" = true ]; then
            printf '%s\n' "$restored_enabled" | list_has_extension || rollback_ok=false
        elif printf '%s\n' "$restored_enabled" | list_has_extension; then
            rollback_ok=false
        fi
    else
        rollback_ok=false
    fi
    if restored_active=$(gnome_extensions list --active 2>/dev/null); then
        if [ "$previous_active" = true ]; then
            printf '%s\n' "$restored_active" | list_has_extension || rollback_ok=false
        elif printf '%s\n' "$restored_active" | list_has_extension; then
            rollback_ok=false
        fi
    else
        rollback_ok=false
    fi
    if [ "$previous_active" = true ]; then
        if [ -n "$previous_panel_runtime" ] && panel_runtime_matches "$previous_panel_runtime"; then
            printf '%s\n' 'Previous panel runtime proven restored.' >&2
        else
            printf '%s\n' 'Previous panel runtime restoration is unproven; log out and in.' >&2
        fi
    fi
    printf '%s\n' "$problem" >&2
    if [ "$rollback_ok" = true ]; then
        printf '%s\n' "Previous extension state restored (files/configuration; enabled=$previous_enabled, active=$previous_active). Runtime proof is reported separately above." >&2
    else
        printf '%s\n' 'WARNING: extension rollback failed; inspect the rollback and failed-copy directories.' >&2
    fi
    if [ "$rollback_ok" = true ]; then archive_panel_record 'rolled-back'; fi
    printf '%s\n' 'The user service and CLI remain installed and usable.' >&2
    printf '%s\n' 'A logout and login may be needed for Shell extension discovery.' >&2
    exit 2
}

panel_runtime_matches() {
    expected=$1
    "$python_bin" "$base_dir/core_install.py" --python "$python_bin" --panel-runtime "$expected"
}

# All recovery functions exist before the first switch so signal traps can use them.
if ! "$python_bin" - "$panel_record" "$extension_target" "$backup" "$stage" "$previous_installed" "$previous_enabled" "$previous_active" <<'PYRECORD'
import json
import os
from pathlib import Path
import sys
record, target, backup, stage, installed, enabled, active = sys.argv[1:]
with open(record, 'x', encoding='utf-8') as stream:
    json.dump({'target': target, 'backup': backup, 'stage': stage,
               'previousInstalled': installed == 'true', 'previousEnabled': enabled == 'true',
               'previousActive': active == 'true'}, stream, indent=2)
    stream.flush()
    os.fsync(stream.fileno())
directory = os.open(Path(record).parent, os.O_RDONLY | os.O_DIRECTORY)
try:
    os.fsync(directory)
finally:
    os.close(directory)
PYRECORD
then
    panel_fail_before_switch 'Top-bar control pending: could not persist transaction intent; no panel files switched.'
fi
panel_switch_started=true
if [ "$previous_installed" = true ] && ! mv "$extension_target" "$backup"; then
    archive_panel_record 'unchanged'
    panel_fail_before_switch 'Top-bar control pending: could not preserve the installed extension.'
fi
if ! mv "$stage" "$extension_target"; then
    printf '%s\n' 'Top-bar control pending: could not activate the staged extension directory.' >&2
    if [ "$previous_installed" = true ]; then
        if [ ! -e "$extension_target" ] && mv "$backup" "$extension_target" 2>/dev/null; then
            archive_panel_record 'rolled-back'
            printf '%s\n' "Previous extension directory and state remain in place (enabled=$previous_enabled, active=$previous_active)." >&2
        else
            printf '%s\n' 'WARNING: extension rollback failed after the staged switch failed; inspect the rollback and staging directories.' >&2
        fi
    else
        archive_panel_record 'unchanged'
        printf '%s\n' 'No previous extension existed; inspect the retained staging directory.' >&2
    fi
    printf '%s\n' 'The user service and CLI remain installed and usable.' >&2
    exit 2
fi

if ! gnome_extensions info "$extension_id" >/dev/null 2>&1; then
    rollback_panel 'Top-bar control pending: Shell did not discover the staged extension.'
fi
if [ "$previous_enabled" = true ]; then
    if ! gnome_extensions disable "$extension_id" >/dev/null 2>&1; then
        rollback_panel 'Top-bar control pending: Shell could not unload the previous extension before activating the staged copy.'
    fi
fi
if ! gnome_extensions enable "$extension_id" >/dev/null 2>&1; then
    rollback_panel 'Top-bar control pending: Shell did not enable the staged extension.'
fi
if ! enabled_list=$(gnome_extensions list --enabled) ||
        ! printf '%s\n' "$enabled_list" | list_has_extension; then
    rollback_panel 'Top-bar control pending: GNOME did not report the staged extension enabled.'
fi
if ! active_list=$(gnome_extensions list --active) ||
        ! printf '%s\n' "$active_list" | list_has_extension; then
    rollback_panel 'Top-bar control pending: extension is enabled but not active in Shell.'
fi
if ! panel_runtime_matches "$expected_panel_runtime"; then
    rollback_panel 'Top-bar control pending: exact staged JavaScript runtime was not proven loaded.'
fi
trap '' INT TERM
archive_panel_record 'committed'
if [ "$previous_installed" = true ]; then
    retained="$extension_parent/.sleep-disabler-previous.$(date +%s).$$"
    if ! mv "$backup" "$retained"; then
        printf '%s\n' "Deployment healthy; previous extension backup remains at $backup." >&2
    fi
fi
printf '%s\n' 'Exact staged top-bar runtime active in Shell. Confirm that its panel item is visible.'
