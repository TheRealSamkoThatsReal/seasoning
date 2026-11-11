// seasoning for web pages: the season falls over the page and piles up on its elements.
// Standalone it draws everything itself (ChromeOS, or no desktop app); with the
// seasoning desktop app running it takes the app's weather and the app hands the
// page area over to it.
(() => {
  "use strict";
  if (window.top !== window || window.__seasoning) return;
  window.__seasoning = true;

  // ── tuning (mirrors the desktop core) ──────────────────────────────────
  const COL = 3;                 // px per height-map column
  const FALL = 70;               // px/s, mid-depth
  const MAX_WIND = 360;
  const BASE = 150;              // particles on screen at amount 1 (a page is smaller than a desktop)
  const MAXH = 16;               // pile cap on elements
  const REPOSE = 0.9, COHESION = 5.0, EDGE = 2.4, SPILL = 10;
  const PLOW_R = 11, WAKE_R = 70;
  const MAX_SURFACES = 60, CAP = 900;

  const SEASONS = {
    winter: { fall: 1.0, sway: 1.0, wind: 1.0, size: 1.0, count: 1.0, mass: 1.0, maxh: 1.0, shape: 0, spin: 0 },
    fall:   { fall: 0.55, sway: 3.2, wind: 1.5, size: 3.0, count: 0.5, mass: 0.45, maxh: 0.55, shape: 1, spin: 2.6 },
    spring: { fall: 0.42, sway: 2.6, wind: 1.3, size: 1.5, count: 0.7, mass: 0.35, maxh: 0.3, shape: 2, spin: 2.0 },
    summer: { fall: 0.16, sway: 2.2, wind: 1.2, size: 2.4, count: 0.3, mass: 0.0, maxh: 0.0, shape: 3, spin: 0.6 },
  };
  const COLORS = {
    winter: ["#ffffff"],
    fall: ["#db5c17", "#bd2912", "#eba324", "#85451a", "#cc7a1f"],
    spring: ["#ffc7db", "#faa8c7", "#ffe6f0"],
    summer: ["#fffff5"],
  };
  const LITTER = {
    fall: ["#cc5417", "#a82612", "#db9421", "#733d1a"],
    spring: ["#ffc7db", "#f7a3c2", "#ffebf2"],
  };

  function seasonNow(d = new Date()) {
    const m = d.getMonth() + 1;
    return m === 12 || m <= 2 ? "winter" : m <= 5 ? "spring" : m <= 8 ? "summer" : "fall";
  }

  // ── settings (storage) + companion overrides (desktop app) ──────────────
  const api = globalThis.browser ?? globalThis.chrome;
  let cfg = { enabled: true, season: "auto", intensity: 1.0, wind_deg: 90, wind: 0, stick: 0.35,
              piles: true, companion: true, disabledSites: [] };
  let companion = null;          // desktop app state when it's running
  const S = () => SEASONS[season()];
  const season = () => {
    const s = companion ? companion.season : cfg.season;
    return SEASONS[s] ? s : seasonNow();
  };
  const amount = () => (companion ? companion.intensity : cfg.intensity);
  const paused = () => !!(companion && (companion.paused || companion.hidden));
  function wind() {
    const src = companion || cfg;
    const a = (src.wind_deg * Math.PI) / 180, s = src.wind;
    return [Math.cos(a) * s * MAX_WIND, Math.sin(a) * s * MAX_WIND * 0.35];
  }
  const siteOff = () => cfg.disabledSites.includes(location.hostname);
  const active = () => cfg.enabled && !siteOff();

  // ── canvas ─────────────────────────────────────────────────────────────
  const cv = document.createElement("canvas");
  cv.setAttribute("aria-hidden", "true");
  cv.style.cssText = "position:fixed;inset:0;width:100vw;height:100vh;pointer-events:none;" +
    "z-index:2147483647;display:block;contain:strict;";
  const ctx = cv.getContext("2d");
  let W = 0, H = 0, DPR = 1;
  function resize() {
    DPR = window.devicePixelRatio || 1;
    W = innerWidth; H = innerHeight;
    cv.width = Math.round(W * DPR); cv.height = Math.round(H * DPR);
  }

  // ── sprites ────────────────────────────────────────────────────────────
  const sprites = {};
  function sprite(key, size, draw) {
    if (sprites[key]) return sprites[key];
    const c = document.createElement("canvas");
    c.width = c.height = size;
    draw(c.getContext("2d"), size);
    return (sprites[key] = c);
  }
  function spriteFor(shape, color) {
    if (shape === 0) return sprite("snow", 32, (g, n) => {
      const r = g.createRadialGradient(n / 2, n / 2, 0, n / 2, n / 2, n / 2);
      r.addColorStop(0, "rgba(255,255,255,1)"); r.addColorStop(0.45, "rgba(255,255,255,.92)");
      r.addColorStop(1, "rgba(235,245,255,0)");
      g.fillStyle = r; g.fillRect(0, 0, n, n);
    });
    if (shape === 1) return sprite("leaf" + color, 48, (g, n) => {
      // vesica leaf with a midrib and a stem, lit from one side
      g.translate(n / 2, n / 2);
      g.beginPath();
      g.moveTo(0, -n * 0.42);
      g.quadraticCurveTo(n * 0.34, -n * 0.05, 0, n * 0.36);
      g.quadraticCurveTo(-n * 0.34, -n * 0.05, 0, -n * 0.42);
      const lg = g.createLinearGradient(-n * 0.3, 0, n * 0.3, 0);
      lg.addColorStop(0, shade(color, 0.8)); lg.addColorStop(1, shade(color, 1.12));
      g.fillStyle = lg; g.fill();
      g.strokeStyle = shade(color, 0.62); g.lineWidth = n * 0.035;
      g.beginPath(); g.moveTo(0, -n * 0.38); g.lineTo(0, n * 0.46); g.stroke();
    });
    if (shape === 2) return sprite("petal" + color, 32, (g, n) => {
      g.translate(n / 2, n / 2);
      g.beginPath(); g.ellipse(0, 0, n * 0.26, n * 0.4, 0, 0, Math.PI * 2);
      const lg = g.createLinearGradient(0, n * 0.4, 0, -n * 0.4);
      lg.addColorStop(0, color); lg.addColorStop(1, "#ffffff");
      g.fillStyle = lg; g.fill();
      g.globalCompositeOperation = "destination-out";
      g.beginPath(); g.arc(0, -n * 0.44, n * 0.12, 0, Math.PI * 2); g.fill();
    });
    return sprite("fluff", 40, (g, n) => {
      g.translate(n / 2, n / 2);
      g.strokeStyle = "rgba(255,255,250,.75)"; g.lineWidth = 1;
      for (let k = 0; k < 9; k++) {
        const a = (k / 9) * Math.PI * 2;
        g.beginPath(); g.moveTo(Math.cos(a) * n * 0.08, Math.sin(a) * n * 0.08);
        g.lineTo(Math.cos(a) * n * 0.46, Math.sin(a) * n * 0.46); g.stroke();
      }
      const r = g.createRadialGradient(0, 0, 0, 0, 0, n * 0.16);
      r.addColorStop(0, "rgba(255,255,255,1)"); r.addColorStop(1, "rgba(255,255,255,0)");
      g.fillStyle = r; g.beginPath(); g.arc(0, 0, n * 0.16, 0, Math.PI * 2); g.fill();
    });
  }
  function shade(hex, k) {
    const v = parseInt(hex.slice(1), 16);
    const c = (x) => Math.max(0, Math.min(255, Math.round(x * k)));
    return `rgb(${c(v >> 16)},${c((v >> 8) & 255)},${c(v & 255)})`;
  }

  // ── particles (viewport coordinates) ──────────────────────────────────
  const P = {
    x: new Float32Array(CAP), y: new Float32Array(CAP), d: new Float32Array(CAP), r: new Float32Array(CAP),
    ph: new Float32Array(CAP), ph2: new Float32Array(CAP), fr: new Float32Array(CAP), sw: new Float32Array(CAP),
    vx: new Float32Array(CAP), vy: new Float32Array(CAP), ang: new Float32Array(CAP), spin: new Float32Array(CAP),
    col: new Array(CAP).fill("#ffffff"), alive: new Uint8Array(CAP), near: new Uint8Array(CAP),
    sticky: new Uint8Array(CAP), origin: new Array(CAP).fill(null),
  };
  let t = 0, spawnAcc = 0, prefill = true;
  const rnd = (a, b) => a + Math.random() * (b - a);
  function beta22() { const u = Math.random(), v = Math.random(), w = Math.random(); return [u, v, w].sort()[1]; }

  function style(i) {
    const s = S(), name = season(), pal = COLORS[name];
    P.col[i] = pal[(Math.random() * pal.length) | 0];
    P.r[i] = (0.9 + P.d[i] * 2.8 + rnd(-0.3, 0.3)) * s.size;
    P.spin[i] = rnd(-1, 1) * s.spin;
    P.ang[i] = rnd(0, 6.283);
  }
  function free() { for (let i = 0; i < CAP; i++) if (!P.alive[i]) return i; return -1; }
  function spawn(k, anywhere) {
    const [wx, wy] = wind(), s = S();
    for (let n = 0; n < k; n++) {
      const i = free(); if (i < 0) return;
      P.d[i] = beta22();
      P.x[i] = rnd(-20, W + 20);
      P.y[i] = anywhere ? rnd(-30, H) : rnd(-30, -4);
      if (!anywhere && Math.abs(wx) > 1) {
        const vxe = Math.abs(wx) * s.wind * 0.9, vye = Math.max(FALL * s.fall * 0.9 + wy, 6);
        if (Math.random() < (vxe * H) / (vxe * H + vye * W)) {
          P.x[i] = wx > 0 ? -15 : W + 15; P.y[i] = rnd(-20, H * 0.9);
        }
      }
      P.near[i] = P.d[i] > 0.35 && s.maxh > 0 ? 1 : 0;
      P.sticky[i] = Math.random() < (companion ? companion.stick : cfg.stick) ? 1 : 0;
      P.origin[i] = null;
      P.ph[i] = rnd(0, 6.283); P.ph2[i] = rnd(0, 6.283); P.fr[i] = rnd(0.35, 0.9); P.sw[i] = rnd(3, 10);
      P.vx[i] = P.vy[i] = 0;
      style(i);
      P.alive[i] = 1;
    }
  }
  function loose(x, y, vx, vy, origin) {   // a particle knocked off a pile
    const i = free(); if (i < 0) return;
    P.d[i] = rnd(0.6, 1); P.x[i] = x; P.y[i] = y; P.vx[i] = vx; P.vy[i] = vy;
    P.near[i] = 1; P.sticky[i] = 1; P.origin[i] = origin;
    P.ph[i] = rnd(0, 6.283); P.ph2[i] = rnd(0, 6.283); P.fr[i] = rnd(0.35, 0.9); P.sw[i] = rnd(3, 10);
    style(i); P.r[i] = rnd(1.1, 2.4) * S().size;
    P.alive[i] = 1;
  }

  // ── piles on page elements (document coordinates unless fixed) ───────────
  class Pile {
    constructor(el, x0, top, w, fixed, radii) {
      this.el = el; this.x0 = x0; this.top = top; this.w = w; this.fixed = fixed;
      this.n = Math.max(1, Math.floor(w / COL));
      this.h = new Float32Array(this.n); this.fresh = new Float32Array(this.n);
      this.vis = new Uint8Array(this.n).fill(1);
      this.base = new Float32Array(this.n);
      const [rl, rr] = radii;
      for (let c = 0; c < this.n; c++) {      // follow the element's rounded corners
        const x = (c + 0.5) * COL, dl = x, dr = w - x;
        let drop = 0;
        if (dl < rl) drop = rl - Math.sqrt(Math.max(0, rl * rl - (rl - dl) ** 2));
        if (dr < rr) drop = Math.max(drop, rr - Math.sqrt(Math.max(0, rr * rr - (rr - dr) ** 2)));
        this.base[c] = drop;
      }
      this.cap = new Float32Array(this.n);
      let wob = 0;
      for (let c = 0; c < this.n; c++) {      // undulating cap: drifts, not a slab
        wob += rnd(-1, 1); wob *= 0.97;
        this.cap[c] = MAXH * S().maxh * (0.8 + 0.2 * Math.max(-1, Math.min(1, wob / 6)));
      }
      this.spillL = this.spillR = 0;
    }
    screenTop() { return this.fixed ? this.top : this.top - scrollY; }
    screenX() { return this.fixed ? this.x0 : this.x0 - scrollX; }
    deposit(c, amt, packed) {
      for (const [o, k] of [[-2, 0.1], [-1, 0.2], [0, 0.4], [1, 0.2], [2, 0.1]]) {
        const j = c + o;
        if (j < 0) { this.spill(amt * k, -1); continue; }
        if (j >= this.n) { this.spill(amt * k, 1); continue; }
        this.h[j] = Math.min(this.h[j] + amt * k, this.cap[j]);
        if (!packed) this.fresh[j] = 0.8;
      }
    }
    spill(a, side) {
      if (side < 0) { this.spillL += a; while (this.spillL >= SPILL) { this.spillL -= SPILL; spills.push([this, -1]); } }
      else { this.spillR += a; while (this.spillR >= SPILL) { this.spillR -= SPILL; spills.push([this, 1]); } }
    }
    settle(dt, wx) {
      const h = this.h, n = this.n;
      let any = false;
      for (let c = 0; c < n; c++) if (h[c] > 0.01) { any = true; break; }
      if (!any) return;
      const fw = Math.min(0.5, (Math.abs(wx) / MAX_WIND) * 1.4 * dt);
      if (fw > 0.0005) {                   // wind drifts the loose top layer
        if (wx > 0) { for (let c = n - 1; c >= 0; c--) { const m = Math.max(h[c] - 1, 0) * fw; h[c] -= m; if (c + 1 < n) h[c + 1] += m; else this.spill(m, 1); } }
        else { for (let c = 0; c < n; c++) { const m = Math.max(h[c] - 1, 0) * fw; h[c] -= m; if (c > 0) h[c - 1] += m; else this.spill(m, -1); } }
      }
      for (let pass = 0; pass < 6; pass++) {
        for (let c = 0; c + 1 < n; c++) {
          const d = (h[c + 1] - this.base[c + 1]) - (h[c] - this.base[c]);
          const R = this.fresh[c] > 0 || this.fresh[c + 1] > 0 ? REPOSE : COHESION;
          const ex = Math.abs(d) - R;
          if (ex <= 0) continue;
          // snow that slides stays loose, so a landing keeps slumping until it rests
          if (d > 0) { const f = Math.min(ex * 0.4, h[c + 1] * 0.5); h[c + 1] -= f; h[c] += f; this.fresh[c] = Math.max(this.fresh[c], this.fresh[c + 1]); }
          else { const f = Math.min(ex * 0.4, h[c] * 0.5); h[c] -= f; h[c + 1] += f; this.fresh[c + 1] = Math.max(this.fresh[c + 1], this.fresh[c]); }
        }
        if (h[0] > EDGE) { const x = (h[0] - EDGE) * 0.3; h[0] -= x; this.spill(x, -1); }
        if (h[n - 1] > EDGE) { const x = (h[n - 1] - EDGE) * 0.3; h[n - 1] -= x; this.spill(x, 1); }
      }
      for (let c = 0; c < n; c++) { if (h[c] > this.cap[c]) h[c] = this.cap[c]; if (this.fresh[c] > 0) this.fresh[c] -= dt; }
    }
    mass() { let m = 0; for (let c = 0; c < this.n; c++) m += this.h[c]; return m; }
    shake() {                               // the element moved or went away: it all falls
      const sx = this.screenX(), sy = this.screenTop();
      for (let c = 0; c < this.n; c += 2) {
        if (this.h[c] < 1) continue;
        const k = Math.min(3, 1 + (this.h[c] / 4) | 0);
        for (let q = 0; q < k; q++) loose(sx + c * COL + rnd(0, COL), sy + this.base[c] - rnd(0, this.h[c]),
                                          rnd(-25, 25), rnd(-40, 20), this.el);
      }
      this.h.fill(0);
    }
  }
  const piles = new Map();       // element -> Pile
  const spills = [];

  // which elements look like solid boxes snow could sit on
  const SOLID_TAGS = new Set(["IMG", "VIDEO", "CANVAS", "SVG", "IFRAME", "BUTTON", "INPUT", "TEXTAREA",
                              "SELECT", "PICTURE", "EMBED", "OBJECT"]);
  function alpha(c) {
    if (!c || c === "transparent") return 0;
    const m = c.match(/rgba?\(([^)]+)\)/);
    if (!m) return 1;
    const p = m[1].split(/[ ,/]+/).filter(Boolean);
    return p.length > 3 ? parseFloat(p[3]) : 1;
  }
  function isFixed(el) {
    for (let e = el; e && e !== document.body; e = e.parentElement) {
      const p = getComputedStyle(e).position;
      if (p === "fixed" || p === "sticky") return true;
    }
    return false;
  }
  function visibleTop(el, r) {
    // hit-test along the top edge: only the parts not covered by something else count
    const y = r.top + 2;
    // an edge flush with the top of the window (fixed headers) would catch everything
    // falling in from above while its snow is drawn off-screen: not a surface
    if (r.top < 6 || y > H) return null;
    const n = Math.max(1, Math.floor(r.width / COL));
    const vis = new Uint8Array(n);
    const step = Math.max(24, r.width / 24), cols = Math.ceil(r.width / step);   // ≤25 hit-tests per edge
    let anyVis = false;
    for (let k = 0; k <= cols; k++) {
      const x = Math.min(r.left + k * step + 1, r.right - 1);
      if (x < 0 || x > W) continue;
      const hit = document.elementFromPoint(x, y);
      const ok = hit && (hit === el || el.contains(hit));
      if (ok) {
        anyVis = true;
        const c0 = Math.floor((k * step) / COL), c1 = Math.min(n, Math.ceil(((k + 1) * step) / COL));
        for (let c = Math.max(0, c0 - 3); c < c1; c++) vis[c] = 1;
      }
    }
    return anyVis ? vis : null;
  }
  function scanSurfaces() {
    if (!document.body || !active() || S().maxh <= 0 || !cfg.piles) {
      for (const p of piles.values()) if (p.mass() > 0) p.shake();
      piles.clear();
      return;
    }
    const found = [];
    const all = document.body.getElementsByTagName("*");
    const lim = Math.min(all.length, 5000);
    for (let i = 0; i < lim; i++) {
      const el = all[i];
      if (el === cv) continue;
      const r = el.getBoundingClientRect();
      if (r.width < 60 || r.height < 16 || r.bottom < -100 || r.top > H + 50 || r.right < 0 || r.left > W) continue;
      if (r.width >= W - 4 && r.height >= H * 0.85) continue;          // page-sized wrappers
      const cs = getComputedStyle(el);
      if (cs.visibility === "hidden" || parseFloat(cs.opacity) < 0.3) continue;
      const solid = SOLID_TAGS.has(el.tagName.toUpperCase()) || alpha(cs.backgroundColor) > 0.15 ||
        cs.backgroundImage !== "none" ||
        (parseFloat(cs.borderTopWidth) >= 1 && alpha(cs.borderTopColor) > 0.25);
      if (!solid) continue;
      found.push([el, r, cs]);
    }
    // prefer prominent, near-the-top boxes; drop near-duplicates of an outer box's top edge
    found.sort((a, b) => a[1].top - b[1].top || b[1].width - a[1].width);
    const keep = [];
    for (const f of found) {
      const r = f[1];
      if (keep.some(([, k]) => Math.abs(k.top - r.top) < 3 && r.left >= k.left - 2 && r.right <= k.right + 2)) continue;
      keep.push(f);
      if (keep.length >= MAX_SURFACES) break;
    }
    const seen = new Set();
    for (const [el, r, cs] of keep) {
      const vis = visibleTop(el, r);
      if (!vis) continue;
      const fixed = isFixed(el);
      const x0 = fixed ? r.left : r.left + scrollX, top = fixed ? r.top : r.top + scrollY;
      let p = piles.get(el);
      if (p && (Math.abs(p.x0 - x0) > 1 || Math.abs(p.top - top) > 1 || Math.abs(p.w - r.width) > 1 || p.fixed !== fixed)) {
        p.shake(); p = null;
      }
      if (!p) {
        p = new Pile(el, x0, top, r.width, fixed,
          [Math.min(parseFloat(cs.borderTopLeftRadius) || 0, 40), Math.min(parseFloat(cs.borderTopRightRadius) || 0, 40)]);
        piles.set(el, p);
      }
      p.vis = vis.length === p.n ? vis : p.vis;
      seen.add(el);
    }
    for (const [el, p] of piles) {
      if (seen.has(el)) continue;
      // scrolled out of view: keep it if the element is still there, else it falls off
      if (el.isConnected) {
        const r = el.getBoundingClientRect();
        if (r.bottom < 0 || r.top > H) continue;
      }
      if (p.mass() > 0) p.shake();
      piles.delete(el);
    }
  }

  // ── cursor ─────────────────────────────────────────────────────────────
  let cur = null, curPrev = null, curV = [0, 0], curT = 0;
  addEventListener("pointermove", (e) => {
    const now = performance.now();
    if (cur) {
      const dt = Math.max((now - curT) / 1000, 1e-3);
      curV = [0.5 * curV[0] + 0.5 * (e.clientX - cur[0]) / dt, 0.5 * curV[1] + 0.5 * (e.clientY - cur[1]) / dt];
    }
    cur = [e.clientX, e.clientY]; curT = now;
  }, { passive: true });

  function cursorStep() {
    if (!cur) return;
    const prev = curPrev || cur;
    curPrev = cur;
    const speed = Math.hypot(curV[0], curV[1]);
    if (performance.now() - curT > 120) { curV = [curV[0] * 0.5, curV[1] * 0.5]; }
    if (speed < 25) return;
    for (let i = 0; i < CAP; i++) {          // wake through falling particles
      if (!P.alive[i]) continue;
      const dx = P.x[i] - cur[0], dy = P.y[i] - cur[1], dist = Math.hypot(dx, dy);
      if (dist >= WAKE_R) continue;
      const f = (1 - dist / WAKE_R) ** 2, push = Math.min(speed, 1500) * 0.35, k = Math.min(f * 0.6, 1);
      P.vx[i] += (curV[0] * 0.7 + (dx / (dist + 1e-3)) * push - P.vx[i]) * k;
      P.vy[i] += (curV[1] * 0.7 + (dy / (dist + 1e-3)) * push - P.vy[i]) * k;
    }
    const steps = Math.max(1, Math.floor(Math.hypot(cur[0] - prev[0], cur[1] - prev[1]) / 6));
    for (let s = 1; s <= steps; s++) {        // plow piles with the arrow's body
      const cx = prev[0] + (cur[0] - prev[0]) * (s / steps) + 6, cy = prev[1] + (cur[1] - prev[1]) * (s / steps) + 16;
      for (const p of piles.values()) plow(p, cx, cy);
    }
  }
  function plow(p, cx, cy) {
    const sx = p.screenX(), sy = p.screenTop();
    if (cx < sx - PLOW_R || cx > sx + p.w + PLOW_R || cy < sy - MAXH - 6 || cy > sy + 30) return;
    const c0 = Math.max(0, Math.floor((cx - PLOW_R - sx) / COL)), c1 = Math.min(p.n, Math.floor((cx + PLOW_R - sx) / COL) + 1);
    let m = 0;
    for (let c = c0; c < c1; c++) {
      if (!p.vis[c]) continue;
      const x = sx + (c + 0.5) * COL, fall = Math.max(0, 1 - ((x - cx) / PLOW_R) ** 2);
      const keep = Math.max(0, sy + p.base[c] - cy);
      const cut = Math.max(0, p.h[c] - keep) * fall;
      p.h[c] -= cut; p.fresh[c] = 0; m += cut;
    }
    if (m < 0.2) return;
    const sp = Math.hypot(curV[0], curV[1]) + 1e-3, horiz = Math.abs(curV[0]) / sp, shove = m * 0.65 * horiz;
    if (shove > 0) {
      const side = curV[0] > 0 ? 1 : -1, ahead = Math.round((cx + side * (PLOW_R + 4) - sx) / COL);
      for (let k = 0; k < 5; k++) p.deposit(ahead + side * k, shove / 5, true);
    }
    p.kick = (p.kick || 0) + (m - shove);
    while (p.kick >= 6.5) {
      p.kick -= 6.5;
      loose(cx + rnd(-PLOW_R, PLOW_R), Math.min(cy, sy) - rnd(2, 8), curV[0] * rnd(0.3, 0.8), -Math.abs(curV[1]) * 0.3 - rnd(60, 160), p.el);
    }
  }

  // ── simulation ──────────────────────────────────────────────────────────
  let frame = 0;
  function step(dt) {
    t += dt; frame++;
    const s = S(), [wx, wy] = wind();
    const target = Math.round(BASE * amount() * s.count);
    if (prefill) { spawn(target, true); prefill = false; }
    if (!paused()) {
      spawnAcc += ((1.8 * target * FALL * s.fall) / (H + 60)) * dt;
      let live = 0; for (let i = 0; i < CAP; i++) live += P.alive[i];
      if (live < target && spawnAcc >= 1) { const k = Math.min(spawnAcc | 0, target - live); spawnAcc -= spawnAcc | 0; spawn(k, false); }
    }
    cursorStep();
    const decx = 0.97 ** (dt * 60), decy = 0.95 ** (dt * 60), list = [...piles.values()];
    for (let i = 0; i < CAP; i++) {
      if (!P.alive[i]) continue;
      const d = P.d[i];
      let fall = FALL * s.fall * (0.45 + 0.9 * d);
      if (s.shape === 1 || s.shape === 2) fall *= 1 + 0.55 * Math.sin(P.ph2[i] + t * P.fr[i] * 2.9);
      const vy = Math.max(fall + wy * s.wind * (0.5 + d), 12 * s.fall) + P.vy[i];
      const sway = P.sw[i] * s.sway * (Math.sin(P.ph[i] + t * P.fr[i]) + 0.45 * Math.sin(P.ph2[i] + t * P.fr[i] * 2.37));
      const vx = wx * s.wind * (0.45 + 0.9 * d) + P.vx[i] + sway;
      P.vx[i] *= decx; P.vy[i] *= decy;
      const py = P.y[i];
      P.x[i] += vx * dt; P.y[i] += vy * dt;
      P.ang[i] += P.spin[i] * dt;
      if (P.near[i] && P.sticky[i] && s.maxh > 0) {
        for (const p of list) {
          if (p.el === P.origin[i]) continue;          // not back where it was knocked off
          const sx = p.screenX(), c = Math.floor((P.x[i] - sx) / COL);
          if (c < 0 || c >= p.n || !p.vis[c]) continue;
          const sy = p.screenTop() + p.base[c] - p.h[c];
          if (P.y[i] >= sy && py <= sy + 2.5) {
            if (p.h[c] < p.cap[c] - 0.5) {
              const amt = Math.min(3, 1 / Math.max(companion ? companion.stick : cfg.stick, 0.05)) *
                2.4 * (P.r[i] / s.size) ** 2 * s.mass;
              p.deposit(c + Math.round((wx / MAX_WIND) * 6 + rnd(-1.5, 1.5)), amt, false);
            }
            P.alive[i] = 0;
            break;
          }
        }
      }
      if (P.y[i] > H + 20 || P.y[i] < -300 || P.x[i] < -80 || P.x[i] > W + 80) P.alive[i] = 0;
    }
    for (let k = 0; k < list.length; k++) if ((k + frame) % 4 === 0) list[k].settle(dt * 4, wx);
    while (spills.length) {
      const [p, side] = spills.pop(), sy = p.screenTop() + (side < 0 ? p.base[0] : p.base[p.n - 1]) - 2;
      loose(side < 0 ? p.screenX() - 4 : p.screenX() + p.w + 4, sy, side * rnd(10, 30), -10, p.el);
    }
  }

  // ── drawing ─────────────────────────────────────────────────────────────
  function drawPile(p) {
    const sx = p.screenX(), sy = p.screenTop(), n = p.n, h = p.h;
    if (sy < -MAXH || sy > H + 10) return;
    let any = false;
    for (let c = 0; c < n; c++) if (h[c] > 0.3 && p.vis[c]) { any = true; break; }
    if (!any) return;
    const name = season();
    ctx.save();
    ctx.beginPath();                          // clip to the visible stretches of the top edge
    let run = -1;
    for (let c = 0; c <= n; c++) {
      if (c < n && p.vis[c]) { if (run < 0) run = c; continue; }
      if (run >= 0) { ctx.rect(sx + run * COL - 1, sy - MAXH - 4, (c - run) * COL + 2, MAXH + 40); run = -1; }
    }
    ctx.clip();
    const path = new Path2D();
    path.moveTo(sx, sy + p.base[0]);
    for (let c = 0; c < n; c++) {
      const hs = c > 0 && c < n - 1 ? (h[c - 1] + 2 * h[c] + h[c + 1]) / 4 : h[c];
      path.lineTo(sx + (c + 0.5) * COL, sy + p.base[c] - hs);
    }
    path.lineTo(sx + p.w, sy + p.base[n - 1]);
    for (let c = n - 1; c >= 0; c--) path.lineTo(sx + (c + 0.5) * COL, sy + p.base[c]);
    path.closePath();
    ctx.translate(0, 1.2);
    ctx.fillStyle = "rgba(13,20,38,.3)"; ctx.fill(path);
    ctx.translate(0, -1.2);
    if (LITTER[name]) {
      ctx.fillStyle = litterPattern(name); ctx.fill(path);
    } else {
      const g = ctx.createLinearGradient(0, sy - MAXH, 0, sy + 1);
      g.addColorStop(0, "rgba(255,255,255,.98)"); g.addColorStop(0.6, "rgba(242,247,255,.97)");
      g.addColorStop(1, "rgba(204,219,242,.95)");
      ctx.fillStyle = g; ctx.fill(path);
    }
    ctx.restore();
  }
  const patterns = {};
  function litterPattern(name) {
    if (patterns[name]) return patterns[name];
    const c = document.createElement("canvas"); c.width = 48; c.height = 24;
    const g = c.getContext("2d"), pal = LITTER[name];
    for (let y = 0; y < 24; y += 3) for (let x = -6; x < 48; x += 6) {
      g.fillStyle = shade(pal[(Math.random() * pal.length) | 0], rnd(0.8, 1.1));
      g.fillRect(x + ((y / 3) % 2) * 3, y, 6, 3);
    }
    return (patterns[name] = ctx.createPattern(c, "repeat"));
  }
  function draw() {
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, cv.width, cv.height);
    ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
    for (const p of piles.values()) drawPile(p);
    const s = S();
    for (let i = 0; i < CAP; i++) {
      if (!P.alive[i]) continue;
      const img = spriteFor(s.shape, P.col[i]);
      const size = P.r[i] * 3.2, a = s.shape === 0 ? 0.35 + 0.6 * P.d[i] : 0.55 + 0.45 * P.d[i];
      ctx.globalAlpha = a;
      if (s.shape === 0) {
        ctx.drawImage(img, P.x[i] - size / 2, P.y[i] - size / 2, size, size);
      } else {
        const an = P.ang[i], flip = Math.max(Math.abs(Math.cos(an * 1.7 + 0.6)), 0.32);
        const c = Math.cos(an), sn = Math.sin(an);
        ctx.setTransform(DPR * c * flip, DPR * sn * flip, -DPR * sn, DPR * c, DPR * P.x[i], DPR * P.y[i]);
        ctx.drawImage(img, -size / 2, -size / 2, size, size);
        ctx.setTransform(DPR, 0, 0, DPR, 0, 0);
      }
    }
    ctx.globalAlpha = 1;
  }

  // ── loop ────────────────────────────────────────────────────────────────
  let last = 0, lastScan = 0, running = false, curSeason = null;
  function loop(now) {
    if (!running) return;
    const dt = Math.min((now - last) / 1000, 0.1);
    last = now;
    if (season() !== curSeason) {              // season changed: everything lying around goes
      curSeason = season();
      for (const p of piles.values()) p.h.fill(0);
      piles.clear(); P.alive.fill(0); prefill = true;
    }
    if (now - lastScan > 1000) {
      lastScan = now; scanSurfaces();
      cv.dataset.stats = JSON.stringify(stats());   // readable from the page (tests, debugging)
    }
    if (dt > 0) step(dt);
    draw();
    requestAnimationFrame(loop);
  }
  function start() {
    if (running || !active()) return;
    if (!cv.isConnected) (document.body || document.documentElement).appendChild(cv);
    resize(); running = true; last = performance.now(); lastScan = 0;
    requestAnimationFrame(loop);
  }
  function stop() {
    running = false;
    ctx.clearRect(0, 0, cv.width, cv.height);
  }
  function refresh() {
    if (active() && document.visibilityState === "visible") start(); else stop();
  }
  addEventListener("resize", () => { resize(); lastScan = 0; }, { passive: true });
  addEventListener("scroll", () => { lastScan = Math.min(lastScan, performance.now() - 800); }, { passive: true });
  document.addEventListener("visibilitychange", refresh);
  // keep the canvas on top if the page rebuilds <body> (single-page apps)
  new MutationObserver(() => { if (running && !cv.isConnected) document.body?.appendChild(cv); })
    .observe(document.documentElement, { childList: true, subtree: false });

  // ── messaging: settings, companion, popup commands ────────────────────────
  api.storage.local.get(cfg).then?.((v) => { cfg = { ...cfg, ...v }; refresh(); }) ??
    api.storage.local.get(cfg, (v) => { cfg = { ...cfg, ...v }; refresh(); });
  api.storage.onChanged.addListener((ch) => {
    for (const k in ch) cfg[k] = ch[k].newValue;
    refresh();
  });
  api.runtime.onMessage.addListener((msg, _s, reply) => {
    if (msg.type === "clear") { for (const p of piles.values()) p.h.fill(0); }
    if (msg.type === "stats") reply(stats());
    return false;
  });
  function hello() {
    // tell the desktop app (through the background script) which page we're animating
    if (!cfg.companion) { if (companion) { companion = null; prefill = true; } return; }
    if (document.visibilityState !== "visible" || !active()) return;
    const top = Math.max(0, (outerHeight - innerHeight)) * (window.devicePixelRatio || 1);
    try {
      api.runtime.sendMessage({ type: "hello", title: document.title, top }, (res) => {
        void api.runtime.lastError;
        const was = !!companion;
        companion = res && res.companion ? res.companion : null;
        if (was !== !!companion) prefill = true;
      });
    } catch (e) { /* extension reloaded */ }
  }
  hello();
  setInterval(hello, 2000);
  function stats() {
    let n = 0; for (let i = 0; i < CAP; i++) n += P.alive[i];
    const big = [...piles.values()].sort((a, b) => b.mass() - a.mass())[0];
    return { running, particles: n, surfaces: piles.size, season: season(), companion: !!companion,
             mass: Math.round([...piles.values()].reduce((a, p) => a + p.mass(), 0)),
             debug: big ? { n: big.n, h: [...big.h].filter((_, i) => i % 4 === 0).map((v) => Math.round(v)),
                            vis: [...big.vis].filter((_, i) => i % 4 === 0).join("") } : null };
  }
})();
