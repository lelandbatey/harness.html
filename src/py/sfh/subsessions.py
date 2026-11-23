"""Subsessions (/subsession): user-owned child sessions. A subsession starts with an empty context in its
parent's working directory (and with its provider/model/effort), but is otherwise a full session of its own:
the user converses with it, and may change its cwd and model independently. It "belongs to" the parent
(`sessions.parent_id`, `kind='subsession'`): it nests under it in the session lists and its cost rolls up.

Older databases may also hold child sessions of kind 'subagent' (from before subagents became threads inside
a session); they are shown the same way."""
from xml.sax.saxutils import quoteattr

KIND = "subsession"


def info(store, sid):
    row = store.query_one("SELECT parent_id, kind FROM sessions WHERE id=?", (sid,))
    parent, kind = (row or (None, None))
    children = store.query_one("SELECT COUNT(*) FROM sessions WHERE parent_id=?", (sid,))[0]
    return {"parent_id": parent, "kind": kind or "chat", "children": children}


def system_note(store, sid):
    """Tells a subsession's model where it comes from (it doesn't see the parent's conversation)."""
    row = store.query_one("SELECT s.kind, p.id, p.title FROM sessions s LEFT JOIN sessions p ON p.id = s.parent_id "
                          "WHERE s.id=?", (sid,))
    if not row or row[0] != KIND or not row[1]:
        return ""
    return ("\n\n# Subsession\nThis session is a subsession the user started from session %s (%s). It has its own "
            "conversation: you do not see the parent's history. Treat the parent like a peer session: only use "
            "`ask`/`message` on it when the user asks you to." % (row[1], quoteattr(row[2] or "untitled")))


def create(agent, parent_sid, client=None):
    """Creates a subsession of `parent_sid` (same cwd, provider, model and effort); returns its summary."""
    parent = agent.require(parent_sid)
    s = agent.create(parent["cwd"], remember=False)
    agent.store.execute(
        "UPDATE sessions SET parent_id=?, kind=?, "
        "provider=(SELECT provider FROM sessions WHERE id=?), model=(SELECT model FROM sessions WHERE id=?), "
        "effort=(SELECT effort FROM sessions WHERE id=?) WHERE id=?",
        (parent_sid, KIND, parent_sid, parent_sid, parent_sid, s["id"]))
    agent.updated(parent_sid)
    return agent.updated(s["id"])
