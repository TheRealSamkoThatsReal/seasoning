"""Windows platform: a transparent, click-through, always-on-top GLFW window.

Window geometry comes from EnumWindows (which walks top-level windows in z-order,
topmost first) plus DWM for the visible frame bounds and the cloaked state of
windows on other virtual desktops. Control is a tray icon, a Tk panel and the
same text commands as on Linux, over a localhost socket.
"""
import ctypes
import os
import queue
import subprocess
import sys
import time
from ctypes import wintypes

from . import core
from .core import (LEDGE_F, PHYS_HZ, POLL_MS, SEASONS, APP, Renderer, Sim, daemon_alive,
                   load_state, send, serve, state)

np = core.np

user32 = ctypes.WinDLL("user32", use_last_error=True) if sys.platform == "win32" else None
dwmapi = ctypes.WinDLL("dwmapi") if sys.platform == "win32" else None

if user32 is not None:
    H_, I_, B_ = wintypes.HWND, ctypes.c_int, wintypes.BOOL
    for name, res, args in (
        ("GetForegroundWindow", H_, ()),
        ("FindWindowW", H_, (wintypes.LPCWSTR, wintypes.LPCWSTR)),
        ("IsWindowVisible", B_, (H_,)), ("IsIconic", B_, (H_,)), ("IsZoomed", B_, (H_,)),
        ("GetClassNameW", I_, (H_, wintypes.LPWSTR, I_)),
        ("GetWindowTextLengthW", I_, (H_,)), ("GetWindowTextW", I_, (H_, wintypes.LPWSTR, I_)),
        ("GetWindowRect", B_, (H_, ctypes.POINTER(wintypes.RECT))),
        ("GetCursorPos", B_, (ctypes.POINTER(wintypes.POINT),)),
        ("SetWindowPos", B_, (H_, H_, I_, I_, I_, I_, wintypes.UINT)),
        ("SystemParametersInfoW", B_, (wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT)),
    ):
        f = getattr(user32, name)
        f.restype, f.argtypes = res, list(args)
    _gwl = getattr(user32, "GetWindowLongPtrW", None) or user32.GetWindowLongW
    _gwl.restype, _gwl.argtypes = ctypes.c_ssize_t, [H_, I_]
    _swl = getattr(user32, "SetWindowLongPtrW", None) or user32.SetWindowLongW
    _swl.restype, _swl.argtypes = ctypes.c_ssize_t, [H_, I_, ctypes.c_ssize_t]
    dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
    dwmapi.DwmGetWindowAttribute.argtypes = [H_, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]

DWMWA_EXTENDED_FRAME_BOUNDS = 9
DWMWA_CLOAKED = 14
DWMWA_WINDOW_CORNER_PREFERENCE = 33
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
WS_EX_NOACTIVATE = 0x08000000
WS_EX_APPWINDOW = 0x00040000
WS_EX_TOPMOST = 0x00000008
SWP_NOMOVE, SWP_NOSIZE, SWP_NOACTIVATE, SWP_FRAMECHANGED = 0x2, 0x1, 0x10, 0x20
HWND_TOPMOST = -1
SPI_GETDESKWALLPAPER = 0x0073

# shell surfaces that are not "windows" snow should sit on
SKIP_CLASSES = {
    "Progman", "WorkerW", "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "NotifyIconOverflowWindow",
    "Windows.UI.Core.CoreWindow", "Xaml_WindowedPopupClass", "TopLevelWindowForOverflowXamlIsland",
    "XamlExplorerHostIslandWindow", "ForegroundStaging", "MultitaskingViewFrame",
    "TaskListThumbnailWnd", "tooltips_class32", "#32768", "GLFW30",
}


def _setup_dpi():
    try:   # per-monitor v2: every coordinate below is in real pixels
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)
        except (AttributeError, OSError):
            user32.SetProcessDPIAware()


def _class_name(hwnd):
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _title(hwnd):
    n = user32.GetWindowTextLengthW(hwnd)
    if not n:
        return ""
    buf = ctypes.create_unicode_buffer(n + 1)
    user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _dwm_int(hwnd, attr):
    v = ctypes.c_int(0)
    if dwmapi.DwmGetWindowAttribute(hwnd, attr, ctypes.byref(v), ctypes.sizeof(v)) != 0:
        return None
    return v.value


