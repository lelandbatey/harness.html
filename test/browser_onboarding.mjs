// Headless end-to-end test of onboarding (questions/034): no mock provider, a fake OpenAI-compatible endpoint.
//   1. file://, not running            -> step 1 of 2 (start the server)
//   2. server runs, no H, no P         -> step 2 (provider form: model list, Test, Save) -> normal UI live
//   3. H, no P                         -> read only + dismissable banner (returns on reload), #setup, #about
//   4. served over http (W), with H    -> read-only snapshot + "install your own"; never probes a local port
//   5. W, empty copy                   -> landing page; Download saves the file's exact bytes
// Usage: node test/browser_onboarding.mjs   (CHROME=/path/to/chromium to override; KEEP=1 keeps the temp dir)
import fs from "node:fs";
import http from "node:http";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
let puppeteer;
for (const base of [path.join(repo, "package.json"), path.join(repo, "..", "local_sqlite_selfcontained_html", "package.json")]) {
  try { puppeteer = createRequire(base)("puppeteer-core"); break; } catch {}
}
if (!puppeteer) { console.error("puppeteer-core not found"); process.exit(1); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// inside the repo (gitignored .smoke-*): snap Chromium has a private /tmp
const tmp = fs.mkdtempSync(path.join(repo, ".smoke-onboard-"));
const keepTmp = !!process.env.KEEP;
process.on("exit", () => { if (!keepTmp) try { fs.rmSync(tmp, { recursive: true, force: true }); } catch {} });
const py = process.env.PYTHON || "python3";
function build(out) {
  if (process.env.SFH_TEST_BUILD) { fs.copyFileSync(process.env.SFH_TEST_BUILD, out); fs.chmodSync(out, 0o755); return; }  // shared by test/run.py
  const r = spawnSync(py, [path.join(repo, "tools", "build.py"), "--fresh", "-o", out], { encoding: "utf8" });
  if (r.status !== 0) { console.error("build failed:\n" + r.stdout + r.stderr); process.exit(1); }
  fs.chmodSync(out, 0o755);
}
const file = path.join(tmp, "harness.html");
build(file);
const env = { ...process.env, XDG_CONFIG_HOME: path.join(tmp, "config"), XDG_STATE_HOME: path.join(tmp, "state"), SHELL: "/bin/sh", SFH_SAVE_DELAY: "0.05", SFH_SHUTDOWN_TIMEOUT: "0.2" };
delete env.ANTHROPIC_API_KEY;
delete env.SFH_MOCK;
for (const k of Object.keys(env)) if (k.startsWith("AI_")) delete env[k];

// a fake OpenAI-compatible endpoint
const seen = { auth: [] };
const fake = http.createServer((req, res) => {
  seen.auth.push(req.headers.authorization || "");
  if (req.method === "GET") {
    res.writeHead(200, { "content-type": "application/json" });
    res.end(JSON.stringify({ data: [{ id: "fake-small" }, { id: "fake-big" }] }));
    return;
  }
  let body = "";
  req.on("data", (d) => (body += d));
  req.on("end", () => {
    const j = JSON.parse(body || "{}");
    if (!j.stream) {
      res.writeHead(200, { "content-type": "application/json" });
      res.end(JSON.stringify({ choices: [{ index: 0, message: { role: "assistant", content: j.max_tokens <= 16 ? "OK" : '{"title": "Fake chat", "subjects": ["fake"]}' }, finish_reason: "stop" }] }));
      return;
    }
    res.writeHead(200, { "content-type": "text/event-stream" });
    const ch = (delta, finish = null) => "data: " + JSON.stringify({ id: "c", choices: [{ index: 0, delta, finish_reason: finish }] }) + "\n\n";
    res.end(ch({ role: "assistant", content: "Hello from " }) + ch({ content: "the fake endpoint" }) + ch({}, "stop") + "data: [DONE]\n\n");
  });
});
await new Promise((r) => fake.listen(0, "127.0.0.1", r));
const fakeBase = "http://127.0.0.1:" + fake.address().port + "/v1";

// a static web server for the "published copy" steps
let webRoot = tmp;
const web = http.createServer((req, res) => {
  const p = path.join(webRoot, decodeURIComponent(new URL(req.url, "http://x").pathname));
  if (!p.startsWith(webRoot) || !fs.existsSync(p)) { res.writeHead(404); res.end(); return; }
  res.writeHead(200, { "content-type": "text/html; charset=utf-8" });
  res.end(fs.readFileSync(p));
});
await new Promise((r) => web.listen(0, "127.0.0.1", r));

let proc = null, browser = null;
function start() {
  const p = spawn("sh", [file], { env, cwd: tmp });
  p.out = "";
  p.stdout.on("data", (d) => (p.out += d));
  p.stderr.on("data", (d) => (p.out += d));
  return p;
}
async function stop(p) {
  if (!p || p.exitCode !== null) return;
  const done = new Promise((r) => p.once("exit", r));
  p.kill("SIGTERM");
  let timer;  // cleared: a pending 5s timer would keep node running after the test
  await Promise.race([done, new Promise((r) => { timer = setTimeout(r, 5000); })]);
  clearTimeout(timer);
}
async function cleanup() {
  await stop(proc);
  if (browser) await browser.close().catch(() => {});
  fake.close();
  web.close();
}
async function fail(msg) {
  console.error("FAIL:", msg);
  if (proc) console.error("--- server output ---\n" + proc.out);
  await cleanup();
  process.exit(1);
}
process.on("uncaughtException", (e) => fail("exception in the test: " + (e && e.stack || e)));
process.on("unhandledRejection", (e) => fail("unhandled rejection in the test: " + (e && e.stack || e)));
const step = (m) => console.log("ok: " + m);
async function waitFor(page, fn, what, timeout = 10000, ...args) {
  try {
    await page.waitForFunction(fn, { timeout, polling: 50 }, ...args);
  } catch (e) {
    const txt = await page.evaluate(() => document.body.innerText.slice(0, 1500)).catch(() => "?");
    await fail(`timed out waiting for: ${what}\n--- page text ---\n${txt}`);
  }
}

browser = await puppeteer.launch({ executablePath: process.env.CHROME || "/usr/bin/chromium-browser", headless: true, args: ["--no-sandbox"] });
const errors = [];
const localProbes = [];
async function newPage(url) {
  const page = await browser.newPage();
  // look for a (re)started server every 150ms instead of every 2s
  await page.evaluateOnNewDocument(() => { try { localStorage.setItem("sfh:probeMs", "150"); } catch {} });
  await page.setViewport({ width: 1200, height: 850 });
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("console", (m) => { if (m.type() === "error" && !/ERR_CONNECTION_REFUSED|Failed to load resource|WebSocket connection/.test(m.text())) errors.push(m.text()); });
  if (url.startsWith("http")) page.on("request", (r) => { const m = /^(?:http|ws):\/\/127\.0\.0\.1:(\d+)\//.exec(r.url()); if (m && +m[1] >= 47100 && +m[1] < 47900) localProbes.push(r.url()); });
  page.on("dialog", (d) => d.accept());
  await page.goto(url);
  return page;
}
const checkErrors = async () => { if (errors.length) await fail("page errors:\n" + errors.join("\n")); };

// 1. file://, not running: step 1 of 2
const p1 = await newPage("file://" + file);
await waitFor(p1, () => !document.querySelector("#connect").hidden && document.querySelector("#step-run").classList.contains("active") &&
  document.querySelector("#step-provider").classList.contains("pending") && document.querySelector("#connect-tagline").textContent.length > 20, "step 1 of 2");
step("not running: step 1 (start the server), step 2 pending");

// 2. start: no history, no provider -> step 2 with the provider form; the app stays hidden
proc = start();
await waitFor(p1, () => document.querySelector("#step-run").classList.contains("done") && document.querySelector("#step-provider").classList.contains("active") &&
  document.querySelector("#step-provider-body form.setup-form") && document.querySelector("#app").hidden, "step 2 with the provider form", 20000);
if (!/none configured/.test(proc.out)) await fail("server start does not mention the missing provider:\n" + proc.out);
await p1.type("#step-provider-body input[name=base_url]", fakeBase);
await p1.type("#step-provider-body input[name=token]", "sk-onboard");
await p1.evaluate(() => document.querySelector("#step-provider-body input[name=token]").dispatchEvent(new Event("change")));
await waitFor(p1, () => document.querySelectorAll("#step-provider-body datalist option").length === 2, "model list from the endpoint");
await p1.type("#step-provider-body input[name=model]", "fake-small");
await p1.evaluate(() => [...document.querySelectorAll("#step-provider-body button")].find((b) => b.textContent === "Test").click());
await waitFor(p1, () => /It works/.test(document.querySelector("#step-provider-body .setup-status").textContent), "Test succeeds");
await p1.evaluate(() => [...document.querySelectorAll("#step-provider-body button")].find((b) => b.textContent === "Save and use").click());
await waitFor(p1, () => !document.querySelector("#app").hidden && document.querySelector("#connect").hidden && location.hash === "#new", "switched to the normal UI live", 10000);
const creds = fs.readFileSync(path.join(tmp, "config", "singlefile", "credentials"), "utf8");
if (!/PROVIDER_127_0_0_1_KEY=sk-onboard/.test(creds)) await fail("key not in the credentials file: " + creds);
await sleep(2000);
if (fs.readFileSync(file, "utf8").includes("sk-onboard")) await fail("the key ended up in the html file");
if (!seen.auth.includes("Bearer sk-onboard")) await fail("the endpoint never saw the key");
step("step 2: model list, Test, Save -> normal UI without reload; key in the credentials file only");

// a first message through the fake endpoint
await p1.type(".composer textarea", "hi there");
await p1.keyboard.press("Enter");
await waitFor(p1, () => [...document.querySelectorAll(".msgs .md")].some((e) => e.textContent.includes("Hello from the fake endpoint")), "reply from the fake endpoint", 15000);
step("chat works with the configured provider");

// 3. history, no provider: read only + banner
await p1.evaluate(() => window.SFHApp.rpc("settings.set", { key: "providers", value: [] }).then(() => window.SFHApp.rpc("settings.set", { key: "provider", value: "auto" })));
await waitFor(p1, () => document.querySelector("#notice-provider") && /Set up a provider/.test(document.querySelector(".composer .btn.primary")?.textContent || "") &&
  ![...document.querySelectorAll(".msg-actions .link-btn")].some((b) => b.textContent === "Edit"), "read-only banner, composer, no Edit");
await p1.click("#notice-provider .icon-btn");
await waitFor(p1, () => !document.querySelector("#notice-provider"), "banner dismissed");
await p1.reload();
await waitFor(p1, () => document.querySelector("#notice-provider") && !document.querySelector("#app").hidden, "banner back after reload", 15000);
await p1.click("#notice-provider a");
await waitFor(p1, () => location.hash === "#setup" && document.querySelector("#view form.setup-form"), "#setup page with the form");
await p1.evaluate(() => { location.hash = "#about"; });
await waitFor(p1, () => document.querySelector("#nav-about.active") && /lives in one HTML file/.test(document.querySelector("#view").textContent), "#about page");
step("history without provider: read only, dismissable banner (back on reload), #setup, #about");
await stop(proc);
proc = null;
await sleep(500);

// 4. the same file served over http (a published copy with history): snapshot + install banner, no local probes
const p2 = await newPage("http://127.0.0.1:" + web.address().port + "/harness.html");
await waitFor(p2, () => document.querySelector("#app.offline") && /shared copy/.test(document.querySelector("#banner").textContent), "web copy: read-only snapshot + install banner");
await p2.evaluate(() => [...document.querySelectorAll("#banner button")].find((b) => b.textContent === "Install your own").click());
await waitFor(p2, () => !document.querySelector("#landing").hidden && document.querySelector("#landing-download"), "landing page from the banner");
step("served over http with history: read-only snapshot, install banner -> landing page");

// 5. an empty published copy: landing page; Download saves the exact bytes
const pub = path.join(tmp, "pub");
fs.mkdirSync(pub);
build(path.join(pub, "harness.html"));
webRoot = pub;
const dl = path.join(tmp, "downloads");
fs.mkdirSync(dl);
const p3 = await newPage("http://127.0.0.1:" + web.address().port + "/harness.html");
await waitFor(p3, () => !document.querySelector("#landing").hidden && document.querySelector("#app").hidden && /HTML only/.test(document.querySelector("#landing").textContent), "landing page");
const cdp = await p3.target().createCDPSession();
await cdp.send("Browser.setDownloadBehavior", { behavior: "allow", downloadPath: dl });
await p3.click("#landing-download");
for (let i = 0; i < 100 && !fs.existsSync(path.join(dl, "harness.html")); i++) await sleep(100);
if (!fs.existsSync(path.join(dl, "harness.html"))) await fail("download did not arrive: " + fs.readdirSync(dl));
if (!fs.readFileSync(path.join(dl, "harness.html")).equals(fs.readFileSync(path.join(pub, "harness.html")))) await fail("downloaded bytes differ from the published file");
if (localProbes.length) await fail("a web copy contacted a local server: " + localProbes.join(", "));
step("empty web copy: landing page; Download saves identical bytes; no local server contacted");

await checkErrors();
await cleanup();
console.log("PASS");
