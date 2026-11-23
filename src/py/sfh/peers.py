"""Peer sessions: other sessions working in the same repository at the same time.

Awareness (who is active, what they do, which files they touched), advisory file claims, edit/write
conflict checks, and messages between sessions delivered through the recipient's inbox at its next step
boundary. All state is in memory (Runtime fields + Hub.claims); see docs/DESIGN.md v10."""
import asyncio
import os
from xml.sax.saxutils import escape, quoteattr

from . import tools
from .store import now_ms

ACTIVE_MS = 15 * 60 * 1000          # a session counts as active this long after its last update
RECENT_EDIT_MS = 15 * 60 * 1000     # edits by others this recent trigger warnings
AUTO_CLAIM_MS = 10 * 60 * 1000      # edit/write claim the file this long
CLAIM_MS = 30 * 60 * 1000           # default for the claim tool
MAX_CLAIM_MIN = 240
WAKE_LIMIT, WAKE_WINDOW_MS = 5, 10 * 60 * 1000
MAX_MESSAGE = 4000
KINDS = ("question", "reply", "info")

_roots = {}

AVOID = ("Avoid conflicts first: check `peers` and claims before editing, prefer files nobody else is working "
         "on, and `claim` files before larger edits. Message another session (`send_message`) only to ask "
         "something you need or to settle a conflict you can't avoid.")


def repo_root(cwd):
    """The nearest ancestor of cwd containing .git, else cwd itself (cached)."""
    if not cwd:
        return None
    if cwd in _roots:
        return _roots[cwd]
    d = cwd
    while True:
        if os.path.exists(os.path.join(d, ".git")):
            break
        up = os.path.dirname(d)
        if up == d:
            d = cwd
            break
        d = up
    _roots[cwd] = d
    return d


def covers(claimed, path):
    return path == claimed or path.startswith(claimed.rstrip(os.sep) + os.sep)


def mtime(p):
    try:
        return os.stat(p).st_mtime_ns
    except OSError:
        return None


