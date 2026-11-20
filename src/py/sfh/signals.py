"""What a session is actually about, for the title + identicon subjects prompt.

A session's starting working directory is a weak signal: sessions are often all started in the same
root folder and then work on unrelated projects in its subfolders. extract() looks at what the session
touched instead: paths given to file tools, `cd`/`-C` targets and paths in bash commands, paths and
subfolder names mentioned in the user's messages, and per-message working directories. It returns
{"projects": [...], "paths": [...]} (absolute, most used first, capped): projects are the nearest
folders with a project marker (.git, package.json, ...) below the session's cwd, or else the first
folder under it. Pure apart from cheap, cached filesystem checks (exists / isdir / listdir)."""
import collections
import os
import re
import tempfile

MAX_PROJECTS = 8
MAX_PATHS = 12
MAX_NAMES = 2000      # subfolder names (two levels under the cwd) matched against the user's words
USER_WEIGHT = 3       # a path the user named counts like three tool calls on it
MARKERS = (".git", "package.json", "pyproject.toml", "setup.py", "go.mod", "Cargo.toml", "pom.xml",
           "build.gradle", "Gemfile", "composer.json", "deno.json", "Makefile")
GENERIC = set("""src lib libs test tests spec docs doc bin build dist out tmp temp vendor node_modules
scripts script assets static public config configs tools util utils data examples example
include pkg cmd internal app web""".split())
SKIP = ("/dev/", "/proc/", "/sys/")
TMP = tempfile.gettempdir()
DEPS_RE = re.compile(r"/(?:node_modules|site-packages|vendor|\.git|\.venv|venv|__pycache__)(?:/|$)")
URL_RE = re.compile(r"[A-Za-z][\w+.-]*://\S+")
PATH_RE = re.compile(r"(?<![\w:/.~$-])(~?/[\w.@+-]+(?:/[\w.@+-]+)*/?|\.{1,2}/[\w.@+-]+(?:/[\w.@+-]+)*/?|"
                     r"[\w@+-][\w.@+-]*(?:/[\w.@+-]+)+/?)")
CD_RE = re.compile(r"(?:^|[;&|(]|\s)(?:cd|pushd)\s+(['\"]?)([^\s;&|'\"()`$]+)\1")
DASH_C_RE = re.compile(r"\s-C\s+(['\"]?)([^\s;&|'\"()`$]+)\1")
WORD_RE = re.compile(r"[\w.@+-]+")


class _FS:
    """Cached filesystem checks (one extract() call)."""

    def __init__(self):
        self.cache = {}

    def _get(self, kind, p, fn):
        k = (kind, p)
        if k not in self.cache:
            try:
                self.cache[k] = fn(p)
            except OSError:
                self.cache[k] = None
        return self.cache[k]

    def exists(self, p):
        return bool(self._get("e", p, os.path.exists))

    def isdir(self, p):
        return bool(self._get("d", p, os.path.isdir))

    def listdirs(self, p):
        names = self._get("l", p, os.listdir) or []
        return [n for n in sorted(names) if not n.startswith(".") and self.isdir(os.path.join(p, n))]


def under(p, root):
    """True when p is root or inside it."""
    return bool(root) and (p == root or p.startswith(root.rstrip("/") + "/"))


def short(p, cwd):
    """p for display: relative to cwd when inside it, else with ~ for the home directory."""
    if cwd and under(p, cwd) and p != cwd:
        return os.path.relpath(p, cwd)
    home = os.path.expanduser("~")
    if home not in ("", "/") and under(p, home):
        return "~" + p[len(home.rstrip("/")):]
    return p


def resolve(p, base):
    p = os.path.expanduser(p.strip().strip("'\"").rstrip(".,:;"))
    if not p or "$" in p or "*" in p:
        return None
    if not os.path.isabs(p):
        if not base:
            return None
        p = os.path.join(base, p)
    p = os.path.normpath(p)
    if p == "/" or p.startswith(SKIP):
        return None
    return p


def is_project(d, fs):
    return any(fs.exists(os.path.join(d, m)) for m in MARKERS)


def project_of(p, root, fs):
    """The project/folder p belongs to: the nearest folder with a project marker below the session's root
    folder (or anywhere, outside it), else the root itself when it is a project, else the first folder
    under the root that isn't a generic one (src, docs, ...). None for scratch / home / root-level files."""
    home = os.path.expanduser("~")
    inside = under(p, root)
    m = DEPS_RE.search(p)  # a dependency belongs to the project that uses it
    d = start = p[:m.start()] if m else p if fs.isdir(p) else os.path.dirname(p)
    for _ in range(8):
        if not d or d in ("/", home) or (inside and d == root) or (root and under(root, d)):
            break
        if is_project(d, fs) and not os.path.basename(d).startswith("."):  # not .opencode/, .github/
            return d
        d = os.path.dirname(d)
    if inside:
        if is_project(root, fs):
            return root
        rel = os.path.relpath(p, root)
        first = rel.split(os.sep)[0]
        if rel == "." or first.startswith(".") or first.lower() in GENERIC:
            return None
        return os.path.join(root, first) if (os.sep in rel or fs.isdir(p)) else None
    if start in ("/", home) or under(start, TMP) or under(start, "/tmp"):
        return None
    return start