def _frame(hwnd):
    """Visible frame (no drop shadow) in screen pixels."""
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS,
                                    ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def _ex_style(hwnd):
    return _gwl(hwnd, GWL_EXSTYLE)


def enum_windows():
    """Top-level windows, topmost first."""
    out = []
    proto = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        out.append(hwnd)
        return True
    user32.EnumWindows(proto(cb), 0)
    return out


def wallpaper_path():
    buf = ctypes.create_unicode_buffer(520)
    user32.SystemParametersInfoW(SPI_GETDESKWALLPAPER, 520, buf, 0)
    p = buf.value
    if p and os.path.isfile(p):
        return p
    # slideshows / Spotlight / pictures set from apps live here as a cached copy
    t = os.path.join(os.environ.get("APPDATA", ""), "Microsoft", "Windows", "Themes", "TranscodedWallpaper")
    return t if os.path.isfile(t) else None


def wallpaper_fit():
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Control Panel\Desktop") as k:
            style = str(winreg.QueryValueEx(k, "WallpaperStyle")[0])
            tile = str(winreg.QueryValueEx(k, "TileWallpaper")[0])
    except OSError:
        return "cover"
    if tile == "1":
        return "stretch"            # tiled: edges are everywhere anyway; good enough
    return {"10": "cover", "6": "contain", "2": "stretch", "0": "center", "22": "cover"}.get(style, "cover")


current_wallpaper = wallpaper_path


