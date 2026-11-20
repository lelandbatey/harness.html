"""End-to-end tests of peer sessions (awareness, claims, edit/write checks, messages) with the mock provider
(`[[tool:NAME:{json}]]` makes it call any tool).

Run: python3 -m unittest test.test_peers   (from the repo root)"""
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


def tool(name, **inp):
    return "[[tool:%s:%s]]" % (name, json.dumps(inp))


class TestPeers(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sfh-peers-")
        cls.html = os.path.join(cls.tmp, "harness.html")
        with open(cls.html, "w") as f:
            f.write(EMPTY_HTML)
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
        # each test works in its own repository (a .git dir marks the root; sessions in a subdir are peers)
        self.repo = tempfile.mkdtemp(dir=self.tmp, prefix="repo-")
        os.makedirs(os.path.join(self.repo, ".git"))
        os.makedirs(os.path.join(self.repo, "src"))

    def tearDown(self):
        self.c.close()

    def new_session(self, cwd=None):
        s = self.c.call("sessions.create", {"cwd": cwd or self.repo})
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

    def summary(self, sid):
        return next(s for s in self.c.call("sessions.list") if s["id"] == sid)

    def run_turn(self, sid, text, timeout=15):
        n = len(self.messages(sid))
        self.c.call("chat.send", {"session_id": sid, "text": text})
        self.wait_for(lambda: len(self.messages(sid)) > n and self.summary(sid)["status"] == "idle", timeout)
        return self.messages(sid)[n:]

    def results(self, msgs):
        return [b for m in msgs for b in m["content"] if b.get("type") == "tool_result"]

    def blocks(self, sid, key):
        return [b for m in self.messages(sid) for b in m["content"] if b.get(key)]

    def test_write_conflicts_claims_and_awareness(self):
        a, b = self.new_session(), self.new_session(os.path.join(self.repo, "src"))
        self.run_turn(a, "write it " + tool("write", path="f.txt", content="from a\n"))
        sa = self.summary(a)
        self.assertEqual(sa["repo"], self.repo)
        self.assertEqual([c["path"] for c in sa["claims"]], ["f.txt"])
        self.assertTrue(sa["claims"][0]["auto"])
        self.assertEqual([e["path"] for e in sa["edited"]], ["f.txt"])
        self.assertEqual(sa["activity"]["tool"], "write")

        # b (in a subdirectory of the same repo) never read f.txt: overwriting it is refused
        r = self.results(self.run_turn(b, "clobber " + tool("write", path="../f.txt", content="from b\n")))
        self.assertTrue(r[0]["is_error"], r)
        self.assertIn("changed by another session", r[0]["content"])
        self.assertIn(a, r[0]["content"])
        with open(os.path.join(self.repo, "f.txt")) as f:
            self.assertEqual(f.read(), "from a\n")
        # b was told about a before its first prompt
        note = self.blocks(b, "peer_sessions")
        self.assertEqual(note[0]["peer_sessions"], [a])
        self.assertIn("f.txt", note[0]["text"])
        self.assertIn("Avoid conflicts first", note[0]["text"])
        # the note is not a prompt: the mock answered the user's text only
        last = self.messages(b)[-1]
        self.assertNotIn("peer-sessions", texts(last))

        # edit is allowed but warns about a's claim
        r = self.results(self.run_turn(b, tool("edit", path="../f.txt", old_string="from a", new_string="from b")))
        self.assertFalse(r[0]["is_error"], r)
        self.assertIn("⚠", r[0]["content"])
        self.assertIn("claimed by session %s" % a, r[0]["content"])
        # after reading it, b may overwrite it
        self.run_turn(b, tool("read", path="../f.txt"))
        r = self.results(self.run_turn(b, tool("write", path="../f.txt", content="b again\n")))
        self.assertFalse(r[0]["is_error"], r)

        # explicit claims: a directory claim blocks claims inside it
        r = self.results(self.run_turn(a, tool("claim", paths=["src"], note="refactoring src")))
        self.assertIn("Claimed for 30 min: src", r[0]["content"])
        r = self.results(self.run_turn(b, tool("claim", paths=["x.py"], note="mine")))
        self.assertIn("NOT claimed", r[0]["content"])
        self.assertIn("refactoring src", r[0]["content"])
        r = self.results(self.run_turn(b, tool("peers")))
        self.assertIn(a, r[0]["content"])
        self.assertIn("src (refactoring src)", r[0]["content"])
        r = self.results(self.run_turn(a, tool("release")))
        self.assertIn("src", r[0]["content"])
        self.assertNotIn("src", [c["path"] for c in self.summary(a)["claims"]])
        self.assertEqual([p["session_id"] for p in self.c.call("peers.list", {"session_id": b})], [a])

    def test_message_to_idle_user_session_is_held(self):
        a, b = self.new_session(), self.new_session()
        self.run_turn(a, "hello")
        r = self.results(self.run_turn(b, tool("send_message", session_id=a, text="Are you editing app.js?")))
        self.assertFalse(r[0]["is_error"], r)
        self.assertIn("owned by the user", r[0]["content"])
        sa = self.summary(a)
        self.assertEqual(sa["status"], "idle")
        self.assertEqual(len(sa["inbox"]), 1)
        self.assertEqual(sa["inbox"][0]["kind"], "question")
        self.assertEqual(sa["inbox"][0]["from"], b)
        # the user lets it answer
        self.assertTrue(self.c.call("peers.deliver", {"session_id": a})["started"])
        pm = self.wait_for(lambda: self.blocks(a, "peer_message"))
        self.assertEqual(pm[0]["peer_message"]["text"], "Are you editing app.js?")
        self.assertIn('kind="question"', pm[0]["text"])
        self.assertIn('reply_to="%s"' % pm[0]["peer_message"]["id"], pm[0]["text"])
        self.wait_for(lambda: self.summary(a)["status"] == "idle" and self.messages(a)[-1]["role"] == "assistant")
        self.assertEqual(self.summary(a)["inbox"], [])
        # an info message is held and arrives with the next prompt
        self.run_turn(b, tool("send_message", session_id=a, text="FYI: I renamed foo", kind="info"))
        msgs = self.run_turn(a, "next prompt")
        self.assertEqual(msgs[0]["content"][-1]["peer_message"]["kind"], "info")
        self.assertEqual(texts(msgs[1]), "next prompt")
        # the model gets the held message together with the prompt
        self.assertIn("FYI: I renamed foo", texts(msgs[-1]))
        self.assertTrue(texts(msgs[-1]).endswith("next prompt"))

    def test_question_wakes_idle_subagent_without_reporting_to_parent(self):
        parent = self.new_session()
        self.c.call("chat.send", {"session_id": parent, "text": "[[subagent:child task]]"})
        child = self.wait_for(lambda: next((s for s in self.c.call("sessions.list") if s["parent_id"] == parent), None))
        cid = child["id"]
        self.wait_for(lambda: self.summary(cid)["status"] == "idle" and self.summary(parent)["status"] == "idle"
                      and any("<subagent-result" in texts(m) for m in self.messages(parent)))
        n_parent = len(self.messages(parent))
        other = self.new_session()
        r = self.results(self.run_turn(other, tool("send_message", session_id=cid, text="what did you find?")))
        self.assertIn("starts a turn to answer", r[0]["content"])
        self.wait_for(lambda: self.blocks(cid, "peer_message") and self.summary(cid)["status"] == "idle"
                      and self.messages(cid)[-1]["role"] == "assistant")
        self.assertIn("Mock reply", texts(self.messages(cid)[-1]))
        time.sleep(0.5)
        self.assertEqual(len(self.messages(parent)), n_parent)  # no second <subagent-result>

    def test_message_reaches_running_session_between_steps(self):
        a, b = self.new_session(), self.new_session()
        self.c.call("chat.send", {"session_id": a, "text": "[[bash:sleep 1.5; echo done]]"})
        self.wait_for(lambda: self.summary(a)["status"] == "running")
        r = self.results(self.run_turn(b, tool("send_message", session_id=a, text="heads up", kind="info")))
        self.assertIn("after its current step", r[0]["content"])
        self.wait_for(lambda: self.summary(a)["status"] == "idle" and self.messages(a)[-1]["role"] == "assistant")
        msgs = self.messages(a)
        roles = [("results" if self.results([m]) else "peer" if any(x.get("peer_message") for x in m["content"])
                  else m["role"]) for m in msgs]
        i = roles.index("results")
        self.assertEqual(roles[i + 1], "peer", roles)
        self.assertEqual(roles[i + 2], "assistant", roles)
        self.assertIn("Tool said: done", texts(msgs[-1]))  # the mock still sees the tool results

    def test_scope(self):
        a = self.new_session()
        elsewhere = tempfile.mkdtemp(dir=self.tmp, prefix="other-")
        os.makedirs(os.path.join(elsewhere, ".git"))
        z = self.new_session(elsewhere)
        self.run_turn(z, "hi")
        r = self.results(self.run_turn(a, tool("send_message", session_id=z, text="hi?")))
        self.assertTrue(r[0]["is_error"])
        self.assertIn("another repository", r[0]["content"])
        self.assertEqual(self.c.call("peers.list", {"session_id": a}), [])
        self.assertEqual(self.blocks(a, "peer_sessions"), [])


if __name__ == "__main__":
    unittest.main()
