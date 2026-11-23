"""End-to-end tests of subsessions (/subsession): user-owned child sessions with an empty context, nested
under the session they were started from, with their own cwd and model.

Run: python3 -m unittest test.test_subsessions   (from the repo root)"""
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


class TestSubsessions(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sfh-subsess-")
        cls.html = os.path.join(cls.tmp, "harness.html")
        with open(cls.html, "w") as f:
            f.write(EMPTY_HTML)
        cls.workdir = os.path.join(cls.tmp, "work")
        cls.otherdir = os.path.join(cls.tmp, "other")
        os.makedirs(cls.workdir)
        os.makedirs(cls.otherdir)
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
        self.events = []

    def tearDown(self):
        self.c.close()

    def new_session(self):
        s = self.c.call("sessions.create", {"cwd": self.workdir})
        self.c.call("sessions.subscribe", {"session_id": s["id"]})
        return s["id"]

    def wait_for(self, pred, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.events += self.c.poll_events(0.05)
            v = pred()
            if v:
                return v
            time.sleep(0.05)
        raise AssertionError("condition not met in %ss" % timeout)

    def summary(self, sid):
        return next((s for s in self.c.call("sessions.list") if s["id"] == sid), None)

    def messages(self, sid):
        return self.c.call("sessions.subscribe", {"session_id": sid})["messages"]

    def opened(self):
        return [e["data"]["session_id"] for e in self.events if e["event"] == "session.open"]

    def test_subsession_command(self):
        parent = self.new_session()
        self.c.call("sessions.set_model", {"session_id": parent, "model": "mock-other"})
        self.c.call("chat.send", {"session_id": parent, "text": "/subsession hello sub"})
        child = self.wait_for(lambda: self.opened() and self.opened()[0])
        s = self.summary(child)
        self.assertEqual(s["parent_id"], parent)
        self.assertEqual(s["kind"], "subsession")
        self.assertEqual(s["cwd"], self.workdir)
        self.assertEqual(s["model_override"], "mock-other")  # inherits the parent's model choice
        # an empty context: only its own prompt, answered in the subsession
        self.wait_for(lambda: len(self.messages(child)) >= 2 and self.summary(child)["status"] == "idle")
        msgs = self.messages(child)
        self.assertEqual(texts(msgs[0]), "hello sub")
        self.assertEqual(texts(msgs[-1]), "**Mock reply** to: hello sub")
        self.assertEqual([m for m in self.messages(parent) if m["role"] != "system"], [])
        p = self.summary(parent)
        self.assertEqual(p["children"], 1)
        self.assertTrue(p.get("usage_subsessions"), p)
        # its model sees where it comes from
        self.c.call("chat.send", {"session_id": child, "text": "/system"})
        note = self.wait_for(lambda: next((e["data"]["text"] for e in self.events if e["event"] == "notice"
                                           and "# Subsession" in e["data"].get("text", "")), None))
        self.assertIn(parent, note)

    def test_own_cwd_and_model(self):
        parent = self.new_session()
        child = self.c.call("sessions.create", {"parent_id": parent})["id"]
        self.assertEqual(self.summary(child)["kind"], "subsession")
        self.c.call("sessions.set_cwd", {"session_id": child, "cwd": self.otherdir})
        self.c.call("sessions.set_model", {"session_id": child, "model": "mock-sub"})
        self.assertEqual(self.summary(child)["cwd"], self.otherdir)
        self.assertEqual(self.summary(parent)["cwd"], self.workdir)
        self.assertEqual(self.summary(child)["model_override"], "mock-sub")
        self.assertIsNone(self.summary(parent)["model_override"])

    def test_nested_subsessions_and_parent_delete(self):
        parent = self.new_session()
        child = self.c.call("sessions.create", {"parent_id": parent})["id"]
        grandchild = self.c.call("sessions.create", {"parent_id": child})["id"]
        self.assertEqual(self.summary(grandchild)["parent_id"], child)
        self.c.call("chat.send", {"session_id": grandchild, "text": "deep"})
        self.wait_for(lambda: self.summary(grandchild)["status"] == "idle" and self.summary(grandchild).get("usage"))
        self.assertTrue(self.summary(parent).get("usage_subsessions"))  # costs roll up all levels
        self.c.call("sessions.delete", {"session_id": parent})
        self.assertIsNone(self.summary(parent))
        self.assertIsNone(self.summary(child)["parent_id"])  # it outlives its parent as a top-level session
        self.assertEqual(self.summary(grandchild)["parent_id"], child)


if __name__ == "__main__":
    unittest.main()
