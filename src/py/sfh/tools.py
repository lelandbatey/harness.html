"""Agent tools. execute() is blocking; the agent runs it in a thread."""
import fnmatch
import glob as globmod
import os
import re
import signal
import subprocess
import threading

MAX_OUTPUT = 30000
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv"}

DEFINITIONS = [
    {"name": "bash",
     "description": "Run a shell command (bash -c) in the session's working directory. Returns combined "
                    "stdout+stderr (truncated to ~30KB) and the exit code. Non-interactive; default timeout 120s.",
     "input_schema": {"type": "object", "properties": {
         "command": {"type": "string", "description": "The command to run"},
         "timeout": {"type": "integer", "description": "Timeout in seconds (default 120)"}},
         "required": ["command"]}},
    {"name": "read",
     "description": "Read a text file. Returns lines prefixed with their 1-based line numbers. Use offset/limit "
                    "for large files (default: first 2000 lines).",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"},
         "offset": {"type": "integer", "description": "1-based line to start at"},
         "limit": {"type": "integer", "description": "max lines to return"}},
         "required": ["path"]}},
    {"name": "write",
     "description": "Create or overwrite a file with the given content (parent directories are created).",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "content": {"type": "string"}}, "required": ["path", "content"]}},
    {"name": "edit",
     "description": "Replace an exact string in a file. old_string must match exactly once unless replace_all "
                    "is true. Include enough context to make it unique.",
     "input_schema": {"type": "object", "properties": {
         "path": {"type": "string"}, "old_string": {"type": "string"}, "new_string": {"type": "string"},
         "replace_all": {"type": "boolean"}}, "required": ["path", "old_string", "new_string"]}},
    {"name": "glob",
     "description": "Find files by glob pattern (supports **), newest first. Optional base path.",
     "input_schema": {"type": "object", "properties": {
         "pattern": {"type": "string"}, "path": {"type": "string"}}, "required": ["pattern"]}},
    {"name": "grep",
     "description": "Search file contents with a Python regular expression. Returns path:line: text matches. "
                    "Optional base path and filename glob filter (include). Skips .git and node_modules.",
     "input_schema": {"type": "object", "properties": {
         "pattern": {"type": "string"}, "path": {"type": "string"}, "include": {"type": "string"},
         "ignore_case": {"type": "boolean"}}, "required": ["pattern"]}},
    {"name": "skill",
     "description": "Load the full instructions of a skill (see the skills list in the system prompt) by name.",
     "input_schema": {"type": "object", "properties": {"name": {"type": "string"}}, "required": ["name"]}},
    {"name": "webfetch",
     "description": "Fetch an http(s) URL and return its content. HTML is converted to markdown by default "
                    "(format: markdown|text|html). Output is capped at ~100KB. Binary content is refused.",
     "input_schema": {"type": "object", "properties": {
         "url": {"type": "string"},
         "format": {"type": "string", "enum": ["markdown", "text", "html"]},
         "timeout": {"type": "integer", "description": "Seconds (default 30, max 120)"}},
         "required": ["url"]}},
    {"name": "subagent",
     "description": "Launch a subagent: a separate background session (same cwd and tools) that works on a task "
                    "independently. Returns immediately with the child's session_id; when the child finishes, its "
                    "final message arrives as a <subagent-result> user message. Launch several for parallel work. "
                    "Pass session_id to send a follow-up prompt to an existing child.",
     "input_schema": {"type": "object", "properties": {
         "description": {"type": "string", "description": "Short (3-5 words) description of the task"},
         "prompt": {"type": "string", "description": "The full, self-contained task for the subagent"},
         "session_id": {"type": "string", "description": "Continue an existing child session"}},
         "required": ["description", "prompt"]}},
    {"name": "peers",
     "description": "List the other sessions active in this repository right now (running, or active in the last "
                    "15 minutes): their title, status, what they are doing, the files they claimed and recently "
                    "edited, and your own claims. Check this before you start changing files, and again before "
                    "editing a file you haven't touched yet, so you don't overwrite or conflict with another "
                    "session's work. If a peer works on the same files, adjust your plan first (other files, a "
                    "narrower change, or later); use send_message only for information you can't get otherwise.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "claim",
     "description": "Mark files or directories you are about to change, with a short note on what you are doing, "
                    "so other sessions avoid them. Advisory; expires automatically (default 30 min). Claim before "
                    "multi-step edits and release when done (edit/write also claim the file for 10 min on their "
                    "own). If a path is already claimed by another session, don't edit it: work around it, or ask "
                    "the owner with send_message if you can't continue without it.",
     "input_schema": {"type": "object", "properties": {
         "paths": {"type": "array", "items": {"type": "string"}, "description": "Files or directories"},
         "note": {"type": "string", "description": "What you are doing there (shown to other sessions)"},
         "minutes": {"type": "integer", "description": "How long (default 30, max 240)"}},
         "required": ["paths"]}},
    {"name": "release",
     "description": "Release your claims on the given paths (or all your claims when paths is omitted) once you "
                    "are done with them, so other sessions can work there.",
     "input_schema": {"type": "object", "properties": {
         "paths": {"type": "array", "items": {"type": "string"}}}}},
    {"name": "send_message",
     "description": "Send a short message to another session (see `peers`). Use it to ask a question you can't "
                    "answer yourself (e.g. \"are you changing the Runtime signature?\"), to tell a session about a "
                    "conflict you can't avoid, or to answer a question it asked you (pass reply_to). Not for status "
                    "updates or chatter: `peers` already shows what everyone is doing. The message reaches the "
                    "recipient between its steps; replies arrive later as <peer-message> in your conversation. "
                    "Don't wait for them: continue with work that doesn't depend on the answer. Keep messages "
                    "specific and self-contained.",
     "input_schema": {"type": "object", "properties": {
         "session_id": {"type": "string", "description": "The recipient session"},
         "text": {"type": "string"},
         "kind": {"type": "string", "enum": ["question", "reply", "info"],
                  "description": "question expects an answer; reply answers one (with reply_to); info needs none"},
         "reply_to": {"type": "string", "description": "The id (pm_...) of the message you are answering"}},
         "required": ["session_id", "text"]}},
]