class WinPlatform:
    wallpaper_settle_ms = 1500       # Windows cross-fades a new wallpaper in ~1 s

    def __init__(self, glfw, window):
        self.glfw = glfw
        self.window = window
        self.hwnd = glfw.get_win32_window(window)
        self.W, self.H = glfw.get_window_size(window)
        mode = glfw.get_video_mode(glfw.get_primary_monitor())
        self.mon_w, self.mon_h = mode.size.width, mode.size.height
        self.refresh = float(mode.refresh_rate or 60)
        self.win11 = sys.getwindowsversion().build >= 22000
        self.visible = True
        self.timers = []              # (due, fn)
        self.cmds = queue.Queue()     # commands from the tray thread
        self.quit_flag = False
        self.wallpaper_fit = wallpaper_fit()

    # ---- geometry ----------------------------------------------------------
    def snapshot(self):
        wins, alive = [], set()
        fg = user32.GetForegroundWindow()
        hide = False
        for hwnd in enum_windows():
            if not hwnd or hwnd == self.hwnd:
                continue
            alive.add(hwnd)
            if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
                continue
            if _dwm_int(hwnd, DWMWA_CLOAKED):          # other virtual desktop / suspended app
                continue
            cls = _class_name(hwnd)
            if cls in SKIP_CLASSES:
                continue
            ex = _ex_style(hwnd)
            if ex & WS_EX_TOOLWINDOW and not ex & WS_EX_APPWINDOW:
                continue                               # tooltips, tray flyouts, OSDs
            l, t, r, b = _frame(hwnd)
            w, h = r - l, b - t
            if w < 60 or h < 40:
                continue
            if r <= 0 or b <= 0 or l >= self.mon_w or t >= self.mon_h:
                continue                               # not on the primary monitor
            if hwnd == fg and l <= 0 and t <= 0 and r >= self.mon_w and b >= self.mon_h:
                hide = True                            # fullscreen game / video: get out of the way
            if not _title(hwnd) and not ex & WS_EX_APPWINDOW:
                continue
            zoomed = bool(user32.IsZoomed(hwnd))
            pref = _dwm_int(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE) if self.win11 else None
            radius = 0.0 if (not self.win11 or zoomed or pref == 1) else (4.0 if pref == 3 else 8.0)
            wins.append({"addr": int(hwnd), "x": l, "y": t, "w": w, "h": h, "radius": radius})
        return {"origin": (0, 0), "size": (self.W, self.H), "refresh": self.refresh,
                "ws": 0, "special": 0, "windows": wins, "alive": {int(a) for a in alive}, "hide": hide}

    def decoration(self):
        return (8.0 if self.win11 else 0.0, 2.0)

    def ws_anim(self):
        # Virtual-desktop switches show up as windows getting cloaked; their snow just falls.
        return ("none", 0.0, lambda t: 1.0)

    @staticmethod
    def slide_direction(old, new):
        return -1

    def active_ws(self):
        return None

    def cursor(self):
        pt = wintypes.POINT()
        if not user32.GetCursorPos(ctypes.byref(pt)):
            return None
        return (pt.x, pt.y)

    def wallpaper(self):
        self.wallpaper_fit = wallpaper_fit()
        return wallpaper_path()

    def bar_mask(self, path, sim):
        """Low-res mask of the taskbar(s): no ledges under them."""
        F = LEDGE_F
        w, h = sim.W // F, sim.H // F
        m = np.zeros((h, w), bool)
        for cls in ("Shell_TrayWnd", "Shell_SecondaryTrayWnd"):
            hwnd = user32.FindWindowW(cls, None)
            if not hwnd:
                continue
            r = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(r))
            m[max(r.top // F - 2, 0):r.bottom // F + 1, max(r.left // F, 0):r.right // F + 1] = True
        return m if m.any() else None

    # ---- window / loop plumbing -------------------------------------------
    def set_visible(self, v):
        if v == self.visible:
            return
        self.visible = v
        (self.glfw.show_window if v else self.glfw.hide_window)(self.window)
        if v:
            _style_overlay(self.hwnd)

    def later(self, ms, fn):
        self.timers.append((time.monotonic() + ms / 1000.0, fn))

    def idle(self, fn):
        self.timers.append((0.0, fn))

    def run_timers(self):
        now = time.monotonic()
        due = [t for t in self.timers if t[0] <= now]
        if due:
            self.timers = [t for t in self.timers if t[0] > now]
            for _, fn in due:
                fn()

    def quit(self):
        self.quit_flag = True


def _style_overlay(hwnd):
    """No taskbar button, no Alt-Tab entry, never takes focus, stays on top."""
    ex = _ex_style(hwnd)
    _swl(hwnd, GWL_EXSTYLE, ex | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE | WS_EX_TOPMOST)
    user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                        SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_FRAMECHANGED)


# ── tray ─────────────────────────────────────────────────────────────────
def _icon_image():
    import math
    from PIL import Image, ImageDraw
    im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    c = (32, 32)
    for k in range(6):
        a = k * math.pi / 3
        x, y = c[0] + 26 * math.cos(a), c[1] + 26 * math.sin(a)
        d.line([c, (x, y)], fill=(225, 240, 255, 255), width=5)
        for s in (-1, 1):
            bx, by = c[0] + 16 * math.cos(a), c[1] + 16 * math.sin(a)
            d.line([(bx, by), (bx + 9 * math.cos(a + s * 0.9), by + 9 * math.sin(a + s * 0.9))],
                   fill=(225, 240, 255, 255), width=4)
    d.ellipse([26, 26, 38, 38], fill=(255, 255, 255, 255))
    return im


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def _self_cmd(*args):
    if getattr(sys, "frozen", False):
        return [sys.executable, *args]
    return [sys.executable, "-m", "seasoning", *args]


def _autostart(enable=None):
    import winreg
    cmd = subprocess.list2cmdline(_self_cmd("run"))
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ | winreg.KEY_WRITE) as k:
        if enable is None:
            try:
                winreg.QueryValueEx(k, APP)
                return True
            except OSError:
                return False
        if enable:
            winreg.SetValueEx(k, APP, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, APP)
            except OSError:
                pass
        return enable


