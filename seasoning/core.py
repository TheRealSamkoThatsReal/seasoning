"""Shared core of seasoning: the particle/pile simulation, seasons, wallpaper-edge
detection and the GPU renderer. Platform layers (hyprland.py, windows.py) supply a
`Platform` object with the window list, cursor, wallpaper and timers.
"""
import json
import math
import os
import sys
import threading
import time

import numpy as np

APP = "seasoning"
rng = np.random.default_rng()

# ── tuning ───────────────────────────────────────────────────────────────
FPS = 60                   # legacy name: target render rate (snapped to refresh/k)
PHYS_HZ = 36               # physics steps; the shader interpolates between them
BASE_FLAKES = 380          # on screen at intensity 1
FALL = 70.0                # px/s for a mid-depth flake
MAX_WIND = 360.0           # px/s horizontal at 100%
COL = 2                    # px per heightmap column
MAXH = 22.0                # pile cap on windows
FLOOR_MAXH = 12.0          # pile cap along the bottom of the screen
LEDGE_MAXH = 9.0           # pile cap on wallpaper ledges
COHESION = 4.0             # settled snow holds walls this steep (carved grooves stay put)
LEDGE_REPOSE = 1.3         # ledges are rough: snow holds a steeper slope there
REPOSE = 0.7               # max height step between columns before it slumps
EDGE_REPOSE = 2.6          # snow clings a bit harder at window edges
SPILL_UNIT = 10.0         # pile volume that becomes one loose flake (≈ one landed flake)
# What falls in each season. fall/sway/wind scale the motion, size scales particle
# radius, count scales how many are on screen, mass scales how much one landing adds
# to a pile, maxh scales pile caps (0 = nothing piles up), shape picks the sprite.
SEASONS = {
    "winter": dict(fall=1.0, sway=1.0, wind=1.0, size=1.0, count=1.0, mass=1.0, maxh=1.0,
                   shape=0, spin=0.0),
    "fall":   dict(fall=0.55, sway=3.2, wind=1.5, size=3.0, count=0.5, mass=0.45, maxh=0.55,
                   shape=1, spin=2.6),
    "spring": dict(fall=0.42, sway=2.6, wind=1.3, size=1.5, count=0.7, mass=0.35, maxh=0.3,
                   shape=2, spin=2.0),
    "summer": dict(fall=0.16, sway=2.2, wind=1.2, size=2.4, count=0.3, mass=0.0, maxh=0.0,
                   shape=3, spin=0.6),
}
SEASON_COLORS = {          # particle palettes (rgb)
    "winter": [(1.0, 1.0, 1.0)],
    "fall":   [(0.86, 0.36, 0.09), (0.74, 0.16, 0.07), (0.92, 0.64, 0.14), (0.52, 0.27, 0.10),
               (0.80, 0.48, 0.12)],
    "spring": [(1.0, 0.78, 0.86), (0.98, 0.66, 0.78), (1.0, 0.90, 0.94)],
    "summer": [(1.0, 1.0, 0.96)],
}


def season_now(when=None):
    """Meteorological seasons, northern hemisphere (Dec-Feb winter, Mar-May spring, ...)."""
    m = (when or time.localtime()).tm_mon
    return {12: "winter", 1: "winter", 2: "winter", 3: "spring", 4: "spring", 5: "spring",
            6: "summer", 7: "summer", 8: "summer"}.get(m, "fall")


CURSOR_R = 70.0            # px: how far the cursor's wake reaches into falling snow
PLOW_R = 11.0              # px: half-width of the groove the cursor cuts through piles
CURSOR_BODY = (6.0, 16.0)  # arrow pointer: centre is ~6px right of the hotspot, body reaches ~16px below it
POLL_MS = 60               # window geometry poll



# ── state ────────────────────────────────────────────────────────────────
def config_dir():
    if sys.platform == "win32":
        return os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), APP)
    return os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), APP)


STATE = os.path.join(config_dir(), "state.json")
DEFAULTS = {"wind_deg": 90.0, "wind": 0.0, "intensity": 1.0, "floor": True, "ledges": True,
            "stick": 0.35, "season": "auto"}


def load_state():
    s = dict(DEFAULTS)
    try:
        with open(STATE) as f:
            s.update(json.load(f))
    except (OSError, ValueError):
        pass
    return s


def save_state(s):
    os.makedirs(os.path.dirname(STATE), exist_ok=True)
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(s, f)
    os.replace(tmp, STATE)


state = load_state()


# ── control channel (CLI / panel -> running overlay) ─────────────────────
def ipc_address():
    if sys.platform == "win32":
        return ("127.0.0.1", 47913)
    run = os.environ.get("XDG_RUNTIME_DIR", f"/tmp/{APP}-{os.getuid()}")
    return os.path.join(run, f"{APP}.sock")


def _connect(timeout):
    import socket
    addr = ipc_address()
    fam = socket.AF_INET if isinstance(addr, tuple) else socket.AF_UNIX
    c = socket.socket(fam, socket.SOCK_STREAM)
    c.settimeout(timeout)
    c.connect(addr)
    return c


def send(cmd, timeout=1.0):
    c = _connect(timeout)
    c.sendall(cmd.encode() + b"\n")
    out = b""
    while True:
        b = c.recv(65536)
        if not b:
            break
        out += b
    c.close()
    return out.decode()


def daemon_alive():
    try:
        send("ping", 0.5)
        return True
    except OSError:
        return False


BRIDGE_PORT = 47914


def serve_bridge(sim):
    """Local HTTP endpoint for the browser extension (127.0.0.1 only).

    GET  /state  -> the weather (season, wind, amount, ...) so pages match the desktop
    POST /page   -> {"titles": [...], "top": px} tabs the extension is drawing in; the
                    desktop stops drawing its own particles over those pages' area
    POST /command-> {"cmd": "season fall"} settings changed from the extension popup
    """
    import threading as _t
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    class H(BaseHTTPRequestHandler):
        def _send(self, code, body=b"{}"):
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "content-type")
            self.send_header("Access-Control-Allow-Private-Network", "true")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self):
            self._send(204, b"")

        def do_GET(self):
            if self.path.split("?")[0] != "/state":
                return self._send(404)
            body = {"app": APP, "season": sim.season, "intensity": state.get("intensity", 1.0),
                    "wind_deg": state.get("wind_deg", 90.0), "wind": state.get("wind", 0.0),
                    "stick": state.get("stick", 0.35), "paused": bool(sim.paused),
                    "hidden": bool(sim.hidden)}
            self._send(200, json.dumps(body).encode())

        def do_POST(self):
            if self.path == "/command":
                try:
                    n = min(int(self.headers.get("Content-Length", "0")), 4096)
                    cmd = str(json.loads(self.rfile.read(n) or b"{}").get("cmd", ""))
                except (ValueError, TypeError):
                    return self._send(400)
                # only settings the browser popup offers; run on the UI thread
                if cmd.split(" ")[0] not in ("season", "intensity", "wind", "pause", "clear", "stick"):
                    return self._send(403)
                sim.plat.idle(lambda: sim.command(cmd))
                return self._send(200)
            if self.path != "/page":
                return self._send(404)
            try:
                n = min(int(self.headers.get("Content-Length", "0")), 65536)
                data = json.loads(self.rfile.read(n) or b"{}")
                titles = [str(t)[:300] for t in data.get("titles", [])][:16]
                top = max(0.0, min(float(data.get("top", 0)), 400.0))
            except (ValueError, TypeError):
                return self._send(400)
            pages = getattr(sim, "pages", None) or {}
            exp = time.monotonic() + 6.0             # the extension re-announces every ~2s
            for t in titles:
                pages[t] = (exp, top)
            sim.pages = pages
            self._send(200)

        def log_message(self, *a):
            pass

    try:
        srv = ThreadingHTTPServer(("127.0.0.1", BRIDGE_PORT), H)
    except OSError as e:
        print(f"seasoning: browser bridge unavailable ({e})", file=sys.stderr)
        return None
    _t.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def serve(sim, register):
    """Open the control socket; `register(sock, on_ready)` hooks it into the UI loop."""
    import socket
    addr = ipc_address()
    if isinstance(addr, tuple):
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    else:
        try:
            os.unlink(addr)
        except FileNotFoundError:
            pass
        srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(addr)
    srv.listen(8)
    srv.setblocking(False)

    def on_ready():
        try:
            conn, _ = srv.accept()
        except BlockingIOError:
            return
        conn.settimeout(0.5)
        try:
            data = conn.recv(4096).decode().strip()
            conn.sendall(sim.command(data).encode())
        except OSError:
            pass
        finally:
            conn.close()
    register(srv, on_ready)
    return srv


# ── wallpaper ledges ─────────────────────────────────────────────────────
LEDGE_F = 4              # analysis runs at 1/4 resolution (480x270 for 1080p)
LEDGE_MIN_LEN = 15       # low-res columns (= 60px) for a ledge to count
LEDGE_MAX_SLOPE = 0.42   # ~23 deg; steeper edges can't hold snow
LEDGE_MAX = 45           # keep the strongest N


