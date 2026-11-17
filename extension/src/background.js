// Background: the only part allowed to talk to the seasoning desktop app on
// 127.0.0.1 (page scripts can't reach localhost from https pages).
const api = globalThis.browser ?? globalThis.chrome;
let BRIDGE = "http://127.0.0.1:47914";
// (tests point this at a fake desktop app with the hidden `bridgePort` setting)
{
  const got = (v) => { if (v && v.bridgePort) BRIDGE = `http://127.0.0.1:${v.bridgePort}`; };
  const p = api.storage.local.get({ bridgePort: 0 }, got);
  if (p && p.then) p.then(got);
  api.storage.onChanged.addListener((ch) => {
    if (ch.bridgePort) BRIDGE = `http://127.0.0.1:${ch.bridgePort.newValue || 47914}`;
  });
}

let desktop = null;         // last /state from the desktop app, or null
let checkedAt = 0;
let misses = 0;             // consecutive failed checks
let seen = false;           // has this browser ever talked to the desktop app?
// (remembered across restarts, so a browser that never had the app - ChromeOS -
// keeps snowing on its own instead of waiting for an app that doesn't exist)
{
  const got = (v) => { seen = seen || !!(v && v.desktopSeen); };
  const p = api.storage.local.get({ desktopSeen: false }, got);   // Chrome: callback
  if (p && p.then) p.then(got);                                    // Firefox: promise
}

// the desktop app counts as switched off once it has been seen and then stops
// answering twice in a row (~4 s; one missed check could just be a hiccup)
const desktopOff = () => seen && misses >= 2;

async function fetchState() {
  if (Date.now() - checkedAt < 1500) return desktop;
  checkedAt = Date.now();
  try {
    const r = await fetch(`${BRIDGE}/state`, { cache: "no-store" });
    desktop = r.ok ? await r.json() : null;
  } catch {
    desktop = null;          // app not running
  }
  if (desktop) {
    misses = 0;
    if (!seen) { seen = true; api.storage.local.set({ desktopSeen: true }); }
  } else {
    misses++;
  }
  return desktop;
}

async function announce(title, top, left, dpr) {
  if (!desktop || !title) return;
  try {
    await fetch(`${BRIDGE}/page`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ titles: [title], top, left, dpr }),
    });
  } catch { /* app went away */ }
}

api.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === "hello") {
    fetchState().then((st) => {
      if (st) announce(msg.title, msg.top, msg.left, msg.dpr);
      reply({ companion: st, desktopOff: desktopOff() });
    });
    return true;             // async reply
  }
  if (msg.type === "handoff") {               // particles crossing between desktop and page
    (async () => {
      try {
        if (msg.back && msg.back.length) {
          fetch(`${BRIDGE}/handback`, {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ title: msg.title, items: msg.back }),
          }).catch(() => {});
        }
        const r = await fetch(`${BRIDGE}/handoff?title=${encodeURIComponent(msg.title)}`, { cache: "no-store" });
        reply({ items: r.ok ? await r.json() : [] });
      } catch {
        reply({ items: [] });
      }
    })();
    return true;
  }
  if (msg.type === "desktop") {               // popup: status / change a desktop setting
    (async () => {
      if (msg.cmd && desktop) {
        try {
          await fetch(`${BRIDGE}/command`, {
            method: "POST", headers: { "content-type": "application/json" },
            body: JSON.stringify({ cmd: msg.cmd }),
          });
        } catch { /* ignore */ }
        checkedAt = 0;
      }
      const st = await fetchState();
      reply({ companion: st, desktopOff: desktopOff() });
    })();
    return true;
  }
  return false;
});
