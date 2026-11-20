"""Tests of the webfetch tool: the html→markdown converter (offline) and fetching from a local http.server.

Run: python3 -m unittest test.test_webfetch   (from the repo root)"""
import http.server
import os
import sys
import tempfile
import threading
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src", "py"), os.path.join(ROOT, "vendor")]

from sfh import tools, webfetch  # noqa: E402

PAGE = """<!DOCTYPE html><html><head><title>Test Page</title><style>body{color:red}</style>
<script>alert('x')</script></head><body>
<nav><a href="/home">Home</a> | <a href="/about">About</a></nav>
<h1>Main   heading</h1>
<p>Some <b>bold</b> and <i>italic</i> text with a <a href="/rel/link?x=1">relative link</a>
and <code>inline_code()</code>.</p>
<ul><li>first</li><li>second<ul><li>nested</li></ul></li></ul>
<ol><li>one</li><li>two</li></ol>
<pre><code>def f():
    return 1
</code></pre>
<blockquote>quoted words</blockquote>
<table><tr><th>Name</th><th>Value</th></tr><tr><td>a</td><td>1</td></tr><tr><td>b|c</td><td>2</td></tr></table>
<img src="/pic.png" alt="A picture"><img src="/spacer.gif">
<footer>copyright noise</footer>
</body></html>"""


class TestConverter(unittest.TestCase):
    def setUp(self):
        self.md = webfetch.html_to_markdown(PAGE, "http://example.test/dir/page.html")

    def test_noise_dropped(self):
        for noise in ("alert", "color:red", "Home", "About", "copyright", "spacer"):
            self.assertNotIn(noise, self.md)

    def test_structure(self):
        md = self.md
        self.assertIn("# Main heading", md)
        self.assertIn("**bold**", md)
        self.assertIn("*italic*", md)
        self.assertIn("[relative link](http://example.test/rel/link?x=1)", md)
        self.assertIn("`inline_code()`", md)
        self.assertIn("- first\n- second\n  - nested", md)
        self.assertIn("1. one\n2. two", md)
        self.assertIn("```\ndef f():\n    return 1\n\n```", md)
        self.assertIn("> quoted words", md)
        self.assertIn("| Name | Value |\n|---|---|\n| a | 1 |\n| b\\|c | 2 |", md)
        self.assertIn("![A picture](http://example.test/pic.png)", md)
        self.assertNotIn("\n\n\n", md)

    def test_title_prepended_without_h1(self):
        md = webfetch.html_to_markdown("<html><head><title>Only Title</title></head><body><p>x</p></body></html>")
        self.assertTrue(md.startswith("# Only Title\n\nx"), md)

    def test_text(self):
        t = webfetch.html_to_text(PAGE)
        self.assertIn("relative link", t)
        self.assertNotIn("](", t)
        self.assertNotIn("**", t)

    def test_cap(self):
        out = webfetch.cap("x" * 200000)
        self.assertLess(len(out), 110000)
        self.assertIn("truncated", out)


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_GET(self):
        routes = {
            "/page": (200, "text/html; charset=utf-8", PAGE.encode()),
            "/latin": (200, "text/html; charset=iso-8859-1", "<p>caf\xe9</p>".encode("latin-1")),
            "/plain": (200, "text/plain", b"just *text*"),
            "/bin": (200, "application/octet-stream", b"\x00\x01\x02binary"),
            "/big": (200, "text/plain", b"y" * 300000),
        }
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/page")
            self.end_headers()
            return
        if self.path == "/ua":
            body = self.headers.get("User-Agent", "").encode()
            routes["/ua"] = (200, "text/plain", body)
        code, ctype, body = routes.get(self.path, (404, "text/plain", b"nope"))
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class TestFetch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.base = "http://127.0.0.1:%d" % cls.httpd.server_address[1]
        threading.Thread(target=cls.httpd.serve_forever, args=(0.05,), daemon=True).start()
        cls.ctx = tools.Context(tempfile.gettempdir())

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def run_tool(self, **inp):
        return tools.execute("webfetch", inp, self.ctx)

    def test_markdown_via_redirect(self):
        out, err = self.run_tool(url=self.base + "/redirect")
        self.assertFalse(err, out)
        self.assertIn("URL: %s/page" % self.base, out)
        self.assertIn("# Main heading", out)
        self.assertIn("(%s/rel/link?x=1)" % self.base, out)

    def test_formats(self):
        out, _ = self.run_tool(url=self.base + "/page", format="html")
        self.assertIn("<script>", out)
        out, _ = self.run_tool(url=self.base + "/page", format="text")
        self.assertIn("relative link", out)
        self.assertNotIn("<p>", out)
        out, err = self.run_tool(url=self.base + "/page", format="pdf")
        self.assertTrue(err)

    def test_charset_plain_ua(self):
        out, _ = self.run_tool(url=self.base + "/latin")
        self.assertIn("café", out)
        out, _ = self.run_tool(url=self.base + "/plain")
        self.assertTrue(out.endswith("just *text*"), out)
        out, _ = self.run_tool(url=self.base + "/ua")
        self.assertIn("Mozilla/5.0", out)

    def test_errors(self):
        out, err = self.run_tool(url=self.base + "/bin")
        self.assertTrue(err)
        self.assertIn("binary", out)
        out, err = self.run_tool(url=self.base + "/missing")
        self.assertTrue(err)
        self.assertIn("404", out)
        out, err = self.run_tool(url="file:///etc/passwd")
        self.assertTrue(err)
        self.assertIn("http", out)

    def test_truncation(self):
        out, err = self.run_tool(url=self.base + "/big", timeout=500)
        self.assertFalse(err)
        self.assertIn("truncated", out)
        self.assertLess(len(out), 110000)


if __name__ == "__main__":
    unittest.main()
