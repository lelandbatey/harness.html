"""Local file links in message text, resolved when the message is stored.

The browser renders inline code that looks like a path (`src/a.py:12`) and markdown links to local
paths as clickable links that open the file in the in-page terminal. They are only clickable when the
file existed when the message was written, resolved against the working directory of that message.
find() collects the candidates the web renderer (src/web/markdown.js) can turn into links and
returns {path-as-written: {"path": absolute, "dir": bool}} for those that exist. Keys use the same
normalisation as markdown.js (`localFromHref`/`localFromCode` + `splitLine`): line suffixes
(`:12`, `:12:3`, `#L12`) are split off, `file://` is stripped and hrefs are percent-decoded."""
import os
import re
import urllib.parse

FENCE_RE = re.compile(r"^ {0,3}(`{3,}|~{3,})[^\n]*\n.*?(?:^ {0,3}\1[ \t]*$|\Z)", re.S | re.M)
CODE_RE = re.compile(r"(?<!`)(`+)(?!`)(.+?)(?<!`)\1(?!`)", re.S)
LINK_RE = re.compile(r"\]\(\s*(<[^>\n]*>|[^)\s]+)")
REFDEF_RE = re.compile(r"^ {0,3}\[[^\]\n]+\]:\s*(<[^>\n]*>|\S+)", re.M)
AUTOLINK_RE = re.compile(r"<(file://[^>\s]+)>", re.I)
LINE_RE = re.compile(r"^(.*?)(?::(\d+)(?::\d+)?|#L(\d+)(?:-L?\d+)?)$")
PATH_CHARS_RE = re.compile(r"^[\w.@+~\-/]+$")
SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.\-]*:")
MAX_CANDIDATES = 400


def split_line(p):
    m = LINE_RE.match(p)
    if m and m.group(1):
        return m.group(1)
    return p


def from_href(href):
    """Path part of a link href that points at a local file (None for URLs, anchors, ...)."""
    href = href.strip()
    if href.startswith("<") and href.endswith(">"):
        href = href[1:-1].strip()
    if not href:
        return None
    if href.lower().startswith("file://"):
        p = urllib.parse.unquote(re.sub(r"^file://[^/]*", "", href, flags=re.I))
    elif SCHEME_RE.match(href) or href.startswith(("#", "//", "?")):
        return None
    else:
        p = urllib.parse.unquote(href)
    return split_line(p) or None


def from_code(s):
    """Path part of an inline code span that could be a path. Deliberately loose: whether the target
    exists decides, so `Makefile` or `README` work too."""
    s = s.strip()
    if len(s) < 2 or len(s) > 400 or re.search(r"\s", s):
        return None
    if s.lower().startswith("file://"):
        return from_href(s)
    if "://" in s or s.startswith("//"):
        return None
    p = split_line(s)
    if not PATH_CHARS_RE.match(p) or p == "/" or re.match(r"^\.+$", p):
        return None
    return p


def candidates(text):
    """(path-as-written, kind) pairs; kind is "code" or "href"."""
    out = []
    seen = set()

    def add(p, kind):
        if p and (p, kind) not in seen and len(seen) < MAX_CANDIDATES:
            seen.add((p, kind))
            out.append((p, kind))
    for m in LINK_RE.finditer(text):
        add(from_href(m.group(1)), "href")
    for m in REFDEF_RE.finditer(text):
        add(from_href(m.group(1)), "href")
    for m in AUTOLINK_RE.finditer(text):
        add(from_href(m.group(1)), "href")
    body = FENCE_RE.sub("\n", text)
    for m in CODE_RE.finditer(body):
        add(from_code(m.group(2)), "code")
    return out


def resolve(path, cwd):
    p = os.path.expanduser(path) if path.startswith("~") else path
    return os.path.normpath(os.path.join(cwd or "/", p))


def find(texts, cwd):
    """{path-as-written: {"path", "dir"}} for the local paths mentioned in texts that exist now."""
    links = {}
    for text in texts:
        if not text:
            continue
        for p, kind in candidates(text):
            if p in links:
                continue
            ap = resolve(p, cwd)
            try:
                is_dir = os.path.isdir(ap)
                exists = is_dir or os.path.exists(ap)
            except (OSError, ValueError):
                continue
            if not exists:
                continue
            # a bare word in inline code (`src`, `test`) is only a link when it names a file
            if kind == "code" and "/" not in p and is_dir:
                continue
            links[p] = {"path": ap, "dir": is_dir}
    return links


def for_content(content, cwd):
    """links for a message's content blocks (text and thinking)."""
    texts = []
    for b in content or []:
        if b.get("type") == "text":
            texts.append(b.get("text") or "")
        elif b.get("type") == "thinking":
            texts.append(b.get("thinking") or "")
    return find(texts, cwd)