def start_tray(plat, sim):
    try:
        import pystray
    except ImportError:
        print("seasoning: pystray not installed, no tray icon", file=sys.stderr)
        return None
    q = plat.cmds

    def cmd(c):
        return lambda icon, item: q.put(c)

    def season_item(name):
        return pystray.MenuItem(name.capitalize(), cmd(f"season {name}"), radio=True,
                                checked=lambda item: state.get("season", "auto") == name)

    def amount_item(label, v):
        return pystray.MenuItem(label, cmd(f"intensity {v}"), radio=True,
                                checked=lambda item: abs(state.get("intensity", 1.0) - v) < 0.01)

    def toggle_autostart(icon, item):
        _autostart(not _autostart())

    menu = pystray.Menu(
        pystray.MenuItem("Controls…", lambda icon, item: subprocess.Popen(
            _self_cmd("panel"), creationflags=0x08000000), default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Season", pystray.Menu(
            *[season_item(n) for n in ("auto", "winter", "spring", "summer", "fall")])),
        pystray.MenuItem("Amount", pystray.Menu(
            amount_item("Light", 0.4), amount_item("Normal", 1.0), amount_item("Heavy", 2.0),
            amount_item("Blizzard", 3.0))),
        pystray.MenuItem("Pause", cmd("pause"), checked=lambda item: sim.paused),
        pystray.MenuItem("Clear piles", cmd("clear")),
        pystray.MenuItem("Pile on wallpaper edges", cmd("ledges"),
                         checked=lambda item: state.get("ledges", True)),
        pystray.MenuItem("Pile along the bottom", cmd("floor"),
                         checked=lambda item: state.get("floor", True)),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Start with Windows", toggle_autostart, checked=lambda item: _autostart()),
        pystray.MenuItem("Quit", cmd("quit")),
    )
    icon = pystray.Icon(APP, _icon_image(), "seasoning", menu)
    icon.run_detached()
    return icon


# ── the overlay ──────────────────────────────────────────────────────────
def run_overlay(selftest=0.0):
    if daemon_alive():
        print("seasoning: already running", file=sys.stderr)
        return
    _setup_dpi()
    import glfw
    if not glfw.init():
        sys.exit("seasoning: GLFW failed to initialise")
    mon = glfw.get_primary_monitor()
    mode = glfw.get_video_mode(mon)
    W, H = mode.size.width, mode.size.height
    glfw.window_hint(glfw.CONTEXT_VERSION_MAJOR, 3)
    glfw.window_hint(glfw.CONTEXT_VERSION_MINOR, 3)
    glfw.window_hint(glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE)
    glfw.window_hint(glfw.OPENGL_FORWARD_COMPAT, True)
    glfw.window_hint(glfw.TRANSPARENT_FRAMEBUFFER, True)
    glfw.window_hint(glfw.DECORATED, False)
    glfw.window_hint(glfw.FLOATING, True)
    glfw.window_hint(glfw.RESIZABLE, False)
    glfw.window_hint(glfw.FOCUSED, False)
    glfw.window_hint(glfw.FOCUS_ON_SHOW, False)
    glfw.window_hint(glfw.VISIBLE, False)
    glfw.window_hint(glfw.MOUSE_PASSTHROUGH, True)
    # One pixel short of the monitor: a topmost window covering the whole screen is
    # treated by Windows as a fullscreen app (taskbar and notifications change behaviour).
    win = glfw.create_window(W, H - 1, APP, None, None)
    if not win:
        sys.exit("seasoning: could not create an OpenGL 3.3 window (update your graphics driver)")
    glfw.set_window_pos(win, 0, 0)
    glfw.make_context_current(win)
    glfw.swap_interval(1)
    if not glfw.get_window_attrib(win, glfw.TRANSPARENT_FRAMEBUFFER):
        print("seasoning: warning: no transparent framebuffer; the overlay may be opaque", file=sys.stderr)
    core.init_gl(lambda n: glfw.get_proc_address(n))
    renderer = Renderer(es=False)

    plat = WinPlatform(glfw, win)
    _style_overlay(plat.hwnd)
    glfw.show_window(win)
    _style_overlay(plat.hwnd)
    sim = Sim(plat)
    sim.gl = renderer

    ipc = []
    serve(sim, lambda srv, on_ready: ipc.append(on_ready))
    tray = start_tray(plat, sim)

    sim.poll()
    plat.later(500, sim.watch_wallpaper)
    t_poll = t_wall = time.monotonic()
    last_step = step_at = time.monotonic()
    step_dt = 1.0 / PHYS_HZ
    started = time.monotonic()
    frames = 0
    try:
        while not glfw.window_should_close(win) and not plat.quit_flag:
            glfw.poll_events()
            now = time.monotonic()
            if now - t_poll >= POLL_MS / 1000.0:
                t_poll = now
                sim.poll()
            if now - t_wall >= 3.0:
                t_wall = now
                sim.watch_wallpaper()
            plat.run_timers()
            for on_ready in ipc:
                for _ in range(4):                   # drain a few queued connections
                    on_ready()
            while not plat.cmds.empty():
                sim.command(plat.cmds.get_nowait())
            if selftest and now - started > selftest:
                break
            if sim.hidden:
                time.sleep(0.1)
                continue
            if now - last_step >= 1.0 / PHYS_HZ - 0.002:
                dt = min(now - last_step, 0.1)
                sim.step(dt)
                step_dt = max(dt, 1e-3)
                step_at = last_step = now
            if sim.flake_arr is not None:
                renderer.upload_flakes(sim.flake_arr)
                sim.flake_arr = None
            pb = sim.pile_batch()
            if pb is not None:
                renderer.upload_piles(*pb)
            fw, fh = glfw.get_framebuffer_size(win)
            core.GL.Viewport(0, 0, fw, fh)
            t = min(max((now - step_at) / step_dt, 0.0), 1.0)
            renderer.draw(float(plat.W), float(plat.H), fw / max(plat.W, 1), t, SEASONS[sim.season]["shape"])
            glfw.swap_buffers(win)
            frames += 1
            if not sim.alive.any() and not any(p.dirty for p in sim.surfaces()):
                time.sleep(0.05)                     # nothing moving: idle gently
        shot = os.environ.get("SEASONING_SELFTEST_SHOT")
        if selftest and shot:
            from PIL import ImageGrab
            ImageGrab.grab().save(shot)          # the desktop as composited, overlay included
    finally:
        if tray is not None:
            tray.stop()
        glfw.terminate()
    if selftest:
        mass = {str(k): round(float(p.h.sum())) for k, p in sim.piles.items()}
        print(f"seasoning selftest: {frames} frames in {selftest:.0f}s, {int(sim.alive.sum())} particles, "
              f"{len(sim.visible)} windows, pile mass {mass}, {len(sim.ledges)} ledges "
              f"(wallpaper {sim.wp_path}), season {sim.season}")


