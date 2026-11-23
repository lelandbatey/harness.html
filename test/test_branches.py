"""End-to-end tests of lightweight conversation branching (/aside), auto-promotion, branch tools
(`branches`, `ask_branch`, `message_branch`), live multi-client switching, and offline branch snapshots.

Run: python3 -m unittest test.test_branches   (from the repo root)"""
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

from sfh import htmlfile  # noqa: E402
from test_server import EMPTY_HTML, SECRET_ENV, ServerProc, wait_event, wait_idle  # noqa: E402


def texts(msg):
    return "".join(b.get("text", "") for b in msg["content"] if b.get("type") == "text")


class TestBranches(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sfh-br-")
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
        self.c.call("hello", {"client": "web"})

    def tearDown(self):
        self.c.close()

    def new_session(self):
        s = self.c.call("sessions.create", {"cwd": self.workdir})
        self.c.call("sessions.subscribe", {"session_id": s["id"]})
        return s["id"]

    def send_and_wait(self, sid, text):
        before = len(self.c.call("sessions.subscribe", {"session_id": sid})["messages"])
        self.c.call("chat.send", {"session_id": sid, "text": text})
        self.wait_for(lambda: (
            (snap := self.c.call("sessions.subscribe", {"session_id": sid}))
            and len(snap["messages"]) >= before + 2
            and snap["messages"][-1]["role"] == "assistant"
            and snap["session"]["status"] == "idle"
        ))

    def wait_for(self, pred, timeout=15):
        deadline = time.time() + timeout
        while time.time() < deadline:
            self.c.poll_events(0.05)
            v = pred()
            if v:
                return v
            time.sleep(0.05)
        raise AssertionError("condition not met in %ss" % timeout)

    def test_aside_create_switch_rename_delete(self):
        sid = self.new_session()
        self.send_and_wait(sid, "first main question")
        self.send_and_wait(sid, "second main question")
        snap0 = self.c.call("sessions.subscribe", {"session_id": sid})
        main_bid = snap0["branch_id"]
        msgs0 = snap0["messages"]
        self.assertEqual(len(msgs0), 4)
        self.assertEqual(len(snap0["branches"]["branches"]), 1)

        # Start an /aside with prompt from the tip
        self.send_and_wait(sid, "/aside quick detour about ports")
        snap1 = self.c.call("sessions.subscribe", {"session_id": sid})
        aside_bid = snap1["branch_id"]
        self.assertNotEqual(aside_bid, main_bid)
        self.assertEqual(len(snap1["messages"]), 6)
        self.assertEqual(texts(snap1["messages"][4]), "quick detour about ports")
        # Main branch appears as a fork at message #4 (the last shared message)
        fork_key = str(msgs0[3]["id"])
        self.assertIn(fork_key, snap1["branches"]["forks"])
        self.assertEqual(snap1["branches"]["forks"][fork_key][0]["branch_id"], main_bid)

        # Switch back to main via /branch main
        self.c.call("chat.send", {"session_id": sid, "text": "/branch main"})
        wait_event(self.c, lambda n, d: n == "branch.switched" and d["branch_id"] == main_bid)
        snap2 = self.c.call("sessions.subscribe", {"session_id": sid})
        self.assertEqual(snap2["branch_id"], main_bid)
        self.assertEqual(len(snap2["messages"]), 4)
        # Aside appears as a fork at message #4
        chips = snap2["branches"]["forks"][fork_key]
        self.assertEqual(len(chips), 1)
        self.assertEqual(chips[0]["branch_id"], aside_bid)
        self.assertEqual(chips[0]["own_messages"], 2)

        # Branch from an earlier mid-conversation message (msgs0[1]) via RPC
        created = self.c.call("branches.create", {
            "session_id": sid, "from_message_id": msgs0[1]["id"], "title": "earlier tangent",
            "prompt": "what about earlier?"})
        mid_bid = created["branch"]["id"]
        wait_idle(self.c, sid)
        snap3 = self.c.call("sessions.subscribe", {"session_id": sid})
        self.assertEqual(snap3["branch_id"], mid_bid)
        self.assertEqual([texts(m) for m in snap3["messages"][:3]],
                         ["first main question", "**Mock reply** to: first main question", "what about earlier?"])
        # Rename and delete
        renamed = self.c.call("branches.rename", {"session_id": sid, "branch_id": mid_bid, "title": "renamed tangent"})
        self.assertEqual(renamed["name"], "renamed tangent")
        self.c.call("branches.delete", {"session_id": sid, "branch_id": mid_bid})
        snap4 = self.c.call("sessions.subscribe", {"session_id": sid})
        self.assertEqual(snap4["branch_id"], main_bid)
        self.assertEqual(len(snap4["branches"]["branches"]), 2)

    def test_aside_auto_promotes_to_main(self):
        sid = self.new_session()
        self.send_and_wait(sid, "initial setup")
        main_bid = self.c.call("sessions.subscribe", {"session_id": sid})["branch_id"]

        # Create aside at tip of main and chat 3 prompts there -> should auto-promote to main!
        self.send_and_wait(sid, "/aside exploring option B")
        aside_bid = self.c.call("sessions.subscribe", {"session_id": sid})["branch_id"]
        self.assertNotEqual(aside_bid, main_bid)
        info1 = self.c.call("branches.list", {"session_id": sid})
        self.assertEqual(info1["main_branch_id"], main_bid)  # 1 prompt: still aside

        self.send_and_wait(sid, "second prompt on aside")
        info2 = self.c.call("branches.list", {"session_id": sid})
        self.assertEqual(info2["main_branch_id"], main_bid)  # 2 prompts: still aside

        self.send_and_wait(sid, "third prompt on aside makes it main")
        info3 = self.c.call("branches.list", {"session_id": sid})
        self.assertEqual(info3["main_branch_id"], aside_bid)  # 3 prompts & deeper past fork -> promoted!
        old_main = next(b for b in info3["branches"] if b["id"] == main_bid)
        self.assertEqual(old_main["kind"], "aside")
        self.assertFalse(old_main["is_main"])

    def test_agent_metadata_ask_branch_and_message_branch(self):
        sid = self.new_session()
        self.send_and_wait(sid, "main topic alpha")
        main_bid = self.c.call("sessions.subscribe", {"session_id": sid})["branch_id"]

        # Before any aside, /system has NO session branches section
        self.c.call("chat.send", {"session_id": sid, "text": "/system"})
        ev, _ = wait_event(self.c, lambda n, d: n == "notice" and "Effective system prompt" in d.get("text", ""))
        self.assertNotIn("Session branches", ev["data"]["text"])

        # Start an aside with a distinctive secret fact
        self.send_and_wait(sid, "/aside secret value is ZEBRA-42")
        aside_bid = self.c.call("sessions.subscribe", {"session_id": sid})["branch_id"]
        self.c.call("branches.rename", {"session_id": sid, "branch_id": aside_bid, "title": "zebra research"})

        # Switch back to main
        self.c.call("branches.switch", {"session_id": sid, "branch_id": main_bid})

        # Now /system shows metadata-only note (title & id, NOT the secret message text!)
        self.c.call("chat.send", {"session_id": sid, "text": "/system"})
        ev, _ = wait_event(self.c, lambda n, d: n == "notice" and "Effective system prompt" in d.get("text", ""))
        sys_text = ev["data"]["text"]
        self.assertIn("Session threads (metadata only)", sys_text)
        self.assertIn("zebra research", sys_text)
        self.assertIn(aside_bid, sys_text)
        self.assertNotIn("ZEBRA-42", sys_text)

        # 1. Unified `threads` tool returns metadata only (no ZEBRA-42 leak)
        self.send_and_wait(sid, '[[tool:threads:{}]]')
        msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
        tr = [b for m in msgs for b in m["content"] if b.get("type") == "tool_result"][-1]
        self.assertFalse(tr["is_error"], tr)
        self.assertIn("zebra research", tr["content"])
        self.assertNotIn("ZEBRA-42", tr["content"])

        # 2. Unified `ask` one-shot /btw question against aside's context (does NOT modify aside!)
        aside_msgs_before = next(b["own_messages"] for b in self.c.call("branches.list", {"session_id": sid})["branches"]
                                 if b["id"] == aside_bid)
        self.send_and_wait(sid, '[[tool:ask:{"target_id":"%s","question":"[[history]] what is the secret?"}]]'
                           % aside_bid)
        msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
        tr = [b for m in msgs for b in m["content"] if b.get("type") == "tool_result"][-1]
        self.assertFalse(tr["is_error"], tr)
        self.assertIn("zebra research", tr["content"])
        self.assertIn("History: 5 messages", tr["content"])  # 2 main + 2 aside + 1 question!
        aside_msgs_after = next(b["own_messages"] for b in self.c.call("branches.list", {"session_id": sid})["branches"]
                                if b["id"] == aside_bid)
        self.assertEqual(aside_msgs_before, aside_msgs_after)

        # 3. Unified `message` talks to aside like a subagent: runs a turn on aside & delivers <thread-message> back!
        self.c.call("chat.send", {"session_id": sid,
                                  "text": '[[tool:message:{"target_id":"%s","text":"check status please"}]]'
                                  % aside_bid})
        def got_thread_result():
            ms = self.c.call("sessions.subscribe", {"session_id": sid, "branch_id": main_bid})["messages"]
            has_br = any("<thread-message" in texts(m) and aside_bid in texts(m) and "check status please" in texts(m)
                         for m in ms if m["role"] == "user")
            idle = self.c.call("sessions.subscribe", {"session_id": sid, "branch_id": main_bid})["session"]["status"] == "idle"
            return has_br and idle and ms[-1]["role"] == "assistant"
        self.wait_for(got_thread_result)
        aside_msgs_final = next(b["own_messages"] for b in self.c.call("branches.list", {"session_id": sid})["branches"]
                                if b["id"] == aside_bid)
        self.assertEqual(aside_msgs_final, aside_msgs_before + 2)

    def test_per_tab_views_and_offline_snapshot(self):
        c2 = self.server.rpc()
        self.addCleanup(c2.close)
        c2.call("hello", {"client": "web"})
        sid = self.new_session()
        main_bid = self.c.call("branches.list", {"session_id": sid})["main_branch_id"]
        c2.call("sessions.subscribe", {"session_id": sid, "branch_id": main_bid})
        self.send_and_wait(sid, "hello world")
        # Client 1 starts an aside — client 1 switches to it, while client 2 STAYS on Main (per-tab!)
        self.send_and_wait(sid, "/aside parallel exploration")
        aside_bid = self.c.call("branches.list", {"session_id": sid})["active_branch_id"]
        self.assertNotEqual(aside_bid, main_bid)
        # Client 2 receives branches.updated with its OWN active_branch_id still == main_bid!
        ev, seen = wait_event(c2, lambda n, d: n == "branches.updated" and len(d["branches"]["branches"]) == 2)
        self.assertEqual(ev["data"]["branches"]["active_branch_id"], main_bid)
        self.assertFalse(any(e["event"] == "branch.switched" for e in seen))
        # Client 2 sends a message on Main while Client 1 is on Aside: both stay on their own branches!
        c2.call("chat.send", {"session_id": sid, "text": "staying on main"})
        self.wait_for(lambda: len(c2.call("sessions.subscribe", {"session_id": sid, "branch_id": main_bid})["messages"]) == 4
                      and self.c.call("sessions.subscribe", {"session_id": sid, "branch_id": aside_bid})["session"]["status"] == "idle")
        main_msgs = c2.call("sessions.subscribe", {"session_id": sid, "branch_id": main_bid})["messages"]
        aside_msgs = self.c.call("sessions.subscribe", {"session_id": sid, "branch_id": aside_bid})["messages"]
        self.assertEqual(texts(main_msgs[2]), "staying on main")
        self.assertEqual(texts(aside_msgs[2]), "parallel exploration")
        # Wait for debounced save and check offline snapshot carries all branches
        deadline = time.time() + 10
        snap = None
        while time.time() < deadline:
            raw = htmlfile.decode_offline(htmlfile.read(self.html))
            if raw:
                d = json.loads(raw)
                if d.get("branches", {}).get(sid):
                    snap = d["branches"][sid]
                    break
            time.sleep(0.1)
        self.assertIsNotNone(snap, "offline snapshot missing branches")
        self.assertEqual(len(snap["info"]["branches"]), 2)
        self.assertEqual(len(snap["threads"]), 2)
        self.assertIn(aside_bid, snap["threads"])


if __name__ == "__main__":
    unittest.main()
