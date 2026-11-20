"""Sessions and their turn loops: LLM streaming, tool execution, queueing, stop, edit, titles, /btw."""
import asyncio
import collections
import json
import os
import re
import threading

from . import links, llm, peers, signals, skills, subagents, tools
from .store import now_ms

MAX_ITERATIONS = 300   # unattended LLM calls (tool loop iterations) per user message
MAX_RECENT_CWDS = 30
MSG_COLS = ("id, session_id, role, content, created_at, parent_id, model, provider, cwd, links, usage, effort, "
            "started_at")

STOPWORDS = set("""a about above after again against all also am an and any are as at be because been before
being below between both but by can could did do does doing down during each few for from further had has
have having he her here hers him his how i if in into is it its itself just me more most my no nor not now
of off on once only or other our ours out over own please same she should so some such than that the their
theirs them then there these they this those through to too under until up very was we were what when where
which while who whom why will with would you your yours hi hello hey thanks thank let lets get got make want
need like know think tell show give use using one two new way thing things something maybe really""".split())


def keywords(text, n=3):
    counts = collections.Counter()
    order = {}
    for i, w in enumerate(re.findall(r"[A-Za-z][A-Za-z0-9_+#.-]*[A-Za-z0-9+#]|[A-Za-z]{3,}", text)):
        w = w.lower()
        if len(w) < 3 or w in STOPWORDS:
            continue
        counts[w] += 1
        order.setdefault(w, i)
    return sorted(counts, key=lambda w: (-counts[w], order[w]))[:n]


def fallback_title(text, n_subjects=3):
    words = re.sub(r"\[\[.*?\]\]", " ", text).split()
    title = " ".join(words[:6]) or "New session"
    if len(title) > 80:
        title = title[:77] + "..."
    return title, keywords(text, n_subjects)


TITLE_RULES = (
    "The title is as brief as possible (at most 8 words, no quotes, no trailing period), most specific thing "
    "first, and never starts with a verb: name the project/thing first and put the action after it. Examples:\n"
    '- not "Apply and merge foo-project PR 231" but "foo-project PR 231 apply and merge"\n'
    '- not "Fix login redirect loop in acme-web" but "acme-web login redirect loop fix"\n'
    '- not "Explain how Python asyncio cancellation works" but "Python asyncio cancellation explained"')


def subjects_prompt(n, text, cwd, context, sig=None):
    """The title + identicon subjects prompt (every title generation uses it: first message, end of the
    first exchange, /retitle): a domain > topic > specific ontology, informed by what this session
    actually touched (sig, see signals.extract) and by recent sessions, so work on the same project keeps
    the same domain/topic (and so similar identicons) even when unrelated sessions share a cwd."""
    tiers = ('3 strings: [domain, topic, specific] - domain = the broad field (e.g. "software development", '
             '"home cooking"), topic = the project or area within it (e.g. a project name), specific = what this '
             'session is about' if n == 3 else
             "%d strings ordered from the broad domain, through narrower topics, to the most specific thing" % n)
    recent = "\n".join("- %s | %s | %s | %s" % (
        c["title"] or "(untitled)", c["cwd"] or "?",
        ", ".join(signals.short(p, cwd) for p in (c.get("signals") or {}).get("projects", [])[:3]) or "-",
        " > ".join(c["subjects"])) for c in context) or "(none)"
    about = signals.describe(sig, cwd) or "(nothing beyond the conversation itself)"
    return ("Summarize this conversation for a session list. Reply with ONLY a JSON object: "
            '{"title": "...", "subjects": [...]}.\n\n%s\n\n'
            "The subjects are short lowercase phrases: %s.\n\n"
            "What this session is actually about - strong signals, from the projects/folders and files its user "
            "messages and tool calls name or touch (paths relative to its working directory):\n%s\n"
            "This session's starting working directory - a WEAK signal, many unrelated sessions start in the same "
            "root folder: %s\n\n"
            "The subjects pick the session's identicon, so sessions about the same thing must share wording. Recent "
            "sessions (title | working directory | projects/folders they touched | subjects), those sharing a "
            "project with this one first:\n%s\n\n"
            "Decide by the actual subject (the project, subfolder or repo worked on, the title), never by the working "
            "directory alone. If this conversation is in the same field as a recent session, reuse that domain "
            "EXACTLY; if it works on the same project/subfolder as that session, also reuse its topic EXACTLY. A "
            "recent session in the same working directory that worked on a different subfolder or project is NOT "
            "the same topic: give this session its own. Only invent new wording when nothing fits.\n\n"
            "<conversation>\n%s\n</conversation>" % (TITLE_RULES, tiers, about, cwd or "?", recent, text))


def message_dict(row, versions=None):
    mid, sid, role, content, created, parent, model, provider, cwd, links_json, usage, effort, started = row
    m = {"id": mid, "session_id": sid, "role": role, "content": json.loads(content), "created_at": created,
         "parent_id": parent, "model": model, "provider": provider, "cwd": cwd}
    if effort:
        m["effort"] = effort
    if started:
        m["started_at"] = started
    if usage:
        m["usage"] = json.loads(usage)
    if links_json is not None:  # absent for messages stored before links were resolved at write time
        try:
            m["links"] = json.loads(links_json)
        except ValueError:
            pass
    if role == "user":
        m["versions"] = versions or 1
    return m


def is_prompt(content):
    """A real user prompt (not a tool-output message, not a compaction summary)."""
    return (any(b.get("type") != "tool_result" for b in content) and not is_compaction(content)
            and not any(peers.is_peer_block(b) for b in content))


def is_compaction(content):
    """The marker /compact stores: earlier messages are replaced by its summary for the model."""
    return any(b.get("compaction") for b in content or [])


