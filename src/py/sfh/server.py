"""The tremolo app: HTTP endpoints, the /ws RPC + event channel, /pty, startup and shutdown."""
import asyncio
import base64
import contextlib
import hmac
import json
import os
import re
import secrets
import signal
import socket
import sys
import time
import urllib.parse

from tremolo import Application

from . import about, agent as agentmod, branches, commands, common, creds as credsmod, htmlfile, llm, pty_ws, skills
from .store import Store, set_token as store_set_token

CORS_HEADERS = {"Access-Control-Allow-Headers": "content-type, x-sfh-path, x-sfh-token",
                "Access-Control-Allow-Methods": "GET, POST, OPTIONS",
                "Access-Control-Allow-Private-Network": "true",
                "Access-Control-Max-Age": "600",
                "Vary": "Origin",
                "Access-Control-Expose-Headers": "x-sfh-warning"}
PAIRING_SECONDS = 300
MODELS_TTL = 300
PRICING_TTL = 3600       # a fetched price list is reused this long
PRICING_RETRY = 300      # after a failed / empty pricing fetch, try again after this long
PROVIDER_KEYS = ("provider", "providers", "model", "cheap_model")
MAX_DIR_ENTRIES = 3000
TOKEN_CLOSE_CODE = 4001
OFFLINE_SETTINGS = ("model", "cheap_model", "identicon_subjects", "editor", "default_cwd", "ui.initial_view")


class Client:
    """One /ws connection. Frames go through a queue so events stay ordered and sends never block callers."""

    def __init__(self, websocket, kind="web"):
        self.ws = websocket
        self.kind = kind
        self.subs = set()
        self.branch_of = {}  # session_id -> branch_id currently viewed by this client/tab
        self.q = asyncio.Queue()
        self.closed = False
        self.task = asyncio.ensure_future(self._writer())

    def send(self, obj):
        if not self.closed:
            self.q.put_nowait(json.dumps(obj))

    def event(self, name, data):
        self.send({"type": "event", "event": name, "data": data})

    async def _writer(self):
        try:
            while True:
                frame = await self.q.get()
                if frame is None:
                    return
                await self.ws.send(frame)
        except Exception:
            self.closed = True

    def close(self):
        self.closed = True
        self.task.cancel()

    async def close_with(self, code, reason):
        """Flushes queued frames, then closes the websocket with code."""
        self.q.put_nowait(None)
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.shield(self.task), 2)
        self.closed = True
        await close_websocket(self.ws, code, reason)


async def close_websocket(websocket, code, reason=b""):
    """Sends a close frame with code + reason, then drops the connection shortly after."""
    with contextlib.suppress(Exception):
        await websocket.send(code.to_bytes(2, "big") + reason, opcode=8)
    await asyncio.sleep(0.5)
    with contextlib.suppress(Exception):
        websocket.response.close()


