"""AGENTS.md collection, skills discovery, the built-in self-edit skill and the system prompt."""
import datetime
import os
import platform

SKILL_DIRS = ("skills", os.path.join(".opencode", "skills"), os.path.join(".claude", "skills"))
BUILTIN_NAME = "singlefile-harness"
BUILTIN_DESCRIPTION = ("How to inspect, configure and modify this LLM harness itself (the single html file "
                       "you are running in): settings, system prompt, data, and its program/web code.")

BUILTIN_SKILL = """\
---
name: singlefile-harness
description: {description}
---
# Editing the single-file LLM harness

You are running inside a harness that is ONE file: `{html}`
It is at the same time a web page (the chat UI, opened as file://) and a program (`sh '{html}'` runs the
server you are talking through). All its state lives inside the file.

## Discover

    sh '{html}' help          # usage, all subcommands, extraction instructions
    sh '{html}' info          # port, db size, session count, whether the server runs

The top of the file has an LLM-oriented header comment explaining its layout. Do not open the whole file
with `read` blindly: it contains a large base64 data block and a bundled program. Use `grep -n` / `read` with
offset+limit.

## File structure

1. A shell header (`<!-- 2>/dev/null; exec python3 -c ...`) so `sh FILE` runs the program.
2. HTML head + `<style>` (web CSS) and `<body>` markup.
3. `<script type="text/x-python" id="sfh-program">` between the lines `#--SFH-PROGRAM-BEGIN--` and
   `#--SFH-PROGRAM-END--`: the bundled python program (server, agent, tools, TUI; stdlib + vendored tremolo).
4. `<script>` with the web client JS (xterm.js, markdown renderer, identicons, app.js).
5. The data block between `<!--SFH-DATA-BEGIN-->` and `<!--SFH-DATA-END-->`: base64(gzip(sqlite db)).
   The running server owns it and rewrites it ~1.5s after changes; never edit it by hand.

## Settings (take effect immediately, no restart)

    sh '{html}' settings                     # list
    sh '{html}' settings get system_prompt
    sh '{html}' settings set model '"claude-sonnet-5-5"'      # value is JSON
    sh '{html}' settings set thinking_budget 8000

Keys: provider ("auto"|"anthropic"|a configured provider id), model, cheap_model, max_tokens, thinking_budget (0 = off;
used when no effort level is sent), effort (default effort level: null = none, else e.g. "low"|"medium"|"high"|"xhigh"|"max";
sessions can pick their own with /effort), system_prompt, builtin_skill_enabled, editor, identicon_subjects, credentials_storage ("xdg"|"embedded"),
ui.initial_view, tui_mouse (TUI mouse wheel scrolling; false = plain drag selects text), model_overrides ({{provider id or "*": {{model id: {{"context_window": int, "efforts": [levels]}}}}}}, e.g. to cap a
1M-context model at 200k, or to say which effort levels a model takes; [] = none). Credentials (API keys) are NOT settings; they live in $XDG_CONFIG_HOME/singlefile/credentials
or are managed in the UI's settings view. Never print or copy secret values.

## Data

    sh '{html}' sql "SELECT id, title FROM sessions ORDER BY updated_at DESC LIMIT 5"
    sh '{html}' sql "UPDATE sessions SET title='x' WHERE id='s_...'" --write

Tables: sessions, messages (content = JSON Anthropic content blocks), settings (JSON values), credentials,
files, meta. While the server runs, `sql` goes through it so the in-memory db stays authoritative.

## Changing the program or the web UI

    sh '{html}' extract-program /tmp/sfh-src     # unbundled python (sfh/, tremolo/) + web assets

Edit the extracted sources, then either rebuild the html from the project repository (`make build`, which
produces a new file whose data block you then move over with `db-extract`/`db-inject`), or make a small,
careful edit directly inside the program block of `{html}` (keep it valid: no `</script` inside it).
Make a backup copy of the file first. Program changes only take effect after the server restarts
(stop it with Ctrl-C in its terminal and run `sh '{html}'` again) — tell the user to do that, since
restarting kills the session you are running in. Web UI changes take effect on page reload.
"""


def parse_frontmatter(text):
    meta = {}
    if not text.startswith("---"):
        return meta, text
    end = text.find("\n---", 3)
    if end < 0:
        return meta, text
    for line in text[3:end].splitlines():
        if ":" in line and not line.startswith((" ", "\t")):
            k, _, v = line.partition(":")
            meta[k.strip()] = v.strip().strip("'\"")
    body = text[end + 4:]
    return meta, body.lstrip("\n")


def agents_files(cwd):
    """AGENTS.md (or CLAUDE.md when a dir has no AGENTS.md) from / down to cwd, outermost first."""
    dirs = []
    d = os.path.abspath(cwd)
    while True:
        dirs.append(d)
        parent = os.path.dirname(d)
        if parent == d:
            break
        d = parent
    out = []
    for d in reversed(dirs):
        for n in ("AGENTS.md", "CLAUDE.md"):
            p = os.path.join(d, n)
            if os.path.isfile(p):
                out.append(p)
                break
    return out


def discover(cwd, settings, html_path):
    """[{name, description, source, path}] in precedence order (first name wins)."""
    seen = {}
    for rel in SKILL_DIRS:
        root = os.path.join(cwd, rel)
        try:
            entries = sorted(os.listdir(root))
        except OSError:
            continue
        for e in entries:
            p = os.path.join(root, e, "SKILL.md")
            if not os.path.isfile(p):
                continue
            try:
                with open(p, encoding="utf-8", errors="replace") as f:
                    meta, _ = parse_frontmatter(f.read(8192))
            except OSError:
                continue
            name = meta.get("name") or e
            if name not in seen:
                seen[name] = {"name": name, "description": meta.get("description", ""), "source": rel, "path": p}
    if settings.get("builtin_skill_enabled", True) and BUILTIN_NAME not in seen:
        seen[BUILTIN_NAME] = {"name": BUILTIN_NAME, "description": BUILTIN_DESCRIPTION, "source": "builtin",
                              "path": html_path}
    return list(seen.values())


def load(name, cwd, settings, html_path):
    for s in discover(cwd, settings, html_path):
        if s["name"] != name:
            continue
        if s["source"] == "builtin":
            return BUILTIN_SKILL.format(html=html_path, description=BUILTIN_DESCRIPTION)
        with open(s["path"], encoding="utf-8", errors="replace") as f:
            text = f.read()
        return "Skill directory: %s\n\n%s" % (os.path.dirname(s["path"]), text)
    return None


def _read(p, limit=100000):
    try:
        with open(p, encoding="utf-8", errors="replace") as f:
            return f.read(limit)
    except OSError:
        return ""


def system_prompt(settings, cwd, html_path):
    parts = [settings.get("system_prompt") or ""]
    parts.append("# Environment\n- Working directory: %s\n- Date: %s\n- Platform: %s\n"
                 "- Harness file (this program + UI): %s"
                 % (cwd, datetime.date.today().isoformat(), platform.platform(), html_path))
    for p in agents_files(cwd):
        text = _read(p).strip()
        if text:
            parts.append("# Instructions from %s\n\n%s" % (p, text))
    sk = discover(cwd, settings, html_path)
    if sk:
        lines = ["# Skills", "Load a skill's full instructions with the `skill` tool when it matches the task:"]
        lines += ["- %s: %s" % (s["name"], s["description"]) for s in sk]
        parts.append("\n".join(lines))
    return "\n\n".join(x for x in parts if x)
