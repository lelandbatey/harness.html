"""End-to-end tests of the subagent tool with the mock provider (`[[subagent:PROMPT]]`).

Run: python3 -m unittest test.test_subagents   (from the repo root)"""
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
            time.sleep(0.1)
        raise AssertionError("condition not met in %ss" % timeout)

    def messages(self, sid):
        return self.c.call("sessions.subscribe", {"session_id": sid})["messages"]

    def results(self, sid):
        return [texts(m) for m in self.messages(sid) if m["role"] == "user" and "<subagent-result" in texts(m)]

    def summary(self, sid):
        return next(s for s in self.c.call("sessions.list") if s["id"] == sid)

    def children(self, parent):
        return [s for s in self.c.call("sessions.list") if s["parent_id"] == parent]

    def test_child_result_starts_parent_turn(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "go [[subagent:hello child]]"})
        # the tool returns immediately with the child's id
        tool_result = self.wait_for(lambda: next((b for m in self.messages(sid) for b in m["content"]
                                                  if b.get("type") == "tool_result"), None))
        self.assertFalse(tool_result["is_error"], tool_result)
        self.assertIn('"status": "started"', tool_result["content"])
        kids = self.children(sid)
        self.assertEqual(len(kids), 1)
        child = kids[0]
        self.assertIn(child["id"], tool_result["content"])
        self.assertEqual(child["kind"], "subagent")
        self.assertEqual(child["cwd"], self.workdir)
        self.assertEqual(self.summary(sid)["children"], 1)
        self.assertEqual(self.summary(sid)["kind"], "chat")
        self.assertIsNone(self.summary(sid)["parent_id"])
        res = self.wait_for(lambda: self.results(sid))
        self.assertIn('session_id="%s"' % child["id"], res[0])
        self.assertIn('description="mock task"', res[0])
        self.assertIn('status="completed"', res[0])
        self.assertIn("**Mock reply** to: hello child", res[0])
        # the parent ran another turn answering the result
        self.wait_for(lambda: self.messages(sid)[-1]["role"] == "assistant" and
                      "subagent-result" in texts(self.messages(sid)[-1]) and
                      self.summary(sid)["status"] == "idle")
        # (a <peer-sessions> note may come first: sessions of other tests can still run in the same workdir)
        cm = [m for m in self.messages(child["id"]) if not all(b.get("peer_sessions") for b in m["content"])]
        self.assertEqual(texts(cm[0]), "hello child")
        self.assertEqual(texts(cm[-1]), "**Mock reply** to: hello child")

    def test_two_children_concurrently(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "[[subagent:first [[slow]]]] [[subagent:second [[slow]]]]"})
        kids = self.wait_for(lambda: len(self.children(sid)) == 2 and self.children(sid))
        self.wait_for(lambda: all(s["status"] == "running" for s in self.children(sid)), 10)
        res = self.wait_for(lambda: len(self.results(sid)) == 2 and self.results(sid), 40)
        for k in kids:
            self.assertTrue(any(k["id"] in r and 'status="completed"' in r for r in res), res)

    def test_stop_child(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "[[subagent:long one [[slow]]]]"})
        child = self.wait_for(lambda: self.children(sid) and self.children(sid)[0]["status"] == "running"
                              and self.children(sid)[0])
        self.c.call("chat.stop", {"session_id": child["id"]})
        res = self.wait_for(lambda: self.results(sid))
        self.assertIn('status="stopped"', res[0])
        self.assertIn(child["id"], res[0])

    def test_child_cannot_launch_subagents(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "[[subagent:nested [[subagent:deeper]]]]"})
        res = self.wait_for(lambda: self.results(sid))
        child = self.children(sid)[0]
        self.assertEqual(self.children(child["id"]), [])
        tr = [b for m in self.messages(child["id"]) for b in m["content"] if b.get("type") == "tool_result"]
        self.assertTrue(tr[0]["is_error"])
        self.assertIn("not available", tr[0]["content"])
        self.assertIn('status="completed"', res[0])


if __name__ == "__main__":
    unittest.main()
