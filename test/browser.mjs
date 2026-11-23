// Headless end-to-end test of the browser client against the real program (mock LLM provider).
// Usage: node test/browser.mjs  (builds its own empty copy)        (CHROME=/path/to/chromium to override; KEEP=1 keeps the temp dir)
// Runs from any cwd and can run concurrently with other copies (own temp dir, port derived from its path).
// PART=1 / PART=2 runs one half (test/run.py runs both at once); $SFH_TEST_BUILD: copy that build instead of building.
import fs from "node:fs";
import path from "node:path";
import { spawn, spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import { createRequire } from "node:module";

const repo = path.dirname(path.dirname(fileURLToPath(import.meta.url)));
// puppeteer-core: from this repo if installed, else from the sibling project that has it
let puppeteer;
for (const base of [path.join(repo, "package.json"), path.join(repo, "..", "local_sqlite_selfcontained_html", "package.json")]) {
  try { puppeteer = createRequire(base)("puppeteer-core"); break; } catch {}
}
if (!puppeteer) { console.error("puppeteer-core not found"); process.exit(1); }
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// not os.tmpdir(): snap Chromium has a private /tmp and could not open the file. The dir lives in the
// repo (gitignored .smoke-*) and is removed on any exit, pass or fail, unless KEEP=1.
const tmp = fs.mkdtempSync(path.join(repo, ".smoke-browser-"));
let keepTmp = !!process.env.KEEP;
function removeTmp() {
  if (keepTmp) return;
  keepTmp = true; // once
  try { fs.rmSync(tmp, { recursive: true, force: true }); } catch {}
}
process.on("exit", removeTmp);
let interrupted = false;
for (const sig of ["SIGINT", "SIGTERM", "SIGHUP"]) process.on(sig, () => {
  interrupted = true;
  console.error("interrupted (" + sig + "): stopping servers and browser");
  cleanup().finally(() => process.exit(130));
  setTimeout(() => process.exit(130), 8000).unref();
});
// dirs left by runs killed with SIGKILL (no exit handler): anything over an hour old
for (const d of fs.readdirSync(repo)) {
  const full = path.join(repo, d);
  try { if (d.startsWith(".smoke-browser-") && full !== tmp && Date.now() - fs.statSync(full).mtimeMs > 3600e3) fs.rmSync(full, { recursive: true, force: true }); } catch {}
}
const file = path.join(tmp, "harness.html");
// a fresh, empty build (dist/harness.html holds the owner's data and must not be used)
// ($SFH_TEST_BUILD: such a build made by test/run.py, shared by the browser tests)
if (process.env.SFH_TEST_BUILD) fs.copyFileSync(process.env.SFH_TEST_BUILD, file);
else {
  const built = spawnSync(process.env.PYTHON || "python3", [path.join(repo, "tools", "build.py"), "--fresh", "-o", file], { encoding: "utf8" });
  if (built.status !== 0) { console.error("build failed:\n" + built.stdout + built.stderr); process.exit(1); }
}
fs.chmodSync(file, 0o755);
// SHELL=/bin/sh keeps the /pty shell step independent of the user's rc files
const env = { ...process.env, XDG_CONFIG_HOME: path.join(tmp, "config"), XDG_STATE_HOME: path.join(tmp, "state"), SHELL: "/bin/sh", SFH_MOCK: "1",
  // shorter built-in delays (see test/fastenv.py); a small nonzero chunk delay keeps streamed states observable
  SFH_MOCK_DELAY: "0.003", SFH_MOCK_SLOW_DELAY: "0.05", SFH_SAVE_DELAY: "0.05", SFH_SHUTDOWN_TIMEOUT: "0.2" };
delete env.ANTHROPIC_API_KEY;
for (const k of Object.keys(env)) if (k.startsWith("AI_")) delete env[k]; // the owner's shell exports a real provider
const fileToken = () => (/<meta name="sfh-token" content="([^"]*)"/.exec(fs.readFileSync(file, "utf8")) || [])[1] || "";
let dialogPolicy = []; // queue of [regex, accept] for confirm() dialogs