class Server:
    def __init__(self, html_path):
        self.html_path = html_path
        self.server_cwd = os.getcwd()
        self.models_cache = {}     # provider id -> (monotonic time, [model])
        self.models_failed = {}    # provider id -> monotonic time of the last failed background fetch
        self.effort_refused = set()  # (provider id, model) that answered a request with an effort level with a 400
        self.pricing_cache = {}    # provider id -> (monotonic time, price table or None)
        self._pricing_inflight = {}
        self.loop = None
        self.store = Store(html_path)
        self.creds = credsmod.Creds(self.store)
        self.agent = agentmod.Agent(self)
        self.clients = set()
        self.ptys = set()
        self.port = None
        self.pairing_until = 0.0
        self.store.offline = self.offline_snapshot
        self.moved = False         # the file runs somewhere new (fingerprint differs) and kept its token
        self._onboarding = None
        token = htmlfile.read_token(html_path)
        fp, stored_fp = common.fingerprint(html_path), self.meta("fingerprint")
        if not token or (stored_fp and stored_fp != fp and not self.has_history() and not self.has_provider()):
            # first start, or a first-time setup of a copy made elsewhere (e.g. a downloaded published file)
            token = self.new_token()
            self.pairing_until = time.monotonic() + PAIRING_SECONDS
        elif not stored_fp:
            self.set_meta("fingerprint", fp)   # files from before fingerprints: adopt the current place
        elif stored_fp != fp:
            self.moved = True                  # suggest regenerating (settings / banner), don't force it
        self.store.token = token
        self.store.save(force=True)

    @property
    def token(self):
        return self.store.token

    def new_token(self):
        token = secrets.token_urlsafe(24)
        self.store.token = token
        self.set_meta("token_generated_at", str(int(time.time() * 1000)))
        self.set_meta("fingerprint", common.fingerprint(self.html_path))
        self.moved = False
        return token

    def meta(self, key):
        row = self.store.query_one("SELECT value FROM meta WHERE key=?", (key,))
        return row[0] if row else None

    def set_meta(self, key, value):
        self.store.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))

    # --- onboarding signals (questions/034) ------------------------------------------------------

    def has_history(self):
        """H: a session exists. Sessions made with the test-only mock provider don't count outside test mode."""
        if llm.mock_enabled():
            return bool(self.store.query_one("SELECT 1 FROM sessions LIMIT 1"))
        return bool(self.store.query_one(
            "SELECT 1 FROM sessions s WHERE NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id "
            "AND m.provider = 'mock') LIMIT 1"))

    def has_provider(self):
        """P: a usable provider is configured (see llm.configured)."""
        return llm.configured(self.store.settings(), self.creds)

    def onboarding(self):
        return {"has_history": self.has_history(), "has_provider": self.has_provider(), "moved": self.moved}

    def onboarding_changed(self):
        """Pushes the `onboarding` event when H/P/moved changed since the last push."""
        state = self.onboarding()
        if state != self._onboarding:
            self._onboarding = state
            self.broadcast("onboarding", state)

    def require_provider(self):
        if not self.has_provider():
            raise llm.NoProviderError()

    def pairing_token(self):
        return self.token if time.monotonic() < self.pairing_until else None

    # --- events --------------------------------------------------------------------------------

    def broadcast(self, event, data):
        for c in list(self.clients):
            c.event(event, data)

    def emit(self, sid, event, data):
        for c in list(self.clients):
            if sid in c.subs:
                c.event(event, data)

    # --- auth helpers --------------------------------------------------------------------------

    def cors(self, request, response):
        origin = header(request, b"origin")
        response.set_header("Access-Control-Allow-Origin", origin if origin in ("null", "file://") else "null")
        for k, v in CORS_HEADERS.items():
            response.set_header(k, v)

    def path_ok(self, request):
        if not common.origin_ok(header(request, b"origin")):
            return False
        path = header(request, b"x-sfh-path") or (request.query.get("path") or [None])[0]
        return path == self.html_path

    def check(self, request):
        """None when the request may proceed, else the HTTP status to refuse it with (403 / 401)."""
        if not self.path_ok(request):
            return 403
        given = header(request, b"x-sfh-token") or (request.query.get("token") or [""])[0] or ""
        if not hmac.compare_digest(given.encode(), self.token.encode()):
            return 401
        return None

    async def regenerate_token(self):
        self.pairing_until = 0.0
        self.new_token()
        await agentmod.in_thread(self.store.save, True)
        self.onboarding_changed()

    def close_all_websockets(self):
        for c in list(self.clients):
            asyncio.ensure_future(c.close_with(TOKEN_CLOSE_CODE, b"token-changed"))
        for ws in list(self.ptys):
            asyncio.ensure_future(close_websocket(ws, TOKEN_CLOSE_CODE, b"token-changed"))

    # --- offline snapshot ----------------------------------------------------------------------

    def offline_snapshot(self):
        settings = self.store.settings()
        sessions = [dict(s, status="idle", active_status="idle") for s in self.agent.list()]
        branch_data = {}
        for s in sessions:
            bd = branches.all_branch_data(self.agent, s["id"])
            if bd:
                branch_data[s["id"]] = bd
        snap = {"saved_at": int(time.time() * 1000), "version": common.VERSION, "sessions": sessions,
                "has_history": self.has_history(),
                "messages": {s["id"]: self.agent.messages(s["id"]) for s in sessions},
                "settings": {k: settings.get(k) for k in OFFLINE_SETTINGS}}
        if branch_data:
            snap["branches"] = branch_data
        return json.dumps(snap).encode("utf-8")

    # --- RPC -----------------------------------------------------------------------------------

    def hello_result(self, params):
        settings = self.store.settings()
        try:
            provider = llm.resolve_provider(settings, self.creds)
        except llm.LLMError as e:
            provider = {"id": settings.get("provider"), "type": None, "model": None, "cheap_model": None,
                        "error": str(e)}
        return {"version": common.VERSION, "path": self.html_path, "port": self.port, "settings": settings,
                "sessions": self.agent.list(), "initial_view": settings.get("ui.initial_view"),
                "provider": llm.public(provider), "has_api_key": bool(llm.api_key(provider, self.creds)),
                "commands": commands.COMMANDS, "onboarding": self.onboarding(), "about": about.ABOUT}

    async def rpc(self, client, method, p):
        a = self.agent
        if method == "hello":
            client.kind = p.get("client") or client.kind
            return self.hello_result(p)
        if method == "sessions.list":
            return a.list()
        if method == "sessions.create":
            s = a.create(p.get("cwd"))
            self.onboarding_changed()
            return s
        if method == "sessions.set_cwd":
            return a.set_cwd(p["session_id"], p["cwd"])
        if method == "sessions.set_model":
            kw = {k: p[k] for k in ("model", "provider", "effort") if k in p}
            return a.set_model(p["session_id"], **kw)
        if method == "models.list":
            return await self.list_models(p.get("provider"), bool(p.get("refresh")), p.get("session_id"))
        if method == "cwd.info":
            return a.cwd_info()
        if method == "fs.list":
            return list_dir(p.get("path"), bool(p.get("hidden")), a.default_cwd())
        if method == "sessions.subscribe":
            sid = p["session_id"]
            bid = branches.resolve(self.store, sid, p["branch_id"]) if p.get("branch_id") else branches.active_id(self.store, sid)
            snap = a.snapshot(sid, branch_id=bid)
            client.subs.add(sid)
            client.branch_of[sid] = bid
            return snap
        if method == "sessions.unsubscribe":
            sid = p.get("session_id")
            client.subs.discard(sid)
            client.branch_of.pop(sid, None)
            return True
        if method == "sessions.rename":
            return a.rename(p["session_id"], p.get("title") or "")
        if method == "sessions.delete":
            await a.delete(p["session_id"])
            for c in self.clients:
                c.subs.discard(p["session_id"])
                c.branch_of.pop(p["session_id"], None)
            self.onboarding_changed()
            return True
        if method == "chat.send":
            sid, text = p["session_id"], str(p.get("text") or "")
            a.require(sid)
            if not text.strip():
                raise ValueError("empty message")
            bid = p.get("branch_id") or client.branch_of.get(sid) or a.active_branch(sid)
            if commands.is_command(text):
                await commands.handle(self, client, sid, text, branch_id=bid)
                return {"queued": False}
            self.require_provider()
            return {"queued": await a.send(sid, text, branch_id=bid)}
        if method == "chat.edit":
            self.require_provider()
            sid = p["session_id"]
            bid = p.get("branch_id") or client.branch_of.get(sid) or a.active_branch(sid)
            await a.edit(sid, int(p["message_id"]), str(p.get("text") or ""), branch_id=bid)
            return True
        if method == "peers.list":
            a.require(p["session_id"])
            return a.peers.peers_of(p["session_id"])
        if method == "peers.deliver":
            self.require_provider()
            return {"started": a.deliver_inbox(p["session_id"])}
        if method == "chat.stop":
            sid = p["session_id"]
            a.require(sid)
            bid = p.get("branch_id") or client.branch_of.get(sid) or a.active_branch(sid)
            await a.stop_branch(sid, bid)
            return True
        if method == "branches.list":
            sid = p["session_id"]
            a.require(sid)
            bid = p.get("branch_id") or client.branch_of.get(sid)
            return branches.tree_info(a, sid, active_bid=bid)
        if method == "branches.create":
            sid = p["session_id"]
            prompt = (p.get("prompt") or "").strip()
            if prompt:
                self.require_provider()
            from_mid = p.get("from_message_id")
            title = p.get("title") or (branches.fallback_branch_name(prompt) if prompt else "")
            pbid = p.get("parent_branch_id") or client.branch_of.get(sid)
            b = branches.create(a, sid, from_message_id=from_mid, title=title,
                                kind=p.get("kind") or "aside", switch=p.get("switch", True),
                                parent_branch_id=pbid, context_mode=p.get("context_mode") or "fork",
                                client=client)
            if prompt:
                await a.send(sid, prompt, branch_id=b["id"])
            return {"branch": b, "snapshot": a.snapshot(sid, branch_id=b["id"])}
        if method == "branches.switch":
            return branches.switch(a, p["session_id"], p["branch_id"], client=client)
        if method == "branches.rename":
            return branches.rename(a, p["session_id"], p["branch_id"], p.get("title") or "")
        if method == "branches.promote":
            return branches.promote(a, p["session_id"], p["branch_id"])
        if method == "branches.delete":
            return await branches.delete(a, p["session_id"], p["branch_id"])
        if method == "settings.get":
            return self.store.settings()
        if method == "settings.set":
            self.store.set_setting(p["key"], p.get("value"))
            s = self.store.settings()
            self.broadcast("settings.updated", s)
            if p["key"] in PROVIDER_KEYS:
                self.provider_changed()
            elif p["key"] == "model_overrides":  # context gauges change
                for x in self.agent.list():
                    self.broadcast("session.updated", x)
            return s
        if method == "creds.get":
            return self.creds.info()
        if method == "creds.set":
            self.creds.set(p.get("name"), p.get("value"))
            self.broadcast("settings.updated", self.store.settings())
            self.provider_changed()
            return self.creds.info()
        if method == "creds.storage":
            self.creds.set_storage(p.get("mode"))
            self.broadcast("settings.updated", self.store.settings())
            return self.creds.info()
        if method == "onboarding.get":
            return self.onboarding()
        if method == "providers.probe":    # model list of a provider that is not saved yet
            prov, creds = self.draft_provider(p)
            try:
                return {"models": await agentmod.in_thread(llm.list_models, prov, creds)}
            except llm.LLMError as e:
                return {"models": [], "error": str(e)}
        if method == "providers.test":     # a tiny completion with a provider that is not saved yet
            prov, creds = self.draft_provider(p)
            if not prov.get("model"):
                return {"ok": False, "error": "choose a model first"}
            usage = {}
            try:
                reply = await agentmod.in_thread(llm.complete, prov, creds, "Reply with the single word: OK",
                                                 prov["model"], 16, self.store.settings(), usage)
            except llm.LLMError as e:
                return {"ok": False, "error": str(e)}
            return {"ok": True, "reply": (reply or "").strip()[:200]}
        if method == "providers.setup":    # save a provider (+ its key in the credentials) and make it active
            return self.setup_provider(p)
        if method == "providers.list":
            return [{k: p.get(k) for k in ("id", "type", "base_url", "model", "cheap_model", "builtin", "has_key")}
                    for p in llm.providers(self.store.settings(), self.creds)]
        if method == "auth.get":
            row = self.store.query_one("SELECT value FROM meta WHERE key='token_generated_at'")
            return {"token": self.token, "generated_at": int(row[0]) if row else None}
        if method == "auth.regenerate":
            await self.regenerate_token()
            return True
        if method == "skills.list":
            s = a.require(p["session_id"])
            return skills.discover(s["cwd"], self.store.settings(), self.html_path)
        if method == "fs.resolve":
            # relative paths resolve against the given cwd (the cwd of the message the link is in)
            base = p.get("cwd") or a.require(p["session_id"])["cwd"]
            path = os.path.normpath(os.path.join(base, os.path.expanduser(str(p.get("path") or ""))))
            return {"path": path, "exists": os.path.exists(path), "is_dir": os.path.isdir(path)}
        raise ValueError("unknown method: %s" % method)

    def provider_changed(self):
        """The global provider/model may have changed: sessions that follow it show a new model."""
        self.models_cache.clear()
        self.models_failed.clear()
        self.pricing_cache.clear()
        for s in self.agent.list():
            self.broadcast("session.updated", s)
        self.onboarding_changed()

    @staticmethod
    def draft_provider(p):
        """(provider dict, creds) for an unsaved provider spec {type, base_url, model, token}."""
        t = p.get("type") or "openai"
        if t not in ("openai", "anthropic"):
            raise ValueError("type must be openai or anthropic")
        base = (p.get("base_url") or "").strip().rstrip("/") or None
        if t == "openai" and not base:
            raise ValueError("an OpenAI-compatible provider needs a base URL")
        prov = {"id": "draft", "type": t, "base_url": base, "model": (p.get("model") or "").strip() or None,
                "cheap_model": None, "api_key_credential": "DRAFT_KEY"}
        if t == "anthropic" and not prov["model"]:
            prov["model"] = llm.ANTHROPIC_MODEL
        token = (p.get("token") or "").strip()
        return prov, {"DRAFT_KEY": token} if token else {}

    def setup_provider(self, p):
        prov, creds = self.draft_provider(p)
        if not prov["model"]:
            raise ValueError("choose a model")
        pid = (p.get("id") or "").strip()
        if not pid:
            host = urllib.parse.urlparse(prov["base_url"] or "").hostname or prov["type"]
            pid = re.sub(r"[^A-Za-z0-9_.-]+", "-", host).strip("-") or "main"
            if pid in ("auto", "env", "mock"):
                pid = "main"
        if not re.match(r"^[A-Za-z0-9_.-]+$", pid) or pid in ("auto", "env", "mock"):
            raise ValueError("bad provider id %r" % pid)
        cred = "PROVIDER_" + re.sub(r"[^A-Z0-9]+", "_", pid.upper()) + "_KEY"
        if creds:
            self.creds.set(cred, creds["DRAFT_KEY"])
        entry = {"id": pid, "type": prov["type"], "base_url": prov["base_url"], "model": prov["model"],
                 "cheap_model": (p.get("cheap_model") or "").strip() or None, "api_key_credential": cred}
        others = [x for x in (self.store.setting("providers") or []) if isinstance(x, dict) and x.get("id") != pid]
        self.store.set_setting("providers", others + [entry])
        self.store.set_setting("provider", pid)
        self.store.set_setting("model", None)   # the provider's own model applies
        self.broadcast("settings.updated", self.store.settings())
        self.provider_changed()
        return {"id": pid, "onboarding": self.onboarding()}

    def context_window(self, provider_id, model):
        """(window, overridden): the model's context window size from the user's `model_overrides` setting,
        else from the provider's (cached) model list, else None. A missing list is fetched in the
        background; sessions using that provider are re-broadcast afterwards."""
        if not provider_id or not model:
            return None, False
        cw = llm.model_override(self.store.settings(), provider_id, model).get("context_window")
        if cw:
            return cw, True
        hit = self.models_cache.get(provider_id)
        if hit is None:
            self._prefetch_models(provider_id)
            return None, False
        # proxies may prefix ids ("anthropic/claude-x" vs "claude-x")
        return (llm.find_model(hit[1], model) or {}).get("context_window"), False

    def effort_info(self, provider_id, model):
        """{"efforts": [level, ...] | None, "adaptive": bool | None, "source"}: the effort levels a model takes,
        from the user's `model_overrides` setting ("override"), else the model refused one this run
        ("refused": []), else the provider's (cached) model list ("provider"); efforts None when unknown (an
        effort level is then tried, see llm.effort_rejected). adaptive: whether it supports adaptive thinking."""
        if not provider_id or not model:
            return {"efforts": None, "adaptive": None, "source": None}
        hit = self.models_cache.get(provider_id)
        if hit is None:
            self._prefetch_models(provider_id)
        entry = (llm.find_model(hit[1], model) or {}) if hit else {}
        adaptive = entry.get("adaptive_thinking")
        ov = llm.model_override(self.store.settings(), provider_id, model).get("efforts")
        if ov is not None:
            return {"efforts": ov, "adaptive": adaptive, "source": "override"}
        if (provider_id, model) in self.effort_refused:
            return {"efforts": [], "adaptive": adaptive, "source": "refused"}
        if entry.get("efforts") is not None:
            return {"efforts": entry["efforts"], "adaptive": adaptive, "source": "provider"}
        return {"efforts": None, "adaptive": adaptive, "source": None}

    def _prefetch_models(self, provider_id):
        loop = self.loop
        failed = self.models_failed.get(provider_id)
        if loop is None or provider_id in getattr(self, "_fetching", set()) or (
                failed and time.monotonic() - failed < MODELS_TTL):
            return
        self._fetching = getattr(self, "_fetching", set()) | {provider_id}

        async def fetch():
            try:
                r = await self.list_models(provider_id)
            except Exception:
                r = {"error": True}
            finally:
                self._fetching.discard(provider_id)
            if r.get("error"):
                self.models_failed[provider_id] = time.monotonic()
                return
            for s in self.agent.list():
                if s.get("provider") == provider_id:
                    self.broadcast("session.updated", s)
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(fetch()))

    async def pricing(self, provider):
        """The provider's price list from {origin}/settings/api/pricing (llm.fetch_pricing), or None.
        Cached per provider; concurrent callers share one fetch."""
        pid = provider["id"]
        hit = self.pricing_cache.get(pid)
        if hit and time.monotonic() - hit[0] < (PRICING_TTL if hit[1] else PRICING_RETRY):
            return hit[1]
        fut = self._pricing_inflight.get(pid)
        if fut is None:
            async def fetch():
                try:
                    table = await agentmod.in_thread(llm.fetch_pricing, provider, self.creds)
                except Exception:
                    table = None
                self.pricing_cache[pid] = (time.monotonic(), table)
                return table
            fut = self._pricing_inflight[pid] = asyncio.ensure_future(fetch())
            fut.add_done_callback(lambda f: self._pricing_inflight.pop(pid, None))
        return await asyncio.shield(fut)

    async def list_models(self, provider_id=None, refresh=False, session_id=None):
        """{"provider", "models": [{"id", "name"?}], "cached_at", "error"?} for a provider (default: the
        session's, else the global one). Cached for MODELS_TTL seconds per provider."""
        settings = self.store.settings()
        if provider_id:
            provider = llm.resolve_provider(settings, self.creds, provider_id)
        elif session_id:
            provider = self.agent.session_llm(session_id, settings)[0]
        else:
            provider = llm.resolve_provider(settings, self.creds)
        key = provider["id"]
        hit = self.models_cache.get(key)
        if hit and not refresh and time.monotonic() - hit[0] < MODELS_TTL:
            return {"provider": key, "models": self._with_overrides(key, hit[1], settings), "cached": True}
        try:
            models = await agentmod.in_thread(llm.list_models, provider, self.creds)
        except llm.LLMError as e:
            return {"provider": key, "models": [], "error": str(e)}
        self.models_cache[key] = (time.monotonic(), models)
        return {"provider": key, "models": self._with_overrides(key, models, settings), "cached": False}

    @staticmethod
    def _with_overrides(provider_id, models, settings):
        """Copies of the models with "context_window_override" / "efforts_override" where the user set one (the
        cache keeps the provider's own values)."""
        out = []
        for m in models:
            ov = llm.model_override(settings, provider_id, m["id"])
            if ov.get("context_window"):
                m = dict(m, context_window_override=ov["context_window"])
            if ov.get("efforts") is not None:
                m = dict(m, efforts_override=ov["efforts"])
            out.append(m)
        return out

    async def handle_ws(self, websocket):
        await websocket.accept()
        client = Client(websocket)
        self.clients.add(client)
        try:
            while True:
                raw = await websocket.receive(timeout=86400)
                try:
                    msg = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if msg.get("type") == "rpc":
                    asyncio.ensure_future(self._dispatch(client, msg))
        except Exception:
            pass
        finally:
            self.clients.discard(client)
            client.close()

    async def _dispatch(self, client, msg):
        rid = msg.get("id")
        try:
            result = await self.rpc(client, msg.get("method"), msg.get("params") or {})
            client.send({"type": "rpc_result", "id": rid, "result": result})
            if msg.get("method") == "auth.regenerate":
                self.close_all_websockets()
        except KeyError as e:
            client.send({"type": "rpc_error", "id": rid, "error": "missing parameter %s" % e})
        except Exception as e:
            client.send({"type": "rpc_error", "id": rid, "error": str(e) or type(e).__name__})

    # --- HTTP ----------------------------------------------------------------------------------

    def export_html(self, view):
        """(html text, warning or None). The copy gets an empty token; embedded credentials travel along."""
        overrides = {"ui.initial_view": view} if view else {}
        embedded = self.creds.storage() == "embedded"
        data = self.store.export_bytes(overrides, strip_credentials=not embedded)
        src = htmlfile.encode_data(htmlfile.read(self.html_path), data)
        src = htmlfile.encode_offline(src, self.offline_snapshot())
        src = store_set_token(src, "")
        return src, ("export contains embedded credentials" if embedded else None)

    def sql(self, body):
        cols, rows = self.store.run_sql(body.get("sql") or "", body.get("params") or [], bool(body.get("write")))
        if body.get("write"):
            self.broadcast("settings.updated", self.store.settings())
        return {"columns": cols, "rows": [[jsonable(v) for v in r] for r in rows]}


