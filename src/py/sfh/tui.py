"""Minimal TUI client: takes over the screen (no curses), one session at a time.

The pure parts (KeyParser, Editor, wrapping/rendering) are terminal-independent and unit-tested.
"""
import codecs
import html
import json
import os
import re
import select
import signal
import subprocess
import sys
import time
import unicodedata

from sfh import about
from sfh import common
from sfh import htmlfile
from sfh import wsclient

ESC_TIMEOUT = 0.05
WHEEL_LINES = 3  # transcript lines per mouse wheel notch (with Shift/Alt/Ctrl: a page)
TICK_MS = 5000   # live timers (thinking, tool runs, the turn) advance in steps of this, so they don't flicker

# mouse reporting: button events (1000) in SGR encoding (1006); no motion tracking
MOUSE_ON = b"\x1b[?1000h\x1b[?1006h"
MOUSE_OFF = b"\x1b[?1006l\x1b[?1000l"

RESET = "\x1b[0m"
BOLD = "\x1b[1m"
DIM = "\x1b[2m"
ITALIC = "\x1b[3m"
REVERSE = "\x1b[7m"
RED = "\x1b[31m"
GREEN = "\x1b[32m"
YELLOW = "\x1b[33m"
BLUE = "\x1b[34m"
MAGENTA = "\x1b[35m"
CYAN = "\x1b[36m"

_SGR_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


# ---------------------------------------------------------------------------------------------
# width / wrapping

def char_width(ch):
    o = ord(ch)
    if o < 32 or o == 0x7F:
        return 0
    if unicodedata.combining(ch) or unicodedata.category(ch) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(ch) in ("W", "F"):
        return 2
    return 1


def str_width(s):
    return sum(char_width(c) for c in s)


def visible_width(s):
    return str_width(_SGR_RE.sub("", s))


def clean_text(s):
    s = s.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    return "".join(c for c in s if c == "\n" or ord(c) >= 32 and c != "\x7f")


def clip(s, width):
    """Truncates plain text to width columns, adding … when cut."""
    if str_width(s) <= width:
        return s
    out, w = "", 0
    for c in s:
        cw = char_width(c)
        if w + cw > width - 1:
            break
        out += c
        w += cw
    return out + "…"


def _hard_split(tok, width):
    parts, cur, w = [], "", 0
    for c in tok:
        cw = char_width(c)
        if w + cw > width and cur:
            parts.append(cur)
            cur, w = "", 0
        cur += c
        w += cw
    parts.append(cur)
    return parts


def wrap(text, width):
    """Word-wraps plain text to width columns; returns a list of lines (no trailing spaces)."""
    width = max(1, width)
    out = []
    for para in clean_text(text).split("\n"):
        if not para:
            out.append("")
            continue
        lines, cur, w = [], "", 0
        for tok in re.findall(r"\S+|\s+", para):
            tw = str_width(tok)
            if tok.isspace():
                if w + tw <= width:
                    cur += tok
                    w += tw
                else:
                    lines.append(cur)
                    cur, w = "", 0
            elif w + tw <= width:
                cur += tok
                w += tw
            elif tw <= width:
                lines.append(cur)
                cur, w = tok, tw
            else:
                if cur:
                    lines.append(cur)
                parts = _hard_split(tok, width)
                lines.extend(parts[:-1])
                cur, w = parts[-1], str_width(parts[-1])
        lines.append(cur)
        out.extend(l.rstrip(" ") for l in lines)
    return out


# ---------------------------------------------------------------------------------------------
# key decoding

class KeyParser:
    """Turns raw terminal input (possibly split across reads) into keys.

    Keys are strings: single printable characters, "enter", "C-a".."C-z", "up", "down", "left",
    "right", "home", "end", "pgup", "pgdn", "del", "backspace", "esc", "tab", "M-x", or a tuple
    ("paste", text) for bracketed paste, or ("mouse", button, x, y, pressed) for a mouse report
    (SGR `ESC [ < b ; x ; y M|m`, or legacy X10 `ESC [ M` + 3 bytes; button keeps its modifier bits,
    x/y are 1-based). Mouse reports never turn into text keys, also when split across reads.
    """

    # flushes (ESC_TIMEOUT of silence each) a half-received mouse report survives before it's dropped
    MOUSE_PATIENCE = 10

    def __init__(self):
        self.dec = codecs.getincrementaldecoder("utf-8")("replace")
        self.buf = ""
        self.paste = None
        self.waited = 0

    def feed(self, data):
        if isinstance(data, bytes):
            data = self.dec.decode(data)
        self.buf += data
        self.waited = 0
        return self._parse(final=False)

    def pending_escape(self):
        return self.buf.startswith("\x1b")

    def flush(self):
        """Called after ESC_TIMEOUT of silence: a dangling ESC is the Esc key. A half-received mouse
        report is kept a little longer (slow links split them), then dropped silently."""
        if self._partial_mouse():
            self.waited += 1
            if self.waited < self.MOUSE_PATIENCE:
                return []
            if self.buf != "\x1b[":
                self.buf = ""
        self.waited = 0
        return self._parse(final=True)

    _PARTIAL_SGR_MOUSE = re.compile(r"\x1b\[<[0-9;]*\Z")

    def _partial_mouse(self):
        b = self.buf
        return b == "\x1b[" or bool(self._PARTIAL_SGR_MOUSE.match(b)) or (b.startswith("\x1b[M") and len(b) < 6)

    _CSI_TILDE = {"1": "home", "7": "home", "4": "end", "8": "end", "3": "del", "5": "pgup",
                  "6": "pgdn", "2": "insert"}
    _CSI_LETTER = {"A": "up", "B": "down", "C": "right", "D": "left", "H": "home", "F": "end",
                   "Z": "S-tab"}

    def _parse(self, final):
        keys = []
        b = self.buf
        i = 0
        n = len(b)
        while i < n:
            if self.paste is not None:
                j = b.find("\x1b[201~", i)
                if j < 0:
                    # keep a possible partial terminator in the buffer
                    keep = 0
                    for k in range(1, 6):
                        if "\x1b[201~".startswith(b[n - k:]) and n - k >= i:
                            keep = k
                    self.paste += b[i:n - keep]
                    i = n - keep
                    break
                self.paste += b[i:j]
                keys.append(("paste", self.paste))
                self.paste = None
                i = j + 6
                continue
            c = b[i]
            if c == "\x1b":
                if i + 1 >= n:
                    if final:
                        keys.append("esc")
                        i += 1
                    break
                c2 = b[i + 1]
                if c2 == "[":
                    j = i + 2
                    while j < n and not ("\x40" <= b[j] <= "\x7e") and b[j] != "\x1b":
                        j += 1
                    if j < n and b[j] == "\x1b":  # cut off by a new sequence: drop the fragment
                        i = j
                        continue
                    if j >= n:
                        if final:
                            keys.append("esc")
                            i += 1
                            continue
                        break
                    params, fin = b[i + 2:j], b[j]
                    if fin == "M" and not params:  # legacy X10 mouse: ESC [ M Cb Cx Cy
                        if j + 3 >= n:
                            if final:  # truncated: drop it
                                i = n
                            break
                        cb, cx, cy = (ord(ch) - 32 for ch in b[j + 1:j + 4])
                        keys.append(("mouse", cb, cx, cy, (cb & 3) != 3 or cb >= 64))
                        i = j + 4
                        continue
                    i = j + 1
                    if params.startswith("<"):  # SGR mouse: ESC [ < b ; x ; y M (press) / m (release)
                        f = params[1:].split(";")
                        if fin in "Mm" and len(f) == 3 and all(x.isdigit() for x in f):
                            keys.append(("mouse", int(f[0]), int(f[1]), int(f[2]), fin == "M"))
                        continue
                    if fin == "~":
                        p = params.split(";")[0]
                        if p == "200":
                            self.paste = ""
                        elif p in self._CSI_TILDE:
                            keys.append(self._CSI_TILDE[p])
                    elif fin in self._CSI_LETTER:
                        keys.append(self._CSI_LETTER[fin])
                    continue
                if c2 == "O":
                    if i + 2 >= n:
                        if final:
                            keys.append("M-O")
                            i += 2
                            continue
                        break
                    k = self._CSI_LETTER.get(b[i + 2])
                    if k:
                        keys.append(k)
                    i += 3
                    continue
                if c2 == "\x1b":
                    keys.append("esc")
                    i += 1
                    continue
                keys.append("M-" + c2)
                i += 2
                continue
            i += 1
            if c == "\r":
                keys.append("enter")
            elif c == "\n":
                keys.append("C-j")
            elif c in ("\x7f", "\x08"):
                keys.append("backspace")
            elif c == "\t":
                keys.append("tab")
            elif c == "\x00":
                keys.append("C-space")
            elif ord(c) < 32:
                keys.append("C-" + chr(ord(c) + 96))
            else:
                keys.append(c)
        self.buf = b[i:]
        return keys


# ---------------------------------------------------------------------------------------------
# line editor