class ToolError(Exception):
    pass


class Context:
    """Per-turn tool context; cancel() kills running subprocesses."""

    def __init__(self, cwd, load_skill=None):
        self.cwd = cwd
        self.load_skill = load_skill
        self.subagent = None     # callable(description, prompt, session_id) -> text; None in child sessions
        self.peer = None         # peers.ToolBridge
        self.procs = set()
        self.cancelled = False
        self.lock = threading.Lock()

    def cancel(self):
        self.cancelled = True
        with self.lock:
            procs = list(self.procs)
        for p in procs:
            try:
                os.killpg(p.pid, signal.SIGKILL)
            except OSError:
                pass

    def resolve(self, path):
        if not path:
            raise ToolError("path is required")
        return os.path.normpath(os.path.join(self.cwd, os.path.expanduser(path)))


def truncate(s, limit=MAX_OUTPUT):
    if len(s) <= limit:
        return s
    half = limit // 2
    return s[:half] + "\n\n[... %d characters truncated ...]\n\n" % (len(s) - 2 * half) + s[-half:]


def execute(name, inp, ctx):
    """Returns (text, is_error)."""
    fn = globals().get("tool_" + str(name))
    if fn is None:
        return "unknown tool: %s" % name, True
    if not isinstance(inp, dict):
        return "tool input must be an object", True
    try:
        return fn(ctx, **inp), False
    except ToolError as e:
        return str(e), True
    except TypeError as e:
        return "bad arguments for %s: %s" % (name, e), True
    except OSError as e:
        return "%s: %s" % (type(e).__name__, e), True
    except Exception as e:
        return "%s failed: %s: %s" % (name, type(e).__name__, e), True


