"""Subagents: background child sessions launched by the `subagent` tool; their final message is delivered
back to the parent session as a <subagent-result> user message through the parent's queue."""
import asyncio
import json
from xml.sax.saxutils import quoteattr

from . import tools

SYSTEM_NOTE = ("# Subagent\nYou are a subagent: a background session launched by another agent for the task in "
               "the first user message. Work on it autonomously (nobody can answer questions). Your final message "
               "is returned verbatim to the parent agent, so make it a complete, self-contained report of what you "
               "found or did.")


def info(store, sid):
    row = store.query_one("SELECT parent_id, kind FROM sessions WHERE id=?", (sid,))
    parent, kind = (row or (None, None))
    children = store.query_one("SELECT COUNT(*) FROM sessions WHERE parent_id=?", (sid,))[0]
    return {"parent_id": parent, "kind": kind or "chat", "children": children}


def is_child(store, sid):
    row = store.query_one("SELECT parent_id FROM sessions WHERE id=?", (sid,))
    return bool(row and row[0])


def tool_defs(store, sid):
    if is_child(store, sid):
        return [d for d in tools.DEFINITIONS if d["name"] != "subagent"]
    return tools.DEFINITIONS


def system_note(store, sid):
    return "\n\n" + SYSTEM_NOTE if is_child(store, sid) else ""


def attach(agent, sid, ctx, loop):
    """Gives a parent session's tool context the ability to launch children (called on the event loop)."""
    if is_child(agent.store, sid):
        return

    def launch(description, prompt, session_id=None):
        fut = asyncio.run_coroutine_threadsafe(start(agent, sid, str(description or ""), str(prompt or ""),
                                                     session_id), loop)
        return json.dumps(fut.result(30))
    ctx.subagent = launch


async def start(agent, parent, description, prompt, session_id=None):
    if not prompt.strip():
        raise tools.ToolError("prompt is required")
    store = agent.store
    if session_id:
        if not is_child(store, session_id) or info(store, session_id)["parent_id"] != parent:
            raise tools.ToolError("%s is not a subagent of this session" % session_id)
        if description:
            store.execute("UPDATE sessions SET title=? WHERE id=?", (description[:200], session_id))
        sid = session_id
    else:
        cwd = agent.require(parent)["cwd"]
        sid = agent.create(cwd, remember=False)["id"]
        # the child uses the same provider/model/effort choice as its parent (NULL = follow the global setting)
        store.execute("UPDATE sessions SET parent_id=?, kind='subagent', title=?, "
                      "provider=(SELECT provider FROM sessions WHERE id=?), model=(SELECT model FROM sessions WHERE id=?), "
                      "effort=(SELECT effort FROM sessions WHERE id=?) "
                      "WHERE id=?", (parent, description[:200], parent, parent, parent, sid))
    agent.rt[sid].sub_description = description or getattr(agent.rt[sid], "sub_description", "") or ""
    queued = await agent.send(sid, prompt)
    agent.updated(sid)
    agent.updated(parent)
    return {"session_id": sid, "status": "queued" if queued else "started"}


def final_text(agent, sid):
    for m in reversed(agent.messages(sid)):
        if m["role"] != "assistant":
            continue
        texts = [b.get("text", "") for b in m["content"] if b.get("type") == "text" and b.get("text")]
        if texts:
            return "\n".join(texts)
    return ""


def turn_finished(agent, sid, rt, error=None):
    """Called when a session's run loop ends (queue drained). Signals the parent of a child session."""
    row = agent.store.query_one("SELECT parent_id, title FROM sessions WHERE id=?", (sid,))
    if not row or not row[0]:
        return
    parent = row[0]
    if rt.cancel is not None and rt.cancel.is_set():
        status = "stopped"
    elif error is not None:
        status = "error"
    else:
        status = "completed"
    text = final_text(agent, sid)
    if status == "error" and error is not None:
        text = (text + "\n\n" if text else "") + "Error: %s" % error
    description = getattr(rt, "sub_description", None) or row[1] or ""
    msg = "<subagent-result session_id=%s description=%s status=%s>%s</subagent-result>" % (
        quoteattr(sid), quoteattr(description), quoteattr(status), text)

    async def deliver():
        if agent.summary(parent):
            await agent.send(parent, msg)
    agent.spawn(deliver())
