const api = globalThis.browser ?? globalThis.chrome;
const $ = (id) => document.getElementById(id);
const DEFAULTS = { enabled: true, season: "auto", intensity: 1.0, wind_deg: 90, wind: 0, stick: 0.35,
                   piles: true, companion: true, disabledSites: [] };

let cfg = { ...DEFAULTS }, host = null, tabId = null, desktop = null;

const get = (keys) => new Promise((res) => {
  const p = api.storage.local.get(keys, res);
  if (p && p.then) p.then(res);
});
const set = (obj) => api.storage.local.set(obj);
const msgBg = (m) => new Promise((res) => api.runtime.sendMessage(m, (r) => { void api.runtime.lastError; res(r); }));
const msgTab = (m) => new Promise((res) => {
  if (tabId == null) return res(null);
  api.tabs.sendMessage(tabId, m, (r) => { void api.runtime.lastError; res(r); });
});

// wind as one slider: -1 (blowing left) … 0 calm … +1 (blowing right)
const windToX = (deg, s) => Math.round(Math.cos((deg * Math.PI) / 180) * s * 100) / 100;
const xToWind = (x) => ({ wind_deg: x < 0 ? 180 : 0, wind: Math.abs(x) });

function render() {
  const src = desktop || cfg;
  $("enabled").checked = cfg.enabled;
  $("season").value = desktop ? (cfg.season === "auto" ? "auto" : desktop.season) : cfg.season;
  $("intensity").value = src.intensity;
  $("windx").value = windToX(src.wind_deg, src.wind);
  $("piles").checked = cfg.piles;
  $("companion").checked = cfg.companion;
  $("site").checked = !host || !cfg.disabledSites.includes(host);
  $("site").disabled = !host;
  $("sync").hidden = !desktop;
}

// with the desktop app running, weather settings go to the app (everything follows it)
async function weather(cmd, local) {
  if (desktop) desktop = (await msgBg({ type: "desktop", cmd })).companion || desktop;
  else await set(local);
  Object.assign(cfg, desktop ? {} : local);
}

async function stats() {
  const s = await msgTab({ type: "stats" });
  $("stats").textContent = s
    ? `${s.season} · ${s.particles} falling · ${s.surfaces} surfaces` + (s.companion ? " · synced" : "")
    : host ? "Reload the page to start here." : "Not available on this page.";
}

(async () => {
  cfg = { ...DEFAULTS, ...(await get(DEFAULTS)) };
  const [tab] = await new Promise((res) => {
    const p = api.tabs.query({ active: true, currentWindow: true }, res);
    if (p && p.then) p.then(res);
  });
  if (tab) {
    tabId = tab.id;
    try { const u = new URL(tab.url); if (/^https?:$/.test(u.protocol)) host = u.hostname; } catch { /* */ }
  }
  desktop = cfg.companion ? (await msgBg({ type: "desktop" }))?.companion || null : null;
  render();
  stats();
  setInterval(stats, 1000);
})();

$("enabled").onchange = (e) => set({ enabled: e.target.checked });
$("piles").onchange = (e) => set({ piles: e.target.checked });
$("companion").onchange = async (e) => {
  cfg.companion = e.target.checked;
  await set({ companion: cfg.companion });
  desktop = cfg.companion ? (await msgBg({ type: "desktop" }))?.companion || null : null;
  render();
};
$("site").onchange = (e) => {
  const s = new Set(cfg.disabledSites);
  e.target.checked ? s.delete(host) : s.add(host);
  cfg.disabledSites = [...s];
  set({ disabledSites: cfg.disabledSites });
};
$("season").onchange = (e) => {
  const v = e.target.value;
  set({ season: v });
  cfg.season = v;
  if (desktop) msgBg({ type: "desktop", cmd: `season ${v}` });
};
$("intensity").oninput = (e) => weather(`intensity ${e.target.value}`, { intensity: +e.target.value });
$("windx").oninput = (e) => {
  const w = xToWind(+e.target.value);
  weather(`wind ${w.wind_deg} ${Math.round(w.wind * 100)}`, w);
};
$("clear").onclick = () => {
  msgTab({ type: "clear" });
  if (desktop) msgBg({ type: "desktop", cmd: "clear" });
};
