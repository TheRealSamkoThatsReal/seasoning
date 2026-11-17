// Sync with the desktop app: with a (fake) desktop app that goes off and on again,
// the page must stop and resume; a browser that never saw the app keeps snowing.
//   node extension/test/companion.js
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const path = require("path");

const EXT = path.resolve(__dirname, "..", "dist", "chrome");
const SITE = path.join(__dirname, "site");
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function fakeDesktop(port) {         // just enough of the real app's bridge
  const srv = http.createServer((req, res) => {
    res.setHeader("access-control-allow-origin", "*");
    res.setHeader("access-control-allow-headers", "content-type");
    if (req.method === "OPTIONS") { res.writeHead(204); return res.end(); }
    if (req.url.startsWith("/state")) {
      res.writeHead(200, { "content-type": "application/json" });
      return res.end(JSON.stringify({ app: "seasoning", season: "winter", intensity: 1, wind_deg: 90,
                                      wind: 0, stick: 0.35, paused: false, hidden: false }));
    }
    if (req.url.startsWith("/handoff")) { res.writeHead(200); return res.end("[]"); }
    res.writeHead(200); res.end("{}");
  });
  return new Promise((r) => srv.listen(port, "127.0.0.1", () => r(srv)));
}

async function launch() {
  const site = http.createServer((req, res) => {
    fs.readFile(path.join(SITE, "index.html"), (e, d) => { res.writeHead(200, { "content-type": "text/html" }); res.end(d); });
  });
  await new Promise((r) => site.listen(0, "127.0.0.1", r));
  const ctx = await chromium.launchPersistentContext(fs.mkdtempSync("/tmp/seasoning-sync-"), {
    executablePath: process.env.EXE || undefined, headless: true, channel: "chromium",
    viewport: { width: 1280, height: 800 },
    args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`],
  });
  let [sw] = ctx.serviceWorkers();
  if (!sw) sw = await ctx.waitForEvent("serviceworker", { timeout: 20000 });
  return { ctx, sw, site, url: `http://127.0.0.1:${site.address().port}/` };
}

const stats = (page) => page.evaluate(() =>
  JSON.parse(document.querySelector("canvas[aria-hidden]")?.dataset.stats || "null"));

(async () => {
  const fail = [];
  const port = 47000 + Math.floor(Math.random() * 900);

  // 1) desktop app on -> off -> on
  {
    const { ctx, sw, site, url } = await launch();
    await sw.evaluate((p) => chrome.storage.local.set({ companion: true, bridgePort: p }), port);
    let app = await fakeDesktop(port);
    const page = await ctx.newPage();
    await page.goto(url);
    await sleep(5000);
    let st = await stats(page);
    console.log("app on      :", st && { running: st.running, companion: st.companion });
    if (!st || !st.running || !st.companion) fail.push("did not sync with the running app");
    await new Promise((r) => app.close(r));
    await sleep(8000);
    st = await stats(page);
    console.log("app off     :", st && { running: st.running });
    if (!st || st.running) fail.push("kept snowing after the desktop app went off");
    app = await fakeDesktop(port);
    await sleep(6000);
    st = await stats(page);
    console.log("app back on :", st && { running: st.running, companion: st.companion });
    if (!st || !st.running) fail.push("did not come back when the app came back");
    await new Promise((r) => app.close(r));
    await ctx.close(); site.close();
  }

  // 2) a browser that has never seen the app (ChromeOS) keeps snowing on its own
  {
    const { ctx, sw, site, url } = await launch();
    await sw.evaluate((p) => chrome.storage.local.set({ companion: true, bridgePort: p }), port + 1);
    const page = await ctx.newPage();
    await page.goto(url);
    await sleep(8000);
    const st = await stats(page);
    console.log("never seen  :", st && { running: st.running, companion: st.companion });
    if (!st || !st.running) fail.push("stopped even though no desktop app was ever seen");
    await ctx.close(); site.close();
  }

  if (fail.length) { console.error("FAIL:", fail.join("; ")); process.exit(1); }
  console.log("sync test: ok");
})().catch((e) => { console.error(e); process.exit(1); });