COMPACT_PROMPT = """Your task now is to compact this conversation: write a summary that will REPLACE the whole conversation above as your context from here on (the messages themselves will no longer be visible to you). Write it so that you can continue the work seamlessly. Include:

1. The user's goals and requests, with any constraints and preferences they stated (quote exact wording when it matters).
2. What has been done: decisions made, files read/created/changed (with paths) and why, commands run and their important results, errors hit and how they were resolved.
3. Important facts learned about the code/environment (names, paths, APIs, conventions).
4. The current state, open questions, and the exact next steps that were planned or in progress.

Be specific and complete but concise; prefer lists. Reply with the summary only."""

SUMMARY_HEADER = ("<conversation-summary>\nThe earlier part of this conversation was compacted (by /compact) into "
                  "the summary below; the original messages are no longer available to you.\n\n%s\n"
                  "</conversation-summary>")


class Runtime:
    """In-memory state of one session."""

    def __init__(self):
        self.task = None
        self.queue = []
        self.draft = None
        self.stream = None
        self.cancel = None       # asyncio.Event for the running turn
        self.tool_ctx = None
        self.btw_tasks = set()
        # peer sessions (peers.py), in memory only
        self.activity = None     # {"tool", "summary", "at"} of the last tool call
        self.edited = {}         # abs path -> ms of this session's last edit/write
        self.seen = {}           # abs path -> mtime_ns after this session's last read/edit/write
        self.inbox = []          # undelivered peer messages
        self.peer_sig = None     # the peer ids the model was last told about
        self.wakes = []          # ms of turns started by peer questions
        self.prompted = False    # the current run had a real prompt (not only a peer wake)
        self.tools = {}          # tool_use id -> {"started_at", "ended_at"?} of the current step's tool runs