def list_dir(path, hidden=False, default=None):
    """Sub-directories of path, for the browser's folder picker. A file path lists its directory."""
    raw = str(path or "").strip() or default or os.path.expanduser("~")
    path = os.path.abspath(os.path.expanduser(raw))
    out = {"path": path, "parent": os.path.dirname(path) if path != "/" else None, "exists": True, "dirs": []}
    if not os.path.isdir(path):
        if os.path.exists(path):
            return list_dir(os.path.dirname(path), hidden, default)
        out["exists"] = False
        out["error"] = "no such directory"
        return out
    try:
        names = os.listdir(path)
    except OSError as e:
        out["error"] = e.strerror or str(e)
        return out
    dirs = []
    for n in names:
        if not hidden and n.startswith("."):
            continue
        try:
            if os.path.isdir(os.path.join(path, n)):
                dirs.append(n)
        except OSError:
            continue
    dirs.sort(key=lambda n: (n.lower(), n))
    out["truncated"] = len(dirs) > MAX_DIR_ENTRIES
    out["dirs"] = dirs[:MAX_DIR_ENTRIES]
    out["hidden_count"] = sum(1 for n in names if n.startswith(".")) if not hidden else 0
    return out


def header(request, name):
    v = request.headers.get(name)
    return v[0].decode("latin-1") if v else None


