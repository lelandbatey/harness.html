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
    {"name": "threads",
     "description": "List all other threads you can see: (1) branches and subagents inside this session "
                    "(metadata only: id, title, kind, fresh/fork context, status, message count) and "
                    "(2) other active peer sessions in this repository (status, activity, claimed and recently "
                    "edited files, plus your own claims). Check this before editing files so you don't conflict "
                    "with peers. For user-owned side-branches (asides), inspect metadata only and ask the user "
                    "before querying or messaging one unless the user asked you to.",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "spawn",
     "description": "Spawn a background subagent thread inside this session to work on a task concurrently. "
                    "Choose context='fresh' (default, clean isolated context) or context='fork' (inherits "
                    "this conversation's prior context). Returns immediately with the new thread_id; when it "
                    "finishes, its final report arrives as a <thread-message>. Launch several for parallel work. "
                    "To follow up with an existing thread later, use `ask` or `message`.",
     "input_schema": {"type": "object", "properties": {
         "description": {"type": "string", "description": "Short (3-5 words) title for the subagent thread"},
         "prompt": {"type": "string", "description": "The full task for the subagent"},
         "context": {"type": "string", "enum": ["fresh", "fork"],
                     "description": "'fresh' (default, clean context) or 'fork' (inherits conversation up to here)"}},
         "required": ["description", "prompt"]}},
    {"name": "ask",
     "description": "Ask a quick one-shot read-only question (like /btw) answered immediately from ANY other "
                    "thread's context — a branch or subagent in this session, OR an active/idle peer session "
                    "in this repository — without modifying that thread's history or running tools there. "
                    "For user side-branches (asides), only use when the user asked you to check or agreed.",
     "input_schema": {"type": "object", "properties": {
         "target_id": {"type": "string", "description": "Thread/branch id (b_…), branch name, or peer session id (s_…)"},
         "question": {"type": "string", "description": "Specific question to answer from that thread's context"}},
         "required": ["target_id", "question"]}},
    {"name": "message",
     "description": "Send an asynchronous message or task to another thread — a subagent or branch in this "
                    "session (starts a background turn with tools and delivers its result back as a "
                    "<thread-message>), or a peer session in this repository (delivered at its step boundary; "
                    "use `ask` instead if you just want an immediate read-only answer from an idle peer). "
                    "For user side-branches, only use when the user asked you to or agreed.",
     "input_schema": {"type": "object", "properties": {
         "target_id": {"type": "string", "description": "Thread/branch id (b_…), branch name, or peer session id (s_…)"},
         "text": {"type": "string", "description": "The task, question, reply or note to send"},
         "kind": {"type": "string", "enum": ["task", "question", "reply", "info"],
                  "description": "task delegates work (default for local threads); question/reply/info for peers"},
         "reply_to": {"type": "string", "description": "For peer replies: the id (pm_…) you are answering"}},
         "required": ["target_id", "text"]}},
    {"name": "claim",
     "description": "Mark files or directories you are about to change, with a short note on what you are doing, "
                    "so other threads/sessions avoid them. Advisory; expires automatically (default 30 min). "
                    "Claim before multi-step edits and release when done (edit/write also auto-claim for 10 min). "
                    "If a path is already claimed by another thread/session, work around it or use `ask`/`message`.",
     "input_schema": {"type": "object", "properties": {
         "paths": {"type": "array", "items": {"type": "string"}, "description": "Files or directories"},
         "note": {"type": "string", "description": "What you are doing there (shown to other sessions)"},
         "minutes": {"type": "integer", "description": "How long (default 30, max 240)"}},
         "required": ["paths"]}},
    {"name": "release",
     "description": "Release your claims on the given paths (or all your claims when paths is omitted) once you "
                    "are done with them, so other threads/sessions can work there.",
     "input_schema": {"type": "object", "properties": {
         "paths": {"type": "array", "items": {"type": "string"}}}}},
]


class ToolError(Exception):
    pass


class Context:
    """Per-turn tool context; cancel() kills running subprocesses."""

    def __init__(self, cwd, load_skill=None):
        self.cwd = cwd
        self.load_skill = load_skill
        self.peer = None         # peers.ToolBridge
        self.branch = None       # branches.ToolBridge
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


def tool_subagent(ctx, description, prompt, session_id=None, context="fresh"):
    """Alias for `spawn` (or `message` when continuing an existing thread) for models that call the
    tool by its old name (from older conversations)."""
    if session_id:
        return _branch(ctx).message(session_id, prompt, "task", None)
    return _branch(ctx).spawn(description, prompt, context)


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


def _branch(ctx):
    if not ctx.branch:
        raise ToolError("thread tools are not available here")
    return ctx.branch


def tool_threads(ctx):
    return _branch(ctx).list_threads()


def tool_spawn(ctx, description, prompt, context="fresh"):
    return _branch(ctx).spawn(description, prompt, context)


def tool_ask(ctx, target_id, question):
    return _branch(ctx).ask(target_id, question)


def tool_message(ctx, target_id, text, kind=None, reply_to=None):
    return _branch(ctx).message(target_id, text, kind, reply_to)


# Backwards-compatible aliases
def tool_branches(ctx):
    return _branch(ctx).list_branches()


def tool_ask_branch(ctx, branch_id, question):
    return _branch(ctx).ask(branch_id, question)


def tool_message_branch(ctx, branch_id, prompt):
    return _branch(ctx).message(branch_id, prompt, "task", None)
