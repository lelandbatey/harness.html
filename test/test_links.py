"""links.py: local file links are resolved (against the message's cwd) when a message is stored."""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src", "py"), os.path.join(ROOT, "vendor")]

from sfh import links  # noqa: E402


class LinksTest(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="sfh-links-")
        os.makedirs(os.path.join(self.d, "src"))
        for f in ("src/a.py", "Makefile", "my file.txt"):
            with open(os.path.join(self.d, f), "w") as fh:
                fh.write("x\n")

    def tearDown(self):
        shutil.rmtree(self.d, ignore_errors=True)

    def test_existing_paths_only(self):
        text = ("See `src/a.py:12`, `src/missing.py`, `Makefile`, `src` and [the dir](src/) and "
                "[gone](nope.md) and [abs](%s/Makefile#L3) and `x = 1`." % self.d)
        found = links.find([text], self.d)
        self.assertEqual(found["src/a.py"], {"path": os.path.join(self.d, "src/a.py"), "dir": False})
        self.assertEqual(found["Makefile"]["path"], os.path.join(self.d, "Makefile"))
        self.assertEqual(found["src/"], {"path": os.path.join(self.d, "src"), "dir": True})
        self.assertIn(self.d + "/Makefile", found)
        for k in ("src/missing.py", "nope.md", "x", "src"):  # a bare dir name in inline code is not a link
            self.assertNotIn(k, found)

    def test_relative_to_cwd(self):
        self.assertIn("a.py", links.find(["`a.py`"], os.path.join(self.d, "src")))
        self.assertNotIn("a.py", links.find(["`a.py`"], self.d))

    def test_href_decoding_and_file_urls(self):
        found = links.find(["[f](my%%20file.txt) <file://%s/Makefile>" % self.d], self.d)
        self.assertIn("my file.txt", found)
        self.assertIn(self.d + "/Makefile", found)

    def test_fenced_code_is_ignored(self):
        self.assertEqual(links.find(["```\n`Makefile`\n```\n"], self.d), {})

    def test_for_content_reads_text_and_thinking(self):
        content = [{"type": "thinking", "thinking": "look at `Makefile`"},
                   {"type": "tool_use", "id": "t", "name": "bash", "input": {"command": "cat src/a.py"}},
                   {"type": "text", "text": "done: `src/a.py`"}]
        self.assertEqual(sorted(links.for_content(content, self.d)), ["Makefile", "src/a.py"])


if __name__ == "__main__":
    unittest.main()
