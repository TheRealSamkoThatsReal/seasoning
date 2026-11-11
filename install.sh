#!/usr/bin/env bash
# Install seasoning for Hyprland: checks dependencies and puts a `seasoning`
# launcher in ~/.local/bin that runs this checkout.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
bin="${HOME}/.local/bin"

missing=()
python3 -c "import gi; gi.require_version('Gtk', '4.0'); gi.require_version('Gtk4LayerShell', '1.0')" 2>/dev/null \
    || missing+=("PyGObject + gtk4 + gtk4-layer-shell")
python3 -c "import numpy" 2>/dev/null || missing+=("numpy")
python3 -c "import PIL" 2>/dev/null || missing+=("pillow")
command -v hyprctl >/dev/null || missing+=("Hyprland")
if ((${#missing[@]})); then
    echo "Missing: ${missing[*]}"
    echo "Arch:          sudo pacman -S python-gobject gtk4 gtk4-layer-shell python-numpy python-pillow grim"
    echo "Fedora:        sudo dnf install python3-gobject gtk4 gtk4-layer-shell python3-numpy python3-pillow grim"
    echo "Debian/Ubuntu: sudo apt install python3-gi gir1.2-gtk-4.0 gir1.2-gtk4layershell-1.0 python3-numpy python3-pil grim"
    exit 1
fi
command -v grim >/dev/null || echo "note: install grim so wallpaper edges hidden under your bar/dock are skipped"

mkdir -p "$bin"
cat > "$bin/seasoning" <<LAUNCH
#!/bin/sh
PYTHONPATH="$here\${PYTHONPATH:+:\$PYTHONPATH}" exec python3 -m seasoning "\$@"
LAUNCH
chmod +x "$bin/seasoning"
echo "Installed $bin/seasoning"
echo
echo "Add to your Hyprland config, e.g. (hyprland.conf):"
echo "  exec-once = seasoning start"
echo "  bind = SUPER, N, exec, seasoning toggle"
echo "  bind = SUPER SHIFT, N, exec, seasoning panel"
echo "  windowrule = float, class:^(seasoning.panel)\$"