let proc = null, browser = null;
const procs = [];
function start() {
  const p = spawn("sh", [file], { env, cwd: tmp });
  p.out = "";
  p.stdout.on("data", (d) => (p.out += d));
  p.stderr.on("data", (d) => (p.out += d));
  procs.push(p);
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
let cleaning = null;
function cleanup() { return cleaning || (cleaning = doCleanup()); }
async function doCleanup() {
  for (const p of procs) await stop(p);
  if (browser) await browser.close().catch(() => {});
}
async function fail(msg) {
  if (interrupted) return new Promise(() => {}); // pending waits reject once the browser closes; the signal handler exits
  console.error("FAIL:", msg);
  if (proc) console.error("--- server output ---\n" + proc.out);
  await cleanup();
  console.error(keepTmp ? "temp dir kept: " + tmp : "(KEEP=1 keeps the temp dir for inspection)");
  process.exit(1);
}
// a bug in the test itself must still stop the servers and the browser (and remove the temp dir)
process.on("uncaughtException", (e) => fail("exception in the test: " + (e && e.stack || e)));
process.on("unhandledRejection", (e) => fail("unhandled rejection in the test: " + (e && e.stack || e)));
const step = (m) => console.log("ok: " + m);
// Types into a field and checks that every keystroke landed. A re-render that detaches or replaces the
// focused field mid-typing drops the remaining keys silently (focus falls back to <body>), which would
// otherwise surface much later as an unrelated-looking timeout.
async function typeInto(page, sel, text) {
  await waitFor(page, (s) => { const e = document.querySelector(s); return e && !e.disabled && e.offsetParent !== null; }, "field " + sel + " ready", 10000, sel);
  const before = await page.$eval(sel, (e) => e.value);
  await page.type(sel, text);
  const got = await page.evaluate((s) => {
    const e = document.querySelector(s), a = document.activeElement;
    return { value: e && e.value, focused: !!e && e === a, active: a ? a.tagName + (a.className ? "." + a.className : "") : "none" };
  }, sel);
  if (got.value !== before + text || !got.focused)
    await fail(`keystrokes lost typing into ${sel}: expected ${JSON.stringify(before + text)}, field has ${JSON.stringify(got.value)}; focus is on ${got.active}`);
}
async function waitFor(page, fn, what, timeout = 10000, ...args) {
  try {
    await page.waitForFunction(fn, { timeout, polling: 50 }, ...args);
  } catch (e) {
    const html = await page.evaluate(() => (document.querySelector("#view")?.innerText?.slice(0, 1500) || "") + (document.querySelector("#term-panel").hidden ? "" : "\n--- terminal ---\n" + document.querySelector("#term-host .xterm-rows")?.textContent + "\nactive=" + document.activeElement.className + " ws=" + window.SFHApp.terminal.ws?.readyState + " " + window.SFHApp.terminal.ws?.url)).catch(() => "?");
    await fail(`timed out waiting for: ${what}\n--- view text ---\n${html}`);
  }
}

browser = await puppeteer.launch({ executablePath: process.env.CHROME || "/usr/bin/chromium-browser", headless: true,
                                   args: ["--no-sandbox"] });
const errors = [];
async function newPage(hash = "") {
  const page = await browser.newPage();
  // look for a (re)started server every 150ms instead of every 2s
  await page.evaluateOnNewDocument(() => { try { localStorage.setItem("sfh:probeMs", "150"); } catch {} });
  await page.setViewport({ width: 1200, height: 850 });
  page.on("pageerror", (e) => errors.push(String(e)));
  page.on("dialog", async (d) => {
    const i = dialogPolicy.findIndex(([re]) => re.test(d.message()));
    if (i < 0) { errors.push("unexpected dialog: " + d.message()); await d.dismiss(); return; }
    const [, accept] = dialogPolicy.splice(i, 1)[0];
    page.lastDialog = d.message();
    accept ? await d.accept() : await d.dismiss();
  });
  page.on("console", (m) => { if (m.type() === "error" && !/ERR_CONNECTION_REFUSED|Failed to load resource|WebSocket connection/.test(m.text())) errors.push(m.text()); });
  await page.goto("file://" + file + hash);
  return page;
}
const checkErrors = async () => { if (errors.length) await fail("page errors:\n" + errors.join("\n")); };

async function terminalStep() {
  await p1.bringToFront(); // background tabs pause requestAnimationFrame, which xterm paints with
  await p1.evaluate(() => [...document.querySelectorAll(".chat-head .btn")].find((b) => b.textContent === "Terminal").click());
  await waitFor(p1, () => !document.querySelector("#term-panel").hidden && document.querySelector("#term-host .xterm"), "terminal panel opened");
  await sleep(500);
  await p1.keyboard.type("echo sfh-$((40+2))\r");
  await waitFor(p1, () => (document.querySelector("#term-host .xterm-rows")?.textContent || "").includes("sfh-42"), "shell output in xterm", 8000);
  await p1.keyboard.type("exit\r");
  await waitFor(p1, () => (document.querySelector("#term-host .xterm-rows")?.textContent || "").includes("process exited"), "exit message", 5000);
  await p1.click("#term-close");
  step("in-page terminal runs a shell over /pty");
}

// 1. not running -> connect screen with the command
const p1 = await newPage();
await waitFor(p1, () => !document.querySelector("#connect").hidden, "not-running screen");
const cmd = await p1.$eval("#connect-cmd", (e) => e.textContent);
if (!cmd.includes("sh '" + file + "'")) await fail("run command wrong: " + cmd);
step("not-running screen shows " + cmd);

if (fileToken()) await fail("fresh build already has a token");

// 2. start the server -> it writes a token into the file; the page (loaded before) pairs via health
proc = start();
await waitFor(p1, () => !document.querySelector("#app").hidden && window.SFHApp.state.connected, "auto-connect", 20000);
await waitFor(p1, () => location.hash === "#new", "empty db opens the new-session view");
const token1 = fileToken();
if (!token1) await fail("server did not write a token into the file");
if ((await p1.evaluate(() => window.SFHApp.state.token)) !== token1) await fail("page token is not the file token");
step("auto-connected via pairing token; new-session view");

// The rest runs in two halves: PART=1 (steps 3-11d) and PART=2 (the terminal, steps 13-19), which
// test/run.py runs at the same time, each with its own copy, server and browser. Unset: both, one after another.
const PART = process.env.PART || "";
let sid, p2;
if (PART !== "2") {

// 3. send the first message -> session created, streamed mock reply rendered as markdown + thinking.
// New sessions default to the server's cwd (tmp); only paths that exist when the reply is stored become links.
fs.mkdirSync(path.join(tmp, "src"), { recursive: true });
fs.writeFileSync(path.join(tmp, "src", "a.js"), "1\n2\n3\n");
await waitFor(p1, (dir) => document.querySelector("#new-cwd") && document.querySelector("#new-cwd").value === dir, "new-session cwd defaults to the server's cwd", 10000, tmp);
await typeInto(p1, ".composer textarea", "Hello there, please look at `src/a.js:3` and `src/missing.js`");
await p1.keyboard.press("Enter");
await waitFor(p1, () => location.hash.startsWith("#session/"), "navigated to the new session");
sid = await p1.evaluate(() => decodeURIComponent(location.hash.slice(9)));
await waitFor(p1, () => [...document.querySelectorAll(".msgs .msg.assistant .md strong")].some((e) => e.textContent === "Mock reply"),
  "markdown-rendered mock reply");
await waitFor(p1, () => [...document.querySelectorAll(".msgs .thinking")].some((e) => e.textContent.includes("Considering")), "thinking block");
const thinkingFont = await p1.$eval(".msgs .thinking-body", (e) => getComputedStyle(e).fontStyle);
if (thinkingFont !== "italic") await fail("thinking block not in a distinct style: " + thinkingFont);
// thinking stays collapsed while the model reasons: only its summary (label + preview) shows
if (await p1.evaluate(() => [...document.querySelectorAll(".msgs .thinking")].some((e) => e.open))) await fail("a thinking block opened by itself");
await waitFor(p1, (abs) => document.querySelector('.msgs .msg.assistant code.local-path[data-local-path="src/a.js"][data-line="3"][data-abs="' + abs + '"]'),
  "existing inline-code path marked as local", 10000, path.join(tmp, "src", "a.js"));
if (await p1.evaluate(() => !!document.querySelector('.msgs code.local-path[data-local-path="src/missing.js"]'))) await fail("a path that does not exist is clickable");
step("streamed reply rendered (markdown, thinking, local path link)");
// the mock reports a cost: the session total shows in the bottom-right corner
await waitFor(p1, () => /^\$[\d.]+ · \d+ in · \d+ out$/.test(document.querySelector(".chat-foot .session-cost")?.textContent || ""), "session cost shown");
step("session cost in the bottom-right corner");

await waitFor(p1, (id) => {
  const a = document.querySelector('#sb-list a[data-sid="' + id + '"]');
  return a && a.querySelector("svg") && /hello/i.test(a.textContent);
}, "title + identicon in sidebar", 10000, sid);
await waitFor(p1, () => /hello/i.test(document.title) && document.querySelector('link[rel="icon"]').href.startsWith("data:image/svg+xml"),
  "document title + identicon favicon");
step("title appears in sidebar; favicon + document title set");

// 4. local file link -> fs.resolve -> (file deleted since) error toast
fs.rmSync(path.join(tmp, "src", "a.js"));
await p1.click('.msgs .msg.assistant code.local-path[data-local-path="src/a.js"]');
await waitFor(p1, () => [...document.querySelectorAll(".toast")].some((t) => t.textContent.includes("No longer exists")), "toast for a file deleted since");
step("local path click resolves via fs.resolve");

// 4b. folder picker (click the cwd in the header) and model picker (click the model badge)
await p1.click(".chat-head .cwd-btn");
await waitFor(p1, () => document.querySelector(".folder-picker .fp-item") && document.querySelector(".folder-picker .fp-path").value.length > 1, "folder picker lists folders");
await p1.keyboard.press("Escape");
await waitFor(p1, () => !document.querySelector(".modal-back"), "folder picker closes on Esc");
await p1.click(".chat-head .model-btn");
await waitFor(p1, () => [...document.querySelectorAll(".model-picker .mp-item")].some((b) => b.getAttribute("data-model") === "mock"), "model picker lists the provider's models");
await p1.keyboard.press("Escape");
await waitFor(p1, () => !document.querySelector(".modal-back"), "model picker closes on Esc");
step("folder picker and model picker open and close");
if (process.env.TERM_AT === "4") await terminalStep();

// 5. a second page on the same session mirrors a slow streaming reply live
p2 = await newPage("#session/" + sid);
await waitFor(p2, () => window.SFHApp.current && !window.SFHApp.current.loading && document.querySelectorAll(".msgs .msg.assistant").length >= 1, "second page loaded the session");
await typeInto(p1, ".composer textarea", "[[slow]] stream this slowly");
await p1.keyboard.press("Enter");
await waitFor(p2, () => document.querySelector(".draft-host .msg.draft") && document.querySelector(".chat-head .badge.running"), "page 2 sees the live draft");
// while running, the favicon cycles "working" frames (identicon + a segment travelling around the edge)
const favs = new Set();
for (let i = 0; i < 8; i++) { favs.add(await p2.evaluate(() => document.querySelector('link[rel="icon"]').href)); await sleep(70); }
const spinning = [...favs].filter((h) => decodeURIComponent(h).includes("stroke-dasharray"));
if (spinning.length < 3) await fail("favicon does not animate while running: " + spinning.length + " distinct working frames");
step("favicon animates while the session runs");
await waitFor(p2, () => /Mock/.test(document.querySelector(".draft-host")?.textContent || ""), "page 2 sees streamed draft text", 15000);
const stillRunning = await p2.evaluate(() => !!document.querySelector(".chat-head .badge.running"));
if (!stillRunning) await fail("draft text only appeared after the turn finished (not live)");
await waitFor(p2, () => document.querySelectorAll(".msgs .msg.assistant").length >= 2 && !document.querySelector(".draft-host .msg"), "page 2 sees the final message", 30000);
await waitFor(p1, () => document.querySelectorAll(".msgs .msg.assistant").length >= 2 && !document.querySelector(".chat-head .badge.running"), "page 1 finished");
await waitFor(p1, () => !decodeURIComponent(document.querySelector('link[rel="icon"]').href).includes("stroke-dasharray"), "favicon stops animating when idle", 3000);
step("second page mirrored the streaming reply live");

// 6. stop a running turn with Esc
await typeInto(p1, ".composer textarea", "[[slow]] stop me please");
await p1.keyboard.press("Enter");
await waitFor(p1, () => document.querySelector(".chat-head .badge.running"), "turn running");
await sleep(400);
await p1.keyboard.press("Escape");
await waitFor(p1, () => !document.querySelector(".chat-head .badge.running"), "Esc stopped the turn", 5000);
await waitFor(p2, () => !document.querySelector(".chat-head .badge.running"), "page 2 sees the stop", 5000);
step("Esc stops a running turn");

// 7. edit the first user message -> everything after it is replaced
await p1.evaluate(() => [...document.querySelector(".msgs .msg.user").querySelectorAll(".link-btn")].find((b) => b.textContent === "Edit").click());
await waitFor(p1, () => document.querySelector(".edit-box textarea") === document.activeElement, "inline editor focused");
await p1.evaluate(() => { const t = document.querySelector(".edit-box textarea"); t.value = ""; });
await typeInto(p1, ".edit-box textarea", "Edited question");
await p1.keyboard.press("Enter");
const editedOk = () => {
  const users = [...document.querySelectorAll(".msgs .msg.user .bubble")].map((b) => b.textContent);
  const reply = [...document.querySelectorAll(".msgs .msg.assistant .md")].map((e) => e.textContent).join(" ");
  return users.length === 1 && users[0] === "Edited question" && reply.includes("to: Edited question") && !document.querySelector(".draft-host .msg");
};
await waitFor(p1, editedOk, "edited conversation on page 1", 15000);
await waitFor(p2, editedOk, "edited conversation mirrored on page 2", 15000);
step("edit + resend truncates and regenerates (mirrored)");

// 8. ArrowUp in an empty composer edits the last user message
await p1.focus(".composer textarea");
await p1.keyboard.press("ArrowUp");
await waitFor(p1, () => document.querySelector(".edit-box textarea") === document.activeElement, "ArrowUp opens inline edit");
await p1.keyboard.press("Escape");
await waitFor(p1, () => !document.querySelector(".edit-box"), "Esc cancels inline edit");
step("ArrowUp edits last message; Esc cancels");

// 9. slash command popup + a server-side command (notice)
await typeInto(p1, ".composer textarea", "/he");
await waitFor(p1, () => !document.querySelector(".cmd-pop").hidden && document.querySelector(".cmd-pop .item.sel")?.textContent.includes("/help"), "command popup");
await p1.keyboard.press("Enter"); // accept completion
await p1.keyboard.press("Enter"); // send
await waitFor(p1, () => [...document.querySelectorAll(".notice")].some((n) => n.textContent.includes("Commands")), "/help notice");
step("slash command completion + notice");

// 9b. context gauge + /compact: a collapsible summary card, earlier messages dimmed, the gauge estimates
await waitFor(p1, () => /^context [\d.]+k? \/ 200k \(\d+%\)$/.test(document.querySelector(".chat-foot .session-ctx")?.textContent || ""), "context gauge shown");
await typeInto(p1, ".composer textarea", "/compact");
await p1.keyboard.press("Escape"); // close the command popup
await p1.keyboard.press("Enter");
await waitFor(p1, () => document.querySelector(".msgs .compaction-wrap details.compaction") && document.querySelector(".msgs .msg.pre-compact"), "compaction card", 15000);
await waitFor(p1, () => (document.querySelector(".chat-foot .session-ctx")?.textContent || "").startsWith("context ~"), "context estimated after /compact");
step("context gauge; /compact shows a summary card");

// 9b. subagent threads (v17): the spawn card links to the child thread, its <thread-message> report comes back
// as a card, and the sidebar tree lists the thread under the session (below a pinned ★ main entry)
await p1.bringToFront();
await typeInto(p1, ".composer textarea", "[[subagent:child task here]]");
await p1.keyboard.press("Enter");
await waitFor(p1, (s) => [...document.querySelectorAll(".msgs .tool")].some((t) => t.querySelector(".tname")?.textContent === "spawn" &&
  /mock task/.test(t.querySelector(".tsum")?.textContent || "") && (t.querySelector("a.sub-link")?.getAttribute("href") || "").startsWith("#session/" + encodeURIComponent(s) + "/")),
  "spawn tool card with a link to the child thread", 15000, sid);
const rootBid = await p1.evaluate(() => window.SFHApp.current.branchId);
const childBid = await p1.evaluate(() => decodeURIComponent([...document.querySelectorAll(".msgs .tool a.sub-link")].pop().getAttribute("href").split("/").pop()));
if (!childBid || childBid === rootBid) await fail("bad child thread id: " + childBid);
await waitFor(p1, (s, b) => {
  const card = document.querySelector(".msgs .msg.subresult .branch-result-card");
  return card && /Subagent report/.test(card.textContent) && /completed/.test(card.textContent) &&
    /child task here/.test(card.querySelector(".md")?.textContent || "") && card.querySelector('a.branch-jump[href="#session/' + encodeURIComponent(s) + "/" + encodeURIComponent(b) + '"]');
}, "subagent report card", 20000, sid, childBid);
const srEdit = await p1.evaluate(() => [...document.querySelectorAll(".msgs .msg.subresult button")].some((b) => b.textContent === "Edit") ||
  [...document.querySelectorAll(".msgs .msg.user .bubble")].some((b) => b.textContent.includes("<thread-message")));
if (srEdit) await fail("subagent report rendered as an editable user bubble");
await waitFor(p1, () => !document.querySelector(".chat-head .badge.running"), "parent idle after the report turn", 15000);
await waitFor(p1, (s, b, r) => document.querySelector('#sb-list li.thread-item:not(.pinned-main) a[data-sid="' + s + '"][data-bid="' + b + '"]') &&
  document.querySelector('#sb-list li.pinned-main a[data-sid="' + s + '"][data-bid="' + r + '"]'), "child thread + pinned main entry in the sidebar tree", 8000, sid, childBid, rootBid);
await p1.evaluate((p) => document.querySelector('#sb-list a[data-sid="' + p + '"]:not([data-bid])').parentElement.querySelector(".tree-tog").click(), sid);
await waitFor(p1, (s, b) => !document.querySelector('#sb-list a[data-sid="' + s + '"][data-bid="' + b + '"]'), "collapse hides the threads", 5000, sid, childBid);
await p1.evaluate((p) => document.querySelector('#sb-list a[data-sid="' + p + '"]:not([data-bid])').parentElement.querySelector(".tree-tog").click(), sid);
await waitFor(p1, (s, b) => document.querySelector('#sb-list a[data-sid="' + s + '"][data-bid="' + b + '"]'), "expand shows the threads", 5000, sid, childBid);
step("subagent thread: spawn card link, report card, sidebar thread tree with pinned main");

// 9b2. spatial view (v18): children on the right / the parent on the left, beside their fork message; the mode
// follows the free width (full preview card, title, node with a hover card), the parent degrading first
await p1.setViewport({ width: 1900, height: 900 });
await waitFor(p1, (b) => {
  const a = document.querySelector('.spatial .sv-group.sv-right a.sv-chip.sv-full[data-branch-id="' + b + '"]');
  return a && /child task here/.test(a.querySelector(".sv-prompt")?.textContent || "") && a.classList.contains("is-fresh") && a.querySelector(".sv-reply");
}, "wide: full preview card for the child", 8000, childBid);
const geo = await p1.evaluate((b) => {
  const a = document.querySelector('.spatial a[data-branch-id="' + b + '"]').getBoundingClientRect(), m = document.querySelector(".msgs").getBoundingClientRect();
  return { cardLeft: a.left, msgsRight: m.right };
}, childBid);
if (geo.cardLeft < geo.msgsRight) await fail("child preview overlaps the conversation: " + JSON.stringify(geo));
await p1.setViewport({ width: 1200, height: 850 });
await waitFor(p1, (b) => document.querySelector('.spatial .sv-group.sv-right.mode-node a.sv-node[data-branch-id="' + b + '"] .sv-pop'), "narrow: a node with a hover card", 8000, childBid);
await p1.setViewport({ width: 1900, height: 900 });
await waitFor(p1, (b) => document.querySelector('.spatial a.sv-full[data-branch-id="' + b + '"]'), "wide again", 8000, childBid);
await p1.evaluate((b) => document.querySelector('.spatial a.sv-chip[data-branch-id="' + b + '"]').click(), childBid);
await waitFor(p1, (s, b, r) => location.hash === "#session/" + encodeURIComponent(s) + "/" + encodeURIComponent(b) && window.SFHApp.current.branchId === b &&
  document.querySelector('.spatial .sv-group.sv-left a.sv-chip[data-branch-id="' + r + '"]') && document.querySelector(".chat-inner.sv-in-right"),
  "child opened (slides in from the right) with the parent on the left", 8000, sid, childBid, rootBid);
await p1.evaluate((r) => document.querySelector('.spatial .sv-left a.sv-chip[data-branch-id="' + r + '"]').click(), rootBid);
await waitFor(p1, (r) => window.SFHApp.current.branchId === r && !window.SFHApp.current.loading && document.querySelector(".chat-inner.sv-in-left"),
  "back at the parent (slides in from the left)", 8000, rootBid);
// an aside: inherited history starts collapsed; expanding it is stored per thread; the parent link stays
await typeInto(p1, ".composer textarea", "/aside detour question");
await p1.keyboard.press("Enter");
await waitFor(p1, (r) => {
  const c = window.SFHApp.current;
  return c.branchId !== r && !c.loading && c.messages.some((m) => m.role === "assistant" && JSON.stringify(m.content).includes("to: detour question")) &&
    document.querySelector(".msgs > .inherited[hidden]") && /earlier message/.test(document.querySelector(".msgs > .inherit-bar")?.textContent || "");
}, "aside with collapsed inherited history", 15000, rootBid);
const asideBid = await p1.evaluate(() => window.SFHApp.current.branchId);
await waitFor(p1, (r) => document.querySelector('.spatial .sv-left a.sv-chip[data-branch-id="' + r + '"]'), "aside: parent on the left", 8000, rootBid);
await p1.click(".msgs > .inherit-bar");
await waitFor(p1, (r) => document.querySelector(".msgs > .inherited:not([hidden]) .msg") && document.querySelector('.spatial .sv-left a.sv-chip[data-branch-id="' + r + '"]'),
  "expanded inherited history keeps the parent link", 5000, rootBid);
await waitFor(p1, async (s, b) => (await window.SFHApp.rpc("branches.list", { session_id: s })).branches.find((x) => x.id === b)?.expanded === true,
  "expanded state stored on the thread", 5000, sid, asideBid);
// ★ main is a tag: promoting moves the pinned entry and the star, nothing else; then move it back
const threadKinds = (s) => p1.evaluate(async (i) => (await window.SFHApp.rpc("branches.list", { session_id: i })).branches.map((b) => b.id + ":" + b.kind).join(","), s);
const kinds0 = await threadKinds(sid);
await p1.evaluate(() => [...document.querySelectorAll(".branch-origin-div button")].find((b) => /Set as main/.test(b.textContent)).click());
await waitFor(p1, (s, b) => document.querySelector('#sb-list li.pinned-main a[data-sid="' + s + '"][data-bid="' + b + '"]') &&
  /★/.test(document.querySelector(".chat-head .branch-btn")?.textContent || ""), "promoted aside: pinned ★ entry + header star", 8000, sid, asideBid);
const kinds1 = await threadKinds(sid);
if (kinds1 !== kinds0 || !/:aside$/.test(kinds1)) await fail("promotion changed thread kinds/order: " + kinds0 + " -> " + kinds1);
await p1.evaluate((s, r) => window.SFHApp.rpc("branches.promote", { session_id: s, branch_id: r }), sid, rootBid);
await waitFor(p1, (s, r) => document.querySelector('#sb-list li.pinned-main a[data-sid="' + s + '"][data-bid="' + r + '"]'), "main tag back on the root", 8000, sid, rootBid);
// more than 2 threads at one message: 2 stacked + "+N" expanding in place
const forkAt = await p1.evaluate(() => { const c = window.SFHApp.current, a = c.branches.branches.find((b) => b.id === c.branchId); return a.origin_id; });
await p1.evaluate(async (s, r, at) => {
  for (const t of ["second detour", "third detour"])
    await window.SFHApp.rpc("branches.create", { session_id: s, from_message_id: at, title: t, switch: false, parent_branch_id: r });
}, sid, rootBid, forkAt);
await p1.evaluate((r) => document.querySelector('.spatial .sv-left a.sv-chip[data-branch-id="' + r + '"]').click(), rootBid);
await waitFor(p1, (r, at) => window.SFHApp.current.branchId === r && document.querySelectorAll('.spatial .sv-group[data-fork-at="' + at + '"] a.sv-chip').length === 2 &&
  document.querySelector('.spatial .sv-group[data-fork-at="' + at + '"] .sv-more')?.textContent === "+1", "2 stacked + “+1”", 8000, rootBid, String(forkAt));
const stackOrder = await p1.evaluate((at) => [...document.querySelectorAll('.spatial .sv-group[data-fork-at="' + at + '"] .sv-name')].map((e) => e.textContent).join("|"), String(forkAt));
if (!/^[^|]+\|second detour$/.test(stackOrder)) await fail("stack not in creation order: " + stackOrder);
await p1.evaluate((at) => document.querySelector('.spatial .sv-group[data-fork-at="' + at + '"] .sv-more').click(), String(forkAt));
await waitFor(p1, (at) => document.querySelectorAll('.spatial .sv-group[data-fork-at="' + at + '"] a.sv-chip').length === 3, "“+1” expands the stack in place", 5000, String(forkAt));
// mid width: the right side shows titles while the parent side (needs more room) already degrades to a node
await p1.evaluate((b) => { location.hash = "#session/" + encodeURIComponent(window.SFHApp.current.id) + "/" + encodeURIComponent(b); }, asideBid);
await waitFor(p1, (b) => window.SFHApp.current.branchId === b && !window.SFHApp.current.loading, "aside again", 8000, asideBid);
await p1.setViewport({ width: 1400, height: 850 });
await waitFor(p1, () => document.querySelector(".spatial .sv-group.sv-left.mode-node a.sv-node"), "mid width: the parent degrades to a node first", 8000);
await p1.setViewport({ width: 1200, height: 850 });
await p1.evaluate((r) => { location.hash = "#session/" + encodeURIComponent(window.SFHApp.current.id) + "/" + encodeURIComponent(r); }, rootBid);
await waitFor(p1, (r) => window.SFHApp.current.branchId === r && !window.SFHApp.current.loading, "back on the root thread", 8000, rootBid);
step("spatial view: modes by width, sideways navigation, collapsed history, ★ main tag, +N stack");

// 9b3. minimap: real mouse clicks open the sidebar and ⇥ closes it again (the click must not bubble into the
// freshly re-rendered minimal strip, whose own click handler would re-expand it)
await p1.evaluate(() => { if (window.SFHApp.current.minimapExpanded) document.querySelector(".branch-minimap.expanded .bm-head .icon-btn").click(); });
await waitFor(p1, () => document.querySelector(".branch-minimap.minimal:not([hidden])"), "minimal minimap", 5000);
await p1.click(".branch-minimap.minimal");
await waitFor(p1, () => document.querySelector(".branch-minimap.expanded .bm-head .icon-btn"), "minimap expanded by a click", 5000);
// git graph: a row per kept node, the viewed thread's tip marked, a node on another thread opens that thread
await waitFor(p1, () => {
  const g = window.SFHApp.current.branches.graph, rows = document.querySelectorAll(".branch-minimap .bm-row[data-node]");
  return g && rows.length === g.nodes.length && document.querySelectorAll(".branch-minimap .bm-node").length === g.nodes.length &&
    document.querySelector(".branch-minimap .bm-row.active") && document.querySelector(".branch-minimap .bm-here");
}, "graph rows match the server's nodes", 5000);
if (process.env.SHOT) await p1.screenshot({ path: process.env.SHOT });
const other = await p1.evaluate(() => {
  const c = window.SFHApp.current, n = c.branches.graph.nodes.find((x) => x.mid != null && x.thread !== c.branchId && !c.messages.some((m) => m.id === x.mid));
  return n && { id: n.id, thread: n.thread, mid: n.mid };
});
if (!other) await fail("no graph node on another thread");
await p1.click('.branch-minimap .bm-row[data-node="' + other.id + '"]');
await waitFor(p1, (o) => window.SFHApp.current.branchId === o.thread && !window.SFHApp.current.loading &&
  document.querySelector('.msgs [data-mid="' + o.mid + '"].bm-flash'), "node click opens its thread at the message", 8000, other);
const first = await p1.evaluate(() => window.SFHApp.current.branches.graph.nodes[0].id);
await p1.click('.branch-minimap .bm-row[data-node="' + first + '"]');
await waitFor(p1, (o, f) => window.SFHApp.current.branchId === o.thread &&
  document.querySelector('.msgs [data-mid="' + f.slice(1) + '"].bm-flash'), "a node on the viewed thread just scrolls", 8000, other, first);
await p1.evaluate((r) => { location.hash = "#session/" + encodeURIComponent(window.SFHApp.current.id) + "/" + encodeURIComponent(r); }, rootBid);
await waitFor(p1, (r) => window.SFHApp.current.branchId === r && !window.SFHApp.current.loading, "back on the root thread", 8000, rootBid);
await p1.click(".branch-minimap.expanded .bm-head .icon-btn");
await new Promise((r) => setTimeout(r, 150));
await waitFor(p1, () => document.querySelector(".branch-minimap.minimal") && !window.SFHApp.current.minimapExpanded, "⇥ minimizes the sidebar", 5000);
step("minimap: git graph rows, node clicks switch thread or scroll, ⇥ minimizes");

// 9c. peer sessions: a second session in the same directory asks this one a question (held: it is the
// user's idle session), the peer strip + inbox show it, "Let it answer" delivers it; a shared file is flagged
await typeInto(p1, ".composer textarea", '[[tool:write:{"path": "peer-file.txt", "content": "x"}]]');
await p1.keyboard.press("Enter");
await waitFor(p1, () => [...document.querySelectorAll(".msgs .tool .tname")].some((e) => e.textContent === "write") && !document.querySelector(".chat-head .badge.running"), "write tool ran", 15000);
const peerId = await p1.evaluate(async (p) => {
  const app = window.SFHApp, me = app.state.sessions.get(p);
  const b = await app.rpc("sessions.create", { cwd: me.cwd });
  await app.rpc("chat.send", { session_id: b.id, text: '[[tool:send_message:{"session_id": "' + p + '", "text": "Are you done with peer-file.txt?"}]]' });
  return b.id;
}, sid);
await waitFor(p1, (b) => document.querySelector('.peer-strip:not([hidden]) a.peer-chip[href="#session/' + encodeURIComponent(b) + '"]'), "peer strip shows the other session", 15000, peerId);
await waitFor(p1, (b) => {
  const card = document.querySelector(".inbox-host .inbox-card");
  return card && /Are you done with peer-file\.txt\?/.test(card.textContent) && card.querySelector('a.sub-link[data-child="' + b + '"]') &&
    [...card.querySelectorAll("button")].some((x) => x.textContent === "Let it answer");
}, "held question shown above the composer", 15000, peerId);
await waitFor(p1, (p) => document.querySelector('#sb-list a[data-sid="' + p + '"] .inbox-count.q')?.textContent === "?1", "sidebar question badge", 5000, sid);
await p1.evaluate(() => [...document.querySelectorAll(".inbox-card button")].find((x) => x.textContent === "Let it answer").click());
await waitFor(p1, () => {
  const c = document.querySelector(".msgs .msg.peer .peer-card.k-question");
  return c && /Question from/.test(c.textContent) && /peer-file\.txt/.test(c.textContent) && !document.querySelector(".inbox-host .inbox-card");
}, "question delivered as a peer card", 15000);
await waitFor(p1, () => !document.querySelector(".chat-head .badge.running"), "answer turn finished", 15000);
await p1.evaluate((b) => window.SFHApp.rpc("chat.send", { session_id: b, text: '[[tool:edit:{"path": "peer-file.txt", "old_string": "x", "new_string": "y"}]]' }), peerId);
await waitFor(p1, () => /also works on\s*\S*peer-file\.txt/.test(document.querySelector(".peer-strip .peer-conflict")?.textContent || ""), "conflict line for the shared file", 15000);
const peerBubble = await p1.evaluate(() => [...document.querySelectorAll(".msgs .msg.user .bubble")].some((b) => /peer-message|peer-sessions/.test(b.textContent)));
if (peerBubble) await fail("peer message rendered as a user bubble");
step("peer sessions: strip, held question + Let it answer, peer card, shared-file conflict");

// 10. sessions grid
await p1.evaluate(() => { location.hash = "#sessions"; });
await waitFor(p1, (id) => document.querySelector('.card[data-sid="' + id + '"] svg') && document.querySelector(".card .meta"), "sessions grid card", 5000, sid);
const cardText = await p1.$eval(".card", (e) => e.textContent);
if (!/message/.test(cardText)) await fail("card lacks message count: " + cardText);
const nThreads = await p1.evaluate(async (s) => (await window.SFHApp.rpc("branches.list", { session_id: s })).branches.length, sid);
await waitFor(p1, (p, n) => (document.querySelector('.card[data-sid="' + p + '"]')?.textContent || "").includes("⑂" + n + " branches"), "thread count on the grid card", 5000, sid, nThreads);
step("sessions grid (with the thread count)");

// 11. settings round trip (visible on the other page via settings.updated)
await p1.evaluate(() => { location.hash = "#settings"; });
await waitFor(p1, () => document.querySelector("#set-model") && document.querySelector(".sysprompt").value.length > 20, "settings form with system prompt");
await p1.evaluate(() => { const i = document.querySelector("#set-model"); i.value = "test-model-x"; i.dispatchEvent(new Event("change")); });
await waitFor(p2, () => window.SFHApp.state.settings.model === "test-model-x", "settings.updated reached page 2");
await waitFor(p1, () => document.querySelector("#creds-host .creds-add"), "credentials section");
await typeInto(p1, "#creds-host input[type=text]", "SFH_TEST_SECRET");
await typeInto(p1, "#creds-host input[type=password]", "hunter2");
await p1.keyboard.press("Enter");
await waitFor(p1, () => [...document.querySelectorAll(".creds-list li")].some((l) => l.textContent.includes("SFH_TEST_SECRET")), "credential listed");
const credFile = path.join(tmp, "config", "singlefile", "credentials");
if (!fs.existsSync(credFile) || !fs.readFileSync(credFile, "utf8").includes("SFH_TEST_SECRET=hunter2")) await fail("credential not written to " + credFile);
const pageHtml = await p1.content();
if (pageHtml.includes("hunter2")) await fail("credential value leaked into the page");
step("settings round trip; credential stored in XDG file, not shown");

// 11b. working directory defaults: the last new-session cwd wins; default_cwd is only the fallback;
// recent directories are listed most recently used first
const wd = path.join(tmp, "wd");
fs.mkdirSync(wd);
await p1.evaluate((v) => { const i = document.querySelector("#set-default_cwd"); i.value = v; i.dispatchEvent(new Event("change")); }, wd);
await waitFor(p1, (v) => window.SFHApp.state.settings.default_cwd === v, "default_cwd saved", 5000, wd);
let info = await p1.evaluate(() => window.SFHApp.rpc("cwd.info"));
if (info.default !== tmp) await fail("default cwd should stay the last new-session cwd, got " + info.default);
const made = await p1.evaluate((v) => window.SFHApp.rpc("sessions.create", { cwd: v }), wd);
info = await p1.evaluate(() => window.SFHApp.rpc("cwd.info"));
if (info.default !== wd || info.recent[0].path !== wd) await fail("last new-session cwd not remembered: " + JSON.stringify(info));
await p1.evaluate((id) => window.SFHApp.rpc("sessions.delete", { session_id: id }), made.id);
await p1.evaluate(() => window.SFHApp.rpc("settings.set", { key: "default_cwd", value: null }));
step("cwd defaults: last new-session cwd, recent list");

// 11c. providers: add a configured provider (key -> credential), it becomes active under auto, then delete it
await waitFor(p1, () => document.querySelector("#providers-host #prov-add"), "providers section");
await p1.bringToFront();
await p1.click("#prov-add");
await waitFor(p1, () => document.querySelector(".prov-form"), "provider form");
await typeInto(p1, '.prov-form input[name="id"]', "local");
await p1.select('.prov-form select[name="type"]', "openai");
await typeInto(p1, '.prov-form input[name="base_url"]', "http://127.0.0.1:9/v1");
await typeInto(p1, '.prov-form input[name="model"]', "local-model-1");
await typeInto(p1, '.prov-form input[name="api_key"]', "sk-local-secret");
await p1.evaluate(() => [...document.querySelectorAll(".prov-form .btn")].find((b) => b.textContent === "Save provider").click());
await waitFor(p1, () => { const li = document.querySelector('.prov-list li[data-provider="local"]'); return li && /local-model-1/.test(li.textContent) && !/no key/.test(li.textContent); }, "configured provider listed with key", 8000);
if (!fs.readFileSync(credFile, "utf8").includes("PROVIDER_LOCAL_KEY=sk-local-secret")) await fail("provider key not stored as credential");
const provSetting = await p1.evaluate(() => window.SFHApp.state.settings.providers);
if (!provSetting?.[0] || provSetting[0].api_key_credential !== "PROVIDER_LOCAL_KEY" || JSON.stringify(provSetting).includes("sk-local")) await fail("providers setting wrong: " + JSON.stringify(provSetting));
await waitFor(p1, () => [...document.querySelectorAll("#set-provider option")].some((o) => o.value === "local"), "provider selector offers the new id");
await waitFor(p2, () => /local/.test(document.querySelector(".chat-head .badge.provider")?.textContent || ""), "chat header on page 2 shows the active provider", 8000);
await waitFor(p1, () => document.querySelector('.prov-list li[data-provider="local"] .badge.active'), "provider marked active", 8000);
dialogPolicy.push([/Delete provider local/, true]);
await p1.evaluate(() => [...document.querySelectorAll('.prov-list li[data-provider="local"] .btn')].find((b) => b.textContent === "Delete").click());
await waitFor(p1, () => !document.querySelector('.prov-list li[data-provider="local"]'), "provider deleted", 8000);
await waitFor(p2, () => /mock/.test(document.querySelector(".chat-head .badge.provider")?.textContent || ""), "header back to mock", 8000);
if (fs.readFileSync(credFile, "utf8").includes("PROVIDER_LOCAL_KEY")) await fail("provider key not removed");
step("providers settings round trip (add with key, active badge, delete)");

// 11d. model overrides: browse the provider's models, set a smaller context window, the gauge follows, remove it
await waitFor(p1, () => document.querySelector('#overrides-host .ov-models li[data-model="mock"] .ov-input'), "overrides browser lists the mock model", 8000);
await p1.evaluate(() => { const i = document.querySelector('#overrides-host .ov-models li[data-model="mock"] .ov-input'); i.value = "50k"; i.dispatchEvent(new Event("change")); });
await waitFor(p1, () => window.SFHApp.state.settings.model_overrides?.mock?.mock?.context_window === 50000, "override saved", 5000);
await waitFor(p1, () => document.querySelector('#overrides-host .ov-current li[data-override="mock|mock"]'), "override listed");
await waitFor(p2, () => / \/ 50k/.test(document.querySelector(".session-ctx")?.textContent || ""), "context gauge on page 2 uses the override", 8000);
await p1.evaluate(() => [...document.querySelectorAll('#overrides-host .ov-current li[data-override="mock|mock"] .btn')].find((b) => b.textContent === "Remove").click());
await waitFor(p1, () => !document.querySelector('#overrides-host .ov-current li[data-override]') && !window.SFHApp.state.settings.model_overrides?.mock, "override removed", 5000);
await waitFor(p2, () => / \/ 200k/.test(document.querySelector(".session-ctx")?.textContent || ""), "gauge back to the provider's window", 8000);
step("model overrides: set from the model list, gauge follows, remove");
} else {
  // PART=2 starts from what steps 3-11 leave behind, made over RPC: a session whose first message was edited
  // (two versions), the model setting changed, and a second page showing the session
  const replied = (id, text) => waitFor(p1, async (i, t) => {
    const r = await window.SFHApp.rpc("sessions.subscribe", { session_id: i }), last = r.messages[r.messages.length - 1];
    return last && last.role === "assistant" && last.content.some((b) => b.type === "text" && b.text.includes("to: " + t)) &&
      window.SFHApp.state.sessions.get(i)?.status === "idle";
  }, "reply to " + text, 15000, id, text);
  sid = await p1.evaluate(async (cwd) => (await window.SFHApp.rpc("sessions.create", { cwd })).id, tmp);
  await p1.evaluate((id) => window.SFHApp.rpc("chat.send", { session_id: id, text: "Original question" }), sid);
  await replied(sid, "Original question");
  await p1.evaluate(async (id) => {
    const first = (await window.SFHApp.rpc("sessions.subscribe", { session_id: id })).messages.find((m) => m.role === "user");
    await window.SFHApp.rpc("chat.edit", { session_id: id, message_id: first.id, text: "Edited question" });
  }, sid);
  await replied(sid, "Edited question");
  await p1.evaluate(() => window.SFHApp.rpc("settings.set", { key: "model", value: "test-model-x" }));
  p2 = await newPage("#session/" + sid);
  await waitFor(p2, (id) => window.SFHApp.current?.id === id && !window.SFHApp.current.loading, "page 2 shows the session", 10000, sid);
}

if (PART !== "1") {
await p1.evaluate((id) => { location.hash = "#session/" + encodeURIComponent(id); }, sid);
await waitFor(p1, (id) => window.SFHApp.current?.id === id && !window.SFHApp.current.loading, "session reopened", 10000, sid);
if (!process.env.TERM_AT) await terminalStep();

// 13. restart the server -> both pages reconnect, data persisted in the html file
await stop(proc);
await waitFor(p1, () => !document.querySelector("#banner").hidden, "reconnecting banner", 8000);
proc = start();
await waitFor(p1, () => document.querySelector("#banner").hidden && window.SFHApp.state.connected, "page 1 reconnected", 20000);
await waitFor(p1, () => [...document.querySelectorAll(".msgs .msg.user .bubble")].some((b) => b.textContent === "Edited question"), "history after restart");
await waitFor(p2, () => window.SFHApp.state.connected && window.SFHApp.state.settings.model === "test-model-x", "page 2 reconnected; setting persisted", 20000);
await typeInto(p1, ".composer textarea", "after restart");
await p1.keyboard.press("Enter");
await waitFor(p2, () => [...document.querySelectorAll(".msgs .md")].some((e) => e.textContent.includes("to: after restart")), "re-subscribed after reconnect (page 2 sees new reply)", 15000);
step("restart: reconnect, re-subscribe, data persisted");

// 14. export link works and opens to the session
const port = await p1.evaluate(() => window.SFHApp.state.port);
const exportUrl = (tok) => `http://127.0.0.1:${port}/api/export?path=${encodeURIComponent(file)}&view=session:${sid}&token=${encodeURIComponent(tok)}`;
const noTok = await fetch(exportUrl(""));
if (noTok.status !== 401) await fail("export without token should be 401, got " + noTok.status);
const exp = await fetch(exportUrl(token1));
const expText = await exp.text();
if (exp.status !== 200 || !expText.includes("sfh-data") || expText.includes("hunter2")) await fail("export failed: " + exp.status);
if (!/<meta name="sfh-token" content="">/.test(expText)) await fail("export must carry an empty token");
step("export endpoint (token required, copy has no token)");

// 15. offline read-only view: stop server, reload -> snapshot from the file; start -> live again
await p2.close();
await stop(proc);
await p1.reload();
await waitFor(p1, () => document.querySelector("#app.offline") && !document.querySelector("#app").hidden, "offline view shown", 10000);
await waitFor(p1, (id) => document.querySelector('#sb-list a[data-sid="' + id + '"]'), "offline sidebar lists the session", 5000, sid);
await waitFor(p1, () => /Offline/.test(document.querySelector("#banner").textContent) && document.querySelector("#banner code")?.textContent.startsWith("sh '"), "offline banner with command");
await p1.evaluate((id) => { location.hash = "#session/" + encodeURIComponent(id); }, sid);
await waitFor(p1, () => [...document.querySelectorAll(".msgs .msg.user .bubble")].some((b) => b.textContent === "after restart") &&
  [...document.querySelectorAll(".msgs .md")].some((e) => e.textContent.includes("to: after restart")), "offline chat shows prior messages");
const ro = await p1.evaluate(() => ({ composer: document.querySelector(".composer-wrap").hidden,
  edit: [...document.querySelectorAll(".msgs .link-btn")].some((b) => b.textContent === "Edit") }));
if (!ro.composer || ro.edit) await fail("offline view not read-only: " + JSON.stringify(ro));
await p1.evaluate(() => { location.hash = "#sessions"; });
await waitFor(p1, (id) => document.querySelector('.card[data-sid="' + id + '"]') && !document.querySelector(".card .del"), "offline sessions grid (no delete)", 5000, sid);
step("offline read-only view from the snapshot");
proc = start();
await waitFor(p1, () => window.SFHApp.state.connected && !document.querySelector("#app.offline") && document.querySelector("#banner").hidden, "offline page went live", 20000);
await p1.evaluate((id) => { location.hash = "#session/" + encodeURIComponent(id); }, sid);
await waitFor(p1, (id) => window.SFHApp.current?.id === id && !window.SFHApp.current.loading && !document.querySelector(".composer-wrap").hidden, "composer back when live", 10000, sid);
await waitFor(p1, () => document.querySelector(".msgs .msg.user .versions")?.textContent.includes("2 versions"), "edited message shows versions");
step("switched from offline to live; edit versions shown");

// 16. regenerate the token -> page shows the reload screen; reload works with the new token
const p3 = await newPage("#sessions");
await waitFor(p3, () => window.SFHApp.state.connected, "third page connected");
await p1.bringToFront(); // puppeteer clicks hang in background tabs
await p1.evaluate(() => { location.hash = "#settings"; });
await waitFor(p1, () => document.querySelector("#token-reveal"), "access token section");
await p1.click("#token-reveal");
await waitFor(p1, (t) => document.querySelector(".token-val").textContent === t, "token revealed", 5000, token1);
dialogPolicy.push([/new access token/, true]);
await p1.click("#token-regenerate");
for (const pg of [p1, p3]) await waitFor(pg, () => !document.querySelector("#gate").hidden && /changed/.test(document.querySelector("#gate-title").textContent), "token-changed screen", 8000);
const token2 = fileToken();
if (!token2 || token2 === token1) await fail("token not regenerated in the file");
if ((await fetch(exportUrl(token1))).status !== 401) await fail("old token still accepted");
await p3.close();
await p1.reload();
await waitFor(p1, () => window.SFHApp.state.connected && document.querySelector("#gate").hidden, "reloaded page connects with the new token", 20000);
step("regenerate token: reload screen, reload connects");

// 17. embedded credentials: export asks for confirmation and the server warns
await p1.evaluate(() => { location.hash = "#settings"; });
await waitFor(p1, () => document.querySelector('#creds-host input[value="embedded"]'), "credential storage radios");
dialogPolicy.push([/Store credentials inside/, true]);
await p1.click('#creds-host input[value="embedded"]');
await waitFor(p1, () => window.SFHApp.state.settings.credentials_storage === "embedded", "storage switched to embedded", 8000);
dialogPolicy.push([/CONTAIN your API keys/, false]);
await p1.evaluate(() => [...document.querySelectorAll(".settings .btn")].find((b) => b.textContent === "Export").click());
await sleep(300);
if (!/CONTAIN/.test(p1.lastDialog || "")) await fail("no export confirmation with embedded credentials");
const expE = await fetch(exportUrl(token2));
await expE.text();
if (!/embedded credentials/.test(expE.headers.get("x-sfh-warning") || "")) await fail("missing X-SFH-Warning header: " + expE.headers.get("x-sfh-warning"));
step("export with embedded credentials warns");

// 18. HTML in markdown + artifacts: the mock echoes the prompt, so the reply carries an inline <svg> and an
// ```artifact fence. Sent over RPC (no typing) in a fresh session so earlier steps' counts are untouched.
const artSid = (await p1.evaluate(() => window.SFHApp.rpc("sessions.create", {}))).id;
await p1.bringToFront(); // drafts paint on requestAnimationFrame, which background tabs pause
await p1.evaluate((id) => { location.hash = "#session/" + encodeURIComponent(id); }, artSid);
await waitFor(p1, (id) => window.SFHApp.current?.id === id && !window.SFHApp.current.loading, "artifact session opened", 10000, artSid);
await p1.evaluate((id) => {
  // record whether the open fence showed its placeholder in the streaming draft (an observer, not polling,
  // so a short-lived state is not missed)
  window.__sawPending = false;
  const mo = new MutationObserver(() => { if (document.querySelector(".draft-host .artifact.pending")) { window.__sawPending = true; mo.disconnect(); } });
  mo.observe(document.querySelector("#view"), { childList: true, subtree: true, characterData: true });
  const art = "<!doctype html><html><body><p id=x>artifact body</p>\n" + Array.from({ length: 12 }, (_, i) => "<p>line " + i + "</p>").join("\n") + "\n</body></html>";
  return window.SFHApp.rpc("chat.send", { session_id: id, text: 'Look:\n\n<svg width="50" height="20"><rect id="r" width="10" height="10" fill="red"/></svg> <b onclick="alert(1)">bold</b>\n\n```artifact title="Demo"\n' + art + "\n```\n\nafter" });
}, artSid);
await waitFor(p1, () => document.querySelector(".msgs .msg.assistant .artifact-frame > iframe") && !document.querySelector(".chat-head .badge.running"), "artifact iframe in the stored reply", 15000);
const art = await p1.evaluate(() => {
  const m = [...document.querySelectorAll(".msgs .msg.assistant")].pop(), f = m.querySelector(".artifact-frame > iframe");
  const b = [...m.querySelectorAll("b")].find((x) => x.textContent === "bold");
  return { rect: !!m.querySelector('svg rect#mdu-r[fill="red"]'), unprefixed: !!document.getElementById("r"), bold: b ? b.outerHTML : null,
    sandbox: f.getAttribute("sandbox"), cd: f.contentDocument === null, after: [...m.querySelectorAll("p")].some((p) => p.textContent === "after"), sawPending: window.__sawPending };
});
if (!art.rect || art.unprefixed) await fail("inline svg not rendered with an mdu- prefixed id: " + JSON.stringify(art));
if (art.bold !== "<b>bold</b>") await fail("event handler not stripped: " + art.bold);
if (art.sandbox !== "allow-scripts allow-popups allow-modals" || !art.cd) await fail("artifact iframe not sandboxed away from the page: " + JSON.stringify(art));
if (!art.after) await fail("text after the artifact fence missing");
if (!art.sawPending) await fail("no \"building artifact…\" placeholder while the fence was streaming");
step("HTML in markdown (prefixed svg ids, handlers stripped) and a sandboxed artifact (placeholder while streaming)");

// 19. sidebar: drag the right-edge handle (mouse → pointer events), clamped to 160px … 50% of the window;
// collapse/expand with the button; width + collapsed state survive a reload (localStorage); double-click
// resets. Then the tui_mouse checkbox in settings. The viewport is 1200px wide: max 600px.
await p1.bringToFront();
const sbW = () => p1.evaluate(() => Math.round(document.querySelector("#sidebar").getBoundingClientRect().width));
const sbStored = () => p1.evaluate(() => JSON.parse(localStorage.getItem("sfh:sbWidth")));
async function dragHandle(dx) {
  const b = await (await p1.$("#sb-resize")).boundingBox();
  const x = b.x + b.width / 2, y = b.y + 200;
  await p1.mouse.move(x, y);
  await p1.mouse.down();
  await p1.mouse.move(x + dx / 2, y, { steps: 3 });
  await p1.mouse.move(x + dx, y, { steps: 3 });
  await p1.mouse.up();
}
if (await p1.evaluate(() => document.querySelector("#app").classList.contains("sb-collapsed"))) await p1.click("#sb-toggle");
await waitFor(p1, () => document.querySelector("#sidebar").getBoundingClientRect().width === 264, "sidebar at its default width");
await dragHandle(100);
if ((await sbW()) !== 364 || (await sbStored()) !== 364) await fail("drag +100px: width " + (await sbW()) + ", stored " + (await sbStored()));
await dragHandle(2000);
if ((await sbW()) !== 600 || (await sbStored()) !== 600) await fail("not clamped to 50% of the window: " + (await sbW()));
await dragHandle(-2000);
if ((await sbW()) !== 160 || (await sbStored()) !== 160) await fail("not clamped to 160px: " + (await sbW()));
const ico = await p1.evaluate(() => {
  const i = document.querySelector(".sb-list a .ico svg").getBoundingClientRect(), sb = document.querySelector("#sidebar").getBoundingClientRect();
  return { w: Math.round(i.width), h: Math.round(i.height), inside: i.left >= sb.left && i.right <= sb.right };
});
if (ico.w !== 16 || ico.h !== 16 || !ico.inside) await fail("identicon at the minimum width: " + JSON.stringify(ico));
await p1.focus("#sb-resize");
await p1.keyboard.press("ArrowRight");
await waitFor(p1, () => document.querySelector("#sidebar").getBoundingClientRect().width === 176 && localStorage.getItem("sfh:sbWidth") === "176", "ArrowRight on the focused handle widens by 16px");
if (await p1.evaluate(() => getSelection().toString())) await fail("dragging selected text");
await p1.click("#sb-toggle");
await waitFor(p1, () => document.querySelector("#app").classList.contains("sb-collapsed") && document.querySelector("#sidebar").getBoundingClientRect().width === 50 &&
  document.querySelector("#sb-toggle").getAttribute("aria-expanded") === "false", "collapsed to the rail");
await p1.reload();
await waitFor(p1, (id) => window.SFHApp.current?.id === id && !window.SFHApp.current.loading, "session reopened after reload", 15000, artSid);
if (!(await p1.evaluate(() => document.querySelector("#app").classList.contains("sb-collapsed")))) await fail("collapsed state lost on reload");
await p1.click("#sb-toggle");
await waitFor(p1, () => document.querySelector("#sidebar").getBoundingClientRect().width === 176, "expanded to the stored width after reload");
await p1.setViewport({ width: 700, height: 850 }); // narrow: overlay sidebar with the default width, no handle
await waitFor(p1, () => getComputedStyle(document.querySelector("#sidebar")).position === "absolute" && document.querySelector("#sidebar").getBoundingClientRect().width === 264 &&
  !document.querySelector("#sb-resize").offsetParent, "narrow overlay ignores the dragged width");
await p1.setViewport({ width: 1200, height: 850 });
await waitFor(p1, () => document.querySelector("#sidebar").getBoundingClientRect().width === 176, "dragged width back on a wide window");
const hb = await (await p1.$("#sb-resize")).boundingBox();
await p1.mouse.click(hb.x + hb.width / 2, hb.y + 200, { count: 2 });
await waitFor(p1, () => document.querySelector("#sidebar").getBoundingClientRect().width === 264 && localStorage.getItem("sfh:sbWidth") === "null", "double-click resets the width");
await p1.evaluate(() => { location.hash = "#settings"; });
await waitFor(p1, () => document.querySelector("#set-tui_mouse")?.checked === true, "tui_mouse checkbox (on by default)");
await p1.click("#set-tui_mouse");
await waitFor(p1, () => window.SFHApp.state.settings.tui_mouse === false, "tui_mouse saved", 5000);
if ((await p1.evaluate(() => window.SFHApp.rpc("settings.get"))).tui_mouse !== false) await fail("tui_mouse not stored by the server");
await p1.click("#set-tui_mouse");
await waitFor(p1, () => window.SFHApp.state.settings.tui_mouse === true, "tui_mouse back on", 5000);
step("sidebar: drag resize (clamped), keyboard, collapse/expand, reload, reset; tui_mouse setting");
}

await checkErrors();
await cleanup();
console.log("PASS");
