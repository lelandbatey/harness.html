#!/usr/bin/env python3
"""AST bundler: turns python packages into ONE python source file that is safe to inline in an
HTML <script> element.

Each module is parsed, its relative imports are made absolute, docstrings are dropped, string
literals containing HTML-dangerous sequences are split, and the result is ast.unparse'd. The
bundle embeds every module's rewritten source in a dict served by a sys.meta_path finder, plus the
ORIGINAL sources (zlib+base64) so `extract-program` can write readable files back out.

    python3 tools/bundle.py -o out.py --entry sfh.main [--prune tremolo] vendor/tremolo src/py/sfh
"""
import argparse
import ast
import base64
import json
import os
import re
import sys
import zlib

DANGER = re.compile(r"</?script|<!--|-->|</style", re.I)
FORBIDDEN_LINES = ("#--SFH-PROGRAM-BEGIN--", "#--SFH-PROGRAM-END--")


class BundleError(Exception):
    pass


def collect(pkg_dir):
    """Returns {modname: (relpath, is_pkg, source)} for the package rooted at pkg_dir."""
    pkg_dir = os.path.abspath(pkg_dir)
    base = os.path.dirname(pkg_dir)
    out = {}
    for dirpath, dirnames, filenames in os.walk(pkg_dir):
        dirnames[:] = sorted(d for d in dirnames if d != "__pycache__" and not d.startswith("."))
        rel_dir = os.path.relpath(dirpath, base)
        pkg = rel_dir.replace(os.sep, ".")
        if "__init__.py" not in filenames:
            out[pkg] = (os.path.join(rel_dir, "__init__.py"), True, "")
        for fn in sorted(filenames):
            if not fn.endswith(".py"):
                continue
            with open(os.path.join(dirpath, fn), encoding="utf-8") as f:
                src = f.read()
            rel = os.path.join(rel_dir, fn)
            if fn == "__init__.py":
                out[pkg] = (rel, True, src)
            else:
                out[pkg + "." + fn[:-3]] = (rel, False, src)
    return out


def _resolve(modname, is_pkg, node):
    if not node.level:
        return node.module
    parts = modname.split(".")
    if not is_pkg:
        parts = parts[:-1]
    if node.level > 1:
        parts = parts[:len(parts) - (node.level - 1)]
    return ".".join(parts + ([node.module] if node.module else []))


def _split_str(s, empty):
    """Splits s so that no piece contains a dangerous sequence."""
    pieces, last = [], 0
    for m in DANGER.finditer(s):
        cut = m.start() + (2 if m.group(0) == "-->" else 1)
        pieces.append(s[last:cut])
        last = cut
    pieces.append(s[last:])
    return [p for p in pieces if p != empty] or [empty]


class Rewriter(ast.NodeTransformer):
    def __init__(self, modname, is_pkg):
        self.modname, self.is_pkg = modname, is_pkg

    def _strip_doc(self, node):
        b = node.body
        if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
                and isinstance(b[0].value.value, str):
            node.body = b[1:] or [ast.Pass()]
        return node

    def visit_Module(self, node):
        return self.generic_visit(self._strip_doc(node))

    def visit_FunctionDef(self, node):
        return self.generic_visit(self._strip_doc(node))

    visit_AsyncFunctionDef = visit_ClassDef = visit_FunctionDef

    def visit_ImportFrom(self, node):
        if node.level:
            node.module, node.level = _resolve(self.modname, self.is_pkg, node), 0
        return node

    def visit_JoinedStr(self, node):
        values = []
        for v in node.values:
            if isinstance(v, ast.Constant) and isinstance(v.value, str) and DANGER.search(v.value):
                for i, p in enumerate(_split_str(v.value, "")):
                    if i:  # route the boundary char through an expression so unparse can't merge
                        values.append(ast.FormattedValue(ast.Constant(p[0]), -1, None))
                        p = p[1:]
                    if p:
                        values.append(ast.Constant(p))
            else:
                values.append(self.visit(v))
        node.values = values
        return node

    def visit_Constant(self, node):
        v = node.value
        if isinstance(v, (str, bytes)):
            text = v if isinstance(v, str) else v.decode("latin-1")
            if DANGER.search(text):
                parts = _split_str(text, "")
                if isinstance(v, bytes):
                    parts = [p.encode("latin-1") for p in parts]
                expr = ast.Constant(parts[0])
                for p in parts[1:]:
                    expr = ast.BinOp(expr, ast.Add(), ast.Constant(p))
                return expr
        return node


def rewrite(modname, is_pkg, src, filename):
    tree = ast.parse(src, filename)
    tree = Rewriter(modname, is_pkg).visit(tree)
    ast.fix_missing_locations(tree)
    return ast.unparse(tree)