class Editor:
    """Multi-line prompt editor state machine. handle(key) returns an action tuple or None:
    ("submit", text, edit_message_id), ("exit",), ("stop",), ("scroll", pages), ("redraw",)."""

    def __init__(self):
        self.lines = [""]
        self.row = 0
        self.col = 0
        self.goal = None
        self.history = []
        self.hist_idx = None
        self.saved = None
        self.edit_id = None

    @property
    def text(self):
        return "\n".join(self.lines)

    def set_text(self, text, cursor="end"):
        self.lines = clean_text(text).split("\n")
        if cursor == "first":
            self.row, self.col = 0, len(self.lines[0])
        else:
            self.row, self.col = len(self.lines) - 1, len(self.lines[-1])
        self.goal = None

    def clear(self):
        self.set_text("")
        self.hist_idx = None
        self.saved = None
        self.edit_id = None

    def load_edit(self, text, message_id):
        self.set_text(text)
        self.edit_id = message_id
        self.hist_idx = None

    def insert(self, s):
        s = clean_text(s)
        line = self.lines[self.row]
        before, after = line[:self.col], line[self.col:]
        parts = s.split("\n")
        if len(parts) == 1:
            self.lines[self.row] = before + s + after
            self.col += len(s)
        else:
            new = [before + parts[0]] + parts[1:-1] + [parts[-1] + after]
            self.lines[self.row:self.row + 1] = new
            self.row += len(parts) - 1
            self.col = len(parts[-1])
        self.goal = None

    def _vmove(self, d):
        if self.goal is None:
            self.goal = self.col
        self.row += d
        self.col = min(self.goal, len(self.lines[self.row]))

    def _history(self, d):
        if not self.history:
            return
        if self.hist_idx is None:
            if d > 0:
                return
            self.saved = self.text
            self.hist_idx = len(self.history) - 1
        else:
            idx = self.hist_idx + d
            if idx < 0:
                return
            if idx >= len(self.history):
                self.hist_idx = None
                self.set_text(self.saved or "")
                self.saved = None
                return
            self.hist_idx = idx
        self.set_text(self.history[self.hist_idx], cursor="first" if d < 0 else "end")

    def handle(self, key):
        if isinstance(key, tuple):
            if key[0] == "paste":
                self.insert(key[1])
            return None
        line = self.lines[self.row]
        if len(key) == 1:
            self.insert(key)
        elif key == "tab":
            self.insert("    ")
        elif key == "enter":
            text = self.text
            if not text.strip():
                return None
            eid = self.edit_id
            self.clear()
            return ("submit", text, eid)
        elif key == "C-j":
            self.insert("\n")
        elif key in ("C-a", "home"):
            self.col, self.goal = 0, None
        elif key in ("C-e", "end"):
            self.col, self.goal = len(line), None
        elif key in ("left", "C-b"):
            if self.col > 0:
                self.col -= 1
            elif self.row > 0:
                self.row -= 1
                self.col = len(self.lines[self.row])
            self.goal = None
        elif key in ("right", "C-f"):
            if self.col < len(line):
                self.col += 1
            elif self.row < len(self.lines) - 1:
                self.row += 1
                self.col = 0
            self.goal = None
        elif key in ("up", "C-p"):
            if self.row == 0:
                self._history(-1)
            else:
                self._vmove(-1)
        elif key in ("down", "C-n"):
            if self.row == len(self.lines) - 1:
                self._history(1)
            else:
                self._vmove(1)
        elif key == "backspace":
            if self.col > 0:
                self.lines[self.row] = line[:self.col - 1] + line[self.col:]
                self.col -= 1
            elif self.row > 0:
                prev = self.lines[self.row - 1]
                self.lines[self.row - 1:self.row + 1] = [prev + line]
                self.row -= 1
                self.col = len(prev)
            self.goal = None
        elif key == "del" or (key == "C-d" and self.text):
            if self.col < len(line):
                self.lines[self.row] = line[:self.col] + line[self.col + 1:]
            elif self.row < len(self.lines) - 1:
                self.lines[self.row:self.row + 2] = [line + self.lines[self.row + 1]]
        elif key == "C-d":
            return ("exit",)
        elif key == "C-k":
            if self.col < len(line):
                self.lines[self.row] = line[:self.col]
            elif self.row < len(self.lines) - 1:
                self.lines[self.row:self.row + 2] = [line + self.lines[self.row + 1]]
        elif key == "C-u":
            self.lines[self.row] = line[self.col:]
            self.col = 0
        elif key in ("C-w", "M-\x7f"):
            j = self.col
            while j > 0 and line[j - 1].isspace():
                j -= 1
            while j > 0 and not line[j - 1].isspace():
                j -= 1
            if j == self.col and self.col == 0 and self.row > 0:
                return self.handle("backspace")
            self.lines[self.row] = line[:j] + line[self.col:]
            self.col = j
        elif key == "C-c":
            if self.text or self.edit_id is not None:
                self.clear()
            else:
                return ("exit",)
        elif key == "esc":
            return ("stop",)
        elif key == "pgup":
            return ("scroll", 1)
        elif key == "pgdn":
            return ("scroll", -1)
        elif key == "C-l":
            return ("redraw",)
        return None


def editor_view(ed, width, max_h, prefix="❯ "):
    """Lays the editor out in rows. Returns (rows, cursor_row, cursor_col) with rows already
    windowed to at most max_h so the cursor is visible."""
    pw = str_width(prefix)
    avail = max(1, width - pw)
    rows = []
    cur = (0, pw)
    for li, line in enumerate(ed.lines):
        chunks, starts = [], []
        c, w, start = "", 0, 0
        for i, ch in enumerate(line):
            cw = char_width(ch)
            if w + cw > avail:
                chunks.append(c)
                starts.append(start)
                c, w, start = "", 0, i
            c += ch
            w += cw
        chunks.append(c)
        starts.append(start)
        for k, (ch, st) in enumerate(zip(chunks, starts)):
            if li == ed.row:
                end = st + len(ch)
                last = k == len(chunks) - 1
                if st <= ed.col < end or (last and ed.col >= st):
                    cur = (len(rows), pw + str_width(line[st:ed.col]))
                    if cur[1] >= width:
                        cur = (len(rows) + 1, pw)
            p = prefix if (li == 0 and k == 0) else " " * pw
            rows.append(p + ch)
    if cur[0] >= len(rows):
        rows.append(" " * pw)
    max_h = max(1, max_h)
    top = 0
    if len(rows) > max_h:
        top = min(max(0, cur[0] - max_h + 1), len(rows) - max_h)
    return rows[top:top + max_h], cur[0] - top, cur[1]


# ---------------------------------------------------------------------------------------------
# transcript rendering

def styled(text, style):
    return style + text + RESET if style else text


def inline_md(line, base=""):
    def rep(m):
        if m.group(1) is not None:
            return BOLD + m.group(1) + "\x1b[22m" + base
        return CYAN + m.group(2) + "\x1b[39m" + base
    return re.sub(r"\*\*(.+?)\*\*|`([^`]+)`", rep, line)


def user_text(msg):
    return "\n".join(b.get("text", "") for b in msg.get("content") or [] if b.get("type") == "text")


def is_prompt_msg(msg):
    """A user message the user typed (not tool results, a delivered subagent result or a /compact summary)."""
    return (msg.get("role") == "user" and not is_tool_result_msg(msg) and not is_compaction_msg(msg)
            and not is_peer_msg(msg) and not parse_subagent_result(user_text(msg)))


def is_peer_msg(msg):
    """Delivered peer messages / <peer-sessions> notes (stored as user messages, not typed by the user)."""
    c = msg.get("content") or []
    return msg.get("role") == "user" and bool(c) and all(b.get("peer_message") or b.get("peer_sessions") for b in c)


def render_peer_blocks(msg, width):
    out = []
    for b in msg.get("content") or []:
        pm = b.get("peer_message")
        if pm:
            title = "✉ %s from %s \"%s\"%s" % (pm.get("kind") or "message", pm.get("from", "?"), pm.get("from_title", ""),
                                             " ↩ " + pm["reply_to"] if pm.get("reply_to") else "")
            out += [""] + render_box(title, render_markdown(pm.get("text") or "", width - 2), width, YELLOW)
        elif b.get("peer_sessions") is not None:
            ids = b["peer_sessions"]
            out += ["", styled(clip("⇄ peers active in this repo: %s" % (", ".join(ids) if ids else "none now"),
                                    width), DIM)]
    return out


def is_compaction_msg(msg):
    return msg.get("role") == "user" and any(b.get("compaction") for b in msg.get("content") or [])


def is_tool_result_msg(msg):
    c = msg.get("content") or []
    return msg.get("role") == "user" and bool(c) and all(b.get("type") == "tool_result" for b in c)


def _result_text(content):
    if isinstance(content, list):
        return "\n".join(b.get("text", "") for b in content if isinstance(b, dict))
    return str(content or "")


def tool_summary(block):
    name = block.get("name", "?")
    inp = block.get("input")
    if inp is None and "partial_json" in block:
        try:
            inp = json.loads(block["partial_json"])
        except Exception:
            return "%s(%s" % (name, (block.get("partial_json") or "")[-60:])
    inp = inp or {}
    if name == "subagent" and isinstance(inp, dict) and isinstance(inp.get("description"), str):
        return "subagent(%s)" % inp["description"].split("\n")[0]
    if isinstance(inp, dict):
        for k in ("command", "path", "file_path", "pattern", "url", "name"):
            if isinstance(inp.get(k), str):
                return "%s(%s)" % (name, inp[k].split("\n")[0])
    return "%s(%s)" % (name, json.dumps(inp, ensure_ascii=False))


