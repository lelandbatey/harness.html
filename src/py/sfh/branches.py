"""Unified session threads: branches (/aside, edit) and subagents (fresh or forked context)
living in one tree inside a session, with unified inter-thread communication (threads, spawn,
ask, message) that also spans external peer sessions in the same repository.

Each row in `branches` represents a thread of `session_id`:
- `kind`: 'main' | 'aside' | 'subagent' | 'edit'
- `context_mode`: 'fork' (inherits messages up to `origin_id`) | 'fresh' (`origin_id` is NULL; clean context)
- `origin_id`: message id where LLM history forks from (NULL for main or fresh-context subagent)
- `spawned_at_id`: message id where this thread attaches visually in the UI/minimap
- `parent_branch_id`: the thread id that created/forked this thread (NULL for main)
"""
import asyncio
import json
import os
import re
from xml.sax.saxutils import quoteattr

from . import llm, peers, skills, tools
from .store import now_ms

PROMOTE_PROMPTS = 3  # user prompts on an aside before it auto-promotes past an older main

BRANCH_COLS = ("id, session_id, name, origin_id, head_id, kind, created_at, updated_at, "
               "parent_branch_id, spawned_at_id, context_mode")

BRANCH_TITLE_RULES = (
    "Reply with ONLY a short branch title (at most 6 words, no quotes, no trailing period), "
    "most specific thing first, never starting with a verb. Example: "
    '"FNV-1a port hash check" or "SQLite WAL lock investigation".'
)

SUBAGENT_SYSTEM_NOTE = (
    "# Subagent thread\n"
    "You are a subagent thread launched by another thread in this session for the task in the user message. "
    "Work on it autonomously (nobody can answer interactive questions). Your final message is returned verbatim "
    "to the parent thread, so make it a complete, self-contained report of what you found or did."
)


def _new_id():
    return "b_" + os.urandom(4).hex()


def _prompt_text(content):
    return " ".join(
        b.get("text", "") for b in (content or [])
        if b.get("type") == "text" and not peers.is_peer_block(b) and not b.get("compaction")
    ).strip()


def _is_user_prompt(role, content):
    if role != "user" or not content:
        return False
    if all(b.get("type") == "tool_result" for b in content):
        return False
    if any(b.get("compaction") for b in content):
        return False
    if any(peers.is_peer_block(b) for b in content):
        return False
    t = _prompt_text(content)
    if t.startswith("<subagent-result") or t.startswith("<branch-result") or t.startswith("<thread-message"):
        return False
    return bool(t)


def fallback_branch_name(text):
    words = re.sub(r"\[\[.*?\]\]", " ", str(text or "")).split()
    name = " ".join(words[:5])
    if len(name) > 56:
        name = name[:53] + "..."
    return name


def _all_parents(store, sid):
    return dict(store.query("SELECT id, parent_id FROM messages WHERE session_id=?", (sid,)))


def path_ids(store, sid, head_id, parents=None):
    """Message ids from root to head_id, oldest first."""
    if head_id is None:
        return []
    if parents is None:
        parents = _all_parents(store, sid)
    out = []
    cur = head_id
    while cur is not None and cur in parents and len(out) <= len(parents):
        out.append(cur)
        cur = parents[cur]
    out.reverse()
    return out


def ensure(store, sid):
    """Ensures `sid` has at least a `main` branch and a valid `sessions.active_branch_id`.
    Returns the session's default active branch id (or None if the session does not exist)."""
    with store.lock:
        srow = store.query_one("SELECT head_id, active_branch_id, created_at, updated_at FROM sessions WHERE id=?",
                               (sid,))
        if not srow:
            return None
        s_head, active_bid, created_at, updated_at = srow
        rows = store.query("SELECT id, kind FROM branches WHERE session_id=?", (sid,))
        by_id = {r[0]: r for r in rows}
        if not rows:
            t = created_at or now_ms()
            bid = _new_id()
            store.execute(
                "INSERT INTO branches(id, session_id, name, origin_id, head_id, kind, created_at, updated_at, "
                "parent_branch_id, spawned_at_id, context_mode) "
                "VALUES(?, ?, 'Main', NULL, ?, 'main', ?, ?, NULL, NULL, 'fork')",
                (bid, sid, s_head, t, updated_at or t))
            store.execute("UPDATE sessions SET active_branch_id=? WHERE id=?", (bid, sid))
            _discover_orphans(store, sid, bid, s_head, t)
            return bid
        if active_bid not in by_id:
            main = next((r[0] for r in rows if r[1] == "main"), rows[0][0])
            store.execute("UPDATE sessions SET active_branch_id=? WHERE id=?", (main, sid))
            active_bid = main
        if not any(r[1] == "main" for r in rows):
            store.execute("UPDATE branches SET kind='main' WHERE id=?", (active_bid,))
        return active_bid


def _discover_orphans(store, sid, main_bid, main_head, fallback_t):
    """For DBs created before v16 that had edit branches in `messages`, register orphan leaf tips."""
    mrows = store.query("SELECT id, parent_id, role, content, created_at FROM messages WHERE session_id=? ORDER BY id",
                        (sid,))
    if not mrows:
        return
    parents = {r[0]: r[1] for r in mrows}
    has_child = {r[1] for r in mrows if r[1] is not None}
    main_set = set(path_ids(store, sid, main_head, parents))
    leaves = [r for r in mrows if r[0] not in has_child and r[0] != main_head]
    by_id = {r[0]: r for r in mrows}
    for leaf in leaves:
        p = path_ids(store, sid, leaf[0], parents)
        origin = None
        first_own = None
        for mid in p:
            if mid in main_set:
                origin = mid
            elif first_own is None:
                first_own = mid
        name = ""
        if first_own and first_own in by_id:
            try:
                name = fallback_branch_name(_prompt_text(json.loads(by_id[first_own][3])))
            except ValueError:
                pass
        t = leaf[4] or fallback_t
        store.execute(
            "INSERT INTO branches(id, session_id, name, origin_id, head_id, kind, created_at, updated_at, "
            "parent_branch_id, spawned_at_id, context_mode) "
            "VALUES(?, ?, ?, ?, ?, 'edit', ?, ?, ?, ?, 'fork')",
            (_new_id(), sid, name or "Previous version", origin, leaf[0], t, t, main_bid, origin))


