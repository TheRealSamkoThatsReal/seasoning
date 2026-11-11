// End-to-end test: load the unpacked Chrome build in Chromium, open the test page,
// let the season run, check the extension is animating and piling up, screenshot.
//   node extension/test/run.js            (needs `npm i playwright` + a Chromium)
// env: EXE=<chromium binary>  SECS=30  SEASON=winter  AMOUNT=2  SHOT=out.png
const { chromium } = require("playwright");
const http = require("http");
const fs = require("fs");
const path = require("path");

const EXT = path.resolve(__dirname, "..", "dist", "chrome");
const SITE = path.join(__dirname, "site");
const SECS = +(process.env.SECS || 30);

const server = http.createServer((req, res) => {
  const f = path.join(SITE, req.url === "/" ? "index.html" : req.url);
  fs.readFile(f, (err, data) => {
    if (err) { res.writeHead(404); return res.end(); }
    res.writeHead(200, { "content-type": "text/html" }); res.end(data);
  });
});

(async () => {
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  const url = `http://127.0.0.1:${server.address().port}/`;
  const ctx = await chromium.launchPersistentContext(fs.mkdtempSync("/tmp/seasoning-ext-"), {
    executablePath: process.env.EXE || undefined, headless: true, channel: "chromium",
    viewport: { width: 1280, height: 800 },
    args: [`--disable-extensions-except=${EXT}`, `--load-extension=${EXT}`],
  });
  let [sw] = ctx.serviceWorkers();
  if (!sw) sw = await ctx.waitForEvent("serviceworker", { timeout: 20000 });
  await sw.evaluate((s) => chrome.storage.local.set(s),
                    { season: process.env.SEASON || "winter", intensity: +(process.env.AMOUNT || 2),
                      companion: false });   // independent of any desktop app on this machine
  const page = await ctx.newPage();
  const errors = [];
  page.on("pageerror", (e) => errors.push(String(e)));
  await page.goto(url, { waitUntil: "load" });
  let st = null;
  for (let s = 0; s < SECS; s += 10) {
    await page.waitForTimeout(10000);
    st = JSON.parse(await page.evaluate(() => document.querySelector("canvas[aria-hidden]")?.dataset.stats || "null"));
    console.log(`t=${s + 10}s`, st && { ...st, debug: undefined });
  }
  if (process.env.SHOT) await page.screenshot({ path: process.env.SHOT });
  await ctx.close();
  server.close();
  const fail = [];
  if (!st || !st.running) fail.push("not running");
  if (st && st.particles < 50) fail.push(`only ${st.particles} particles`);
  if (st && st.surfaces < 5) fail.push(`only ${st.surfaces} surfaces`);
  if (st && st.mass < 500) fail.push(`piles too small (${st.mass})`);
  if (errors.length) fail.push(`page errors: ${errors.join("; ")}`);
  if (fail.length) { console.error("FAIL:", fail.join(", ")); process.exit(1); }
  console.log("extension test: ok");
})().catch((e) => { console.error(e); process.exit(1); });
