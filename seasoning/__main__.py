"""seasoning — the season falls on your desktop and piles up on your windows.

    seasoning                 run the overlay (foreground)
    seasoning start           start it in the background (no-op if running)
    seasoning toggle          start it, or stop it if it's running
    seasoning panel           wind pad, snowfall, season and pile controls
    seasoning season S        winter | spring | summer | fall | auto (follow the calendar)
    seasoning wind DEG PCT    wind toward DEG (0=E, 90=down, 180=W), PCT 0-100
    seasoning intensity X     how much falls, 0.0-3.0 (1 = default)
    seasoning stick F         share of near flakes that land on windows, 0-1 (default 0.35)
    seasoning ledges          toggle piling on edges detected in the wallpaper
    seasoning ledges IMG|show [OUT.png]   preview the edges found in an image
    seasoning rescan          re-detect wallpaper edges
    seasoning pause|clear|floor|quit|status|debug
"""
import sys

from . import core


def platform():
    if sys.platform == "win32":
        from . import windows
        return windows
    from . import hyprland
    return hyprland


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    plat = platform()
    if not a or a[0] == "run":
        plat.run_overlay()
    elif a[0] == "start":
        if not core.daemon_alive():
            plat.spawn_detached()
    elif a[0] == "toggle":
        if core.daemon_alive():
            core.send("quit")
        else:
            plat.spawn_detached()
    elif a[0] == "selftest":
        # run the real overlay for N seconds, then report (used by CI on Windows)
        if sys.platform != "win32":
            print("selftest is Windows-only; on Linux run `python -m pytest tests`", file=sys.stderr)
            return 2
        plat.run_overlay(selftest=float(a[1]) if len(a) > 1 else 10.0)
    elif a[0] == "panel":
        plat.run_panel()
    elif a[0] == "ledges" and len(a) > 1:
        img = plat.current_wallpaper() if a[1] == "show" else a[1]
        out = a[2] if len(a) > 2 else "seasoning-ledges.png"
        print(f"{core.ledges_preview(img, out)} ledges -> {out}")
    elif a[0] in ("-h", "--help", "help"):
        print(__doc__)
    else:
        try:
            print(core.send(" ".join(a)))
        except OSError:
            print("seasoning: not running (start it with `seasoning start`)", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
