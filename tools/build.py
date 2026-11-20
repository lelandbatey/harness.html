#!/usr/bin/env python3
"""Assembles dist/harness.html from src/template.html, the bundled python program and web assets.

    python3 tools/build.py [-o dist/harness.html] [--fresh [--force]]

When the output file already exists and holds data from a prior run, the new build keeps it: the db
(data block), the offline snapshot and the access token are carried over (the previous file is kept
as OUTPUT.prev). --fresh builds an empty file instead; it refuses to replace an existing output that holds
data (sessions, settings, a token) unless --force is given. A server running on the file keeps working:
its next save splices its in-memory db into the new file, keeping the new program.
"""
import argparse
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "src", "py"))

import json  # noqa: E402

import bundle  # noqa: E402
from sfh import about, common, htmlfile  # noqa: E402

CSS_FILES = ["vendor/xterm/xterm.css", "src/web/app.css"]
JS_FILES = ["vendor/xterm/xterm.js", "vendor/xterm/addon-fit.js", "src/web/markdown.js",
            "src/web/identicon.js", "src/web/app.js"]
BODY_FILE = "src/web/body.html"
# Files where "</script" may be escaped to "<\/script" automatically (minified vendor code, where
# such text only occurs inside string literals).
JS_AUTO_ESCAPE = {"vendor/xterm/xterm.js", "vendor/xterm/addon-fit.js"}
# Separator line written between concatenated assets; extract-program splits on it.
FILE_MARK = "/*--SFH-FILE: %s--*/"


class BuildError(Exception):
    pass


def read_opt(rel):
    p = os.path.join(ROOT, rel)
    if not os.path.exists(p):
        print("build: warning: %s is missing, skipped" % rel, file=sys.stderr)
        return None
    with open(p, encoding="utf-8") as f:
        return f.read()


def check(text, rel, pattern):
    for i, line in enumerate(text.split("\n"), 1):
        m = re.search(pattern, line, re.I)
        if m:
            raise BuildError("%s:%d: %r is not allowed inline (split it, e.g. '<' + '/script'): %s"
                             % (rel, i, m.group(0), line.strip()[:160]))


def assets(files, kind):
    out = []
    for rel in files:
        text = read_opt(rel)
        if text is None:
            continue
        if kind == "js":
            if rel in JS_AUTO_ESCAPE:
                text = re.sub(r"<(/script)", r"<\\\1", text, flags=re.I)
            check(text, rel, r"</script|<!--|<script")
        else:
            check(text, rel, r"</style")
        check(text, rel, r"SFH-DATA-|^#--SFH-PROGRAM-")
        out.append(FILE_MARK % rel + "\n" + text.rstrip("\n"))
    return "\n".join(out)


def about_js():
    """window.SFH_ABOUT from sfh/about.py (the single source of the "what is this" text), so the page can show
    it before a server runs. Placed before app.js; extract-program writes it out as about.gen.js."""
    text = "window.SFH_ABOUT = %s;" % json.dumps(about.ABOUT, ensure_ascii=False, indent=1)
    text = text.replace("</", "<\\/").replace("<!--", "<\\!--")
    rel = "src/web/about.gen.js"
    check(text, rel, r"</script|<!--|<script|SFH-DATA-|^#--SFH-PROGRAM-")
    return FILE_MARK % rel + "\n" + text


def has_data(src):
    """Whether an html file holds anything worth keeping: a db, an offline snapshot or an access token."""
    try:
        return bool(htmlfile.decode_data(src) or htmlfile.decode_offline(src) or htmlfile.decode_token(src))
    except Exception:
        return True


def carry_over(old_src, html):
    """html with the data block, offline snapshot and token of old_src (an earlier build with data).
    Raises BuildError when old_src has data that cannot be read (never silently drop it)."""
    try:
        data = htmlfile.decode_data(old_src)
        offline = htmlfile.decode_offline(old_src)
        token = htmlfile.decode_token(old_src)
    except Exception as e:
        raise BuildError("the existing output has data that could not be read (%s); refusing to overwrite it. "
                         "Move it away, or use --fresh to build an empty file" % e)
    if data:
        html = htmlfile.encode_data(html, data)
    if offline:
        html = htmlfile.encode_offline(html, offline)
    if token:
        html = htmlfile.encode_token(html, token)
    return html, bool(data)


def build(out_path, fresh=False, force=False):
    template = read_opt("src/template.html")
    if template is None:
        raise BuildError("src/template.html is required")
    program, mods = bundle.bundle([os.path.join(ROOT, "vendor/tremolo"), os.path.join(ROOT, "src/py/sfh")],
                                  "sfh.main", prune=["tremolo"])
    body = read_opt(BODY_FILE) or ""
    check(body, BODY_FILE, r"SFH-DATA-|^#--SFH-PROGRAM-|<!--SFH")
    js = assets(JS_FILES, "js")
    app_mark = FILE_MARK % "src/web/app.js"
    js = js.replace(app_mark, about_js() + "\n" + app_mark, 1) if app_mark in js else js + "\n" + about_js()
    parts = {"VERSION": common.VERSION, "CSS": assets(CSS_FILES, "css"), "JS": js,
             "BODY": body.rstrip("\n"), "PROGRAM": program.rstrip("\n")}
    html = re.sub(r"\{\{([A-Z]+)\}\}", lambda m: parts[m.group(1)], template)
    if htmlfile.program_source(html) != parts["PROGRAM"]:
        raise BuildError("program block does not round-trip (marker collision?)")
    if htmlfile.decode_data(html) != b"":
        raise BuildError("data block is not empty/locatable")
    kept = False
    if fresh and not force and os.path.exists(out_path) and has_data(htmlfile.read(out_path)):
        raise BuildError("%s holds data (sessions, settings or an access token) that an empty build would delete; "
                         "move it away first, or pass --force (make ... FORCE=1)" % os.path.relpath(out_path))
    if not fresh and os.path.exists(out_path):
        old = htmlfile.read(out_path)
        html, kept = carry_over(old, html)
        if kept:
            with open(out_path + ".prev", "w", encoding="utf-8") as f:
                f.write(old)
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(html)
    os.chmod(tmp, 0o755)
    os.replace(tmp, out_path)
    build.kept = kept
    return html, mods


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--output", default=os.path.join(ROOT, "dist", "harness.html"))
    ap.add_argument("--fresh", action="store_true", help="do not carry over data from an existing output file")
    ap.add_argument("--force", action="store_true", help="with --fresh: replace an output file that holds data")
    a = ap.parse_args(argv)
    try:
        html, mods = build(a.output, a.fresh, a.force)
    except (BuildError, bundle.BundleError) as e:
        sys.exit("build: error: %s" % e)
    print("build: %s (%d modules, %.0f KiB)%s" % (os.path.relpath(a.output), len(mods), len(html.encode()) / 1024,
                                                 "; kept the data of the previous build (old file: %s.prev)"
                                                 % os.path.relpath(a.output) if build.kept else ""))


if __name__ == "__main__":
    main()
