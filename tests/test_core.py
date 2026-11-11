"""Headless tests for the shared simulation (no window, no GPU)."""
import os
import sys
import tempfile

import numpy as np
import pytest

os.environ.setdefault("XDG_CONFIG_HOME", tempfile.mkdtemp())   # don't touch the real config
os.environ.setdefault("APPDATA", os.environ["XDG_CONFIG_HOME"])
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from seasoning import core  # noqa: E402


class FakePlatform:
    wallpaper_settle_ms = 0

    def __init__(self, windows=None, cursor=None, W=1920, H=1080):
        self.W, self.H = W, H
        self.windows = windows if windows is not None else [
            {"addr": "A", "x": 20, "y": 72, "w": 1880, "h": 912}]
        self.cur = cursor
        self.visible = True

    def snapshot(self):
        return {"origin": (0, 0), "size": (self.W, self.H), "refresh": 60.0, "ws": 1,
                "windows": self.windows, "alive": {w["addr"] for w in self.windows}, "hide": False}

    def decoration(self):
        return (12.0, 2.0)

    def ws_anim(self):
        return ("none", 0.0, lambda t: 1.0)

    def slide_direction(self, a, b):
        return -1

    def active_ws(self):
        return None

    def cursor(self):
        return self.cur

    def wallpaper(self):
        return None

    def bar_mask(self, path, sim):
        return None

    def set_visible(self, v):
        self.visible = v

    def later(self, ms, fn):
        fn()

    def idle(self, fn):
        fn()

    def quit(self):
        pass


def make_sim(**kw):
    core.state.update(core.DEFAULTS)
    core.state.update(kw.pop("state", {}))
    plat = FakePlatform(**kw)
    sim = core.Sim(plat)
    sim.poll()
    return sim


def run(sim, seconds, dt=1 / 36):
    for i in range(int(seconds / dt)):
        sim.step(dt)
        if i % 2 == 0:
            sim.poll()


def test_population_reaches_target():
    sim = make_sim(state={"season": "winter"})
    run(sim, 20)
    target = core.BASE_FLAKES
    assert 0.6 * target < sim.alive.sum() < 1.3 * target