def jsonable(v):
    if isinstance(v, (bytes, bytearray, memoryview)):
        return base64.b64encode(bytes(v)).decode("ascii")
    return v


def build_app(srv):
    app = Application()

    def deny(response, status):
        response.set_status(status, b"Unauthorized" if status == 401 else b"Forbidden")
        response.set_content_type(b"text/plain")
        return b"bad or missing access token" if status == 401 else b"forbidden"

    def json_response(response, obj, status=200):
        if status != 200:
            response.set_status(status, b"Error")
        response.set_content_type(b"application/json")
        return json.dumps(obj)

    @app.route("/api/health")
    async def health(request, response, **_):
        srv.cors(request, response)
        if request.method == b"OPTIONS":
            response.set_status(204, b"No Content")
            return b""
        info = {"app": common.APP_ID, "path": srv.html_path, "pid": os.getpid(), "version": common.VERSION,
                "port": srv.port}
        pairing = srv.pairing_token()
        if pairing and srv.path_ok(request) and (request.query.get("path") or [None])[0] == srv.html_path:
            info["pairing_token"] = pairing
        return json_response(response, info)

    @app.route("/api/export")
    async def export(request, response, **_):
        srv.cors(request, response)
        if request.method == b"OPTIONS":
            response.set_status(204, b"No Content")
            return b""
        refused = srv.check(request)
        if refused:
            return deny(response, refused)
        view = (request.query.get("view") or [None])[0]
        text, warning = await agentmod.in_thread(srv.export_html, view)
        name = os.path.splitext(os.path.basename(srv.html_path))[0] + "-export.html"
        if warning:
            response.set_header("X-SFH-Warning", warning)
        response.set_content_type(b"text/html; charset=utf-8")
        response.set_header("Content-Disposition", 'attachment; filename="%s"' % name)
        return text.encode("utf-8")

    @app.route("/api/sql")
    async def sql(request, response, **_):
        srv.cors(request, response)
        if request.method == b"OPTIONS":
            response.set_status(204, b"No Content")
            return b""
        refused = srv.check(request)
        if refused:
            return deny(response, refused)
        if request.method != b"POST":
            return json_response(response, {"error": "POST required"}, 405)
        try:
            body = json.loads(bytes(await request.body()) or b"{}")
            return json_response(response, srv.sql(body))
        except Exception as e:
            return json_response(response, {"error": str(e)}, 400)

    @app.route("/ws")
    async def ws(request, response, websocket=None, **_):
        refused = srv.check(request)
        if refused:
            return deny(response, refused)
        if websocket is None:
            response.set_status(400, b"Bad Request")
            return b"websocket required"
        await srv.handle_ws(websocket)

    @app.route("/pty")
    async def pty(request, response, websocket=None, **_):
        refused = srv.check(request)
        if refused:
            return deny(response, refused)
        if websocket is None:
            response.set_status(400, b"Bad Request")
            return b"websocket required"
        q = {k: v[0] for k, v in request.query.items()}
        try:
            argv = pty_ws.build_argv(q.get("cmd", "shell"), srv.store.setting("editor"), q.get("file"),
                                     q.get("line") if (q.get("line") or "").isdigit() else None)
        except ValueError as e:
            response.set_status(400, b"Bad Request")
            return str(e)
        except pty_ws.EditorNotFound as e:  # tell the in-page terminal why, rather than a bare close
            await websocket.accept()
            with contextlib.suppress(Exception):
                await websocket.send(json.dumps({"type": "output", "data": "%s\r\n" % e}))
                await websocket.send(json.dumps({"type": "exit", "code": 127}))
                await websocket.close()
            return
        await websocket.accept()
        srv.ptys.add(websocket)
        try:
            await pty_ws.bridge(websocket, argv, q.get("cwd"), int(q.get("cols") or 80), int(q.get("rows") or 24))
        except Exception:
            pass
        finally:
            srv.ptys.discard(websocket)
        with contextlib.suppress(Exception):
            await websocket.close()

    return app