ARTIFACT_FENCE = re.compile(r"\s{0,3}(`{3,}|~{3,})\s*artifact(?:\s+(.*))?$", re.I)


def artifact_title(info):
    """Title from an artifact fence's info string: `artifact title="…"` or `artifact Some title`."""
    info = info or ""
    m = re.search(r"\btitle\s*=\s*(?:\"([^\"]*)\"|'([^']*)'|(\S+))", info, re.I)
    t = next((g for g in m.groups() if g is not None), "") if m else info.strip().strip("\"'")
    return t.strip() or "Artifact"


def render_markdown(text, width):
    out = []
    in_code = False
    artifact = None  # the opening fence while inside an artifact block (its HTML is not shown here)
    for src in clean_text(text).split("\n"):
        if artifact is not None:
            s = src.strip()
            if s.startswith(artifact) and not s.strip(artifact[0]):
                artifact = None
            continue
        m = None if in_code else ARTIFACT_FENCE.match(src)
        if m:
            artifact = m.group(1)
            out.append("  " + styled(clip("[artifact: %s — open in web UI]" % artifact_title(m.group(2)), width - 2), CYAN))
            continue
        if src.lstrip().startswith("```"):
            in_code = not in_code
            out.append(styled("  " + clip(src, width - 2), DIM))
            continue
        if in_code:
            for l in wrap(src, width - 4) or [""]:
                out.append("  " + styled("│ " + l, GREEN))
            continue
        m = re.match(r"(#{1,6})\s+(.*)", src)
        if m:
            for l in wrap(m.group(2), width - 2):
                out.append("  " + styled(l, BOLD + YELLOW))
            continue
        m = re.match(r"(\s*)[-*+]\s+(.*)", src)
        if m:
            ind = m.group(1)
            ls = wrap(m.group(2), width - 4 - len(ind))
            for k, l in enumerate(ls):
                out.append("  " + ind + ("• " if k == 0 else "  ") + inline_md(l))
            continue
        for l in wrap(src, width - 2):
            out.append("  " + inline_md(l))
    return out