def test_snow_piles_on_window_top():
    sim = make_sim(state={"season": "winter", "intensity": 2.0})
    run(sim, 40)
    p = sim.piles["A"]
    assert p.h.sum() > 500                      # something accumulated
    assert p.h.max() <= p.maxh + 1e-6           # never above the cap
    # rounded corners: the floor of the pile drops at both ends
    assert p.base[0] > p.base[p.n // 2] and p.base[-1] > p.base[p.n // 2]


def test_density_even_under_wind():
    """Regression: side-entering flakes once made the upwind edge 2x as dense."""
    sim = make_sim(state={"season": "winter", "wind_deg": 150.0, "wind": 0.3})
    run(sim, 30)
    hist = np.zeros(8)
    for _ in range(40):
        run(sim, 0.5)
        x = sim.x[sim.alive]
        hist += np.histogram(x, bins=8, range=(0, sim.W))[0]
    rel = hist / hist.mean()
    assert rel.max() < 1.6, rel
    assert rel.min() > 0.55, rel


def test_knocked_off_snow_does_not_reland_on_same_window():
    sim = make_sim(state={"season": "winter", "intensity": 2.0})
    run(sim, 40)
    core.state["intensity"] = 0.0
    run(sim, 30)                                 # let the fresh snow already in the air land
    p = sim.piles["A"]
    before = p.h.sum()
    assert before > 500
    sim.shake(p)
    p.h[:] = 0
    run(sim, 8)                                  # let every loose flake fall
    assert sim.piles["A"].h.sum() < 0.02 * before


def test_cursor_plow_carves_a_trench():
    sim = make_sim(state={"season": "winter", "intensity": 2.0})
    run(sim, 40)
    core.state["intensity"] = 0.0
    p = sim.piles["A"]
    mid = slice(p.n // 3, 2 * p.n // 3)
    before = p.h[mid].mean()
    # sweep the arrow tip just above the pile, left to right
    for x in np.linspace(sim.W / 3, 2 * sim.W / 3, 60):
        sim.plat.cur = (float(x), 72 - 14)
        sim.step(1 / 36)
    assert p.h[mid].mean() < 0.6 * before


@pytest.mark.parametrize("season", ["winter", "spring", "summer", "fall"])
def test_seasons_render_data(season):
    sim = make_sim(state={"season": season})
    run(sim, 5)
    arr = sim.flake_arr
    assert arr is not None and arr.shape[1] == 12
    assert set(np.unique(arr[:, 9])) == {core.SEASONS[season]["shape"]}
    if core.SEASONS[season]["maxh"] == 0:        # summer: nothing piles up
        run(sim, 20)
        assert all(p.h.sum() == 0 for p in sim.piles.values())


def test_season_calendar():
    import time
    for month, want in ((1, "winter"), (4, "spring"), (7, "summer"), (10, "fall"), (12, "winter")):
        t = time.struct_time((2026, month, 15, 12, 0, 0, 0, 0, -1))
        assert core.season_now(t) == want


def test_ledges_on_a_horizon(tmp_path):
    from PIL import Image, ImageDraw
    im = Image.new("RGB", (1920, 1080), (40, 60, 120))
    d = ImageDraw.Draw(im)
    d.rectangle([0, 700, 1920, 1080], fill=(30, 30, 30))        # dark ground under a blue sky
    d.rectangle([600, 400, 900, 700], fill=(200, 190, 170))      # a building with a flat roof
    path = tmp_path / "wall.png"
    im.save(path)
    ls = core.detect_ledges(str(path), 1920, 1080)
    ys = [float(np.median(y)) for _, y in ls]
    assert any(abs(y - 700) < 10 for y in ys), ys                # the horizon
    assert any(abs(y - 400) < 10 for y in ys), ys                # the roof


def test_flat_wallpaper_has_no_ledges(tmp_path):
    from PIL import Image
    path = tmp_path / "flat.png"
    Image.new("RGB", (1920, 1080), (90, 90, 100)).save(path)
    assert core.detect_ledges(str(path), 1920, 1080) == []


def test_bezier_ease():
    e = core.bezier_ease((0.1, 1.0), (0.0, 1.0))
    vals = [e(t / 20) for t in range(21)]
    assert vals[0] == 0.0 and vals[-1] == 1.0
    assert all(b >= a - 1e-9 for a, b in zip(vals, vals[1:]))


def test_browser_page_mask():
    """A tab the extension announced hides desktop particles over that window's page area."""
    import time
    wins = [{"addr": "B", "x": 100, "y": 50, "w": 1200, "h": 800, "title": "My Page — Zen Browser"},
            {"addr": "T", "x": 1350, "y": 50, "w": 500, "h": 400, "title": "terminal"}]
    sim = make_sim(windows=wins, state={"season": "winter"})
    assert sim.masks == []
    now = time.monotonic()
    sim.pages = {"My Page": {"exp": now + 5, "top": 90.0, "left": 0.0, "k": 1.0, "polled": now}}
    sim.poll()
    assert sim.masks == [(100, 50 + 90.0, 1200, 800 - 90.0)]
    sim.pages["My Page"]["polled"] = now - 5                     # tab stopped taking hand-offs
    sim.poll()
    assert sim.masks == []
    sim.pages = {"My Page": {"exp": now - 1, "top": 90.0, "left": 0.0, "k": 1.0, "polled": now}}
    sim.poll()                                                   # announcement expired
    assert sim.masks == []


def test_particles_hand_off_to_page_and_back():
    """A particle drifting into an animated page is passed over, not hidden; returns come back."""
    import time
    wins = [{"addr": "B", "x": 0, "y": 300, "w": 1920, "h": 780, "title": "Pg — Browser"}]
    sim = make_sim(windows=wins, state={"season": "fall", "intensity": 2.0})
    now = time.monotonic()
    sim.pages = {"Pg": {"exp": now + 60, "top": 40.0, "left": 0.0, "k": 1.0, "polled": now}}
    sim.poll()
    got = []
    for _ in range(36 * 8):
        sim.step(1 / 36)
        sim.pages["Pg"]["polled"] = time.monotonic()
        with sim.hand_lock:
            got += sim.handoff.pop("Pg", [])
    assert len(got) > 20
    over_page = sim.alive & (sim.y > 345) & (sim.y < 1080) & (sim.x >= 0) & (sim.x < 1920)
    assert not over_page.any()                                   # nothing left drawn over the page
    p = got[-1]                                                  # (the first ones are the initial fill)
    assert {"x", "y", "d", "r", "ph", "fr", "sw", "ang", "col"} <= set(p)
    assert -1 <= p["y"] <= 40                                    # handed over right at the page's top
    n0 = int(sim.alive.sum())
    back = dict(p, x=-5.0, y=200.0)                              # left the page through its left side
    with sim.hand_lock:
        sim.handback.append(("Pg", back))
    sim.step(1 / 36)
    assert int(sim.alive.sum()) >= n0
