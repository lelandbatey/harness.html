"""Server-side slash commands (texts sent with chat.send that start with "/")."""
import asyncio

from . import about, branches, llm, skills, subsessions

COMMANDS = [
    {"name": "help", "description": "List the available commands"},
    {"name": "aside", "description": "/aside [message] — start a new side-branch from the current message and switch to it"},
    {"name": "branch", "description": "/branch [id|name|main|promote|rename <title>] — switch, promote or rename branches"},
    {"name": "branches", "description": "List all branches (asides) of this session"},
    {"name": "subsession", "description": "/subsession [message] — start a new conversation with an empty context in "
                                          "this session's directory, nested under this session, and switch to it"},
    {"name": "btw", "description": "/btw <question> — ask a side question using this session's context; "
                                   "the answer is not added to the conversation"},
    {"name": "retitle", "description": "Regenerate this thread's title (on the main thread, also the session title and subjects)"},
    {"name": "title", "description": "/title <text> — set the session title"},
    {"name": "compact", "description": "/compact [instructions] — summarize the conversation so far; the model "
                                       "continues from the summary (frees context)"},
    {"name": "model", "description": "/model [name|default] — show or set this session's model"},
    {"name": "effort", "description": "/effort [level|default] — show or set this session's effort level "
                                      "(how much the model thinks/reasons)"},
    {"name": "provider", "description": "/provider [id|default] — show or set this session's provider"},
    {"name": "models", "description": "/models [filter] — list the models this session's provider offers"},
    {"name": "cd", "description": "/cd <dir> — change this session's working directory"},
    {"name": "skills", "description": "List the skills available in this session"},
    {"name": "system", "description": "Show the effective system prompt for this session"},
    {"name": "peers", "description": "Show the other sessions active in this repository, their claims and edits"},
]


LLM_COMMANDS = ("btw", "retitle", "compact")   # disabled while no provider is configured


def is_command(text):
    t = text.lstrip()
    if not t.startswith("/") or t.startswith("//"):
        return False
    name = t[1:].split(None, 1)[0] if t[1:].strip() else ""
    return any(c["name"] == name for c in COMMANDS)


