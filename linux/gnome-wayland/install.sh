#!/bin/sh
set -eu

base_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
extension_id='sleep-disabler@local'
extension_target="${XDG_DATA_HOME:-$HOME/.local/share}/gnome-shell/extensions/$extension_id"
program_target="$HOME/.local/lib/sleep-disabler-gnome"
unit_target="$HOME/.config/systemd/user"
bin_target="$HOME/.local/bin"

case ":${XDG_CURRENT_DESKTOP:-}:" in
    *GNOME*|*gnome*) ;;
    *) printf '%s\n' 'Run this installer from a GNOME desktop session.' >&2; exit 1 ;;
esac
if [ "${XDG_SESSION_TYPE:-}" != wayland ]; then
    printf '%s\n' 'Run this installer from your GNOME Wayland desktop session.' >&2
    exit 1
fi
if ! /usr/bin/python3 -c 'import dbus, gi' >/dev/null 2>&1; then
    printf '%s\n' 'Install dependencies first: sudo apt install python3-dbus python3-gi' >&2
    exit 1
fi
install -d "$program_target" "$extension_target" "$unit_target" "$bin_target"
install -m 644 "$base_dir/agent.py" "$program_target/agent.py"
install -m 644 "$base_dir/ctl.py" "$program_target/ctl.py"
install -m 644 "$base_dir/extension/$extension_id/metadata.json" "$extension_target/metadata.json"
install -m 644 "$base_dir/extension/$extension_id/extension.js" "$extension_target/extension.js"
install -m 644 "$base_dir/sleep-disabler-gnome.service" "$unit_target/sleep-disabler-gnome.service"

cat > "$bin_target/sleep-disablerctl" <<'EOF'
#!/bin/sh
exec /usr/bin/python3 "$HOME/.local/lib/sleep-disabler-gnome/ctl.py" "$@"
EOF
chmod 755 "$bin_target/sleep-disablerctl"

systemctl --user daemon-reload
systemctl --user enable sleep-disabler-gnome.service
systemctl --user restart sleep-disabler-gnome.service
if ! systemctl --user is-active --quiet sleep-disabler-gnome.service; then
    printf '%s\n' 'Sleep Disabler agent did not start; inspect systemctl --user status sleep-disabler-gnome.service.' >&2
    exit 1
fi
if ! /usr/bin/python3 "$program_target/ctl.py" status >/dev/null; then
    printf '%s\n' 'Sleep Disabler agent is active but its D-Bus API is unavailable.' >&2
    exit 1
fi
printf '%s\n' 'Agent usable: service active and session D-Bus API responded. Prevention starts OFF.'
printf '%s\n' "CLI: $bin_target/sleep-disablerctl status"
if ! command -v gnome-extensions >/dev/null 2>&1; then
    printf '%s\n' 'Top-bar control pending: gnome-extensions tool is unavailable.' >&2
    exit 2
fi
if ! gnome-extensions info "$extension_id" >/dev/null 2>&1; then
    printf '%s\n' 'Top-bar control pending: Shell has not discovered the extension.' >&2
    printf '%s\n' 'Log out and back in, then run: gnome-extensions enable sleep-disabler@local' >&2
    exit 2
fi
# Reload an already enabled extension after copying a new version.
gnome-extensions disable "$extension_id" >/dev/null 2>&1 || true
if ! gnome-extensions enable "$extension_id"; then
    printf '%s\n' 'Top-bar control pending: Shell has not discovered or enabled the extension.' >&2
    printf '%s\n' 'Log out and back in, then run: gnome-extensions enable sleep-disabler@local' >&2
    exit 2
fi
enabled=false
if ! enabled_list=$(gnome-extensions list --enabled); then
    printf '%s\n' 'Top-bar control pending: GNOME could not report enabled extensions.' >&2
    exit 2
fi
while IFS= read -r item; do
    if [ "$item" = "$extension_id" ]; then enabled=true; break; fi
done <<EOF
$enabled_list
EOF
if [ "$enabled" != true ]; then
    printf '%s\n' 'Top-bar control pending: GNOME did not report the extension enabled.' >&2
    exit 2
fi
if ! active_list=$(gnome-extensions list --active); then
    printf '%s\n' 'Top-bar control pending: GNOME could not report active extensions.' >&2
    exit 2
fi
active=false
while IFS= read -r item; do
    if [ "$item" = "$extension_id" ]; then active=true; break; fi
done <<EOF
$active_list
EOF
if [ "$active" != true ]; then
    printf '%s\n' 'Top-bar control pending: extension enabled but not active in Shell.' >&2
    exit 2
fi
printf '%s\n' 'Top-bar extension active in Shell. Confirm that its panel item is visible.'