def ago(ms):
    s = max(0, (now_ms() - ms) // 1000)
    if s < 60:
        return "%ds ago" % s
    if s < 3600:
        return "%d min ago" % (s // 60)
    return "%dh ago" % (s // 3600)


def short_input(name, inp):
    if not isinstance(inp, dict):
        return ""
    for k in ("path", "command", "pattern", "url", "name", "session_id", "description"):
        v = inp.get(k)
        if isinstance(v, str) and v:
            v = " ".join(v.split())
            return v[:120] + ("…" if len(v) > 120 else "")
    if isinstance(inp.get("paths"), list):
        return ", ".join(str(p) for p in inp["paths"])[:120]
    return ""


def is_peer_block(b):
    return bool(b.get("peer_message") or b.get("peer_sessions"))


class Hub:
    def __init__(self, agent):
        self.agent = agent
        self.store = agent.store
        self.claims = {}        # abs path -> {"session_id", "note", "auto", "at", "expires_at"}
        self.last_edit = {}     # abs path -> (session id, ms)

    # --- basics --------------------------------------------------------------------------------

    def rt(self, sid):
        return self.agent.rt[sid]

    def session_row(self, sid):
        return self.store.query_one("SELECT id, title, cwd, kind, parent_id, updated_at FROM sessions WHERE id=?",
                                    (sid,))

    def repo(self, sid):
        row = self.session_row(sid)
        return repo_root(row[2]) if row else None

    def rel(self, path, root):
        try:
            r = os.path.relpath(path, root) if root else path
        except ValueError:
            return path
        return path if r.startswith("..") else r

    def resolve(self, sid, path):
        row = self.session_row(sid)
        return os.path.normpath(os.path.join(row[2] if row else "/", os.path.expanduser(str(path))))

    def prune(self):
        t = now_ms()
        for p in [p for p, c in self.claims.items() if c["expires_at"] <= t]:
            del self.claims[p]

    def claims_of(self, sid, root=None):
        self.prune()
        root = root or self.repo(sid)
        return [{"path": self.rel(p, root), "abs": p, "note": c["note"], "auto": c["auto"],
                 "expires_at": c["expires_at"]}
                for p, c in sorted(self.claims.items()) if c["session_id"] == sid]

    def edited_of(self, sid, root=None, limit=10):
        root = root or self.repo(sid)
        t = now_ms() - RECENT_EDIT_MS
        rt = self.agent.rt.get(sid)
        items = sorted(((p, at) for p, at in (rt.edited.items() if rt else ()) if at >= t),
                       key=lambda x: -x[1])[:limit]
        return [{"path": self.rel(p, root), "abs": p, "at": at} for p, at in items]

    def summary_fields(self, sid, cwd):
        """Extra fields for the session summary."""
        root = repo_root(cwd)
        rt = self.agent.rt.get(sid)
        return {"repo": root,
                "activity": rt.activity if rt else None,
                "edited": [{"path": e["path"], "at": e["at"]} for e in self.edited_of(sid, root)],
                "claims": [{k: c[k] for k in ("path", "note", "auto", "expires_at")} for c in self.claims_of(sid, root)],
                "inbox": [{"id": m["id"], "from": m["from"], "from_title": m["from_title"], "kind": m["kind"],
                           "text": m["text"][:300], "at": m["at"]} for m in (rt.inbox if rt else [])]}

    def relation(self, sid, other_row, me_row):
        if me_row[4] and other_row[0] == me_row[4]:
            return "parent"
        if other_row[4] == sid:
            return "child"
        if me_row[4] and other_row[4] == me_row[4]:
            return "sibling"
        return "repo"

    def peers_of(self, sid):
        """Active sessions in the same repo (or related), with what they are doing."""
        me = self.session_row(sid)
        if not me:
            return []
        root = repo_root(me[2])
        since = now_ms() - ACTIVE_MS
        running = {k for k in list(self.agent.rt) if self.agent.running(k)}
        rows = self.store.query("SELECT id, title, cwd, kind, parent_id, updated_at FROM sessions "
                                "WHERE id != ? AND head_id IS NOT NULL AND updated_at >= ?", (sid, since))
        ids = {r[0] for r in rows}
        rows = list(rows) + [self.session_row(k) for k in running if k != sid and k not in ids]
        out = []
        for r in rows:
            if not r or repo_root(r[2]) != root:
                continue
            rt = self.agent.rt.get(r[0])
            st = "running" if r[0] in running else "idle"
            out.append({"session_id": r[0], "title": r[1] or "", "status": st, "kind": r[3] or "chat",
                        "relation": self.relation(sid, r, me), "updated_at": r[5],
                        "activity": rt.activity if rt and st == "running" else None,
                        "claims": [{k: c[k] for k in ("path", "note", "auto")} for c in self.claims_of(r[0], root)],
                        "edited": [{"path": e["path"], "at": e["at"]} for e in self.edited_of(r[0], root)]})
        out.sort(key=lambda p: (p["status"] != "running", -(p["updated_at"] or 0)))
        return out

    def latest_prompt(self, sid):
        for m in reversed(self.agent.messages(sid)):
            if m["role"] == "user":
                t = "\n".join(b.get("text", "") for b in m["content"] if b.get("type") == "text"
                              and not is_peer_block(b) and not b.get("compaction"))
                if t.strip() and not t.startswith("<subagent-result"):
                    t = " ".join(t.split())
                    return t[:200] + ("…" if len(t) > 200 else "")
        return ""

    def describe(self, p, prompt=True):
        bits = [p["status"]]
        if p["relation"] != "repo":
            bits.append("your " + p["relation"])
        if p["activity"]:
            bits.append("now: %s %s" % (p["activity"]["tool"], p["activity"]["summary"]))
        line = '- %s "%s" (%s)' % (p["session_id"], p["title"] or "untitled", "; ".join(bits))
        if p["claims"]:
            line += "\n  claims: " + ", ".join(c["path"] + (" (%s)" % c["note"] if c["note"] else "")
                                               for c in p["claims"])
        if p["edited"]:
            line += "\n  edited: " + ", ".join("%s (%s)" % (e["path"], ago(e["at"])) for e in p["edited"])
        if prompt:
            lp = self.latest_prompt(p["session_id"])
            if lp:
                line += "\n  latest prompt: " + lp
        return line

    # --- the peers tool ------------------------------------------------------------------------

    def peers_text(self, sid):
        peers = self.peers_of(sid)
        root = self.repo(sid)
        out = ["Repository: %s" % root]
        if peers:
            out.append("Other sessions active here:")
            out += [self.describe(p) for p in peers]
        else:
            out.append("No other sessions are active in this repository.")
        mine = self.claims_of(sid, root)
        out.append("Your claims: " + (", ".join(c["path"] + (" (auto)" if c["auto"] else "") for c in mine)
                                      if mine else "none"))
        if peers:
            out.append("\n" + AVOID)
        return "\n".join(out)

    # --- claims --------------------------------------------------------------------------------

    def holder(self, path, sid):
        """The claim of another session covering path, or None."""
        self.prune()
        for p, c in self.claims.items():
            if c["session_id"] != sid and (covers(p, path) or covers(path, p)):
                return p, c
        return None

    def claim(self, sid, paths, note="", minutes=None):
        if isinstance(paths, str):
            paths = [paths]
        if not paths:
            raise tools.ToolError("paths is required")
        ttl = CLAIM_MS if not minutes else max(1, min(MAX_CLAIM_MIN, int(minutes))) * 60000
        root = self.repo(sid)
        got, refused = [], []
        for raw in paths:
            p = self.resolve(sid, raw)
            h = self.holder(p, sid)
            if h:
                refused.append("%s: claimed by %s (%s)%s" % (self.rel(p, root), h[1]["session_id"],
                                                              self.title(h[1]["session_id"]),
                                                              ": " + h[1]["note"] if h[1]["note"] else ""))
                continue
            self.claims[p] = {"session_id": sid, "note": str(note or "")[:200], "auto": False, "at": now_ms(),
                              "expires_at": now_ms() + ttl}
            got.append(self.rel(p, root))
        self.agent.updated(sid)
        out = []
        if got:
            out.append("Claimed for %d min: %s" % (ttl // 60000, ", ".join(got)))
        if refused:
            out.append("NOT claimed (held by another session; don't edit these, work around them or ask the "
                       "owner with send_message):\n" + "\n".join("- " + r for r in refused))
        return "\n".join(out)

    def release(self, sid, paths=None):
        if isinstance(paths, str):
            paths = [paths]
        root = self.repo(sid)
        if paths:
            targets = [self.resolve(sid, p) for p in paths]
            gone = [p for p, c in list(self.claims.items()) if c["session_id"] == sid
                    and any(covers(t, p) for t in targets)]
        else:
            gone = [p for p, c in list(self.claims.items()) if c["session_id"] == sid]
        for p in gone:
            del self.claims[p]
        self.agent.updated(sid)
        return "Released: " + (", ".join(self.rel(p, root) for p in gone) if gone else "nothing (no matching claims)")

    def title(self, sid):
        row = self.session_row(sid)
        return (row[1] or "untitled") if row else "deleted session"

    # --- tool hooks (on the event loop) -------------------------------------------------------

    def before_tool(self, sid, name, inp):
        """An error text when the tool must not run, else None."""
        if name != "write" or not isinstance(inp, dict) or not inp.get("path"):
            return None
        p = self.resolve(sid, inp["path"])
        last = self.last_edit.get(p)
        if not last or last[0] == sid or not os.path.exists(p):
            return None
        seen = self.rt(sid).seen.get(p)
        if seen is not None and seen == mtime(p):
            return None
        return ("Refused: %s was changed by another session, %s (%s), %s, after you last read it. Overwriting "
                "it now would discard that work. `read` it first, then make your change (prefer `edit`), or ask "
                "that session with send_message." % (inp["path"], last[0], self.title(last[0]), ago(last[1])))

    def after_tool(self, sid, name, inp, is_err):
        """Records activity/reads/edits; returns a warning to append to the result, or ""."""
        rt = self.rt(sid)
        rt.activity = {"tool": name, "summary": short_input(name, inp), "at": now_ms()}
        if is_err or not isinstance(inp, dict) or not isinstance(inp.get("path"), str):
            return ""
        p = self.resolve(sid, inp["path"])
        if name == "read":
            if os.path.isfile(p):
                rt.seen[p] = mtime(p)
            return ""
        if name not in ("edit", "write"):
            return ""
        t = now_ms()
        rt.edited[p] = t
        rt.seen[p] = mtime(p)
        prev = self.last_edit.get(p)
        self.last_edit[p] = (sid, t)
        h = self.holder(p, sid)
        own = self.claims.get(p)
        if not h and (not own or own["session_id"] == sid):
            if not own or own["auto"]:
                self.claims[p] = {"session_id": sid, "note": "editing", "auto": True, "at": t,
                                  "expires_at": t + AUTO_CLAIM_MS}
            elif own["expires_at"] < t + AUTO_CLAIM_MS:
                own["expires_at"] = t + AUTO_CLAIM_MS
        others = []
        if h:
            c = h[1]
            others.append("is claimed by session %s (\"%s\")%s" % (c["session_id"], self.title(c["session_id"]),
                                                                   ": \"%s\"" % c["note"] if c["note"] else ""))
        for k, r in list(self.agent.rt.items()):
            at = r.edited.get(p) if k != sid else None
            if at and at >= t - RECENT_EDIT_MS and self.session_row(k):
                others.append("was edited by session %s (\"%s\") %s" % (k, self.title(k), ago(at)))
        if not others:
            return ""
        reread = " Re-read the file to check you didn't undo its changes." if prev and prev[0] != sid else ""
        return ("\n\n⚠ %s %s. Your change was applied.%s Before more edits here, check `peers` or ask that "
                "session with send_message; prefer working on other files meanwhile."
                % (inp["path"], " and ".join(others), reread))

    # --- messages ------------------------------------------------------------------------------

    def can_message(self, sid, to):
        me, other = self.session_row(sid), self.session_row(to)
        if not other:
            return False
        if repo_root(me[2]) == repo_root(other[2]):
            return True
        return self.relation(sid, other, me) != "repo"

    def send(self, sid, to, text, kind=None, reply_to=None):
        to = str(to or "").strip()
        text = str(text or "").strip()
        if not text:
            raise tools.ToolError("text is required")
        if to == sid:
            raise tools.ToolError("that is this session")
        if not self.session_row(to):
            raise tools.ToolError("no session %s (see `peers` for active sessions)" % to)
        if not self.can_message(sid, to):
            raise tools.ToolError("%s works in another repository; you can only message sessions in this "
                                  "repository or your parent/subsessions" % to)
        if kind not in KINDS:
            kind = "reply" if reply_to else ("question" if "?" in text else "info")
        msg = {"id": "pm_" + os.urandom(4).hex(), "from": sid, "from_title": self.title(sid), "to": to,
               "kind": kind, "text": text[:MAX_MESSAGE], "reply_to": str(reply_to) if reply_to else None,
               "at": now_ms()}
        rt = self.rt(to)
        rt.inbox.append(msg)
        name = '%s ("%s")' % (to, self.title(to))
        if self.agent.running(to):
            status = "Sent %s to %s: it is running and gets the message after its current step." % (msg["id"], name)
        elif kind == "question" and self.may_wake(to):
            self.note_wake(to)
            self.agent.wake(to)
            status = "Sent %s to %s: it was idle and now starts a turn to answer." % (msg["id"], name)
        else:
            why = ("it is idle and owned by the user, so it answers when the user lets it or sends it a prompt"
                   if kind == "question" else "it is idle and sees it with its next prompt")
            status = "Sent %s to %s: %s." % (msg["id"], name, why)
        self.agent.updated(to)
        if kind == "question":
            status += " A reply arrives later as a <peer-message> in your conversation; don't wait for it, continue " \
                      "with work that doesn't depend on the answer."
        return status

    def may_wake(self, sid):
        """An idle session may be started by a peer question: (old) subagent sessions only (top-level sessions
        and subsessions belong to the user), within the wake limit."""
        row = self.session_row(sid)
        if not row or not row[4] or row[3] != "subagent":
            return False
        return self.under_limit(sid)

    def under_limit(self, sid):
        t = now_ms()
        rt = self.rt(sid)
        rt.wakes = [w for w in rt.wakes if w > t - WAKE_WINDOW_MS]
        return len(rt.wakes) < WAKE_LIMIT

    def note_wake(self, sid):
        self.rt(sid).wakes.append(now_ms())

    def wants_turn(self, sid):
        """At the end of a run: a question arrived during the last step and the session may answer it now."""
        rt = self.agent.rt.get(sid)
        if rt and any(m["kind"] == "question" for m in rt.inbox) and self.under_limit(sid):
            self.note_wake(sid)
            return True
        return False

    def message_block(self, m):
        attrs = "id=%s from=%s title=%s kind=%s" % (quoteattr(m["id"]), quoteattr(m["from"]),
                                                     quoteattr(m["from_title"]), quoteattr(m["kind"]))
        if m["reply_to"]:
            attrs += " reply_to=%s" % quoteattr(m["reply_to"])
        if m["kind"] == "question":
            note = ("Another session working in this repository asks you this. If you can, answer briefly with "
                    "send_message(session_id=\"%s\", reply_to=\"%s\", text=...), then continue your own task."
                    % (m["from"], m["id"]))
        elif m["kind"] == "reply":
            note = "This answers your message %s." % (m["reply_to"] or "")
        else:
            note = "For your information, from another session in this repository; no reply needed."
        text = "<peer-message %s>\n%s\n</peer-message>\n<note>%s</note>" % (attrs, escape(m["text"]), note)
        return {"type": "text", "text": text,
                "peer_message": {k: m[k] for k in ("id", "from", "from_title", "kind", "reply_to", "text")}}

    def sessions_block(self, sid):
        """A <peer-sessions> block when the set of active peers changed since the session last saw it."""
        rt = self.rt(sid)
        # only peers that can conflict: running, or holding claims / recent edits; a session's own (old) subagent
        # parent and children are known to it already (the `peers` tool lists everyone). Subsessions are separate
        # conversations of the user that can conflict like any other session.
        me = self.session_row(sid)
        own = lambda p: ((p["relation"] == "child" and p["kind"] == "subagent")
                         or (p["relation"] == "parent" and me and me[3] == "subagent"))
        peers = [p for p in self.peers_of(sid) if not own(p)
                 and (p["status"] == "running" or p["claims"] or p["edited"])]
        sig = tuple(sorted(p["session_id"] for p in peers))
        if sig == rt.peer_sig or (rt.peer_sig is None and not sig):
            rt.peer_sig = sig
            return None
        rt.peer_sig = sig
        if not peers:
            text = "<peer-sessions>\nNo other sessions are active in this repository now.\n</peer-sessions>"
        else:
            text = ("<peer-sessions>\nOther sessions are working in this repository at the same time:\n%s\n\n%s\n"
                    "</peer-sessions>" % ("\n".join(self.describe(p, prompt=False) for p in peers), AVOID))
        return {"type": "text", "text": text, "peer_sessions": list(sig)}

    def pending_blocks(self, sid):
        """Blocks to store before the next LLM call: the peer-sessions note (if changed) + the inbox."""
        rt = self.rt(sid)
        blocks = []
        b = self.sessions_block(sid)
        if b:
            blocks.append(b)
        if rt.inbox:
            blocks += [self.message_block(m) for m in rt.inbox]
            rt.inbox = []
        return blocks

    def forget(self, sid):
        for p in [p for p, c in self.claims.items() if c["session_id"] == sid]:
            del self.claims[p]

    def forget_thread(self, bid):
        for p in [p for p, c in self.claims.items() if c.get("branch_id") == bid]:
            del self.claims[p]


class ToolBridge:
    """The peer tools' access to the hub from the tool thread (everything runs on the event loop)."""

    def __init__(self, hub, sid, loop):
        self.hub, self.sid, self.loop = hub, sid, loop

    def _call(self, fn, *args):
        async def run():
            return fn(*args)
        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(30)

    def peers(self):
        return self._call(self.hub.peers_text, self.sid)

    def claim(self, paths, note, minutes):
        return self._call(self.hub.claim, self.sid, paths, note, minutes)

    def release(self, paths):
        return self._call(self.hub.release, self.sid, paths)

    def send(self, to, text, kind, reply_to):
        return self._call(self.hub.send, self.sid, to, text, kind, reply_to)