class _Discard:
    """Swallows tremolo's stdout chatter ('worker (pid N) is started at …') while serving."""
    line_buffering = True

    def write(self, s):
        return len(s)

    def flush(self):
        pass


def bind_port(path):
    for port in common.candidate_ports(path):
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
        except OSError:
            sock.close()
            continue
        sock.setblocking(False)
        return sock, port
    return None, None


def serve(html_path, args):
    html_path = common.html_path(html_path)
    running = common.find_server(html_path)
    if running:
        print("The server for %s is already running on http://127.0.0.1:%d" % (html_path, running))
        sys.exit(1)
    sock, port = bind_port(html_path)
    if sock is None:
        print("sfh: none of the ports %s is free" % common.candidate_ports(html_path), file=sys.stderr)
        sys.exit(1)
    srv = Server(html_path)
    srv.port = port
    app = build_app(srv)
    settings = srv.store.settings()
    print("Single-file LLM harness %s" % common.VERSION)
    print("  open:        file://%s" % html_path)
    print("  port:        %d" % port)
    if srv.has_provider():
        p = llm.resolve_provider(settings, srv.creds)
        print("  provider:    %s (%s, model %s)" % (p["id"], p["type"], p["model"]))
    else:
        print("  provider:    none configured. Open the page above to set one up, or set $AI_ENDPOINT and")
        print("               $AI_MODEL (+ $AI_TOKEN) and restart. Until then nothing can be sent to an LLM.")
    if srv.pairing_until:
        print("  token:       generated a new access token (reload open pages within 5 minutes to pair)")
    elif srv.moved:
        print("  token:       this file now runs for a different host/user/path than its access token was made")
        print("               for; consider `sh %s token regenerate` (or Settings → Access token)." % html_path)
    print("  credentials: %s" % (credsmod.file_path() if srv.creds.storage() == "xdg"
                                 else "embedded in the html file (setting credentials_storage)"))
    print("Press Ctrl-C to stop.")
    sys.stdout.flush()

    async def amain():
        loop = asyncio.get_running_loop()
        srv.loop = loop
        task = asyncio.current_task()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)
        await app.serve("127.0.0.1", port, sock=sock, log_level="WARNING", server_name="SFH",
                        shutdown_timeout=float(os.environ.get("SFH_SHUTDOWN_TIMEOUT", 2)),  # tests shorten it
                        client_max_body_size=64 * 1048576,
                        ws_max_payload_size=64 * 1048576, download_rate=1 << 30, upload_rate=1 << 30)

    real_stdout = sys.stdout
    sys.stdout = _Discard()
    try:
        with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
            asyncio.run(amain())
    finally:
        sys.stdout = real_stdout
        try:
            srv.store.save()
        except Exception as e:
            print("sfh: final save failed: %s" % e, file=sys.stderr)
        print("Stopped; data saved into %s" % html_path)