def text_paths(text):
    return [m.group(1) for m in PATH_RE.finditer(URL_RE.sub(" ", text or ""))]


def extract(msgs, cwd, is_skipped=lambda b: False):
    """{"projects": [...], "paths": [...]} for a session's messages (dicts with role, content, cwd)."""
    fs = _FS()
    root = os.path.normpath(cwd) if cwd else None
    found = []  # (absolute path, weight)
    seen = set()  # paths of the current block (each counts once per block)

    def add(raw, base, weight=1, strict=False):
        p = resolve(raw, base)
        # loose candidates (words in prose and commands, relative paths) count only when they or their
        # folder exist (a file about to be written); "/models" in prose is no path
        strict = strict or not os.path.isabs(os.path.expanduser(raw.strip().strip("'\"")))
        if p and p not in seen and (not strict or fs.exists(p) or (
                os.path.dirname(p) != "/" and fs.isdir(os.path.dirname(p)))):
            seen.add(p)
            found.append((p, weight))

    user_text = []
    for m in msgs:
        base = os.path.normpath(m.get("cwd") or root or "") or None
        if base and base != root:
            add(base, None)  # the session's cwd was changed: where it moved to counts
        for b in m.get("content") or []:
            seen.clear()
            if is_skipped(b):
                continue
            if b.get("type") == "text" and m.get("role") == "user":
                user_text.append(b.get("text") or "")
                for raw in text_paths(b.get("text")):
                    add(raw, base, USER_WEIGHT, strict=True)
            elif b.get("type") == "tool_use":
                inp = b.get("input") if isinstance(b.get("input"), dict) else {}
                for k in ("path", "file_path", "cwd"):
                    if isinstance(inp.get(k), str) and inp[k].strip():
                        add(inp[k], base)
                cmd = inp.get("command")
                if isinstance(cmd, str):
                    cds = [mm.group(2) for mm in CD_RE.finditer(cmd)] + [mm.group(2) for mm in DASH_C_RE.finditer(cmd)]
                    for raw in cds:
                        add(raw, base)
                    cbase = resolve(cds[0], base) if cds else base
                    for raw in text_paths(cmd):
                        add(raw, cbase or base, strict=True)
    # subfolders of the cwd (two levels) named in the user's words, e.g. "fix the build of foo-app"
    if root and fs.isdir(root) and user_text:
        words = collections.Counter(w.lower() for w in WORD_RE.findall("\n".join(user_text)))
        names = []
        for n1 in fs.listdirs(root)[:MAX_NAMES]:
            names.append(n1)
            if len(names) < MAX_NAMES:
                names += [os.path.join(n1, n2) for n2 in fs.listdirs(os.path.join(root, n1))[:MAX_NAMES - len(names)]]
        for rel in names:
            leaf = os.path.basename(rel)
            if len(leaf) >= 3 and leaf.lower() not in GENERIC and words.get(leaf.lower()):
                found.append((os.path.join(root, rel), USER_WEIGHT * words[leaf.lower()]))

    projects, paths = collections.Counter(), collections.Counter()
    order = {}
    for i, (p, w) in enumerate(found):
        order.setdefault(p, i)
        if p != root:
            paths[p] += w
        proj = project_of(p, root, fs)
        if proj:
            order.setdefault(proj, i)
            projects[proj] += w

    # a folder holding more specific projects that got at least as much use isn't one itself here (e.g. a
    # root folder that is a repo of repos)
    for p in list(projects):
        if projects[p] <= sum(w for q, w in projects.items() if q != p and under(q, p)):
            del projects[p]

    def top(c, n):
        return sorted(c, key=lambda p: (-c[p], order[p]))[:n]
    return {"projects": top(projects, MAX_PROJECTS), "paths": top(paths, MAX_PATHS)}


def describe(sig, cwd, n_paths=MAX_PATHS):
    """Compact one-line-per-kind text of extract()'s result, paths shown relative to cwd."""
    sig = sig or {}
    lines = []
    if sig.get("projects"):
        lines.append("Projects/folders: " + ", ".join(short(p, cwd) for p in sig["projects"]))
    if sig.get("paths") and n_paths:
        lines.append("Paths touched or named: " + ", ".join(short(p, cwd) for p in sig["paths"][:n_paths]))
    return "\n".join(lines)