def fmt_dur(ms):
    """3.4s, 42s, 2m 05s, 1h 03m."""
    s = max(0, ms or 0) / 1000.0
    if s < 10:
        return "%.1fs" % s
    if s < 60:
        return "%ds" % s
    m = int(s // 60)
    if m < 60:
        return "%dm %02ds" % (m, int(s) % 60)
    return "%dh %02dm" % (m // 60, m % 60)


def live_dur(start, now):
    """Elapsed time of something still running, in TICK_MS steps (5s, 10s, … 1m 05s)."""
    e = max(0, (now or 0) - (start or 0)) // TICK_MS * TICK_MS
    return "%ds" % (e // 1000) if e < 60000 else fmt_dur(e)


def block_dur(b):
    """ms a block streamed for (first to last delta), or None for blocks stored without timing."""
    if b.get("started_at") and b.get("ended_at"):
        return b["ended_at"] - b["started_at"]
    return None


def render_thinking(text, width, label=None):
    style = DIM + ITALIC
    head = [styled(clip("┊ " + label, width), DIM)] if label else []
    return head + [styled("┊ ", DIM) + styled(l, style) for l in wrap(text.strip("\n"), width - 2)]


def thinking_label(b, live=None):
    """"thought for 12s" (stored), "thinking… 10s" (streaming now), or None (no timing)."""
    if live is not None and live.get("now") and b.get("started_at"):
        return "thinking… %s" % live_dur(b["started_at"], live["now"])
    d = block_dur(b)
    return "thought for %s" % fmt_dur(d) if d is not None else None


def tool_dur_label(block, result, live=None):
    """" · 3.4s" (finished), " · 10s…" (running now) or ""."""
    if result is not None:
        d = block_dur(result)
        return " · " + fmt_dur(d) if d is not None else ""
    run = ((live or {}).get("tools") or {}).get(block.get("id"))
    if run and run.get("ended_at"):
        return " · " + fmt_dur(run["ended_at"] - run["started_at"])
    if run and live.get("now"):
        return " · %s…" % live_dur(run["started_at"], live["now"])
    return ""


def subagent_child_id(block, result):
    """The child session id a subagent tool_use launched (from its JSON result), or None."""
    if block.get("name") != "subagent" or result is None or result.get("is_error"):
        return None
    try:
        sid = json.loads(_result_text(result.get("content"))).get("session_id")
    except Exception:
        return None
    return sid if isinstance(sid, str) else None


def render_tool(block, result, width, max_result_lines=4, live=None):
    dur = tool_dur_label(block, result, live)
    child = subagent_child_id(block, result)
    if child:
        arrow = " → " + child
        head = clip(tool_summary(block), max(1, width - 2 - str_width(arrow + dur)))
        return [styled("⏺ ", GREEN) + styled(head, BOLD) + styled(arrow, CYAN) + styled(dur, DIM)]
    out = [styled("⏺ ", GREEN) + styled(clip(tool_summary(block), max(1, width - 2 - str_width(dur))), BOLD) +
           styled(dur, DIM)]
    if result is None:
        return out
    text = _result_text(result.get("content")).rstrip("\n")
    style = RED if result.get("is_error") else DIM
    lines = text.split("\n") if text else ["(no output)"]
    for k, l in enumerate(lines[:max_result_lines]):
        out.append(styled(("  ⎿ " if k == 0 else "    ") + clip(clean_text(l), width - 4), style))
    if len(lines) > max_result_lines:
        out.append(styled("    … +%d lines" % (len(lines) - max_result_lines), DIM))
    return out


def render_blocks(blocks, width, results, live=None, streaming=False):
    """live: {"now": server time ms, "tools": {tool_use id: {"started_at", "ended_at"?}}} for running timers;
    streaming: these are the blocks of the response streaming now (its last block is still growing)."""
    out = []
    for i, b in enumerate(blocks):
        t = b.get("type")
        if t == "thinking":
            if b.get("thinking"):
                growing = streaming and i == len(blocks) - 1
                out += render_thinking(b["thinking"], width, thinking_label(b, live if growing else None))
        elif t == "redacted_thinking":
            out.append(styled("┊ (redacted thinking)", DIM + ITALIC))
        elif t == "text":
            if b.get("text"):
                out += render_markdown(b["text"], width)
        elif t == "tool_use":
            out += render_tool(b, results.get(b.get("id")), width, live=live)
    return out


def message_timing(msg):
    """"text 4.2s · response 18.3s" for an assistant message with text (how long its text streamed, and its
    whole request from start to end); "" otherwise or without timing."""
    texts = [block_dur(b) for b in msg.get("content") or [] if b.get("type") == "text" and b.get("text")]
    if not texts or any(d is None for d in texts):
        return ""
    parts = ["text " + fmt_dur(sum(texts))]
    if msg.get("started_at") and msg.get("created_at"):
        parts.append("response " + fmt_dur(msg["created_at"] - msg["started_at"]))
    return " · ".join(parts)


def render_box(title, body, width, color):
    out = [styled("╭─ " + clip(title, width - 4) + " ", color)]
    for l in body:
        out.append(styled("│ ", color) + l)
    out.append(styled("╰─", color))
    return out


SUBAGENT_RESULT_RE = re.compile(r"^<subagent-result((?:\s+[\w-]+=(?:\"[^\"]*\"|'[^']*'))*)\s*>(.*?)(?:</subagent-result>\s*)?$",
                                re.S)


def parse_subagent_result(text):
    """{"session_id", "description", "status", "body"} for a <subagent-result> user message, else None."""
    m = SUBAGENT_RESULT_RE.match(text or "")
    if not m:
        return None
    attrs = {k: html.unescape(dq if q.startswith('"') else sq)
             for k, q, dq, sq in re.findall(r"([\w-]+)=(\"([^\"]*)\"|'([^']*)')", m.group(1))}
    return {"session_id": attrs.get("session_id", ""), "description": attrs.get("description", ""),
            "status": attrs.get("status", ""), "body": m.group(2)}


def render_subagent_result(r, width, max_lines=6):
    status = r["status"] or "completed"
    color = RED if status == "error" else (DIM if status == "stopped" else MAGENTA)
    title = "⤷ subagent %s [%s]" % (r["description"] or r["session_id"] or "?", status)
    body = render_markdown(r["body"].strip() or "(no output)", width - 2)
    if len(body) > max_lines:
        body = body[:max_lines] + [styled("  … +%d lines" % (len(body) - max_lines), DIM)]
    if r["session_id"]:
        body.append(styled("  " + clip("session " + r["session_id"], width - 4), DIM))
    return render_box(title, body, width, color)


def render_message(msg, width, results, live=None):
    if msg.get("role") == "user":
        if is_tool_result_msg(msg):
            return []
        if is_compaction_msg(msg):
            text = user_text(msg)
            body = render_markdown(text, width - 2)
            n = next((b.get("compacted") for b in msg["content"] if b.get("compaction")), None)
            title = "⇣ conversation compacted%s · ~%s tokens · the model continues from here" % (
                " (%s messages)" % n if n else "", fmt_tokens(len(text) // 4))
            if len(body) > 8:
                body = body[:8] + [styled("  … +%d lines of summary" % (len(body) - 8), DIM)]
            return [""] + render_box(title, body, width, DIM)
        if is_peer_msg(msg):
            return render_peer_blocks(msg, width)
        sr = parse_subagent_result(user_text(msg))
        if sr:
            return [""] + render_subagent_result(sr, width)
        ls = wrap(user_text(msg), width - 2) or [""]
        out = [""] + [styled("❯ " if k == 0 else "  ", BOLD + CYAN) + styled(l, BOLD) for k, l in enumerate(ls)]
        v = msg.get("versions") or 1
        if isinstance(v, int) and v > 1:
            out.append(styled("  (edited, %d versions)" % v, DIM))
        return out
    out = [""] + render_blocks(msg.get("content") or [], width, results, live)
    timing = message_timing(msg)
    if timing:
        out.append(styled(clip("  ⏱ " + timing, width), DIM))
    return out


def render_item(item, width, results, live=None):
    kind = item[0]
    if kind == "msg":
        return render_message(item[1], width, results, live)
    if kind == "notice":
        d = item[1]
        color = RED if d.get("level") == "error" else BLUE
        return [""] + render_box("notice · not in context", render_markdown(d.get("text", ""), width - 2),
                                 width, color)
    if kind == "btw":
        d = item[1]
        body = render_markdown(d.get("text", ""), width - 2)
        if d.get("error"):
            body += [styled(l, RED) for l in wrap(str(d["error"]), width - 4)]
        if not d.get("done"):
            body.append(styled("…", DIM))
        return [""] + render_box("/btw %s · not in context" % (d.get("question") or ""), body, width, MAGENTA)
    if kind == "local":
        return [styled(l, DIM) for l in wrap(item[1], width)]
    return []


def collect_results(items):
    res = {}
    for it in items:
        if it[0] == "msg":
            for b in it[1].get("content") or []:
                if b.get("type") == "tool_result":
                    res[b.get("tool_use_id")] = b
    return res


def turn_ends(items, running=False):
    """{index of a turn's last message item: turn duration ms}. A turn runs from a prompt to the last message
    before the next prompt (its end: that message's created_at); the newest turn has no end while running,
    and turns without an assistant reply get none."""
    out = {}
    start = last = None
    answered = False

    def close():
        if start is not None and last is not None and answered:
            out[last[0]] = last[1] - start

    for i, it in enumerate(items):
        if it[0] != "msg":
            continue
        m = it[1]
        if starts_turn(m):
            close()
            start, last, answered = m.get("created_at"), None, False
            continue
        if start is not None and m.get("created_at"):
            last = (i, m["created_at"])
            answered = answered or m.get("role") == "assistant"
    if not running:
        close()
    return out


def starts_turn(msg):
    """A typed prompt or a delivered subagent result (both start a turn of the session)."""
    return is_prompt_msg(msg) or (msg.get("role") == "user" and bool(parse_subagent_result(user_text(msg))))


def turn_start(items):
    """created_at of the newest turn start (when the running turn started), or None."""
    for it in reversed(items):
        if it[0] == "msg" and starts_turn(it[1]):
            return it[1].get("created_at")
    return None


def llm_change_label(prev, msg):
    """"⇄ model x · effort high" when an assistant message was made with another model or effort level than the
    previous one, else None."""
    if prev is None:
        return None
    parts = []
    if (msg.get("model"), msg.get("provider")) != (prev.get("model"), prev.get("provider")):
        parts.append("model %s (%s)" % (msg.get("model") or "?", msg.get("provider") or "?"))
    if msg.get("effort") != prev.get("effort"):
        parts.append("effort %s" % (msg.get("effort") or "none"))
    return "⇄ " + " · ".join(parts) if parts else None


def render_transcript(items, draft, width, cache=None, live=None, running=False):
    """live: running timers (see render_blocks); running: the session's turn is in progress."""
    results = collect_results(items)
    ends = turn_ends(items, running)
    pending = live and live.get("tools") and live.get("now")
    out = []
    prev = None
    for i, it in enumerate(items):
        if it[0] == "msg" and it[1].get("role") == "assistant":
            change = llm_change_label(prev, it[1])
            if change:
                out += ["", styled(clip("  " + change, width), DIM)]
            prev = it[1]
        key = None
        if cache is not None and it[0] == "msg" and it[1].get("id") is not None:
            ids = tuple(b.get("id") for b in it[1].get("content") or [] if b.get("type") == "tool_use")
            key = (it[1]["id"], width, tuple(i in results for i in ids))
            if pending and any(t not in results and t in live["tools"] for t in ids):
                key += (live["now"] // TICK_MS,)  # a tool of it runs: its timer advances
            if key in cache:
                lines = cache[key]
            else:
                lines = cache[key] = render_item(it, width, results, live)
        else:
            lines = render_item(it, width, results, live)
        out += lines
        if i in ends:
            out.append(styled(clip("  ── turn took %s" % fmt_dur(ends[i]), width), DIM))
    if draft and draft.get("content"):
        out.append("")
        out += render_blocks(draft["content"], width, {}, live, streaming=True)
    return out


# ---------------------------------------------------------------------------------------------
# /resume picker

def relative_time(ms, now_ms):
    if not ms:
        return ""
    d = max(0, (now_ms - ms) // 1000)
    for unit, n in (("d", 86400), ("h", 3600), ("m", 60)):
        if d >= n:
            return "%d%s ago" % (d // n, unit)
    return "just now"


def nest_sessions(sessions):
    """[(session, depth)] with subagent sessions right after (indented under) their parent, keeping the
    given order among siblings. Children of unknown parents stay at top level."""
    ids = {s.get("id") for s in sessions}
    kids = {}
    roots = []
    for s in sessions:
        p = s.get("parent_id")
        if p and p != s.get("id") and p in ids:
            kids.setdefault(p, []).append(s)
        else:
            roots.append(s)
    out, seen = [], set()

    def walk(s, depth):
        if s.get("id") in seen:
            return
        seen.add(s.get("id"))
        out.append((s, depth))
        for k in kids.get(s.get("id"), []):
            walk(k, depth + 1)
    for s in roots:
        walk(s, 0)
    for s in sessions:  # cycles
        if s.get("id") not in seen:
            walk(s, 0)
    return out


def tree_prefix(depth):
    return "  " * (depth - 1) + "↳ " if depth else ""


class SessionPicker:
    """Interactive session list: type to filter, up/down (C-p/C-n) to move, enter/esc.
    handle(key) returns ("resume", session_id), ("cancel",) or None."""
    prompt = "resume: "
    action = "resume"

    def __init__(self, sessions, now_ms=None):
        self.sessions = [dict(s, _depth=d) for s, d in nest_sessions(list(sessions))]
        self.now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        self.query = ""
        self.sel = 0
        self.top = 0

    @property
    def matches(self):
        words = self.query.lower().split()
        out = []
        for s in self.sessions:
            hay = " ".join(str(s.get(k) or "") for k in ("title", "id", "cwd")).lower()
            if all(w in hay for w in words):
                out.append(s)
        return out

    def handle(self, key):
        if isinstance(key, tuple):
            if key[0] == "paste":
                self.query += clean_text(key[1]).replace("\n", " ")
                self.sel = 0
            return None
        n = len(self.matches)
        if key in ("esc", "C-c", "C-g"):
            return ("cancel",)
        if key == "enter":
            m = self.matches
            return (self.action, m[self.sel]["id"]) if m else None
        if key in ("up", "C-p"):
            self.sel = max(0, self.sel - 1)
        elif key in ("down", "C-n", "tab"):
            self.sel = min(max(0, n - 1), self.sel + 1)
        elif key == "pgup":
            self.sel = max(0, self.sel - 10)
        elif key == "pgdn":
            self.sel = min(max(0, n - 1), self.sel + 10)
        elif key == "backspace":
            self.query = self.query[:-1]
            self.sel = 0
        elif key in ("C-u", "C-w"):
            self.query = ""
            self.sel = 0
        elif len(key) == 1:
            self.query += key
            self.sel = 0
        return None

    def render(self, width, height):
        """Exactly `height` rows (>= 1): a filter line, then the visible part of the list."""
        m = self.matches
        rows = [styled("resume: ", BOLD) + self.query + styled("▏", DIM) +
                styled("  %d/%d · ↑/↓ move · Enter resume · Esc cancel" % (len(m), len(self.sessions)), DIM)]
        avail = max(0, height - 1)
        if self.sel < self.top:
            self.top = self.sel
        elif avail and self.sel >= self.top + avail:
            self.top = self.sel - avail + 1
        self.top = max(0, min(self.top, max(0, len(m) - avail)))
        for i in range(self.top, min(len(m), self.top + avail)):
            s = m[i]
            meta = "%s · %s · %s msgs · %s" % (s.get("id", ""), relative_time(s.get("updated_at"), self.now_ms),
                                              s.get("message_count", 0), s.get("cwd") or "")
            title = tree_prefix(s.get("_depth", 0)) + (s.get("title") or "(untitled)")
            if s.get("children"):
                meta += " · %d subagent%s" % (s["children"], "" if s["children"] == 1 else "s")
            line = clip("%s  %s" % (title, meta), max(1, width - 2))
            if i == self.sel:
                rows.append(REVERSE + "❯ " + line + " " * max(0, width - 2 - str_width(line)) + RESET)
            else:
                t = clip(title, max(1, width - 2))
                rows.append("  " + t + styled(line[len(t):], DIM))
        if not m:
            rows.append(styled("  (no matching sessions)", DIM))
        rows = rows[:max(1, height)]
        return rows + [""] * (max(1, height) - len(rows))


def fmt_tokens(n):
    n = int(n or 0)
    if n < 1000:
        return str(n)
    if n < 1000000:
        return ("%.1fk" if n < 10000 else "%.0fk") % (n / 1000.0)
    return "%.2fM" % (n / 1e6)


def usage_label(session):
    """"$0.0123 · 12.3k in · 2.1k out" for a session summary (incl. subagents); "" before any response.
    The cost shows when the provider reported one or its price list gave one ("~": some of it is
    estimated from the price list; "+": some responses have no cost)."""
    total = {}
    for u in (session.get("usage"), session.get("usage_subagents")):
        for k, v in (u or {}).items():
            total[k] = (total.get(k) or 0) + (v or 0)
    if not total.get("calls"):
        return ""
    if total.get("priced_calls"):
        c = float(total.get("cost") or 0)
        cost = ("~" if total.get("estimated_calls") else "") + "$" + ("%.4f" % c if c < 0.01 else "%.2f" % c) + ("+" if total["priced_calls"] < total["calls"] else "")
    else:
        cost = "cost n/a"
    tin = sum(int(total.get(k) or 0) for k in ("input_tokens", "cache_read_tokens", "cache_write_tokens"))
    return "%s · %s in · %s out" % (cost, fmt_tokens(tin), fmt_tokens(total.get("output_tokens")))


def context_label(session):
    """"ctx 45.2k/200k 23%" (or just "ctx 45.2k" when the window size is unknown)."""
    ctx = session.get("context") or {}
    if ctx.get("used") is None:
        return "ctx –/%s" % fmt_tokens(ctx["window"]) if ctx.get("window") else ""
    used = ("~" if ctx.get("estimated") else "") + fmt_tokens(ctx["used"])
    if ctx.get("window"):
        return "ctx %s/%s %d%%" % (used, fmt_tokens(ctx["window"]), round(100.0 * ctx["used"] / ctx["window"]))
    return "ctx " + used


def effort_label(s):
    """The effort level a session uses: "high", "default" (none set), "xhigh→high" / "high→none" (the model
    doesn't take the wanted level)."""
    want, eff = s.get("effort_wanted"), s.get("effort")
    if not want:
        return "default"
    if want != eff:
        return "%s→%s" % (want, eff or "none")
    return want


def provider_label(p):
    """hello.provider is {"id","type","model","cheap_model"} (older servers: a plain string)."""
    if isinstance(p, dict):
        name = p.get("id") or p.get("type") or ""
        return "%s:%s" % (name, p["model"]) if p.get("model") else name
    return str(p or "")


class ModelPicker(SessionPicker):
    """/model without an argument: the provider's models (from models.list), type to filter.
    handle(key) returns ("model", model_id) ("" = the provider's default), ("cancel",) or None."""
    prompt = "model: "
    action = "model"

    def __init__(self, models, current=None, provider=""):
        self.sessions = [{"id": "", "name": "provider default"}] + [dict(m) for m in models]
        self.current = current
        self.provider = provider
        self.query = ""
        self.top = 0
        ids = [m["id"] for m in self.sessions]
        self.sel = ids.index(current) if current in ids else 0

    @property
    def matches(self):
        words = self.query.lower().split()
        return [m for m in self.sessions
                if all(w in (m["id"] + " " + (m.get("name") or "")).lower() for w in words)]

    def render(self, width, height):
        m = self.matches
        rows = [styled(self.prompt, BOLD) + self.query + styled("▏", DIM) +
                styled("  %d/%d of %s · ↑/↓ move · Enter choose · Esc cancel" % (
                    len(m), len(self.sessions), self.provider or "the provider"), DIM)]
        avail = max(0, height - 1)
        if self.sel < self.top:
            self.top = self.sel
        elif avail and self.sel >= self.top + avail:
            self.top = self.sel - avail + 1
        self.top = max(0, min(self.top, max(0, len(m) - avail)))
        for i in range(self.top, min(len(m), self.top + avail)):
            x = m[i]
            eff = x.get("efforts_override") if x.get("efforts_override") is not None else x.get("efforts")
            label = (x["id"] or "(default)") + ("  " + x["name"] if x.get("name") else "") + \
                ("  effort: " + ("/".join(eff) or "none") if eff is not None else "") + \
                ("  ✓ in use" if x["id"] and x["id"] == self.current else "")
            line = clip(label, max(1, width - 2))
            if i == self.sel:
                rows.append(REVERSE + "❯ " + line + " " * max(0, width - 2 - str_width(line)) + RESET)
            else:
                rows.append("  " + line)
        return rows + [""] * (height - len(rows))


# ---------------------------------------------------------------------------------------------
# server connection

def _log_path(path):
    return os.path.join(common.state_dir(), "%08x.log" % common.fnv1a32(path))


def spawn_server(path):
    os.makedirs(common.state_dir(), exist_ok=True)
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        bundled = "#--SFH-PROGRAM-BEGIN--" in f.read()
    if bundled:
        cmd = ["sh", path, "serve"]
    else:  # dev mode (PYTHONPATH=src/py:vendor python3 -m sfh <html> tui)
        cmd = [sys.executable, "-m", "sfh", path, "serve"]
    log = open(_log_path(path), "ab")
    subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
                     close_fds=True)
    log.close()


def ensure_server(path, wait=10.0):
    port = common.find_server(path)
    if port:
        return port
    ensure_server.spawned = True
    sys.stderr.write("starting server (log: %s)...\n" % _log_path(path))
    spawn_server(path)
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.2)
        port = common.find_server(path, timeout=0.3)
        if port:
            # a fresh file gets its access token written by the server on startup
            while not htmlfile.read_token(path) and time.time() < deadline:
                time.sleep(0.1)
            return port
    raise SystemExit("server did not start; see " + _log_path(path))


ensure_server.spawned = False   # set when this process started the background server


SETUP_STEPS = ("base", "key", "model")
SETUP_PROMPTS = {"base": "Base URL", "key": "API key", "model": "Model"}


def parse_args(args):
    opts = {"list": False, "resume": None, "cwd": None}
    it = iter(args)
    for a in it:
        if a == "--list":
            opts["list"] = True
        elif a == "--resume":
            opts["resume"] = next(it, None)
        elif a.startswith("--resume="):
            opts["resume"] = a.split("=", 1)[1]
        elif a == "--cwd":
            opts["cwd"] = next(it, None)
        elif a.startswith("--cwd="):
            opts["cwd"] = a.split("=", 1)[1]
        elif a in ("-h", "--help"):
            print("usage: tui [--list] [--resume ID] [--cwd DIR]\n"
                  "Mouse wheel / PgUp / PgDn scroll the transcript. With mouse reporting on, select text with\n"
                  "Shift+drag; /mouse (or setting tui_mouse=false) turns it off.")
            raise SystemExit(0)
        else:
            raise SystemExit("tui: unknown argument " + a)
    return opts


def format_session(s, depth=0):
    return "%s  %-8s %3s msgs  %s%s  (%s)" % (s.get("id"), s.get("status", ""), s.get("message_count", 0),
                                              tree_prefix(depth), s.get("title") or "(untitled)", s.get("cwd") or "")


def format_sessions(lst):
    return [format_session(s, d) for s, d in nest_sessions(lst)]


# ---------------------------------------------------------------------------------------------
# the app

class App:
    def __init__(self, path, opts):
        self.path = path
        self.opts = opts
        self.cwd = os.path.abspath(opts.get("cwd") or os.getcwd())
        self.rpc = None
        self.session = None
        self.items = []
        self.draft = None
        self.queue = []
        self.editor = Editor()
        self.parser = KeyParser()
        self.scroll = 0
        self.flash = ""
        self.cache = {}
        self.dirty = True
        self.rows, self.cols = 24, 80
        self.tlines_h = 10
        self.quit = False
        self.picker = None
        self.hello = {}
        self.mouse = True        # mouse wheel scrolling wanted (setting tui_mouse, /mouse)
        self.term_active = False  # raw mode + alternate screen (+ mouse reporting) are on
        self.out_fd = None
        self.anchor = None       # (width, transcript line count) of the last frame, to hold a scrolled view
        self.setup = None        # first run (no history, no provider): the provider setup screen's state
        self.tools = {}          # tool_use id -> {"started_at", "ended_at"?}: the running step's tool runs
        self.skew = 0            # server clock - local clock (ms), for live timers
        self.tick = None         # the TICK_MS step of the last frame drawn while a turn runs

    # -- connection / session
    def connect(self):
        port = ensure_server(self.path)
        try:
            self.rpc = wsclient.RPC.connect(port, self.path, token=htmlfile.read_token(self.path))
        except wsclient.AuthError:
            raise SystemExit("tui: " + common.TOKEN_CHANGED)
        self.hello = self.rpc.call("hello", {"client": "tui", "cwd": self.cwd})
        self.mouse = (self.hello.get("settings") or {}).get("tui_mouse", True) is not False

    # -- onboarding (questions/034): same rules as the web page
    def onboarding(self):
        return (self.hello or {}).get("onboarding") or {"has_history": True, "has_provider": True, "moved": False}

    def provider_ready(self):
        return bool(self.onboarding().get("has_provider", True))

    def needs_setup(self):
        o = self.onboarding()
        return not o.get("has_history") and not o.get("has_provider")

    def start_setup(self):
        self.setup = {"step": "base", "spec": {"type": "openai", "base_url": "", "token": "", "model": ""},
                      "models": [], "msg": "", "level": "", "tested": None}
        self.editor.clear()
        self.dirty = True

    def finish_setup(self):
        """A provider exists now (set up here, in a browser, or by another client): open the session."""
        self.setup = None
        self.editor.clear()
        self.open_session(self.opts.get("resume"))
        self.items.append(("local", "Provider ready. Ask anything."))
        self.dirty = True

    def setup_key(self, key):
        st = self.setup
        if key == "enter" and not self.editor.text.strip():
            act = ("submit", "", None)   # an empty API key is fine
            self.editor.clear()
        else:
            act = self.editor.handle(key)
        if act is None:
            return
        if act[0] == "exit":
            self.quit = True
        elif act[0] == "submit":
            self.setup_submit(act[1].strip())
        elif act[0] == "stop" and st["step"] != "base":   # Esc: back one step
            st["step"] = SETUP_STEPS[SETUP_STEPS.index(st["step"]) - 1]
            st["msg"] = ""
        self.dirty = True

    def setup_submit(self, text):
        st, spec = self.setup, self.setup["spec"]

        def say(msg, level=""):
            st["msg"], st["level"] = msg, level
        if st["step"] == "base":
            if not text:
                return say("Enter the base URL of an OpenAI-compatible API, or `anthropic`.", "error")
            if text.lower() == "anthropic":
                spec.update(type="anthropic", base_url="")
            else:
                spec.update(type="openai", base_url=text)
            st["step"] = "key"
            return say("")
        if st["step"] == "key":
            spec["token"] = text
            st["step"] = "model"
            try:
                r = self.rpc.call("providers.probe", spec)
            except wsclient.RPCError as e:
                r = {"models": [], "error": str(e)}
            st["models"] = [m["id"] for m in r.get("models") or []]
            if r.get("error"):
                return say("Couldn't list models (%s). Type a model id, or Esc to go back." % r["error"], "error")
            return say("%d models available." % len(st["models"]))
        if st["step"] == "model":
            if not text:
                return say("Type a model id.", "error")
            spec["model"] = text
            if st["tested"] != text:   # test first; entering the same model again after a failure saves anyway
                say("Testing %s…" % text)
                self.draw()
                try:
                    r = self.rpc.call("providers.test", spec)
                except wsclient.RPCError as e:
                    r = {"ok": False, "error": str(e)}
                st["tested"] = text
                if not r.get("ok"):
                    return say("Test failed: %s. Fix it (Esc goes back), or press Enter on the same model to save "
                               "anyway." % r.get("error"), "error")
            try:
                r = self.rpc.call("providers.setup", spec)
            except wsclient.RPCError as e:
                return say("Saving failed: %s" % e, "error")
            self.hello["onboarding"] = r.get("onboarding") or dict(self.onboarding(), has_provider=True)
            self.finish_setup()

    def setup_frame(self):
        W, H = self.cols, self.rows
        st, spec = self.setup, self.setup["spec"]
        head = " Set up · %s " % os.path.basename(self.path)
        out = [REVERSE + clip(head, W) + " " * max(0, W - str_width(clip(head, W))) + RESET, ""]
        width = min(W - 2, 100)

        def para(text, style=""):
            for line in wrap(text, width):
                out.append(" " + (styled(line, style) if style else line))
        para(about.ABOUT["tagline"], BOLD)
        out.append("")
        para(about.ABOUT["what"].replace(about.ABOUT["tagline"], "").strip())
        out.append("")
        para("Connect an LLM", BOLD)
        para(about.ABOUT["provider"])
        out.append("")
        shown = {"base": spec["base_url"] or ("anthropic" if spec["type"] == "anthropic" else ""),
                 "key": "•" * min(len(spec["token"]), 12) if spec["token"] else "",
                 "model": spec["model"]}
        cur_i = SETUP_STEPS.index(st["step"])
        for i, k in enumerate(SETUP_STEPS):
            mark = styled("❯", BOLD + CYAN) if i == cur_i else (styled("✓", GREEN) if i < cur_i else " ")
            val = shown[k] if i < cur_i else ""
            if k == "key" and i < cur_i and not val:
                val = "(none)"
            out.append(" %s %d. %-9s %s" % (mark, i + 1, SETUP_PROMPTS[k], val))
        if st["msg"]:
            out.append("")
            para(st["msg"], RED if st["level"] == "error" else YELLOW)
        if st["step"] == "model" and st["models"]:
            line = ", ".join(st["models"][:40]) + (" …" if len(st["models"]) > 40 else "")
            for l in wrap(line, width - 2)[:6]:
                out.append("   " + styled(l, DIM))
        out.append("")
        para("Alternatives: open file://%s in a browser (it has the same form), or quit, export AI_ENDPOINT, "
             "AI_MODEL (and AI_TOKEN), and run this again%s." % (
                 self.path, ", since leaving this screen stops the server this TUI started" if ensure_server.spawned
                 else "; the server must be restarted to see them"), DIM)
        hint = {"base": "e.g. http://127.0.0.1:11434/v1 · https://api.openai.com/v1 · or: anthropic",
                "key": "Enter with nothing for none (local endpoints)", "model": "a model id from the list"}[st["step"]]
        prompt = SETUP_PROMPTS[st["step"]] + " ❯ "
        text = self.editor.text.replace("\n", " ")
        if st["step"] == "key":
            text = "•" * len(text)
        body_h = max(1, H - 4)
        body = out[:body_h]
        body += [""] * (body_h - len(body))
        text = clip(text, max(1, W - str_width(prompt) - 1))
        inp = styled(prompt, BOLD) + text
        foot = DIM + clip("Enter next · Esc back · Ctrl-C quit · " + hint, W) + RESET
        rows = body + [DIM + "─" * W + RESET, inp, foot]
        return rows[:H], (body_h + 1, min(W - 1, str_width(prompt) + str_width(text)))

    def stop_spawned_server(self):
        """Leaving the first-run screen unconfigured: stop the background server this TUI started, so env vars
        exported before the next run are seen by a fresh server."""
        if not ensure_server.spawned:
            return
        port = common.find_server(self.path)
        info = common.health(port) if port else None
        if info and info.get("pid"):
            try:
                os.kill(int(info["pid"]), signal.SIGTERM)
            except OSError:
                pass

    def open_session(self, sid=None):
        if self.session:
            try:
                self.rpc.call("sessions.unsubscribe", {"session_id": self.session["id"]})
            except wsclient.RPCError:
                pass
        if sid is None:
            sid = self.rpc.call("sessions.create", {"cwd": self.cwd})["id"]
        r = self.rpc.call("sessions.subscribe", {"session_id": sid})
        self.session = r["session"]
        self.items = [("msg", m) for m in r.get("messages") or []]
        self.draft = r.get("draft")
        self.queue = r.get("queue") or []
        self.tools = dict(r.get("tools") or {})
        if r.get("now"):
            self.skew = r["now"] - int(time.time() * 1000)
        self.cache.clear()
        self.scroll = 0
        self.editor.clear()
        self.rebuild_history()
        self.dirty = True

    def rebuild_history(self):
        self.editor.history = [user_text(it[1]) for it in self.items
                               if it[0] == "msg" and is_prompt_msg(it[1])]

    def sid(self):
        return self.session and self.session["id"]

    # -- events
    def on_event(self, ev):
        name, d = ev.get("event"), ev.get("data") or {}
        sid = self.sid()
        self.dirty = True
        if name == "onboarding":
            self.hello["onboarding"] = d
            return
        if name == "settings.updated":
            if "tui_mouse" in d and (d["tui_mouse"] is not False) != self.mouse:
                self.set_mouse(d["tui_mouse"] is not False)
            return
        if name == "session.updated":
            if d.get("id") == sid:
                self.session = d
            return
        if name == "session.deleted":
            if d.get("session_id") == sid:
                self.items.append(("local", "This session was deleted by another client."))
            return
        if d.get("session_id") not in (None, sid):
            return
        if name == "message.added":
            self.items.append(("msg", d))
            if is_prompt_msg(d):
                t = user_text(d)
                if not self.editor.history or self.editor.history[-1] != t:
                    self.editor.history.append(t)
        elif name == "messages.truncated":
            fid = d.get("from_id")
            self.items = [it for it in self.items
                          if not (it[0] == "msg" and it[1].get("id") is not None and it[1]["id"] >= fid)]
            self.cache.clear()
            self.rebuild_history()
        elif name == "draft.start":
            self.draft = {"turn_id": d.get("turn_id"), "content": []}
        elif name == "draft.block":
            if self.draft is None:
                self.draft = {"turn_id": d.get("turn_id"), "content": []}
            c = self.draft["content"]
            idx = d.get("index", len(c))
            while len(c) <= idx:
                c.append({"type": "text", "text": ""})
            c[idx] = dict(d.get("block") or {})
        elif name == "draft.delta":
            if self.draft is None:
                return
            c = self.draft["content"]
            idx = d.get("index", 0)
            if idx >= len(c):
                return
            b = c[idx]
            for k in ("text", "thinking", "partial_json"):
                if d.get(k) is not None:
                    b[k] = b.get(k, "") + d[k]
            b["ended_at"] = self.server_now()
        elif name == "draft.end":
            self.draft = None
        elif name == "tool.start":
            self.tools[d.get("tool_use_id")] = {"started_at": d.get("started_at")}
        elif name == "tool.end":
            self.tools[d.get("tool_use_id")] = {"started_at": d.get("started_at"), "ended_at": d.get("ended_at")}
        elif name == "queue.updated":
            self.queue = d.get("queue") or []
        elif name == "btw.start":
            self.items.append(("btw", {"btw_id": d.get("btw_id"), "question": d.get("question"), "text": ""}))
        elif name in ("btw.delta", "btw.end"):
            for it in reversed(self.items):
                if it[0] == "btw" and it[1].get("btw_id") == d.get("btw_id"):
                    if d.get("reset"):  # the provider was retried: the answer starts over
                        it[1]["text"] = ""
                    if d.get("text"):
                        it[1]["text"] += d["text"]
                    if d.get("error"):
                        it[1]["error"] = d["error"]
                    if name == "btw.end":
                        it[1]["done"] = True
                    break
        elif name == "notice":
            self.items.append(("notice", d))

    def drain(self, timeout=0):
        for ev in self.rpc.poll_events(timeout):
            self.on_event(ev)
        while self.rpc.has_pending():
            for ev in self.rpc.poll_events(0):
                self.on_event(ev)
        if self.setup is not None and not self.needs_setup():
            self.finish_setup()

    # -- commands
    def submit(self, text, edit_id):
        self.scroll = 0
        s = text.strip()
        if s.startswith("/"):
            cmd, _, arg = s.partition(" ")
            arg = arg.strip()
            self.editor.history.append(s)
            if cmd in ("/quit", "/exit"):
                self.quit = True
                return
            if cmd == "/sessions":
                lst = self.rpc.call("sessions.list")
                self.items.append(("local", "Sessions (/resume ID to switch):\n" +
                                   "\n".join(format_sessions(lst))))
                return
            if cmd == "/resume":
                if not arg:
                    try:
                        lst = self.rpc.call("sessions.list")
                    except wsclient.RPCError as e:
                        self.flash = "listing sessions failed: %s" % e
                        return
                    self.picker = SessionPicker(lst)
                    return
                try:
                    self.open_session(arg)
                except wsclient.RPCError as e:
                    self.flash = "resume failed: %s" % e
                return
            if cmd == "/new":
                self.open_session(None)
                return
            if cmd == "/mouse" and arg in ("", "on", "off"):
                self.set_mouse(not self.mouse if not arg else arg == "on")
                self.flash = ("mouse on: wheel scrolls (Shift+drag selects text)" if self.mouse else
                              "mouse off: drag selects text; PgUp/PgDn scroll") + \
                             " · for this TUI only; setting tui_mouse makes it permanent"
                return
            if cmd == "/model" and not arg:
                try:
                    r = self.rpc.call("models.list", {"session_id": self.sid()})
                except wsclient.RPCError as e:
                    self.flash = "listing models failed: %s" % e
                    return
                if r.get("error"):
                    self.flash = "could not list models (%s); use /model <id>" % r["error"]
                    return
                self.picker = ModelPicker(r.get("models") or [], (self.session or {}).get("model"), r.get("provider"))
                return
            if cmd == "/edit":
                for it in reversed(self.items):
                    if it[0] == "msg" and is_prompt_msg(it[1]):
                        self.editor.load_edit(user_text(it[1]), it[1]["id"])
                        self.flash = "editing message #%s — Enter resends, Ctrl-C cancels" % it[1]["id"]
                        return
                self.flash = "no previous user message to edit"
                return
        if not self.provider_ready() and not re.match(r"\s*/(?!btw\b|retitle\b|compact\b)\w", text):
            self.editor.set_text(text) if edit_id is None else self.editor.load_edit(text, edit_id)
            self.flash = ("read only: no LLM provider is configured. Open file://%s in a browser to set one up, "
                          "or set $AI_ENDPOINT/$AI_MODEL and restart the server" % self.path)
            return
        try:
            if edit_id is not None:
                self.rpc.call("chat.edit", {"session_id": self.sid(), "message_id": edit_id, "text": text})
            else:
                r = self.rpc.call("chat.send", {"session_id": self.sid(), "text": text})
                if r and r.get("queued"):
                    self.flash = "queued (turn in progress)"
        except wsclient.RPCError as e:
            self.flash = "error: %s" % e

    def picker_key(self, key):
        act = self.picker.handle(key)
        if act is None:
            return
        self.picker = None
        if act[0] == "model":
            try:
                s = self.rpc.call("sessions.set_model", {"session_id": self.sid(), "model": act[1] or None})
                self.session = dict(self.session or {}, **s)
                self.flash = "model: %s" % s.get("model")
            except wsclient.RPCError as e:
                self.flash = "setting the model failed: %s" % e
        elif act[0] == "resume":
            try:
                self.open_session(act[1])
            except wsclient.RPCError as e:
                self.flash = "resume failed: %s" % e

    def handle_key(self, key):
        if isinstance(key, tuple) and key[0] == "mouse":
            self.mouse_key(key)
            return
        if key == "C-z":
            self.suspend()
            return
        if self.setup is not None:
            self.setup_key(key)
            return
        if self.picker is not None:
            self.picker_key(key)
            self.dirty = True
        elif key in ("tab", "S-tab"):
            self.cycle_effort(1 if key == "tab" else -1)
        else:
            self.action(self.editor.handle(key))

    def cycle_effort(self, d=1):
        """Tab / Shift-Tab: the session's next/previous effort level (default, then the model's levels)."""
        s = self.session or {}
        if not s.get("id"):
            return
        levels = [None] + list(s.get("efforts") or [])
        cur = s.get("effort_override")
        if cur not in levels:  # a level this model doesn't take: continue from the one it gets
            cur = s.get("effort")
        i = levels.index(cur) if cur in levels else 0
        nxt = levels[(i + d) % len(levels)]
        try:
            s = self.rpc.call("sessions.set_model", {"session_id": s["id"], "effort": nxt})
            self.session = dict(self.session or {}, **s)
            self.flash = "effort: %s · Tab/Shift-Tab cycles · /effort" % effort_label(self.session)
        except wsclient.RPCError as e:
            self.flash = "setting the effort failed: %s" % e
        self.dirty = True

    def server_now(self):
        return int(time.time() * 1000) + self.skew

    def timers_changed(self):
        """Whether a live timer shown (the turn, the thinking streaming now, running tools) reached a new
        TICK_MS step since the last call."""
        now = self.server_now()
        draft = [b for b in (self.draft or {}).get("content") or [] if b]
        starts = [turn_start(self.items)] + [b.get("started_at") for b in draft[-1:] if b.get("type") == "thinking"]
        starts += [t.get("started_at") for t in self.tools.values() if not t.get("ended_at")]
        sig = tuple(live_dur(x, now) for x in starts if x)
        changed = sig != getattr(self, "_timer_sig", None)
        self._timer_sig = sig
        return changed

    def mouse_key(self, key):
        """Wheel up/down scrolls the transcript (or moves in a picker); clicks etc. are ignored."""
        _, btn, _x, _y, pressed = key
        if not pressed or btn & 32:  # releases, drags
            return
        mods = btn & (4 | 8 | 16)  # shift, alt, ctrl
        d = {64: 1, 65: -1}.get(btn & ~(4 | 8 | 16))
        if d is None:  # clicks, horizontal wheel
            return
        if self.picker is not None:
            self.picker.handle("up" if d > 0 else "down")
        else:
            step = max(1, self.tlines_h - 2) if mods else WHEEL_LINES
            self.scroll = max(0, self.scroll + d * step)
        self.dirty = True

    def action(self, act):
        if act is None:
            return
        k = act[0]
        if k == "submit":
            self.flash = ""
            self.submit(act[1], act[2])
        elif k == "exit":
            self.quit = True
        elif k == "stop":
            if self.session and self.session.get("status") == "running" or self.draft or self.queue:
                self.rpc.call("chat.stop", {"session_id": self.sid()})
                self.flash = "stopped"
        elif k == "scroll":
            page = max(1, self.tlines_h - 2)
            self.scroll = max(0, self.scroll + act[1] * page)
        self.dirty = True

    # -- drawing
    def frame(self):
        """Builds the full screen as a list of rows plus cursor position."""
        if self.setup is not None:
            return self.setup_frame()
        W, H = self.cols, self.rows
        s = self.session or {}
        status = s.get("status", "")
        prov = ("%s:%s" % (s["provider"], s["model"]) if s.get("provider") and s.get("model")
                else provider_label((self.hello or {}).get("provider")))
        head = " %s · %s · %s · effort %s · %s " % (s.get("title") or "(untitled)", s.get("id", ""),
                                                    prov or s.get("model", ""), effort_label(s), status)
        header = REVERSE + clip(head, W) + " " * max(0, W - str_width(clip(head, W))) + RESET
        ed_rows, cr, cc = editor_view(self.editor, W, max(1, H // 3))
        sep_info = ""
        if self.queue:
            sep_info = " %d queued: %s " % (len(self.queue), clip(self.queue[0].replace("\n", " "), 30))
        if self.editor.edit_id is not None:
            sep_info += " editing #%s " % self.editor.edit_id
        sep = DIM + "─" * 2 + RESET + YELLOW + sep_info + RESET + DIM + "─" * max(0, W - 2 - str_width(sep_info)) + RESET
        if self.flash:
            foot = self.flash
        elif not self.provider_ready():
            foot = "read only: no LLM provider configured (open the page in a browser to set one up) · /help"
        elif status == "running":
            started = turn_start(self.items)
            foot = "running%s Esc stop · Enter queues · PgUp/PgDn scroll" % (
                " " + live_dur(started, self.server_now()) + " ·" if started else "…")
        else:
            foot = ("Enter send · C-j newline · ↑/↓ history · Tab effort · Esc stop · PgUp/PgDn · "
                    "/edit /sessions /new /mouse /quit")
        cost = " · ".join(x for x in (context_label(s), usage_label(s)) if x)
        if cost and W - str_width(cost) - 2 >= 10:   # session cost in the bottom-right corner
            left = clip(foot, W - str_width(cost) - 2)
            footer = DIM + left + " " * (W - str_width(left) - str_width(cost)) + cost + RESET
        else:
            footer = DIM + clip(foot, W) + RESET
        th = max(1, H - 3 - len(ed_rows))
        self.tlines_h = th
        if self.picker is not None:
            view = self.picker.render(W, th)
            rows = [header] + view + [sep] + ed_rows + [footer]
            return rows[:H], (1, min(W - 1, str_width(self.picker.prompt + self.picker.query)))
        live = {"now": self.server_now(), "tools": self.tools} if status == "running" else None
        lines = render_transcript(self.items, self.draft, W, self.cache, live, status == "running")
        if self.scroll and self.anchor and self.anchor[0] == W:
            # scrolled up: output added below must not move the view (it's counted as "more lines")
            self.scroll = max(0, self.scroll + len(lines) - self.anchor[1])
        self.anchor = (W, len(lines))
        maxscroll = max(0, len(lines) - th)
        self.scroll = min(self.scroll, maxscroll)
        end = len(lines) - self.scroll
        view = lines[max(0, end - th):end]
        if self.scroll:
            view = view[:-1] + [styled(clip("  ↓ %d more lines · scroll down / PgDn to return " % self.scroll, W), REVERSE)] if view else view
        view = [""] * (th - len(view)) + view
        rows = [header] + view + [sep] + ed_rows + [footer]
        return rows[:H], (1 + th + 1 + cr, cc)

    def draw(self):
        rows, (cr, cc) = self.frame()
        out = ["\x1b[?2026h\x1b[?25l\x1b[H"]
        for i, r in enumerate(rows):
            out.append(r + RESET + "\x1b[K")
            if i < len(rows) - 1:
                out.append("\r\n")
        out.append("\x1b[J\x1b[%d;%dH\x1b[?25h\x1b[?2026l" % (cr + 1, cc + 1))
        data = "".join(out).encode("utf-8", "replace")
        while data:
            n = os.write(self.out_fd, data)
            data = data[n:]
        self.dirty = False

    def resize(self):
        try:
            sz = os.get_terminal_size(self.out_fd)
            self.cols, self.rows = max(10, sz.columns), max(5, sz.lines)
        except OSError:
            pass
        self.cache.clear()
        self.dirty = True

    # -- main loop
    def loop(self):
        in_fd = self.in_fd
        while not self.quit:
            if self.dirty:
                self.draw()
            timeout = None
            if self.rpc.has_pending():
                timeout = 0
            elif self.parser.pending_escape():
                timeout = ESC_TIMEOUT
            elif (self.session or {}).get("status") == "running":
                timeout = 1.0   # live timers: redrawn when one of them reaches its next TICK_MS step
            r, _, _ = select.select([in_fd, self.rpc.fileno(), self.wake_r], [], [], timeout)
            if (self.session or {}).get("status") == "running":
                tick = self.server_now() // 1000
                if tick != self.tick:
                    self.tick = tick
                    self.dirty = self.dirty or self.timers_changed()
            if self.wake_r in r:
                os.read(self.wake_r, 1024)
                if self.winch:
                    self.winch = False
                    self.resize()
            if in_fd in r:
                data = os.read(in_fd, 65536)
                if not data:
                    return
                for key in self.parser.feed(data):
                    self.handle_key(key)
                    if self.quit:
                        return
                self.dirty = True
            elif not r and self.parser.pending_escape():
                for key in self.parser.flush():
                    self.handle_key(key)
            if self.rpc.fileno() in r or self.rpc.has_pending():
                try:
                    self.drain(0)
                except wsclient.AuthError:
                    self.reconnect("Access token changed; re-reading it from the file and reconnecting…")
                except ConnectionError:
                    self.reconnect()

    def reconnect(self, why="Disconnected from server; reconnecting…"):
        sid = self.sid()
        self.items.append(("local", why))
        self.draw()
        last = None
        for _ in range(20):
            try:
                self.connect()
                if self.setup is not None:   # still on the first-run screen: no session to reopen
                    if not self.needs_setup():
                        self.finish_setup()
                    return
                self.session = None
                self.open_session(sid)
                self.items.append(("local", "Reconnected."))
                return
            except (OSError, ConnectionError, SystemExit, wsclient.RPCError) as e:
                last = e
                time.sleep(0.5)
        raise SystemExit("lost connection to server" + (": %s" % last if last else ""))

    def set_mouse(self, on):
        self.mouse = bool(on)
        if self.term_active:
            os.write(self.out_fd, MOUSE_ON if self.mouse else MOUSE_OFF)

    def enter_terminal(self):
        """Raw mode, alternate screen, bracketed paste and (if wanted) mouse reporting."""
        import termios
        new = termios.tcgetattr(self.in_fd)
        new[0] &= ~(termios.BRKINT | termios.ICRNL | termios.INPCK | termios.ISTRIP | termios.IXON | termios.INLCR)
        new[3] &= ~(termios.ECHO | termios.ICANON | termios.IEXTEN | termios.ISIG)
        new[6][termios.VMIN] = 1
        new[6][termios.VTIME] = 0
        termios.tcsetattr(self.in_fd, termios.TCSAFLUSH, new)
        os.write(self.out_fd, b"\x1b[?1049h\x1b[?2004h" + (MOUSE_ON if self.mouse else b"") + b"\x1b[H\x1b[2J")
        self.term_active = True

    def leave_terminal(self, saved):
        """Undoes enter_terminal (mouse reporting always switched off, whatever its state)."""
        import termios
        self.term_active = False
        os.write(self.out_fd, MOUSE_OFF + b"\x1b[?2004l\x1b[?2026l\x1b[0m\x1b[?25h\x1b[?1049l")
        termios.tcsetattr(self.in_fd, termios.TCSAFLUSH, saved)

    def suspend(self):
        """Ctrl-Z (ISIG is off, so it arrives as a key) or SIGTSTP: give the terminal back, stop the
        job like the shell expects, and take the terminal over again on resume (SIGCONT)."""
        if not self.term_active:
            return
        self.leave_terminal(self.saved_tty)
        prev = signal.signal(signal.SIGTSTP, signal.SIG_DFL)
        try:
            os.killpg(os.getpgrp(), signal.SIGTSTP)  # returns after fg/bg sends SIGCONT
        finally:
            signal.signal(signal.SIGTSTP, prev)
            self.enter_terminal()
            self.parser = KeyParser()
            self.resize()

    def run(self):
        import termios
        self.in_fd = sys.stdin.fileno()
        self.out_fd = sys.stdout.fileno()
        old = termios.tcgetattr(self.in_fd)
        self.saved_tty = old
        self.wake_r, self.wake_w = os.pipe()
        os.set_blocking(self.wake_w, False)
        self.winch = False

        def on_winch(*_):
            self.winch = True
            try:
                os.write(self.wake_w, b"w")
            except OSError:
                pass

        def on_term(*_):
            raise SystemExit(0)

        def on_tstp(*_):  # `kill -TSTP`: leave the terminal usable while stopped
            self.suspend()
            on_winch()  # wake the loop to redraw

        prev = {}
        for sig, h in ((signal.SIGWINCH, on_winch), (signal.SIGTERM, on_term), (signal.SIGHUP, on_term),
                       (signal.SIGTSTP, on_tstp)):
            prev[sig] = signal.signal(sig, h)
        try:
            self.enter_terminal()
            self.resize()
            self.loop()
        finally:
            self.leave_terminal(old)
            for sig, h in prev.items():
                signal.signal(sig, h)
            os.close(self.wake_r)
            os.close(self.wake_w)


def run(html_path, args):
    path = common.html_path(html_path)
    opts = parse_args(list(args or []))
    app = App(path, opts)
    app.connect()
    if opts["list"]:
        for line in format_sessions(app.rpc.call("sessions.list")):
            print(line)
        app.rpc.close()
        return 0
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise SystemExit("tui: stdin/stdout must be a terminal")
    if app.needs_setup():
        app.start_setup()
    else:
        try:
            app.open_session(opts["resume"])
        except wsclient.RPCError as e:
            raise SystemExit("tui: %s" % e)
    app.drain(0)
    try:
        app.run()
    finally:
        try:
            app.rpc.close()
        except Exception:
            pass
        if app.setup is not None:
            app.stop_spawned_server()
    if app.session:
        print("session %s — resume with: tui --resume %s" % (app.session["id"], app.session["id"]))
    return 0