def tool_bash(ctx, command, timeout=120):
    if ctx.cancelled:
        raise ToolError("interrupted")
    shell = "/bin/bash" if os.path.exists("/bin/bash") else "/bin/sh"
    p = subprocess.Popen([shell, "-c", command], cwd=ctx.cwd, stdin=subprocess.DEVNULL,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
    with ctx.lock:
        ctx.procs.add(p)
    try:
        try:
            out, _ = p.communicate(timeout=max(1, int(timeout or 120)))
            note = ""
        except subprocess.TimeoutExpired:
            os.killpg(p.pid, signal.SIGKILL)
            out, _ = p.communicate()
            note = "\n[timed out after %ss]" % timeout
    finally:
        with ctx.lock:
            ctx.procs.discard(p)
    text = truncate(out.decode("utf-8", "replace")) + note
    if ctx.cancelled:
        text += "\n[interrupted]"
    if p.returncode:
        text += "\n[exit code %d]" % p.returncode
    return text or "(no output)"


def tool_read(ctx, path, offset=1, limit=2000):
    p = ctx.resolve(path)
    if os.path.isdir(p):
        raise ToolError("%s is a directory: %s" % (path, ", ".join(sorted(os.listdir(p))[:200])))
    with open(p, "rb") as f:
        data = f.read()
    if b"\0" in data[:8192]:
        raise ToolError("%s looks like a binary file (%d bytes)" % (path, len(data)))
    lines = data.decode("utf-8", "replace").splitlines()
    offset = max(1, int(offset or 1))
    limit = max(1, int(limit or 2000))
    chunk = lines[offset - 1:offset - 1 + limit]
    out = ["%6d\t%s" % (offset + i, ln if len(ln) <= 2000 else ln[:2000] + "…") for i, ln in enumerate(chunk)]
    if offset - 1 + limit < len(lines):
        out.append("[... %d more lines; use offset=%d ...]" % (len(lines) - (offset - 1 + limit), offset + limit))
    if not lines:
        return "(empty file)"
    return truncate("\n".join(out))


def tool_write(ctx, path, content):
    p = ctx.resolve(path)
    d = os.path.dirname(p)
    if d:
        os.makedirs(d, exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)
    return "wrote %d bytes to %s" % (len(content.encode("utf-8")), p)


def tool_edit(ctx, path, old_string, new_string, replace_all=False):
    p = ctx.resolve(path)
    with open(p, encoding="utf-8") as f:
        text = f.read()
    if old_string == "":
        raise ToolError("old_string must not be empty")
    n = text.count(old_string)
    if n == 0:
        raise ToolError("old_string not found in %s" % path)
    if n > 1 and not replace_all:
        raise ToolError("old_string matches %d times in %s; add context or set replace_all" % (n, path))
    text = text.replace(old_string, new_string) if replace_all else text.replace(old_string, new_string, 1)
    with open(p, "w", encoding="utf-8") as f:
        f.write(text)
    return "edited %s (%d replacement%s)" % (p, n if replace_all else 1, "s" if replace_all and n > 1 else "")


def tool_glob(ctx, pattern, path=None):
    base = ctx.resolve(path) if path else ctx.cwd
    matches = globmod.glob(os.path.join(base, pattern), recursive=True)
    matches = [m for m in matches if not (set(m.split(os.sep)) & SKIP_DIRS)]

    def mtime(m):
        try:
            return os.path.getmtime(m)
        except OSError:
            return 0
    matches.sort(key=mtime, reverse=True)
    more = len(matches) - 500
    out = "\n".join(matches[:500]) or "(no matches)"
    if more > 0:
        out += "\n[... %d more]" % more
    return out


def tool_grep(ctx, pattern, path=None, include=None, ignore_case=False):
    try:
        rx = re.compile(pattern, re.I if ignore_case else 0)
    except re.error as e:
        raise ToolError("bad regex: %s" % e)
    base = ctx.resolve(path) if path else ctx.cwd
    files = [base] if os.path.isfile(base) else None
    out = []
    limit = 300

    def walk():
        for root, dirs, names in os.walk(base):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
            for n in sorted(names):
                if include and not fnmatch.fnmatch(n, include):
                    continue
                yield os.path.join(root, n)
    for fp in files or walk():
        if ctx.cancelled or len(out) >= limit:
            break
        try:
            if os.path.getsize(fp) > 5 * 1024 * 1024:
                continue
            with open(fp, "rb") as f:
                data = f.read()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue
        for i, line in enumerate(data.decode("utf-8", "replace").splitlines(), 1):
            if rx.search(line):
                out.append("%s:%d: %s" % (os.path.relpath(fp, ctx.cwd), i, line[:300]))
                if len(out) >= limit:
                    out.append("[... stopped after %d matches]" % limit)
                    break
    return truncate("\n".join(out)) if out else "(no matches)"


def tool_skill(ctx, name):
    if not ctx.load_skill:
        raise ToolError("skills unavailable")
    text = ctx.load_skill(name)
    if text is None:
        raise ToolError("no skill named %r" % name)
    return text


def tool_webfetch(ctx, url, format="markdown", timeout=30):
    from . import webfetch
    try:
        return webfetch.fetch(url, format, timeout)
    except webfetch.FetchError as e:
        raise ToolError(str(e))


def tool_subagent(ctx, description, prompt, session_id=None):
    if not ctx.subagent:
        raise ToolError("subagents are not available here (subagents cannot launch subagents)")
    return ctx.subagent(description, prompt, session_id)


def _peer(ctx):
    if not ctx.peer:
        raise ToolError("peer tools are not available here")
    return ctx.peer


def tool_peers(ctx):
    return _peer(ctx).peers()


def tool_claim(ctx, paths, note="", minutes=None):
    return _peer(ctx).claim(paths, note, minutes)


def tool_release(ctx, paths=None):
    return _peer(ctx).release(paths)


def tool_send_message(ctx, session_id, text, kind=None, reply_to=None):
    return _peer(ctx).send(session_id, text, kind, reply_to)
