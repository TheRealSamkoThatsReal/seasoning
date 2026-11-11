"""Hyprland platform: a GTK4 layer-shell overlay fed by Hyprland's IPC socket."""
import json
import math
import os
import subprocess
import sys

from . import core
from .core import (LEDGE_F, MAXH, PHYS_HZ, POLL_MS, SEASONS, APP, Renderer, Sim,
                   bezier_ease, daemon_alive, load_cover, load_state, send, serve)

np = core.np
LAYER_LIBS = ("/usr/lib/libgtk4-layer-shell.so", "/usr/lib64/libgtk4-layer-shell.so",
              "/usr/lib/x86_64-linux-gnu/libgtk4-layer-shell.so",
              "/usr/lib/aarch64-linux-gnu/libgtk4-layer-shell.so",
              "/usr/local/lib/libgtk4-layer-shell.so")


# ── Hyprland IPC ─────────────────────────────────────────────────────────
def hypr_sock():
    run = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    sig = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE")
    if not sig:
        d = os.path.join(run, "hypr")
        sigs = sorted(os.listdir(d), key=lambda n: os.path.getmtime(os.path.join(d, n)))
        sig = sigs[-1]
    return os.path.join(run, "hypr", sig, ".socket.sock")


def hypr(req, path):
    import socket
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(0.5)
    s.connect(path)
    s.sendall(req.encode())
    out = b""
    while True:
        b = s.recv(262144)
        if not b:
            break
        out += b
    s.close()
    return json.loads(out)


def current_wallpaper():
    """Wallpaper path from awww / swww / hyprpaper, whichever is running."""
    for cmd, key in ((["awww", "query"], "image:"), (["swww", "query"], "image:")):
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=2).stdout
        except (OSError, subprocess.TimeoutExpired):
            continue
        for line in out.splitlines():
            if key in line:
                return line.split(key, 1)[1].strip()
    try:   # hyprpaper: "MONITOR = /path"
        out = subprocess.run(["hyprctl", "hyprpaper", "listactive"], capture_output=True,
                             text=True, timeout=2).stdout
        for line in out.splitlines():
            if "=" in line:
                p = line.split("=", 1)[1].strip()
                if os.path.isfile(p):
                    return p
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