def active_id(store, sid):
    return ensure(store, sid)


def main_id(store, sid):
    ensure(store, sid)
    row = store.query_one("SELECT id FROM branches WHERE session_id=? AND kind='main' LIMIT 1", (sid,))
    if row:
        return row[0]
    row = store.query_one("SELECT id FROM branches WHERE session_id=? ORDER BY created_at LIMIT 1", (sid,))
    return row[0] if row else None


def get_row(store, sid, bid):
    ensure(store, sid)
    return store.query_one("SELECT %s FROM branches WHERE session_id=? AND id=?" % BRANCH_COLS, (sid, bid))


def is_subagent_branch(store, sid, bid):
    if not bid:
        return False
    row = get_row(store, sid, bid)
    return bool(row and row[5] == "subagent")


def head_of(store, sid, bid=None):
    bid = bid or ensure(store, sid)
    if not bid:
        return None
    row = store.query_one("SELECT head_id FROM branches WHERE session_id=? AND id=?", (sid, bid))
    return row[0] if row else None


def resolve(store, sid, ref):
    """Resolves a branch in `sid` by id, unique id prefix, 'main', or case-insensitive title match."""
    ensure(store, sid)
    ref = str(ref or "").strip()
    if not ref:
        raise ValueError("branch id or name is required")
    rows = store.query("SELECT id, name, kind FROM branches WHERE session_id=? ORDER BY created_at", (sid,))
    if ref.lower() == "main":
        m = next((r[0] for r in rows if r[2] == "main"), None)
        if m:
            return m
    exact = [r[0] for r in rows if r[0] == ref]
    if exact:
        return exact[0]
    pref = [r[0] for r in rows if r[0].startswith(ref)]
    if len(pref) == 1:
        return pref[0]
    low = ref.lower()
    by_name = [r[0] for r in rows if (r[1] or "").lower() == low]
    if len(by_name) == 1:
        return by_name[0]
    sub = [r[0] for r in rows if low in (r[1] or "").lower()]
    if len(sub) == 1:
        return sub[0]
    if len(sub) > 1 or len(pref) > 1:
        raise ValueError("ambiguous branch %r; use the branch id" % ref)
    raise ValueError("no such branch: %s" % ref)


def resolve_global(agent, sid, ref):
    """Resolves a target thread ID across THIS session (branches/subagents by id/name/'main')
    OR across peer sessions in the same repository (by session_id `s_...` or a peer's branch_id `b_...`).
    Returns (target_sid, target_bid, scope) where scope is 'local' or 'peer'."""
    store = agent.store
    ref = str(ref or "").strip()
    if not ref:
        raise ValueError("target_id is required")
    # 1. Try local thread in this session first
    try:
        bid = resolve(store, sid, ref)
        return sid, bid, "local"
    except ValueError:
        pass
    # 2. Check if ref is a branch id belonging to another session
    brow = store.query_one("SELECT session_id, id FROM branches WHERE id=?", (ref,))
    if brow:
        tsid, tbid = brow
        if not agent.peers.can_message(sid, tsid):
            raise ValueError("%s works in another repository; you can only contact threads in this session "
                             "or sessions in this repository" % ref)
        return tsid, tbid, "peer"
    # 3. Check if ref is a peer session id (`s_...`)
    srow = store.query_one("SELECT id FROM sessions WHERE id=?", (ref,))
    if srow:
        tsid = srow[0]
        if tsid == sid:
            raise ValueError("that is this session; specify a branch_id inside it instead")
        if not agent.peers.can_message(sid, tsid):
            raise ValueError("%s works in another repository; you can only contact sessions in this repository" % ref)
        tbid = ensure(store, tsid)
        return tsid, tbid, "peer"
    raise ValueError("no such thread or session: %s (call `threads` to see available ids)" % ref)


def set_head(store, sid, bid, head_id):
    """Updates branch head_id (and sessions.head_id if bid is the session's default active branch)."""
    t = now_ms()
    with store.lock:
        store.execute("UPDATE branches SET head_id=?, updated_at=? WHERE session_id=? AND id=?",
                      (head_id, t, sid, bid))
        active = store.query_one("SELECT active_branch_id FROM sessions WHERE id=?", (sid,))
        if active and active[0] == bid:
            store.execute("UPDATE sessions SET head_id=?, updated_at=? WHERE id=?", (head_id, t, sid))
        else:
            store.execute("UPDATE sessions SET updated_at=? WHERE id=?", (t, sid))


def record_edit_branch(store, sid, active_bid, edit_msg_id, old_head_id, old_text):
    """Before chat.edit rewinds active_bid to edit_msg.parent_id, preserve the old tip as an 'edit' branch."""
    if old_head_id is None:
        return None
    erow = store.query_one("SELECT parent_id, created_at FROM messages WHERE id=? AND session_id=?",
                           (edit_msg_id, sid))
    if not erow:
        return None
    origin_id, created = erow
    dup = store.query_one("SELECT id FROM branches WHERE session_id=? AND head_id=? AND id != ?",
                          (sid, old_head_id, active_bid))
    if dup:
        return dup[0]
    bid = _new_id()
    t = now_ms()
    name = fallback_branch_name(old_text) or "Previous version"
    store.execute(
        "INSERT INTO branches(id, session_id, name, origin_id, head_id, kind, created_at, updated_at, "
        "parent_branch_id, spawned_at_id, context_mode) "
        "VALUES(?, ?, ?, ?, ?, 'edit', ?, ?, ?, ?, 'fork')",
        (bid, sid, name, origin_id, old_head_id, created or t, t, active_bid, origin_id))
    return bid