async def handle(server, client, sid, text, branch_id=None):
    """Runs a command. Notices for informational commands go only to the requesting client."""
    agent = server.agent
    name, _, arg = text.lstrip()[1:].partition(" ")
    arg = arg.strip()
    session = agent.require(sid)
    bid = branch_id or (client.branch_of.get(sid) if client else None) or branches.active_id(agent.store, sid)

    def notice(t, level="info", everyone=False):
        data = {"session_id": sid, "branch_id": bid, "level": level, "text": t}
        if everyone or client is None:
            server.emit(sid, "notice", data)
        else:
            client.event("notice", data)

    if name in LLM_COMMANDS and not server.has_provider():
        return notice(llm.NO_PROVIDER, "error")
    if name == "help":
        notice("**What is this?** " + about.ABOUT["what"] + "\n\n**Commands**\n\n" + "\n".join("- `/%s` — %s" % (c["name"], c["description"]) for c in COMMANDS))
    elif name == "aside":
        if arg and not server.has_provider():
            return notice(llm.NO_PROVIDER, "error")
        b = branches.create(agent, sid, title=branches.fallback_branch_name(arg) if arg else "",
                            parent_branch_id=bid, client=client)
        bid = b["id"]
        if arg:
            await agent.send(sid, arg, branch_id=b["id"])
        else:
            notice("Started aside branch `%s`. Send a message here, or switch back with `/branch main`." % b["id"])
    elif name == "subsession":
        if arg and not server.has_provider():
            return notice(llm.NO_PROVIDER, "error")
        s = subsessions.create(agent, sid)
        if client is not None:
            client.event("session.open", {"session_id": s["id"], "from_session_id": sid})
        if arg:
            await agent.send(s["id"], arg)
        notice("Started subsession `%s`. Return here from its header or the session list." % s["id"])
    elif name == "branches":
        notice("**Threads**\n\n```\n%s\n```" % branches.branches_text(agent, sid, bid))
    elif name == "branch":
        if not arg:
            notice("**Threads**\n\n```\n%s\n```\n\nSwitch with `/branch <id|name|main>`, promote with `/branch promote`, "
                   "or rename with `/branch rename <title>`." % branches.branches_text(agent, sid, bid))
        else:
            sub, _, rest = arg.partition(" ")
            rest = rest.strip()
            if sub == "promote":
                b = branches.promote(agent, sid, rest or bid)
                notice("Thread **%s** (`%s`) is now the main thread (★)." % (b["name"], b["id"]), everyone=True)
            elif sub == "rename":
                if not rest:
                    return notice("usage: `/branch rename <title>`", "error")
                b = branches.rename(agent, sid, bid, rest)
                notice("Renamed branch `%s` to **%s**." % (b["id"], b["name"]), everyone=True)
            else:
                try:
                    snap = branches.switch(agent, sid, arg, client=client)
                    bid = snap["branch_id"]
                    b = next((x for x in snap["branches"]["branches"] if x["id"] == snap["branch_id"]), None)
                    notice("Switched to branch **%s** (`%s`)." % (b["name"] if b else arg, snap["branch_id"]))
                except ValueError as e:
                    notice(str(e), "error")
    elif name == "btw":
        if not arg:
            return notice("usage: `/btw <question>`", "error")
        agent.spawn(agent.btw(sid, arg, branch_id=bid))
    elif name == "peers":
        notice("**Peers**\n\n```\n%s\n```" % agent.peers.peers_text(sid))
    elif name == "retitle":
        # on the main thread: the session's title and the thread's; on any other thread: only that thread's
        if bid == branches.main_id(agent.store, sid):
            await asyncio.gather(agent.retitle(sid), branches.retitle(agent, sid, bid))
        else:
            await branches.retitle(agent, sid, bid)
    elif name == "compact":
        await agent.compact(sid, arg, branch_id=bid)
    elif name == "title":
        if not arg:
            return notice("usage: `/title <text>`", "error")
        agent.rename(sid, arg)
    elif name == "model":
        if arg:
            s = agent.set_model(sid, model=None if arg == "default" else arg)
            notice("Model for this session set to `%s` (provider `%s`)." % (s["model"], s["provider"]), everyone=True)
        else:
            notice("This session uses model `%s` from provider `%s`%s. `/models` lists the provider's models." % (
                session["model"], session["provider"], "" if session.get("model_override") else " (the default)"))
    elif name == "effort":
        if arg:
            s = agent.set_model(sid, effort=None if arg == "default" else arg)
            notice("Effort for this session set to `%s`%s." % (
                s.get("effort_wanted") or "default", effort_note(s)), everyone=True)
        else:
            notice("This session's effort level is `%s`%s%s. Levels %s: %s. Set one with `/effort <level>` "
                   "(`/effort default` follows the global setting)." % (
                       session.get("effort_wanted") or "default",
                       "" if session.get("effort_override") else " (the default)", effort_note(session),
                       "%s takes" % session.get("model") if session.get("efforts_known") else
                       "(the provider doesn't say which %s takes; tried, dropped if refused)" % session.get("model"),
                       ", ".join("`%s`" % e for e in session.get("efforts") or []) or "none"))
    elif name == "provider":
        if arg:
            s = agent.set_model(sid, provider=None if arg == "default" else arg)
            notice("Provider for this session set to `%s` (model `%s`)." % (s["provider"], s["model"]), everyone=True)
        else:
            ids = [p["id"] for p in llm.providers(agent.settings(), server.creds)]
            notice("This session uses provider `%s`%s. Available: %s" % (
                session["provider"], "" if session.get("provider_override") else " (the default)",
                ", ".join("`%s`" % i for i in ids)))
    elif name == "models":
        r = await server.list_models(session_id=sid, refresh=True)
        if r.get("error"):
            return notice("Could not list the models of `%s`: %s" % (r["provider"], r["error"]), "error")
        words = arg.lower().split()
        shown = [m for m in r["models"] if all(w in (m["id"] + " " + (m.get("name") or "")).lower() for w in words)]
        notice("**Models of `%s`**%s\n\n" % (r["provider"], " matching `%s`" % arg if arg else "") + ("\n".join(
            "- `%s`%s%s%s%s" % (m["id"], " — " + m["name"] if m.get("name") else "", _window(m), _efforts(m),
                              " **(in use)**" if m["id"] == session["model"] else "") for m in shown) or "(none)") +
               "\n\nSet one with `/model <id>`. Context windows can be overridden in Settings → Model overrides.")
    elif name == "cd":
        if not arg:
            return notice("Working directory: `%s`. Usage: `/cd <dir>`" % session["cwd"])
        agent.set_cwd(sid, arg)
    elif name == "skills":
        sk = skills.discover(session["cwd"], agent.settings(), server.html_path)
        notice("**Skills**\n\n" + ("\n".join("- **%s** (%s) — %s" % (s["name"], s["source"], s["description"])
                                             for s in sk) or "(none)"))
    elif name == "system":
        notice("**Effective system prompt**\n\n````\n%s\n````" % agent.system_prompt(sid, bid))
    else:
        notice("unknown command `/%s`" % name, "error")


def effort_note(s):
    """" (sent as `high`: ...)" when the level a session wants is not the one its model gets."""
    want, eff = s.get("effort_wanted"), s.get("effort")
    if want and want != eff:
        return " (sent as `%s`: %s doesn't take `%s`)" % (eff, s.get("model"), want) if eff else \
            " (not sent: %s takes no effort level)" % s.get("model")
    return ""


def _efforts(m):
    eff = m.get("efforts_override") if m.get("efforts_override") is not None else m.get("efforts")
    return " · effort: %s" % ("/".join(eff) or "none") if eff is not None else ""


def _window(m):
    """" · 200k context" (" · 200k context (override; provider: 1M)") for /models."""
    def k(n):
        return "%gM" % (n / 1e6) if n >= 1000000 else "%gk" % round(n / 1e3) if n >= 1000 else str(n)
    cw, ov = m.get("context_window"), m.get("context_window_override")
    if ov:
        return " · %s context (override%s)" % (k(ov), "; provider: %s" % k(cw) if cw else "")
    return " · %s context" % k(cw) if cw else ""
