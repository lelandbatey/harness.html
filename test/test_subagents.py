"""End-to-end tests of unified subagent threads (`spawn` with context='fresh' and context='fork').

Run: python3 -m unittest test.test_subagents   (from the repo root)"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(ROOT, "src", "py"), os.path.join(ROOT, "vendor")]

from test_server import EMPTY_HTML, SECRET_ENV, ServerProc  # noqa: E402


def texts(msg):
    return "".join(b.get("text", "") for b in msg["content"] if b.get("type") == "text")


class TestSubagents(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sfh-sub-")
        cls.html = os.path.join(cls.tmp, "harness.html")
        with open(cls.html, "w") as f:
            f.write(EMPTY_HTML)
        cls.workdir = os.path.join(cls.tmp, "work")
        os.makedirs(cls.workdir)
        env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(cls.tmp, "cfg"), SHELL="/bin/sh", SFH_MOCK="1",
                   PYTHONPATH=os.pathsep.join([os.path.join(ROOT, "src", "py"), os.path.join(ROOT, "vendor")]))
        for k in list(env):
            if k in SECRET_ENV or k.startswith("AI_"):
                env.pop(k)
        cls.server = ServerProc(cls.html, env).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.stop()
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def setUp(self):
        self.c = self.server.rpc()
        self.c.call("hello", {"client": "tui"})

    def tearDown(self):
        self.c.close()

    def new_session(self):
        s = self.c.call("sessions.create", {"cwd": self.workdir})
        self.c.call("sessions.subscribe", {"session_id": s["id"]})
        return s["id"]

    def wait_for(self, pred, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.c.poll_events(0.05)
            v = pred()
            if v:
                return v
            time.sleep(0.05)
        raise AssertionError("condition not met in %ss" % timeout)

    def messages(self, sid, bid=None):
        p = {"session_id": sid}
        if bid:
            p["branch_id"] = bid
        return self.c.call("sessions.subscribe", p)["messages"]

    def results(self, sid):
        return [texts(m) for m in self.messages(sid) if m["role"] == "user" and "<thread-message" in texts(m)]

    def summary(self, sid):
        return next(s for s in self.c.call("sessions.list") if s["id"] == sid)

    def subagent_threads(self, sid):
        return [b for b in self.c.call("branches.list", {"session_id": sid})["branches"]
                if b["kind"] == "subagent"]

    def test_child_result_starts_parent_turn(self):
        sid = self.new_session()
        main_bid = self.c.call("branches.list", {"session_id": sid})["main_branch_id"]
        self.c.call("chat.send", {"session_id": sid, "text": "go [[subagent:hello child]]"})
        tool_result = self.wait_for(lambda: next((b for m in self.messages(sid, main_bid) for b in m["content"]
                                                  if b.get("type") == "tool_result"), None))
        self.assertFalse(tool_result["is_error"], tool_result)
        parsed = json.loads(tool_result["content"])
        self.assertEqual(parsed["status"], "started")
        self.assertEqual(parsed["context"], "fresh")
        kids = self.subagent_threads(sid)
        self.assertEqual(len(kids), 1)
        child = kids[0]
        self.assertEqual(child["id"], parsed["thread_id"])
        self.assertEqual(child["kind"], "subagent")
        self.assertEqual(child["context_mode"], "fresh")
        self.assertIsNone(child["origin_id"])
        self.assertIsNotNone(child["spawned_at_id"])
        # Session summary exposes the thread tree for the left sidebar
        s = self.summary(sid)
        self.assertEqual(s["branch_count"], 2)
        self.assertTrue(any(t["id"] == child["id"] and t["kind"] == "subagent" for t in s["threads"]))
        res = self.wait_for(lambda: self.results(sid))
        self.assertIn('from="%s"' % child["id"], res[0])
        self.assertIn('title="mock task"', res[0])
        self.assertIn('kind="subagent"', res[0])
        self.assertIn('status="completed"', res[0])
        self.assertIn("**Mock reply** to: hello child", res[0])
        # parent ran another turn answering the result
        self.wait_for(lambda: self.messages(sid, main_bid)[-1]["role"] == "assistant" and
                      "thread-message" in texts(self.messages(sid, main_bid)[-1]) and
                      self.summary(sid)["status"] == "idle")
        # fresh subagent only has its own messages (no parent prefix!)
        cm = [m for m in self.messages(sid, child["id"]) if not all(b.get("peer_sessions") for b in m["content"])]
        self.assertEqual(len(cm), 2)
        self.assertEqual(texts(cm[0]), "hello child")
        self.assertEqual(texts(cm[-1]), "**Mock reply** to: hello child")

    def test_forked_subagent_inherits_parent_context(self):
        sid = self.new_session()
        main_bid = self.c.call("branches.list", {"session_id": sid})["main_branch_id"]
        self.c.call("chat.send", {"session_id": sid, "text": "parent context seed"})
        self.wait_for(lambda: len(self.messages(sid, main_bid)) >= 2 and self.summary(sid)["status"] == "idle")
        # Spawn a forked subagent that inspects [[history]]
        self.c.call("chat.send", {"session_id": sid, "text": "check [[fork:[[history]] forked check]]"})
        res = self.wait_for(lambda: self.results(sid))
        kids = self.subagent_threads(sid)
        self.assertEqual(len(kids), 1)
        child = kids[0]
        self.assertEqual(child["context_mode"], "fork")
        self.assertIsNotNone(child["origin_id"])
        # Forked subagent saw parent messages (parent prompt + parent reply + trigger prompt + its own prompt = 4!)
        self.assertIn("History: 4 messages", res[0])
        self.assertIn("parent context seed", res[0])

    def test_two_children_concurrently(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "[[subagent:first [[slow]]]] [[subagent:second [[slow]]]]"})
        kids = self.wait_for(lambda: len(self.subagent_threads(sid)) == 2 and self.subagent_threads(sid))
        self.wait_for(lambda: all(k["status"] == "running" for k in self.subagent_threads(sid)), 10)
        res = self.wait_for(lambda: len(self.results(sid)) == 2 and self.results(sid), 40)
        for k in kids:
            self.assertTrue(any(k["id"] in r and 'status="completed"' in r for r in res), res)

    def test_stop_child(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "[[subagent:long one [[slow]]]]"})
        child = self.wait_for(lambda: self.subagent_threads(sid) and self.subagent_threads(sid)[0]["status"] == "running"
                              and self.subagent_threads(sid)[0])
        self.c.call("chat.stop", {"session_id": sid, "branch_id": child["id"]})
        res = self.wait_for(lambda: self.results(sid))
        self.assertIn('status="stopped"', res[0])
        self.assertIn(child["id"], res[0])

    def test_child_cannot_launch_subagents(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "[[subagent:nested [[subagent:deeper]]]]"})
        res = self.wait_for(lambda: self.results(sid))
        kids = self.subagent_threads(sid)
        self.assertEqual(len(kids), 1)
        child = kids[0]
        tr = [b for m in self.messages(sid, child["id"]) for b in m["content"] if b.get("type") == "tool_result"]
        self.assertTrue(tr[0]["is_error"])
        self.assertIn("not available", tr[0]["content"])
        self.assertIn('status="completed"', res[0])


if __name__ == "__main__":
    unittest.main()