class HyprPlatform:
    wallpaper_settle_ms = 3500        # awww/swww transitions take a moment

    def __init__(self, win, GLib):
        self.win = win
        self.GLib = GLib
        self.sock = hypr_sock()
        self.app = None

    def snapshot(self):
        try:
            mons = hypr("j/monitors", self.sock)
            clients = hypr("j/clients", self.sock)
        except (OSError, ValueError):
            return None
        if not mons:
            return None
        mon = mons[0]                                 # single overlay on the first monitor
        mx, my = mon["x"], mon["y"]
        if self.win is not None and self.win.get_width():
            W, H = self.win.get_width(), self.win.get_height()
        else:
            W, H = int(mon["width"] / mon.get("scale", 1)), int(mon["height"] / mon.get("scale", 1))
        ws = mon["activeWorkspace"]["id"]
        sp = mon.get("specialWorkspace", {}).get("id", 0)
        ids = {ws} | ({sp} if sp else set())
        vis = [c for c in clients
               if c.get("mapped", True) and not c.get("hidden")
               and c["workspace"]["id"] in ids and c.get("monitor") == mon["id"]]

        def rank(c):
            return ((2 if c["workspace"]["id"] < 0 else 0) + (1 if c.get("floating") or c.get("pinned") else 0),
                    -c.get("focusHistoryID", 0))
        vis.sort(key=rank, reverse=True)              # topmost first
        b = getattr(self, "border", 2)
        wins = [{"addr": c["address"], "x": c["at"][0] - mx - b, "y": c["at"][1] - my - b,
                 "w": c["size"][0] + 2 * b, "h": c["size"][1] + 2 * b, "title": c.get("title", "")}
                for c in vis]
        return {"origin": (mx, my), "size": (W, H), "refresh": float(mon.get("refreshRate", 60.0)),
                "ws": ws, "special": sp, "windows": wins,
                "alive": {c["address"] for c in clients},
                # hide for real fullscreen (games); maximise (mode 1) keeps the snow
                "hide": any(int(c.get("fullscreen", 0) or 0) == 2 for c in vis)}

    def decoration(self):
        try:
            self.border = int(hypr("j/getoption general:border_size", self.sock).get("int", 2))
            rd = float(hypr("j/getoption decoration:rounding", self.sock).get("int", 0))
            pw = float(hypr("j/getoption decoration:rounding_power", self.sock).get("float", 2.0))
            return (rd + self.border if rd > 0 else 0.0, max(pw, 1.0))
        except (OSError, ValueError, AttributeError):
            return None

    def ws_anim(self):
        try:
            anims, curves = hypr("j/animations", self.sock)[:2]
            by = {a["name"]: a for a in anims}
            a = next((by[n] for n in ("workspacesOut", "workspaces", "global")
                      if n in by and by[n].get("overridden")), by.get("workspaces"))
            if a and a.get("enabled", True):
                bez = {c["name"]: c for c in curves}.get(a.get("bezier") or "default")
                ease = bezier_ease((bez["X0"], bez["Y0"]), (bez["X1"], bez["Y1"])) if bez else (lambda t: t)
                # Hyprland: speed is in units of 100 ms
                return ((a.get("style") or "slide").split()[0], max(float(a.get("speed") or 7.0), 0.5) / 10.0, ease)
            return ("none", 0.0, lambda t: 1.0)
        except (OSError, ValueError, KeyError, TypeError, IndexError):
            return None

    @staticmethod
    def slide_direction(old, new):
        return -1 if new > old else 1                 # higher id: old workspace slides left

    def active_ws(self):
        try:
            return hypr("j/activeworkspace", self.sock).get("id")
        except (OSError, ValueError, AttributeError):
            return None

    def cursor(self):
        try:
            c = hypr("j/cursorpos", self.sock)
            return (c["x"], c["y"])
        except (OSError, ValueError, KeyError):
            return None

    def wallpaper(self):
        return current_wallpaper()

    def bar_mask(self, path, sim):
        """Low-res mask of wallpaper hidden under bars/docks (top+overlay layers).

        Layer rects are much bigger than what bars draw, so compare a screenshot
        with the wallpaper inside those rects: only where they differ is
        something actually covering it.
        """
        F = LEDGE_F
        w, h = sim.W // F, sim.H // F
        mx, my = sim.mon
        try:
            layers = hypr("j/layers", self.sock)
        except (OSError, ValueError):
            return None
        region = np.zeros((h, w), bool)
        for mon in layers.values():
            for lvl, arr in mon.get("levels", {}).items():
                if int(lvl) < 2:
                    continue
                for l in arr:
                    if l.get("namespace") == APP:
                        continue
                    x, y = (l["x"] - mx) // F, (l["y"] - my) // F
                    region[max(y, 0):(l["y"] - my + l["h"]) // F + 1,
                           max(x, 0):(l["x"] - mx + l["w"]) // F + 1] = True
        for ax, ay, aw, ah in sim.rects:              # windows aren't bars
            region[max(int(ay - MAXH - 4) // F, 0):int(ay + ah) // F + 1,
                   max(int(ax) // F, 0):int(ax + aw) // F + 1] = False
        if not region.any():
            return None
        try:
            import io
            from PIL import Image
            ppm = subprocess.run(["grim", "-s", str(1 / F), "-t", "ppm", "-"],
                                 capture_output=True, timeout=3).stdout
            shot = np.asarray(Image.open(io.BytesIO(ppm)).convert("RGB").resize((w, h)), np.float32)
            wall = np.asarray(load_cover(path, w, h), np.float32)
        except Exception as e:
            print(f"seasoning: bar mask skipped ({e}); is grim installed?", file=sys.stderr)
            return None
        m = (np.abs(shot - wall).max(-1) > 40) & region
        # erode once (drops flakes caught in the shot), then grow a margin
        pm = np.pad(m, 1)
        m = pm[1:-1, 1:-1] & pm[:-2, 1:-1] & pm[2:, 1:-1] & pm[1:-1, :-2] & pm[1:-1, 2:]
        for _ in range(3):
            pm = np.pad(m, 1)
            m = pm[1:-1, 1:-1] | pm[:-2, 1:-1] | pm[2:, 1:-1] | pm[1:-1, :-2] | pm[1:-1, 2:]
        return m

    def set_visible(self, v):
        if self.win is not None:
            self.win.set_visible(v)

    def later(self, ms, fn):
        self.GLib.timeout_add(max(1, int(ms)), lambda: (fn(), False)[1])

    def idle(self, fn):
        self.GLib.idle_add(lambda: (fn(), False)[1])

    def quit(self):
        if self.app is not None:
            self.GLib.idle_add(self.app.quit)


# ── the overlay ──────────────────────────────────────────────────────────
def run_overlay():
    # GTK's default Vulkan compositor has to import the GLArea's GL frame every
    # frame and waits on a fence each time (the clock fell to ~20 Hz). With the
    # GL compositor the hand-off is free.
    os.environ.setdefault("GSK_RENDERER", "gl")
    if "gtk4-layer-shell" not in os.environ.get("LD_PRELOAD", ""):
        # gtk4-layer-shell must load before libwayland-client
        lib = next((p for p in LAYER_LIBS if os.path.exists(p)), None)
        if lib is None:
            sys.exit("seasoning: gtk4-layer-shell not found (install gtk4-layer-shell)")
        env = dict(os.environ)
        env["LD_PRELOAD"] = (lib + " " + env.get("LD_PRELOAD", "")).strip()
        os.execve(sys.executable, [sys.executable, "-m", "seasoning"] + sys.argv[1:], env)

    if daemon_alive():
        print("seasoning: already running", file=sys.stderr)
        sys.exit(0)

    import ctypes
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_version("Gdk", "4.0")
    gi.require_version("Gtk4LayerShell", "1.0")
    gi.require_foreign("cairo")
    import cairo
    from gi.repository import Gdk, GLib, Gtk
    from gi.repository import Gtk4LayerShell as LS

    app = Gtk.Application(application_id="seasoning.overlay")

    def activate(_app):
        win = Gtk.Window(application=app)
        win.set_decorated(False)
        LS.init_for_window(win)
        LS.set_namespace(win, APP)
        LS.set_layer(win, LS.Layer.OVERLAY)
        LS.set_keyboard_mode(win, LS.KeyboardMode.NONE)
        LS.set_exclusive_zone(win, -1)
        for e in (LS.Edge.TOP, LS.Edge.BOTTOM, LS.Edge.LEFT, LS.Edge.RIGHT):
            LS.set_anchor(win, e, True)
        css = Gtk.CssProvider()
        css.load_from_string("window { background: transparent; }")
        Gtk.StyleContext.add_provider_for_display(Gdk.Display.get_default(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        plat = HyprPlatform(win, GLib)
        plat.app = app
        sim = Sim(plat)
        area = Gtk.GLArea()
        area.set_has_depth_buffer(False)
        area.set_has_stencil_buffer(False)
        area.set_can_target(False)
        win.set_child(area)
        frame = {"now": 0.0, "step_at": 0.0, "step_dt": 1.0 / PHYS_HZ, "tick": 0, "last": None,
                 "renders": 0}
        sim.fclock = frame

        def on_gl_realize(a):
            a.make_current()
            if a.get_error() is not None:
                print(f"seasoning: GL context failed: {a.get_error()}", file=sys.stderr)
                return
            ep = ctypes.CDLL("libepoxy.so.0")
            core.init_gl(lambda n: ctypes.c_void_p.in_dll(ep, "epoxy_" + n).value)
            es = a.get_context().get_use_es()
            sim.gl = Renderer(es)
            print(f"seasoning: rendering on GPU ({'GLES' if es else 'GL core'})", file=sys.stderr)

        def on_gl_render(a, ctx):
            g = sim.gl
            if g is None:
                return True
            if sim.flake_arr is not None:
                g.upload_flakes(sim.flake_arr)
                sim.flake_arr = None
            pb = sim.pile_batch()
            if pb is not None:
                g.upload_piles(*pb)
            frame["renders"] += 1
            t = (frame["now"] - frame["step_at"]) / frame["step_dt"]
            g.draw(float(a.get_width()), float(a.get_height()), float(a.get_scale_factor()),
                   min(max(t, 0.0), 1.0), SEASONS[sim.season]["shape"], getattr(sim, "masks", ()))
            return True
        area.connect("realize", on_gl_realize)
        area.connect("render", on_gl_render)

        def on_realize(w):
            w.get_surface().set_input_region(cairo.Region())   # click-through
        win.connect("realize", on_realize)
        win.present()

        sim.poll()
        GLib.timeout_add(POLL_MS, sim.poll)

        def on_tick(widget, fc):
            # Draw on EVERY tick: on Wayland GTK's clock advances off the compositor's
            # frame-done callback, so skipping a draw stalls the clock. Physics runs on
            # elapsed time at PHYS_HZ; the shader interpolates between steps.
            now = fc.get_frame_time() / 1e6
            frame["tick"] += 1
            frame["now"] = now
            if frame["last"] is None:
                frame["last"] = frame["step_at"] = now
            elif now - frame["last"] >= 1.0 / PHYS_HZ - 0.002:
                dt = min(now - frame["last"], 0.1)
                sim.step(dt)
                frame["step_dt"] = max(dt, 1e-3)
                frame["step_at"] = frame["last"] = now
            if (sim.alive.any() or sim.flake_arr is not None or sim.gl is None
                    or sim.gl.flake_n or sim.pile_sig is None
                    or any(p.dirty for p in sim.surfaces())):
                area.queue_render()
            return True
        area.add_tick_callback(on_tick)

        def first_wallpaper_check():
            sim.watch_wallpaper()
            GLib.timeout_add(3000, sim.watch_wallpaper)   # notice wallpaper changes
            return False
        GLib.timeout_add(500, first_wallpaper_check)

        def register(srv, on_ready):
            GLib.io_add_watch(srv.fileno(), GLib.PRIORITY_DEFAULT, GLib.IO_IN,
                              lambda *_: (on_ready(), True)[1])
        app._srv = serve(sim, register)
        app._bridge = core.serve_bridge(sim)
        app.hold()

    app.connect("activate", activate)
    prof_secs = float(os.environ.get("SEASONING_PROFILE", "0") or 0)
    if prof_secs:
        import cProfile
        prof = cProfile.Profile()

        def dump():
            prof.disable()
            prof.dump_stats("/tmp/seasoning.prof")
            return False
        GLib.timeout_add(int(prof_secs * 1000), dump)
        prof.enable()
    try:
        app.run([])
    finally:
        try:
            os.unlink(core.ipc_address())
        except OSError:
            pass


def spawn_detached():
    log = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), f"{APP}.log")
    subprocess.Popen([sys.executable, "-m", "seasoning", "run"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=open(log, "a"), start_new_session=True,
                     env=dict(os.environ, PYTHONPATH=os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
                              + os.pathsep + os.environ.get("PYTHONPATH", "")))


# ── the control panel ─────────────────────────────────────────────────────────
def run_panel():
    import gi
    gi.require_version("Gtk", "4.0")
    gi.require_foreign("cairo")
    from gi.repository import GLib, Gtk

    if not daemon_alive():
        spawn_detached()
        for _ in range(40):
            if daemon_alive():
                break
            GLib.usleep(100_000)
    try:
        st = json.loads(send("status"))
    except (OSError, ValueError):
        st = load_state()

    app = Gtk.Application(application_id="seasoning.panel")
    COMPASS = ["E", "SE", "S", "SW", "W", "NW", "N", "NE"]

    def activate(_app):
        win = Gtk.ApplicationWindow(application=app, title="seasoning")
        win.set_default_size(280, 400)
        css = Gtk.CssProvider()
        css.load_from_string("""
            window { background: #0e1626; color: #cfe3ff; }
            .title { font-weight: 700; font-size: 15px; }
            .dim { color: #6f86a8; font-size: 11px; }
            .big { font-size: 13px; font-weight: 600; }
            button { background: #18243a; color: #cfe3ff; border-radius: 8px; border: 1px solid #27406a; }
        """)
        Gtk.StyleContext.add_provider_for_display(win.get_display(), css,
                                                  Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10,
                      margin_top=14, margin_bottom=14, margin_start=14, margin_end=14)
        win.set_child(box)
        t = Gtk.Label(label="❄  seasoning", xalign=0)
        t.add_css_class("title")
        box.append(t)

        vec = [math.cos(math.radians(st["wind_deg"])) * st["wind"],
               math.sin(math.radians(st["wind_deg"])) * st["wind"]]
        info = Gtk.Label()
        info.add_css_class("big")

        def update_label():
            s = min(1.0, math.hypot(*vec))
            ang = math.degrees(math.atan2(vec[1], vec[0])) % 360
            info.set_label(f"{round(s * 100)}%  ·  {COMPASS[int((ang + 22.5) // 45) % 8]}" if s > 0.02 else "calm")
            return ang, s

        area = Gtk.DrawingArea(content_width=240, content_height=240)

        def draw(_a, cr, w, h):
            cx, cy, R = w / 2, h / 2, min(w, h) / 2 - 8
            cr.set_source_rgb(0.07, 0.11, 0.19)
            cr.arc(cx, cy, R, 0, 2 * math.pi)
            cr.fill()
            cr.set_source_rgba(0.35, 0.55, 0.9, 0.35)
            cr.set_line_width(1)
            for f in (0.33, 0.66, 1.0):
                cr.arc(cx, cy, R * f, 0, 2 * math.pi)
                cr.stroke()
            ex, ey = cx + vec[0] * R, cy + vec[1] * R
            cr.set_source_rgb(0.33, 0.65, 1.0)
            cr.set_line_width(4)
            cr.set_line_cap(1)
            cr.move_to(cx, cy)
            cr.line_to(ex, ey)
            cr.stroke()
            if math.hypot(*vec) > 0.05:
                a = math.atan2(vec[1], vec[0])
                for s in (-1, 1):
                    cr.move_to(ex, ey)
                    cr.line_to(ex - 12 * math.cos(a + s * 0.5), ey - 12 * math.sin(a + s * 0.5))
                cr.stroke()
            cr.arc(cx, cy, 4, 0, 2 * math.pi)
            cr.fill()

        area.set_draw_func(draw)
        last = [0]

        def set_from(x, y):
            w, h = area.get_width(), area.get_height()
            R = min(w, h) / 2 - 8
            dx, dy = (x - w / 2) / R, (y - h / 2) / R
            m = math.hypot(dx, dy)
            if m > 1:
                dx, dy = dx / m, dy / m
            vec[0], vec[1] = dx, dy
            ang, s = update_label()
            area.queue_draw()
            now = GLib.get_monotonic_time()
            if now - last[0] > 40_000:
                last[0] = now
                push(ang, s)

        def push(ang, s):
            try:
                send(f"wind {ang:.1f} {s * 100:.1f}")
            except OSError:
                pass

        drag = Gtk.GestureDrag()
        start = [0, 0]

        def begin(g, x, y):
            start[:] = [x, y]
            set_from(x, y)

        def upd(g, ox, oy):
            set_from(start[0] + ox, start[1] + oy)

        def end(g, ox, oy):
            set_from(start[0] + ox, start[1] + oy)
            push(*update_label())
        drag.connect("drag-begin", begin)
        drag.connect("drag-update", upd)
        drag.connect("drag-end", end)
        area.add_controller(drag)
        box.append(area)
        box.append(info)
        hint = Gtk.Label(label="drag from the centre to set the wind")
        hint.add_css_class("dim")
        box.append(hint)
        update_label()

        il = Gtk.Label(label="Snowfall", xalign=0)
        il.add_css_class("big")
        box.append(il)
        sc = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 3, 0.05)
        sc.set_value(st.get("intensity", 1.0))
        sc.connect("value-changed", lambda s: send(f"intensity {s.get_value():.2f}"))
        box.append(sc)

        row = Gtk.Box(spacing=8, homogeneous=True)
        row2 = Gtk.Box(spacing=8, homogeneous=True)
        for i, (label, cmd) in enumerate((("Pause", "pause"), ("Clear", "clear"),
                                          ("Floor", "floor"), ("Ledges", "ledges"))):
            b = Gtk.Button(label=label)
            b.connect("clicked", lambda _b, c=cmd: send(c))
            (row if i < 2 else row2).append(b)
        box.append(row)
        box.append(row2)
        order = ["auto", "winter", "spring", "summer", "fall"]
        sb = Gtk.Button(label=f"Season: {st.get('season', 'auto')}")

        def cycle(_b):
            cur = sb.get_label().split(": ", 1)[1]
            nxt = order[(order.index(cur) + 1) % len(order)] if cur in order else "auto"
            try:
                send(f"season {nxt}")
            except OSError:
                pass
            sb.set_label(f"Season: {nxt}")
        sb.connect("clicked", cycle)
        box.append(sb)
        q = Gtk.Button(label="Stop")
        q.connect("clicked", lambda _b: (send("quit"), win.close()))
        box.append(q)
        win.present()

    app.connect("activate", activate)
    app.run([])



