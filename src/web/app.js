// The browser client. Talks to the program embedded in this same file over ws://127.0.0.1:<port>/ws
// (see docs/DESIGN.md "Websocket protocol" and "Browser client"). Everything on screen is driven by
// server events, so several tabs on one session mirror each other.
(function () {
  "use strict";

  var MD = window.SFHMarkdown, IC = window.SFHIdenticon;
  var APP_ID = "singlefile-llm-harness";
  var SELF_PATH = decodeURIComponent(location.pathname);
  var ENC_PATH = encodeURIComponent(SELF_PATH);
  var RUN_CMD = "sh " + shq(SELF_PATH);
  var IS_WEB = location.protocol !== "file:";   // W: a copy served from a web page never contacts a local server
  var ABOUT = window.SFH_ABOUT || {};
  var LS = {
    get: function (k, d) { try { var v = localStorage.getItem("sfh:" + k); return v === null ? d : JSON.parse(v); } catch (e) { return d; } },
    set: function (k, v) { try { localStorage.setItem("sfh:" + k, JSON.stringify(v)); } catch (e) { /* ignore */ } }
  };

  function $(sel, root) { return (root || document).querySelector(sel); }
  function shq(s) { return "'" + s.replace(/'/g, "'\\''") + "'"; }
  function el(tag, attrs) {
    var e = document.createElement(tag);
    if (attrs) for (var k in attrs) {
      var v = attrs[k];
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k === "html") e.innerHTML = v;
      else if (k.slice(0, 2) === "on") e.addEventListener(k.slice(2), v);
      else e.setAttribute(k, v === true ? "" : v);
    }
    for (var i = 2; i < arguments.length; i++) append(e, arguments[i]);
    return e;
  }
  function append(e, kid) {
    if (kid === null || kid === undefined || kid === false) return;
    if (Array.isArray(kid)) { kid.forEach(function (k) { append(e, k); }); return; }
    e.append(kid.nodeType ? kid : String(kid));
  }
  function svgEl(markup) { var s = el("span", { class: "ico", html: markup }); return s; }

  function fnv1a(s) {
    var bytes = new TextEncoder().encode(s), h = 0x811c9dc5;
    for (var i = 0; i < bytes.length; i++) { h ^= bytes[i]; h = Math.imul(h, 0x01000193) >>> 0; }
    return h >>> 0;
  }
  function candidatePorts(p) {
    var h = fnv1a(p), out = [];
    for (var i = 0; i < 8; i++) out.push(47100 + ((h + i) % 800));
    return out;
  }

  function ago(ms) {
    if (!ms) return "";
    var s = Math.max(0, (Date.now() - ms) / 1000);
    if (s < 45) return "just now";
    if (s < 3600) return Math.round(s / 60) + "m ago";
    if (s < 86400) return Math.round(s / 3600) + "h ago";
    if (s < 86400 * 7) return Math.round(s / 86400) + "d ago";
    return new Date(ms).toLocaleDateString();
  }
  function basename(p) { return String(p || "").replace(/\/+$/, "").split("/").pop() || p; }
  function tildify(p) {
    var m = /^\/(home|Users)\/[^\/]+/.exec(p || "");
    return m ? "~" + p.slice(m[0].length) : p || "";
  }
  function copyText(text) {
    if (navigator.clipboard && window.isSecureContext) return navigator.clipboard.writeText(text).catch(fallback);
    fallback();
    return Promise.resolve();
    function fallback() {
      var ta = el("textarea", { style: "position:fixed;opacity:0" });
      ta.value = text; document.body.append(ta); ta.select();
      try { document.execCommand("copy"); } catch (e) { /* ignore */ }
      ta.remove();
    }
  }
  function toast(text, level) {
    var t = el("div", { class: "toast" + (level === "error" ? " error" : ""), text: text });
    $("#toasts").append(t);
    setTimeout(function () { t.remove(); }, level === "error" ? 7000 : 3500);
  }

  // ---- state -----------------------------------------------------------------------------------

  var META_TOKEN = (function () {
    var m = document.querySelector('meta[name="sfh-token"]');
    return m ? (m.getAttribute("content") || "").trim() : "";
  })();

  var S = {
    token: META_TOKEN, fatal: null, offline: false, offlineData: null,
    port: null, ws: null, connected: false, everConnected: false, lostAt: 0,
    rpcSeq: 0, pending: new Map(),
    hello: null, settings: {}, sessions: new Map(), commands: [], provider: null,
    route: null, onboarding: null, setupShown: false, setupSeq: 0, noticeDismissed: {},
    composerDrafts: new Map()
  };
  var cur = null; // the open chat: {id, messages, draft, queue, ...dom}

  // ---- connection ------------------------------------------------------------------------------

  function probePort(port) {
    var ctl = new AbortController();
    var timer = setTimeout(function () { ctl.abort(); }, 1500);
    return fetch("http://127.0.0.1:" + port + "/api/health?path=" + ENC_PATH, { signal: ctl.signal, cache: "no-store" })
      .then(function (r) { return r.json(); })
      .then(function (j) { return j && j.app === APP_ID && j.path === SELF_PATH ? { port: port, pairing: j.pairing_token || "" } : null; })
      .catch(function () { return null; })
      .finally(function () { clearTimeout(timer); });
  }
  function findServer() {
    return Promise.all(candidatePorts(SELF_PATH).map(probePort)).then(function (r) {
      for (var i = 0; i < r.length; i++) if (r[i]) return r[i];
      return null;
    });
  }
  function tokQ() { return "path=" + ENC_PATH + "&token=" + encodeURIComponent(S.token || ""); }
  // a refused websocket upgrade looks like any other failure; ask over HTTP whether the token is the problem
  function tokenRejected(port) {
    return fetch("http://127.0.0.1:" + port + "/api/sql", { method: "POST", cache: "no-store",
      headers: { "content-type": "application/json", "X-SFH-Path": SELF_PATH, "X-SFH-Token": S.token || "" },
      body: JSON.stringify({ sql: "SELECT 1", params: [], write: false }) })
      .then(function (r) { return r.status === 401; }, function () { return false; });
  }

  var probeTimer = null;
  var PROBE_MS = LS.get("probeMs", 2000);  // how often to look for the server while it's away (tests shorten it)
  function connectLoop() {
    clearTimeout(probeTimer);
    if (S.fatal) return;
    findServer().then(function (found) {
      if (S.fatal) return;
      if (found) {
        // a pairing token means the server just made a new one: newer than whatever this page loaded with
        if (found.pairing) S.token = found.pairing;
        if (!S.token) { showGate("no-token"); return; }
        openWs(found.port);
      } else { notFound(); probeTimer = setTimeout(connectLoop, PROBE_MS); }
    });
  }

  function notFound() {
    if (!S.everConnected) { if (S.offlineData) showOffline(); else showConnect(); return; }
    showBanner(Date.now() - S.lostAt > 5000);
  }

  function showGate(kind) {
    S.fatal = kind;
    clearTimeout(probeTimer);
    if (S.ws) { var w = S.ws; S.ws = null; S.connected = false; try { w.close(); } catch (e) { /* ignore */ } }
    $("#app").hidden = true;
    $("#connect").hidden = true;
    $("#gate").hidden = false;
    $("#gate").setAttribute("data-kind", kind);
    if (kind === "no-token") {
      $("#gate-title").textContent = "This page has no access token yet";
      $("#gate-text").textContent = "The server has just written an access token into this file. Reload the page to pick it up.";
    } else {
      $("#gate-title").textContent = "Access token changed";
      $("#gate-text").textContent = "The access token of this file was changed (regenerated). Reload this page to read the new one from the file.";
    }
    document.title = "Reload · " + basename(SELF_PATH);
  }

  function openWs(port) {
    var ws = new WebSocket("ws://127.0.0.1:" + port + "/ws?" + tokQ());
    var opened = false;
    S.ws = ws;
    ws.onopen = function () {
      opened = true;
      S.port = port;
      S.connected = true;
      rpc("hello", { client: "web" }).then(onHello, function (e) { toast("hello failed: " + e.message, "error"); });
    };
    ws.onmessage = function (ev) {
      var msg;
      try { msg = JSON.parse(ev.data); } catch (e) { return; }
      onFrame(msg);
    };
    ws.onclose = function (ev) {
      if (S.ws !== ws) return;
      S.ws = null;
      S.connected = false;
      S.pending.forEach(function (p) { p.reject(new Error("disconnected")); });
      S.pending.clear();
      if (ev && ev.code === 4001) { showGate("token-changed"); return; }
      if (S.everConnected) {
        if (opened) S.lostAt = Date.now();
        showBanner(Date.now() - S.lostAt > 5000);
      } else if (S.offline) showOfflineBanner();
      updateComposer();
      if (opened) { probeTimer = setTimeout(connectLoop, Math.min(300, PROBE_MS)); return; }
      tokenRejected(port).then(function (bad) {
        if (bad) showGate("token-changed");
        else probeTimer = setTimeout(connectLoop, PROBE_MS);
      });
    };
  }

  function rpc(method, params) {
    return new Promise(function (resolve, reject) {
      if (!S.ws || S.ws.readyState !== 1) { reject(new Error("not connected")); return; }
      var id = ++S.rpcSeq;
      S.pending.set(id, { resolve: resolve, reject: reject, method: method });
      S.ws.send(JSON.stringify({ type: "rpc", id: id, method: method, params: params || {} }));
    });
  }
  function rpcToast(method, params) {
    return rpc(method, params).catch(function (e) { toast(method + ": " + e.message, "error"); throw e; });
  }

  function onFrame(msg) {
    if (msg.type === "rpc_result" || msg.type === "rpc_error") {
      var p = S.pending.get(msg.id);
      if (!p) return;
      S.pending.delete(msg.id);
      if (msg.type === "rpc_result") p.resolve(msg.result);
      else p.reject(new Error(msg.error || "error"));
    } else if (msg.type === "event") {
      try { onEvent(msg.event, msg.data || {}); } catch (e) { console.error("event " + msg.event, e); }
    }
  }

  function onHello(h) {
    var first = !S.everConnected && !S.offline;
    S.hello = h;
    S.settings = h.settings || {};
    S.commands = h.commands || [];
    S.provider = h.provider;
    S.onboarding = h.onboarding || null;
    S.sessions = new Map((h.sessions || []).map(function (s) { return [s.id, s]; }));
    S.everConnected = true;
    S.offline = false;
    $("#app").classList.remove("offline");
    hideBanner();
    if (needsSetup()) { showSetup(); return; }
    S.setupShown = false;
    $("#connect").hidden = true;
    $("#app").hidden = false;
    renderNotices();
    renderSidebar();
    if (first && !location.hash) {
      var iv = viewHash(h.initial_view);
      if (iv) history.replaceState(null, "", iv);
    }
    route(true);
  }


  function showBanner(long) {
    var b = $("#banner");
    b.hidden = false;
    b.textContent = "";
    append(b, [el("span", { class: "pulse-dot" }), el("span", { text: "Reconnecting to the server…" })]);
    if (long) {
      append(b, [el("span", { class: "muted", text: "Not running? Start it with" }), el("code", { text: RUN_CMD }),
        el("button", { class: "btn sm", type: "button", text: "Copy", onclick: function () { copyText(RUN_CMD); toast("Copied"); } })]);
    }
  }
  function hideBanner() { $("#banner").hidden = true; }

  // ---- offline read-only view (snapshot the server writes into this file on every save) ---------

  function loadOffline() {
    var node = document.getElementById("sfh-offline");
    var b64 = node ? node.textContent.replace(/\s+/g, "") : "";
    if (!b64 || typeof DecompressionStream !== "function") return Promise.resolve(null);
    return Promise.resolve().then(function () {
      var bin = atob(b64), bytes = new Uint8Array(bin.length);
      for (var i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
      return new Response(new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"))).json();
    }).then(function (d) { return d && typeof d === "object" ? d : null; }, function (e) {
      console.warn("offline snapshot unreadable", e);
      return null;
    });
  }

  function showOffline() {
    if (S.offline) return;
    var d = S.offlineData;
    S.offline = true;
    S.settings = d.settings || {};
    S.provider = null;
    S.sessions = new Map((d.sessions || []).map(function (s) { return [s.id, Object.assign({}, s, { status: "idle" })]; }));
    $("#connect").hidden = true;
    $("#landing").hidden = true;
    $("#app").hidden = false;
    $("#app").classList.add("offline");
    showOfflineBanner();
    if (!location.hash) {
      var iv = viewHash(S.settings["ui.initial_view"]);
      if (iv) history.replaceState(null, "", iv);
    }
    route(true);
  }

  function showOfflineBanner() {
    var b = $("#banner");
    b.hidden = false;
    b.textContent = "";
    var at = S.offlineData && Number(S.offlineData.saved_at);
    if (at && at < 1e12) at *= 1000;
    var when = at ? " (snapshot from " + new Date(at).toLocaleString() + ")" : "";
    if (IS_WEB) {
      append(b, [el("strong", { text: "A shared copy, read only" + when + "." }),
        el("span", { class: "muted", text: about("tagline") }),
        el("button", { class: "btn sm primary", type: "button", text: "Install your own", onclick: showLanding }),
        el("button", { class: "btn sm", type: "button", text: "Download", onclick: downloadSelf })]);
      return;
    }
    append(b, [el("span", { class: "pulse-dot" }), el("strong", { text: "Offline — read only" + when + "." }),
      el("span", { class: "muted", text: "Start the server with" }), el("code", { text: RUN_CMD }),
      el("button", { class: "btn sm", type: "button", text: "Copy", onclick: function () { copyText(RUN_CMD); toast("Copied"); } })]);
  }

  // ---- events ----------------------------------------------------------------------------------

  function forCur(d) { return cur && !cur.loading && d.session_id === cur.id; }
  function forCurBranch(d) { return forCur(d) && (!d.branch_id || !cur.branchId || d.branch_id === cur.branchId); }

  function onEvent(name, d) {
    switch (name) {
      case "session.updated":
        var wasRunning = (S.sessions.get(d.id) || {}).status === "running";
        S.sessions.set(d.id, d);
        renderSidebar();
        if (cur && cur.id === d.id) {
          updateHeader();
          if (wasRunning !== (d.status === "running") && cur.msgsEl) renderTurnMarks();
        }
        else if (cur && cur.msgsEl) {
          var cs = S.sessions.get(cur.id);
          if (cs && cs.parent_id === d.id) updateHeader();
          else if (cs && cs.repo && cs.repo === d.repo) renderPeerStrip();
          refreshChildBadges();
        }
        if (S.route && S.route.name === "sessions") renderSessionsGrid();
        break;
      case "session.deleted":
        S.sessions.delete(d.session_id);
        renderSidebar();
        if (cur && cur.id === d.session_id) { toast("This session was deleted"); location.hash = "#sessions"; }
        else if (S.route && S.route.name === "sessions") renderSessionsGrid();
        break;
      case "settings.updated":
        var provChanged = JSON.stringify([S.settings.providers, S.settings.provider]) !== JSON.stringify([d.providers, d.provider]);
        S.settings = d;
        if (d._provider || d.active_provider) S.provider = d._provider || d.active_provider;
        else refreshProvider();
        if (S.route && S.route.name === "settings") {
          refreshSettingsValues();
          if (provChanged) loadProviders();
          renderOverrideList();
          var oh = $("#overrides-host");
          if (oh && !oh.contains(document.activeElement)) renderOverrideBrowser();
        }
        if (cur) updateHeader();
        break;
      case "branches.updated":
        if (!forCur(d)) break;
        cur.branches = d.branches || cur.branches;
        if (cur.branches && cur.branches.active_branch_id) cur.branchId = cur.branches.active_branch_id;
        renderForkMarks();
        renderMinimap();
        updateHeader();
        break;
      case "branch.switched":
        if (!forCur(d)) break;
        applyBranchSwitch(d, true);
        break;
      case "message.added":
        if (!forCurBranch(d)) break;
        if (cur.messages.some(function (m) { return m.id === d.id; })) break;
        if (d.role === "assistant" && cur.draft) { cur.draft = null; renderDraft(); }
        cur.messages.push(d);
        appendMessage(d);
        renderTurnMarks();
        renderForkMarks();
        updateTurnTimer(S.sessions.get(cur.id));
        if (d.role === "user" && !isToolResultMsg(d)) cur.stick = true;
        afterContent();
        break;
      case "messages.truncated":
        if (!forCurBranch(d)) break;
        cur.messages = cur.messages.filter(function (m) { return m.id < d.from_id; });
        renderMessages();
        break;
      case "draft.start":
        if (!forCurBranch(d)) break;
        cur.draft = { turn_id: d.turn_id, content: [] };
        renderDraft();
        break;
      case "draft.block":
        if (!forCurBranch(d) || !cur.draft) break;
        var blk = Object.assign({}, d.block);
        if (blk.type === "text" && blk.text == null) blk.text = "";
        if (blk.type === "thinking" && blk.thinking == null) blk.thinking = "";
        cur.draft.content[d.index] = blk;
        markDirty(d.index);
        break;
      case "draft.delta": {
        if (!forCurBranch(d) || !cur.draft) break;
        var b = cur.draft.content[d.index];
        if (!b) b = cur.draft.content[d.index] = { type: d.thinking != null ? "thinking" : d.partial_json != null ? "tool_use" : "text" };
        if (d.text != null) b.text = (b.text || "") + d.text;
        if (d.thinking != null) b.thinking = (b.thinking || "") + d.thinking;
        if (d.partial_json != null) b.partial_json = (b.partial_json || "") + d.partial_json;
        b.ended_at = serverNow();
        markDirty(d.index);
        break;
      }
      case "tool.start": case "tool.end":
        if (!forCurBranch(d)) break;
        cur.tools[d.tool_use_id] = { started_at: d.started_at, ended_at: d.ended_at };
        var tc = cur.toolCards.get(d.tool_use_id);
        if (tc) setToolTiming(tc, cur.tools[d.tool_use_id]);
        break;
      case "draft.end":
        if (!forCurBranch(d)) break;
        cur.draft = null;
        renderDraft();
        break;
      case "queue.updated":
        if (!forCurBranch(d)) break;
        cur.queue = d.queue || [];
        renderQueue();
        break;
      case "onboarding":
        applyOnboarding(d);
        break;
      case "notice":
        if (d.session_id && cur && d.session_id === cur.id && (!d.branch_id || !cur.branchId || d.branch_id === cur.branchId)) addNotice(d);
        else if (!d.session_id) toast(d.text, d.level);
        break;
      case "btw.start": case "btw.delta": case "btw.end":
        if (forCurBranch(d)) onBtw(name, d);
        break;
    }
  }

  // ---- routing ---------------------------------------------------------------------------------

  function viewHash(v) {
    if (!v || typeof v !== "string") return null;
    if (v.indexOf("session:") === 0) return "#session/" + encodeURIComponent(v.slice(8));
    if (v === "sessions" || v === "settings" || v === "new" || v === "about" || v === "setup") return "#" + v;
    return null;
  }
  function parseHash() {
    var h = location.hash.replace(/^#\/?/, "");
    if (h.indexOf("session/") === 0) {
      var parts = h.slice(8).split("/");
      return { name: "session", id: decodeURIComponent(parts[0]), bid: parts[1] ? decodeURIComponent(parts[1]) : null };
    }
    if (h === "sessions" || h === "settings" || h === "new" || h === "about" || h === "setup") return { name: h };
    return null;
  }
  function currentViewSpec() {
    if (cur) return "session:" + cur.id;
    if (S.lastSession && S.sessions.has(S.lastSession)) return "session:" + S.lastSession;
    return "sessions";
  }

  function route(force) {
    if (!S.everConnected && !S.offline) return;
    var r = parseHash();
    if (S.offline && r && r.name === "new") { history.replaceState(null, "", "#sessions"); r = parseHash(); }
    if (!r) {
      history.replaceState(null, "", S.sessions.size ? "#sessions" : "#new");
      r = parseHash();
    }
    var same = S.route && S.route.name === r.name && S.route.id === r.id && S.route.bid === r.bid;
    S.route = r;
    if (same && !force) return;
    // Per-tab thread switch inside the already-open session: stay mounted and slide sideways!
    if (!force && cur && r.name === "session" && r.id === cur.id) {
      loadBranchInTab(r.bid, true);
      renderSidebar();
      return;
    }
    if (cur && (r.name !== "session" || r.id !== cur.id || force)) closeChat(r.name === "session" && r.id === cur.id);
    $("#nav-sessions").classList.toggle("active", r.name === "sessions");
    $("#nav-settings").classList.toggle("active", r.name === "settings");
    $("#nav-about").classList.toggle("active", r.name === "about");
    if (r.name === "session") openChat(r.id, r.bid);
    else if (r.name === "new") renderNew();
    else if (r.name === "sessions") renderSessions();
    else if (r.name === "settings") renderSettings();
    else if (r.name === "about") renderAbout();
    else if (r.name === "setup") renderSetupPage();
    renderSidebar();
  }
  window.addEventListener("hashchange", function () { route(false); });

  function setView(node) {
    var v = $("#view");
    v.textContent = "";
    v.append(node);
  }

  // ---- favicon + title -------------------------------------------------------------------------

  function setFavicon(href) {
    var l = document.querySelector('link[rel="icon"]');
    if (!l) { l = el("link", { rel: "icon", type: "image/svg+xml" }); document.head.append(l); }
    if (l.getAttribute("href") !== href) l.setAttribute("href", href);
  }
  function defaultFavicon() {
    return "data:image/svg+xml," + encodeURIComponent(
      '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 16 16"><rect width="16" height="16" rx="3" fill="#3b6fe0"/>' +
      '<path d="M4 5h8M4 8h6M4 11h4" stroke="#fff" stroke-width="1.6" stroke-linecap="round"/></svg>');
  }
  function setPageMeta(title, favicon) {
    stopFavAnim();
    document.title = title;
    setFavicon(favicon || defaultFavicon());
  }

  // While the open session is running, its identicon favicon animates (a segment travels around the
  // edge), so its tab visibly "does something". Frames are precomputed per session look.
  var FAV_FRAMES = 12, favAnim = { timer: 0, sid: null, key: "", frames: [], i: 0 };
  function stopFavAnim() {
    if (favAnim.timer) clearInterval(favAnim.timer);
    favAnim.timer = 0;
    favAnim.sid = null;
  }
  function animateFavicon(s) {
    var key = s.id + "|" + JSON.stringify(s.subjects || []) + "|" + (s.title || "");
    if (favAnim.timer && favAnim.sid === s.id && favAnim.key === key) return;
    stopFavAnim();
    favAnim.sid = s.id;
    favAnim.key = key;
    favAnim.frames = [];
    for (var i = 0; i < FAV_FRAMES; i++) favAnim.frames.push(IC.dataUrl(s, 32, { spin: i / FAV_FRAMES }));
    favAnim.timer = setInterval(function () {
      var live = cur && cur.id === favAnim.sid ? S.sessions.get(favAnim.sid) : null;
      if (!live || live.status !== "running") { var still = live; stopFavAnim(); if (still) setFavicon(IC.dataUrl(still, 32)); return; }
      favAnim.i = (favAnim.i + 1) % FAV_FRAMES;
      setFavicon(favAnim.frames[favAnim.i]);
    }, 120);
  }

  // ---- sidebar ---------------------------------------------------------------------------------

  var iconCache = new Map();
  function iconSvg(s, size) {
    var key = s.id + "|" + size + "|" + JSON.stringify(s.subjects || []) + "|" + (s.subjects && s.subjects.length ? "" : s.title || "");
    var v = iconCache.get(key);
    if (!v) { v = IC.svg(s, size); iconCache.set(key, v); }
    return v;
  }
  function sortedSessions() {
    return Array.from(S.sessions.values()).sort(function (a, b) { return (b.updated_at || 0) - (a.updated_at || 0); });
  }
  function titleOf(s) { return (s && s.title) || "Untitled session"; }
  // Subagent sessions nest under their parent; children of unknown parents are shown at top level.
  function sessionTree(list) {
    var ids = new Set(list.map(function (s) { return s.id; }));
    var kids = new Map();
    var roots = [];
    list.forEach(function (s) {
      if (s.parent_id && s.parent_id !== s.id && ids.has(s.parent_id)) {
        if (!kids.has(s.parent_id)) kids.set(s.parent_id, []);
        kids.get(s.parent_id).push(s);
      } else roots.push(s);
    });
    function node(s, seen) {
      seen = new Set(seen).add(s.id);
      return { s: s, kids: (kids.get(s.id) || []).filter(function (k) { return !seen.has(k.id); }).map(function (k) { return node(k, seen); }) };
    }
    return roots.map(function (s) { return node(s, []); });
  }
  function treeCollapsed(id) { return !!(LS.get("collapsed", {}) || {})[id]; }
  function toggleTree(id) {
    var m = LS.get("collapsed", {}) || {};
    if (m[id]) delete m[id]; else m[id] = true;
    LS.set("collapsed", m);
    renderSidebar();
    if (S.route && S.route.name === "sessions") renderSessionsGrid();
  }
  function orderThreadsTree(list) {
    var byId = new Map((list || []).map(function (b) { return [b.id, b]; }));
    var kids = new Map(), roots = [];
    (list || []).forEach(function (b) {
      if (b.parent_branch_id && b.parent_branch_id !== b.id && byId.has(b.parent_branch_id)) {
        if (!kids.has(b.parent_branch_id)) kids.set(b.parent_branch_id, []);
        kids.get(b.parent_branch_id).push(b);
      } else roots.push(b);
    });
    kids.forEach(function (arr) {
      arr.sort(function (a, b) {
        return (a.origin_depth || 0) - (b.origin_depth || 0) || (a.created_at || 0) - (b.created_at || 0);
      });
    });
    var ordered = [], seen = new Set();
    function walk(b, depth) {
      if (seen.has(b.id)) return;
      seen.add(b.id);
      ordered.push({ b: b, depth: depth });
      (kids.get(b.id) || []).forEach(function (k) { walk(k, depth + 1); });
    }
    roots.forEach(function (r) { walk(r, 0); });
    (list || []).forEach(function (b) { if (!seen.has(b.id)) walk(b, 0); });
    return { ordered: ordered, kids: kids };
  }

  function threadGlyph(t) {
    if (t.is_main || t.kind === "main") return "●";
    if (t.kind === "subagent") return t.context_mode === "fresh" ? "◌" : "⑂";
    if (t.kind === "edit") return "↺";
    return "⑂";
  }

  function treeToggle(n) {
    var threads = (n.s.threads && n.s.threads.length > 1) ? n.s.threads : [];
    var count = n.kids.length + (threads.length > 1 ? threads.length : 0);
    if (!count) return null;
    var closed = treeCollapsed(n.s.id);
    var what = threads.length > 1 ? threads.length + " threads" : n.kids.length + " subagent" + (n.kids.length === 1 ? "" : "s");
    return el("button", { class: "tree-tog", type: "button", title: (closed ? "Show " : "Hide ") + what,
      "aria-expanded": closed ? "false" : "true", text: closed ? "▸" : "▾",
      onclick: function (e) { e.preventDefault(); e.stopPropagation(); toggleTree(n.s.id); } });
  }

  function renderSidebar() {
    var ul = $("#sb-list");
    var activeId = S.route && S.route.name === "session" ? S.route.id : null;
    var activeBid = cur && cur.id === activeId ? cur.branchId : (S.route && S.route.bid);
    ul.textContent = "";
    function add(n, depth) {
      var s = n.s;
      var threads = s.threads && s.threads.length > 1 ? s.threads : [];
      var a = el("a", { href: "#session/" + encodeURIComponent(s.id), title: titleOf(s) + (s.cwd ? "\n" + s.cwd : ""),
        class: s.id === activeId && !threads.length ? "active" : (s.id === activeId && !activeBid ? "active" : null), "data-sid": s.id },
        svgEl(iconSvg(s, 16)),
        el("span", { class: "t" + (s.title ? "" : " untitled"), text: titleOf(s) }),
        n.kids.length ? el("span", { class: "kids-count", title: n.kids.length + " subagent" + (n.kids.length === 1 ? "" : "s"), text: String(n.kids.length) }) : null,
        s.branch_count > 1 ? el("span", { class: "branch-count", title: s.branch_count + " threads", text: "⑂" + s.branch_count }) : null,
        inboxBadge(s), peerBadge(s),
        s.status === "running" ? el("span", { class: "status-dot", title: "running" }) : null);
      var li = el("li", { class: depth ? "child" : null, style: depth ? "--depth:" + depth : null }, treeToggle(n), a);
      ul.append(li);
      if (!treeCollapsed(s.id)) {
        if (threads.length > 1) {
          var cmap = branchLaneMap(threads);
          orderThreadsTree(threads).ordered.forEach(function (item) {
            var t = item.b;
            var isCur = s.id === activeId && activeBid === t.id;
            var col = laneColor(cmap.get(t.id) || 0);
            var href = "#session/" + encodeURIComponent(s.id) + "/" + encodeURIComponent(t.id);
            var tip = t.name + " (" + t.kind + (t.kind === "subagent" ? " · " + (t.context_mode || "fresh") : "") + ")";
            var ta = el("a", { href: href, class: isCur ? "active" : null, title: tip, "data-sid": s.id, "data-bid": t.id },
              el("span", { class: "th-ico", style: "--lane-color:" + col, text: threadGlyph(t) }),
              el("span", { class: "t", text: t.name }),
              t.kind === "subagent" ? el("span", { class: "th-tag", text: t.context_mode === "fresh" ? "sub" : "fork" }) : null,
              t.status === "running" ? el("span", { class: "status-dot", title: "running" }) : null);
            ul.append(el("li", { class: "thread-item", style: "--depth:" + (depth + 1 + Math.min(3, item.depth)) }, ta));
          });
        }
        n.kids.forEach(function (k) { add(k, depth + 1); });
      }
    }
    sessionTree(sortedSessions()).forEach(function (n) { add(n, 0); });
    if (!S.sessions.size) ul.append(el("li", { class: "muted small", style: "padding:6px 10px", text: "No sessions yet" }));
  }
  function toggleSidebar() {
    var c = !$("#app").classList.contains("sb-collapsed");
    $("#app").classList.toggle("sb-collapsed", c);
    LS.set("sbCollapsed", c);
    syncSbToggle();
  }
  function syncSbToggle() {
    var c = $("#app").classList.contains("sb-collapsed"), b = $("#sb-toggle");
    b.setAttribute("aria-expanded", c ? "false" : "true");
    b.title = (c ? "Expand" : "Collapse") + " sidebar (Ctrl+B)";
    b.setAttribute("aria-label", (c ? "Expand" : "Collapse") + " sidebar");
  }

  // Sidebar width (v14): dragged by the handle on its right edge (pointer events + capture: mouse, pen
  // and touch), arrow keys on the focused handle, double-click resets. A per-browser view preference in
  // localStorage like sbCollapsed/termHeight: nothing goes to the server and nothing re-renders, so
  // dragging can't steal focus; it is stored once, at the end of a drag. The narrow-window overlay
  // ignores it (CSS). null = the default width.
  var SB_DEFAULT = 264, SB_MIN = 160;  // SB_DEFAULT = --sb-default in app.css
  function sbMax() { return Math.max(SB_MIN, Math.floor(window.innerWidth * 0.5)); }
  function clampSb(w) { return Math.round(Math.max(SB_MIN, Math.min(sbMax(), w))); }
  function setSbWidth(w, save) {
    var app = $("#app"), h = $("#sb-resize");
    var px = w == null ? Math.min(SB_DEFAULT, sbMax()) : clampSb(w);
    if (w == null) app.style.removeProperty("--sb-width");
    else app.style.setProperty("--sb-width", px + "px");
    h.setAttribute("aria-valuemin", SB_MIN);
    h.setAttribute("aria-valuemax", sbMax());
    h.setAttribute("aria-valuenow", px);
    if (save) LS.set("sbWidth", w == null ? null : px);
  }
  function initSbResize() {
    var h = $("#sb-resize"), app = $("#app"), sb = $("#sidebar");
    h.addEventListener("pointerdown", function (e) {
      if (e.button !== 0 || app.classList.contains("sb-collapsed")) return;
      e.preventDefault();  // no text selection, and focus stays where it was
      var startX = e.clientX, startW = sb.getBoundingClientRect().width, w = startW, done = false;
      try { h.setPointerCapture(e.pointerId); } catch (err) { /* ignore */ }
      app.classList.add("sb-resizing");
      function move(ev) { w = startW + ev.clientX - startX; setSbWidth(w, false); }
      function end() {
        if (done) return;
        done = true;
        h.removeEventListener("pointermove", move);
        h.removeEventListener("pointerup", end);
        h.removeEventListener("pointercancel", end);
        h.removeEventListener("lostpointercapture", end);
        app.classList.remove("sb-resizing");
        if (w !== startW) setSbWidth(w, true);
      }
      h.addEventListener("pointermove", move);
      h.addEventListener("pointerup", end);
      h.addEventListener("pointercancel", end);
      h.addEventListener("lostpointercapture", end);
    });
    h.addEventListener("dblclick", function () { setSbWidth(null, true); });
    h.addEventListener("keydown", function (e) {
      var w = Number(h.getAttribute("aria-valuenow")), step = e.shiftKey ? 64 : 16;  // not the rect: it animates
      if (e.key === "ArrowLeft") w -= step;
      else if (e.key === "ArrowRight") w += step;
      else if (e.key === "Home") w = SB_MIN;
      else if (e.key === "End") w = sbMax();
      else return;
      e.preventDefault();
      setSbWidth(w, true);
    });
    // a smaller window clamps the shown width; the stored one is kept
    window.addEventListener("resize", function () { setSbWidth(LS.get("sbWidth", null), false); });
    setSbWidth(LS.get("sbWidth", null), false);
  }

  // ---- chat view -------------------------------------------------------------------------------

  function contentOf(m) {
    if (typeof m.content === "string") return [{ type: "text", text: m.content }];
    return Array.isArray(m.content) ? m.content : [];
  }
  function isToolResultMsg(m) {
    var c = contentOf(m);
    return m.role === "user" && c.length > 0 && c.every(function (b) { return b.type === "tool_result"; });
  }
  function userText(m) {
    return contentOf(m).filter(function (b) { return b.type === "text"; }).map(function (b) { return b.text; }).join("\n\n");
  }

  function closeChat(keepComposer) {
    if (!cur) return;
    if (!keepComposer) S.composerDrafts.set(cur.id, cur.ta.value);
    else S.composerDrafts.set(cur.id, cur.ta.value);
    var id = cur.id;
    if (cur.raf) cancelAnimationFrame(cur.raf);
    cur = null;
    $("#btw-box").hidden = true;
    $("#btw-box").textContent = "";
    if (S.connected) rpc("sessions.unsubscribe", { session_id: id }).catch(function () {});
  }

  function buildChat(opts) {
    var c = {
      id: opts.id || null, messages: [], draft: null, queue: [], loading: true, stick: true,
      toolCards: new Map(), dirty: new Set(), draftEls: [], raf: 0, tools: {}, skew: 0,
      branchId: null, branches: null, offlineBranches: null, minimapExpanded: LS.get("minimapExpanded", false)
    };
    c.head = el("header", { class: "chat-head" });
    c.msgsEl = el("div", { class: "msgs" });
    c.draftEl = el("div", { class: "draft-host" });
    c.queueEl = el("div", { class: "queue-host" });
    c.inboxEl = el("div", { class: "inbox-host" });
    c.peersEl = el("div", { class: "peer-strip", hidden: true });
    c.minimapEl = el("aside", { class: "branch-minimap", hidden: true, "aria-label": "Branch minimap" });
    c.jump = el("button", { class: "jump-bottom", type: "button", title: "Jump to latest", hidden: true, text: "↓",
      onclick: function () { c.stick = true; scrollBottom(); } });
    c.inner = el("div", { class: "chat-inner" }, opts.before || null, c.msgsEl, c.draftEl, c.queueEl, c.inboxEl);
    c.scroll = el("div", { class: "chat-scroll" }, c.inner, c.jump);
    c.scroll.addEventListener("scroll", function () {
      var atBottom = c.scroll.scrollTop + c.scroll.clientHeight >= c.scroll.scrollHeight - 40;
      c.stick = atBottom;
      c.jump.hidden = atBottom;
    });
    c.body = el("div", { class: "chat-body" }, c.scroll, c.minimapEl);
    c.ta = el("textarea", { rows: 1, placeholder: opts.placeholder || "Message…  (/ for commands)", "aria-label": "Message" });
    c.sendBtn = el("button", { class: "btn primary", type: "button", text: "Send", onclick: function () { sendComposer(); } });
    c.stopBtn = el("button", { class: "btn danger", type: "button", text: "Stop", title: "Stop (Esc)", hidden: true,
      onclick: function () { stopRun(); } });
    c.pop = el("div", { class: "cmd-pop", hidden: true });
    c.composer = el("div", { class: "composer-wrap" }, c.pop,
      el("div", { class: "composer" }, c.ta, c.stopBtn, c.sendBtn),
      el("div", { class: "composer-hint" },
        el("span", null, el("kbd", { text: "Enter" }), " send"),
        el("span", null, el("kbd", { text: "Shift+Enter" }), " newline"),
        el("span", null, el("kbd", { text: "↑" }), " edit last"),
        el("span", null, el("kbd", { text: "Esc" }), " stop"),
        el("span", null, el("kbd", { text: "/" }), " commands")));
    c.costEl = el("span", { class: "session-cost" });
    c.ctxEl = el("span", { class: "session-ctx" });
    c.turnEl = el("span", { class: "session-turn", hidden: true, title: "How long the current turn has been running" });
    c.root = el("div", { class: "view chat" }, c.head, c.peersEl, c.body, c.composer, el("div", { class: "chat-foot" }, c.turnEl, c.ctxEl, c.costEl));
    c.ta.addEventListener("input", function () { autoGrow(c.ta); updatePopup(); });
    c.ta.addEventListener("keydown", composerKeydown);
    return c;
  }

  function autoGrow(ta) {
    ta.style.height = "auto";
    ta.style.height = Math.min(ta.scrollHeight, window.innerHeight * 0.4) + "px";
  }

  function openChat(id, bid) {
    var c = buildChat({ id: id });
    newView = null;
    cur = c;
    setView(c.root);
    c.ta.value = S.composerDrafts.get(id) || "";
    autoGrow(c.ta);
    updateHeader();
    updateComposer();
    S.lastSession = id;
    if (S.offline) c.composer.hidden = true;
    var offBd = S.offline && S.offlineData && S.offlineData.branches ? S.offlineData.branches[id] : null;
    var sub;
    if (S.offline) {
      var offBid = bid && offBd && offBd.threads && offBd.threads[bid] ? bid : (offBd && offBd.info ? offBd.info.active_branch_id : null);
      var offMsgs = offBd && offBid && offBd.threads[offBid]
        ? offBd.threads[offBid].map(function (mid) { return offBd.messages[String(mid)]; }).filter(Boolean)
        : (((S.offlineData && S.offlineData.messages) || {})[id] || []);
      var offInfo = offBd && offBid ? recomputeOfflineBranches(offBd, offBid) : (offBd ? offBd.info : null);
      sub = Promise.resolve({ session: S.sessions.get(id), messages: offMsgs, draft: null, queue: [], branches: offInfo, branch_id: offBid });
    } else {
      sub = rpc("sessions.subscribe", bid ? { session_id: id, branch_id: bid } : { session_id: id });
    }
    sub.then(function (snap) {
      if (cur !== c) return;
      c.loading = false;
      if (offBd) c.offlineBranches = offBd;
      if (snap.session) S.sessions.set(id, snap.session);
      c.branchId = snap.branch_id || (snap.branches && snap.branches.active_branch_id) || null;
      c.branches = snap.branches || null;
      c.messages = snap.messages || [];
      c.draft = snap.draft || null;
      c.queue = snap.queue || [];
      c.tools = snap.tools || {};
      if (snap.now) c.skew = snap.now - Date.now();
      renderMessages();
      renderMinimap();
      renderQueue();
      updateHeader();
      updateComposer();
      renderSidebar();
      if (!window.matchMedia("(pointer: coarse)").matches) c.ta.focus();
    }, function (e) {
      if (cur !== c) return;
      cur = null;
      setPageMeta("Not found · " + basename(SELF_PATH));
      setView(el("div", { class: "page" }, el("div", { class: "page-inner" },
        el("div", { class: "empty" }, el("p", { text: "Session " + id + " could not be opened: " + e.message }),
          el("a", { class: "btn", href: "#sessions", text: "All sessions" })))));
    });
  }

  function loadBranchInTab(bid, animate) {
    var c = cur;
    if (!c) return;
    if (bid && bid === c.branchId) return;
    if (S.offline) {
      var bd = c.offlineBranches;
      if (!bd || !bd.threads) return;
      var target = bid && bd.threads[bid] ? bid : bd.info.active_branch_id;
      if (!target || !bd.threads[target]) return;
      var msgs = bd.threads[target].map(function (id) { return bd.messages[String(id)]; }).filter(Boolean);
      var info = recomputeOfflineBranches(bd, target);
      applyBranchSwitch({ branch_id: target, branches: info, messages: msgs, draft: null, queue: [], tools: {} }, animate);
      return;
    }
    rpcToast("sessions.subscribe", bid ? { session_id: c.id, branch_id: bid } : { session_id: c.id }).then(function (snap) {
      if (cur === c) applyBranchSwitch(snap, animate);
    }).catch(function () {});
  }

  function updateHeader() {
    if (!cur) return;
    var s = S.sessions.get(cur.id) || { id: cur.id, title: "" };
    var h = cur.head;
    h.textContent = "";
    var titleBtn = el("button", { class: "chat-title", type: "button", title: "Click to rename", text: titleOf(s), onclick: startRename });
    var running = s.status === "running";
    var parent = s.parent_id ? S.sessions.get(s.parent_id) : null;
    append(h, [svgEl(iconSvg(s, 32)),
      el("div", { class: "info" }, titleBtn,
        el("div", { class: "chat-meta" },
          s.parent_id ? el("span", { class: "sub-of" }, "subagent of ",
            el("a", { class: "parent-link", href: "#session/" + encodeURIComponent(s.parent_id), text: parent ? titleOf(parent) : s.parent_id })) : null,
          s.cwd ? (S.offline ? el("span", { class: "cwd", title: s.cwd, text: tildify(s.cwd) })
            : el("button", { class: "cwd cwd-btn", type: "button", title: s.cwd + "\nClick to change this session's working directory",
              text: tildify(s.cwd), onclick: changeSessionCwd })) : null,
          providerBadge(s),
          effortControl(s),
          branchHeaderBadge(s),
          S.offline ? el("span", { class: "badge", text: "read only" }) : el("span", { class: "badge" + (running ? " running" : ""), text: running ? "running" : "idle" }))),
      S.offline ? null : el("button", { class: "btn sm", type: "button", title: "Open a shell in this session's directory", text: "Terminal",
        onclick: function () { openTerminal({ cmd: "shell", cwd: s.cwd, title: "shell · " + tildify(s.cwd) }); } })]);
    setPageMeta(titleOf(s), IC.dataUrl(s, 32));
    if (running && !S.offline) animateFavicon(s);
    renderPeerStrip();
    renderInbox(s);
    updateCost(s);
    updateTurnTimer(s);
    updateComposer();
  }

  // settings.updated carries only settings; the resolved provider comes from hello
  var provTimer = 0;
  function refreshProvider() {
    clearTimeout(provTimer);
    provTimer = setTimeout(function () {
      if (!S.connected) return;
      rpc("hello", { client: "web" }).then(function (h) {
        S.provider = h.provider;
        if (S.hello) S.hello.has_api_key = h.has_api_key;
        if (cur) updateHeader();
        if (S.route && S.route.name === "settings") renderProviders();
      }, function () {});
    }, 150);
  }

  function providerBadge(s) {
    if (s && s.provider) {
      var isMock = s.provider === "mock" || s.model === "mock";
      var label = s.provider + (s.model && !isMock ? " · " + s.model : "");
      var tip = (s.provider_error ? s.provider_error + "\n" : "") +
        (isMock ? "No provider configured: replies come from the built-in mock provider" : "Provider " + s.provider + ", model " + (s.model || "?")) +
        (s.provider_override || s.model_override ? "\n(chosen for this session)" : "\n(the global default)");
      var cls = "badge provider" + (isMock ? " mock" : "") + (s.provider_error ? " warn" : "") + (s.provider_override || s.model_override ? " pinned" : "");
      if (S.offline || !cur || cur.id !== s.id) return el("span", { class: cls, "data-provider": s.provider, title: tip, text: label });
      return el("button", { class: cls + " model-btn", type: "button", "data-provider": s.provider, title: tip + "\nClick to change the model",
        onclick: changeSessionModel }, label, el("span", { class: "caret", text: "▾" }));
    }
    var p = S.provider;
    if (p && typeof p === "object") {
      var model = S.settings.model || p.model || "";
      var mock = p.type === "mock";
      return el("span", { class: "badge provider" + (mock ? " mock" : ""), "data-provider": p.id || "",
        title: mock ? "No provider configured: replies come from the built-in mock provider" : "Provider " + (p.id || "?") + " (" + (p.type || "?") + ")",
        text: (p.id || p.type || "?") + (model && !mock ? " · " + model : "") });
    }
    if (p === "mock") return el("span", { class: "badge mock", title: "No API key configured: replies come from the built-in mock provider", text: "mock" });
    return s.model ? el("span", { text: s.model }) : null;
  }

  // the session's effort level: a dropdown of the levels its model takes ("default" = the global setting)
  function effortControl(s) {
    if (!s || !s.provider) return null;
    var want = s.effort_wanted || null, eff = s.effort || null;
    var glob = S.settings.effort || null;
    var tip = "Effort: how much the model thinks/reasons" +
      (s.efforts_known ? "" : "\nThe provider doesn't say which levels " + (s.model || "this model") + " takes; a level is tried and dropped if refused.") +
      (want && want !== eff ? "\n" + (s.model || "This model") + " doesn't take " + want + (eff ? "; sent as " + eff : "; none is sent") : "") +
      (s.effort_override ? "\n(chosen for this session)" : "\n(the global default)");
    var label = function (x) { return x + (want === x && eff && eff !== x ? " → " + eff : ""); };
    if (S.offline || !cur || cur.id !== s.id)
      return el("span", { class: "badge effort", title: tip, text: "effort " + (want ? label(want) : "default") });
    var levels = (s.efforts || []).slice();
    if (s.effort_override && levels.indexOf(s.effort_override) < 0) levels.push(s.effort_override);
    var sel = el("select", { class: "effort-select" + (s.effort_override ? " pinned" : "") + (want && want !== eff ? " warn" : ""),
        title: tip, "aria-label": "Effort level" },
      el("option", { value: "", text: "effort: default" + (glob ? " (" + glob + ")" : "") }),
      levels.map(function (x) { return el("option", { value: x, text: "effort: " + label(x) }); }));
    sel.value = s.effort_override || "";
    sel.addEventListener("change", function () {
      var id = s.id;
      rpcToast("sessions.set_model", { session_id: id, effort: sel.value || null })
        .then(function (sum) { S.sessions.set(sum.id, sum); renderSidebar(); updateHeader(); }, function () { updateHeader(); });
    });
    return sel;
  }

  function startRename() {
    if (S.offline) return;
    var s = S.sessions.get(cur.id) || {};
    var btn = $(".chat-title", cur.head);
    var input = el("input", { type: "text", class: "chat-title-input", value: s.title || "" });
    var id = cur.id, done = false;
    function finish(save) {
      if (done) return;
      done = true;
      var t = input.value.trim();
      if (save && t && t !== s.title) rpcToast("sessions.rename", { session_id: id, title: t }).then(function (sum) { S.sessions.set(sum.id, sum); renderSidebar(); updateHeader(); });
      else updateHeader();
    }
    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") { e.preventDefault(); finish(true); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); finish(false); }
    });
    input.addEventListener("blur", function () { finish(true); });
    btn.replaceWith(input);
    input.focus();
    input.select();
  }

  // ---- usage / cost ----------------------------------------------------------------------------

  function fmtTokens(n) {
    n = Number(n) || 0;
    if (n < 1000) return String(n);
    if (n < 1e6) return (n / 1000).toFixed(n < 1e4 ? 1 : 0) + "k";
    return (n / 1e6).toFixed(n < 1e7 ? 2 : 1) + "M";
  }
  function fmtCost(c) { c = Number(c) || 0; return "$" + (c === 0 ? "0" : c < 0.01 ? c.toFixed(4) : c < 100 ? c.toFixed(2) : c.toFixed(0)); }
  function sumUsage(a, b) {
    if (!a) return b ? Object.assign({}, b) : null;
    if (!b) return Object.assign({}, a);
    var o = {};
    ["input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens", "calls", "priced_calls", "estimated_calls", "cost"].forEach(function (k) {
      o[k] = (Number(a[k]) || 0) + (Number(b[k]) || 0);
    });
    return o;
  }
  function inTokens(u) { return (u.input_tokens || 0) + (u.cache_read_tokens || 0) + (u.cache_write_tokens || 0); }
  // "$0.0123 · 12.3k in · 2.1k out"; cost when the provider reported one or its price list gave one
  // ("~" = some of it is estimated from the price list, "+" = some responses have no cost at all)
  function usageText(u) {
    if (!u || !u.calls) return "";
    var cost = u.priced_calls ? (u.estimated_calls ? "~" : "") + fmtCost(u.cost) + (u.priced_calls < u.calls ? "+" : "") : "cost n/a";
    return cost + " · " + fmtTokens(inTokens(u)) + " in · " + fmtTokens(u.output_tokens) + " out";
  }
  function usageTip(u, label) {
    if (!u || !u.calls) return "";
    return label + ": " + u.calls + " response" + (u.calls === 1 ? "" : "s") +
      "\n  input " + (u.input_tokens || 0) + " (+ cache read " + (u.cache_read_tokens || 0) + ", cache write " + (u.cache_write_tokens || 0) + ")" +
      "\n  output " + (u.output_tokens || 0) +
      "\n  cost " + (u.priced_calls ? fmtCost(u.cost) + (u.priced_calls < u.calls ? " (" + u.priced_calls + " of " + u.calls + " responses have a cost)" : "") +
        (u.estimated_calls ? "\n  " + (u.estimated_calls === u.priced_calls ? "all" : u.estimated_calls + " of " + u.priced_calls) +
          " estimated from the provider's price list (/settings/api/pricing)" : "")
        : "not reported by the provider, and no price list for this model");
  }
  // "context 45.2k / 200k (23%)": what the conversation takes of the model's context window now
  function contextText(ctx) {
    if (!ctx || (ctx.used == null && !ctx.window)) return "";
    if (ctx.used == null) return "context – / " + fmtTokens(ctx.window);
    var t = "context " + (ctx.estimated ? "~" : "") + fmtTokens(ctx.used);
    return ctx.window ? t + " / " + fmtTokens(ctx.window) + " (" + Math.round(100 * ctx.used / ctx.window) + "%)" : t;
  }
  function updateContext(s) {
    if (!cur || !cur.ctxEl) return;
    var ctx = s.context;
    var pct = ctx && ctx.window && ctx.used != null ? ctx.used / ctx.window : 0;
    cur.ctxEl.textContent = contextText(ctx);
    cur.ctxEl.className = "session-ctx" + (pct >= 0.8 ? " warn" : "") + (pct >= 0.95 ? " full" : "");
    cur.ctxEl.title = ctx && ctx.used != null ? "Tokens this conversation occupies in the model's context" +
      (ctx.estimated ? " (estimated from the text, ~4 characters per token)" : " (as of the last response)") +
      (ctx.window ? ": " + ctx.used + " of " + ctx.window + (ctx.window_override ? " (your override in Settings → Model overrides)" : "")
        : "; the provider did not say how large the window of " + (s.model || "this model") + " is (set one in Settings → Model overrides)") +
      ".\n/compact summarizes the conversation to free context." : "";
  }

  function updateCost(s) {
    updateContext(s);
    if (!cur || !cur.costEl) return;
    var total = sumUsage(s.usage, s.usage_subagents);
    cur.costEl.textContent = usageText(total);
    cur.costEl.title = [usageTip(total, "Session total" + (s.usage_subagents ? " incl. subagents" : "")),
      s.usage_subagents ? usageTip(s.usage, "This session") : "", usageTip(s.usage_subagents, "Subagents")].filter(Boolean).join("\n\n");
  }

  function changeSessionCwd() {
    if (!cur || S.offline) return;
    var id = cur.id, s = S.sessions.get(id) || {};
    pickFolder({ title: "Working directory of this session", initial: s.cwd,
      note: "Applies from the next step of the agent on; earlier messages keep the directory they were written in." })
      .then(function (dir) {
        if (!dir || dir === s.cwd) return;
        rpcToast("sessions.set_cwd", { session_id: id, cwd: dir }).then(function (sum) { S.sessions.set(sum.id, sum); renderSidebar(); updateHeader(); }, function () {});
      });
  }
  function changeSessionModel() {
    if (!cur || S.offline) return;
    var id = cur.id, s = S.sessions.get(id) || {};
    pickModel({ title: "Model for this session", provider: s.provider_override || null, model: s.model_override || null,
      current: { provider: s.provider, model: s.model }, sessionId: id,
      note: "Takes effect at the next LLM call, also in the middle of a running turn. Each message records the model it was written with." })
      .then(function (r) {
        if (!r) return;
        rpcToast("sessions.set_model", { session_id: id, provider: r.provider, model: r.model })
          .then(function (sum) { S.sessions.set(sum.id, sum); renderSidebar(); updateHeader(); }, function () {});
      });
  }

  function curThreadRunning() {
    if (!cur) return false;
    if (cur.branches && cur.branches.branches) {
      var active = cur.branches.branches.find(function (b) { return b.id === cur.branchId; });
      if (active) return active.status === "running";
    }
    var s = S.sessions.get(cur.id);
    return !!(s && (s.active_status ? s.active_status === "running" : s.status === "running"));
  }

  function updateComposer() {
    if (newView && !cur) {
      newView.sendBtn.textContent = providerReady() ? "Send" : "Set up a provider →";
      return;
    }
    if (!cur) return;
    var running = curThreadRunning();
    cur.stopBtn.hidden = !running;
    cur.sendBtn.disabled = !S.connected;
    cur.sendBtn.textContent = !providerReady() ? "Set up a provider →" : running ? "Queue" : "Send";
    cur.sendBtn.title = !providerReady() ? "No LLM provider is configured, so nothing can be sent"
      : running ? "Queued until the current turn finishes" : "Send (Enter)";
  }

  // messages

  function renderMessages() {
    var c = cur;
    c.msgsEl.textContent = "";
    c.toolCards.clear();
    c.ctx = null;
    c.messages.forEach(appendMessage);
    if (!c.messages.length && !c.draft) {
      var s = S.sessions.get(c.id) || { id: c.id };
      c.msgsEl.append(el("div", { class: "empty-chat" }, svgEl(iconSvg(s, 64)),
        el("p", { text: "Ask anything. The agent works in " + tildify(s.cwd || "") + "." })));
    }
    renderDraft();
    renderTurnMarks();
    renderForkMarks();
    c.stick = true;
    afterContent();
  }

  // a divider where the model/provider or the working directory changes between messages
  function contextDivider(c, m) {
    var prev = c.ctx || {};
    var parts = [];
    if (m.model && prev.model && (m.model !== prev.model || (m.provider && prev.provider && m.provider !== prev.provider)))
      parts.push(el("span", { class: "ctx-model", title: "From here on, messages were written with this model" }, "model → ",
        el("strong", { text: m.model }), m.provider ? el("span", { class: "muted", text: " (" + m.provider + ")" }) : null));
    if (m.role === "assistant" && prev.answered && (m.effort || null) !== (prev.effort || null))
      parts.push(el("span", { class: "ctx-effort", title: "From here on, messages were written with this effort level" }, "effort → ",
        el("strong", { text: m.effort || "none" })));
    if (m.cwd && prev.cwd && m.cwd !== prev.cwd)
      parts.push(el("span", { class: "ctx-cwd", title: m.cwd }, "cwd → ", el("code", { text: tildify(m.cwd) })));
    c.ctx = { model: m.model || prev.model, provider: m.provider || prev.provider, cwd: m.cwd || prev.cwd,
      effort: m.role === "assistant" ? m.effort : prev.effort, answered: prev.answered || m.role === "assistant" };
    if (parts.length) c.msgsEl.append(el("div", { class: "ctx-divider" }, parts));
  }

  function appendMessage(m) {
    var c = cur;
    var empty = $(".empty-chat", c.msgsEl);
    if (empty) empty.remove();
    contextDivider(c, m);
    if (isToolResultMsg(m)) {
      contentOf(m).forEach(function (b) {
        var card = c.toolCards.get(b.tool_use_id);
        if (card) setToolResult(card, b);
        else {
          card = toolCard({ type: "tool_use", id: b.tool_use_id, name: "result", input: {} });
          setToolResult(card, b);
          c.msgsEl.append(el("div", { class: "msg assistant", "data-mid": m.id }, card));
        }
      });
      return;
    }
    if (isCompactionMsg(m)) {
      // everything above is no longer sent to the model
      c.msgsEl.querySelectorAll(".msg, .ctx-divider").forEach(function (e) { e.classList.add("pre-compact"); });
      c.msgsEl.append(compactionCard(m));
      return;
    }
    if (isPeerMsg(m)) { c.msgsEl.append(peerMsgEl(m)); return; }
    var sr = m.role === "user" ? parseSubResult(userText(m)) : null;
    var br = !sr && m.role === "user" ? parseBranchResult(userText(m)) : null;
    c.msgsEl.append(sr ? subResultCard(m, sr) : br ? branchResultCard(m, br) : m.role === "user" ? userBubble(m) : assistantMsg(m));
  }

  // ---- unified session threads (/aside + subagents) + two-mode minimap (v16/v17) ---------------

  var LANE_COLORS = ["var(--accent)", "#2c9a5a", "#d97706", "#9333ea", "#0891b2", "#e11d48"];
  function laneColor(i) { return LANE_COLORS[Math.abs(i || 0) % LANE_COLORS.length]; }
  function threadHref(sid, bid) { return "#session/" + encodeURIComponent(sid) + "/" + encodeURIComponent(bid); }

  function parseBranchResult(text) {
    var m = /^<(?:branch-result|thread-message)((?:\s+[\w-]+=(?:"[^"]*"|'[^']*'))*)\s*>([\s\S]*?)(?:<\/(?:branch-result|thread-message)>\s*)?$/.exec(text || "");
    if (!m) return null;
    var attrs = {}, re = /([\w-]+)=(?:"([^"]*)"|'([^']*)')/g, a;
    while ((a = re.exec(m[1]))) attrs[a[1]] = unescAttr(a[2] != null ? a[2] : a[3]);
    return { branch_id: attrs.from || attrs.branch_id || "", title: attrs.title || "",
      kind: attrs.kind || "branch", status: attrs.status || "", body: m[2] };
  }
  function isBranchResultMsg(m) { return m.role === "user" && !!parseBranchResult(userText(m)); }
  function branchResultCard(m, r) {
    var st = r.status || "completed";
    var kind = r.kind || "branch";
    var lbl = kind === "subagent" ? "Subagent report" : kind === "aside" ? "Aside report" : "Thread message";
    var jump = r.branch_id && cur
      ? el("a", { class: "link-btn branch-jump", href: threadHref(cur.id, r.branch_id), text: "Open thread →" })
      : null;
    return el("div", { class: "msg subresult", "data-mid": m.id },
      el("div", { class: "subresult-card branch-result-card kind-" + kind + (st === "completed" ? "" : " " + st) },
        el("div", { class: "subresult-head" },
          el("span", { class: "lbl", text: lbl }),
          el("span", { class: "desc", text: r.title || r.branch_id || "thread" }),
          el("span", { class: "badge st-" + st, text: st }),
          jump),
        mdDiv(r.body.trim() || "(no output)", null, m.links)));
  }

  function branchHeaderBadge(s) {
    if (!cur || !cur.branches || !cur.branches.branches || cur.branches.branches.length <= 1) return null;
    var list = cur.branches.branches;
    var active = list.find(function (b) { return b.id === cur.branchId; }) || list[0];
    var isAside = active && !active.is_main && active.kind !== "subagent";
    var isSub = active && active.kind === "subagent";
    var lbl = threadGlyph(active) + " " + (active ? active.name : "Main") + " · " + list.length;
    return el("button", { class: "badge branch-btn" + (isAside ? " is-aside" : "") + (isSub ? " is-subagent" : ""),
      type: "button",
      title: (active ? active.name + " (" + active.kind + (isSub ? ", " + (active.context_mode || "fresh") + " context" : "") + ")" : "") +
        "\nClick to " + (cur.minimapExpanded ? "minimize" : "expand") + " the thread graph sidebar",
      onclick: function () { setMinimapExpanded(!cur.minimapExpanded); }, text: lbl });
  }

  function setMinimapExpanded(exp) {
    if (!cur) return;
    cur.minimapExpanded = !!exp;
    LS.set("minimapExpanded", cur.minimapExpanded);
    renderMinimap();
    updateHeader();
  }

  function branchLaneMap(branchesList) {
    var map = new Map();
    if (!branchesList) return map;
    var nextLane = 1;
    branchesList.forEach(function (b) {
      if (b.is_main || b.kind === "main") map.set(b.id, 0);
      else map.set(b.id, nextLane++);
    });
    return map;
  }

  function applyBranchSwitch(snap, animate) {
    var c = cur;
    if (!c) return;
    if (snap.session) S.sessions.set(c.id, snap.session);
    c.branchId = snap.branch_id || (snap.branches && snap.branches.active_branch_id) || c.branchId;
    c.branches = snap.branches || c.branches;
    c.messages = snap.messages || [];
    c.draft = snap.draft || null;
    c.queue = snap.queue || [];
    c.tools = snap.tools || {};
    if (snap.now) c.skew = snap.now - Date.now();
    // Keep URL hash synced with this tab's viewed thread without adding redundant history entries
    if (c.branchId) {
      var want = threadHref(c.id, c.branchId);
      if (location.hash !== want && S.route && S.route.name === "session" && S.route.id === c.id) {
        S.route.bid = c.branchId;
        history.replaceState(null, "", want);
      }
    }
    renderMessages();
    renderMinimap();
    renderQueue();
    updateHeader();
    updateComposer();
    renderSidebar();
    if (animate && c.inner) {
      c.inner.classList.remove("branch-slide");
      void c.inner.offsetWidth;
      c.inner.classList.add("branch-slide");
    }
  }

  function switchBranch(bid) {
    var c = cur;
    if (!c || !bid || bid === c.branchId) return;
    // Per-tab navigation via hash: updates only THIS tab (and integrates with browser Back/Forward)
    location.hash = threadHref(c.id, bid);
  }

  function recomputeOfflineBranches(bd, activeBid) {
    var activePath = bd.threads[activeBid] || [];
    var activeSet = new Set(activePath);
    var branches = bd.info.branches.map(function (b) { return Object.assign({}, b, { is_active: b.id === activeBid }); });
    var byId = {};
    branches.forEach(function (b) { byId[b.id] = b; });
    var forks = {};
    var grouped = new Map();
    branches.forEach(function (b) {
      if (b.id === activeBid) return;
      if (b.context_mode === "fresh") {
        var attach = b.spawned_at_id;
        var fkey = attach != null && activeSet.has(attach) ? String(attach) : (b.parent_branch_id === activeBid ? "root" : null);
        if (fkey) {
          var fgk = fkey + "|f:" + b.id;
          grouped.set(fgk, { key: fkey, bids: [b.id] });
        }
        return;
      }
      var p = bd.threads[b.id] || [];
      if (b.kind === "subagent" && b.spawned_at_id != null && activeSet.has(b.spawned_at_id)) {
        grouped.set(String(b.spawned_at_id) + "|sub:" + b.id, { key: String(b.spawned_at_id), bids: [b.id] });
        return;
      }
      var lca = null;
      for (var i = p.length - 1; i >= 0; i--) if (activeSet.has(p[i])) { lca = p[i]; break; }
      var key = lca != null ? String(lca) : "root";
      var idx = lca != null ? p.indexOf(lca) : -1;
      var fa = idx + 1 < p.length ? p[idx + 1] : null;
      var gkey = key + "|" + (fa != null ? "m:" + fa : "s:" + b.id);
      if (!grouped.has(gkey)) grouped.set(gkey, { key: key, bids: [] });
      grouped.get(gkey).bids.push(b.id);
    });
    grouped.forEach(function (g) {
      g.bids.sort(function (a, b) {
        var sa = byId[a], sb = byId[b];
        return (sa.is_main ? 0 : 1) - (sb.is_main ? 0 : 1) || (sa.created_at || 0) - (sb.created_at || 0);
      });
      var rep = byId[g.bids[0]];
      if (!forks[g.key]) forks[g.key] = [];
      forks[g.key].push({ branch_id: rep.id, name: rep.name, kind: rep.kind, context_mode: rep.context_mode || "fork",
        status: "idle", own_messages: rep.own_messages, is_main: rep.is_main, nested: g.bids.length - 1 });
    });
    var act = byId[activeBid];
    return { active_branch_id: activeBid, main_branch_id: bd.info.main_branch_id,
      active_origin_id: act ? act.origin_id : null, active_context_mode: act ? act.context_mode : "fork",
      branches: branches, forks: forks };
  }

  function startAsideFrom(mid) {
    var c = cur;
    if (!c || S.offline) return;
    rpcToast("branches.create", { session_id: c.id, from_message_id: mid == null ? undefined : mid,
      parent_branch_id: c.branchId || undefined }).then(function (r) {
      if (cur === c && r && r.snapshot) {
        if (cur.branchId !== r.snapshot.branch_id) applyBranchSwitch(r.snapshot, true);
        c.ta.focus();
      }
    }).catch(function () {});
  }

  function renameBranch(bid, currentName) {
    var c = cur;
    if (!c || S.offline) return;
    var next = prompt("Thread title:", currentName || "");
    if (!next || !next.trim() || next.trim() === currentName) return;
    rpcToast("branches.rename", { session_id: c.id, branch_id: bid, title: next.trim() }).catch(function () {});
  }

  function promoteBranch(bid) {
    var c = cur;
    if (!c || S.offline) return;
    rpcToast("branches.promote", { session_id: c.id, branch_id: bid }).then(function () {
      toast("Set as main thread");
    }).catch(function () {});
  }

  function deleteBranch(bid, name) {
    var c = cur;
    if (!c || S.offline) return;
    if (!confirm('Delete thread "' + (name || bid) + '"? Messages unique to this thread will be removed.')) return;
    rpcToast("branches.delete", { session_id: c.id, branch_id: bid }).catch(function () {});
  }

  function findMsgAnchor(c, mid) {
    var byId = {};
    c.messages.forEach(function (m, i) { byId[m.id] = i; });
    var start = byId[mid];
    if (start === undefined) return null;
    for (var i = start; i >= 0; i--) {
      var els = c.msgsEl.querySelectorAll('[data-mid="' + c.messages[i].id + '"]');
      if (els.length) return els[els.length - 1];
    }
    return null;
  }

  function renderForkMarks() {
    var c = cur;
    if (!c || !c.msgsEl) return;
    c.msgsEl.querySelectorAll(".branch-fork, .branch-origin-div").forEach(function (e) { e.remove(); });
    if (!c.branches || !c.branches.branches || c.branches.branches.length <= 1) return;
    var info = c.branches;
    var lanes = branchLaneMap(info.branches);
    var active = info.branches.find(function (b) { return b.id === c.branchId; });

    // 1. Active thread origin banner/divider
    if (active && !active.is_main) {
      var parentB = active.parent_branch_id ? info.branches.find(function (b) { return b.id === active.parent_branch_id; }) : null;
      var kindLbl = active.kind === "subagent" ? ("subagent · " + (active.context_mode || "fresh") + " context")
        : active.kind === "edit" ? "version" : "aside";
      var div = el("div", { class: "branch-origin-div", "data-branch-origin": active.id },
        el("span", null, (active.context_mode === "fresh" ? "◌ " : "↳ ") + kindLbl + ": ", el("strong", { text: active.name })),
        parentB ? el("a", { class: "bact", href: threadHref(c.id, parentB.id), title: "Back to " + parentB.name,
          text: "← " + parentB.name }) : null,
        S.offline ? null : el("button", { class: "bact", type: "button", title: "Make this the main thread", text: "Set as main",
          onclick: function () { promoteBranch(active.id); } }),
        S.offline ? null : el("button", { class: "bact", type: "button", title: "Rename this thread", text: "Rename",
          onclick: function () { renameBranch(active.id, active.name); } }));
      if (active.origin_id != null) {
        var anchor = findMsgAnchor(c, active.origin_id);
        if (anchor) anchor.after(div);
        else c.msgsEl.prepend(div);
      } else {
        c.msgsEl.prepend(div);
      }
    }

    // 2. Inline fork/spawn strips along the displayed thread
    var forks = info.forks || {};
    Object.keys(forks).forEach(function (key) {
      var chips = forks[key];
      if (!chips || !chips.length) return;
      var total = chips.reduce(function (acc, ch) { return acc + 1 + (ch.nested || 0); }, 0);
      var strip = el("div", { class: "branch-fork", "data-fork-at": key },
        el("span", { class: "branch-fork-lbl", text: "⑂ " + total + " thread" + (total === 1 ? "" : "s") }));
      chips.forEach(function (ch) {
        var col = laneColor(lanes.get(ch.branch_id) || 1);
        var isFresh = ch.context_mode === "fresh";
        var btn = el("a", { class: "branch-chip" + (ch.is_main ? " is-main" : "") + (isFresh ? " is-fresh" : ""),
          href: threadHref(c.id, ch.branch_id), style: "--lane-color:" + col, "data-branch-id": ch.branch_id,
          title: ch.name + " (" + ch.kind + (ch.kind === "subagent" ? ", " + (ch.context_mode || "fresh") + " context" : "") +
            ", " + ch.own_messages + " message" + (ch.own_messages === 1 ? "" : "s") +
            (ch.nested ? ", +" + ch.nested + " sub-thread" + (ch.nested === 1 ? "" : "s") : "") +
            ")\nClick to switch this tab · middle-click to open in a new tab" },
          el("span", { class: "bdot" }),
          el("span", { class: "bname", text: ch.name }),
          ch.is_main ? el("span", { class: "btag", text: "main" }) : (ch.kind === "subagent" ? el("span", { class: "btag", text: isFresh ? "sub·fresh" : "sub·fork" }) : null),
          ch.status === "running" ? el("span", { class: "status-dot", title: "running" }) : null,
          el("span", { class: "bmeta", text: ch.own_messages + "m" + (ch.nested ? " +" + ch.nested : "") }));
        strip.append(btn);
      });
      if (!S.offline && key !== "root") {
        strip.append(el("button", { class: "branch-add", type: "button", title: "Start another aside from this message",
          text: "+ Aside", onclick: function () { startAsideFrom(Number(key)); } }));
      }
      if (key === "root") {
        c.msgsEl.prepend(strip);
      } else {
        var anchor = findMsgAnchor(c, Number(key));
        if (anchor) anchor.after(strip);
      }
    });
  }

  // Build SVG markup for the thread graph (shared by both minimal translucent mode and expanded sidebar)
  function buildGraphSvg(ordered, kids, colorMap, opts) {
    var ROW_H = opts.rowH, PAD_X = opts.padX, LANE_W = opts.laneW, mini = !!opts.mini;
    var rowOf = new Map(), laneOf = new Map(), maxLane = 0;
    ordered.forEach(function (item, idx) {
      rowOf.set(item.b.id, idx);
      var lane = item.b.is_main ? 0 : Math.min(4, Math.max(1, item.depth));
      laneOf.set(item.b.id, lane);
      if (lane > maxLane) maxLane = lane;
    });
    var graphW = PAD_X * 2 + maxLane * LANE_W;
    var barArea = mini ? 34 : 0;
    var svgW = graphW + barArea;
    var svgH = ordered.length * ROW_H;
    var svgParts = [];

    // Vertical trunks for parent threads down to their last direct child
    ordered.forEach(function (item, idx) {
      var b = item.b, ch = kids.get(b.id) || [];
      if (!ch.length) return;
      var lastIdx = idx;
      ch.forEach(function (k) { if (rowOf.has(k.id) && rowOf.get(k.id) > lastIdx) lastIdx = rowOf.get(k.id); });
      if (lastIdx > idx) {
        var px = PAD_X + laneOf.get(b.id) * LANE_W;
        var y0 = idx * ROW_H + ROW_H / 2;
        var y1 = lastIdx * ROW_H + ROW_H / 2 - (mini ? 6 : 14);
        var pcol = laneColor(colorMap.get(b.id) || 0);
        if (y1 > y0) {
          svgParts.push('<line x1="' + px + '" y1="' + y0 + '" x2="' + px + '" y2="' + y1 +
            '" stroke="' + pcol + '" stroke-width="' + (mini ? 1.5 : (b.is_active ? 2.4 : 1.7)) + '" stroke-opacity="0.45" stroke-linecap="round"/>');
        }
      }
    });

    // Curved parent -> child connectors (solid = forked context, dashed = fresh context!)
    ordered.forEach(function (item, idx) {
      var b = item.b;
      var x = PAD_X + laneOf.get(b.id) * LANE_W;
      var y = idx * ROW_H + ROW_H / 2;
      var col = laneColor(colorMap.get(b.id) || 0);
      var isFresh = b.context_mode === "fresh";
      var dash = isFresh ? (mini ? ' stroke-dasharray="2 2"' : ' stroke-dasharray="3.5 2.5"') : "";
      var pId = b.parent_branch_id;
      if (pId && rowOf.has(pId)) {
        var px = PAD_X + laneOf.get(pId) * LANE_W;
        var py = Math.max(rowOf.get(pId) * ROW_H + ROW_H / 2, y - ROW_H * 0.72);
        var midY = (py + y) / 2;
        svgParts.push('<path d="M' + px + ',' + py + ' C' + px + ',' + midY + ' ' + x + ',' + midY + ' ' + x + ',' + y +
          '" fill="none" stroke="' + col + '" stroke-width="' + (mini ? (b.is_active ? 2 : 1.3) : (b.is_active ? 2.4 : 1.7)) +
          '" stroke-opacity="' + (b.is_active ? "1" : "0.78") + '"' + dash + ' stroke-linecap="round"/>');
      } else if (idx > 0) {
        var rx = PAD_X, ry = ROW_H / 2;
        svgParts.push('<path d="M' + rx + ',' + ry + ' C' + rx + ',' + (ry + 8) + ' ' + x + ',' + (y - 8) + ' ' + x + ',' + y +
          '" fill="none" stroke="' + col + '" stroke-width="1.4" stroke-dasharray="2.5 2" stroke-opacity="0.6"/>');
      }
    });

    // Nodes + (in minimal mode) Sublime Text-style miniature proportional bars
    var maxMsgs = Math.max(1, ordered.reduce(function (m, it) { return Math.max(m, it.b.own_messages || 1); }, 1));
    ordered.forEach(function (item, idx) {
      var b = item.b;
      var x = PAD_X + laneOf.get(b.id) * LANE_W;
      var y = idx * ROW_H + ROW_H / 2;
      var col = laneColor(colorMap.get(b.id) || 0);
      var isFresh = b.context_mode === "fresh";
      var rActive = mini ? 3.2 : 4.2, rNorm = mini ? 2.4 : 3.4;
      if (b.is_active) {
        svgParts.push('<circle cx="' + x + '" cy="' + y + '" r="' + (mini ? 5.6 : 7.2) + '" fill="' + col + '" fill-opacity="0.24"/>');
        svgParts.push('<circle cx="' + x + '" cy="' + y + '" r="' + rActive + '" fill="' + (isFresh ? "var(--panel)" : col) +
          '" stroke="' + (isFresh ? col : "var(--panel)") + '" stroke-width="' + (isFresh ? 2 : 1.4) + '"/>');
      } else {
        svgParts.push('<circle cx="' + x + '" cy="' + y + '" r="' + rNorm + '" fill="var(--panel)" stroke="' + col +
          '" stroke-width="' + (mini ? 1.6 : 2) + '"' + (isFresh ? ' stroke-dasharray="1.8 1.2"' : "") + '/>');
      }
      if (mini) {
        var bw = Math.max(6, Math.round(28 * Math.sqrt((b.own_messages || 1) / maxMsgs)));
        var bx = graphW;
        svgParts.push('<rect x="' + bx + '" y="' + (y - 2) + '" width="' + bw + '" height="4" rx="2" fill="' + col +
          '" fill-opacity="' + (b.is_active ? "0.9" : "0.35") + '"><title>' + MD.escape(b.name + " (" + b.kind + ", " + b.own_messages + "m)") + '</title></rect>');
      }
    });

    return '<svg xmlns="http://www.w3.org/2000/svg" width="' + svgW + '" height="' + svgH +
      '" viewBox="0 0 ' + svgW + ' ' + svgH + '">' + svgParts.join("") + "</svg>";
  }

  function renderMinimap() {
    var c = cur;
    if (!c || !c.minimapEl) return;
    var info = c.branches;
    var list = info && info.branches ? info.branches : [];
    if (list.length <= 1) {
      c.minimapEl.hidden = true;
      c.minimapEl.textContent = "";
      c.minimapEl.className = "branch-minimap";
      return;
    }
    c.minimapEl.hidden = false;
    c.minimapEl.textContent = "";
    var tree = orderThreadsTree(list);
    var ordered = tree.ordered, kids = tree.kids;
    var colorMap = branchLaneMap(list);

    // Mode A (default): Sublime Text-like translucent minimal strip; clicking opens the full graph sidebar
    if (!c.minimapExpanded) {
      c.minimapEl.className = "branch-minimap minimal";
      c.minimapEl.tabIndex = 0;
      c.minimapEl.setAttribute("role", "button");
      c.minimapEl.title = "Thread minimap (" + list.length + " threads) — click to open the full graph sidebar";
      c.minimapEl.onclick = function () { setMinimapExpanded(true); };
      c.minimapEl.onkeydown = function (e) { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); setMinimapExpanded(true); } };
      var miniSvg = el("div", { class: "bm-svg", html: buildGraphSvg(ordered, kids, colorMap, { rowH: 18, padX: 8, laneW: 10, mini: true }) });
      c.minimapEl.append(
        el("div", { class: "bm-mini-head" }, el("span", { text: "⑂ " + list.length }), el("span", { text: "◂" })),
        miniSvg);
      return;
    }

    // Mode B (expanded): full right-hand graph sidebar
    c.minimapEl.className = "branch-minimap expanded";
    c.minimapEl.removeAttribute("tabindex");
    c.minimapEl.removeAttribute("role");
    c.minimapEl.title = "";
    c.minimapEl.onclick = null;
    c.minimapEl.onkeydown = null;

    var svg = el("div", { class: "bm-svg", html: buildGraphSvg(ordered, kids, colorMap, { rowH: 40, padX: 12, laneW: 14, mini: false }) });
    var rows = el("div", { class: "bm-rows" });
    ordered.forEach(function (item) {
      var b = item.b;
      var href = threadHref(c.id, b.id);
      var acts = el("div", { class: "bm-row-acts" },
        el("a", { class: "bm-act", href: href, target: "_blank", rel: "noopener", title: "Open in a new tab", text: "↗",
          onclick: function (e) { e.stopPropagation(); } }),
        S.offline || b.is_main ? null : el("button", { class: "bm-act", type: "button", title: "Set as main thread", text: "★",
          onclick: function (e) { e.preventDefault(); e.stopPropagation(); promoteBranch(b.id); } }),
        S.offline ? null : el("button", { class: "bm-act", type: "button", title: "Rename thread", text: "✎",
          onclick: function (e) { e.preventDefault(); e.stopPropagation(); renameBranch(b.id, b.name); } }),
        S.offline ? null : el("button", { class: "bm-act del", type: "button", title: "Delete thread", text: "✕",
          onclick: function (e) { e.preventDefault(); e.stopPropagation(); deleteBranch(b.id, b.name); } }));
      var kindTag = b.is_main ? "main" : (b.kind === "subagent" ? ("sub·" + (b.context_mode || "fresh")) : b.kind);
      var row = el("a", { class: "bm-row" + (b.is_active ? " active" : "") + (b.is_main ? " is-main" : "") +
          (b.kind === "subagent" ? " is-subagent" : ""),
        href: href, "data-branch-id": b.id,
        title: b.name + " (" + b.id + ", " + b.kind + ", " + (b.context_mode || "fork") + " context)\n" +
          b.own_messages + " message" + (b.own_messages === 1 ? "" : "s") +
          (b.fork_preview ? "\nAt: " + b.fork_preview : "") },
        el("div", { class: "bm-row-info" },
          el("span", { class: "bm-row-title", text: b.name }),
          el("span", { class: "bm-row-sub" },
            el("span", { class: "btag", text: kindTag }),
            el("span", { text: b.own_messages + "m" }),
            b.status === "running" ? el("span", { class: "status-dot", title: "running" }) : null)),
        acts);
      rows.append(row);
    });

    c.minimapEl.append(
      el("div", { class: "bm-head" },
        el("span", { text: "⑂ Threads (" + list.length + ")" }),
        el("span", { class: "spacer" }),
        S.offline ? null : el("button", { class: "link-btn", type: "button", title: "Start a new aside from the current tip (/aside)",
          text: "+ Aside", onclick: function () { startAsideFrom(null); } }),
        el("button", { class: "icon-btn", type: "button", style: "width:22px;height:22px", title: "Minimize to compact minimap", text: "⇥",
          onclick: function () { setMinimapExpanded(false); } })),
      el("div", { class: "bm-body" }, el("div", { class: "bm-graph-wrap" }, svg, rows)));
  }

  function decorateBranchCard(card, b) {
    var name = card._name.textContent;
    var input = b.input || {};
    if (name === "threads" || name === "branches") { card._sum.textContent = "list session threads & peers"; return; }
    if (name === "spawn") {
      var ctx = input.context || "fresh";
      if (input.description) {
        card._sum.textContent = "";
        card._sum.append(el("span", { class: "badge", text: ctx }), " ", el("span", { class: "host", text: input.description }));
      }
      var tid = card._threadId;
      if (b.content !== undefined) {
        try { var parsed = JSON.parse(resultText(b)); tid = parsed.thread_id || parsed.branch_id || tid; } catch (e) { /* not json */ }
      }
      if (tid && cur && card._threadId !== tid) {
        card._threadId = tid;
        if (card._child) card._child.remove();
        var th = cur.branches && cur.branches.branches ? cur.branches.branches.find(function (x) { return x.id === tid; }) : null;
        card._child = el("span", { class: "sub-child" }, "→ ",
          el("a", { class: "sub-link", href: threadHref(cur.id, tid), text: th ? th.name : tid }));
        card._child.addEventListener("click", function (e) { e.stopPropagation(); });
        card._stat.before(card._child);
      }
      return;
    }
    var targetId = typeof input.target_id === "string" ? input.target_id : (typeof input.branch_id === "string" ? input.branch_id : "");
    var found = cur && cur.branches && cur.branches.branches ? cur.branches.branches.find(function (x) { return x.id === targetId; }) : null;
    var peer = !found ? S.sessions.get(targetId) : null;
    var label = found ? found.name : (peer ? titleOf(peer) : targetId);
    var text = typeof input.question === "string" ? input.question : (typeof input.text === "string" ? input.text : (typeof input.prompt === "string" ? input.prompt : ""));
    card._sum.textContent = "";
    card._sum.append("⑂ ", el("span", { class: "host", text: label || "?" }),
      text ? el("span", { class: "path", text: "  " + text.replace(/\s+/g, " ").slice(0, 120) }) : null);
  }

  // ---- peer sessions ---------------------------------------------------------------------------
  // Sessions working in the same repository at the same time (server: sfh/peers.py, DESIGN v10).

  var PEER_ACTIVE_MS = 15 * 60 * 1000;
  function sessionActive(s) { return s.status === "running" || (!!s.updated_at && Date.now() - s.updated_at < PEER_ACTIVE_MS); }
  function peersOf(s) {
    if (!s || !s.repo) return [];
    var out = [];
    S.sessions.forEach(function (o) {
      if (o.id !== s.id && o.repo === s.repo && o.message_count > 0 && sessionActive(o)) out.push(o);
    });
    return out.sort(function (a, b) { return (a.status === "running" ? 0 : 1) - (b.status === "running" ? 0 : 1) || (b.updated_at || 0) - (a.updated_at || 0); });
  }
  function peerActivity(o) {
    if (o.status === "running") return o.activity ? o.activity.tool + (o.activity.summary ? " " + o.activity.summary : "") : "running";
    return "idle · " + ago(o.updated_at);
  }
  function peerFiles(o) {  // [{path, how}] claims + recent edits (paths relative to the repo)
    var out = [];
    (o.claims || []).forEach(function (c) { out.push({ path: c.path, how: c.auto ? "editing" : "claimed" + (c.note ? ": " + c.note : "") }); });
    (o.edited || []).forEach(function (e) { if (!out.some(function (x) { return x.path === e.path; })) out.push({ path: e.path, how: "edited " + ago(e.at) }); });
    return out;
  }
  function pathsOverlap(a, b) { return a === b || a.indexOf(b + "/") === 0 || b.indexOf(a + "/") === 0; }
  function peerTip(o) {
    return [titleOf(o), o.id + " · " + (o.parent_id ? "subagent" : "session"), peerActivity(o)]
      .concat(peerFiles(o).map(function (f) { return "  " + f.path + " (" + f.how + ")"; })).join("\n");
  }
  function peerBadge(s) {
    if (!sessionActive(s)) return null;
    var ps = peersOf(s).filter(function (o) { return o.id !== s.parent_id && o.parent_id !== s.id; });
    if (!ps.length) return null;
    return el("span", { class: "peer-count", title: "Also active in " + tildify(s.repo) + ":\n\n" + ps.map(peerTip).join("\n\n"), text: "⇄" + ps.length });
  }
  function inboxBadge(s) {
    var n = (s.inbox || []).length;
    if (!n) return null;
    var q = s.inbox.filter(function (m) { return m.kind === "question"; }).length;
    return el("span", { class: "inbox-count" + (q ? " q" : ""), title: n + " message" + (n === 1 ? "" : "s") + " from other sessions waiting" + (q ? " (" + q + " question" + (q === 1 ? "" : "s") + ")" : ""), text: (q ? "?" : "✉") + n });
  }

  function renderPeerStrip() {
    var c = cur;
    if (!c || !c.peersEl) return;
    var s = S.sessions.get(c.id);
    var ps = s ? peersOf(s) : [];
    c.peersEl.textContent = "";
    c.peersEl.hidden = !ps.length;
    if (!ps.length) return;
    var mine = peerFiles(s);
    var row = el("div", { class: "peer-row" }, el("span", { class: "peer-lbl", title: "Other sessions working in " + s.repo + " (running, or active in the last 15 minutes)", text: "Also active here" }));
    var conflicts = [];
    ps.forEach(function (o) {
      var files = peerFiles(o);
      var rel = o.id === s.parent_id ? "parent" : o.parent_id === s.id ? "subagent" : o.parent_id && o.parent_id === s.parent_id ? "sibling" : null;
      row.append(el("a", { class: "peer-chip" + (o.status === "running" ? " running" : ""), href: "#session/" + encodeURIComponent(o.id), title: peerTip(o) },
        svgEl(iconSvg(o, 16)),
        el("span", { class: "pt", text: titleOf(o) }),
        rel ? el("span", { class: "prel", text: rel }) : null,
        o.status === "running" ? el("span", { class: "status-dot" }) : null,
        el("span", { class: "pact", text: peerActivity(o) }),
        files.length ? el("span", { class: "pfiles", text: files.length + " file" + (files.length === 1 ? "" : "s") }) : null));
      files.forEach(function (f) {
        var hit = mine.find(function (m) { return pathsOverlap(m.path, f.path); });
        if (hit) conflicts.push({ peer: o, path: f.path, how: f.how });
      });
    });
    c.peersEl.append(row);
    conflicts.slice(0, 3).forEach(function (x) {
      c.peersEl.append(el("div", { class: "peer-conflict" }, "⚠ ",
        el("a", { href: "#session/" + encodeURIComponent(x.peer.id), text: titleOf(x.peer) }),
        " also works on ", el("code", { text: x.path }), el("span", { class: "muted", text: " (" + x.how + ")" })));
    });
    if (conflicts.length > 3) c.peersEl.append(el("div", { class: "peer-conflict muted", text: "… and " + (conflicts.length - 3) + " more shared files" }));
  }

  function renderInbox(s) {
    var c = cur;
    if (!c || !c.inboxEl) return;
    c.inboxEl.textContent = "";
    var box = (s && s.inbox) || [];
    if (!box.length) return;
    var running = s.status === "running";
    var card = el("div", { class: "inbox-card" },
      el("div", { class: "inbox-head" },
        el("span", { class: "lbl", text: "Waiting for this session" }),
        el("span", { class: "muted small", text: running ? "delivered after its current step" : "held until you let it answer or send a prompt" }),
        running || S.offline || !providerReady() ? null : el("button", { class: "btn sm primary", type: "button", text: "Let it answer",
          title: "Deliver these messages now; the session starts a turn",
          onclick: function () { rpcToast("peers.deliver", { session_id: s.id }).then(function () { c.stick = true; }); } })));
    box.forEach(function (m) {
      var from = S.sessions.get(m.from) || { id: m.from, title: m.from_title };
      card.append(el("div", { class: "inbox-item" }, el("span", { class: "badge pk-" + m.kind, text: m.kind }),
        svgEl(iconSvg(from, 16)), childLink(m.from), el("span", { class: "itext", text: m.text })));
    });
    c.inboxEl.append(card);
    afterContent();
  }

  function isPeerMsg(m) {
    var c = contentOf(m);
    return m.role === "user" && c.length > 0 && c.every(function (b) { return b.peer_message || b.peer_sessions; });
  }
  function peerMsgEl(m) {
    var wrap = el("div", { class: "msg peer", "data-mid": m.id, title: msgTitle(m) || null });
    contentOf(m).forEach(function (b) {
      if (b.peer_message) {
        var pm = b.peer_message, from = S.sessions.get(pm.from) || { id: pm.from, title: pm.from_title };
        var lbl = pm.kind === "question" ? "Question from" : pm.kind === "reply" ? "Reply from" : "Message from";
        wrap.append(el("div", { class: "peer-card k-" + (pm.kind || "info") },
          el("div", { class: "peer-head" }, el("span", { class: "lbl", text: lbl }), svgEl(iconSvg(from, 16)), childLink(pm.from),
            pm.reply_to ? el("span", { class: "muted small", title: "answers message " + pm.reply_to, text: "↩ " + pm.reply_to }) : null,
            el("span", { class: "muted small pid", text: pm.id })),
          mdDiv(pm.text || "", null, m.links)));
      } else if (b.peer_sessions) {
        var ids = b.peer_sessions;
        var names = ids.map(function (id) { var o = S.sessions.get(id); return o ? titleOf(o) : id; });
        wrap.append(el("details", { class: "peer-note" },
          el("summary", null, "⇄ ", ids.length ? "Told the agent about " + ids.length + " active session" + (ids.length === 1 ? "" : "s") + ": " + names.join(", ") : "Told the agent no other sessions are active here"),
          el("pre", { text: b.text || "" })));
      }
    });
    return wrap;
  }
  function decorateSendCard(card, input) {
    if (typeof input.session_id !== "string" || !input.session_id) return;
    card._sum.textContent = "";
    var to = S.sessions.get(input.session_id) || { id: input.session_id };
    card._sum.append(input.kind ? el("span", { class: "badge pk-" + input.kind, text: input.kind }) : null, " → ", svgEl(iconSvg(to, 14)), " ",
      el("span", { class: "host", text: titleOf(to) === "Untitled session" ? input.session_id : titleOf(to) }),
      el("span", { class: "path", text: typeof input.text === "string" ? "  " + input.text.replace(/\s+/g, " ").slice(0, 120) : "" }));
  }

  // ---- subagents -------------------------------------------------------------------------------

  function unescAttr(s) {
    return s.replace(/&(#x[0-9a-f]+|#\d+|quot|apos|lt|gt|amp);/gi, function (all, e) {
      var k = e.toLowerCase();
      if (k[0] === "#") return String.fromCodePoint(k[1] === "x" ? parseInt(k.slice(2), 16) : parseInt(k.slice(1), 10));
      return { quot: '"', apos: "'", lt: "<", gt: ">", amp: "&" }[k];
    });
  }
  function parseSubResult(text) {
    var m = /^<subagent-result((?:\s+[\w-]+=(?:"[^"]*"|'[^']*'))*)\s*>([\s\S]*?)(?:<\/subagent-result>\s*)?$/.exec(text || "");
    if (!m) return null;
    var attrs = {}, re = /([\w-]+)=(?:"([^"]*)"|'([^']*)')/g, a;
    while ((a = re.exec(m[1]))) attrs[a[1]] = unescAttr(a[2] != null ? a[2] : a[3]);
    return { session_id: attrs.session_id || "", description: attrs.description || "", status: attrs.status || "", body: m[2] };
  }
  function isSubResultMsg(m) { return m.role === "user" && !!parseSubResult(userText(m)); }
  function isCompactionMsg(m) { return m.role === "user" && contentOf(m).some(function (b) { return b && b.compaction; }); }
  function compactionCard(m) {
    var text = userText(m), meta = contentOf(m).find(function (b) { return b.compaction; }) || {};
    return el("div", { class: "msg compaction-wrap", "data-mid": m.id, title: msgTitle(m) || null },
      el("details", { class: "compaction" },
        el("summary", null, el("span", { class: "lbl", text: "Conversation compacted" }),
          el("span", { class: "muted small", text: (meta.compacted ? meta.compacted + " messages → " : "") + "a summary of ~" +
            fmtTokens(Math.round(text.length / 4)) + " tokens. The model continues from here; the messages above are no longer sent." })),
        mdDiv(text, null, m.links)));
  }
  function childStatus(id) {
    var s = S.sessions.get(id);
    return s ? s.status || "idle" : "unknown";
  }
  function childLink(id) {
    var s = S.sessions.get(id);
    return el("a", { class: "sub-link", href: "#session/" + encodeURIComponent(id), "data-child": id, title: id,
      text: s && s.title ? s.title : id });
  }
  function childBadge(id) {
    var st = childStatus(id);
    return el("span", { class: "badge sub-status" + (st === "running" ? " running" : ""), "data-child-status": id, text: st });
  }
  function refreshChildBadges() {
    if (!cur) return;
    cur.msgsEl.querySelectorAll("[data-child-status]").forEach(function (b) {
      var st = childStatus(b.getAttribute("data-child-status"));
      b.textContent = st;
      b.className = "badge sub-status" + (st === "running" ? " running" : "");
    });
  }
  function subResultCard(m, r) {
    var st = r.status || "completed";
    return el("div", { class: "msg subresult", "data-mid": m.id },
      el("div", { class: "subresult-card" + (st === "completed" ? "" : " " + st) },
        el("div", { class: "subresult-head" },
          el("span", { class: "lbl", text: "Subagent result" }),
          el("span", { class: "desc", text: r.description || "subagent" }),
          el("span", { class: "badge st-" + st, text: st }),
          r.session_id ? childLink(r.session_id) : null),
        mdDiv(r.body.trim() || "(no output)", null, m.links)));
  }
  function decorateSubagentCard(card, b) {
    if (card._name.textContent !== "subagent") return;
    var input = b.input || {};
    if (input.description) card._sum.textContent = input.description;
    var id = input.session_id || card._childId;
    if (b.content !== undefined) {
      try { id = JSON.parse(resultText(b)).session_id || id; } catch (e) { /* not json */ }
    }
    if (!id || card._childId === id) return;
    card._childId = id;
    if (card._child) card._child.remove();
    card._child = el("span", { class: "sub-child" }, "→ ", childLink(id), childBadge(id));
    card._child.addEventListener("click", function (e) { e.stopPropagation(); });
    card._stat.before(card._child);
  }

  function userBubble(m) {
    var text = userText(m);
    var wrap = el("div", { class: "msg user", "data-mid": m.id, title: msgTitle(m) || null },
      el("div", { class: "bubble", text: text }),
      m.versions > 1 ? el("div", { class: "versions", title: "This message was edited; older versions are kept in the file", text: "edited · " + m.versions + " versions" }) : null,
      el("div", { class: "msg-actions" },
        el("button", { class: "link-btn", type: "button", text: "Copy", onclick: function () { copyText(text); toast("Copied"); } }),
        S.offline ? null : el("button", { class: "link-btn", type: "button", text: "Aside", title: "Start a side-branch from this message",
          onclick: function () { startAsideFrom(m.id); } }),
        S.offline || !providerReady() ? null : el("button", { class: "link-btn", type: "button", text: "Edit", title: "Edit and resend (↑ in an empty composer)",
          onclick: function () { startEdit(m.id); } })));
    return wrap;
  }

  function startEdit(mid) {
    if (!cur || S.offline || !providerReady()) return;
    var m = cur.messages.find(function (x) { return x.id === mid; });
    var wrap = $('.msg.user[data-mid="' + mid + '"]', cur.msgsEl);
    if (!m || !wrap || $(".edit-box", wrap)) return;
    var ta = el("textarea", { "aria-label": "Edit message" });
    ta.value = userText(m);
    var c = cur;
    function cancel() { wrap.replaceWith(userBubble(m)); c.ta.focus(); }
    function save() {
      var t = ta.value;
      if (!t.trim()) return;
      ta.disabled = true;
      rpcToast("chat.edit", { session_id: c.id, message_id: mid, text: t, branch_id: c.branchId || undefined }).then(function () { c.stick = true; c.ta.focus(); },
        function () { ta.disabled = false; });
    }
    ta.addEventListener("keydown", function (e) {
      if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); save(); }
      else if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); cancel(); }
    });
    ta.addEventListener("input", function () { autoGrow(ta); });
    var box = el("div", { class: "edit-box" }, ta,
      el("div", { class: "row" }, el("span", { class: "muted small", text: "Resending starts a new version from here (the old one stays in the file)." }),
        el("button", { class: "btn sm", type: "button", text: "Cancel", onclick: cancel }),
        el("button", { class: "btn sm primary", type: "button", text: "Save & resend", onclick: save })));
    wrap.textContent = "";
    wrap.append(box);
    autoGrow(ta);
    ta.focus();
    ta.setSelectionRange(ta.value.length, ta.value.length);
  }

  function editLast() {
    if (!cur) return false;
    for (var i = cur.messages.length - 1; i >= 0; i--) {
      var m = cur.messages[i];
      if (m.role === "user" && !isToolResultMsg(m) && !isSubResultMsg(m) && !isBranchResultMsg(m) && !isCompactionMsg(m) && !isPeerMsg(m)) { startEdit(m.id); return true; }
    }
    return false;
  }

  // links: the message's server-resolved local file links (undefined for old messages and notices,
  // which fall back to resolving on click)
  function mdDiv(text, cls, links) {
    var d = el("div", { class: "md" + (cls ? " " + cls : "") });
    d.innerHTML = MD.render(text || "", { links: links });
    mountArtifacts(d);
    return d;
  }
  // Streaming re-render: only top-level nodes from the first changed one on are replaced, so the stable
  // part of a draft (and a finished artifact's iframe in it) isn't re-created on every token.
  function setDraftHtml(e, html) {
    if (e._html === html) return;
    e._html = html;
    var tpl = document.createElement("template");
    tpl.innerHTML = html;
    var nodes = Array.prototype.slice.call(tpl.content.childNodes);
    var keys = nodes.map(function (n) { return n.nodeType === 1 ? n.outerHTML : n.nodeType + ":" + n.textContent; });
    var old = e._keys || [], k = 0;
    while (k < keys.length && k < old.length && keys[k] === old[k] && k < e.childNodes.length) k++;
    while (e.childNodes.length > k) e.lastChild.remove();
    for (var j = k; j < nodes.length; j++) e.append(nodes[j]);
    e._keys = keys;
    mountArtifacts(e);
  }

  // ---- artifacts: ```artifact fences (markdown.js renders the frame + source; see docs/DESIGN.md) ----
  // sandboxed without allow-same-origin: the artifact can't reach this page, its token or the API
  var ARTIFACT_SANDBOX = "allow-scripts allow-popups allow-modals";
  function artifactSource(art) { var c = $(".artifact-src code", art); return c ? c.textContent.replace(/\n$/, "") : ""; }
  function mountArtifacts(root) {
    root.querySelectorAll(".artifact:not(.pending) > .artifact-frame").forEach(function (slot) {
      if (slot.firstChild) return;
      var art = slot.parentNode;
      var f = el("iframe", { sandbox: ARTIFACT_SANDBOX, referrerpolicy: "no-referrer", title: art.dataset.title || "Artifact" });
      f.srcdoc = MD.artifactDoc(artifactSource(art));
      slot.append(f);
    });
  }
  function artifactAction(btn) {
    var art = btn.closest(".artifact");
    if (!art) return;
    var act = btn.dataset.artifactAct, title = art.dataset.title || "Artifact", src = artifactSource(art);
    if (act === "source") {
      var pre = $(".artifact-src", art);
      pre.hidden = !pre.hidden;
      btn.classList.toggle("on", !pre.hidden);
      enhanceCode(art);
    } else if (act === "full") {
      var slot = $(".artifact-frame", art);
      if (slot && slot.requestFullscreen) slot.requestFullscreen().catch(function () { /* ignore */ });
    } else if (act === "open") {
      // a wrapper page that embeds the artifact in the same sandbox (a blob: URL would get this page's origin)
      var page = "<!DOCTYPE html><meta charset=\"utf-8\"><title>" + MD.escape(title) + "</title>" +
        "<style>html,body{margin:0;height:100%}iframe{display:block;border:0;width:100%;height:100%}</style>" +
        '<iframe sandbox="' + ARTIFACT_SANDBOX + '" srcdoc="' + MD.escape(MD.artifactDoc(src, { autoHeight: false })) + '"></iframe>';
      var url = URL.createObjectURL(new Blob([page], { type: "text/html" }));
      window.open(url, "_blank", "noopener");
      setTimeout(function () { URL.revokeObjectURL(url); }, 60000);
    } else if (act === "download") {
      var a = el("a", { href: URL.createObjectURL(new Blob([src], { type: "text/html" })),
        download: (title.replace(/[^\w.\- ]+/g, "").trim().replace(/\s+/g, "-") || "artifact") + ".html" });
      document.body.append(a);
      a.click();
      a.remove();
      setTimeout(function () { URL.revokeObjectURL(a.href); }, 60000);
    }
  }
  window.addEventListener("message", function (e) {
    var h = e.data && e.data.sfhArtifactHeight;
    if (typeof h !== "number" || !isFinite(h)) return;
    var frames = document.querySelectorAll(".artifact-frame > iframe");
    for (var i = 0; i < frames.length; i++) {
      if (frames[i].contentWindow !== e.source) continue;
      frames[i].parentNode.style.height = Math.round(Math.max(60, Math.min(h + 2, window.innerHeight * 0.8))) + "px";
      return;
    }
  });
  function enhanceCode(root) {
    root.querySelectorAll(".md pre").forEach(function (pre) {
      if ($(".copy-code", pre)) return;
      var code = $("code", pre);
      if (code && code.dataset.lang) pre.append(el("span", { class: "lang-tag", text: code.dataset.lang }));
      pre.append(el("button", { class: "copy-code", type: "button", text: "Copy" }));
    });
  }

  // ---- durations -------------------------------------------------------------------------------
  // Stored blocks carry started_at/ended_at (ms, server clock); messages started_at (their request) and
  // created_at (stored = done); tool results when the tool ran. Timers of things still running advance in
  // TICK_MS steps so they don't flicker.

  var TICK_MS = 5000;
  function fmtDur(ms) {  // 3.4s, 42s, 2m 05s, 1h 03m
    var s = Math.max(0, ms || 0) / 1000;
    if (s < 10) return s.toFixed(1) + "s";
    if (s < 60) return Math.floor(s) + "s";
    var m = Math.floor(s / 60);
    if (m < 60) return m + "m " + String(Math.floor(s) % 60).padStart(2, "0") + "s";
    return Math.floor(m / 60) + "h " + String(m % 60).padStart(2, "0") + "m";
  }
  function liveDur(start, now) {
    var e = Math.floor(Math.max(0, now - start) / TICK_MS) * TICK_MS;
    return e < 60000 ? Math.floor(e / 1000) + "s" : fmtDur(e);
  }
  function blockDur(b) { return b && b.started_at && b.ended_at ? b.ended_at - b.started_at : null; }
  function serverNow() { return Date.now() + (cur ? cur.skew || 0 : 0); }
  // an element showing a running timer: prefix + elapsed since start, kept current by tickTimers()
  function liveTimer(e, start, prefix, suffix) {
    e.dataset.liveStart = start;
    e._prefix = prefix || "";
    e._suffix = suffix || "";
    e.textContent = e._prefix + liveDur(start, serverNow()) + e._suffix;
  }
  function stopTimer(e, text) { delete e.dataset.liveStart; e.textContent = text; }
  function tickTimers() {
    if (!cur) return;
    var now = serverNow();
    cur.root.querySelectorAll("[data-live-start]").forEach(function (e) {
      var t = e._prefix + liveDur(Number(e.dataset.liveStart), now) + e._suffix;
      if (e.textContent !== t) e.textContent = t;
    });
  }
  setInterval(tickTimers, 1000);

  // the running turn's timer in the chat footer
  function updateTurnTimer(s) {
    if (!cur || !cur.turnEl) return;
    var start = s && s.status === "running" ? turnStart(cur.messages) : null;
    cur.turnEl.hidden = !start;
    if (start) { if (Number(cur.turnEl.dataset.liveStart) !== start) liveTimer(cur.turnEl, start, "turn "); }
    else stopTimer(cur.turnEl, "");
  }
  function startsTurn(m) { return m.role === "user" && !isToolResultMsg(m) && !isCompactionMsg(m) && !isPeerMsg(m); }
  function turnStart(msgs) {
    for (var i = msgs.length - 1; i >= 0; i--) if (startsTurn(msgs[i])) return msgs[i].created_at || null;
    return null;
  }
  // {id of a turn's last message: duration}: a turn runs from a prompt (or delivered subagent result) to the
  // last message before the next one; the newest has none while it runs, turns without a reply none at all
  function turnEnds(msgs, running) {
    var out = {}, start = null, last = null, answered = false;
    function close() { if (start != null && last && answered) out[last.id] = last.created_at - start; }
    msgs.forEach(function (m) {
      if (startsTurn(m)) { close(); start = m.created_at || null; last = null; answered = false; return; }
      if (start != null && m.created_at) { last = m; answered = answered || m.role === "assistant"; }
    });
    if (!running) close();
    return out;
  }
  function renderTurnMarks() {
    var c = cur;
    if (!c) return;
    c.msgsEl.querySelectorAll(".turn-dur").forEach(function (e) { e.remove(); });
    var s = S.sessions.get(c.id) || {};
    var ends = turnEnds(c.messages, s.status === "running");
    var byId = {};
    c.messages.forEach(function (m, i) { byId[m.id] = i; });
    Object.keys(ends).forEach(function (id) {
      // tool results render into their tool call's card: mark after the newest message that has an element
      for (var i = byId[id]; i >= 0; i--) {
        var els = c.msgsEl.querySelectorAll('[data-mid="' + c.messages[i].id + '"]');
        if (els.length) {
          els[els.length - 1].after(el("div", { class: "turn-dur", title: "From the prompt to the end of the last reply", text: "turn took " + fmtDur(ends[id]) }));
          break;
        }
      }
    });
  }

  function thinkingEl(text, open, links, dur) {
    var body = el("div", { class: "thinking-body md" });
    body.innerHTML = MD.render(text || "", { links: links });
    var preview = el("span", { class: "preview", text: (text || "").replace(/\s+/g, " ").slice(0, 160) });
    var label = el("span", { class: "tlabel", text: dur != null ? "Thought for " + fmtDur(dur) : "Thinking" });
    var d = el("details", { class: "thinking" }, el("summary", null, label, preview), body);
    d._label = label;
    d.open = !!open;
    return d;
  }

  function summarizeInput(name, input) {
    if (!input || typeof input !== "object") return input == null ? "" : String(input);
    if (Array.isArray(input.paths)) return input.paths.join(", ") + (input.note ? " — " + input.note : "");
    if (name === "peers" || name === "release") return name === "release" ? "all claims" : "";
    var keys = ["command", "file_path", "path", "pattern", "url", "query", "name", "skill"];
    for (var i = 0; i < keys.length; i++) if (typeof input[keys[i]] === "string") {
      var v = input[keys[i]];
      if (keys[i] === "pattern" && typeof input.path === "string") v += "  in " + input.path;
      return v.replace(/\s+/g, " ");
    }
    for (var k in input) if (typeof input[k] === "string") return input[k].replace(/\s+/g, " ");
    return JSON.stringify(input);
  }
  function formatInput(name, input) {
    if (input && typeof input === "object" && typeof input.command === "string" && Object.keys(input).length <= 2)
      return input.command + (input.timeout ? "\n# timeout: " + input.timeout : "");
    return JSON.stringify(input, null, 2);
  }
  function resultText(b) {
    var c = b.content;
    if (typeof c === "string") return c;
    if (Array.isArray(c)) return c.map(function (x) { return x.type === "text" ? x.text : "[" + x.type + "]"; }).join("\n");
    return c == null ? "" : JSON.stringify(c, null, 2);
  }

  function toolCard(b) {
    var card = el("details", { class: "tool", "data-tool-id": b.id || "" });
    card._name = el("span", { class: "tname", text: b.name || "tool" });
    card._sum = el("span", { class: "tsum" });
    card._stat = el("span", { class: "tstat pending", title: "running" });
    card._dur = el("span", { class: "tdur" });
    card._input = el("pre", { class: "tool-input" });
    card._resLbl = el("div", { class: "lbl", text: "Result", hidden: true });
    card._result = el("pre", { class: "tool-result", hidden: true });
    card.append(el("summary", null, card._name, card._sum, card._dur, card._stat),
      el("div", { class: "tool-body" }, el("div", { class: "lbl", text: "Input" }), card._input, card._resLbl, card._result));
    setToolInput(card, b);
    if (b.id) cur.toolCards.set(b.id, card);
    if (b.id && cur.tools[b.id]) setToolTiming(card, cur.tools[b.id]);
    return card;
  }
  // t: {started_at, ended_at?} of the tool's run (tool.start/tool.end, or a stored result)
  function setToolTiming(card, t) {
    if (!t || !t.started_at) return;
    if (t.ended_at) { stopTimer(card._dur, fmtDur(t.ended_at - t.started_at)); card._dur.title = "ran for " + fmtDur(t.ended_at - t.started_at); }
    else { liveTimer(card._dur, t.started_at, "", "…"); card._dur.title = "running"; }
  }
  function setToolInput(card, b) {
    var input = b.input, raw = null;
    if (input === undefined && b.partial_json != null) {
      try { input = JSON.parse(b.partial_json); } catch (e) { raw = b.partial_json; }
    }
    if (raw !== null) {
      var m = /"(?:command|file_path|path|pattern|url|query)"\s*:\s*"((?:[^"\\]|\\.)*)/.exec(raw);
      card._sum.textContent = m ? m[1].replace(/\\n/g, " ").replace(/\\(.)/g, "$1") : "…";
      card._input.textContent = raw;
    } else {
      card._sum.textContent = summarizeInput(b.name, input);
      card._input.textContent = formatInput(b.name, input || {});
      if (card._name.textContent === "subagent") decorateSubagentCard(card, { input: input });
      else if (card._name.textContent === "webfetch") decorateWebfetchCard(card, input || {});
      else if (card._name.textContent === "send_message") decorateSendCard(card, input || {});
      else if (card._name.textContent === "spawn" || card._name.textContent === "ask" || card._name.textContent === "message" ||
          card._name.textContent === "threads" || card._name.textContent === "branches" ||
          card._name.textContent === "ask_branch" || card._name.textContent === "message_branch") decorateBranchCard(card, { input: input || {} });
    }
  }
  function decorateWebfetchCard(card, input) {
    var url = typeof input.url === "string" ? input.url : "";
    var u = null;
    try { u = new URL(url); } catch (e) { /* partial or invalid */ }
    card._sum.textContent = "";
    if (u) card._sum.append(el("span", { class: "host", text: u.host }), el("span", { class: "path", text: (u.pathname === "/" ? "" : u.pathname) + u.search }));
    else card._sum.textContent = url;
    if (input.format && input.format !== "markdown") card._sum.append(el("span", { class: "fmt", text: " · " + input.format }));
    if (u && /^https?:$/.test(u.protocol) && !card._link) {
      card._link = el("a", { class: "tool-url", href: url, target: "_blank", rel: "noopener noreferrer", text: url });
      card._input.before(card._link);
    }
  }
  function setToolResult(card, r) {
    var err = !!r.is_error;
    if (r.started_at && r.ended_at) setToolTiming(card, r);
    else if (card._dur.dataset.liveStart) stopTimer(card._dur, "");
    card._stat.className = "tstat " + (err ? "err" : "ok");
    card._stat.textContent = err ? "✕" : "✓";
    card._stat.title = err ? "error" : "done";
    card._resLbl.hidden = false;
    card._result.hidden = false;
    card._result.className = "tool-result" + (err ? " err" : "");
    card._result.textContent = resultText(r);
    if (card._name.textContent === "subagent" && !err) decorateSubagentCard(card, r);
    if (card._name.textContent === "spawn" && !err) decorateBranchCard(card, r);
    if (!err && /\n\n⚠ /.test(card._result.textContent)) {  // a peer-session conflict warning (edit/write)
      card._stat.className = "tstat warn";
      card._stat.textContent = "⚠";
      card._stat.title = "done, with a warning about another session";
    }
  }

  function msgTitle(m) {
    var u = m.usage;
    return [m.model ? "model " + m.model : "", m.provider ? "provider " + m.provider : "", m.effort ? "effort " + m.effort : "",
      m.started_at && m.created_at && m.role === "assistant" ? "response took " + fmtDur(m.created_at - m.started_at) : "",
      m.cwd ? "cwd " + m.cwd : "",
      u ? fmtTokens(inTokens(u)) + " in · " + fmtTokens(u.output_tokens) + " out" + (u.cost != null ? " · " + (u.cost_estimated ? "~" : "") + fmtCost(u.cost) + (u.cost_estimated ? " (estimated)" : "") : "") : "",
      m.created_at ? new Date(m.created_at).toLocaleString() : ""].filter(Boolean).join("\n");
  }
  function assistantMsg(m) {
    var wrap = el("div", { class: "msg assistant", "data-mid": m.id, title: msgTitle(m) || null });
    contentOf(m).forEach(function (b) {
      if (b.type === "text") { if (b.text) wrap.append(mdDiv(b.text, null, m.links)); }
      else if (b.type === "thinking") { if (b.thinking) wrap.append(thinkingEl(b.thinking, false, m.links, blockDur(b))); }
      else if (b.type === "redacted_thinking") wrap.append(el("div", { class: "thinking muted small", text: "(redacted thinking)" }));
      else if (b.type === "tool_use") wrap.append(toolCard(b));
    });
    var timing = messageTiming(m);
    var actions = el("div", { class: "msg-actions" },
      S.offline ? null : el("button", { class: "link-btn", type: "button", text: "Aside", title: "Start a side-branch from this message",
        onclick: function () { startAsideFrom(m.id); } }));
    if (timing || !S.offline) {
      wrap.append(el("div", { class: "msg-foot" },
        timing ? el("div", { class: "msg-timing", title: "How long the text streamed · the whole response, from its request to its end", text: timing }) : null,
        actions));
    }
    enhanceCode(wrap);
    return wrap;
  }
  // "text 4.2s · response 18.3s" for an assistant message with text and timing, else ""
  function messageTiming(m) {
    var texts = contentOf(m).filter(function (b) { return b.type === "text" && b.text; }).map(blockDur);
    if (!texts.length || texts.some(function (d) { return d == null; })) return "";
    var parts = ["text " + fmtDur(texts.reduce(function (a, b) { return a + b; }, 0))];
    if (m.started_at && m.created_at) parts.push("response " + fmtDur(m.created_at - m.started_at));
    return parts.join(" · ");
  }

  function addNotice(d) {
    var n = el("div", { class: "notice" + (d.level === "error" ? " error" : "") });
    var body = mdDiv(d.text || "");
    n.append(body, el("button", { class: "link-btn x", type: "button", title: "Dismiss", text: "✕", onclick: function () { n.remove(); } }));
    enhanceCode(n);
    cur.msgsEl.append(n);
    var e = $(".empty-chat", cur.msgsEl);
    if (e) e.remove();
    cur.stick = true;
    afterContent();
  }

  // draft (streaming)

  function renderDraft() {
    var c = cur;
    if (!c) return;
    c.draftEl.textContent = "";
    c.draftEls = [];
    c.dirty.clear();
    if (!c.draft) return;
    c.draftWrap = el("div", { class: "msg assistant draft" });
    c.typing = el("div", { class: "typing", "aria-label": "working" }, el("i"), el("i"), el("i"));
    c.draftWrap.append(c.typing);
    c.draftEl.append(c.draftWrap);
    c.draft.content.forEach(function (b, i) { if (b) markDirty(i); });
    afterContent();
  }

  function markDirty(i) {
    var c = cur;
    if (!c || !c.draft) return;
    if (!c.draftWrap || !c.draftWrap.isConnected) renderDraft();
    c.dirty.add(i);
    if (!c.raf) c.raf = requestAnimationFrame(flushDraft);
  }

  function flushDraft() {
    var c = cur;
    if (!c) return;
    c.raf = 0;
    if (!c.draft) { c.dirty.clear(); return; }
    c.dirty.forEach(function (i) {
      var b = c.draft.content[i];
      if (!b) return;
      var e = c.draftEls[i];
      if (!e || e._type !== b.type) {
        var ne;
        // stay collapsed while the model reasons: the summary preview shows the text as it streams
        if (b.type === "thinking") {
          ne = thinkingEl("", false);
          if (b.started_at) liveTimer(ne._label, b.started_at, "Thinking… ");
        } else if (b.type === "tool_use") { ne = toolCard(b); }
        else ne = el("div", { class: "md" });
        ne._type = b.type;
        if (e) e.replaceWith(ne);
        else {
          // keep blocks in index order, before the typing indicator
          var next = null;
          for (var k = i + 1; k < c.draftEls.length; k++) if (c.draftEls[k]) { next = c.draftEls[k]; break; }
          c.draftWrap.insertBefore(ne, next || c.typing);
        }
        c.draftEls[i] = e = ne;
      }
      // local file links become clickable once the message is stored (the server checks they exist)
      if (b.type === "text") setDraftHtml(e, MD.render(b.text || "", { links: {}, streaming: true }));
      else if (b.type === "thinking") {
        $(".thinking-body", e).innerHTML = MD.render(b.thinking || "", { links: {}, streaming: true });
        $(".preview", e).textContent = (b.thinking || "").replace(/\s+/g, " ").slice(0, 160);
      } else if (b.type === "tool_use") setToolInput(e, b);
    });
    // a thinking block that stopped growing (a later block started): its final duration
    c.draftEls.forEach(function (e, i) {
      var b = c.draft.content[i];
      if (e && e._label && e._label.dataset.liveStart && i < c.draft.content.length - 1)
        stopTimer(e._label, blockDur(b) != null ? "Thought for " + fmtDur(blockDur(b)) : "Thinking");
    });
    c.dirty.clear();
    afterContent();
  }

  function renderQueue() {
    var c = cur;
    if (!c) return;
    c.queueEl.textContent = "";
    (c.queue || []).forEach(function (t) {
      var sr = parseSubResult(t);
      if (sr) { c.queueEl.append(el("div", { class: "msg user queued" }, el("div", { class: "bubble", text: "Subagent result: " + (sr.description || sr.session_id) }), el("div", { class: "qlabel", text: "queued" }))); return; }
      var br = parseBranchResult(t);
      if (br) { c.queueEl.append(el("div", { class: "msg user queued" }, el("div", { class: "bubble", text: "Branch result: " + (br.title || br.branch_id) }), el("div", { class: "qlabel", text: "queued" }))); return; }
      c.queueEl.append(el("div", { class: "msg user queued" }, el("div", { class: "bubble", text: t }), el("div", { class: "qlabel", text: "queued" })));
    });
    afterContent();
  }

  function afterContent() {
    if (!cur) return;
    if (cur.stick) scrollBottom();
    else cur.jump.hidden = false;
  }
  function scrollBottom() {
    if (!cur) return;
    cur.scroll.scrollTop = cur.scroll.scrollHeight;
    cur.jump.hidden = true;
  }

  // btw side box

  function onBtw(name, d) {
    var box = $("#btw-box");
    var item = box.querySelector('[data-btw="' + CSS.escape(d.btw_id || "") + '"]');
    if (!$(".btw-head", box)) {
      box.append(el("div", { class: "btw-head" }, el("span", { text: "By the way" }),
        el("span", { class: "muted small", style: "font-weight:400", text: "not added to the chat" }), el("span", { class: "spacer" }),
        el("button", { class: "icon-btn", type: "button", title: "Dismiss", text: "✕", onclick: function () { box.hidden = true; box.textContent = ""; } })));
    }
    if (!item) {
      item = el("div", { class: "btw-item", "data-btw": d.btw_id || "" },
        el("div", { class: "q", text: d.question ? "/btw " + d.question : "/btw" }), el("div", { class: "a md" }));
      item._text = "";
      box.append(item);
    }
    box.hidden = false;
    if (d.question && name === "btw.start") $(".q", item).textContent = "/btw " + d.question;
    if (d.reset) item._text = "";
    if (d.text) item._text += d.text;
    if (d.error) { item.classList.add("error"); item._text += (item._text ? "\n\n" : "") + "Error: " + d.error; }
    var a = $(".a", item);
    a.innerHTML = item._text ? MD.render(item._text) : "";
    if (name !== "btw.end" && !d.error && !item._text) a.append(el("div", { class: "typing" }, el("i"), el("i"), el("i")));
    box.scrollTop = box.scrollHeight;
  }

  // composer

  function composerKeydown(e) {
    var c = cur || newView;
    if (!c) return;
    var ta = c.ta;
    if (!c.pop.hidden) {
      var items = c.pop.querySelectorAll(".item");
      if (e.key === "ArrowDown" || e.key === "ArrowUp") {
        e.preventDefault();
        c.popSel = (c.popSel + (e.key === "ArrowDown" ? 1 : items.length - 1)) % items.length;
        items.forEach(function (it, i) { it.classList.toggle("sel", i === c.popSel); });
        if (items[c.popSel]) items[c.popSel].scrollIntoView({ block: "nearest" });
        return;
      }
      if (e.key === "Tab" || (e.key === "Enter" && !e.shiftKey && items.length && ta.value.indexOf(" ") < 0 &&
          ("/" + items[c.popSel]._name) !== ta.value.trim())) {
        e.preventDefault();
        acceptCommand(items[c.popSel]._name);
        return;
      }
      if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); c.pop.hidden = true; return; }
    }
    if (e.key === "Enter" && !e.shiftKey && !e.isComposing) { e.preventDefault(); sendComposer(); return; }
    if (e.key === "ArrowUp" && ta.value === "" && cur) { if (editLast()) e.preventDefault(); return; }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); stopRun(); }
  }

  function updatePopup() {
    var c = cur || newView;
    if (!c) return;
    var v = c.ta.value, m = /^\/(\S*)$/.exec(v);
    if (!m || !S.commands.length) { c.pop.hidden = true; return; }
    var q = m[1].toLowerCase();
    var list = S.commands.filter(function (cmd) { return cmd.name.toLowerCase().indexOf(q) === 0; });
    if (!list.length) { c.pop.hidden = true; return; }
    c.pop.textContent = "";
    c.popSel = 0;
    list.forEach(function (cmd, i) {
      var it = el("div", { class: "item" + (i === 0 ? " sel" : ""), role: "option" },
        el("span", { class: "n", text: "/" + cmd.name }), el("span", { class: "d", text: cmd.description || "" }));
      it._name = cmd.name;
      it.addEventListener("mousedown", function (e) { e.preventDefault(); acceptCommand(cmd.name); });
      c.pop.append(it);
    });
    c.pop.hidden = false;
  }
  function acceptCommand(name) {
    var c = cur || newView;
    c.ta.value = "/" + name + " ";
    c.pop.hidden = true;
    c.ta.focus();
    autoGrow(c.ta);
  }

  function sendComposer() {
    var c = cur || newView;
    if (!c) return;
    var text = c.ta.value;
    if (S.connected && !providerReady() && !/^\s*\/(?!btw|retitle|compact)\w/.test(text)) {
      toast("No LLM provider is configured yet");
      location.hash = "#setup";
      return;
    }
    if (!text.trim()) return;
    if (!S.connected) { toast("Not connected to the server", "error"); return; }
    c.pop.hidden = true;
    if (!cur && newView) { createAndSend(text); return; }
    c.ta.value = "";
    autoGrow(c.ta);
    S.composerDrafts.delete(c.id);
    c.stick = true;
    rpc("chat.send", { session_id: c.id, text: text, branch_id: c.branchId || undefined }).catch(function (e) {
      toast("Send failed: " + e.message, "error");
      if (cur === c && !c.ta.value) { c.ta.value = text; autoGrow(c.ta); }
    });
  }

  function stopRun() {
    if (!cur) return;
    if (curThreadRunning()) rpcToast("chat.stop", { session_id: cur.id, branch_id: cur.branchId || undefined }).catch(function () {});
  }

  // ---- new session view ------------------------------------------------------------------------

  var newView = null;
  function renderNew() {
    var c = buildChat({ placeholder: "What should we work on?" });
    c.head.append(svgEl(defaultIcon(32)), el("div", { class: "info" },
      el("div", { class: "chat-title", text: "New session" }),
      el("div", { class: "chat-meta" }, el("span", { text: "Your first message starts the session; its title is generated from it." }))));
    c.cwd = el("input", { type: "text", list: "cwd-list", placeholder: "loading…", spellcheck: "false", "aria-label": "Working directory" });
    c.cwdList = el("datalist", { id: "cwd-list" });
    c.recent = el("div", { class: "recent-cwds" });
    c.choice = { provider: null, model: null }; // model for the new session (null = the global default)
    c.modelBtn = el("button", { class: "badge provider model-btn", type: "button", title: "Model for the new session", onclick: function () {
      pickModel({ title: "Model for the new session", provider: c.choice.provider, model: c.choice.model, current: globalChoice() })
        .then(function (r) { if (r && newView === c) { c.choice = r; renderNewModel(c); } });
    } });
    renderNewModel(c);
    c.inner.prepend(el("div", { class: "empty-chat" }, svgEl(defaultIcon(64)), el("p", { text: "Start a new session" })),
      el("div", { class: "new-session" },
        el("label", { for: "new-cwd", text: "Working directory" }),
        el("div", { class: "cwd-row" }, c.cwd, el("button", { class: "btn", type: "button", text: "Browse…", onclick: function () {
          pickFolder({ title: "Working directory for the new session", initial: c.cwd.value.trim() || null })
            .then(function (d) { if (d && newView === c) { c.cwd.value = d; markRecent(c); } });
        } }), c.cwdList),
        c.recent,
        el("div", { class: "new-model" }, el("span", { class: "muted small", text: "Model" }), c.modelBtn)));
    c.cwd.id = "new-cwd";
    c.cwd.addEventListener("input", function () { markRecent(c); });
    c.loading = false;
    newView = c;
    rpc("cwd.info").then(function (info) {
      if (newView !== c) return;
      c.cwdInfo = info;
      if (!c.cwd.value) c.cwd.value = info.default || "";
      c.cwd.placeholder = info.server_cwd || "";
      renderRecent(c);
    }, function () { c.cwd.placeholder = "the directory the server runs in"; });
    setView(c.root);
    setPageMeta("New session · " + basename(SELF_PATH));
    updateComposer();
    c.ta.focus();
  }
  function globalChoice() {
    var p = S.provider && typeof S.provider === "object" ? S.provider : {};
    return { provider: p.id || null, model: p.type === "mock" ? "mock" : (S.settings.model || p.model || null) };
  }
  function renderNewModel(c) {
    var g = globalChoice();
    var prov = c.choice.provider || g.provider || "?";
    var model = c.choice.model || (c.choice.provider && c.choice.provider !== g.provider ? "provider default" : g.model) || "";
    c.modelBtn.textContent = "";
    append(c.modelBtn, [prov + (model && model !== "mock" ? " · " + model : ""), el("span", { class: "caret", text: "▾" })]);
    c.modelBtn.classList.toggle("pinned", !!(c.choice.provider || c.choice.model));
  }
  // recently used working directories, most recent first
  function renderRecent(c) {
    var info = c.cwdInfo || {};
    var list = (info.recent || []).filter(function (r) { return r.exists; });
    c.cwdList.textContent = "";
    list.forEach(function (r) { c.cwdList.append(el("option", { value: r.path })); });
    c.recent.textContent = "";
    if (!list.length) return;
    c.recent.append(el("span", { class: "muted small", text: "Recent:" }));
    list.slice(0, 8).forEach(function (r) {
      c.recent.append(el("button", { class: "chip", type: "button", "data-path": r.path, title: r.path + "\nlast used " + ago(r.used_at),
        text: tildify(r.path), onclick: function () { c.cwd.value = r.path; markRecent(c); c.ta.focus(); } }));
    });
    markRecent(c);
  }
  function markRecent(c) {
    var v = c.cwd.value.trim();
    c.recent.querySelectorAll(".chip").forEach(function (b) { b.classList.toggle("active", b.getAttribute("data-path") === v); });
  }
  function defaultIcon(size) {
    return '<svg xmlns="http://www.w3.org/2000/svg" width="' + size + '" height="' + size + '" viewBox="0 0 16 16"><rect width="16" height="16" rx="3" fill="var(--bg-sunk)"/>' +
      '<path d="M8 4.5v7M4.5 8h7" stroke="var(--muted)" stroke-width="1.4" stroke-linecap="round"/></svg>';
  }
  function createAndSend(text) {
    var c = newView, cwd = c.cwd.value.trim();
    if (c.creating) return;
    c.creating = true;
    c.sendBtn.disabled = true;
    c.ta.value = "";
    autoGrow(c.ta);
    rpcToast("sessions.create", cwd ? { cwd: cwd } : {}).then(function (s) {
      S.sessions.set(s.id, s);
      var ch = c.choice || {};
      var pick = ch.provider || ch.model ? rpc("sessions.set_model", { session_id: s.id, provider: ch.provider, model: ch.model })
        .then(function (sum) { S.sessions.set(sum.id, sum); }, function (e) { toast("Model not set: " + e.message, "error"); }) : Promise.resolve();
      return pick.then(function () { return rpc("chat.send", { session_id: s.id, text: text }); }).then(function () {
        newView = null;
        location.hash = "#session/" + encodeURIComponent(s.id);
      });
    }).catch(function (e) {
      c.creating = false;
      c.sendBtn.disabled = false;
      if (!c.ta.value) { c.ta.value = text; autoGrow(c.ta); }
      toast("Could not start session: " + e.message, "error");
    });
  }

  // ---- sessions grid ---------------------------------------------------------------------------

  var gridFilter = "";
  function renderSessions() {
    newView = null;
    var filter = el("input", { type: "text", placeholder: "Filter…", value: gridFilter, "aria-label": "Filter sessions" });
    filter.addEventListener("input", function () { gridFilter = filter.value; renderSessionsGrid(); });
    var page = el("div", { class: "page" }, el("div", { class: "page-inner" },
      el("div", { class: "page-head" }, el("h1", { text: "All sessions" }), filter, S.offline ? null : el("a", { class: "btn primary", href: "#new", text: "New session" })),
      el("div", { id: "sessions-grid" })));
    setView(page);
    setPageMeta("Sessions · " + basename(SELF_PATH));
    renderSessionsGrid();
  }
  function renderSessionsGrid() {
    var host = $("#sessions-grid");
    if (!host) return;
    host.textContent = "";
    var q = gridFilter.trim().toLowerCase();
    var list = sortedSessions().filter(function (s) {
      return !q || [s.title, s.cwd, (s.subjects || []).join(" "), s.id].join(" ").toLowerCase().indexOf(q) >= 0;
    });
    if (!list.length) { host.append(el("div", { class: "empty", text: S.sessions.size ? "No matching sessions." : "No sessions yet. Start one!" })); return; }
    var grid = el("div", { class: "grid" });
    function card(s, n) {
      var del = S.offline ? null : el("button", { class: "icon-btn del", type: "button", title: "Delete session", "aria-label": "Delete session", text: "🗑",
        onclick: function (e) {
          e.preventDefault(); e.stopPropagation();
          if (confirm('Delete "' + titleOf(s) + '"? This cannot be undone.')) rpcToast("sessions.delete", { session_id: s.id }).catch(function () {});
        } });
      var kids = n ? n.kids.length : 0;
      return el("a", { class: "card" + (s.parent_id && n && n.depth ? " child" : ""), href: "#session/" + encodeURIComponent(s.id), "data-sid": s.id },
        svgEl(iconSvg(s, n && n.depth ? 32 : 48)),
        el("div", { class: "body" },
          el("div", { class: "ct", text: titleOf(s) }),
          el("div", { class: "cwd", title: s.cwd || "", text: "\u200e" + tildify(s.cwd || "") }),
          s.subjects && s.subjects.length ? el("div", { class: "subj", text: s.subjects.join(" › ") }) : null,
          el("div", { class: "meta" },
            el("span", { title: s.updated_at ? new Date(s.updated_at).toLocaleString() : "", text: ago(s.updated_at) }),
            el("span", { text: "· " + (s.message_count || 0) + " message" + (s.message_count === 1 ? "" : "s") }),
            kids ? el("span", { class: "kids-count", text: kids + " subagent" + (kids === 1 ? "" : "s") }) : null,
            s.branch_count > 1 ? el("span", { class: "branch-count", text: "⑂" + s.branch_count + " branches" }) : null,
            s.kind === "subagent" ? el("span", { class: "badge", text: "subagent" }) : null,
            s.status === "running" ? el("span", { class: "badge running", text: "running" }) : null)),
        n && n.kids.length ? treeToggle(n) : null,
        del);
    }
    if (q) list.forEach(function (s) { grid.append(card(s, null)); });
    else sessionTree(list).forEach(function (n) {
      var group = el("div", { class: "card-group" }, card(n.s, n));
      if (n.kids.length && !treeCollapsed(n.s.id)) {
        var sub = el("div", { class: "card-kids" });
        (function walk(ns, depth) {
          ns.forEach(function (k) {
            k.depth = depth;
            sub.append(el("div", { class: "card-kid", style: "--depth:" + (depth - 1) }, card(k.s, k)));
            if (!treeCollapsed(k.s.id)) walk(k.kids, depth + 1);
          });
        })(n.kids, 1);
        group.append(sub);
      }
      grid.append(group);
    });
    host.append(grid);
  }

  // ---- settings --------------------------------------------------------------------------------

  var KNOWN = [
    { section: "Model", key: "provider", label: "Provider", type: "select", options: providerOptions,
      note: "auto = the env provider ($AI_ENDPOINT), else the first configured provider, else Anthropic when ANTHROPIC_API_KEY is set, else the built-in mock." },
    { section: "Model", key: "model", label: "Model", type: "text", nullable: true, placeholder: "provider default", models: true,
      note: "Default model of the selected provider; leave blank for the provider's own default. Sessions can pick another provider/model (click the model badge in a chat, or /model)." },
    { section: "Model", key: "cheap_model", label: "Cheap model", type: "text", nullable: true, placeholder: "provider default",
      note: "Used for titles, subjects and /btw. Blank = the provider's cheap model." },
    { section: "Model", key: "max_tokens", label: "Max tokens", type: "number" },
    { section: "Model", key: "thinking_budget", label: "Thinking budget", type: "number", note: "0 disables extended thinking. Used when no effort level is sent." },
    { section: "Model", key: "effort", label: "Effort", type: "select", nullable: true, options: ["", "minimal", "low", "medium", "high", "xhigh", "max"],
      note: "Default effort level (how much the model thinks/reasons) for sessions without their own; sessions pick theirs in the chat header, with /effort or Tab in the TUI. A level the model doesn't take is lowered to one it does." },
    { section: "Network", key: "request_timeout", label: "Request timeout (s)", type: "number", step: "any",
      note: "How long to wait for a provider to start answering before the request is retried." },
    { section: "Network", key: "stream_idle_timeout", label: "Stream idle timeout (s)", type: "number", step: "any",
      note: "How long an answer that has started may stay silent before it counts as stalled and is retried. Long because some proxies send nothing while the model thinks." },
    { section: "Network", key: "max_retries", label: "Max retries", type: "number",
      note: "For timeouts, dropped connections, 429 and 5xx errors. 0 disables retrying." },
    { section: "Network", key: "retry_base_delay", label: "Retry delay (s)", type: "number", step: "any",
      note: "Retry n waits about delay × 2^(n−1) seconds, randomly scaled by 0.5–1.5 (jitter)." },
    { section: "Behaviour", key: "default_cwd", label: "Fallback working directory", type: "text", nullable: true, placeholder: "the directory the server runs in", browse: true,
      note: "New sessions default to the directory the last new session was started in; this is used before any session was started (blank = the directory the server runs in)." },
    { section: "Behaviour", key: "builtin_skill_enabled", label: "Built-in self-edit skill", type: "bool" },
    { section: "Behaviour", key: "editor", label: "Editor", type: "text", note: "Used when clicking file links (runs in the in-page terminal). Empty: $EDITOR, then nano, pico, vi." },
    { section: "Behaviour", key: "identicon_subjects", label: "Identicon subjects", type: "number" },
    { section: "Behaviour", key: "tui_mouse", label: "TUI mouse wheel scrolling", type: "bool",
      note: "Off: plain drag selects text in the terminal (Shift+drag works either way). /mouse toggles it for one TUI." },
    { section: "Behaviour", key: "ui.initial_view", label: "Initial view", type: "text", nullable: true,
      note: 'Where this file opens when there is no #hash: "sessions", "session:<id>", or empty.' }
  ];
  var HIDDEN_KEYS = { system_prompt: 1, credentials_storage: 1, providers: 1, model_overrides: 1 };
  function dirname(p) { return String(p).replace(/\/[^\/]*$/, "") || "/"; }
  function providerOptions() {
    var ids = ["auto"];
    (S.providerList || []).forEach(function (p) { if (ids.indexOf(p.id) < 0) ids.push(p.id); });
    (S.settings.providers || []).forEach(function (p) { if (p && p.id && ids.indexOf(p.id) < 0) ids.push(p.id); });
    if (ids.indexOf("anthropic") < 0) ids.push("anthropic");
    var v = S.settings.provider;
    if (v && ids.indexOf(v) < 0) ids.push(v);
    return ids;
  }

  function renderSettings() {
    newView = null;
    if (S.offline) {
      setView(el("div", { class: "page" }, el("div", { class: "page-inner" }, el("div", { class: "page-head" }, el("h1", { text: "Settings" })),
        el("div", { class: "empty" }, el("p", { text: "Settings are available while the server runs. Start it with" }), el("code", { text: RUN_CMD })))));
      setPageMeta("Settings · " + basename(SELF_PATH));
      return;
    }
    var root = el("div", { class: "page-inner settings" }, el("div", { class: "page-head" }, el("h1", { text: "Settings" })));
    var sections = {};
    function section(name) {
      if (!sections[name]) { sections[name] = el("section", null, el("h2", { text: name })); root.append(sections[name]); }
      return sections[name];
    }
    section("Model");
    var provSec = section("Providers");
    provSec.append(el("p", { class: "note", text: "OpenAI-compatible (Chat Completions) or Anthropic (Messages) endpoints. API keys are stored as credentials, never in settings." }),
      el("div", { id: "providers-host", text: "Loading…" }));
    section("Model overrides").append(
      el("p", { class: "note", text: "Set your own context window for a model: smaller than the provider says (e.g. 200k for a 1M model that costs more past 200k), or one the provider does not report. The context gauge of a chat measures against it (amber at 80%, red at 95%). Values like 200000, 200k or 1M; empty removes the override." }),
      el("div", { id: "overrides-host" }));
    KNOWN.forEach(function (f) { section(f.section).append(fieldRow(f)); });
    loadProviders();

    var tokSec = section("Access token");
    tokSec.append(el("p", { class: "note", text: "Every client (this page, the TUI, the CLI) needs the token stored in this html file to talk to its server." }),
      S.onboarding && S.onboarding.moved ? el("div", { class: "notice-bar warn", id: "settings-moved" },
        el("span", { text: "This file now runs on a different computer, user or path than its token was made for. Regenerating is recommended." }),
        el("button", { class: "btn sm", type: "button", text: "Click here to regenerate", onclick: regenerateToken })) : null,
      el("div", { id: "token-host" }));

    // system prompt
    var sp = el("textarea", { class: "field sysprompt", "data-key": "system_prompt", spellcheck: "false" });
    sp.value = S.settings.system_prompt || "";
    var spSaved = el("span", { class: "saved", text: "Saved" });
    var spSave = el("button", { class: "btn primary sm", type: "button", text: "Save system prompt", onclick: function () { saveSetting("system_prompt", sp.value, spSaved); } });
    sp.addEventListener("keydown", function (e) { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) { e.preventDefault(); spSave.click(); } });
    section("System prompt").append(
      el("p", { class: "note", text: "The base prompt. At run time the server appends environment info (cwd, date, platform), AGENTS.md files from the session's directory upward, and the skill index. Type /system in a chat to see the effective prompt for that session." }),
      el("div", { class: "field wide" }, sp, el("div", { class: "ctl" }, spSave, el("span", { class: "muted small", text: "Ctrl+Enter" }), spSaved)));

    // other unknown keys
    var known = {};
    KNOWN.forEach(function (f) { known[f.key] = 1; });
    Object.keys(S.settings).sort().forEach(function (k) {
      if (known[k] || HIDDEN_KEYS[k]) return;
      var v = S.settings[k];
      var type = typeof v === "boolean" ? "bool" : typeof v === "number" ? "number" : typeof v === "string" || v === null ? "text" : "json";
      section("Other").append(fieldRow({ key: k, label: k, type: type, nullable: v === null }));
    });

    // credentials
    var credsSec = section("Credentials");
    credsSec.append(el("p", { class: "note", text: "Values are write-only here: the server never sends them to the browser." }), el("div", { id: "creds-host", text: "Loading…" }));
    rpc("creds.get").then(renderCreds, function (e) { $("#creds-host").textContent = "Could not load credentials: " + e.message; });

    // export
    var viewSel = el("select", { "aria-label": "Initial view of the export" });
    var spec = currentViewSpec();
    if (spec.indexOf("session:") === 0) {
      var s = S.sessions.get(spec.slice(8));
      viewSel.append(el("option", { value: spec, text: "Session: " + titleOf(s) }));
    }
    viewSel.append(el("option", { value: "sessions", text: "All sessions" }), el("option", { value: "settings", text: "Settings" }));
    section("Export").append(
      el("p", { class: "note", text: "Downloads a copy of this file with all current sessions and settings. Credentials are included only when they are embedded in this file. The copy gets no access token (it makes its own on first run) and opens to the view chosen here." }),
      el("div", { class: "field" }, el("label", { text: "Opens to" }), el("div", { class: "ctl" }, viewSel,
        el("button", { class: "btn primary", type: "button", text: "Export", onclick: function () { doExport(viewSel.value); } }))));

    // about
    var h = S.hello || {};
    section("About").append(el("div", { class: "small muted" },
      el("div", null, "File: ", el("code", { text: SELF_PATH })),
      el("div", { text: "Server: 127.0.0.1:" + S.port + " · version " + (h.version || "?") + " · provider " + providerLabel() +
        (h.has_api_key ? " · API key configured" : " · no API key") })));

    setView(el("div", { class: "page" }, root));
    renderOverrides();
    renderTokenBox(null);
    setPageMeta("Settings · " + basename(SELF_PATH));
  }

  function providerLabel() {
    var p = S.provider;
    if (p && typeof p === "object") return (p.id || "?") + " (" + (p.type || "?") + (p.model ? ", " + p.model : "") + ")";
    return p || "?";
  }

  function fieldRow(f) {
    var v = S.settings[f.key], input;
    var saved = el("span", { class: "saved", text: "Saved" });
    if (f.type === "select") {
      var opts = typeof f.options === "function" ? f.options() : f.options;
      input = el("select", null, opts.map(function (o) { return el("option", { value: o, text: o === "" ? "(none)" : o }); }));
      input._options = f.options;
      input.value = v == null ? (f.nullable ? "" : opts[0]) : v;
    } else if (f.type === "bool") {
      input = el("input", { type: "checkbox" });
      input.checked = !!v;
    } else if (f.type === "number") {
      input = el("input", { type: "number", value: v == null ? "" : v, step: f.step || null });
    } else if (f.type === "json") {
      input = el("textarea", { class: "field", rows: 3 });
      input.value = JSON.stringify(v, null, 2);
    } else {
      input = el("input", { type: "text", value: v == null ? "" : v, placeholder: f.placeholder || (f.nullable ? "(unset)" : "") });
    }
    input.setAttribute("data-key", f.key);
    input.id = "set-" + f.key.replace(/\W/g, "_");
    var extra = null;
    if (f.browse) {
      extra = el("button", { class: "btn sm", type: "button", text: "Browse…", onclick: function () {
        pickFolder({ title: f.label, initial: input.value.trim() || null }).then(function (d) {
          if (d) { input.value = d; saveSetting(f.key, d, saved); }
        });
      } });
    }
    if (f.models) {
      // suggestions from the selected provider's model list
      var dl = el("datalist", { id: input.id + "-list" });
      input.setAttribute("list", dl.id);
      extra = dl;
      rpc("models.list", {}).then(function (r) {
        (r.models || []).forEach(function (x) { dl.append(el("option", { value: x.id, label: x.name || x.id })); });
      }, function () {});
    }
    input.addEventListener("change", function () {
      var val;
      if (f.type === "bool") val = input.checked;
      else if (f.type === "number") { if (input.value === "") return; val = Number(input.value); }
      else if (f.type === "json") { try { val = JSON.parse(input.value); } catch (e) { toast("Invalid JSON", "error"); return; } }
      else val = f.nullable && input.value.trim() === "" ? null : input.value;
      saveSetting(f.key, val, saved);
    });
    return el("div", { class: "field" },
      el("label", { for: input.id }, f.label, f.label !== f.key ? el("code", { text: f.key }) : null),
      el("div", { class: "ctl" }, input, extra, saved),
      f.note ? el("div", { class: "note", style: "grid-column: 2", text: f.note }) : null);
  }

  function saveSetting(key, value, savedEl) {
    rpcToast("settings.set", { key: key, value: value }).then(function (s) {
      S.settings = s;
      if (savedEl) { savedEl.classList.add("show"); setTimeout(function () { savedEl.classList.remove("show"); }, 1500); }
    }).catch(function () {});
  }

  function refreshSettingsValues() {
    document.querySelectorAll(".settings [data-key]").forEach(function (input) {
      if (input === document.activeElement) return;
      var v = S.settings[input.getAttribute("data-key")];
      if (input.tagName === "SELECT" && typeof input._options === "function") {
        input.textContent = "";
        input._options().forEach(function (o) { input.append(el("option", { value: o, text: o })); });
        input.value = v == null ? "auto" : v;
        return;
      }
      if (input.type === "checkbox") input.checked = !!v;
      else if (input.tagName === "TEXTAREA" && !input.classList.contains("sysprompt")) input.value = JSON.stringify(v, null, 2);
      else input.value = v == null ? "" : v;
    });
  }

  function renderCreds(info) {
    var host = $("#creds-host");
    if (!host) return;
    host.textContent = "";
    var mode = info.storage || "xdg";
    var radios = el("div", { class: "radio-row" }, [["xdg", "Separate file (recommended)"], ["embedded", "Embedded in this html file"]].map(function (o) {
      var r = el("input", { type: "radio", name: "creds-storage", value: o[0] });
      r.checked = mode === o[0];
      r.addEventListener("change", function () {
        if (o[0] === "embedded" && !confirm("Store credentials inside this html file? Anyone you send the file to gets your keys (exports still exclude them).")) { r.checked = false; renderCreds(info); return; }
        rpcToast("creds.storage", { mode: o[0] }).then(renderCreds, function () { renderCreds(info); });
      });
      return el("label", null, r, o[1]);
    }));
    var list = el("ul", { class: "creds-list" });
    (info.names || []).forEach(function (n) {
      list.append(el("li", null, el("span", { text: n }), el("span", { class: "v", text: "••••••••" }),
        el("button", { class: "btn sm danger", type: "button", text: "Delete", onclick: function () {
          if (confirm("Delete credential " + n + "?")) rpcToast("creds.set", { name: n, value: null }).then(renderCreds, function () {});
        } })));
    });
    if (!(info.names || []).length) list.append(el("li", { class: "muted" }, el("span", { text: "No credentials stored." })));
    var name = el("input", { type: "text", placeholder: "NAME (e.g. ANTHROPIC_API_KEY)", list: "cred-names", autocomplete: "off", spellcheck: "false" });
    var val = el("input", { type: "password", placeholder: "value", autocomplete: "new-password" });
    function add() {
      var n = name.value.trim();
      if (!/^[A-Za-z_][A-Za-z0-9_]*$/.test(n)) { toast("Name must look like an environment variable", "error"); return; }
      if (!val.value) { toast("Enter a value", "error"); return; }
      rpcToast("creds.set", { name: n, value: val.value }).then(function (i) { renderCreds(i); toast("Saved " + n); }, function () {});
    }
    val.addEventListener("keydown", function (e) { if (e.key === "Enter") { e.preventDefault(); add(); } });
    host.append(
      el("div", { class: "field" }, el("label", { text: "Storage" }), radios),
      info.path ? el("div", { class: "field" }, el("label", { text: "Location" }), el("code", { text: info.path })) : null,
      el("div", { class: "field wide" }, list),
      el("div", { class: "creds-add" }, name, val, el("button", { class: "btn primary", type: "button", text: "Set", onclick: add }),
        el("datalist", { id: "cred-names" }, el("option", { value: "ANTHROPIC_API_KEY" }))),
      el("p", { class: "note", text: "Environment variables with the same name override stored values." }));
  }

  // providers

  function credName(id) { return "PROVIDER_" + String(id).toUpperCase().replace(/[^A-Z0-9]+/g, "_") + "_KEY"; }

  function loadProviders() {
    rpc("providers.list").then(function (list) {
      S.providerList = list || [];
      renderProviders();
      refreshSettingsValues();
    }, function (e) { var h = $("#providers-host"); if (h) h.textContent = "Could not load providers: " + e.message; });
  }

  function renderProviders() {
    var host = $("#providers-host");
    if (!host) return;
    var active = S.provider && typeof S.provider === "object" ? S.provider.id : null;
    var list = el("ul", { class: "prov-list" });
    (S.providerList || []).forEach(function (p) {
      list.append(el("li", { "data-provider": p.id },
        el("span", { class: "pid", text: p.id }),
        el("span", { class: "badge", text: p.type }),
        p.builtin ? el("span", { class: "badge", title: "From $AI_ENDPOINT / $AI_MODEL; not stored", text: "env · read only" }) : null,
        p.id === active ? el("span", { class: "badge active", text: "active" }) : null,
        el("span", { class: "pmeta", title: p.base_url || "", text: [p.model, p.cheap_model && p.cheap_model !== p.model ? "cheap: " + p.cheap_model : "", p.base_url].filter(Boolean).join(" · ") }),
        p.type !== "mock" && !p.has_key ? el("span", { class: "badge warn", text: "no key" }) : null,
        el("span", { class: "spacer" }),
        p.builtin ? null : el("button", { class: "btn sm", type: "button", text: "Edit", onclick: function () { providerForm(p); } }),
        p.builtin ? null : el("button", { class: "btn sm danger", type: "button", text: "Delete", onclick: function () { deleteProvider(p); } })));
    });
    if (!(S.providerList || []).length) list.append(el("li", { class: "muted" }, el("span", { text: "No providers configured. " }), el("a", { href: "#setup", text: "Set one up" })));
    // re-renders (after any settings.updated) swap only the list: detaching an open add/edit form
    // would blur the field the user is typing in and drop the rest of their keystrokes
    var old = $(".prov-list", host);
    if (old && $("#prov-form-host", host)) { old.replaceWith(list); return; }
    host.textContent = "";
    host.append(list, el("div", { id: "prov-form-host" }),
      el("button", { class: "btn", type: "button", id: "prov-add", text: "Add provider", onclick: function () { providerForm(null); } }));
  }

  function configuredProviders() { return (S.settings.providers || []).filter(function (p) { return p && p.id; }); }

  function providerForm(p) {
    var host = $("#prov-form-host");
    if (!host) return;
    var stored = p ? configuredProviders().find(function (x) { return x.id === p.id; }) || p : null;
    var id = el("input", { type: "text", name: "id", placeholder: "e.g. local", value: stored ? stored.id : "", spellcheck: "false" });
    if (stored) id.disabled = true;
    var type = el("select", { name: "type" }, ["openai", "anthropic"].concat((S.providerList || []).some(function (x) { return x.type === "mock"; }) ? ["mock"] : [])
      .map(function (t) { return el("option", { value: t, text: t }); }));
    type.value = stored ? stored.type || "openai" : "openai";
    var base = el("input", { type: "text", name: "base_url", placeholder: "http://127.0.0.1:3456/v1", value: stored && stored.base_url || "", spellcheck: "false" });
    var model = el("input", { type: "text", name: "model", value: stored && stored.model || "", spellcheck: "false" });
    var cheap = el("input", { type: "text", name: "cheap_model", placeholder: "same as model", value: stored && stored.cheap_model || "", spellcheck: "false" });
    var key = el("input", { type: "password", name: "api_key", autocomplete: "new-password",
      placeholder: stored ? (p.has_key ? "unchanged (stored)" : "not set") : "optional" });
    function row(label, input) { return el("div", { class: "field" }, el("label", { text: label }), el("div", { class: "ctl" }, input)); }
    function save() {
      var pid = id.value.trim();
      if (!/^[A-Za-z0-9_.-]+$/.test(pid)) { toast("Provider id: letters, digits, _ . - only", "error"); return; }
      if (pid === "auto" || pid === "env") { toast('"' + pid + '" is reserved', "error"); return; }
      var others = configuredProviders().filter(function (x) { return x.id !== pid; });
      if (!stored && others.length !== configuredProviders().length) { toast("A provider with that id exists", "error"); return; }
      var entry = { id: pid, type: type.value, base_url: base.value.trim() || null, model: model.value.trim() || null,
        cheap_model: cheap.value.trim() || null, api_key_credential: (stored && stored.api_key_credential) || credName(pid) };
      var next = configuredProviders().map(function (x) { return x.id === pid ? entry : x; });
      if (!stored) next.push(entry);
      var chain = key.value ? rpcToast("creds.set", { name: entry.api_key_credential, value: key.value }) : Promise.resolve();
      chain.then(function () { return rpcToast("settings.set", { key: "providers", value: next }); }).then(function (s) {
        S.settings = s;
        host.textContent = "";
        toast("Saved provider " + pid);
        loadProviders();
      }).catch(function () {});
    }
    host.textContent = "";
    host.append(el("div", { class: "prov-form" },
      el("h3", { text: stored ? "Edit provider " + stored.id : "New provider" }),
      row("Id", id), row("Type", type), row("Base URL", base), row("Model", model), row("Cheap model", cheap),
      row("API key", key),
      el("p", { class: "note", text: "The key is stored as credential " + ((stored && stored.api_key_credential) || "PROVIDER_<ID>_KEY") + "." }),
      el("div", { class: "ctl" },
        el("button", { class: "btn primary", type: "button", text: "Save provider", onclick: save }),
        el("button", { class: "btn", type: "button", text: "Cancel", onclick: function () { host.textContent = ""; } }))));
    (stored ? base : id).focus();
  }

  function deleteProvider(p) {
    if (!confirm("Delete provider " + p.id + " and its stored API key?")) return;
    var stored = configuredProviders().find(function (x) { return x.id === p.id; }) || {};
    var next = configuredProviders().filter(function (x) { return x.id !== p.id; });
    rpcToast("settings.set", { key: "providers", value: next }).then(function (s) {
      S.settings = s;
      var jobs = [];
      if (stored.api_key_credential) jobs.push(rpc("creds.set", { name: stored.api_key_credential, value: null }).catch(function () {}));
      if (s.provider === p.id) jobs.push(rpc("settings.set", { key: "provider", value: "auto" }).then(function (s2) { S.settings = s2; }));
      return Promise.all(jobs);
    }).then(function () { loadProviders(); }).catch(function () {});
  }

  // model overrides: settings.model_overrides = {provider id or "*": {model id: {context_window: n}}}

  var OV = { provider: null, filter: "", models: [], error: null, seq: 0 };
  function parseTokens(t) {
    var m = /^\s*([\d.,_]+)\s*([kKmM]?)\s*$/.exec(String(t));
    if (!m) return NaN;
    var n = Number(m[1].replace(/[,_]/g, "")) * (m[2] ? (/k/i.test(m[2]) ? 1e3 : 1e6) : 1);
    return n >= 1 ? Math.round(n) : NaN;
  }
  function overridesTable() {
    var t = S.settings.model_overrides;
    return t && typeof t === "object" && !Array.isArray(t) ? t : {};
  }
  function overrideOf(pid, model) {
    var per = overridesTable()[pid];
    var e = per && per[model];
    return e && e.context_window ? e.context_window : null;
  }
  function setOverride(pid, model, cw, savedEl) {
    var t = JSON.parse(JSON.stringify(overridesTable()));
    var per = t[pid] || {};
    if (cw) per[model] = Object.assign({}, per[model], { context_window: cw });
    else if (per[model]) { delete per[model].context_window; if (!Object.keys(per[model]).length) delete per[model]; }
    if (Object.keys(per).length) t[pid] = per; else delete t[pid];
    rpcToast("settings.set", { key: "model_overrides", value: t }).then(function (s) {
      S.settings = s;
      if (savedEl) { savedEl.classList.add("show"); setTimeout(function () { savedEl.classList.remove("show"); }, 1500); }
      renderOverrideList();
      renderOverrideBrowser();
    }).catch(function () {});
  }

  function renderOverrides() {
    var host = $("#overrides-host");
    if (!host) return;
    host.textContent = "";
    var sel = el("select", { class: "ov-provider", "aria-label": "Provider" });
    var filter = el("input", { type: "text", class: "ov-filter", placeholder: "Filter, or type any model id…", spellcheck: "false", autocomplete: "off", "aria-label": "Model filter" });
    filter.value = OV.filter;
    function fill() {
      var ids = [];
      (S.providerList || []).forEach(function (p) { if (ids.indexOf(p.id) < 0) ids.push(p.id); });
      Object.keys(overridesTable()).forEach(function (k) { if (k !== "*" && ids.indexOf(k) < 0) ids.push(k); });
      if (!OV.provider) OV.provider = S.provider && typeof S.provider === "object" && S.provider.id || ids[0] || "*";
      sel.textContent = "";
      ids.forEach(function (id) { sel.append(el("option", { value: id, text: id })); });
      sel.append(el("option", { value: "*", text: "* (any provider)" }));
      sel.value = OV.provider;
    }
    fill();
    sel.addEventListener("change", function () { OV.provider = sel.value; loadOverrideModels(false); });
    filter.addEventListener("input", function () { OV.filter = filter.value; renderOverrideBrowser(); });
    host.append(el("ul", { class: "prov-list ov-current" }),
      el("div", { class: "mp-bar ov-bar" }, sel, filter,
        el("button", { class: "btn sm", type: "button", text: "Refresh", title: "Ask the provider again for its models", onclick: function () { loadOverrideModels(true); } })),
      el("div", { class: "muted small ov-status" }),
      el("ul", { class: "prov-list ov-models" }));
    renderOverrideList();
    if (!S.providerList) rpc("providers.list").then(function (l) { S.providerList = l || []; fill(); loadOverrideModels(false); }, function () { loadOverrideModels(false); });
    else loadOverrideModels(false);
  }

  function loadOverrideModels(force) {
    var seq = ++OV.seq, pid = OV.provider, status = $("#overrides-host .ov-status");
    OV.models = []; OV.error = null;
    if (pid === "*") { if (status) status.textContent = "Overrides under * apply to that model id with any provider (a provider's own override wins)."; renderOverrideBrowser(); return; }
    if (status) status.textContent = "Loading models of " + pid + "…";
    renderOverrideBrowser();
    rpc("models.list", { provider: pid, refresh: !!force }).then(function (r) {
      if (seq !== OV.seq) return;
      OV.models = r.models || [];
      OV.error = r.error || null;
      var st = $("#overrides-host .ov-status");
      if (st) st.textContent = r.error ? "Could not list models: " + r.error + " — you can still type a model id."
        : OV.models.length + " model" + (OV.models.length === 1 ? "" : "s") + " from " + r.provider + (r.cached ? " (cached)" : "");
      renderOverrideBrowser();
    }, function (e) { var st = $("#overrides-host .ov-status"); if (seq === OV.seq && st) st.textContent = e.message; });
  }

  // one model: id, the provider's window, an input for the override
  function overrideRow(pid, model, providerCw, note) {
    var cur = overrideOf(pid, model);
    var saved = el("span", { class: "saved", text: "Saved" });
    var input = el("input", { type: "text", class: "ov-input", "data-model": model, "aria-label": "Context window override for " + model,
      placeholder: providerCw ? fmtTokens(providerCw) : "–", value: cur ? String(cur) : "", spellcheck: "false", autocomplete: "off" });
    input.addEventListener("change", function () {
      var v = input.value.trim();
      if (!v) { if (cur) setOverride(pid, model, null, saved); return; }
      var n = parseTokens(v);
      if (!n) { toast("Context window: a number of tokens, e.g. 200000, 200k or 1M", "error"); return; }
      if (n !== cur) setOverride(pid, model, n, saved);
    });
    return el("li", { "data-model": model },
      el("span", { class: "pid", text: model }),
      cur ? el("span", { class: "badge active", text: "override" }) : null,
      el("span", { class: "pmeta", text: note || (providerCw ? "provider: " + fmtTokens(providerCw) : "provider: unknown") }),
      el("span", { class: "spacer" }), input, saved);
  }

  function renderOverrideBrowser() {
    var ul = $("#overrides-host .ov-models");
    if (!ul) return;
    ul.textContent = "";
    var pid = OV.provider, q = OV.filter.trim(), ql = q.toLowerCase();
    var per = overridesTable()[pid] || {}, rows = [], seen = {};
    function cwOf(id) { var m = OV.models.find(function (x) { return x.id === id; }); return m ? m.context_window : null; }
    Object.keys(per).sort().forEach(function (id) {   // overridden first, even when not in the provider's list
      if (ql && id.toLowerCase().indexOf(ql) < 0) return;
      seen[id] = 1; rows.push(overrideRow(pid, id, cwOf(id), OV.models.length && !OV.models.some(function (x) { return x.id === id; }) ? "not in the provider's list" : null));
    });
    var rest = OV.models.filter(function (x) { return !seen[x.id] && (!ql || x.id.toLowerCase().indexOf(ql) >= 0 || (x.name || "").toLowerCase().indexOf(ql) >= 0); });
    var LIMIT = 50;
    rest.slice(0, LIMIT).forEach(function (x) { rows.push(overrideRow(pid, x.id, x.context_window, null)); });
    if (q && !seen[q] && !OV.models.some(function (x) { return x.id === q; })) rows.push(overrideRow(pid, q, null, "custom model id"));
    rows.forEach(function (r) { ul.append(r); });
    if (rest.length > LIMIT) ul.append(el("li", { class: "muted" }, el("span", { text: (rest.length - LIMIT) + " more — type to filter" })));
    if (!rows.length) ul.append(el("li", { class: "muted" }, el("span", { text: pid === "*" ? "Type a model id to add an override." : "No matching models." })));
  }

  // every override, all providers
  function renderOverrideList() {
    var ul = $("#overrides-host .ov-current");
    if (!ul) return;
    ul.textContent = "";
    var t = overridesTable();
    Object.keys(t).sort().forEach(function (pid) {
      Object.keys(t[pid] || {}).sort().forEach(function (model) {
        var cw = overrideOf(pid, model);
        if (!cw) return;
        ul.append(el("li", { "data-override": pid + "|" + model },
          el("span", { class: "badge", text: pid === "*" ? "any provider" : pid }),
          el("span", { class: "pid", text: model }),
          el("span", { class: "pmeta", text: "context window " + fmtTokens(cw) }),
          el("span", { class: "spacer" }),
          el("button", { class: "btn sm danger", type: "button", text: "Remove", onclick: function () { setOverride(pid, model, null); } })));
      });
    });
    if (!ul.children.length) ul.append(el("li", { class: "muted" }, el("span", { text: "No overrides." })));
  }

  // access token

  function renderTokenBox(info) {
    var host = $("#token-host");
    if (!host) return;
    host.textContent = "";
    var code = el("code", { class: "token-val", text: info ? info.token : "••••••••••••••••" });
    var gen = info && info.generated_at ? el("span", { class: "muted small", text: "generated " + new Date(info.generated_at < 1e12 ? info.generated_at * 1000 : info.generated_at).toLocaleString() }) : null;
    host.append(el("div", { class: "field" }, el("label", { text: "Token" }), el("div", { class: "ctl" }, code,
      el("button", { class: "btn sm", type: "button", text: info ? "Hide" : "Reveal", id: "token-reveal", onclick: function () {
        if (info) renderTokenBox(null);
        else rpcToast("auth.get").then(renderTokenBox, function () {});
      } }),
      el("button", { class: "btn sm", type: "button", text: "Copy", onclick: function () {
        (info ? Promise.resolve(info) : rpcToast("auth.get")).then(function (i) { copyText(i.token); toast("Token copied"); }, function () {});
      } }), gen)),
      el("div", { class: "field" }, el("label", { text: "Regenerate" }), el("div", { class: "ctl" },
        el("button", { class: "btn sm danger", type: "button", id: "token-regenerate", text: "Regenerate token", onclick: function () {
          if (!confirm("Write a new access token into this file?\n\nEvery connected client (all browser tabs, TUIs) is disconnected and must reload / restart to read the new token from the file.")) return;
          rpcToast("auth.regenerate").catch(function () {});
        } }),
        el("span", { class: "muted small", text: "All clients must reload afterwards." }))));
  }

  function doExport(view) {
    if (S.settings.credentials_storage === "embedded" &&
        !confirm("Credentials are embedded in this file, so the export will CONTAIN your API keys. Anyone you give the copy to can use them.\n\nExport anyway?")) return;
    var url = "http://127.0.0.1:" + S.port + "/api/export?" + tokQ() + "&view=" + encodeURIComponent(view || "");
    var a = el("a", { href: url, download: "" });
    document.body.append(a);
    a.click();
    a.remove();
    toast("Export started");
  }

  // ---- dialogs: folder picker, model picker ----------------------------------------------------

  function openModal(o) {
    var back = el("div", { class: "modal-back" });
    var closeBtn = el("button", { class: "icon-btn", type: "button", title: "Close (Esc)", "aria-label": "Close", text: "✕" });
    var box = el("div", { class: "modal " + (o.cls || ""), role: "dialog", "aria-modal": "true", "aria-label": o.title },
      el("div", { class: "modal-head" }, el("h2", { text: o.title }), el("span", { class: "spacer" }), closeBtn),
      el("div", { class: "modal-body" }, o.note ? el("p", { class: "note", text: o.note }) : null, o.body),
      el("div", { class: "modal-foot" }, o.foot));
    back.append(box);
    $("#sfh-root").append(back);
    var prevFocus = document.activeElement, closed = false;
    var m = {
      back: back, box: box,
      close: function () {
        if (closed) return;
        closed = true;
        back.remove();
        if (prevFocus && prevFocus.isConnected && prevFocus.focus) prevFocus.focus();
        if (o.onClose) o.onClose();
      }
    };
    closeBtn.addEventListener("click", m.close);
    back.addEventListener("mousedown", function (e) { if (e.target === back) m.close(); });
    // keys stay inside the dialog (Esc must not stop the running turn, letters must not jump to the composer)
    box.addEventListener("keydown", function (e) {
      if (e.key === "Escape") { e.preventDefault(); m.close(); }
      e.stopPropagation();
    });
    return m;
  }
  function modalOpen() { return !!document.querySelector(".modal-back"); }

  function folderIcon() {
    return svgEl('<svg viewBox="0 0 16 16" width="15" height="15" aria-hidden="true"><path d="M1.5 4.2c0-.7.5-1.2 1.2-1.2h3.4l1.5 1.6h5.7c.7 0 1.2.5 1.2 1.2v6.5c0 .7-.5 1.2-1.2 1.2H2.7c-.7 0-1.2-.5-1.2-1.2z" fill="currentColor" opacity=".8"/></svg>');
  }

  // A small file explorer for choosing a directory. Resolves with the absolute path, or null.
  function pickFolder(o) {
    o = o || {};
    return new Promise(function (resolve) {
      var st = { path: null, ok: false, hidden: !!LS.get("pickHidden", false), done: false, seq: 0 };
      var input = el("input", { type: "text", class: "fp-path", spellcheck: "false", autocomplete: "off", "aria-label": "Folder path",
        title: "Type a path and press Enter (~ works)" });
      var up = el("button", { class: "btn sm", type: "button", title: "Parent folder (Backspace)", text: "↑" });
      var hid = el("input", { type: "checkbox" });
      hid.checked = st.hidden;
      var list = el("div", { class: "fp-list", role: "listbox", "aria-label": "Folders" });
      var places = el("div", { class: "fp-places" });
      var status = el("div", { class: "fp-status muted small" });
      var choose = el("button", { class: "btn primary", type: "button", text: "Use this folder", disabled: true });
      var chosen = el("code", { class: "fp-chosen" });
      var m = openModal({
        title: o.title || "Choose a folder", cls: "folder-picker", note: o.note,
        body: el("div", { class: "fp-body" }, places, el("div", { class: "fp-main" },
          el("div", { class: "fp-bar" }, up, input, el("label", { class: "fp-hidden small muted", title: "Show hidden folders" }, hid, "hidden")),
          list, status)),
        foot: [chosen, el("span", { class: "spacer" }),
          el("button", { class: "btn", type: "button", text: "Cancel", onclick: function () { m.close(); } }), choose],
        onClose: function () { if (!st.done) resolve(null); }
      });
      function finish() {
        if (!st.ok) return;
        st.done = true;
        resolve(st.path);
        m.close();
      }
      choose.addEventListener("click", finish);
      function go(path, focusList) {
        var seq = ++st.seq;
        list.classList.add("loading");
        rpc("fs.list", { path: path, hidden: st.hidden }).then(function (r) {
          if (seq !== st.seq) return;
          list.classList.remove("loading");
          st.path = r.path;
          st.ok = !!r.exists && !r.error;
          input.value = r.path;
          up.disabled = !r.parent;
          up._to = r.parent;
          choose.disabled = !st.ok;
          chosen.textContent = st.ok ? tildify(r.path) : "";
          list.textContent = "";
          (r.dirs || []).forEach(function (n) {
            var full = (r.path === "/" ? "" : r.path) + "/" + n;
            list.append(el("button", { class: "fp-item", type: "button", role: "option", title: full, "data-name": n,
              onclick: function () { go(full, true); } }, folderIcon(), el("span", { text: n })));
          });
          status.textContent = r.error ? r.error : (r.dirs.length ? r.dirs.length + " folder" + (r.dirs.length === 1 ? "" : "s") : "No sub-folders") +
            (r.truncated ? " (list truncated)" : "") + (!st.hidden && r.hidden_count ? " · " + r.hidden_count + " hidden" : "");
          status.classList.toggle("err", !!r.error);
          places.querySelectorAll(".fp-place").forEach(function (b) { b.classList.toggle("active", b.getAttribute("data-path") === r.path); });
          if (focusList) { var first = $(".fp-item", list); (first || input).focus(); }
        }, function (e) { if (seq === st.seq) { list.classList.remove("loading"); status.textContent = e.message; } });
      }
      up.addEventListener("click", function () { if (up._to) go(up._to, true); });
      hid.addEventListener("change", function () { st.hidden = hid.checked; LS.set("pickHidden", st.hidden); go(st.path || input.value); });
      input.addEventListener("keydown", function (e) {
        if (e.key === "Enter") {
          e.preventDefault();
          var v = input.value.trim();
          if (st.ok && (v === st.path || v === st.path + "/")) finish(); else go(v);
        } else if (e.key === "ArrowDown") { var f = $(".fp-item", list); if (f) { e.preventDefault(); f.focus(); } }
      });
      // list keys: arrows move, Backspace goes up, Enter opens, letters jump to the next match
      list.addEventListener("keydown", function (e) {
        var items = Array.prototype.slice.call(list.querySelectorAll(".fp-item"));
        var i = items.indexOf(document.activeElement);
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          if (e.key === "ArrowUp" && i <= 0) { input.focus(); return; }
          var n = items[Math.max(0, Math.min(items.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
          if (n) n.focus();
        } else if (e.key === "Backspace" || e.key === "ArrowLeft") {
          e.preventDefault();
          if (up._to) go(up._to, true);
        } else if (e.key === "ArrowRight" && i >= 0) {
          e.preventDefault();
          items[i].click();
        } else if (e.key.length === 1 && !e.ctrlKey && !e.metaKey && !e.altKey) {
          var k = e.key.toLowerCase();
          for (var j = 1; j <= items.length; j++) {
            var it = items[(Math.max(i, 0) + j) % items.length];
            if ((it.getAttribute("data-name") || "").toLowerCase().indexOf(k) === 0) { it.focus(); break; }
          }
        }
      });
      rpc("cwd.info").then(function (info) {
        function place(label, path, extra) {
          if (!path) return null;
          return el("button", { class: "fp-place" + (extra ? " " + extra : ""), type: "button", "data-path": path, title: path,
            onclick: function () { go(path, true); } }, el("span", { class: "pl", text: label }), el("span", { class: "pp", text: tildify(path) }));
        }
        places.append(el("div", { class: "fp-label", text: "Places" }),
          place("Home", info.home), place("Server directory", info.server_cwd));
        var recent = (info.recent || []).filter(function (r) { return r.exists; });
        if (recent.length) {
          places.append(el("div", { class: "fp-label", text: "Recent" }));
          recent.slice(0, 15).forEach(function (r) { places.append(place(basename(r.path), r.path, "recent")); });
        }
        if (!st.path && !o.initial) go(info.default, false);
        else if (st.path) places.querySelectorAll(".fp-place").forEach(function (b) { b.classList.toggle("active", b.getAttribute("data-path") === st.path); });
      }, function () {});
      if (o.initial) go(o.initial, false);
      input.focus();
    });
  }

  // Provider + model chooser. o.provider/o.model: the current choice (null = follow the default).
  // Resolves with {provider, model} (nulls = default) or null when cancelled.
  function pickModel(o) {
    o = o || {};
    return new Promise(function (resolve) {
      var st = { provider: o.provider || "", model: o.model || "", models: [], done: false, seq: 0, providers: S.providerList || [] };
      var sel = el("select", { "aria-label": "Provider", class: "mp-provider" });
      var filter = el("input", { type: "text", placeholder: "Filter, or type any model id…", spellcheck: "false", autocomplete: "off", "aria-label": "Model" });
      var refresh = el("button", { class: "btn sm", type: "button", text: "Refresh", title: "Ask the provider again for its models" });
      var list = el("div", { class: "mp-list", role: "listbox", "aria-label": "Models" });
      var status = el("div", { class: "muted small mp-status" });
      var ok = el("button", { class: "btn primary", type: "button", text: "Use this model" });
      var cur0 = o.current || {};
      var m = openModal({
        title: o.title || "Choose a model", cls: "model-picker", note: o.note,
        body: el("div", { class: "mp-body" },
          cur0.provider ? el("div", { class: "small muted" }, "Now: ", el("code", { text: cur0.provider + (cur0.model && cur0.model !== "mock" ? " · " + cur0.model : "") })) : null,
          el("div", { class: "field" }, el("label", { text: "Provider" }), sel),
          el("div", { class: "mp-bar" }, filter, refresh), list, status),
        foot: [el("span", { class: "spacer" }), el("button", { class: "btn", type: "button", text: "Cancel", onclick: function () { m.close(); } }), ok],
        onClose: function () { if (!st.done) resolve(null); }
      });
      function finish() {
        st.done = true;
        resolve({ provider: st.provider || null, model: st.model || null });
        m.close();
      }
      ok.addEventListener("click", finish);
      function effectiveProvider() { return st.provider || globalChoice().provider || ""; }
      function defaultModel(pid) {
        var g = globalChoice();
        if (!pid || pid === g.provider) return g.model;
        var p = st.providers.find(function (x) { return x.id === pid; });
        return p ? p.model : null;
      }
      function fillProviders() {
        var g = globalChoice();
        sel.textContent = "";
        sel.append(el("option", { value: "", text: "Default" + (g.provider ? " (" + g.provider + ")" : "") }));
        st.providers.forEach(function (p) {
          sel.append(el("option", { value: p.id, text: p.id + " — " + p.type + (p.type === "anthropic" && !p.has_key ? " (no key)" : "") }));
        });
        if (st.provider && !st.providers.some(function (p) { return p.id === st.provider; })) sel.append(el("option", { value: st.provider, text: st.provider + " (unknown)" }));
        sel.value = st.provider;
      }
      function load(force) {
        var seq = ++st.seq, pid = effectiveProvider();
        status.textContent = "Loading models of " + (pid || "the default provider") + "…";
        status.classList.remove("err");
        st.models = [];
        render();
        rpc("models.list", pid ? { provider: pid, refresh: !!force } : { refresh: !!force }).then(function (r) {
          if (seq !== st.seq) return;
          st.models = r.models || [];
          status.textContent = r.error ? "Could not list models: " + r.error + " — you can still type a model id."
            : st.models.length + " model" + (st.models.length === 1 ? "" : "s") + " from " + r.provider + (r.cached ? " (cached)" : "");
          status.classList.toggle("err", !!r.error);
          render();
        }, function (e) { if (seq === st.seq) { status.textContent = e.message; status.classList.add("err"); } });
      }
      function item(id, label, sub) {
        var b = el("button", { class: "mp-item" + (st.model === id ? " sel" : ""), type: "button", role: "option", "data-model": id,
          "aria-selected": st.model === id ? "true" : "false" }, el("span", { class: "mid", text: label }), sub ? el("span", { class: "mname", text: sub }) : null);
        b.addEventListener("click", function () { st.model = id; render(); });
        b.addEventListener("dblclick", function () { st.model = id; finish(); });
        return b;
      }
      function render() {
        var q = filter.value.trim(), ql = q.toLowerCase();
        list.textContent = "";
        var dm = defaultModel(effectiveProvider());
        if (!ql) list.append(item("", "Provider default", dm ? dm : ""));
        var shown = st.models.filter(function (x) { return !ql || x.id.toLowerCase().indexOf(ql) >= 0 || (x.name || "").toLowerCase().indexOf(ql) >= 0; });
        shown.forEach(function (x) { list.append(item(x.id, x.id, x.name || "")); });
        if (q && !st.models.some(function (x) { return x.id === q; })) list.append(item(q, "Use “" + q + "”", "custom model id"));
        if (st.model && !q && !st.models.some(function (x) { return x.id === st.model; })) list.append(item(st.model, st.model, "current choice"));
        var s = $(".mp-item.sel", list);
        if (s) s.scrollIntoView({ block: "nearest" });
      }
      sel.addEventListener("change", function () { st.provider = sel.value; st.model = ""; filter.value = ""; load(false); });
      refresh.addEventListener("click", function () { load(true); });
      filter.addEventListener("input", render);
      filter.addEventListener("keydown", function (e) {
        var items = Array.prototype.slice.call(list.querySelectorAll(".mp-item"));
        var i = items.findIndex(function (b) { return b.classList.contains("sel"); });
        if (e.key === "ArrowDown" || e.key === "ArrowUp") {
          e.preventDefault();
          var n = items[Math.max(0, Math.min(items.length - 1, i + (e.key === "ArrowDown" ? 1 : -1)))];
          if (n) { st.model = n.getAttribute("data-model"); render(); }
        } else if (e.key === "Enter") {
          e.preventDefault();
          if (i < 0 && items.length === 1) st.model = items[0].getAttribute("data-model");
          else if (i < 0 && filter.value.trim()) st.model = filter.value.trim();
          finish();
        }
      });
      fillProviders();
      rpc("providers.list").then(function (list2) { st.providers = list2 || []; S.providerList = st.providers; fillProviders(); render(); }, function () {});
      load(false);
      filter.focus();
    });
  }

  // ---- terminal panel --------------------------------------------------------------------------

  var T = { term: null, fit: null, ws: null, ro: null };
  function closeTerminal() {
    if (T.ws) { try { T.ws.onclose = null; T.ws.close(); } catch (e) { /* ignore */ } }
    if (T.ro) T.ro.disconnect();
    if (T.term) T.term.dispose();
    T = { term: null, fit: null, ws: null, ro: null };
    $("#term-panel").hidden = true;
    $("#term-host").textContent = "";
    if (cur) cur.ta.focus();
  }

  function openTerminal(o) {
    if (!S.connected) { toast("Not connected", "error"); return; }
    if (typeof window.Terminal !== "function") { toast("Terminal support (xterm.js) is not bundled in this build", "error"); return; }
    closeTerminal();
    var panel = $("#term-panel");
    panel.hidden = false;
    panel.style.height = (LS.get("termHeight", 0) || Math.round(window.innerHeight * 0.42)) + "px";
    $("#term-title").textContent = o.title || o.cmd;
    var term = new window.Terminal({
      fontFamily: getComputedStyle(document.body).getPropertyValue("--mono") || "monospace", fontSize: 13, cursorBlink: true,
      scrollback: 5000, theme: { background: "#111317", foreground: "#dfe3ea", cursor: "#dfe3ea", selectionBackground: "#3a4a6a" }
    });
    var fit = new window.FitAddon.FitAddon();
    term.loadAddon(fit);
    term.open($("#term-host"));
    try { fit.fit(); } catch (e) { /* ignore */ }
    var q = tokQ() + "&cmd=" + encodeURIComponent(o.cmd) + "&cwd=" + encodeURIComponent(o.cwd || "") +
      "&cols=" + term.cols + "&rows=" + term.rows;
    if (o.file) q += "&file=" + encodeURIComponent(o.file);
    if (o.line) q += "&line=" + encodeURIComponent(o.line);
    var ws = new WebSocket("ws://127.0.0.1:" + S.port + "/pty?" + q);
    var exited = false, pendingInput = [];
    ws.onopen = function () { pendingInput.forEach(function (f) { ws.send(f); }); pendingInput = []; };
    function wsSend(obj) {
      var f = JSON.stringify(obj);
      if (ws.readyState === 1) ws.send(f);
      else if (ws.readyState === 0) pendingInput.push(f);
    }
    ws.onmessage = function (ev) {
      var m;
      try { m = JSON.parse(ev.data); } catch (e) { return; }
      if (m.type === "output") term.write(m.data);
      else if (m.type === "exit") {
        exited = true;
        if (o.cmd === "editor" && m.code === 0) { closeTerminal(); return; }
        term.write("\r\n\x1b[2m[process exited with code " + m.code + "; press any key to close]\x1b[0m\r\n");
      }
    };
    ws.onclose = function () { if (!exited) term.write("\r\n\x1b[2m[connection closed]\x1b[0m\r\n"); exited = true; };
    term.onData(function (d) {
      if (exited) { closeTerminal(); return; }
      wsSend({ type: "input", data: d });
    });
    term.onResize(function (sz) { wsSend({ type: "resize", cols: sz.cols, rows: sz.rows }); });
    var ro = new ResizeObserver(function () { try { fit.fit(); } catch (e) { /* ignore */ } });
    ro.observe($("#term-host"));
    T = { term: term, fit: fit, ws: ws, ro: ro };
    term.focus();
  }

  function initTermResize() {
    var handle = $("#term-resize"), panel = $("#term-panel");
    handle.addEventListener("mousedown", function (e) {
      e.preventDefault();
      var startY = e.clientY, startH = panel.offsetHeight;
      function move(ev) {
        var h = Math.max(120, Math.min(window.innerHeight - 120, startH + (startY - ev.clientY)));
        panel.style.height = h + "px";
      }
      function up() {
        document.removeEventListener("mousemove", move);
        document.removeEventListener("mouseup", up);
        LS.set("termHeight", panel.offsetHeight);
      }
      document.addEventListener("mousemove", move);
      document.addEventListener("mouseup", up);
    });
    $("#term-close").addEventListener("click", closeTerminal);
  }

  // Relative paths are relative to the cwd of the message the link is in. Links of messages stored
  // with resolved links carry data-abs (the absolute path that existed when the message was written).
  function openLocalPath(target) {
    if (!cur) { toast("Open a session to use file links", "error"); return; }
    if (S.offline) { toast("Start the server to open files", "error"); return; }
    var path = target.getAttribute("data-abs") || target.getAttribute("data-local-path"), line = target.getAttribute("data-line");
    var s = S.sessions.get(cur.id) || {};
    var holder = target.closest("[data-mid]");
    var mid = holder ? Number(holder.getAttribute("data-mid")) : null;
    var m = mid != null ? cur.messages.find(function (x) { return x.id === mid; }) : null;
    var cwd = (m && m.cwd) || s.cwd;
    rpcToast("fs.resolve", { session_id: cur.id, path: path, cwd: cwd }).then(function (r) {
      if (!r.exists) toast("No longer exists: " + r.path, "error");
      else if (r.is_dir) openTerminal({ cmd: "shell", cwd: r.path, title: "shell · " + tildify(r.path) });
      else openTerminal({ cmd: "editor", file: r.path, line: line, cwd: cwd, title: tildify(r.path) + (line ? ":" + line : "") });
    }).catch(function () {});
  }

  // ---- global handlers -------------------------------------------------------------------------

  document.addEventListener("click", function (e) {
    var t = e.target.closest && e.target.closest("[data-local-path]");
    if (t && !e.ctrlKey && !e.metaKey && e.button === 0) { e.preventDefault(); openLocalPath(t); return; }
    var ab = e.target.closest && e.target.closest("[data-artifact-act]");
    if (ab) { artifactAction(ab); return; }
    // in-page anchors of agent HTML (ids are namespaced "mdu-"): scroll, don't touch the #route
    var ia = e.target.closest && e.target.closest('a[href^="#mdu-"]');
    if (ia) {
      e.preventDefault();
      var tgt = document.getElementById(ia.getAttribute("href").slice(1));
      if (tgt) tgt.scrollIntoView({ block: "start", behavior: "smooth" });
      return;
    }
    var cp = e.target.closest && e.target.closest(".copy-code");
    if (cp) {
      var code = $("code", cp.parentNode);
      copyText(code ? code.textContent : "");
      cp.textContent = "Copied";
      setTimeout(function () { cp.textContent = "Copy"; }, 1200);
    }
  });

  document.addEventListener("keydown", function (e) {
    if (e.target.closest && e.target.closest("#term-panel")) return;
    if (modalOpen()) return;
    var tag = (e.target.tagName || "").toLowerCase();
    var typing = tag === "input" || tag === "textarea" || tag === "select" || e.target.isContentEditable;
    if ((e.ctrlKey || e.metaKey) && !e.shiftKey && !e.altKey && e.key.toLowerCase() === "b") { e.preventDefault(); toggleSidebar(); return; }
    if (e.altKey && !e.ctrlKey && !e.metaKey && e.key.toLowerCase() === "n") { e.preventDefault(); location.hash = "#new"; return; }
    if (e.key === "Escape" && !typing && cur) { stopRun(); return; }
    if (!typing && !e.ctrlKey && !e.metaKey && !e.altKey && (cur || newView) && e.key.length === 1) {
      var c = cur || newView;
      c.ta.focus();
    }
  });

  // ---- onboarding: first run, provider setup, landing page of a published copy (questions/034) ------
  // Signals: H = has_history, P = has_provider (both from the server), W = IS_WEB (not opened as file://).
  // no H + no P: two-step screen (start the server, set up a provider); H + no P: the normal UI, read only,
  // with a banner; W: never contacts a local server: landing page, or the read-only snapshot + install banner.

  function about(k) { return ((S.hello && S.hello.about) || ABOUT)[k] || ""; }
  function mdBlock(text, cls) { var d = el("div", { class: "md" + (cls ? " " + cls : "") }); d.innerHTML = MD.render(text || ""); return d; }
  function providerReady() { return !S.onboarding || !!S.onboarding.has_provider; }
  function needsSetup() { var o = S.onboarding; return !!o && !o.has_history && !o.has_provider; }

  function setStep(id, state) {
    var li = $("#" + id);
    li.classList.remove("active", "done", "pending");
    li.classList.add(state);
  }

  // not running yet (file://, no snapshot with history): step 1 of 2
  function showConnect() {
    S.setupShown = false;
    $("#app").hidden = true;
    $("#landing").hidden = true;
    $("#connect").hidden = false;
    $("#connect-cmd").textContent = RUN_CMD;
    $("#connect-title").textContent = "Start the harness";
    $("#connect-tagline").textContent = about("tagline");
    setStep("step-run", "active");
    setStep("step-provider", "pending");
    if (!$("#connect-logo").firstChild) $("#connect-logo").innerHTML = IC.svg({ id: SELF_PATH, subjects: ["harness", basename(SELF_PATH)] }, 56);
    document.title = "Not running · " + basename(SELF_PATH);
    setFavicon(defaultFavicon());
  }

  // server runs, but nothing to show and no provider: step 2 of 2
  function showSetup() {
    $("#app").hidden = true;
    $("#landing").hidden = true;
    $("#connect").hidden = false;
    $("#connect-title").textContent = "Set up the harness";
    $("#connect-tagline").textContent = about("tagline");
    setStep("step-run", "done");
    setStep("step-provider", "active");
    if (!$("#connect-logo").firstChild) $("#connect-logo").innerHTML = IC.svg({ id: SELF_PATH, subjects: ["harness", basename(SELF_PATH)] }, 56);
    if (!S.setupShown) {
      var host = $("#step-provider-body");
      host.textContent = "";
      host.append(el("p", { class: "muted", text: about("provider") }), providerSetupForm({}), mdBlock(about("provider_env"), "setup-env"));
    }
    S.setupShown = true;
    document.title = "Set up · " + basename(SELF_PATH);
    setFavicon(defaultFavicon());
  }

  function leaveSetup() {
    S.setupShown = false;
    $("#connect").hidden = true;
    $("#app").hidden = false;
    renderSidebar();
    route(true);
  }

  // the `onboarding` event (and hello): H/P/moved changed
  function applyOnboarding(o) {
    var was = providerReady();
    S.onboarding = o || null;
    if (!S.everConnected || S.offline || S.fatal) return;
    if (needsSetup()) { showSetup(); return; }
    if (S.setupShown) { toast("Provider ready"); leaveSetup(); }
    renderNotices();
    updateComposer();
    if (was !== providerReady() && cur && !cur.loading && cur.msgsEl) { renderMessages(); renderInbox(S.sessions.get(cur.id)); }
  }

  // banners under the connection banner: no provider (read only), file moved (token)
  function renderNotices() {
    var host = $("#notices");
    host.textContent = "";
    if (!S.connected || S.offline || !S.onboarding) return;
    if (!S.onboarding.has_provider && !S.noticeDismissed.provider) {
      host.append(el("div", { class: "notice-bar", id: "notice-provider" },
        el("strong", { text: "No LLM provider is configured." }),
        el("span", { text: "You can browse everything, but nothing can be sent to a model." }),
        el("a", { class: "btn sm primary", href: "#setup", text: "Set up a provider" }),
        el("span", { class: "spacer" }),
        el("button", { class: "icon-btn", type: "button", title: "Dismiss (returns on reload)", "aria-label": "Dismiss", text: "✕",
          onclick: function () { S.noticeDismissed.provider = true; renderNotices(); } })));
    }
    if (S.onboarding.moved && !S.noticeDismissed.moved) {
      host.append(el("div", { class: "notice-bar warn", id: "notice-moved" },
        el("strong", { text: "This file runs somewhere new." }),
        el("span", { text: "It was copied or moved to another computer, user or path since its access token was made. A new token keeps old copies from talking to this server." }),
        el("button", { class: "btn sm", type: "button", text: "Regenerate token", onclick: regenerateToken }),
        el("span", { class: "spacer" }),
        el("button", { class: "icon-btn", type: "button", title: "Dismiss (returns on reload)", "aria-label": "Dismiss", text: "✕",
          onclick: function () { S.noticeDismissed.moved = true; renderNotices(); } })));
    }
  }

  function regenerateToken() {
    if (!confirm("Write a new access token into this file?\n\nEvery connected client (all browser tabs, TUIs) is disconnected and must reload / restart to read the new token from the file.")) return;
    rpcToast("auth.regenerate").catch(function () {});
  }

  // the provider form of step 2 and of #setup: endpoint, key, model (from the endpoint's list), Test, Save
  function providerSetupForm(o) {
    var type = el("select", { name: "type", "aria-label": "Provider type" },
      el("option", { value: "openai", text: "OpenAI-compatible" }), el("option", { value: "anthropic", text: "Anthropic API" }));
    var base = el("input", { type: "text", name: "base_url", spellcheck: "false", placeholder: "https://api.openai.com/v1 or http://127.0.0.1:11434/v1" });
    var key = el("input", { type: "password", name: "token", autocomplete: "new-password", placeholder: "API key (optional for local endpoints)" });
    var dl = el("datalist", { id: "setup-models-" + (++S.setupSeq) });
    var model = el("input", { type: "text", name: "model", list: dl.id, spellcheck: "false", placeholder: "model id (pick from the list once it loads)" });
    var status = el("p", { class: "setup-status muted small" });
    var seq = 0;
    function spec() { return { type: type.value, base_url: base.value.trim(), token: key.value.trim(), model: model.value.trim() }; }
    function say(t, lvl) { status.textContent = t; status.className = "setup-status small " + (lvl || "muted"); }
    function probe() {
      var sp = spec();
      if (sp.type === "openai" && !sp.base_url) return;
      var n = ++seq;
      say("Loading the model list…");
      rpc("providers.probe", sp).then(function (r) {
        if (n !== seq) return;
        dl.textContent = "";
        (r.models || []).forEach(function (m) { dl.append(el("option", { value: m.id })); });
        if (r.error) say("Couldn't list models: " + r.error + ". You can still type a model id.", "warn");
        else {
          say(r.models.length + " model" + (r.models.length === 1 ? "" : "s") + " available. Pick one, then Test or Save.");
          if (!model.value && r.models.length === 1) model.value = r.models[0].id;
        }
      }, function (e) { if (n === seq) say(e.message, "error"); });
    }
    base.addEventListener("change", probe);
    key.addEventListener("change", probe);
    type.addEventListener("change", function () {
      base.placeholder = type.value === "anthropic" ? "https://api.anthropic.com (default)" : "https://api.openai.com/v1 or http://127.0.0.1:11434/v1";
      probe();
    });
    var testBtn = el("button", { class: "btn", type: "button", text: "Test", onclick: function () {
      say("Sending a test message…");
      rpc("providers.test", spec()).then(function (r) {
        if (r.ok) say("It works. The model replied: " + (r.reply || "(empty)"), "ok");
        else say("Test failed: " + r.error, "error");
      }, function (e) { say(e.message, "error"); });
    } });
    var saveBtn = el("button", { class: "btn primary", type: "submit", text: "Save and use" });
    var form = el("form", { class: "setup-form", onsubmit: function (e) {
      e.preventDefault();
      if (!spec().model) { say("Choose a model first.", "error"); model.focus(); return; }
      saveBtn.disabled = true;
      rpc("providers.setup", spec()).then(function (r) {
        saveBtn.disabled = false;
        say("Saved as provider “" + r.id + "”.", "ok");
        toast("Provider " + r.id + " saved");
        if (o.onDone) o.onDone(r);
      }, function (e) { saveBtn.disabled = false; say(e.message, "error"); });
    } },
      el("div", { class: "field" }, el("label", { text: "Type" }), type),
      el("div", { class: "field" }, el("label", { text: "Base URL" }), base),
      el("div", { class: "field" }, el("label", { text: "API key" }), key),
      el("div", { class: "field" }, el("label", { text: "Model" }), model), dl,
      el("div", { class: "ctl" }, saveBtn, testBtn), status);
    return form;
  }

  // #setup: the same form as a page (from the "no provider" banner and the read-only composer)
  function renderSetupPage() {
    newView = null;
    var inner = el("div", { class: "page-inner" }, el("div", { class: "page-head" }, el("h1", { text: "Set up an LLM provider" })));
    if (S.offline) inner.append(el("div", { class: "empty" }, el("p", { text: "Start the server first:" }), el("code", { text: RUN_CMD })));
    else inner.append(el("p", { class: "muted", text: about("provider") }),
      providerReady() ? el("p", { class: "note", text: "A provider is already configured; saving this one makes it the active provider. All providers are listed in Settings." }) : null,
      providerSetupForm({ onDone: function () { location.hash = cur || S.lastSession ? "#session/" + encodeURIComponent((cur && cur.id) || S.lastSession) : "#new"; } }),
      mdBlock(about("provider_env"), "setup-env"));
    setView(el("div", { class: "page" }, inner));
    setPageMeta("Set up a provider · " + basename(SELF_PATH));
  }

  // #about: what this is, how to run it, how to connect an LLM
  function renderAbout() {
    newView = null;
    var inner = el("div", { class: "page-inner about-page" }, el("div", { class: "page-head" }, el("h1", { text: "About this harness" })),
      mdBlock(about("what")),
      el("h2", { text: "Running it" }),
      el("p", { class: "muted", text: about("run") }), el("div", { class: "cmdbox" }, el("code", { text: RUN_CMD }),
        el("button", { class: "btn", type: "button", text: "Copy", onclick: function () { copyText(RUN_CMD); toast("Copied"); } })),
      el("p", { class: "muted small" }, "Append ", el("code", { text: "tui" }), " for the terminal client, or ", el("code", { text: "help" }), " for every command."),
      el("h2", { text: "LLM providers" }),
      el("p", { text: about("provider") }),
      S.offline ? null : el("p", null, el("a", { class: "btn sm", href: "#setup", text: "Set up a provider" }), " ",
        el("a", { class: "btn sm", href: "#settings", text: "All providers in Settings" })),
      mdBlock(about("provider_env"), "setup-env"),
      el("p", { class: "muted small", text: "Version " + ((S.hello && S.hello.version) || "?") + " · " + SELF_PATH }));
    setView(el("div", { class: "page" }, inner));
    setPageMeta("About · " + basename(SELF_PATH));
  }

  // ---- a copy published on the web (W): landing page / download ------------------------------------

  function downloadName() {
    var n = basename(SELF_PATH);
    if (!n || n === "/" || !/\.html?$/i.test(n)) n = "harness.html";
    return n;
  }
  function downloadSelf() {
    var url = location.href.split("#")[0];
    function save(href) { var a = el("a", { href: href, download: downloadName() }); document.body.append(a); a.click(); a.remove(); }
    fetch(url, { cache: "no-store" }).then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.blob();
    }).then(function (b) {
      var u = URL.createObjectURL(new Blob([b], { type: "text/html" }));
      save(u);
      setTimeout(function () { URL.revokeObjectURL(u); }, 30000);
    }, function () { save(url); });   // the browser's own download of the URL (same bytes)
  }

  function showLanding() {
    $("#app").hidden = true;
    $("#connect").hidden = true;
    $("#landing").hidden = false;
    var card = $("#landing-card");
    var example = "sh ~/Downloads/" + shq(downloadName()).replace(/^'(.*)'$/, "$1");
    card.textContent = "";
    card.append(el("div", { class: "connect-logo", html: IC.svg({ id: "singlefile-llm-harness", subjects: ["harness", "llm"] }, 56) }),
      el("h1", { text: "Single-file LLM harness" }),
      el("p", { class: "tagline", text: about("tagline") }),
      mdBlock(about("what").replace(about("tagline"), "").trim()),
      el("h2", { text: "1. Save it" }),
      el("p", null, el("button", { class: "btn primary big", type: "button", id: "landing-download", text: "Download " + downloadName(), onclick: downloadSelf })),
      mdBlock(about("save_alt"), "muted small"),
      el("h2", { text: "2. Run it" }),
      mdBlock(about("install")),
      el("p", { class: "muted small" }, "If you saved it to your Downloads folder, the command will look like ", el("code", { text: example }), "."),
      el("h2", { text: "3. Connect an LLM" }),
      el("p", { text: about("provider") }),
      S.offlineData && hasSnapshotHistory(S.offlineData) ? el("p", null, el("button", { class: "btn", type: "button", text: "Back to the shared sessions",
        onclick: function () { $("#landing").hidden = true; S.offline = false; showOffline(); } })) : null);
    document.title = "Single-file LLM harness";
    setFavicon(defaultFavicon());
  }

  function hasSnapshotHistory(d) {
    if (!d) return false;
    if (typeof d.has_history === "boolean") return d.has_history;
    return !!(d.sessions && d.sessions.length);
  }

  // ---- boot ------------------------------------------------------------------------------------

  function boot() {
    $("#sb-toggle").addEventListener("click", toggleSidebar);
    if (LS.get("sbCollapsed", false) || window.innerWidth < 760) $("#app").classList.add("sb-collapsed");
    syncSbToggle();
    initSbResize();
    $("#connect-copy").addEventListener("click", function () { copyText(RUN_CMD); $("#connect-copy").textContent = "Copied"; setTimeout(function () { $("#connect-copy").textContent = "Copy"; }, 1500); });
    $("#connect-cmd").textContent = RUN_CMD;
    initTermResize();
    setFavicon(defaultFavicon());
    setInterval(function () {
      // keep relative times fresh
      if (S.route && S.route.name === "sessions" && !document.hidden) renderSessionsGrid();
      if (!document.hidden && S.sessions.size) { renderSidebar(); renderPeerStrip(); }  // peers go stale after 15 min
    }, 60000);
    $("#gate-reload").addEventListener("click", function () { location.reload(); });
    loadOffline().then(function (d) {
      S.offlineData = d;
      if (!IS_WEB) { connectLoop(); return; }
      if (hasSnapshotHistory(d)) showOffline(); else showLanding();
    });
  }

  window.SFHApp = { state: S, rpc: rpc, candidatePorts: candidatePorts, fnv1a: fnv1a, get current() { return cur; }, get terminal() { return T; } };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();
})();