def load_cover(path, w, h, fit="cover"):
    """Wallpaper as it appears on a w x h screen. fit: cover (fill+crop, the default
    almost everywhere), stretch, contain (letterboxed), or center."""
    from PIL import Image
    im = Image.open(path)
    im.seek(0)
    im = im.convert("RGB")
    if fit == "stretch":
        return im.resize((w, h), Image.LANCZOS)
    if fit in ("contain", "center"):
        s = min(w / im.width, h / im.height) if fit == "contain" else min(1.0, w / im.width, h / im.height)
        nw, nh = max(1, round(im.width * s)), max(1, round(im.height * s))
        out = Image.new("RGB", (w, h))
        out.paste(im.resize((nw, nh), Image.LANCZOS), ((w - nw) // 2, (h - nh) // 2))
        return out
    s = max(w / im.width, h / im.height)
    nw, nh = max(w, round(im.width * s)), max(h, round(im.height * s))
    im = im.resize((nw, nh), Image.LANCZOS)
    l, t = (nw - w) // 2, (nh - h) // 2
    return im.crop((l, t, l + w, t + h))


def detect_ledges(path, W, H, occluded=None, fit="cover"):
    """Find near-horizontal edges in the wallpaper that snow could sit on.

    Works on any image: the edge threshold is relative (a percentile of this
    image's own gradient strength) with an absolute floor, so flat/minimal
    wallpapers yield few or no ledges and busy ones only their strongest
    lines. Returns [(x0, ys)] in screen px, ys sampled every COL px.
    `occluded` (low-res bool mask) marks pixels covered by bars/docks.
    """
    import numpy as np
    from PIL import ImageFilter
    F = LEDGE_F
    w, h = W // F, H // F
    im = load_cover(path, w, h, fit).filter(ImageFilter.GaussianBlur(0.8))
    a = np.asarray(im, np.float32)
    gy = a[2:, 1:-1] - a[:-2, 1:-1]
    gx = a[1:-1, 2:] - a[1:-1, :-2]
    my = np.sqrt((gy ** 2).sum(-1))
    mx = np.sqrt((gx ** 2).sum(-1))
    mag = np.hypot(my, mx)
    thr = max(float(np.percentile(mag, 93)), 30.0)
    cand = (my > 1.5 * mx) & (my > thr)
    # thin to a 1px line: local max of vertical gradient along y
    up = np.vstack([np.zeros((1, my.shape[1])), my[:-1]])
    dn = np.vstack([my[1:], np.zeros((1, my.shape[1]))])
    cand &= (my >= up) & (my > dn)
    if occluded is not None:
        cand &= ~occluded[1:-1, 1:-1]
    hh, ww = cand.shape

    # trace left→right, linking pixels whose y moves by at most 1 per column
    segs, active = [], []          # active: [xs, ys, strength, last_x]
    for x in range(ww):
        ys = np.nonzero(cand[:, x])[0]
        used = set()
        nxt = []
        for s in active:
            if x - s[3] > 2:       # gap too long: segment ends
                segs.append(s)
                continue
            ly = s[1][-1]
            best = None
            for y in ys:
                if y in used:
                    continue
                if abs(int(y) - ly) <= 1 and (best is None or abs(int(y) - ly) < abs(best - ly)):
                    best = int(y)
            if best is not None:
                used.add(best)
                s[0].append(x)
                s[1].append(best)
                s[2] += float(my[best, x])
                s[3] = x
            nxt.append(s)
        for y in ys:
            if int(y) not in used:
                nxt.append([[x], [int(y)], float(my[y, x]), x])
        active = nxt
    segs += active

    # smooth, then split wherever the edge gets too steep to hold snow
    pieces = []
    for xs, ys, strength, _ in segs:
        if len(xs) < LEDGE_MIN_LEN:
            continue
        xs = np.array(xs, float)
        ys = np.array(ys, float)
        full_x = np.arange(xs[0], xs[-1] + 1)
        fy = np.interp(full_x, xs, ys)
        if len(fy) >= 5:
            k = np.ones(5) / 5
            fy = np.convolve(np.pad(fy, 2, mode="edge"), k, "valid")
        slope = np.abs(np.gradient(fy))
        ok = slope <= LEDGE_MAX_SLOPE
        # real step, not a thin line: colour 3-5px above must differ from 3-5px below
        xi = np.clip(full_x.astype(int) + 1, 0, a.shape[1] - 1)
        yc = np.rint(fy).astype(int) + 1
        ab = np.mean([a[np.clip(yc - k, 0, a.shape[0] - 1), xi] for k in (3, 4, 5)], 0)
        be = np.mean([a[np.clip(yc + k, 0, a.shape[0] - 1), xi] for k in (3, 4, 5)], 0)
        step = np.sqrt(((ab - be) ** 2).sum(-1))
        step = np.convolve(step, np.ones(5) / 5, "same")
        ok &= step > max(0.5 * thr, 24.0)
        start = None
        for i in range(len(ok) + 1):
            if i < len(ok) and ok[i]:
                if start is None:
                    start = i
            elif start is not None:
                if i - start >= LEDGE_MIN_LEN:
                    pieces.append((full_x[start:i], fy[start:i], strength / len(xs)))
                start = None

    # strongest first; drop near-duplicates of an already accepted ledge
    pieces.sort(key=lambda p: -len(p[0]) * p[2])
    occ = np.zeros((hh, ww), bool)
    out = []
    for px, py, st in pieces:
        xi = px.astype(int)
        yi = np.clip(np.rint(py).astype(int), 0, hh - 1)
        if occ[yi, xi].mean() > 0.4:
            continue
        for d in range(-3, 4):
            occ[np.clip(yi + d, 0, hh - 1), xi] = True
        # back to screen px (+1: gradient arrays are offset by one pixel)
        sx0 = (px[0] + 1) * F
        sx1 = (px[-1] + 2) * F
        cols = np.arange(sx0, sx1, COL) + COL / 2
        sy = np.interp(cols, (px + 1.5) * F, (py + 1.5) * F)
        out.append((float(sx0), sy))
        if len(out) >= LEDGE_MAX:
            break
    return out


def ledges_preview(path, out_png, W=1920, H=1080):
    from PIL import ImageDraw
    ls = detect_ledges(path, W, H)
    im = load_cover(path, W, H).convert("RGB")
    d = ImageDraw.Draw(im)
    for x0, ys in ls:
        pts = [(x0 + (i + 0.5) * COL, y) for i, y in enumerate(ys)]
        d.line(pts, fill=(255, 40, 200), width=4)
    im.save(out_png)
    return len(ls)


# ── GPU renderer ─────────────────────────────────────────────────────────
# GL entry points are looked up through whatever loader the platform uses
# (libepoxy under GTK, GLFW's getProcAddress on Windows).
import ctypes

GL = None
_u, _i, _f, _p = ctypes.c_uint, ctypes.c_int, ctypes.c_float, ctypes.c_void_p


def init_gl(getproc):
    """Bind GL functions; `getproc(name) -> address`. Call with a current context."""
    global GL

    def glfn(name, res, *args):
        ptr = getproc(name)
        if not ptr:
            raise RuntimeError(f"GL function {name} unavailable")
        return ctypes.CFUNCTYPE(res, *args)(ptr)

    class GL_:
        CreateShader = glfn("glCreateShader", _u, _u)
        ShaderSource = glfn("glShaderSource", None, _u, _i, ctypes.POINTER(ctypes.c_char_p), _p)
        CompileShader = glfn("glCompileShader", None, _u)
        GetShaderiv = glfn("glGetShaderiv", None, _u, _u, ctypes.POINTER(_i))
        GetShaderInfoLog = glfn("glGetShaderInfoLog", None, _u, _i, _p, ctypes.c_char_p)
        CreateProgram = glfn("glCreateProgram", _u)
        AttachShader = glfn("glAttachShader", None, _u, _u)
        LinkProgram = glfn("glLinkProgram", None, _u)
        GetProgramiv = glfn("glGetProgramiv", None, _u, _u, ctypes.POINTER(_i))
        GetProgramInfoLog = glfn("glGetProgramInfoLog", None, _u, _i, _p, ctypes.c_char_p)
        UseProgram = glfn("glUseProgram", None, _u)
        GetUniformLocation = glfn("glGetUniformLocation", _i, _u, ctypes.c_char_p)
        Uniform1f = glfn("glUniform1f", None, _i, _f)
        Uniform2f = glfn("glUniform2f", None, _i, _f, _f)
        Uniform1i = glfn("glUniform1i", None, _i, _i)
        Uniform4fv = glfn("glUniform4fv", None, _i, _i, _p)
        GenVertexArrays = glfn("glGenVertexArrays", None, _i, ctypes.POINTER(_u))
        BindVertexArray = glfn("glBindVertexArray", None, _u)
        GenBuffers = glfn("glGenBuffers", None, _i, ctypes.POINTER(_u))
        BindBuffer = glfn("glBindBuffer", None, _u, _u)
        BufferData = glfn("glBufferData", None, _u, ctypes.c_ssize_t, _p, _u)
        VertexAttribPointer = glfn("glVertexAttribPointer", None, _u, _i, _u, ctypes.c_ubyte, _i, _p)
        EnableVertexAttribArray = glfn("glEnableVertexAttribArray", None, _u)
        DrawArrays = glfn("glDrawArrays", None, _u, _i, _i)
        Enable = glfn("glEnable", None, _u)
        BlendFunc = glfn("glBlendFunc", None, _u, _u)
        ClearColor = glfn("glClearColor", None, _f, _f, _f, _f)
        Clear = glfn("glClear", None, _u)
        Viewport = glfn("glViewport", None, _i, _i, _i, _i)
        ReadPixels = glfn("glReadPixels", None, _i, _i, _i, _i, _u, _u, _p)

    GL = GL_


FLAKE_VS = """
layout(location=0) in vec4 a_pos;      // prev.xy, cur.xy (logical px)
layout(location=1) in vec2 a_look;     // size px, alpha
layout(location=2) in vec3 a_col;
layout(location=3) in vec3 a_rot;      // shape, angle prev, angle cur
uniform vec2 u_screen; uniform float u_t; uniform float u_scale;
out float v_a; out vec3 v_col; flat out int v_shape; out float v_ang;
void main() {
    vec2 p = mix(a_pos.xy, a_pos.zw, u_t);
    gl_Position = vec4(p.x / u_screen.x * 2.0 - 1.0, 1.0 - p.y / u_screen.y * 2.0, 0.0, 1.0);
    gl_PointSize = a_look.x * u_scale;
    v_a = a_look.y; v_col = a_col; v_shape = int(a_rot.x + 0.5);
    v_ang = mix(a_rot.y, a_rot.z, u_t);
}"""
FLAKE_FS = """
in float v_a; in vec3 v_col; flat in int v_shape; in float v_ang; out vec4 o;
uniform vec2 u_screen; uniform float u_scale; uniform vec4 u_mask[8]; uniform int u_nmask;
void main() {
    vec2 fc = vec2(gl_FragCoord.x / u_scale, u_screen.y - gl_FragCoord.y / u_scale);
    for (int i = 0; i < 8; i++) {          // web pages where the browser extension draws instead
        if (i >= u_nmask) break;
        vec4 m = u_mask[i];
        if (fc.x >= m.x && fc.x < m.x + m.z && fc.y >= m.y && fc.y < m.y + m.w) discard;
    }
    vec2 q = gl_PointCoord - vec2(0.5);
    if (v_shape == 0) {                                   // snowflake: soft disc
        float r = length(q) * 2.0;
        float a = v_a * (1.0 - smoothstep(0.45, 1.0, r)) * mix(1.0, 0.92, smoothstep(0.0, 0.45, r));
        vec3 c = mix(vec3(1.0), vec3(0.92, 0.96, 1.0), smoothstep(0.3, 1.0, r));
        o = vec4(c * a, a);
        return;
    }
    // rotate, then squash x by the tumble so leaves/petals seem to flip in 3D
    float cs = cos(v_ang), sn = sin(v_ang);
    vec2 p = vec2(cs * q.x - sn * q.y, sn * q.x + cs * q.y);
    float flip = cos(v_ang * 1.7 + 0.6);
    p.x /= max(abs(flip), 0.32);
    float a; vec3 c = v_col;
    if (v_shape == 1) {                                   // leaf: vesica + midrib + stem
        float d = max(length(p - vec2(0.22, 0.0)), length(p + vec2(0.22, 0.0))) - 0.46;
        float body = 1.0 - smoothstep(-0.02, 0.02, d);
        float stem = (1.0 - smoothstep(0.012, 0.03, abs(p.x))) * step(0.36, p.y) * step(p.y, 0.49);
        a = max(body, stem * 0.9);
        float rib = (1.0 - smoothstep(0.0, 0.025, abs(p.x))) * step(abs(p.y), 0.38);
        c *= mix(1.0, 0.68, rib) * (0.82 + 0.36 * (p.x + 0.5)) * mix(0.75, 1.0, abs(flip));
    } else if (v_shape == 2) {                            // petal: ellipse with a notch
        float e = length(vec2(p.x / 0.27, p.y / 0.42));
        float notch = 1.0 - smoothstep(0.10, 0.13, length(p - vec2(0.0, -0.44)));
        a = (1.0 - smoothstep(0.9, 1.0, e)) * (1.0 - notch);
        c = mix(c, vec3(1.0), 0.45 * smoothstep(0.1, 0.45, p.y + 0.2));
    } else {                                              // dandelion fluff
        float r = length(q) * 2.0;
        float spokes = 0.5 + 0.5 * cos(atan(q.y, q.x) * 9.0 + v_ang);
        a = exp(-r * r * 22.0) + 0.75 * (1.0 - smoothstep(0.4, 1.0, r)) * spokes * smoothstep(0.08, 0.25, r);
        a = min(a, 1.0);
    }
    a *= v_a;
    o = vec4(c * a, a);
}"""
PILE_VS = """
layout(location=0) in vec4 a_v;        // x, y, depth below surface, snow height
uniform vec2 u_screen; uniform float u_off;
out float v_d; out float v_h;
void main() {
    vec2 p = a_v.xy + vec2(0.0, u_off);
    gl_Position = vec4(p.x / u_screen.x * 2.0 - 1.0, 1.0 - p.y / u_screen.y * 2.0, 0.0, 1.0);
    v_d = a_v.z; v_h = a_v.w;
}"""
PILE_FS = """
in float v_d; in float v_h; out vec4 o;
uniform vec2 u_screen; uniform float u_scale; uniform float u_shadow;
uniform vec4 u_occ[16]; uniform int u_nocc; uniform int u_season;
float hash(vec2 v) { return fract(sin(dot(v, vec2(127.1, 311.7))) * 43758.5453); }
void main() {
    vec2 fc = vec2(gl_FragCoord.x / u_scale, u_screen.y - gl_FragCoord.y / u_scale);
    for (int i = 0; i < 16; i++) {
        if (i >= u_nocc) break;
        vec4 r = u_occ[i];
        if (fc.x >= r.x && fc.x < r.x + r.z && fc.y >= r.y && fc.y < r.y + r.w) discard;
    }
    float a = clamp(v_d + 1.0, 0.0, 1.0) * clamp(v_h, 0.0, 1.0);
    if (u_shadow > 0.5) { a *= 0.30; o = vec4(vec3(0.05, 0.08, 0.15) * a, a); return; }
    float t = clamp(v_d / max(v_h, 1.0), 0.0, 1.0);
    vec3 c;
    if (u_season == 1 || u_season == 2) {
        // leaf litter / petals: a mosaic of overlapping pieces in the season's colours
        vec2 cell = floor(vec2(fc.x / 6.0 + floor(fc.y / 3.0) * 0.5, fc.y / 3.0));
        float h = hash(cell);
        if (u_season == 1) {
            c = h < 0.3 ? vec3(0.80, 0.33, 0.09) : h < 0.55 ? vec3(0.66, 0.15, 0.07)
              : h < 0.78 ? vec3(0.86, 0.58, 0.13) : vec3(0.45, 0.24, 0.10);
        } else {
            c = h < 0.4 ? vec3(1.0, 0.78, 0.86) : h < 0.75 ? vec3(0.97, 0.64, 0.76) : vec3(1.0, 0.92, 0.95);
        }
        c *= 0.80 + 0.30 * hash(cell + 7.0);
        c *= mix(1.0, 0.62, t);                                   // deeper = shadowed
    } else {
        c = mix(vec3(1.0), vec3(0.80, 0.86, 0.95), t * t);
        c = mix(c, vec3(1.0), 0.5 * (1.0 - smoothstep(0.0, 1.2, v_d)));   // bright crest
    }
    a *= mix(0.98, 0.95, t);
    o = vec4(c * a, a);
}"""

class Renderer:
    def __init__(self, es):
        head = "#version 300 es\nprecision highp float;\n" if es else "#version 330 core\n"
        self.flake = self.program(head + FLAKE_VS, head + FLAKE_FS)
        self.pile = self.program(head + PILE_VS, head + PILE_FS)
        self.es = es
        self.u = {}
        for prog, names in ((self.flake, ("u_screen", "u_t", "u_scale", "u_mask", "u_nmask")),
                            (self.pile, ("u_screen", "u_off", "u_scale", "u_shadow", "u_occ", "u_nocc", "u_season"))):
            for n in names:
                self.u[(prog, n)] = GL.GetUniformLocation(prog, n.encode())
        ids = (_u * 2)()
        GL.GenVertexArrays(2, ids)
        self.vao_f, self.vao_p = ids[0], ids[1]
        GL.GenBuffers(2, ids)
        self.vbo_f, self.vbo_p = ids[0], ids[1]
        GL.BindVertexArray(self.vao_f)
        GL.BindBuffer(0x8892, self.vbo_f)
        for loc, size, off in ((0, 4, 0), (1, 2, 16), (2, 3, 24), (3, 3, 36)):
            GL.VertexAttribPointer(loc, size, 0x1406, 0, 48, ctypes.c_void_p(off) if off else None)
            GL.EnableVertexAttribArray(loc)
        GL.BindVertexArray(self.vao_p)
        GL.BindBuffer(0x8892, self.vbo_p)
        GL.VertexAttribPointer(0, 4, 0x1406, 0, 16, None)
        GL.EnableVertexAttribArray(0)
        GL.BindVertexArray(0)
        self.flake_n = 0
        self.pile_draws = []
        self.occ_buf = (_f * 64)()

    @staticmethod
    def shader(kind, src):
        sh = GL.CreateShader(kind)
        arr = (ctypes.c_char_p * 1)(src.encode())
        GL.ShaderSource(sh, 1, arr, None)
        GL.CompileShader(sh)
        ok = _i()
        GL.GetShaderiv(sh, 0x8B81, ctypes.byref(ok))
        if not ok.value:
            log = ctypes.create_string_buffer(4096)
            GL.GetShaderInfoLog(sh, 4096, None, log)
            raise RuntimeError("shader: " + log.value.decode())
        return sh

    def program(self, vs, fs):
        prog = GL.CreateProgram()
        GL.AttachShader(prog, self.shader(0x8B31, vs))
        GL.AttachShader(prog, self.shader(0x8B30, fs))
        GL.LinkProgram(prog)
        ok = _i()
        GL.GetProgramiv(prog, 0x8B82, ctypes.byref(ok))
        if not ok.value:
            log = ctypes.create_string_buffer(4096)
            GL.GetProgramInfoLog(prog, 4096, None, log)
            raise RuntimeError("link: " + log.value.decode())
        return prog

    def upload_flakes(self, arr):
        GL.BindBuffer(0x8892, self.vbo_f)
        GL.BufferData(0x8892, arr.nbytes, arr.ctypes.data, 0x88E0)   # STREAM_DRAW
        self.flake_n = len(arr)

    def upload_piles(self, verts, draws):
        GL.BindBuffer(0x8892, self.vbo_p)
        GL.BufferData(0x8892, verts.nbytes, verts.ctypes.data, 0x88E8)  # DYNAMIC_DRAW
        self.pile_draws = draws

    def draw(self, W, H, scale, t, season=0, masks=()):
        GL.ClearColor(0, 0, 0, 0)
        GL.Clear(0x4000)
        GL.Enable(0x0BE2)
        GL.BlendFunc(1, 0x0303)                  # premultiplied alpha
        if not self.es:
            GL.Enable(0x8642)                    # PROGRAM_POINT_SIZE (core profile)
        if self.pile_draws:
            pr = self.pile
            GL.UseProgram(pr)
            GL.Uniform2f(self.u[(pr, "u_screen")], W, H)
            GL.Uniform1f(self.u[(pr, "u_scale")], scale)
            GL.Uniform1i(self.u[(pr, "u_season")], season)
            GL.BindVertexArray(self.vao_p)
            for shadow in (1.0, 0.0):
                GL.Uniform1f(self.u[(pr, "u_shadow")], shadow)
                GL.Uniform1f(self.u[(pr, "u_off")], 1.2 if shadow else 0.0)
                last_occ = None
                for first, count, occ in self.pile_draws:
                    if occ is not last_occ:
                        n = min(len(occ), 16)
                        for i in range(n):
                            self.occ_buf[i * 4:i * 4 + 4] = occ[i]
                        GL.Uniform4fv(self.u[(pr, "u_occ")], 16, self.occ_buf)
                        GL.Uniform1i(self.u[(pr, "u_nocc")], n)
                        last_occ = occ
                    GL.DrawArrays(5, first, count)            # TRIANGLE_STRIP
        if self.flake_n:
            fp = self.flake
            GL.UseProgram(fp)
            GL.Uniform2f(self.u[(fp, "u_screen")], W, H)
            GL.Uniform1f(self.u[(fp, "u_scale")], scale)
            GL.Uniform1f(self.u[(fp, "u_t")], t)
            n = min(len(masks), 8)
            mb = (_f * 32)()
            for i in range(n):
                mb[i * 4:i * 4 + 4] = [float(v) for v in masks[i]]
            GL.Uniform4fv(self.u[(fp, "u_mask")], 8, mb)
            GL.Uniform1i(self.u[(fp, "u_nmask")], n)
            GL.BindVertexArray(self.vao_f)
            GL.DrawArrays(0, 0, self.flake_n)                  # POINTS
        GL.BindVertexArray(0)
        GL.UseProgram(0)

class Pile:
    """Heightmap of snow on a surface whose floor is `base` (screen y per column).

    Windows: a flat top that curves down at the rounded corners.
    Ledges: a traced wallpaper edge (sloped, bumpy). Floor: flat.
    Snow slides on the *surface elevation* (floor + snow), so it creeps off
    a rounded corner or down a sloping roofline by itself.
    """

    def __init__(self, x0, base, maxh, edge=EDGE_REPOSE, repose=REPOSE):
        self.x0 = float(x0)
        self.base = np.asarray(base, float)
        self.n = len(self.base)
        self.w = self.n * COL
        self.top = float(self.base.min())
        self.bottom = float(self.base.max())
        self.h = np.zeros(self.n)
        self.fresh = np.zeros(self.n)    # seconds of "still loose" left per column
        self.covered = np.zeros(self.n, bool)
        self.clip = ()
        self.maxh, self.edge, self.repose = maxh, edge, repose
        # the cap undulates along the pile so a full pile looks like drifts, not a slab
        wob = np.cumsum(rng.normal(0, 1, self.n + 40))
        wob = np.convolve(wob - wob.mean(), np.ones(41) / 41, "valid")[: self.n]
        wob = wob / (np.abs(wob).max() + 1e-6)
        self.cap = maxh * (0.80 + 0.20 * wob)
        self.key = None
        self.kind = "window"         # window | ledge | floor
        self.owner = None            # window address (survives the window moving)
        self._v = None               # cached GPU vertices (float32 N x 4)
        self.dirty = True
        self.spill_l = self.spill_r = 0.0
        self.spill_x = []

    def set_occluders(self, rects):
        """Rects (x, y, w, h) drawn above this pile: no landing there, and clipped out."""
        rects = tuple(rects)
        if rects == self.clip:
            return
        self.clip = rects
        xs = self.x0 + (np.arange(self.n) + 0.5) * COL
        cov = np.zeros(self.n, bool)
        for ax, ay, aw, ah in rects:
            cov |= ((xs >= ax) & (xs <= ax + aw)
                    & (self.base + 1 >= ay) & (self.base - self.maxh <= ay + ah))
        self.covered = cov

    def deposit(self, cols, amounts, loose=True):
        # little gaussian bump per flake
        for off, k in ((-1, 0.25), (0, 0.5), (1, 0.25)):
            c = cols + off
            ok = (c >= 0) & (c < self.n)
            np.add.at(self.h, c[ok], amounts[ok] * k)
            if loose:                      # pushed/plowed snow is packed, not loose
                self.fresh[c[ok]] = 0.8
            self._spill(float((amounts[(c < 0)] * k).sum()), -1)
            self._spill(float((amounts[(c >= self.n)] * k).sum()), 1)
        np.minimum(self.h, self.cap, out=self.h)
        self.dirty = True

    def settle(self, dt, wind_x):
        h = self.h
        if h.max() <= 0.01:
            return
        before = h.copy()
        # wind drift: blow some of the loose top layer downwind
        fw = min(0.5, abs(wind_x) / MAX_WIND * 1.4 * dt)
        if fw > 0.0005:
            moved = np.maximum(h - 1.0, 0) * fw
            h -= moved
            if wind_x > 0:
                h[1:] += moved[:-1]
                self._spill(moved[-1], 1)
            else:
                h[:-1] += moved[1:]
                self._spill(moved[0], -1)
        # angle of repose on the surface elevation; a column can only give
        # away half of what it holds per pass, so nothing goes negative
        # loose (freshly landed) snow slumps to the repose angle; settled
        # snow is cohesive and holds much steeper walls
        loose = self.fresh > 0
        R = np.where(loose[1:] | loose[:-1], self.repose, COHESION)
        self.fresh = np.maximum(self.fresh - dt, 0)
        for _ in range(8):
            e = h - self.base                  # bigger = higher on screen
            d = e[1:] - e[:-1]                 # > 0: right neighbour higher
            ex = np.abs(d) - R
            f = np.where(ex > 0, ex * 0.4, 0.0) + 0.02 * np.abs(d)
            f_l = np.where(d > 0, np.minimum(f, 0.5 * h[1:]), 0.0)    # right -> left
            f_r = np.where(d < 0, np.minimum(f, 0.5 * h[:-1]), 0.0)   # left -> right
            h[1:] += f_r - f_l
            h[:-1] += f_l - f_r
            # snow that slides stays loose, so a landing keeps slumping until it rests
            # (otherwise sparse snowfall freezes into separate little towers)
            fr = self.fresh
            fr[:-1] = np.where(f_l > 0, np.maximum(fr[:-1], fr[1:]), fr[:-1])
            fr[1:] = np.where(f_r > 0, np.maximum(fr[1:], fr[:-1]), fr[1:])
            loose = fr > 0
            R = np.where(loose[1:] | loose[:-1], self.repose, COHESION)
            if h[0] > self.edge:
                x = (h[0] - self.edge) * 0.3
                h[0] -= x
                self._spill(x, -1)
            if h[-1] > self.edge:
                x = (h[-1] - self.edge) * 0.3
                h[-1] -= x
                self._spill(x, 1)
        over = np.maximum(h - self.cap, 0)
        if over.any():
            h -= over
        if np.abs(h - before).max() > 0.03:
            self.dirty = True

    def _spill(self, amount, side):
        # mass-conserving: one loose flake per SPILL_UNIT of snow lost,
        # launched just OUTSIDE the edge so it can't land back on this pile
        if amount <= 0:
            return
        if side < 0:
            self.spill_l += amount
            while self.spill_l >= SPILL_UNIT:
                self.spill_l -= SPILL_UNIT
                self.spill_x.append((self.x0 - 4.0, self.base[0] - 2, -1))
        else:
            self.spill_r += amount
            while self.spill_r >= SPILL_UNIT:
                self.spill_r -= SPILL_UNIT
                self.spill_x.append((self.x0 + self.w + 4.0, self.base[-1] - 2, 1))

    def melt(self, rate):
        if self.h.max() > 0:
            self.h = np.maximum(self.h - rate, 0)
            self.dirty = True

    def verts(self):
        """Triangle-strip vertices (x, y, depth-below-surface, snow height), cached until dirty.

        Top vertices sit 1px above the snow (depth -1) so the shader can
        anti-alias the crest; end caps with height 0 taper the strip.
        """
        if not self.dirty:
            return self._v
        self.dirty = False
        h = self.h
        if h.max() < 0.3:
            self._v = None
            return None
        hs = h.copy()
        if self.n > 4:
            hs[1:-1] = (h[:-2] + 2 * h[1:-1] + h[2:]) / 4
        xs = self.x0 + (np.arange(self.n) + 0.5) * COL
        bs = self.base
        if self.n > 40:                          # every other column is plenty
            sel = np.r_[np.arange(0, self.n - 1, 2), self.n - 1]
            xs, bs, hs = xs[sel], bs[sel], hs[sel]
        X = np.r_[self.x0, xs, self.x0 + self.w]
        B = np.r_[bs[0], bs, bs[-1]]
        Hh = np.r_[0.0, hs, 0.0]
        v = np.empty((2 * len(X), 4), np.float32)
        v[0::2, 0] = X
        v[0::2, 1] = B - Hh - 1.0
        v[0::2, 2] = -1.0
        v[0::2, 3] = Hh
        v[1::2, 0] = X
        v[1::2, 1] = B
        v[1::2, 2] = Hh
        v[1::2, 3] = Hh
        self._v = v
        return v

def bezier_ease(p1, p2):
    """CSS-style cubic bezier (0,0)-p1-p2-(1,1) as t -> progress, like Hyprland's curves."""
    (x1, y1), (x2, y2) = p1, p2

    def b(u, a, c):
        return 3 * a * u * (1 - u) ** 2 + 3 * c * u * u * (1 - u) + u ** 3

    def ease(t):
        if t <= 0:
            return 0.0
        if t >= 1:
            return 1.0
        lo, hi = 0.0, 1.0
        for _ in range(24):                  # x(u) is monotonic: bisect for u
            mid = (lo + hi) / 2
            if b(mid, x1, x2) < t:
                lo = mid
            else:
                hi = mid
        return b((lo + hi) / 2, y1, y2)
    return ease

def corner_base(top, n, radius, power):
    """Window top edge with Hyprland's rounded corners (superellipse, power 2 = circle)."""
    xs = (np.arange(n) + 0.5) * COL
    dx = np.minimum(xs, n * COL - xs)
    if radius <= 0:
        return np.full(n, float(top))
    t = np.clip((radius - dx) / radius, 0, 1)      # 1 right at the edge
    drop = radius * (1 - (1 - t ** power) ** (1 / power))
    return top + drop

class Sim:
    def __init__(self, plat):
        self.plat = plat
        self.gl = None               # Renderer, made on GLArea realize
        self.flake_arr = None        # pending flake upload
        self.pile_sig = None
        self.pile_check = True       # piles may have changed since the last upload
        self.ws_active = None        # active workspace id (normal) / special id
        self.ws_special = 0
        self.releasing = []          # piles left behind by windows sliding off screen
        self.land_block_until = 0.0  # no landing on windows while they slide in
        self.ws_anim = ("slide", 0.7, lambda t: t)   # refreshed from Hyprland
        self.W, self.H = 1920, 1080
        self.mon = (0, 0)
        self.t = 0.0
        self.paused = False
        self.hidden = False
        self.piles = {}          # address -> Pile (persist across workspaces)
        self.visible = []        # addresses on screen now
        self.floor = None
        self.rects = []
        self.ledges = []         # Piles on wallpaper edges
        self.wp_path = None
        self.deco = (12.0, 2.0)      # corner radius (incl. border), superellipse power
        self.polls = 0
        n = int(BASE_FLAKES * 4)  # capacity: max intensity (3x) + room for shaken-off snow
        self.cap = n
        self.x = rng.uniform(0, self.W, n)
        self.y = rng.uniform(-self.H, self.H, n)
        self.px = self.x.copy()      # positions at the previous step (for interpolation)
        self.py = self.y.copy()
        self.depth = rng.beta(2, 2, n)
        self.r = 0.9 + self.depth * 2.8 + rng.uniform(-0.3, 0.3, n)
        self.phase = rng.uniform(0, 6.283, n)
        self.freq = rng.uniform(0.35, 0.9, n)
        self.phase2 = rng.uniform(0, 6.283, n)
        self.sway = rng.uniform(3, 10, n)
        self.cur = None              # cursor position (screen px)
        self.refresh = 60.0
        self.cur_v = np.zeros(2)     # smoothed cursor velocity (px/s)
        self.collide = self.depth > 0.35
        self.season = self.pick_season()
        self.col = np.ones((n, 3), np.float32)
        self.ang = rng.uniform(0, 6.283, n)
        self.pang = self.ang.copy()
        self.spin = np.zeros(n)
        # only some near flakes land on windows; the rest fall past in front of
        # them, so the lower screen isn't left with only the faint far flakes
        self.sticky = rng.random(n) < state.get("stick", 0.35)
        # surface a loose flake was knocked off (-1 = fresh snowfall); it won't
        # land back on that same window/ledge
        self.origin = np.full(n, -1, np.int64)
        self.owner_ids = {}
        self.alive = np.zeros(n, bool)
        self.vx_extra = np.zeros(n)
        self.vy_extra = np.zeros(n)
        self.spawn_acc = 0.0
        self.prefill = True

    # ---- geometry ------------------------------------------------------
    def poll(self):
        """Refresh window geometry from the platform and keep piles in sync."""
        snap = self.plat.snapshot()
        if snap is None:
            return True
        self.mon = snap["origin"]
        self.W, self.H = snap["size"]
        # where the bottom pile rests: the screen edge, or the top of a taskbar/dock
        self.floor_y = float(snap.get("floor_y", self.H))
        self.refresh = snap.get("refresh", 60.0) or 60.0
        prev_ws, prev_sp = self.ws_active, self.ws_special
        self.ws_active, self.ws_special = snap.get("ws", 0), snap.get("special", 0)
        vis = snap["windows"]                      # topmost first, overlay-relative px
        self.set_hidden(bool(snap.get("hide")))
        if self.polls % 80 == 0:                   # ~every 5s
            d = self.plat.decoration()
            if d:
                self.deco = d
            an = self.plat.ws_anim()
            if an:
                self.ws_anim = an
        self.polls += 1
        if self.polls % 500 == 0:                  # ~every 30s: has the season turned?
            self.set_season(self.pick_season())
        radius, power = self.deco
        rects = []
        seen = []
        for c in vis:
            x0, y0, w, hgt = c["x"], c["y"], c["w"], c["h"]
            rad = c.get("radius", radius)
            addr = c["addr"]
            p = self.piles.get(addr)
            key = (x0, y0, w, rad, power)
            if p is None or p.key != key:
                if p is not None:
                    self.shake(p)
                n = max(1, int(w // COL))
                p = Pile(x0, corner_base(y0, n, rad, power), MAXH * max(self.S()["maxh"], 0.01))
                p.owner = addr
                p.key = key
                self.piles[addr] = p
            # higher windows hide (and block) parts of this pile
            p.set_occluders(rects)
            rects.append((x0, y0, w, hgt))
            seen.append(addr)
        alive_addrs = snap.get("alive", set(seen))
        now = time.monotonic()
        style, dur, ease = self.ws_anim
        switched = prev_ws is not None and self.ws_active != prev_ws
        gone = [a for a in self.visible if a not in seen and a in self.piles]
        for a in gone:
            p = self.piles.pop(a)
            if (switched and a in alive_addrs and style.startswith("slide")
                    and "vert" not in style and dur > 0):
                # the window slides sideways out from under its snow: the snow
                # stays put and falls column by column as support goes away
                direction = self.plat.slide_direction(prev_ws, self.ws_active)
                self.releasing.append({"p": p, "dir": direction, "t0": now - POLL_MS / 2000,
                                       "dur": dur, "ease": ease, "w": float(self.W),
                                       "density": self.shake_budget(p)})
                p.set_occluders(())
            else:
                # closed, moved to another workspace/desktop, minimised: all of it falls
                self.shake(p)
        for a in list(self.piles):
            if a not in alive_addrs or a not in seen:
                del self.piles[a]                  # off-screen windows keep no snow
        if switched or self.ws_special != prev_sp:
            # windows sliding in are still moving: nothing lands until they stop
            self.land_block_until = now + dur
        self.visible = seen
        self.rects = rects
        self.masks = self.page_masks(vis)
        for p in self.ledges:                      # the wallpaper is behind every window
            p.set_occluders(rects)
        self.pile_check = True
        return True

    # ---- wallpaper ledges ----------------------------------------------
    def watch_wallpaper(self):
        if not state.get("ledges", True):
            if self.ledges:
                for p in self.ledges:
                    self.shake(p)
                self.ledges = []
            self.wp_path = None
            self.wp_key = None
            return True
        path = state.get("wallpaper") or self.plat.wallpaper()
        try:
            key = (path, os.path.getmtime(path)) if path else None
        except OSError:
            key = (path, 0)
        if path and key != getattr(self, "wp_key", None):
            self.wp_key = key
            first = self.wp_path is None
            self.wp_path = path
            for p in self.ledges:
                self.shake(p)
            self.ledges = []
            # let the wallpaper transition finish before reading the screen
            self.plat.later(300 if first else self.plat.wallpaper_settle_ms,
                            lambda: self.start_detect(path))
        return True

    def start_detect(self, path):
        if path != self.wp_path:
            return False
        occ = self.plat.bar_mask(path, self)
        W, H = self.W, self.H

        def work():
            try:
                ls = detect_ledges(path, W, H, occ, getattr(self.plat, "wallpaper_fit", "cover"))
            except Exception as e:  # unreadable image, video wallpaper, ...
                print(f"seasoning: no ledges for {path}: {e}", file=sys.stderr)
                ls = []
            self.plat.idle(lambda: self.install_ledges(path, ls))
        threading.Thread(target=work, daemon=True).start()
        return False

    def install_ledges(self, path, ls):
        if path != self.wp_path:
            return False                           # superseded by a newer wallpaper
        self.ledges = []
        for x0, ys in ls:
            p = Pile(x0, ys, LEDGE_MAXH * max(self.S()["maxh"], 0.01), edge=2.0, repose=LEDGE_REPOSE)
            p.kind = "ledge"
            p.set_occluders(self.rects)
            self.ledges.append(p)
        self.pile_check = True
        print(f"seasoning: {len(self.ledges)} ledges on {os.path.basename(path)}", file=sys.stderr)
        return False

    def page_masks(self, vis):
        """Page areas of browser windows showing a tab the extension is animating."""
        pages = getattr(self, "pages", None)
        if not pages:
            return []
        now = time.monotonic()
        live = {t: v for t, v in list(pages.items()) if v[0] > now}
        self.pages = live
        out = []
        for c in vis:
            title = c.get("title") or ""
            for t, (_, top) in live.items():
                if t and t in title:
                    out.append((c["x"], c["y"] + top, c["w"], max(0, c["h"] - top)))
                    break
        return out[:8]

    def set_hidden(self, hide):
        if hide != self.hidden:
            self.hidden = hide
            self.plat.set_visible(not hide)

    def release_step(self):
        """Drop the snow of windows sliding off screen as they uncover it."""
        if not self.releasing:
            return
        now = time.monotonic()
        keep = []
        for r in self.releasing:
            p = r["p"]
            prog = (now - r["t0"]) / r["dur"]
            s = r["w"] * r["ease"](min(max(prog, 0.0), 1.0))
            xs = p.x0 + (np.arange(p.n) + 0.5) * COL
            if r["dir"] < 0:                       # window moving left: right end loses support first
                unsup = xs > p.x0 + p.w - s
            else:
                unsup = xs < p.x0 + s
            drop = unsup & (p.h > 0)
            if drop.any():
                self.shake(p, drop, density=r["density"])
                p.h[drop] = 0
                p.dirty = True
            if p.h.max() > 0.05 and prog < 1.5:
                keep.append(r)
            elif p.h.max() > 0.05:
                self.shake(p)                      # safety: never leave snow floating
        self.releasing = keep
        self.pile_check = True

    def shake_budget(self, p):
        """Fraction of a pile's flakes the pool can afford, planned once per pile."""
        sel = p.h > 0.8
        total = int(np.minimum((p.h[sel] / 3).astype(int) + 1, 4).sum())
        room = int((~self.alive).sum()) - int(self.cap * 0.15)
        return 0.0 if room <= 0 or total == 0 else min(1.0, 0.85 * room / total)

    def pick_season(self):
        s = state.get("season", "auto")
        return s if s in SEASONS else season_now()

    def S(self):
        return SEASONS[self.season]

    def style(self, idx):
        """Colour, size, spin and landing for particles of the current season."""
        k = len(idx)
        if not k:
            return
        S = self.S()
        pal = np.array(SEASON_COLORS[self.season], np.float32)
        self.col[idx] = pal[rng.integers(0, len(pal), k)]
        self.r[idx] = (0.9 + self.depth[idx] * 2.8 + rng.uniform(-0.3, 0.3, k)) * S["size"]
        self.spin[idx] = rng.uniform(-1, 1, k) * S["spin"]
        self.ang[idx] = rng.uniform(0, 6.283, k)
        self.pang[idx] = self.ang[idx]
        if S["maxh"] <= 0:
            self.collide[idx] = False

    def set_season(self, name):
        """Switch what falls; what's lying around blows/melts away."""
        if name == self.season:
            return
        self.season = name
        for p in list(self.piles.values()) + self.ledges + ([self.floor] if self.floor else []):
            p.h[:] = 0
            p.dirty = True
        self.floor = None                  # rebuilt with this season's cap
        self.piles = {}
        self.alive[:] = False
        self.prefill = True
        self.wp_path = None                # ledges get rebuilt with the new caps
        self.wp_key = None
        self.pile_check = True
        print(f"seasoning: season -> {name}", file=sys.stderr)

    def oid(self, p):
        """Small int id for the surface a pile belongs to (the window, not the Pile object)."""
        key = p.owner if p.owner is not None else ("pile", id(p))
        v = self.owner_ids.get(key)
        if v is None:
            v = self.owner_ids[key] = len(self.owner_ids)
        return v

    def shake(self, p, mask=None, push=0.0, density=None):
        """Window moved/closed: its pile (or the masked columns) falls off as loose flakes."""
        sel = p.h > 0.8 if mask is None else (mask & (p.h > 0.8))
        idx = np.nonzero(sel)[0]
        if not len(idx):
            return
        amount = p.h[idx]
        count = np.minimum((amount / 3).astype(int) + 1, 4)
        cols = np.repeat(idx, count)
        if density is not None:                    # planned thinning, same along the whole pile
            cols = cols[rng.random(len(cols)) < density]
        # never take the whole pool (snowfall would stall) and, when short of
        # slots, thin out evenly along the pile instead of dropping whole stretches
        free = np.nonzero(~self.alive)[0]
        room = min(600, len(free) - int(self.cap * 0.15))
        if room <= 0:
            return
        if len(cols) > room:
            cols = cols[np.linspace(0, len(cols) - 1, room).astype(int)]
        free = free[:len(cols)]
        k = len(free)
        if not k:
            return
        self.x[free] = p.x0 + cols * COL + rng.uniform(0, COL, k)
        self.y[free] = p.base[cols] - rng.uniform(0, 1, k) * p.h[cols]
        self.px[free] = self.x[free]
        self.py[free] = self.y[free]
        self.depth[free] = rng.uniform(0.6, 1.0, k)
        self.style(free)
        self.r[free] = rng.uniform(1.2, 2.6, k) * self.S()["size"]
        self.collide[free] = True
        self.sticky[free] = True
        self.origin[free] = self.oid(p)
        self.vx_extra[free] = rng.normal(0, 25, k)
        self.vy_extra[free] = rng.uniform(-40, 30, k)
        self.alive[free] = True

    # ---- simulation ----------------------------------------------------
    def wind(self):
        a = math.radians(state["wind_deg"])
        s = state["wind"]
        return math.cos(a) * s * MAX_WIND, math.sin(a) * s * MAX_WIND * 0.35

    def spawn(self, k, anywhere=False):
        free = np.nonzero(~self.alive)[0][:k]
        k = len(free)
        if not k:
            return
        wx, wy = self.wind()
        d = rng.beta(2, 2, k)
        self.x[free] = rng.uniform(-20, self.W + 20, k)
        self.y[free] = rng.uniform(-30, self.H, k) if anywhere else rng.uniform(-30, -4, k)
        probe_x = self.x[free].copy()          # where to check for shelter
        if not anywhere and abs(wx) > 1:
            # Share of the snow that enters through the upwind screen edge, from
            # the flakes' real mean speeds (depth factor averages 0.9 for vx, and
            # the wind's vertical part speeds up or slows the fall from the top).
            vx_eff = abs(wx) * 0.9
            vy_eff = max(FALL * self.S()["fall"] * 0.9 + wy, 6.0)
            flux_side = vx_eff * self.H
            p_side = flux_side / (flux_side + vy_eff * self.W)
            side = rng.random(k) < p_side
            ns = int(side.sum())
            if ns:
                fs = free[side]
                self.x[fs] = -15.0 if wx > 0 else self.W + 15.0
                self.y[fs] = rng.uniform(-20, self.H * 0.9, ns)
                probe_x[side] = 100.0 if wx > 0 else self.W - 100.0
        # A "near" flake can't exist under a window top: the ones falling from
        # above all landed on it. Near flakes entering from the side (or
        # prefilled) under a window would pour in front of it and over-fill the
        # upwind edge (measured 2x density there), so they simply don't spawn.
        # Don't turn them into far flakes either: the side share applies to
        # every depth, so that would give the far class ~3.5x its side flux.
        ys = self.y[free]
        shelter = np.zeros(k, bool)
        for ax, ay, aw, ah in self.rects:
            shelter |= (probe_x >= ax) & (probe_x <= ax + aw) & (ys > ay)
        stick = rng.random(k) < state.get("stick", 0.35)
        drop = shelter & (d > 0.35) & stick
        if drop.any():
            keep = ~drop
            free, d, stick, k = free[keep], d[keep], stick[keep], int(keep.sum())
            if not k:
                return
        self.sticky[free] = stick
        self.origin[free] = -1
        self.depth[free] = d
        self.collide[free] = d > 0.35
        self.style(free)
        self.phase[free] = rng.uniform(0, 6.283, k)
        self.vx_extra[free] = 0
        self.vy_extra[free] = 0
        self.alive[free] = True

    def step(self, dt):
        if self.hidden or self.paused and not self.alive.any():
            return True
        was = self.alive.copy()
        self.px[:] = self.x
        self.py[:] = self.y
        self.t += dt
        self.steps = getattr(self, "steps", 0) + 1
        wx, wy = self.wind()
        S = self.S()
        target = int(BASE_FLAKES * state["intensity"] * S["count"])
        if self.prefill:
            self.spawn(target, anywhere=True)
            self.prefill = False
        if not self.paused:
            # keep population near target: spawn at the rate flakes leave
            rate = 1.8 * target * FALL * S["fall"] / (self.H + 60)
            self.spawn_acc += rate * dt
            live = int(self.alive.sum())
            if live < target and self.spawn_acc >= 1:
                k = int(self.spawn_acc)
                self.spawn_acc -= k
                self.spawn(min(k, target - live))
        a = self.alive
        d = self.depth
        self.cursor_push(dt)
        ws = self.plat.active_ws()                 # catch workspace switches within a step
        if ws is not None and ws != self.ws_active:
            self.poll()
        self.release_step()
        # extra velocity (cursor kicks, shake-offs) may send flakes upward for a moment
        fall = FALL * S["fall"] * (0.45 + 0.9 * d)
        if S["shape"] in (1, 2):
            # leaves and petals flutter: they speed up and stall as they rock
            fall = fall * (1.0 + 0.55 * np.sin(self.phase2 + self.t * self.freq * 2.9))
        vy = np.maximum(fall + wy * S["wind"] * (0.5 + d), 12.0 * S["fall"]) + self.vy_extra
        sway = self.sway * S["sway"] * (np.sin(self.phase + self.t * self.freq)
                                       + 0.45 * np.sin(self.phase2 + self.t * self.freq * 2.37))
        vx = wx * S["wind"] * (0.45 + 0.9 * d) + self.vx_extra + sway
        self.pang[:] = self.ang
        self.ang += self.spin * dt
        self.vx_extra *= 0.97 ** (dt * 60)
        self.vy_extra *= 0.95 ** (dt * 60)
        py = self.y.copy()
        self.x = np.where(a, self.x + vx * dt, self.x)
        self.y = np.where(a, self.y + vy * dt, self.y)

        # landing
        if time.monotonic() < self.land_block_until:
            piles = []
        else:
            piles = [self.piles[k] for k in self.visible if k in self.piles]
        piles += self.ledges
        if state.get("floor", True):
            if self.floor is None or self.floor.w != self.W or self.floor.top != getattr(self, "floor_y", self.H):
                n = int(self.W // COL)
                self.floor = Pile(0, np.full(n, getattr(self, "floor_y", float(self.H))),
                                  FLOOR_MAXH * max(self.S()["maxh"], 0.01), edge=99)
                self.floor.kind = "floor"
            piles.append(self.floor)
        idx = np.nonzero(a & self.collide)[0]
        if len(idx):
            X, Y, PY = self.x[idx], self.y[idx], py[idx]
            landed = np.zeros(len(idx), bool)
            drift0 = wx / MAX_WIND * 6
            for p in piles:
                cols = np.floor((X - p.x0) / COL).astype(np.int64)
                m = (~landed & (cols >= 0) & (cols < p.n)
                     & (Y >= p.top - p.maxh - 3) & (PY <= p.bottom + 3))
                if not m.any():
                    continue
                j = np.nonzero(m)[0]
                c = cols[j]
                sy = p.base[c] - p.h[c]
                if p is self.floor:
                    hit = Y[j] >= sy
                else:
                    hit = (Y[j] >= sy) & (PY[j] <= sy + 2.5) & ~p.covered[c]
                    if p.kind == "window":
                        hit &= self.sticky[idx[j]]
                    hit &= self.origin[idx[j]] != self.oid(p)      # not back where it came from
                if not hit.any():
                    continue
                jj, cc = j[hit], c[hit]
                landed[jj] = True
                full = p.h[cc] >= p.cap[cc] - 0.5
                # a full spot: let it tumble off instead of stacking
                amt = np.where(full, 0.0, 2.4 * (self.r[idx[jj]] / S["size"]) ** 2 * S["mass"])
                if p.kind == "window":             # fewer flakes stick: keep piles growing as fast
                    amt *= min(3.0, 1.0 / max(state.get("stick", 0.35), 0.05))
                drift = np.rint(rng.normal(drift0, 1.5, len(jj))).astype(np.int64)
                p.deposit(cc + drift, amt)
            self.alive[idx[landed]] = False
        self.frame = getattr(self, "frame", 0) + 1
        budget = max(0, int(target * 1.25) - int(self.alive.sum()))
        for i, p in enumerate(piles):
            if (i + self.frame) % 4 == 0:          # each pile settles at 15 Hz, staggered
                p.settle(dt * 4, wx)
                if self.paused:
                    p.melt(0.2)
                elif p is self.floor:
                    p.melt(0.008)
            if p.spill_x:
                for sx, sy, side in p.spill_x[:budget]:
                    self.drop_from(sx, sy, side, self.oid(p))
                budget = max(0, budget - len(p.spill_x))
                p.spill_x.clear()

        # cull
        dead = a & ((self.y > self.H + 10) | (self.y < -300) | (self.x < -60) | (self.x > self.W + 60))
        self.alive &= ~dead
        # newborn flakes have no previous position: don't streak them across the screen
        new = self.alive & ~was
        self.px[new] = self.x[new]
        self.py[new] = self.y[new]
        idx = np.nonzero(self.alive)[0]
        arr = np.empty((len(idx), 12), np.float32)
        arr[:, 0] = self.px[idx]
        arr[:, 1] = self.py[idx]
        arr[:, 2] = self.x[idx]
        arr[:, 3] = self.y[idx]
        arr[:, 4] = self.r[idx] * 3.2
        arr[:, 5] = 0.35 + 0.6 * self.depth[idx]
        if S["shape"] != 0:
            arr[:, 5] = 0.55 + 0.45 * self.depth[idx]        # leaves/petals are opaque-ish
        arr[:, 6:9] = self.col[idx]
        arr[:, 9] = S["shape"]
        arr[:, 10] = self.pang[idx]
        arr[:, 11] = self.ang[idx]
        self.flake_arr = arr
        self.pile_check = True
        return True

    def pile_batch(self):
        """(vertices, [(first, count, occluders)]) for every drawable pile; None if unchanged."""
        if not self.pile_check:
            return None
        self.pile_check = False
        piles = [self.piles[k] for k in self.visible if k in self.piles] + self.ledges
        piles += [r["p"] for r in self.releasing]
        if self.floor is not None and state.get("floor", True):
            piles.append(self.floor)
        changed = any(p.dirty for p in piles)
        sig = tuple(id(p) for p in piles) + tuple(p.clip for p in piles)
        if not changed and sig == self.pile_sig:
            return None
        self.pile_sig = sig
        chunks, draws, first = [], [], 0
        # ledges first: they sit behind everything else
        for p in self.ledges + [q for q in piles if q not in self.ledges]:
            v = p.verts()
            if v is None:
                continue
            chunks.append(v)
            draws.append((first, len(v), p.clip))
            first += len(v)
        verts = np.concatenate(chunks) if chunks else np.zeros((0, 4), np.float32)
        return verts, draws

    # ---- cursor -------------------------------------------------------
    def cursor_push(self, dt):
        c = self.plat.cursor()
        if c is None:
            return
        pos = np.array([c[0] - self.mon[0], c[1] - self.mon[1]], float)
        if self.cur is None or dt <= 0:
            self.cur = pos
            return
        v = (pos - self.cur) / dt
        n = np.hypot(*v)
        if n > 4000:                               # warp/teleport, not a swipe
            v *= 0
        self.cur_v = self.cur_v * 0.5 + v * 0.5
        prev, self.cur = self.cur, pos
        speed = float(np.hypot(*self.cur_v))
        if speed < 25:
            return
        # falling flakes: dragged along by the cursor's wake and pushed aside
        a = self.alive
        dx = self.x - pos[0]
        dy = self.y - pos[1]
        dist = np.hypot(dx, dy)
        near = a & (dist < CURSOR_R)
        if near.any():
            i = np.nonzero(near)[0]
            f = (1 - dist[i] / CURSOR_R) ** 2
            ux = dx[i] / (dist[i] + 1e-3)
            uy = dy[i] / (dist[i] + 1e-3)
            push = min(speed, 1500) * 0.35
            tx = self.cur_v[0] * 0.7 + ux * push
            ty = self.cur_v[1] * 0.7 + uy * push
            k = np.minimum(f * 0.6, 1.0)
            self.vx_extra[i] += (tx - self.vx_extra[i]) * k
            self.vy_extra[i] += (ty - self.vy_extra[i]) * k
        # piles: carve a groove along the cursor's path, shove the snow ahead
        # of it and kick some up as loose flakes
        steps = max(1, int(np.hypot(*(pos - prev)) // 6))
        for p in self.surfaces():
            if (pos[0] < p.x0 - PLOW_R - 40 and prev[0] < p.x0 - PLOW_R - 40) or \
               (pos[0] > p.x0 + p.w + PLOW_R + 40 and prev[0] > p.x0 + p.w + PLOW_R + 40):
                continue
            lo, hi = p.top - p.maxh - 6 - CURSOR_BODY[1], p.bottom + 3
            if max(pos[1], prev[1]) < lo or min(pos[1], prev[1]) > hi:
                continue
            for t in np.linspace(1.0 / steps, 1.0, steps):
                cx, cy = prev + (pos - prev) * t
                self.plow(p, cx + CURSOR_BODY[0], cy + CURSOR_BODY[1])

    def plow(self, p, cx, cy):
        c0 = max(0, int((cx - PLOW_R - p.x0) // COL))
        c1 = min(p.n, int((cx + PLOW_R - p.x0) // COL) + 1)
        if c1 <= c0:
            return
        sl = slice(c0, c1)
        h = p.h[sl]
        if h.max() < 0.3:
            return
        xs = p.x0 + (np.arange(c0, c1) + 0.5) * COL
        fall = np.clip(1 - ((xs - cx) / PLOW_R) ** 2, 0, 1) * ~p.covered[sl]
        keep = np.clip(p.base[sl] - cy, 0, None)                  # snow below the arrow stays
        cut = np.maximum(h - keep, 0) * fall
        m = float(cut.sum())
        if m < 0.2:
            return
        p.h[sl] = h - cut
        p.fresh[sl] = 0.0                                          # carved walls are packed
        p.dirty = True
        vx, vy = self.cur_v
        sp = np.hypot(vx, vy) + 1e-3
        horiz = abs(vx) / sp
        # most of it is shoved ahead (horizontal moves), the rest flies
        shove = m * 0.65 * horiz
        if shove > 0:
            side = 1 if vx > 0 else -1
            ahead = int(round((cx + side * (PLOW_R + 4) - p.x0) / COL))
            cols = np.arange(ahead, ahead + side * 5, side)
            p.deposit(cols, np.full(5, shove / 5), loose=False)  # spills if it's off the end
        self.kick_acc = getattr(self, "kick_acc", 0.0) + (m - shove)
        n = min(int(self.kick_acc / 6.5), 12)        # a flake re-deposits ~6.5 when it lands
        self.kick_acc -= n * 6.5
        free = np.nonzero(~self.alive)[0][:n]
        k = len(free)
        if k:
            self.x[free] = cx + rng.uniform(-PLOW_R, PLOW_R, k)
            self.y[free] = np.minimum(cy, p.base[min(max((c0 + c1) // 2, 0), p.n - 1)]) - rng.uniform(2, 8, k)
            self.depth[free] = rng.uniform(0.7, 1.0, k)
            self.style(free)
            self.r[free] = rng.uniform(1.0, 2.2, k) * self.S()["size"]
            self.collide[free] = True
            self.sticky[free] = True
            self.origin[free] = self.oid(p)
            self.vx_extra[free] = vx * rng.uniform(0.3, 0.8, k) + rng.normal(0, 30, k)
            self.vy_extra[free] = -abs(vy) * 0.3 - rng.uniform(60, 160, k)
            self.alive[free] = True

    def surfaces(self):
        out = [self.piles[k] for k in self.visible if k in self.piles] + self.ledges
        if self.floor is not None and state.get("floor", True):
            out.append(self.floor)
        return out

    def drop_from(self, x, y, side, origin=-1):
        free = np.nonzero(~self.alive)[0][:1]
        for i in free:
            self.x[i] = x + rng.uniform(-2, 2)
            self.y[i] = y
            self.depth[i] = 0.9
            self.style(np.array([i]))
            self.r[i] = rng.uniform(1.3, 2.2) * self.S()["size"]
            self.collide[i] = True
            self.sticky[i] = True
            self.origin[i] = origin
            self.vx_extra[i] = side * rng.uniform(10, 30)
            self.vy_extra[i] = -10
            self.alive[i] = True

    # ---- control -------------------------------------------------------
    def command(self, line):
        self.pile_check = True
        parts = line.split()
        if not parts:
            return "?"
        c = parts[0]
        if c == "ping":
            return "pong"
        if c == "status":
            return json.dumps({**state, "paused": self.paused, "hidden": self.hidden,
                               "flakes": int(self.alive.sum()), "piles": len(self.visible),
                               "ledges": len(self.ledges), "wallpaper": self.wp_path,
                               "season_now": self.season,
                               "pages": len(getattr(self, "masks", []))})
        if c == "dumpstyle":
            a = np.nonzero(self.alive)[0][:12]
            return json.dumps({"season": self.season, "n": int(self.alive.sum()),
                               "r": self.r[a].round(2).tolist(), "col": self.col[a].round(2).tolist(),
                               "spin": self.spin[a].round(2).tolist(), "y": self.y[a].round(0).tolist(),
                               "depth": self.depth[a].round(2).tolist()})
        if c == "dump":
            a = self.alive
            return json.dumps({"x": self.x[a].round(1).tolist(), "y": self.y[a].round(1).tolist(),
                               "d": self.depth[a].round(2).tolist(), "c": self.collide[a].tolist(),
                               "vx": self.vx_extra[a].round(1).tolist(), "vy": self.vy_extra[a].round(1).tolist(),
                               "rects": self.rects, "W": self.W, "H": self.H, "wind": self.wind()})
        if c == "debug":
            a = self.alive
            x, y = self.x[a], self.y[a]
            return json.dumps({
                "W": self.W, "H": self.H, "alive": int(a.sum()), "steps": getattr(self, "steps", 0),
                "ticks": self.fclock["tick"], "renders": self.fclock["renders"],
                "refresh": self.refresh,
                "nan": int((~np.isfinite(x) | ~np.isfinite(y)).sum()),
                "y_above": int((y < -40).sum()), "y_on": int(((y >= -40) & (y <= self.H)).sum()),
                "x_off": int(((x < 0) | (x > self.W)).sum()),
                "y_hist": np.histogram(y[np.isfinite(y)], bins=8)[0].tolist(),
                "x_hist": np.histogram(x[np.isfinite(x)], bins=8, range=(0, self.W))[0].tolist(),
                "y_range": [float(np.nanmin(y)), float(np.nanmax(y))] if len(y) else None,
                "vx_extra_max": float(np.abs(self.vx_extra[a]).max()) if len(y) else 0,
                "piles": {k: [p.x0, p.top, p.w, round(float(p.h.max()), 1)] for k, p in self.piles.items()},
                "profile": [round(float(v), 1) for v in
                            (self.piles[self.visible[0]].h[::8] if self.visible and self.visible[0] in self.piles else [])],
                "pile_mass": {k: round(float(p.h.sum()), 1) for k, p in self.piles.items()},
                "ledge_snow": [round(float(p.h.sum()), 1) for p in self.ledges],
                "corners": self.deco,
                "bridge_pages": sorted(getattr(self, "pages", {}) or {}),
                "masks": getattr(self, "masks", []),
            })
        if c == "wind" and len(parts) == 3:
            state["wind_deg"] = float(parts[1]) % 360
            state["wind"] = max(0.0, min(1.0, float(parts[2]) / 100))
        elif c == "intensity" and len(parts) == 2:
            state["intensity"] = max(0.0, min(3.0, float(parts[1])))
        elif c == "season" and len(parts) == 2 and parts[1] in list(SEASONS) + ["auto"]:
            state["season"] = parts[1]
            self.set_season(self.pick_season())
        elif c == "stick" and len(parts) == 2:
            state["stick"] = max(0.0, min(1.0, float(parts[1])))
        elif c == "floor":
            state["floor"] = not state.get("floor", True)
            self.floor = None
        elif c == "ledges":
            state["ledges"] = not state.get("ledges", True)
            self.watch_wallpaper()
        elif c == "rescan":
            self.wp_path = None
            self.wp_key = None
            self.watch_wallpaper()
            return "rescanning"
        elif c == "pause":
            self.paused = not self.paused
            return "paused" if self.paused else "snowing"
        elif c == "clear":
            for p in self.piles.values():
                self.shake(p)
                p.h[:] = 0
                p.dirty = True
            for p in self.ledges:
                p.h[:] = 0
                p.dirty = True
            if self.floor is not None:
                self.floor.h[:] = 0
                self.floor.dirty = True
        elif c == "quit":
            self.plat.quit()
            return "bye"
        else:
            return "unknown command"
        save_state(state)
        return "ok"

