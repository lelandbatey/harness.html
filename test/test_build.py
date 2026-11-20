"""Builds the html into a temp dir and exercises the polyglot header and the file-level CLI."""
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
sys.path.insert(0, os.path.join(ROOT, "src", "py"))

import bundle  # noqa: E402
from sfh import htmlfile  # noqa: E402


class BuildTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sfh-test-")
        cls.built = os.path.join(cls.tmp, "built.html")
        r = subprocess.run([sys.executable, os.path.join(ROOT, "tools", "build.py"), "-o", cls.built],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        # each test gets its own copy (and so its own port set: no server is running for it)
        self.html = os.path.join(self.tmp, "h_%s.html" % self._testMethodName)
        shutil.copy(self.built, self.html)
        os.chmod(self.html, 0o755)

    def test_rebuild_keeps_data(self):
        import sqlite3 as sq
        db = sq.connect(":memory:")
        db.execute("CREATE TABLE t(x)")
        db.execute("INSERT INTO t VALUES (42)")
        db.commit()
        raw = db.serialize()
        src = htmlfile.encode_data(htmlfile.read(self.html), raw)
        src = htmlfile.encode_token(src, "tok-abc")
        src = htmlfile.encode_offline(src, b'{"sessions": []}')
        with open(self.html, "w") as f:
            f.write(src)
        build = [sys.executable, os.path.join(ROOT, "tools", "build.py"), "-o", self.html]
        r = subprocess.run(build, capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("kept the data", r.stdout)
        new = htmlfile.read(self.html)
        self.assertEqual(htmlfile.decode_data(new), raw)
        self.assertEqual(htmlfile.decode_token(new), "tok-abc")
        self.assertEqual(htmlfile.decode_offline(new), b'{"sessions": []}')
        self.assertEqual(htmlfile.read(self.html + ".prev"), src)
        # --fresh refuses to delete data unless forced, then builds an empty file
        r = subprocess.run(build + ["--fresh"], capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("holds data", r.stderr)
        self.assertEqual(htmlfile.decode_data(htmlfile.read(self.html)), raw)
        r = subprocess.run(build + ["--fresh", "--force"], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(htmlfile.decode_data(htmlfile.read(self.html)), b"")
        # unreadable data is never silently dropped
        with open(self.html, "w") as f:
            f.write(htmlfile.encode_data(src, raw).replace(htmlfile.DATA_OPEN, htmlfile.DATA_OPEN + "QUJD", 1))
        r = subprocess.run(build, capture_output=True, text=True)
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("refusing to overwrite", r.stderr)

    def sh(self, *args, ok=True):
        env = {k: v for k, v in os.environ.items() if not (k.startswith("AI_") or k == "ANTHROPIC_API_KEY")}
        env.update(XDG_CONFIG_HOME=self.tmp, XDG_STATE_HOME=self.tmp)
        r = subprocess.run(["sh", self.html, *args], capture_output=True, text=True, timeout=60, cwd=self.tmp,
                           env=env)
        if ok:
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        return r

    def test_mode_and_header(self):
        self.assertEqual(os.stat(self.built).st_mode & 0o777, 0o755)
        with open(self.built, encoding="utf-8") as f:
            lines = f.read().split("\n", 3)
        self.assertTrue(lines[0].startswith("<!-- 2>/dev/null;"))
        self.assertEqual(lines[1], "-->")
        self.assertEqual(lines[2], "<!DOCTYPE html>")
        self.assertNotIn("'", lines[0].split("-c '", 1)[1].rsplit("' \"$0\"", 1)[0])

    def test_help(self):
        r = self.sh("help")
        self.assertIn("usage: sh", r.stdout)
        self.assertIn("extract-program", r.stdout)

    def test_unknown_command(self):
        self.assertEqual(self.sh("bogus", ok=False).returncode, 2)

    def test_info(self):
        r = self.sh("info")
        self.assertIn("server_running   False", r.stdout)
        self.assertIn(os.path.abspath(self.html), r.stdout)

    def test_sql_roundtrip(self):
        self.sh("sql", "CREATE TABLE t(x)", "--write")
        self.sh("sql", "INSERT INTO t VALUES (?), (?)", "--write", "hello", "</script>")
        self.assertEqual(self.sh("sql", "SELECT x FROM t ORDER BY rowid").stdout, "x\nhello\n</script>\n")
        r = self.sh("sql", "INSERT INTO t VALUES (1)", ok=False)  # read-only without --write
        self.assertNotEqual(r.returncode, 0)
        self.assertIn('"rows"', self.sh("sql", "SELECT count(*) n FROM t", "--json").stdout)
        # the program block is untouched by data writes
        with open(self.built, encoding="utf-8") as a, open(self.html, encoding="utf-8") as b:
            self.assertEqual(htmlfile.program_source(a.read()), htmlfile.program_source(b.read()))

    def test_db_extract_inject(self):
        db = os.path.join(self.tmp, "in.sqlite")
        con = sqlite3.connect(db)
        con.execute("CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT)")
        con.execute("INSERT INTO settings VALUES ('model', '\"m1\"')")
        con.commit()
        con.close()
        self.sh("db-inject", db)
        self.assertEqual(self.sh("settings", "get", "model").stdout.strip(), '"m1"')
        self.sh("settings", "set", "editor", '"vim"')
        out = os.path.join(self.tmp, "out.sqlite")
        self.sh("db-extract", out)
        con = sqlite3.connect(out)
        self.assertEqual(dict(con.execute("SELECT key, value FROM settings")), {"model": '"m1"', "editor": '"vim"'})
        self.assertNotEqual(self.sh("db-inject", self.html, ok=False).returncode, 0)  # not sqlite

    def test_export(self):
        self.sh("sql", "CREATE TABLE credentials(name TEXT PRIMARY KEY, value TEXT)", "--write")
        self.sh("sql", "INSERT INTO credentials VALUES ('K', 'secret')", "--write")
        out = os.path.join(self.tmp, "exported.html")
        self.sh("export", out, "--view", "session:s_1")
        self.assertTrue(os.access(out, os.X_OK))
        con = sqlite3.connect(":memory:")
        con.deserialize(htmlfile.read_data(out))
        self.assertEqual(con.execute("SELECT count(*) FROM credentials").fetchone()[0], 0)
        self.assertEqual(con.execute("SELECT value FROM settings WHERE key='ui.initial_view'").fetchone()[0],
                         '"session:s_1"')
        self.assertEqual(htmlfile.read_token(out), "")

    def test_export_embedded_credentials(self):
        self.sh("sql", "CREATE TABLE credentials(name TEXT PRIMARY KEY, value TEXT)", "--write")
        self.sh("sql", "INSERT INTO credentials VALUES ('K', 'secret')", "--write")
        self.sh("settings", "set", "credentials_storage", '"embedded"')
        self.sh("token", "regenerate")
        out = os.path.join(self.tmp, "exported_emb.html")
        r = self.sh("export", out)
        self.assertIn("export contains embedded credentials", r.stderr)
        con = sqlite3.connect(":memory:")
        con.deserialize(htmlfile.read_data(out))
        self.assertEqual(con.execute("SELECT value FROM credentials").fetchall(), [("secret",)])
        self.assertEqual(htmlfile.read_token(out), "")
        self.assertTrue(htmlfile.read_token(self.html))

    def test_token_offline(self):
        self.assertNotEqual(self.sh("token", ok=False).returncode, 0)  # fresh build: no token yet
        t1 = self.sh("token", "regenerate").stdout.strip()
        self.assertRegex(t1, r"^[A-Za-z0-9_-]{20,}$")
        self.assertEqual(self.sh("token").stdout.strip(), t1)
        self.assertEqual(htmlfile.read_token(self.html), t1)
        t2 = self.sh("token", "regenerate").stdout.strip()
        self.assertNotEqual(t1, t2)
        self.assertEqual(self.sh("token").stdout.strip(), t2)
        self.assertEqual(self.sh("sql", "SELECT 1 AS n").stdout, "n\n1\n")  # data block untouched
        self.assertEqual(self.sh("token", "bogus", ok=False).returncode, 2)

    def test_settings_defaults(self):
        self.assertEqual(self.sh("settings", "get", "credentials_storage").stdout.strip(), '"xdg"')
        self.assertIn('"provider"', self.sh("settings").stdout)

    def test_extract_program(self):
        d = os.path.join(self.tmp, "extracted")
        self.sh("extract-program", d)
        self.assertTrue(os.path.exists(os.path.join(d, "sfh", "main.py")))
        self.assertTrue(os.path.exists(os.path.join(d, "tremolo", "lib", "websocket.py")))
        with open(os.path.join(d, "sfh", "main.py"), encoding="utf-8") as f, \
                open(os.path.join(ROOT, "src", "py", "sfh", "main.py"), encoding="utf-8") as g:
            self.assertEqual(f.read(), g.read())  # originals, not the rewritten form
        env = dict(os.environ, PYTHONPATH=d)
        r = subprocess.run([sys.executable, "-m", "sfh", self.html, "sql", "SELECT 41+1 AS n"],
                           capture_output=True, text=True, env=env, cwd=self.tmp)
        self.assertEqual(r.stdout, "n\n42\n", r.stderr)
        r = subprocess.run([sys.executable, "-c", "import tremolo, tremolo.http_server"], env=env, cwd=self.tmp)
        self.assertEqual(r.returncode, 0)

    def test_inline_safety(self):
        with open(self.built, encoding="utf-8") as f:
            src = f.read()
        prog = htmlfile.program_source(src)
        self.assertIsNone(re.search(r"</?script|<!--|-->", prog, re.I))
        start = src.index('<script type="text/x-python"')
        # the browser must see exactly the closing tag after the program
        self.assertEqual(re.search(r"</script", src[start:], re.I).start(),
                         src[start:].index("\n#--SFH-PROGRAM-END--\n</script>") + len("\n#--SFH-PROGRAM-END--\n"))

    def test_bundler_escapes_strings(self):
        mod = os.path.join(self.tmp, "pkg_esc")
        os.makedirs(mod, exist_ok=True)
        with open(os.path.join(mod, "__init__.py"), "w") as f:
            f.write('"""doc </script>"""\nfrom .sub import V\n')
        with open(os.path.join(mod, "sub.py"), "w") as f:
            f.write('x = 1\nV = ["</SCRIPT>", b"<!--", f"a-->{x}<script", "ok"]\n'
                    'def main(argv):\n    print(repr(V))\n')
        text, _ = bundle.bundle([mod], "pkg_esc.sub")
        self.assertIsNone(re.search(r"</?script|<!--|-->", text, re.I))
        r = subprocess.run([sys.executable, "-c", text, "x"], capture_output=True, text=True)
        self.assertEqual(r.stdout.strip(), repr(["</SCRIPT>", b"<!--", "a-->1<script", "ok"]), r.stderr)


if __name__ == "__main__":
    unittest.main()
