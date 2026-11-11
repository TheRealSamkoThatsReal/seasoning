// Background: the only part allowed to talk to the seasoning desktop app on
// 127.0.0.1 (page scripts can't reach localhost from https pages).
const api = globalThis.browser ?? globalThis.chrome;
const BRIDGE = "http://127.0.0.1:47914";

let desktop = null;         // last /state from the desktop app, or null
let checkedAt = 0;

async function fetchState() {
  if (Date.now() - checkedAt < 1500) return desktop;
  checkedAt = Date.now();
  try {
    const r = await fetch(`${BRIDGE}/state`, { cache: "no-store" });
    desktop = r.ok ? await r.json() : null;
  } catch {
    desktop = null;          // app not running: pages draw everything themselves
  }
  return desktop;
}

async function announce(title, top) {
  if (!desktop || !title) return;
  try {
    await fetch(`${BRIDGE}/page`, {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ titles: [title], top }),
    });
  } catch { /* app went away */ }
}

api.runtime.onMessage.addListener((msg, sender, reply) => {
  if (msg.type === "hello") {
    fetchState().then((st) => {
      if (st) announce(msg.title, msg.top);
      reply({ companion: st });
    });
    return true;             // async reply
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
      reply({ companion: await fetchState() });
    })();
    return true;
  }
  return false;
});