class Agent:
    def __init__(self, server):
        self.server = server
        self.store = server.store
        self.rt = collections.defaultdict(Runtime)
        self.bg = set()
        self.peers = peers.Hub(self)

    # --- sessions ------------------------------------------------------------------------------

    def settings(self):
        return self.store.settings()

    def provider(self):
        return llm.resolve_provider(self.settings(), self.server.creds)

    def default_model(self):
        try:
            return self.provider()["model"]
        except llm.LLMError:
            return None

    def _global(self, settings=None):
        try:
            return llm.resolve_provider(settings or self.settings(), self.server.creds)
        except llm.LLMError as e:
            return e

    def session_llm(self, sid, settings=None, glob=None):
        """(provider, model) a session uses now: its own provider/model choice, else the global ones.
        Raises LLMError when the chosen provider does not exist."""
        settings = settings or self.settings()
        row = self.store.query_one("SELECT provider, model FROM sessions WHERE id=?", (sid,))
        pid, model = row or (None, None)
        if pid:
            provider = llm.resolve_provider(settings, self.server.creds, pid)
        else:
            provider = glob if glob is not None else self._global(settings)
            if isinstance(provider, Exception):
                raise provider
        if provider["type"] == "mock":
            return provider, "mock"
        return provider, model or provider["model"]

    def session_effort(self, sid, provider_id, model, settings=None):
        """(wanted, effective, info): the effort level the session asks for (its own choice, else the `effort`
        setting; None = none), the one sent to its model (llm.clamp_effort against what the model supports,
        see server.effort_info) and that info."""
        settings = settings or self.settings()
        row = self.store.query_one("SELECT effort FROM sessions WHERE id=?", (sid,))
        wanted = (row[0] if row else None) or settings.get("effort") or None
        info = self.server.effort_info(provider_id, model)
        return wanted, llm.clamp_effort(wanted, info["efforts"]), info

    def summary(self, sid, glob=None):
        row = self.store.query_one("SELECT id, title, cwd, created_at, updated_at, subjects, model, provider, effort "
                                   "FROM sessions WHERE id=?", (sid,))
        if not row:
            return None
        count = len(self.thread_ids(sid))
        rt = self.rt.get(sid)
        try:
            provider, model = self.session_llm(sid, glob=glob)
            pid, perr = provider["id"], None
        except llm.LLMError as e:
            pid, model, perr = row[7], row[6], str(e)
        out = dict({"id": row[0], "title": row[1] or "", "cwd": row[2], "created_at": row[3], "updated_at": row[4],
                    "subjects": json.loads(row[5] or "[]"), "model": model, "provider": pid,
                    "model_override": row[6], "provider_override": row[7],
                    "status": "running" if rt and rt.task and not rt.task.done() else "idle",
                    "message_count": count}, **subagents.info(self.store, sid))
        out.update(self.peers.summary_fields(sid, row[2]))
        if perr:
            out["provider_error"] = perr
        out["usage"] = self.usage(sid)
        if not perr:
            out["context"] = self.context_info(sid, pid, model)
            wanted, eff, info = self.session_effort(sid, pid, model)
            out.update({"effort": eff, "effort_wanted": wanted, "effort_override": row[8],
                        "efforts": info["efforts"] if info["efforts"] is not None else list(llm.DEFAULT_EFFORTS),
                        "efforts_known": info["efforts"] is not None})
        kids = [r[0] for r in self.store.query("SELECT usage FROM sessions WHERE parent_id=? AND usage IS NOT NULL",
                                               (sid,))]
        if kids:
            total = None
            for k in kids:
                total = merge_usage(total, json.loads(k))
            out["usage_subagents"] = total
        return out

    # --- usage / cost --------------------------------------------------------------------------

    def usage(self, sid):
        row = self.store.query_one("SELECT usage FROM sessions WHERE id=?", (sid,))
        return json.loads(row[0]) if row and row[0] else None

    async def price(self, provider, model, u):
        """Fills in u["cost"] (and u["cost_estimated"]) from the provider's price list when the provider
        didn't report a cost itself. Returns u."""
        if u and u.get("cost") is None and model:
            try:
                p = llm.find_price(await self.server.pricing(provider), model)
            except Exception:
                p = None
            if p:
                u["cost"] = round(llm.usage_cost(u, p), 8)
                u["cost_estimated"] = True
        return u

    def add_usage(self, sid, u):
        """Adds one provider response (turn step, title, /btw) to the session's running total."""
        with self.store.lock:
            total = llm.add_usage(self.usage(sid), u)
            self.store.execute("UPDATE sessions SET usage=? WHERE id=?", (json.dumps(total), sid))
        self.updated(sid)
        parent = self.store.query_one("SELECT parent_id FROM sessions WHERE id=?", (sid,))
        if parent and parent[0]:
            self.updated(parent[0])

    def list(self):
        ids = [r[0] for r in self.store.query("SELECT id FROM sessions ORDER BY updated_at DESC")]
        glob = self._global()
        return [self.summary(i, glob) for i in ids]

    def require(self, sid):
        s = self.summary(sid)
        if not s:
            raise ValueError("no such session: %s" % sid)
        return s

    # --- working directories -------------------------------------------------------------------

    def default_cwd(self):
        """Working directory offered for a new session: the one the last new session was started in,
        else the default_cwd setting, else the directory the server runs in."""
        row = self.store.query_one("SELECT value FROM meta WHERE key='last_new_session_cwd'")
        for c in ((row[0] if row else None), self.settings().get("default_cwd"), self.server.server_cwd):
            if c and os.path.isdir(os.path.expanduser(c)):
                return os.path.abspath(os.path.expanduser(c))
        return os.path.dirname(self.server.html_path)

    def recent_cwds(self, limit=MAX_RECENT_CWDS):
        rows = self.store.query("SELECT path, used_at FROM recent_cwds ORDER BY used_at DESC LIMIT ?", (limit,))
        return [{"path": p, "used_at": t, "exists": os.path.isdir(p)} for p, t in rows]

    def cwd_info(self):
        return {"default": self.default_cwd(), "server_cwd": self.server.server_cwd,
                "home": os.path.expanduser("~"), "recent": self.recent_cwds()}

    def use_cwd(self, cwd):
        with self.store.lock:
            self.store.execute("INSERT OR REPLACE INTO recent_cwds(path, used_at) VALUES(?, ?)", (cwd, now_ms()))
            self.store.execute("DELETE FROM recent_cwds WHERE path NOT IN "
                               "(SELECT path FROM recent_cwds ORDER BY used_at DESC LIMIT ?)", (MAX_RECENT_CWDS * 2,))

    @staticmethod
    def check_dir(cwd, base=None):
        cwd = os.path.expanduser(str(cwd).strip())
        cwd = os.path.abspath(os.path.join(base, cwd) if base else cwd)
        if not os.path.isdir(cwd):
            raise ValueError("not a directory: %s" % cwd)
        return cwd

    def create(self, cwd=None, remember=True):
        """remember: a session the user started (not a subagent): its cwd becomes the next default."""
        cwd = self.check_dir(cwd or self.default_cwd())
        sid = "s_" + os.urandom(4).hex()
        t = now_ms()
        self.store.execute("INSERT INTO sessions(id, title, cwd, created_at, updated_at, subjects, model) "
                           "VALUES(?, '', ?, ?, ?, '[]', NULL)", (sid, cwd, t, t))
        if remember:
            self.store.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('last_new_session_cwd', ?)", (cwd,))
            self.use_cwd(cwd)
        return self.updated(sid)

    def set_cwd(self, sid, cwd):
        """Changes the session's working directory (relative paths: against the current one). Messages keep
        the cwd they were written in; the change applies from the next LLM call / tool run."""
        s = self.require(sid)
        cwd = self.check_dir(cwd, s["cwd"])
        if cwd == s["cwd"]:
            return s
        self.store.execute("UPDATE sessions SET cwd=? WHERE id=?", (cwd, sid))
        self.use_cwd(cwd)
        self.server.emit(sid, "notice", {"session_id": sid, "level": "info",
                                         "text": "Working directory changed to `%s`." % cwd})
        return self.updated(sid)

    def updated(self, sid):
        s = self.summary(sid)
        if s:
            self.server.broadcast("session.updated", s)
        return s

    def touch(self, sid):
        self.store.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now_ms(), sid))

    def rename(self, sid, title):
        self.require(sid)
        self.store.execute("UPDATE sessions SET title=? WHERE id=?", (str(title)[:200], sid))
        return self.updated(sid)

    _UNSET = object()

    def set_model(self, sid, model=_UNSET, provider=_UNSET, effort=_UNSET):
        """Sets the session's model, provider and/or effort level (None = follow the global setting). Changing
        the provider without giving a model resets the model to that provider's default. Takes effect at the
        next LLM call, also in the middle of a running turn."""
        self.require(sid)
        settings = self.settings()
        if effort is not self._UNSET:
            effort = (str(effort).strip().lower() or None) if effort else None
            if effort in ("default", "auto"):
                effort = None
            if effort and effort not in llm.EFFORT_LEVELS:
                raise ValueError("unknown effort level %r (one of: %s)" % (effort, ", ".join(llm.EFFORT_LEVELS)))
            self.store.execute("UPDATE sessions SET effort=? WHERE id=?", (effort, sid))
            if model is self._UNSET and provider is self._UNSET:
                return self.updated(sid)
        with self.store.lock:
            row = self.store.query_one("SELECT provider, model FROM sessions WHERE id=?", (sid,))
            pid, cur_model = row
            if provider is not self._UNSET:
                provider = provider or None
                if provider:
                    llm.resolve_provider(settings, self.server.creds, provider)  # raises for unknown ids
                if provider != pid and model is self._UNSET:
                    model = None
                pid = provider
            if model is not self._UNSET:
                cur_model = (str(model).strip() or None) if model else None
            self.store.execute("UPDATE sessions SET provider=?, model=? WHERE id=?", (pid, cur_model, sid))
        return self.updated(sid)

    async def delete(self, sid):
        self.require(sid)
        await self.stop(sid)
        with self.store.lock:
            self.store.execute("DELETE FROM messages WHERE session_id=?", (sid,))
            self.store.execute("DELETE FROM sessions WHERE id=?", (sid,))
        self.rt.pop(sid, None)
        self.peers.forget(sid)
        self.server.broadcast("session.deleted", {"session_id": sid})

    def head(self, sid):
        row = self.store.query_one("SELECT head_id FROM sessions WHERE id=?", (sid,))
        return row[0] if row else None

    def thread_ids(self, sid):
        """Ids of the displayed branch, oldest first (walk from head_id via parent_id)."""
        with self.store.lock:
            parents = dict(self.store.query("SELECT id, parent_id FROM messages WHERE session_id=?", (sid,)))
            cur = self.head(sid)
        out = []
        while cur is not None and cur in parents and len(out) <= len(parents):
            out.append(cur)
            cur = parents[cur]
        out.reverse()
        return out

    def versions(self, sid, parent_id):
        """Number of user prompts sharing parent_id (edited versions, incl. the message itself)."""
        rows = self.store.query("SELECT content FROM messages WHERE session_id=? AND role='user' AND parent_id IS ?",
                                (sid, parent_id))
        return sum(1 for r in rows if is_prompt(json.loads(r[0])))

    def messages(self, sid):
        """The displayed branch of the session."""
        ids = self.thread_ids(sid)
        if not ids:
            return []
        rows = {r[0]: r for r in self.store.query(
            "SELECT %s FROM messages WHERE session_id=?" % MSG_COLS, (sid,))}
        counts = collections.Counter(
            (r[5]) for r in rows.values() if r[2] == "user" and is_prompt(json.loads(r[3])))
        return [message_dict(rows[i], counts.get(rows[i][5])) for i in ids]

    def add_message(self, sid, role, content, model=None, provider=None, usage=None, effort=None, started_at=None):
        """Stores a message on the displayed branch, recording the model/provider it was made with (default:
        what the session uses now), the session's cwd and the local file links in it that exist now. effort:
        the effort level its request was sent with; started_at: when that request started (ms)."""
        t = now_ms()
        if model is None and provider is None:
            try:
                p, model = self.session_llm(sid)
                provider = p["id"]
            except llm.LLMError:
                pass
        cwd = self.store.query_one("SELECT cwd FROM sessions WHERE id=?", (sid,))[0]
        found = links.for_content(content, cwd)
        with self.store.lock:
            parent = self.head(sid)
            mid = self.store.execute(
                "INSERT INTO messages(session_id, role, content, created_at, parent_id, model, provider, cwd, links, "
                "usage, effort, started_at) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, role, json.dumps(content), t, parent, model, provider, cwd, json.dumps(found),
                 json.dumps(usage) if usage else None, effort, started_at))
            self.store.execute("UPDATE sessions SET head_id=? WHERE id=?", (mid, sid))
            self.touch(sid)
        msg = {"id": mid, "session_id": sid, "role": role, "content": content, "created_at": t, "parent_id": parent,
               "model": model, "provider": provider, "cwd": cwd, "links": found}
        if usage:
            msg["usage"] = usage
        if effort:
            msg["effort"] = effort
        if started_at:
            msg["started_at"] = started_at
        if role == "user":
            msg["versions"] = self.versions(sid, parent) if is_prompt(content) else 1
        self.server.emit(sid, "message.added", msg)
        return msg

    def llm_history(self, sid, msgs=None):
        """What the model sees: the displayed branch from the last compaction on, the summary first."""
        msgs = self.messages(sid) if msgs is None else msgs
        cut = max((i for i, m in enumerate(msgs) if m["role"] == "user" and is_compaction(m["content"])), default=None)
        if cut is None:
            return msgs
        summary = "\n".join(b.get("text", "") for b in msgs[cut]["content"] if b.get("type") == "text")
        head = dict(msgs[cut], content=[{"type": "text", "text": SUMMARY_HEADER % summary}])
        return [head] + msgs[cut + 1:]

    def context_info(self, sid, provider_id, model, msgs=None):
        """{"used", "window", "estimated"}: tokens the conversation occupies in the model's context now
        (the last response's input + output since the last compaction; estimated from text length right
        after a compaction or when no response reported usage) and the model's context window size (the user's `model_overrides` setting, else the provider's model
        list; "window_override": true when it comes from the setting)."""
        msgs = self.messages(sid) if msgs is None else msgs
        used, estimated = None, False
        for m in reversed(msgs):
            if m["role"] == "user" and is_compaction(m["content"]):
                text = "".join(b.get("text", "") for b in m["content"])
                # system prompt + tool definitions: about what the first response of the session took as input
                first = next((x["usage"] for x in msgs if x.get("usage")), None) or {}
                base = (first.get("input_tokens", 0) + first.get("cache_read_tokens", 0)
                        + first.get("cache_write_tokens", 0))
                used = len(text) // 4 + base
                estimated = True
                break
            u = m.get("usage")
            if u:
                used = (u.get("input_tokens", 0) + u.get("cache_read_tokens", 0) + u.get("cache_write_tokens", 0)
                        + u.get("output_tokens", 0))
                break
        if used is None and msgs:
            # no response with usage yet (e.g. messages recorded before usage was tracked): estimate from the
            # text the model would get, ~4 characters per token
            used = sum(len(json.dumps(m["content"])) for m in self.llm_history(sid, msgs)) // 4
            estimated = True
        window, overridden = self.server.context_window(provider_id, model)
        out = {"used": used, "window": window, "estimated": estimated}
        if overridden:
            out["window_override"] = True
        return out

    def snapshot(self, sid):
        s = self.require(sid)
        rt = self.rt.get(sid)
        return {"session": s, "messages": self.messages(sid),
                "draft": rt.draft if rt else None, "queue": list(rt.queue) if rt else [],
                "tools": dict(rt.tools) if rt else {}, "now": now_ms()}

    def system_prompt(self, sid):
        s = self.require(sid)
        return skills.system_prompt(self.settings(), s["cwd"], self.server.html_path)

    # --- chat ----------------------------------------------------------------------------------

    def running(self, sid):
        rt = self.rt.get(sid)
        return bool(rt and rt.task and not rt.task.done())

    async def send(self, sid, text):
        self.require(sid)
        rt = self.rt[sid]
        if self.running(sid):
            rt.queue.append(text)
            self.server.emit(sid, "queue.updated", {"session_id": sid, "queue": list(rt.queue)})
            return True
        rt.task = asyncio.ensure_future(self._run(sid, text))
        return False

    def wake(self, sid):
        """Starts a turn without a prompt, to handle the session's inbox (peer questions)."""
        if self.running(sid) or not self.summary(sid):
            return False
        self.rt[sid].task = asyncio.ensure_future(self._run(sid, None))
        return True

    def deliver_inbox(self, sid):
        """peers.deliver: the user lets an idle session handle its held peer messages now."""
        self.require(sid)
        rt = self.rt[sid]
        if not rt.inbox:
            return False
        if self.running(sid):
            return True  # delivered at its next step anyway
        return self.wake(sid)

    def store_peer_blocks(self, sid):
        """Stores the pending peer blocks (peer-sessions note + inbox) as a user message; True if any."""
        blocks = self.peers.pending_blocks(sid)
        if not blocks:
            return False
        self.add_message(sid, "user", blocks)
        self.updated(sid)
        return True

    async def stop(self, sid):
        rt = self.rt.get(sid)
        if not rt:
            return
        if rt.queue:
            rt.queue.clear()
            self.server.emit(sid, "queue.updated", {"session_id": sid, "queue": []})
        if rt.cancel:
            rt.cancel.set()
        if rt.stream:
            stream = rt.stream
            in_thread(stream.close)
        if rt.tool_ctx:
            rt.tool_ctx.cancel()
        if rt.task and not rt.task.done():
            try:
                await asyncio.wait_for(asyncio.shield(rt.task), 10)
            except Exception:
                pass

    async def edit(self, sid, message_id, text):
        self.require(sid)
        await self.stop(sid)
        row = self.store.query_one("SELECT role, parent_id, content FROM messages WHERE id=? AND session_id=?",
                                   (message_id, sid))
        if not row or row[0] != "user" or not is_prompt(json.loads(row[2])):
            raise ValueError("no such user message")
        if message_id not in self.thread_ids(sid):
            raise ValueError("message is not on the displayed branch")
        # the old branch stays in the db; the new prompt becomes a sibling of the edited one
        self.store.execute("UPDATE sessions SET head_id=? WHERE id=?", (row[1], sid))
        self.server.emit(sid, "messages.truncated", {"session_id": sid, "from_id": message_id})
        await self.send(sid, text)

    # --- /compact ------------------------------------------------------------------------------

    async def compact(self, sid, instructions=""):
        """Summarizes the conversation with the session's model; the summary replaces the earlier messages
        for the model from then on (they stay displayed). Runs as the session's task, so the session shows
        as running, messages sent meanwhile queue, and chat.stop cancels it."""
        self.require(sid)
        if self.running(sid):
            raise ValueError("the session is busy: wait for the turn to finish (or stop it), then /compact")
        if not any(m["role"] == "assistant" for m in self.llm_history(sid)[1:]):
            raise ValueError("nothing to compact yet")
        rt = self.rt[sid]
        rt.task = asyncio.ensure_future(self._compact(sid, rt, instructions))
        self.updated(sid)

    async def _compact(self, sid, rt, instructions):
        rt.cancel = asyncio.Event()
        loop = asyncio.get_running_loop()
        notice = lambda text, level="info": self.server.emit(sid, "notice", {"session_id": sid, "level": level,
                                                                             "text": text})
        notice("Compacting the conversation…")
        try:
            settings = self.settings()
            session = self.require(sid)
            provider, model = self.session_llm(sid, settings)
            history = self.llm_history(sid)
            prompt = COMPACT_PROMPT + ("\n\nAdditional instructions from the user: " + instructions
                                       if instructions else "")
            system = skills.system_prompt(settings, session["cwd"], self.server.html_path)
            system += subagents.system_note(self.store, sid)
            stream = llm.retrying_stream(settings, provider, self.server.creds, model, system,
                                         history + [{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                                         None, settings["max_tokens"], 0)
            asyncio.ensure_future(self.server.pricing(provider))  # in parallel with the response
            content, _, error, usage = await self._stream(sid, rt, stream, loop)
            self.server.emit(sid, "draft.end", {"session_id": sid, "turn_id": rt.draft["turn_id"]})
            rt.draft = None
            if usage:
                self.add_usage(sid, await self.price(provider, model, usage))
            if rt.cancel.is_set():
                return notice("Compaction stopped; nothing was changed.")
            if error:
                raise error
            text = "\n\n".join(b.get("text", "") for b in content if b.get("type") == "text").strip()
            if not text:
                raise llm.LLMError("the model returned an empty summary")
            self.add_message(sid, "user", [{"type": "text", "text": text, "compaction": True,
                                            "compacted": len(history)}], model, provider["id"])
            notice("Compacted %d messages into a summary of about %s tokens. The model now sees the summary and "
                   "what follows; the messages above stay here for you." % (len(history), "{:,}".format(len(text) // 4)))
        except Exception as e:
            notice("**Compaction failed:** %s" % e, "error")
        finally:
            rt.cancel = None
            rt.stream = None
            rt.draft = None
            if rt.task is asyncio.current_task():
                rt.task = None
            if rt.queue and self.summary(sid):  # messages sent while compacting
                text = rt.queue.pop(0)
                self.server.emit(sid, "queue.updated", {"session_id": sid, "queue": list(rt.queue)})
                rt.task = asyncio.ensure_future(self._run(sid, text))
            elif self.summary(sid) and self.peers.wants_turn(sid):
                rt.task = asyncio.ensure_future(self._run(sid, None))
            self.updated(sid)

    async def _run(self, sid, text):
        """text None: a wake by a peer question (only the inbox is delivered)."""
        rt = self.rt[sid]
        rt.prompted = False
        first_exchange = False
        try:
            while True:
                first = not any(m["role"] == "user" for m in self.messages(sid))
                had_peer = self.store_peer_blocks(sid)
                if text is None:
                    if not had_peer:
                        break
                    first = False
                else:
                    rt.prompted = True
                    self.add_message(sid, "user", [{"type": "text", "text": text}])
                self.updated(sid)
                if first:
                    # title + identicon subjects from the first message now, and again (below) once the
                    # first exchange is done and control is back with the user
                    first_exchange = True
                    self.spawn(self.retitle(sid))
                try:
                    await self._turn(sid, rt)
                except Exception as e:
                    rt.sub_error = e
                    self.server.emit(sid, "notice", {"session_id": sid, "level": "error", "text": "**Error:** %s" % e})
                if rt.cancel and rt.cancel.is_set():
                    break
                if rt.queue:
                    text = rt.queue.pop(0)
                    self.server.emit(sid, "queue.updated", {"session_id": sid, "queue": list(rt.queue)})
                elif self.peers.wants_turn(sid):  # a question arrived during the last step
                    text = None
                else:
                    break
        finally:
            if rt.prompted:  # a turn only answering peers doesn't report back to the parent
                subagents.turn_finished(self, sid, rt, getattr(rt, "sub_error", None))
            rt.sub_error = None
            rt.cancel = None
            rt.stream = None
            rt.tool_ctx = None
            rt.tools = {}
            rt.draft = None
            loop_task = asyncio.current_task()
            if rt.task is loop_task:
                rt.task = None
            self.updated(sid)
            if first_exchange and self.summary(sid):
                self.spawn(self.retitle(sid))

    async def _turn(self, sid, rt):
        rt.cancel = asyncio.Event()
        loop = asyncio.get_running_loop()
        for _ in range(MAX_ITERATIONS):
            settings = self.settings()
            session = self.require(sid)
            provider, model = self.session_llm(sid, settings)
            _, effort, einfo = self.session_effort(sid, provider["id"], model, settings)
            system = skills.system_prompt(settings, session["cwd"], self.server.html_path)
            system += subagents.system_note(self.store, sid)
            started = now_ms()
            stream = llm.retrying_stream(settings, provider, self.server.creds, model, system, self.llm_history(sid),
                                         subagents.tool_defs(self.store, sid), settings["max_tokens"],
                                         settings["thinking_budget"], effort, einfo["adaptive"])
            asyncio.ensure_future(self.server.pricing(provider))  # in parallel with the response
            content, stop_reason, error, usage = await self._stream(sid, rt, stream, loop)
            cancelled = rt.cancel.is_set()
            if (error and not cancelled and not content and effort and einfo["source"] != "override"
                    and llm.effort_rejected(error)):
                # the model doesn't take effort levels: remember that (until a restart; a `model_overrides`
                # entry makes it permanent) and repeat the request without one
                self.server.effort_refused.add((provider["id"], model))
                self.server.emit(sid, "draft.end", {"session_id": sid, "turn_id": rt.draft["turn_id"]})
                rt.draft = None
                self.server.emit(sid, "notice", {
                    "session_id": sid, "level": "info",
                    "text": "%s refused the effort level `%s` (%s); sending without one." % (model, effort, error)})
                self.updated(sid)
                continue
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            if usage:
                self.add_usage(sid, await self.price(provider, model, usage))
            if content:
                self.add_message(sid, "assistant", content, model, provider["id"], usage, effort, started)
            self.server.emit(sid, "draft.end", {"session_id": sid, "turn_id": rt.draft["turn_id"]})
            rt.draft = None
            if error and not cancelled:
                raise error
            if cancelled or stop_reason != "tool_use" or not tool_uses:
                if tool_uses:
                    self._interrupted_results(sid, tool_uses)
                return
            rt.tool_ctx = tools.Context(session["cwd"], lambda n, c=session["cwd"]: skills.load(
                n, c, self.settings(), self.server.html_path))
            subagents.attach(self, sid, rt.tool_ctx, loop)
            rt.tool_ctx.peer = peers.ToolBridge(self.peers, sid, loop)
            results = []
            rt.tools = {}
            for tu in tool_uses:
                if rt.cancel.is_set():
                    break
                inp = tu.get("input") or {}
                t0 = now_ms()
                refused = self.peers.before_tool(sid, tu["name"], inp)
                if refused:
                    results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": refused,
                                    "is_error": True, "started_at": t0, "ended_at": now_ms()})
                    continue
                self.peers.after_tool(sid, tu["name"], inp, True)  # activity: what it is doing now
                self.updated(sid)
                rt.tools[tu["id"]] = {"started_at": t0}
                self.server.emit(sid, "tool.start", {"session_id": sid, "tool_use_id": tu["id"], "started_at": t0})
                fut = in_thread(tools.execute, tu["name"], inp, rt.tool_ctx)
                cancel_wait = asyncio.ensure_future(rt.cancel.wait())
                await asyncio.wait([fut, cancel_wait], return_when=asyncio.FIRST_COMPLETED)
                cancel_wait.cancel()
                if fut.done():
                    out, is_err = fut.result()
                    out += self.peers.after_tool(sid, tu["name"], inp, is_err)
                else:
                    out, is_err = "interrupted", True
                t1 = now_ms()
                rt.tools[tu["id"]]["ended_at"] = t1
                self.server.emit(sid, "tool.end", {"session_id": sid, "tool_use_id": tu["id"], "started_at": t0,
                                                   "ended_at": t1, "is_error": is_err})
                results.append({"type": "tool_result", "tool_use_id": tu["id"], "content": out, "is_error": is_err,
                                "started_at": t0, "ended_at": t1})
            rt.tool_ctx = None
            rt.tools = {}
            done = {r["tool_use_id"] for r in results}
            results += [{"type": "tool_result", "tool_use_id": tu["id"], "content": "interrupted", "is_error": True}
                        for tu in tool_uses if tu["id"] not in done]
            self.add_message(sid, "user", results, model, provider["id"])
            if rt.cancel.is_set():
                return
            self.store_peer_blocks(sid)  # peer messages / changed peers reach the model at each step
        self.server.emit(sid, "notice", {"session_id": sid, "level": "error",
                                         "text": "Stopped after %d tool iterations." % MAX_ITERATIONS})

    def _interrupted_results(self, sid, tool_uses):
        self.add_message(sid, "user", [{"type": "tool_result", "tool_use_id": tu["id"], "content": "interrupted",
                                        "is_error": True} for tu in tool_uses])

    async def _stream(self, sid, rt, stream, loop):
        """Consumes a provider stream in a thread, mirroring it into rt.draft + draft.* events.
        Returns (content blocks, stop_reason, error, usage)."""
        turn_id = "t_" + os.urandom(4).hex()
        rt.draft = {"turn_id": turn_id, "content": []}
        rt.stream = stream
        q = asyncio.Queue()
        self.server.emit(sid, "draft.start", {"session_id": sid, "turn_id": turn_id})

        def pump():
            try:
                for ev in stream:
                    loop.call_soon_threadsafe(q.put_nowait, ev)
                loop.call_soon_threadsafe(q.put_nowait, ("done",))
            except Exception as e:
                loop.call_soon_threadsafe(q.put_nowait, ("error", e))

        in_thread(pump)
        cancel_wait = asyncio.ensure_future(rt.cancel.wait())
        stop_reason, error, usage = None, None, None
        blocks = rt.draft["content"]
        try:
            while True:
                get = asyncio.ensure_future(q.get())
                await asyncio.wait([get, cancel_wait], return_when=asyncio.FIRST_COMPLETED)
                if not get.done():
                    get.cancel()
                    break
                ev = get.result()
                kind = ev[0]
                if kind == "block":
                    _, idx, block = ev
                    while len(blocks) <= idx:
                        blocks.append(None)
                    block = dict(block)
                    if block.get("type") == "tool_use":
                        block["partial_json"] = ""
                    block["started_at"] = block["ended_at"] = now_ms()
                    blocks[idx] = block
                    self.server.emit(sid, "draft.block", {"session_id": sid, "turn_id": turn_id, "index": idx,
                                                          "block": block})
                elif kind == "delta":
                    _, idx, field, text = ev
                    b = blocks[idx]
                    b[field] = (b.get(field) or "") + text
                    b["ended_at"] = now_ms()  # streaming time: from the block's start to its last delta
                    if field != "signature":
                        self.server.emit(sid, "draft.delta", {"session_id": sid, "turn_id": turn_id, "index": idx,
                                                              field: text})
                elif kind == "stop":
                    stop_reason = ev[1]
                elif kind == "usage":
                    usage = ev[1]
                elif kind == "retry":
                    # the attempt failed; its partial output is dropped and the message streams again
                    _, attempt, retries, delay, err = ev
                    self.server.emit(sid, "notice", {
                        "session_id": sid, "level": "info", "retry": True,
                        "text": "Provider problem: %s — retrying in %.1fs (%d/%d)." % (err, delay, attempt, retries)})
                    self.server.emit(sid, "draft.end", {"session_id": sid, "turn_id": turn_id})
                    turn_id = "t_" + os.urandom(4).hex()
                    blocks = []
                    rt.draft = {"turn_id": turn_id, "content": blocks}
                    self.server.emit(sid, "draft.start", {"session_id": sid, "turn_id": turn_id})
                elif kind == "done":
                    break
                elif kind == "error":
                    error = ev[1]
                    break
        finally:
            cancel_wait.cancel()
            rt.stream = None
        return finalize_blocks(blocks), stop_reason, error, usage

    def spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self.bg.add(t)
        t.add_done_callback(self.bg.discard)
        return t

    # --- titles --------------------------------------------------------------------------------

    async def retitle(self, sid):
        """Regenerates title + identicon subjects. Only the newest call for a session stores its result
        (the first-message one may finish after the one made when the first exchange ended)."""
        if not self.summary(sid):
            return
        rt = self.rt[sid]
        rt.title_gen = gen = getattr(rt, "title_gen", 0) + 1
        msgs = self.messages(sid)
        convo = []
        for m in msgs:
            for b in m["content"]:
                if b.get("type") == "text" and not peers.is_peer_block(b):
                    convo.append("%s: %s" % (m["role"], b["text"]))
        text = "\n".join(convo)[:12000]
        settings = self.settings()
        n = int(settings.get("identicon_subjects") or 3)
        user_text = "\n".join(b.get("text", "") for m in msgs if m["role"] == "user"
                              for b in m["content"] if b.get("type") == "text" and not peers.is_peer_block(b))
        title, subjects = fallback_title(user_text, n)
        session = self.summary(sid) or {}
        # cheap (cached stats, a capped listing of the cwd): inline, so a retitle without an LLM stays one step
        sig = signals.extract(msgs, session.get("cwd"), peers.is_peer_block)
        context = self.subject_context(sid, session.get("cwd"), sig)
        # without an LLM: reuse the domain/topic of a recent session mainly about the same project (or, when
        # neither touched any project, one in the same directory)
        top = sig["projects"][:1]
        near = next((c for c in context if len(c["subjects"]) >= 2 and (
            (c["shared"] and c["signals"]["projects"][:1] == top) or
            (c["same_cwd"] and not top and not c["signals"].get("projects")))), None)
        if near and n >= 3:
            subjects = near["subjects"][:n - 1] + [x for x in subjects if x not in near["subjects"]][:1]
        try:
            provider, _ = self.session_llm(sid, settings)
        except llm.LLMError:
            provider = {"type": "mock"}
        if provider["type"] != "mock" and text:
            prompt = subjects_prompt(n, text, session.get("cwd"), context, sig)
            try:
                used = {}
                try:
                    out = await in_thread(llm.complete, provider, self.server.creds, prompt, provider["cheap_model"],
                                          1000, settings, used)
                finally:
                    if used and self.summary(sid):
                        self.add_usage(sid, await self.price(provider, provider["cheap_model"], used))
                m = re.search(r"\{.*\}", out, re.S)
                data = json.loads(m.group(0))
                if data.get("title"):
                    title = str(data["title"]).strip().strip("\"'`").rstrip(".").strip()[:120] or title
                if isinstance(data.get("subjects"), list) and data["subjects"]:
                    subjects = [str(x).strip().lower() for x in data["subjects"]][:n]
            except Exception:
                pass
        if not self.summary(sid) or getattr(self.rt.get(sid), "title_gen", gen) != gen:
            return
        self.store.execute("UPDATE sessions SET title=?, subjects=?, signals=? WHERE id=?",
                           (title, json.dumps(subjects), json.dumps(sig), sid))
        self.updated(sid)

    def subject_context(self, sid, cwd, sig=None, limit=25):
        """Recent other sessions with subjects, to keep identicons consistent: those that touched one of this
        session's projects (sig["projects"]) first, then those in the same directory, then the rest; each
        with the signals its subjects were made from (computed now for sessions titled before they were
        stored)."""
        rows = self.store.query("SELECT id, title, cwd, subjects, signals FROM sessions WHERE id != ? AND "
                                "subjects IS NOT NULL AND subjects != '[]' ORDER BY updated_at DESC LIMIT 200", (sid,))
        mine = set((sig or {}).get("projects") or [])
        out = []
        for rid, title, rcwd, subj, rsig in rows:
            try:
                subjects = [str(x) for x in json.loads(subj)]
                rsig = json.loads(rsig) if rsig else None
            except ValueError:
                continue
            if subjects:
                out.append({"id": rid, "title": title or "", "cwd": rcwd or "", "subjects": subjects,
                            "signals": rsig, "same_cwd": bool(cwd and rcwd == cwd)})

        def compute(c):  # once per older session: stored like a retitle would
            if c["signals"] is None:
                c["signals"] = signals.extract(self.messages(c["id"]), c["cwd"], peers.is_peer_block)
                self.store.execute("UPDATE sessions SET signals=? WHERE id=?", (json.dumps(c["signals"]), c["id"]))
        if mine:  # the most recent ones can only be compared after computing theirs
            for c in out[:limit]:
                compute(c)
        for c in out:
            c["shared"] = bool(mine & set((c["signals"] or {}).get("projects") or []))
        out.sort(key=lambda c: (not c["shared"], not c["same_cwd"]))  # stable: recency within each group
        out = out[:limit]
        for c in out:  # shown in the prompt
            compute(c)
        return out

    # --- /btw ----------------------------------------------------------------------------------

    async def btw(self, sid, question):
        session = self.require(sid)
        btw_id = "b_" + os.urandom(4).hex()
        settings = self.settings()
        provider, model = self.session_llm(sid, settings)
        history = self.llm_history(sid) + [{"role": "user", "content": [{"type": "text", "text": question}]}]
        system = (skills.system_prompt(settings, session["cwd"], self.server.html_path) +
                  "\n\n# Side question\nThe user asks a quick side question about this conversation. Answer it "
                  "directly and briefly from the context; you cannot use tools for this answer.")
        self.server.emit(sid, "btw.start", {"session_id": sid, "btw_id": btw_id, "question": question})
        loop = asyncio.get_running_loop()
        q = asyncio.Queue()

        def pump():
            try:
                s = llm.retrying_stream(settings, provider, self.server.creds, model, system, history, None,
                                        settings["max_tokens"], 0)
                for ev in s:
                    loop.call_soon_threadsafe(q.put_nowait, ev)
                loop.call_soon_threadsafe(q.put_nowait, ("done",))
            except Exception as e:
                loop.call_soon_threadsafe(q.put_nowait, ("error", e))

        in_thread(pump)
        end = {"session_id": sid, "btw_id": btw_id}
        while True:
            ev = await q.get()
            if ev[0] == "delta" and ev[2] == "text":
                self.server.emit(sid, "btw.delta", {"session_id": sid, "btw_id": btw_id, "text": ev[3]})
            elif ev[0] == "usage":
                self.add_usage(sid, await self.price(provider, model, ev[1]))
            elif ev[0] == "retry":  # clients drop the partial answer
                self.server.emit(sid, "btw.delta", {"session_id": sid, "btw_id": btw_id, "reset": True, "text": ""})
            elif ev[0] == "done":
                break
            elif ev[0] == "error":
                end["error"] = str(ev[1])
                break
        self.server.emit(sid, "btw.end", end)


def merge_usage(a, b):
    """Sum of two usage totals (llm.add_usage format)."""
    out = dict(a or {})
    for k in llm.USAGE_KEYS + ("calls", "priced_calls", "estimated_calls"):
        out[k] = int(out.get(k) or 0) + int((b or {}).get(k) or 0)
    out["cost"] = float(out.get("cost") or 0) + float((b or {}).get("cost") or 0)
    return out


def in_thread(fn, *args):
    """Runs fn in its own daemon thread (no pool: any number of sessions may block concurrently)."""
    loop = asyncio.get_running_loop()
    fut = loop.create_future()

    def done(ok, val):
        if not fut.done():
            fut.set_result(val) if ok else fut.set_exception(val)

    def run():
        try:
            r = fn(*args)
        except BaseException as e:
            loop.call_soon_threadsafe(done, False, e)
        else:
            loop.call_soon_threadsafe(done, True, r)
    threading.Thread(target=run, daemon=True).start()
    return fut


def finalize_blocks(blocks):
    out = []
    for b in blocks:
        if not b:
            continue
        b = dict(b)
        if b.get("type") == "tool_use":
            raw = b.pop("partial_json", "") or ""
            try:
                b["input"] = json.loads(raw) if raw.strip() else {}
            except ValueError:
                b["input"] = {}
        elif b.get("type") == "text" and not b.get("text"):
            continue
        elif b.get("type") == "thinking" and not b.get("thinking"):
            continue
        out.append(b)
    return out