def spawn_detached():
    DETACHED, NO_WINDOW = 0x00000008, 0x08000000
    subprocess.Popen(_self_cmd("run"), creationflags=DETACHED | NO_WINDOW,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ── the control panel (Tk) ───────────────────────────────────────────────
def run_panel():
    import math
    import tkinter as tk

    if not daemon_alive():
        spawn_detached()
        for _ in range(40):
            if daemon_alive():
                break
            time.sleep(0.1)
    try:
        import json
        st = json.loads(send("status"))
    except (OSError, ValueError):
        st = load_state()

    BG, FG, DIM, ACC, BTN = "#0e1626", "#cfe3ff", "#6f86a8", "#55a6ff", "#18243a"
    root = tk.Tk(className="seasoning-panel")
    root.title("seasoning")
    root.configure(bg=BG)
    root.resizable(False, False)
    root.attributes("-topmost", True)
    COMPASS = ["E", "SE", "S", "SW", "W", "NW", "N", "NE"]

    def safe(c):
        try:
            send(c)
        except OSError:
            pass

    tk.Label(root, text="❄  seasoning", bg=BG, fg=FG, font=("Segoe UI", 12, "bold"),
             anchor="w").pack(fill="x", padx=14, pady=(12, 4))
    S = 220
    cv = tk.Canvas(root, width=S, height=S, bg=BG, highlightthickness=0)
    cv.pack(padx=14)
    vec = [math.cos(math.radians(st.get("wind_deg", 90))) * st.get("wind", 0),
           math.sin(math.radians(st.get("wind_deg", 90))) * st.get("wind", 0)]
    info = tk.Label(root, bg=BG, fg=FG, font=("Segoe UI", 10, "bold"))
    info.pack()
    tk.Label(root, text="drag from the centre to set the wind", bg=BG, fg=DIM,
             font=("Segoe UI", 8)).pack()

    def redraw():
        cv.delete("all")
        c, R = S / 2, S / 2 - 8
        for f in (1.0, 0.66, 0.33):
            cv.create_oval(c - R * f, c - R * f, c + R * f, c + R * f, outline="#2a4470",
                           fill="#121c30" if f == 1.0 else "")
        ex, ey = c + vec[0] * R, c + vec[1] * R
        cv.create_line(c, c, ex, ey, fill=ACC, width=4, capstyle="round",
                       arrow="last" if math.hypot(*vec) > 0.05 else "none", arrowshape=(12, 14, 5))
        cv.create_oval(c - 4, c - 4, c + 4, c + 4, fill=ACC, outline="")
        s = min(1.0, math.hypot(*vec))
        ang = math.degrees(math.atan2(vec[1], vec[0])) % 360
        info.config(text=f"{round(s * 100)}%  ·  {COMPASS[int((ang + 22.5) // 45) % 8]}" if s > 0.02 else "calm")
        return ang, s

    last = [0.0]

    def drag(e, final=False):
        c, R = S / 2, S / 2 - 8
        dx, dy = (e.x - c) / R, (e.y - c) / R
        m = math.hypot(dx, dy)
        if m > 1:
            dx, dy = dx / m, dy / m
        vec[0], vec[1] = dx, dy
        ang, s = redraw()
        if final or time.monotonic() - last[0] > 0.05:
            last[0] = time.monotonic()
            safe(f"wind {ang:.1f} {s * 100:.1f}")
    cv.bind("<B1-Motion>", drag)
    cv.bind("<Button-1>", drag)
    cv.bind("<ButtonRelease-1>", lambda e: drag(e, True))
    redraw()

    tk.Label(root, text="Amount", bg=BG, fg=FG, font=("Segoe UI", 10, "bold"),
             anchor="w").pack(fill="x", padx=14, pady=(10, 0))
    sc = tk.Scale(root, from_=0, to=3, resolution=0.05, orient="horizontal", bg=BG, fg=FG,
                  troughcolor=BTN, highlightthickness=0, showvalue=False, length=S,
                  command=lambda v: safe(f"intensity {float(v):.2f}"))
    sc.set(st.get("intensity", 1.0))
    sc.pack(padx=14)

    def button(parent, text, fn):
        return tk.Button(parent, text=text, command=fn, bg=BTN, fg=FG, activebackground="#22355a",
                         activeforeground=FG, relief="flat", font=("Segoe UI", 9), padx=6, pady=4)
    row = tk.Frame(root, bg=BG)
    row.pack(fill="x", padx=14, pady=(8, 0))
    for i, (label, c) in enumerate((("Pause", "pause"), ("Clear", "clear"),
                                    ("Floor", "floor"), ("Ledges", "ledges"))):
        button(row, label, lambda c=c: safe(c)).grid(row=i // 2, column=i % 2, sticky="ew", padx=2, pady=2)
    row.columnconfigure(0, weight=1)
    row.columnconfigure(1, weight=1)
    order = ["auto", "winter", "spring", "summer", "fall"]
    sv = tk.StringVar(value=f"Season: {st.get('season', 'auto')}")

    def cycle():
        cur = sv.get().split(": ", 1)[1]
        nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
        safe(f"season {nxt}")
        sv.set(f"Season: {nxt}")
    tk.Button(root, textvariable=sv, command=cycle, bg=BTN, fg=FG, relief="flat",
              activebackground="#22355a", activeforeground=FG,
              font=("Segoe UI", 9)).pack(fill="x", padx=16, pady=(6, 0))
    button(root, "Stop", lambda: (safe("quit"), root.destroy())).pack(fill="x", padx=16, pady=(6, 14))
    auto = float(os.environ.get("SEASONING_PANEL_SELFTEST", "0") or 0)
    if auto:                                         # CI: show the panel, grab the screen, close
        def finish():
            shot = os.environ.get("SEASONING_PANEL_SHOT")
            if shot:
                from PIL import ImageGrab
                ImageGrab.grab().save(shot)
            print("seasoning panel selftest: ok")
            root.destroy()
        root.after(int(auto * 1000), finish)
    root.mainloop()