def create(agent, sid, from_message_id=None, title="", kind="aside", switch=True,
           parent_branch_id=None, context_mode="fork", spawned_at_id=None, client=None):
    """Creates a new thread inside `sid`.
    - context_mode='fork': inherits messages up to `origin_id` (`from_message_id` or parent branch's head)
    - context_mode='fresh': starts with clean context (`origin_id=None`), visually attached at `spawned_at_id`
    When `switch=True` and `client` is given, only that client switches (per-tab views); `sessions.active_branch_id`
    also updates as the default for new tabs/offline view.
    """
    agent.require(sid)
    store = agent.store
    context_mode = "fresh" if context_mode == "fresh" else "fork"
    with store.lock:
        cur_bid = parent_branch_id or (client.branch_of.get(sid) if client else None) or ensure(store, sid)
        cur_head = head_of(store, sid, cur_bid)
        if context_mode == "fresh":
            origin_id = None
            attach_id = spawned_at_id if spawned_at_id is not None else cur_head
        else:
            if from_message_id is None:
                origin_id = cur_head
            elif from_message_id == 0:
                origin_id = None
            else:
                row = store.query_one("SELECT id FROM messages WHERE id=? AND session_id=?",
                                      (int(from_message_id), sid))
                if not row:
                    raise ValueError("no such message: %s" % from_message_id)
                origin_id = row[0]
            attach_id = spawned_at_id if spawned_at_id is not None else origin_id
        bid = _new_id()
        t = now_ms()
        name = str(title or "").strip()[:120]
        store.execute(
            "INSERT INTO branches(id, session_id, name, origin_id, head_id, kind, created_at, updated_at, "
            "parent_branch_id, spawned_at_id, context_mode) "
            "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (bid, sid, name, origin_id, origin_id, kind, t, t, cur_bid, attach_id, context_mode))
        if switch:
            store.execute("UPDATE sessions SET active_branch_id=?, head_id=?, updated_at=? WHERE id=?",
                          (bid, origin_id, t, sid))
            if client is not None:
                client.branch_of[sid] = bid
        else:
            store.execute("UPDATE sessions SET updated_at=? WHERE id=?", (t, sid))
    if switch:
        emit_switched(agent, sid, bid, client=client)
    emit_updated(agent, sid)
    agent.updated(sid)
    info = tree_info(agent, sid, active_bid=bid)
    return next(b for b in info["branches"] if b["id"] == bid)


def switch(agent, sid, branch_ref, client=None):
    """Switches the requesting client's viewed thread (per-tab!). Also updates sessions.active_branch_id
    as the default for newly opened tabs and offline snapshots. Returns snapshot for `bid`."""
    agent.require(sid)
    store = agent.store
    with store.lock:
        bid = resolve(store, sid, branch_ref)
        row = get_row(store, sid, bid)
        if not row:
            raise ValueError("no such branch: %s" % branch_ref)
        store.execute("UPDATE sessions SET active_branch_id=?, head_id=? WHERE id=?", (bid, row[4], sid))
        if client is not None:
            client.branch_of[sid] = bid
    emit_switched(agent, sid, bid, client=client)
    emit_updated(agent, sid)
    agent.updated(sid)
    return agent.snapshot(sid, branch_id=bid)


def rename(agent, sid, branch_ref, title):
    agent.require(sid)
    store = agent.store
    title = str(title or "").strip()[:120]
    if not title:
        raise ValueError("branch title cannot be empty")
    with store.lock:
        bid = resolve(store, sid, branch_ref)
        store.execute("UPDATE branches SET name=?, updated_at=? WHERE session_id=? AND id=?",
                      (title, now_ms(), sid, bid))
    emit_updated(agent, sid)
    agent.updated(sid)
    return next(b for b in tree_info(agent, sid)["branches"] if b["id"] == bid)


def promote(agent, sid, branch_ref, auto=False):
    """Makes `branch_ref` the new `main` branch; demotes the previous main to `aside`."""
    agent.require(sid)
    store = agent.store
    with store.lock:
        bid = resolve(store, sid, branch_ref)
        row = get_row(store, sid, bid)
        if not row:
            raise ValueError("no such branch: %s" % branch_ref)
        if row[5] == "main":
            return next(b for b in tree_info(agent, sid)["branches"] if b["id"] == bid)
        old_main = store.query_one(
            "SELECT id, name, head_id FROM branches WHERE session_id=? AND kind='main'", (sid,))
        t = now_ms()
        if old_main:
            om_id, om_name, om_head = old_main
            new_old_name = om_name
            if not om_name or om_name.lower() == "main":
                new_old_name = "Previous main"
            store.execute("UPDATE branches SET kind='aside', name=?, updated_at=? WHERE id=?",
                          (new_old_name, t, om_id))
        store.execute("UPDATE branches SET kind='main', updated_at=? WHERE id=?", (t, bid))
    if auto:
        bname = row[2] or bid
        agent.server.emit(sid, "notice", {
            "session_id": sid, "branch_id": bid, "level": "info",
            "text": "Branch **%s** is now the main conversation (the previous main branch is kept as an aside)."
                    % bname})
    emit_updated(agent, sid)
    agent.updated(sid)
    return next(b for b in tree_info(agent, sid)["branches"] if b["id"] == bid)


def maybe_auto_promote(agent, sid, bid):
    """If the user keeps chatting on an aside (>= PROMOTE_PROMPTS user prompts after origin, and
    strictly deeper past the fork point than the current main), promote it to be the new main.
    Subagent threads never auto-promote."""
    store = agent.store
    row = get_row(store, sid, bid)
    if not row or row[5] != "aside":
        return False
    origin_id, head_id = row[3], row[4]
    if head_id is None or head_id == origin_id:
        return False
    m_bid = main_id(store, sid)
    if not m_bid or m_bid == bid:
        return False
    mrow = get_row(store, sid, m_bid)
    if not mrow:
        return False
    parents = _all_parents(store, sid)
    b_path = path_ids(store, sid, head_id, parents)
    m_path = path_ids(store, sid, mrow[4], parents)
    if origin_id in b_path:
        own_ids = b_path[b_path.index(origin_id) + 1:]
    else:
        own_ids = list(b_path)
    if not own_ids:
        return False
    ph = ",".join("?" for _ in own_ids)
    m_rows = store.query("SELECT role, content FROM messages WHERE id IN (%s)" % ph, tuple(own_ids))
    prompts = sum(1 for r, c in m_rows if _is_user_prompt(r, json.loads(c)))
    if prompts < PROMOTE_PROMPTS:
        return False
    m_set = set(m_path)
    lca = next((mid for mid in reversed(b_path) if mid in m_set), None)
    b_after = len(b_path) - (b_path.index(lca) + 1) if lca is not None else len(b_path)
    m_after = len(m_path) - (m_path.index(lca) + 1) if lca is not None else len(m_path)
    if b_after > m_after:
        promote(agent, sid, bid, auto=True)
        return True
    return False


async def delete(agent, sid, branch_ref):
    """Deletes a thread (cannot delete the only thread). Prunes messages not on any remaining thread."""
    agent.require(sid)
    store = agent.store
    bid = resolve(store, sid, branch_ref)
    rows = store.query("SELECT id, kind FROM branches WHERE session_id=?", (sid,))
    if len(rows) <= 1:
        raise ValueError("cannot delete the only branch of a session")
    await agent.stop_branch(sid, bid)
    was_active = active_id(store, sid) == bid
    was_main = any(r[0] == bid and r[1] == "main" for r in rows)
    with store.lock:
        store.execute("DELETE FROM branches WHERE session_id=? AND id=?", (sid, bid))
        remaining = store.query("SELECT id, head_id, kind FROM branches WHERE session_id=? ORDER BY created_at",
                                (sid,))
        fallback = next((r for r in remaining if r[2] == "main"), remaining[0])
        if was_main:
            store.execute("UPDATE branches SET kind='main' WHERE id=?", (fallback[0],))
        if was_active:
            store.execute("UPDATE sessions SET active_branch_id=?, head_id=? WHERE id=?",
                          (fallback[0], fallback[1], sid))
        parents = _all_parents(store, sid)
        keep = set()
        for r in store.query("SELECT origin_id, head_id, spawned_at_id FROM branches WHERE session_id=?", (sid,)):
            for col in r:
                if col is not None:
                    keep.update(path_ids(store, sid, col, parents))
        orphan_ids = [mid for mid in parents if mid not in keep]
        if orphan_ids:
            ph = ",".join("?" for _ in orphan_ids)
            store.execute("DELETE FROM messages WHERE session_id=? AND id IN (%s)" % ph,
                          (sid,) + tuple(orphan_ids))
    agent.brt.pop(bid, None)
    agent.peers.forget_thread(bid)
    # Any client currently viewing the deleted branch gets switched to fallback
    for c in list(agent.server.clients):
        if sid in c.subs and c.branch_of.get(sid) == bid:
            c.branch_of[sid] = fallback[0]
            snap = agent.snapshot(sid, branch_id=fallback[0])
            c.event("branch.switched", dict(snap, session_id=sid))
    emit_updated(agent, sid)
    agent.updated(sid)
    return True


# --- tree / fork metadata for clients & agents ------------------------------------------------

def compact_threads(agent, sid):
    """Lightweight thread list included in `sessions.list` / `session.updated` for the expandable
    left sidebar tree (no message scans beyond branch rows)."""
    store = agent.store
    active_bid = ensure(store, sid)
    if not active_bid:
        return []
    brows = store.query(
        "SELECT id, name, origin_id, head_id, kind, created_at, updated_at, "
        "parent_branch_id, spawned_at_id, context_mode "
        "FROM branches WHERE session_id=? ORDER BY CASE WHEN kind='main' THEN 0 ELSE 1 END, created_at, id",
        (sid,))
    if len(brows) <= 1:
        r = brows[0]
        return [{"id": r[0], "name": r[1] or "Main", "kind": r[4] or "main",
                 "context_mode": r[9] or "fork", "parent_branch_id": None,
                 "status": "running" if agent.branch_running(r[0]) else "idle",
                 "is_main": True}]
    # Use full tree_info's parent resolution so sidebar tree matches minimap tree 1:1
    info = tree_info(agent, sid, active_bid=active_bid)
    return [{k: b[k] for k in ("id", "name", "kind", "context_mode", "parent_branch_id",
                               "status", "own_messages", "is_main", "origin_depth")}
            for b in info["branches"]]


def tree_info(agent, sid, active_bid=None):
    """Full thread tree & inline fork/spawn points relative to `active_bid` (per-tab!)."""
    store = agent.store
    default_bid = ensure(store, sid)
    if not default_bid:
        return {"active_branch_id": None, "main_branch_id": None, "branches": [], "forks": {}}
    with store.lock:
        brows = store.query(
            "SELECT %s FROM branches WHERE session_id=? "
            "ORDER BY CASE WHEN kind='main' THEN 0 ELSE 1 END, created_at, id" % BRANCH_COLS,
            (sid,))
        mrows = store.query("SELECT id, parent_id, role, content FROM messages WHERE session_id=?", (sid,))
    by_id = {r[0]: r for r in brows}
    if not active_bid or active_bid not in by_id:
        active_bid = default_bid

    parents = {r[0]: r[1] for r in mrows}
    msg_meta = {r[0]: (r[2], r[3]) for r in mrows}

    paths = {}
    own_sets = {}
    for r in brows:
        bid = r[0]
        origin_id = r[3]
        head_id = r[4]
        p = path_ids(store, sid, head_id, parents)
        paths[bid] = p
        if origin_id is not None and origin_id in p:
            own_sets[bid] = set(p[p.index(origin_id) + 1:])
        else:
            own_sets[bid] = set(p)

    active_path = paths.get(active_bid, [])
    active_set = set(active_path)
    m_bid = next((r[0] for r in brows if r[5] == "main"), brows[0][0] if brows else None)

    # Resolve parent_branch_id: prefer stored parent_branch_id if valid; else owner of spawned_at_id/origin_id
    parent_branch = {}
    for r in brows:
        bid, _, _, origin_id, _, kind, _, _, stored_pb, spawned_at, ctx_mode = r
        if kind == "main":
            parent_branch[bid] = None
            continue
        if stored_pb and stored_pb != bid and stored_pb in by_id:
            parent_branch[bid] = stored_pb
            continue
        anchor = spawned_at if spawned_at is not None else origin_id
        if anchor is None:
            parent_branch[bid] = m_bid if bid != m_bid else None
            continue
        owner = next((o[0] for o in brows if o[0] != bid and anchor in own_sets[o[0]]), None)
        if owner is None:
            owner = next((o[0] for o in brows if o[0] != bid and anchor in paths[o[0]]), None)
        parent_branch[bid] = owner or (m_bid if bid != m_bid else None)

    sub_counts = {r[0]: 0 for r in brows}
    for r in brows:
        p = parent_branch.get(r[0])
        seen = {r[0]}
        while p and p not in seen:
            seen.add(p)
            sub_counts[p] = sub_counts.get(p, 0) + 1
            p = parent_branch.get(p)

    summaries = []
    sum_by_id = {}
    for r in brows:
        bid, _, name, origin_id, head_id, kind, created_at, updated_at, _, spawned_at, ctx_mode = r
        p = paths[bid]
        ctx_mode = ctx_mode or ("fresh" if (kind == "subagent" and origin_id is None) else "fork")
        attach_id = spawned_at if spawned_at is not None else origin_id
        if origin_id is not None and origin_id in p:
            own = p[p.index(origin_id) + 1:]
        else:
            own = list(p)
        # Depth of the visual attachment point along parent's path (for conversation-order sorting)
        pb = parent_branch.get(bid)
        pp = paths.get(pb, []) if pb else []
        if attach_id is not None and attach_id in pp:
            origin_depth = pp.index(attach_id) + 1
        elif origin_id is not None and origin_id in p:
            origin_depth = p.index(origin_id) + 1
        else:
            origin_depth = 0
        prompts = sum(1 for mid in own if mid in msg_meta and _is_user_prompt(msg_meta[mid][0], json.loads(msg_meta[mid][1])))
        fork_preview = ""
        if attach_id and attach_id in msg_meta:
            t = _prompt_text(json.loads(msg_meta[attach_id][1]))
            fork_preview = " ".join(t.split())[:80]
        is_running = agent.branch_running(bid)
        default_name = "Main" if kind == "main" else ("Subagent" if kind == "subagent" else "New aside")
        s = {
            "id": bid,
            "name": name or default_name,
            "unnamed": not bool(name),
            "kind": kind or "aside",
            "context_mode": ctx_mode,
            "origin_id": origin_id,
            "spawned_at_id": attach_id,
            "origin_depth": origin_depth,
            "fork_preview": fork_preview,
            "parent_branch_id": parent_branch.get(bid),
            "head_id": head_id,
            "status": "running" if is_running else "idle",
            "own_messages": len(own),
            "total_messages": len(p),
            "prompt_count": prompts,
            "sub_branches": sub_counts.get(bid, 0),
            "is_active": bid == active_bid,
            "is_main": kind == "main",
            "created_at": created_at,
            "updated_at": updated_at,
        }
        summaries.append(s)
        sum_by_id[bid] = s

    # Compute inline fork/spawn points along `active_path`
    forks = {}
    grouped = {}
    for r in brows:
        bid = r[0]
        if bid == active_bid:
            continue
        s = sum_by_id[bid]
        if s["context_mode"] == "fresh":
            # Fresh-context subagent attaches directly at its `spawned_at_id` if on active_path
            attach = s["spawned_at_id"]
            if attach is not None and attach in active_set:
                key = str(attach)
                grouped.setdefault((key, ("fresh", bid)), []).append(bid)
            elif parent_branch.get(bid) == active_bid:
                grouped.setdefault(("root", ("fresh", bid)), []).append(bid)
            continue
        # Forked thread: find LCA with active_path
        p = paths[bid]
        lca = next((mid for mid in reversed(p) if mid in active_set), None)
        # For a forked subagent whose spawned_at_id is on active_path (one message after origin_id), attach visually at spawned_at_id
        if s["kind"] == "subagent" and s["spawned_at_id"] in active_set:
            key = str(s["spawned_at_id"])
            grouped.setdefault((key, ("sub", bid)), []).append(bid)
            continue
        key = str(lca) if lca is not None else "root"
        if lca is None:
            fa = p[0] if p else None
        else:
            idx = p.index(lca)
            fa = p[idx + 1] if idx + 1 < len(p) else None
        gkey = (key, ("msg", fa) if fa is not None else ("self", bid))
        grouped.setdefault(gkey, []).append(bid)

    for (key, _), bids in grouped.items():
        def rank(bid):
            s = sum_by_id[bid]
            return (0 if s["is_main"] else 1,
                    0 if s["origin_id"] == (None if key == "root" else int(key)) else 1,
                    s["created_at"] or 0)
        bids.sort(key=rank)
        rep = sum_by_id[bids[0]]
        extra = len(bids) - 1
        chip = {
            "branch_id": rep["id"],
            "name": rep["name"],
            "kind": rep["kind"],
            "context_mode": rep["context_mode"],
            "status": "running" if any(sum_by_id[x]["status"] == "running" for x in bids) else rep["status"],
            "own_messages": rep["own_messages"],
            "is_main": rep["is_main"],
            "nested": extra,
        }
        forks.setdefault(key, []).append(chip)

    for key in forks:
        forks[key].sort(key=lambda c: (0 if c["is_main"] else 1 if c["kind"] == "aside" else 2 if c["kind"] == "subagent" else 3,
                                       sum_by_id[c["branch_id"]]["created_at"] or 0))

    active_sum = sum_by_id.get(active_bid)
    return {
        "active_branch_id": active_bid,
        "main_branch_id": m_bid,
        "active_origin_id": active_sum["origin_id"] if active_sum else None,
        "active_context_mode": active_sum["context_mode"] if active_sum else "fork",
        "branches": summaries,
        "forks": forks,
    }


def all_branch_data(agent, sid):
    """For offline snapshots: returns {info, threads, messages} (deduplicated by message id)."""
    info = tree_info(agent, sid)
    if len(info["branches"]) <= 1:
        return None
    store = agent.store
    parents = _all_parents(store, sid)
    threads = {}
    needed = set()
    for b in info["branches"]:
        ids = path_ids(store, sid, b["head_id"], parents)
        threads[b["id"]] = ids
        needed.update(ids)
    from .agent import MSG_COLS, message_dict, is_prompt
    rows = {r[0]: r for r in store.query("SELECT %s FROM messages WHERE session_id=?" % MSG_COLS, (sid,))}
    counts = {}
    for r in rows.values():
        if r[2] == "user" and is_prompt(json.loads(r[3])):
            counts[r[5]] = counts.get(r[5], 0) + 1
    msgs = {str(mid): message_dict(rows[mid], counts.get(rows[mid][5])) for mid in needed if mid in rows}
    info = dict(info, branches=[dict(b, status="idle") for b in info["branches"]],
                forks={k: [dict(c, status="idle") for c in v] for k, v in info["forks"].items()})
    return {"info": info, "threads": threads, "messages": msgs}


def emit_updated(agent, sid):
    """Sends per-tab `branches.updated` so each subscribed client keeps its own viewed branch active."""
    for c in list(agent.server.clients):
        if sid in c.subs:
            cbid = c.branch_of.get(sid)
            c.event("branches.updated", {"session_id": sid, "branches": tree_info(agent, sid, active_bid=cbid)})


def emit_switched(agent, sid, bid, client=None):
    """Emits `branch.switched` only to `client` (per-tab!), or to all subscribers when client is None."""
    snap = agent.snapshot(sid, branch_id=bid)
    payload = dict(snap, session_id=sid)
    if client is not None:
        client.event("branch.switched", payload)
    else:
        for c in list(agent.server.clients):
            if sid in c.subs:
                c.branch_of[sid] = bid
                c.event("branch.switched", payload)


# --- auto-titling for aside branches ----------------------------------------------------------

async def retitle(agent, sid, bid):
    """Generates a short title for an aside branch (cheap model, or fallback_branch_name)."""
    row = get_row(agent.store, sid, bid)
    if not row or row[5] not in ("aside",):
        return
    rt = agent.branch_rt(bid)
    rt.title_gen = gen = getattr(rt, "title_gen", 0) + 1
    origin_id = row[3]
    msgs = agent.messages(sid, branch_id=bid)
    if origin_id is not None:
        idx = next((i for i, m in enumerate(msgs) if m["id"] == origin_id), -1)
        own = msgs[idx + 1:] if idx >= 0 else msgs
    else:
        own = msgs
    convo = []
    first_user = ""
    for m in own:
        for b in m["content"]:
            if b.get("type") == "text" and not peers.is_peer_block(b) and not b.get("compaction"):
                t = b.get("text", "")
                if not t.startswith("<subagent-result") and not t.startswith("<branch-result") and not t.startswith("<thread-message"):
                    convo.append("%s: %s" % (m["role"], t))
                    if m["role"] == "user" and not first_user:
                        first_user = t
    if not first_user:
        return
    title = fallback_branch_name(first_user) or "Aside"
    settings = agent.settings()
    try:
        provider, _ = agent.session_llm(sid, settings)
    except llm.LLMError:
        provider = {"type": "mock"}
    if provider["type"] != "mock" and convo:
        prompt = "%s\n\n<branch>\n%s\n</branch>" % (BRANCH_TITLE_RULES, "\n".join(convo)[:6000])
        try:
            used = {}
            from .agent import in_thread
            try:
                out = await in_thread(llm.complete, provider, agent.server.creds, prompt,
                                      provider["cheap_model"], 120, settings, used)
            finally:
                if used and agent.summary(sid):
                    agent.add_usage(sid, await agent.price(provider, provider["cheap_model"], used))
            cand = out.strip().splitlines()[0].strip().strip("\"'`").rstrip(".").strip()[:120]
            if cand:
                title = cand
        except Exception:
            pass
    if not get_row(agent.store, sid, bid) or getattr(agent.branch_rt(bid), "title_gen", gen) != gen:
        return
    agent.store.execute("UPDATE branches SET name=? WHERE session_id=? AND id=?", (title, sid, bid))
    emit_updated(agent, sid)
    agent.updated(sid)


# --- agent awareness & unified tools (threads, spawn, ask, message) ---------------------------

def system_note(agent, sid, current_bid=None):
    """Appends subagent instructions (if on a subagent thread) and metadata-only awareness of other
    branches/subagents in this session."""
    info = tree_info(agent, sid, active_bid=current_bid)
    cur_bid = current_bid or info["active_branch_id"]
    cur = next((b for b in info["branches"] if b["id"] == cur_bid), None)
    parts = []
    if cur and cur["kind"] == "subagent":
        parts.append(SUBAGENT_SYSTEM_NOTE)
    others = [b for b in info["branches"]
              if b["id"] != cur_bid and b["kind"] in ("main", "aside", "subagent")
              and (b["own_messages"] > 0 or b["status"] == "running")]
    if others:
        cur_desc = '"%s" (%s, id=%s)' % (cur["name"], cur["kind"], cur["id"]) if cur else str(cur_bid)
        lines = [
            "# Session threads (metadata only)",
            "You are currently on thread %s of this session. Other threads exist in this session:" % cur_desc,
        ]
        for b in sorted(others, key=lambda x: x["id"]):
            fork = ("after msg #%d" % b["spawned_at_id"]) if b["spawned_at_id"] else "from root"
            ctx = "fresh context" if b["context_mode"] == "fresh" else "forked context"
            st = " [running]" if b["status"] == "running" else ""
            lines.append('- %s "%s" (%s, %s, %s, %d msgs%s)' % (
                b["id"], b["name"], b["kind"], ctx, fork, b["own_messages"], st))
        lines.append(
            "\nIMPORTANT: Do NOT read full side-branches (user asides) or contact them on your own. Only look at "
            "this metadata (or call `threads` for fresh status) and ask the user if you should check in — unless "
            "the user proactively tells you to look at or talk to one. When checking any thread (branch, subagent, "
            "or peer session), use `ask` for a quick read-only one-shot question (like /btw, doesn't modify that "
            "thread) or `message` to send it a task/message."
        )
        parts.append("\n".join(lines))
    return ("\n\n" + "\n\n".join(parts)) if parts else ""


def branches_text(agent, sid, current_bid=None):
    """Formatted metadata-only listing of all threads in this session (for `/branches`)."""
    info = tree_info(agent, sid, active_bid=current_bid)
    cur_bid = current_bid or info["active_branch_id"]
    out = ["Threads in this session (metadata only):"]
    for b in info["branches"]:
        tags = [b["kind"], b["context_mode"]]
        if b["id"] == cur_bid:
            tags.append("current")
        if b["status"] == "running":
            tags.append("running")
        fork = ("at msg #%d" % b["spawned_at_id"]) if b["spawned_at_id"] else "root"
        if b["fork_preview"]:
            fork += ' ("%s")' % b["fork_preview"]
        out.append('- %s "%s" (%s) — %s · %d own msg%s · updated %s' % (
            b["id"], b["name"], ", ".join(tags), fork, b["own_messages"],
            "" if b["own_messages"] == 1 else "s", peers.ago(b["updated_at"] or b["created_at"] or now_ms())))
    if len(info["branches"]) > 1:
        out.append("\nDo not query or message user side-branches unless the user asked you to or agreed you should.")
    return "\n".join(out)


def threads_text(agent, sid, current_bid=None):
    """Unified roster for the `threads` tool: this session's threads + peer sessions in this repo."""
    return branches_text(agent, sid, current_bid) + "\n\n" + agent.peers.peers_text(sid)


def tool_defs(store, sid, bid=None):
    """Tools exposed to the model on `(sid, bid)`. Subagent threads cannot recursively `spawn`."""
    if is_subagent_branch(store, sid, bid):
        return [d for d in tools.DEFINITIONS if d["name"] != "spawn"]
    return tools.DEFINITIONS


async def spawn(agent, sid, current_bid, description, prompt, context="fresh"):
    """Spawns a background subagent thread inside this session (`context`: 'fresh' | 'fork').
    Returns immediately with {thread_id, branch_id, session_id, title, context, status}."""
    prompt = str(prompt or "").strip()
    if not prompt:
        raise tools.ToolError("prompt is required")
    if is_subagent_branch(agent.store, sid, current_bid):
        raise tools.ToolError("subagents are not available here (subagent threads cannot spawn subagents)")
    context = str(context or "fresh").strip().lower()
    if context not in ("fresh", "fork"):
        raise tools.ToolError("context must be 'fresh' or 'fork'")
    cur_head = head_of(agent.store, sid, current_bid)
    # For a forked subagent: attach visually at cur_head (the assistant message that called spawn),
    # and inherit LLM context from cur_head's parent (so the forked thread never inherits an unresolved tool_use!).
    if context == "fork":
        prow = agent.store.query_one("SELECT parent_id, content FROM messages WHERE id=? AND session_id=?",
                                     (cur_head, sid)) if cur_head else None
        if prow and any(b.get("type") == "tool_use" for b in json.loads(prow[1])):
            fork_origin = prow[0] if prow[0] is not None else 0
        else:
            fork_origin = cur_head
    else:
        fork_origin = None
    title = str(description or "").strip()[:120] or fallback_branch_name(prompt) or "Subagent"
    b = create(agent, sid, from_message_id=fork_origin, title=title, kind="subagent",
               switch=False, parent_branch_id=current_bid, context_mode=context, spawned_at_id=cur_head)
    bid = b["id"]
    brt = agent.branch_rt(bid)
    brt.notify_branch = current_bid
    brt.sub_description = title
    queued = await agent.send(sid, prompt, branch_id=bid, from_branch=True)
    emit_updated(agent, sid)
    agent.updated(sid)
    return json.dumps({"thread_id": bid, "branch_id": bid, "session_id": sid, "title": title,
                       "context": context, "status": "queued" if queued else "started"})


async def ask(agent, sid, current_bid, target_ref, question):
    """Unified one-shot read-only /btw-style question against ANY thread: a branch/subagent in this
    session, OR a peer session in the same repository (runs immediately without gating/mutation!)."""
    question = str(question or "").strip()
    if not question:
        raise tools.ToolError("question is required")
    try:
        target_sid, target_bid, scope = resolve_global(agent, sid, target_ref)
    except ValueError as e:
        raise tools.ToolError(str(e))
    if target_sid == sid and target_bid == current_bid:
        raise tools.ToolError("that is the current thread; answer directly from your own context")
    session = agent.require(sid)
    target_sess = agent.require(target_sid)
    settings = agent.settings()
    provider, model = agent.session_llm(sid, settings)
    history = agent.llm_history(target_sid, branch_id=target_bid) + [
        {"role": "user", "content": [{"type": "text", "text": question}]}]
    target_row = get_row(agent.store, target_sid, target_bid)
    bname = (target_row[2] if target_row else "") or target_bid
    label = bname if scope == "local" else (target_sess["title"] or target_sid)
    where = ("another branch of this session" if scope == "local" else "another session in this repository")
    system = (skills.system_prompt(settings, target_sess["cwd"], agent.server.html_path) +
              "\n\n# Side question from %s\nA peer thread asks a quick one-shot question about your conversation "
              "(%s). Answer directly and concisely from this conversation's context; you cannot use tools for "
              "this answer." % (where, label))

    from .agent import in_thread

    def run_stream():
        text_parts = []
        usage = None
        s = llm.retrying_stream(settings, provider, agent.server.creds, model, system, history, None,
                                settings["max_tokens"], 0)
        for ev in s:
            if ev[0] == "delta" and ev[2] == "text":
                text_parts.append(ev[3])
            elif ev[0] == "retry":
                text_parts.clear()
            elif ev[0] == "usage":
                usage = ev[1]
        return "".join(text_parts).strip(), usage

    answer, usage = await in_thread(run_stream)
    if usage and agent.summary(sid):
        agent.add_usage(sid, await agent.price(provider, model, usage))
    target_id = target_bid if scope == "local" else target_sid
    return '[Answer from %s "%s"]\n%s' % (target_id, label, answer or "(empty answer)")


async def message(agent, sid, current_bid, target_ref, text, kind=None, reply_to=None):
    """Unified async messaging to ANY thread:
    - Local branch/subagent in this session: starts/queues a turn on that thread (with tools, extending
      its history). Unless kind=='info', delivers a <thread-message kind="result"> back when it finishes.
    - Peer session in this repository: delivers via peers.Hub inbox at step boundary."""
    text = str(text or "").strip()
    if not text:
        raise tools.ToolError("text is required")
    try:
        target_sid, target_bid, scope = resolve_global(agent, sid, target_ref)
    except ValueError as e:
        raise tools.ToolError(str(e))
    if target_sid == sid and target_bid == current_bid:
        raise tools.ToolError("cannot message the current thread")
    if scope == "peer":
        # If targeting a specific subagent thread inside a peer session, wake it directly without reporting to its parent
        target_row = get_row(agent.store, target_sid, target_bid)
        if target_row and target_row[5] == "subagent" and target_ref == target_bid:
            q_text = '<peer-message from=%s title=%s kind=%s>\n%s\n</peer-message>' % (
                quoteattr(sid), quoteattr(agent.peers.title(sid)), quoteattr(kind or "question"), text)
            queued = await agent.send(target_sid, q_text, branch_id=target_bid, from_branch=True)
            return "Sent to subagent thread %s in %s: %s." % (
                target_bid, target_sid, "queued" if queued else "it was idle and now starts a turn to answer")
        return agent.peers.send(sid, target_sid, text, kind, reply_to)
    # Local thread in this session
    cur_rt = agent.branch_rt(current_bid)
    if getattr(cur_rt, "notify_branch", None) == target_bid:
        raise tools.ToolError("thread %s is already waiting for your reply; finish your turn to respond to it"
                              % target_bid)
    target_row = get_row(agent.store, sid, target_bid)
    bname = (target_row[2] if target_row else "") or target_bid
    trt = agent.branch_rt(target_bid)
    if kind != "info":
        trt.notify_branch = current_bid
    queued = await agent.send(sid, text, branch_id=target_bid, from_branch=True)
    emit_updated(agent, sid)
    return json.dumps({"thread_id": target_bid, "branch_id": target_bid, "title": bname,
                       "status": "queued" if queued else "started"})


def final_text(agent, sid, bid):
    for m in reversed(agent.messages(sid, branch_id=bid)):
        if m["role"] != "assistant":
            continue
        texts = [b.get("text", "") for b in m["content"] if b.get("type") == "text" and b.get("text")]
        if texts:
            return "\n".join(texts)
    return ""


def turn_finished(agent, sid, bid, rt, error=None):
    """When a thread turn started by `spawn` or `message` finishes, deliver <thread-message> back."""
    caller_bid = getattr(rt, "notify_branch", None)
    if not caller_bid:
        return
    rt.notify_branch = None
    if not get_row(agent.store, sid, caller_bid):
        return
    row = get_row(agent.store, sid, bid)
    title = getattr(rt, "sub_description", None) or (row[2] if row else "") or bid
    kind = (row[5] if row else None) or "aside"
    if rt.cancel is not None and rt.cancel.is_set():
        status = "stopped"
    elif error is not None:
        status = "error"
    else:
        status = "completed"
    text = final_text(agent, sid, bid)
    if status == "error" and error is not None:
        text = (text + "\n\n" if text else "") + "Error: %s" % error
    msg = "<thread-message from=%s title=%s kind=%s status=%s>%s</thread-message>" % (
        quoteattr(bid), quoteattr(title), quoteattr(kind), quoteattr(status), text)

    async def deliver():
        if agent.summary(sid) and get_row(agent.store, sid, caller_bid):
            await agent.send(sid, msg, branch_id=caller_bid, from_branch=True)
    agent.spawn(deliver())


class ToolBridge:
    """Unified thread tools' access from the tool worker thread onto the asyncio event loop."""

    def __init__(self, agent, sid, bid, loop):
        self.agent, self.sid, self.bid, self.loop = agent, sid, bid, loop

    def list_threads(self):
        async def run():
            return threads_text(self.agent, self.sid, self.bid)
        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(30)

    def list_branches(self):
        async def run():
            return branches_text(self.agent, self.sid, self.bid)
        return asyncio.run_coroutine_threadsafe(run(), self.loop).result(30)

    def spawn(self, description, prompt, context="fresh"):
        return asyncio.run_coroutine_threadsafe(
            spawn(self.agent, self.sid, self.bid, description, prompt, context), self.loop).result(30)

    def ask(self, target_id, question):
        return asyncio.run_coroutine_threadsafe(
            ask(self.agent, self.sid, self.bid, target_id, question), self.loop).result(300)

    def message(self, target_id, text, kind=None, reply_to=None):
        return asyncio.run_coroutine_threadsafe(
            message(self.agent, self.sid, self.bid, target_id, text, kind, reply_to), self.loop).result(30)