def imports_of(modname, is_pkg, src, known):
    """Every module referenced by any import anywhere in the module (incl. inside functions)."""
    found = set()
    for node in ast.walk(ast.parse(src)):
        names = []
        if isinstance(node, ast.Import):
            names = [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            base = _resolve(modname, is_pkg, node)
            names = [base] + [base + "." + a.name for a in node.names]
        for n in names:
            parts = n.split(".")
            for i in range(1, len(parts) + 1):
                if ".".join(parts[:i]) in known:
                    found.add(".".join(parts[:i]))
    return found


def reachable(mods, entry):
    seen, todo = set(), [entry]
    while todo:
        m = todo.pop()
        if m in seen or m not in mods:
            continue
        seen.add(m)
        rel, is_pkg, src = mods[m]
        todo.extend(imports_of(m, is_pkg, src, mods))
        if "." in m:
            todo.append(m.rsplit(".", 1)[0])
    return seen


def check_safe(text, what):
    for i, line in enumerate(text.split("\n"), 1):
        m = DANGER.search(line)
        if m:
            raise BundleError("%s:%d: contains %r (not inline-safe in <script>): %s" % (what, i, m.group(0), line[:200]))
        if line.strip() in FORBIDDEN_LINES:
            raise BundleError("%s:%d: contains a program marker line" % (what, i))


LOADER = r'''
import sys, base64, zlib, json
from importlib.machinery import ModuleSpec


class _SFHLoader:
    def __init__(self, sources, originals):
        self.sources, self.originals = sources, originals

    def find_spec(self, name, path=None, target=None):
        if name in self.sources:
            rel, is_pkg, _ = self.sources[name]
            spec = ModuleSpec(name, self, origin="<sfh>/" + rel, is_package=is_pkg)
            spec.has_location = False
            return spec
        return None

    def create_module(self, spec):
        return None

    def exec_module(self, module):
        rel, is_pkg, src = self.sources[module.__name__]
        module.__file__ = "<sfh>/" + rel
        if is_pkg:
            module.__path__ = []
        exec(compile(src, "<sfh>/" + rel, "exec"), module.__dict__)

    def original_sources(self):
        """{relpath: original source text} of every bundled file (for extract-program)."""
        return json.loads(zlib.decompress(base64.b64decode(self.originals)).decode("utf-8"))


_sfh_loader = _SFHLoader(_SFH_SOURCES, _SFH_ORIGINALS)
sys.meta_path.insert(0, _sfh_loader)
sys.modules["_sfh_bundle"] = sys.modules[__name__]
'''


def bundle(pkg_dirs, entry, prune=(), main_call=None):
    mods = {}
    for d in pkg_dirs:
        mods.update(collect(d))
    if entry not in mods:
        raise BundleError("entry module %s not found" % entry)
    keep = reachable(mods, entry)
    names = sorted(m for m in mods if m in keep or m.split(".")[0] not in prune)
    sources, originals = {}, {}
    for m in names:
        rel, is_pkg, src = mods[m]
        out = rewrite(m, is_pkg, src, rel)
        check_safe(out, rel)
        sources[m] = (rel.replace(os.sep, "/"), is_pkg, out)
        originals[rel.replace(os.sep, "/")] = src
    blob = base64.b64encode(zlib.compress(json.dumps(originals).encode("utf-8"), 9)).decode("ascii")
    blob = "\n".join(blob[i:i + 100] for i in range(0, len(blob), 100))
    lines = ["# Bundled program: %d modules. Run via the html file; see `sh FILE help`." % len(names),
             "_SFH_SOURCES = {"]
    for m in names:
        lines.append("    %r: %r," % (m, sources[m]))
    lines.append("}")
    lines.append('_SFH_ORIGINALS = """\n%s\n"""' % blob)
    lines.append(LOADER.strip("\n"))
    lines.append("if __name__ == '__main__':")
    lines.append("    " + (main_call or "import %s as _m; _m.main(sys.argv)" % entry))
    text = "\n".join(lines) + "\n"
    check_safe(text, "bundle")
    compile(text, "<bundle>", "exec")
    return text, names


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pkg_dirs", nargs="+")
    ap.add_argument("-o", "--output", required=True)
    ap.add_argument("--entry", default="sfh.main")
    ap.add_argument("--prune", action="append", default=[], help="top-level package whose unreachable modules are dropped")
    a = ap.parse_args(argv)
    try:
        text, names = bundle(a.pkg_dirs, a.entry, a.prune)
    except BundleError as e:
        sys.exit("bundle: error: %s" % e)
    with open(a.output, "w", encoding="utf-8") as f:
        f.write(text)
    print("bundle: %d modules, %d bytes -> %s" % (len(names), len(text), a.output))


if __name__ == "__main__":
    main()
