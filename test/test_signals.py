"""Unit tests of what a session is about (signals.extract) and the title/subjects prompt built from it.

Run: python3 -m unittest test.test_signals   (from the repo root)"""
import os
import shutil
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.join(ROOT, "src", "py"), os.path.join(ROOT, "vendor")]

from sfh import agent, signals  # noqa: E402


def user(text, cwd):
    return {"role": "user", "cwd": cwd, "content": [{"type": "text", "text": text}]}


def tool(cwd, name, **inp):
    return {"role": "assistant", "cwd": cwd, "content": [{"type": "tool_use", "id": "t", "name": name, "input": inp}]}


class TestExtract(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="sfh-sig-")
        for d in ("alpha-app/src", "beta-lib/pkg", "group/gamma-svc/cmd", "notes"):
            os.makedirs(os.path.join(self.root, d))
        for f in ("alpha-app/.git", "beta-lib/go.mod", "group/gamma-svc/package.json", "alpha-app/src/main.py"):
            open(os.path.join(self.root, f), "w").close()

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def rel(self, sig, key):
        return [signals.short(p, self.root) for p in sig[key]]

    def test_tool_targets_and_cd(self):
        r = self.root
        msgs = [user("please look at the failing build", r),
                tool(r, "read", path="alpha-app/src/main.py"),
                tool(r, "bash", command="cd %s/beta-lib && go test ./pkg/..." % r),
                tool(r, "edit", path=os.path.join(r, "alpha-app/src/main.py"), old_string="a", new_string="b")]
        sig = signals.extract(msgs, r)
        self.assertEqual(self.rel(sig, "projects"), ["alpha-app", "beta-lib"])
        self.assertEqual(self.rel(sig, "paths")[0], "alpha-app/src/main.py")
        self.assertIn("beta-lib", self.rel(sig, "paths"))

    def test_user_mentions_beat_tool_noise(self):
        r = self.root
        msgs = [user("Apply and merge gamma-svc PR 231 (and/or see https://x.io/a/b)", r),
                tool(r, "glob", pattern="*.md", path="notes")]
        sig = signals.extract(msgs, r)
        self.assertEqual(self.rel(sig, "projects")[0], "group/gamma-svc")  # named subfolder two levels down
        self.assertNotIn("and/or", self.rel(sig, "paths"))  # prose and URLs that are not paths are ignored
        self.assertFalse([p for p in sig["paths"] if "x.io" in p])

    def test_root_alone_is_no_project(self):
        r = self.root
        sig = signals.extract([user("hi", r), tool(r, "bash", command="ls"), tool(r, "read", path="README.md")], r)
        self.assertEqual(sig["projects"], [])

    def test_root_repo(self):
        r = self.root
        open(os.path.join(r, "Makefile"), "w").close()
        # a session on the root repo itself: the root is its project (not src/, docs/, deps)
        msgs = [tool(r, "read", path="notes/a.md"), tool(r, "read", path="node_modules/x/package.json")]
        sig = signals.extract(msgs, r)
        self.assertEqual(sig["projects"], [r])
        # a repo of repos: the subproject worked on, not the root it lives in
        sig = signals.extract([tool(r, "read", path="notes/a.md"), tool(r, "read", path="beta-lib/pkg/a.go")], r)
        self.assertEqual(self.rel(sig, "projects"), ["beta-lib"])

    def test_moved_cwd_and_skipped_blocks(self):
        r = self.root
        peer = {"type": "text", "text": "edited beta-lib/pkg", "peer_sessions": True}
        msgs = [user("hi", r), {"role": "user", "cwd": os.path.join(r, "beta-lib"), "content": [peer]}]
        sig = signals.extract(msgs, r, lambda b: bool(b.get("peer_sessions")))
        self.assertEqual(self.rel(sig, "projects"), ["beta-lib"])  # the cwd it moved to, not the peer text

    def test_caps(self):
        r = self.root
        msgs = [tool(r, "write", path="notes/n%d.md" % i, content="") for i in range(40)]
        sig = signals.extract(msgs, r)
        self.assertEqual(len(sig["paths"]), signals.MAX_PATHS)
        self.assertEqual(self.rel(sig, "projects"), ["notes"])


class TestPrompt(unittest.TestCase):
    def test_prompt(self):
        sig = {"projects": ["/w/alpha-app"], "paths": ["/w/alpha-app/src/main.py"]}
        ctx = [{"title": "beta-lib flaky test fix", "cwd": "/w", "subjects": ["software development", "beta-lib", "x"],
                "signals": {"projects": ["/w/beta-lib"]}, "same_cwd": True},
               {"title": "old", "cwd": "/w", "subjects": ["a", "b", "c"], "signals": {}, "same_cwd": True}]
        p = agent.subjects_prompt(3, "user: merge it", "/w", ctx, sig)
        print("\n----- prompt -----\n%s\n------------------" % p) if os.environ.get("SHOW_PROMPT") else None
        self.assertIn('"foo-project PR 231 apply and merge"', p)
        self.assertIn("never starts with a verb", p)
        self.assertIn("Projects/folders: alpha-app", p)
        self.assertIn("Paths touched or named: alpha-app/src/main.py", p)
        self.assertIn("WEAK signal", p)
        self.assertIn("- beta-lib flaky test fix | /w | beta-lib | software development > beta-lib > x", p)
        self.assertIn("- old | /w | - | a > b > c", p)
        self.assertIn("NOT the same topic", p)
        self.assertIn("<conversation>\nuser: merge it\n</conversation>", p)


if __name__ == "__main__":
    unittest.main()
