"""Render README images: the real simulation over a generated scene with fake windows.

Needs an OpenGL 3.3 context through GLFW (run it on Windows, or under Wine/Xvfb).
    python tools/render_demo.py OUTDIR [season ...]
"""
import ctypes
import os
import sys

import numpy as np
from PIL import Image, ImageDraw, ImageFilter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from seasoning import core  # noqa: E402

W, H = 1600, 900
WINDOWS = [  # x, y, w, h, title
    (120, 210, 620, 420, "notes"),
    (820, 320, 560, 360, "terminal"),
    (960, 140, 360, 150, "player"),
]


def scene(path):
    """An original mountain-and-forest wallpaper (no third-party art)."""
    rs = np.random.default_rng(7)
    y = np.linspace(0, 1, H)[:, None, None]
    sky = np.array([18, 26, 52]) * (1 - y) + np.array([92, 72, 110]) * y      # H x 1 x 3
    im = Image.fromarray(np.repeat(sky, W, axis=1).astype(np.uint8))
    d = ImageDraw.Draw(im)
    for layer, (base, amp, col) in enumerate(((520, 160, (52, 48, 78)), (620, 110, (34, 34, 56)),
                                               (720, 60, (22, 24, 38)))):
        xs = np.arange(0, W + 8, 8)
        ph = rs.uniform(0, 6, 3)
        ys = base - amp * (0.55 * np.sin(xs / 210 + ph[0]) + 0.3 * np.sin(xs / 97 + ph[1])
                           + 0.15 * np.sin(xs / 41 + ph[2]))
        d.polygon([(0, H)] + list(zip(xs.tolist(), ys.tolist())) + [(W, H)], fill=col)
    d.rectangle([0, 800, W, H], fill=(14, 16, 26))
    for _ in range(140):                       # stars
        sx, sy = rs.uniform(0, W), rs.uniform(0, 380)
        d.point((sx, sy), fill=(200, 210, 255))
    im = im.filter(ImageFilter.GaussianBlur(0.6))
    im.save(path)
    return im


def draw_windows(im):
    d = ImageDraw.Draw(im, "RGBA")
    for x, y, w, h, title in WINDOWS:
        d.rounded_rectangle([x + 4, y + 8, x + w + 4, y + h + 10], 12, fill=(0, 0, 0, 90))
        d.rounded_rectangle([x, y, x + w, y + h], 12, fill=(24, 27, 36, 255), outline=(70, 80, 105, 255), width=2)
        d.rounded_rectangle([x + 2, y + 2, x + w - 2, y + 34], 10, fill=(34, 38, 50, 255))
        d.text((x + 16, y + 11), title, fill=(190, 200, 220, 255))
        for i in range(3):
            d.ellipse([x + w - 70 + i * 20, y + 13, x + w - 60 + i * 20, y + 23], fill=(90, 100, 125, 255))
        for i in range(min(8, (h - 60) // 28)):
            d.rounded_rectangle([x + 18, y + 54 + i * 28, x + 18 + (w - 60) * (0.4 + 0.5 * ((i * 37) % 10) / 10),
                                 y + 66 + i * 28], 5, fill=(46, 52, 68, 255))
    return im


class DemoPlatform:
    wallpaper_settle_ms = 0

    def __init__(self, wall):
        self.wall = wall

    def snapshot(self):
        wins = [{"addr": t, "x": x, "y": y, "w": w, "h": h} for x, y, w, h, t in reversed(WINDOWS)]
        return {"origin": (0, 0), "size": (W, H), "ws": 0, "windows": wins,
                "alive": {w["addr"] for w in wins}, "hide": False}

    def decoration(self): return (12.0, 2.0)
    def ws_anim(self): return ("none", 0.0, lambda t: 1.0)
    def slide_direction(self, a, b): return -1
    def active_ws(self): return None
    def cursor(self): return None
    def wallpaper(self): return self.wall
    def bar_mask(self, path, sim): return None
    def set_visible(self, v): pass
    def later(self, ms, fn): fn()
    def idle(self, fn): fn()
    def quit(self): pass


def main():
    out = sys.argv[1]
    seasons = sys.argv[2:] or ["winter", "fall", "spring", "summer"]
    os.makedirs(out, exist_ok=True)
    import glfw
    assert glfw.init()
    for k, v in ((glfw.CONTEXT_VERSION_MAJOR, 3), (glfw.CONTEXT_VERSION_MINOR, 3),
                 (glfw.OPENGL_PROFILE, glfw.OPENGL_CORE_PROFILE), (glfw.OPENGL_FORWARD_COMPAT, True),
                 (glfw.TRANSPARENT_FRAMEBUFFER, True), (glfw.DECORATED, False)):
        glfw.window_hint(k, v)
    win = glfw.create_window(W, H, "demo", None, None)
    glfw.make_context_current(win)
    core.init_gl(lambda n: glfw.get_proc_address(n))
    wall_path = os.path.join(out, "_wallpaper.png")
    base = draw_windows(scene(wall_path).convert("RGBA"))
    for season in seasons:
        core.state.update(core.DEFAULTS)
        core.state.update({"season": season, "intensity": 1.6, "wind_deg": 100.0, "wind": 0.12,
                           "wallpaper": wall_path})
        sim = core.Sim(DemoPlatform(wall_path))
        sim.gl = core.Renderer(es=False)
        sim.poll()
        sim.watch_wallpaper()
        for i in range(36 * 75):                   # 75 simulated seconds: piles build up
            sim.step(1 / 36)
            if i % 2 == 0:
                sim.poll()
        sim.step(1 / 36)
        r = sim.gl
        r.upload_flakes(sim.flake_arr)
        pb = sim.pile_batch()
        if pb is not None:
            r.upload_piles(*pb)
        core.GL.Viewport(0, 0, W, H)
        r.draw(float(W), float(H), 1.0, 1.0, core.SEASONS[sim.season]["shape"])
        buf = (ctypes.c_ubyte * (W * H * 4))()
        core.GL.ReadPixels(0, 0, W, H, 0x1908, 0x1401, buf)      # RGBA, UNSIGNED_BYTE
        rgba = np.frombuffer(buf, np.uint8).reshape(H, W, 4)[::-1].astype(np.float32) / 255
        # premultiplied -> composite over the scene
        bg = np.asarray(base, np.float32)[..., :3] / 255
        comp = rgba[..., :3] + bg * (1 - rgba[..., 3:4])
        Image.fromarray((comp * 255).clip(0, 255).astype(np.uint8)).save(os.path.join(out, f"{season}.png"))
        print(season, "ledges", len(sim.ledges), "piles", {k: round(float(p.h.sum())) for k, p in sim.piles.items()})
    glfw.terminate()


if __name__ == "__main__":
    main()
