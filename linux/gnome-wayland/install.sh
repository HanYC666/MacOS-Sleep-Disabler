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
if ! command -v gnome-extensions >/dev/null 2>&1; then
    printf '%s\n' 'GNOME extension tools are missing (install gnome-shell-extension-prefs or your distro equivalent).' >&2
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
systemctl --user enable --now sleep-disabler-gnome.service
systemctl --user restart sleep-disabler-gnome.service
if ! gnome-extensions enable "$extension_id"; then
    printf '%s\n' 'Extension copied, but GNOME has not discovered it yet. Log out and back in, then run:' >&2
    printf 'gnome-extensions enable %s\n' "$extension_id" >&2
fi

printf '%s\n' 'Installed. Prevention starts OFF. Open the top-bar menu or run:'
printf '%s\n' "  $bin_target/sleep-disablerctl status"
printf '%s\n' 'If the top-bar item is absent, log out and back in once.'
