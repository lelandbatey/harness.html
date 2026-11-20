"""End-to-end tests of the server: real process, mock provider, websocket + HTTP clients.

Run: python3 -m unittest test.test_server   (from the repo root)"""
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [os.path.dirname(os.path.abspath(__file__)), os.path.join(ROOT, "src", "py"), os.path.join(ROOT, "vendor")]

import fastenv  # noqa: E402,F401  (shorter delays in the servers tests start)
from sfh import common, htmlfile, pty_ws  # noqa: E402
from sfh.wsclient import RPC, WSClient  # noqa: E402

EMPTY_HTML = ('<!DOCTYPE html><html><head><meta name="sfh-token" content=""></head><body><p>test</p>\n' +
              htmlfile.DATA_BEGIN + htmlfile.DATA_OPEN + htmlfile.DATA_CLOSE + htmlfile.DATA_END +
              "\n</body></html>\n")
SECRET_ENV = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "AI_ENDPOINT", "AI_MODEL", "AI_TOKEN", "AI_CHEAP_MODEL")


def test_env(tmp, **extra):
    """Environment for a test server: isolated config dir, no real provider credentials."""
    env = dict(os.environ, XDG_CONFIG_HOME=os.path.join(tmp, "cfg"), SHELL="/bin/sh", SFH_MOCK="1",
               PYTHONPATH=os.pathsep.join([os.path.join(ROOT, "src", "py"), os.path.join(ROOT, "vendor")]))
    for k in SECRET_ENV:
        env.pop(k, None)
    env.update(extra)
    return env


class ServerProc:
    def __init__(self, html, env):
        self.html = html
        self.env = env
        self.proc = None
        self.port = None

    def start(self):
        if os.path.exists(os.path.join(ROOT, "src", "py", "sfh", "__main__.py")):
            cmd = [sys.executable, "-m", "sfh", self.html, "serve"]
        else:
            cmd = [sys.executable, "-c", "import sys; from sfh.server import serve; serve(sys.argv[1], [])", self.html]
        self.proc = subprocess.Popen(cmd, env=self.env, cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        deadline = time.time() + 15
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise RuntimeError("server exited: %s" % self.proc.stdout.read().decode())
            self.port = common.find_server(self.html, timeout=0.3)
            if self.port:
                return self
            time.sleep(0.1)
        raise RuntimeError("server did not start")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.send_signal(signal.SIGINT)
            try:
                self.proc.wait(10)
            except subprocess.TimeoutExpired:
                self.proc.kill()
                self.proc.wait()
        out = self.proc.stdout.read().decode() if self.proc else ""
        self.proc.stdout.close()
        return out

    @property
    def token(self):
        return htmlfile.read_token(self.html)

    def ws_url(self, endpoint="/ws", token=None, **query):
        q = dict(query, path=self.html, token=self.token if token is None else token)
        return "ws://127.0.0.1:%d%s?%s" % (self.port, endpoint, urllib.parse.urlencode(q))

    def rpc(self):
        return RPC(WSClient(self.ws_url()))

    def headers(self, **extra):
        return dict({"X-SFH-Path": self.html, "X-SFH-Token": self.token}, **extra)

    def url(self, endpoint, **query):
        q = dict(query)
        return "http://127.0.0.1:%d%s?%s" % (self.port, endpoint, urllib.parse.urlencode(q))


def http(url, method="GET", body=None, headers=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, dict(r.headers), r.read()
    except urllib.error.HTTPError as e:
        return e.code, dict(e.headers), e.read()


def wait_event(rpc, pred, timeout=10):
    """Polls events until pred(event_name, data) is true; returns (match, all events seen)."""
    seen = []
    deadline = time.time() + timeout
    while time.time() < deadline:
        batch = rpc.poll_events(0.1)
        for i, ev in enumerate(batch):
            seen.append(ev)
            if pred(ev["event"], ev["data"]):
                rpc.events.extendleft(reversed(batch[i + 1:]))  # later events stay queued for the next wait
                return ev, seen
    raise AssertionError("event not seen; got %s" % [e["event"] for e in seen])


def wait_count(rpc, sid, n, timeout=15):
    """Waits until the session is idle with at least n displayed messages."""
    return wait_event(rpc, lambda e, d: e == "session.updated" and d["id"] == sid and d["status"] == "idle"
                      and d["message_count"] >= n, timeout)


def wait_idle(rpc, sid, timeout=15):
    return wait_event(rpc, lambda n, d: n == "session.updated" and d["id"] == sid and d["status"] == "idle"
                      and d["message_count"] > 0, timeout)


class Base(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="sfh-test-")
        cls.html = os.path.join(cls.tmp, "harness.html")
        with open(cls.html, "w") as f:
            f.write(EMPTY_HTML)
        cls.workdir = os.path.join(cls.tmp, "work")
        os.makedirs(cls.workdir)
        cls.env = env = test_env(cls.tmp)
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

    def new_session(self, c=None):
        c = c or self.c
        s = c.call("sessions.create", {"cwd": self.workdir})
        c.call("sessions.subscribe", {"session_id": s["id"]})
        return s["id"]


class TestHTTP(Base):
    def test_health(self):
        st, _, body = http(self.server.url("/api/health", path=self.html))
        self.assertEqual(st, 200)
        info = json.loads(body)
        self.assertEqual(info["app"], "singlefile-llm-harness")
        self.assertEqual(info["path"], self.html)
        self.assertEqual(info["port"], self.server.port)
        self.assertLessEqual({"app", "path", "pid", "version", "port"}, set(info))
        # the file had no token: the server generated one and pairs for 5 minutes
        token = self.server.token
        self.assertGreaterEqual(len(token), 24)
        self.assertEqual(info["pairing_token"], token)
        st, _, body = http(self.server.url("/api/health"))  # no path -> no pairing token
        self.assertNotIn("pairing_token", json.loads(body))

    def test_forbidden(self):
        sql = {"sql": "SELECT 1"}
        h = self.server.headers
        st, _, _ = http(self.server.url("/api/sql"), "POST", sql, h(**{"X-SFH-Path": "/wrong.html"}))
        self.assertEqual(st, 403)
        st, _, _ = http(self.server.url("/api/sql"), "POST", sql, {"X-SFH-Token": self.server.token})
        self.assertEqual(st, 403)
        st, _, _ = http(self.server.url("/api/sql"), "POST", sql, h(Origin="http://evil.example"))
        self.assertEqual(st, 403)
        st, _, _ = http(self.server.url("/api/export", path=self.html, token=self.server.token),
                        headers={"Origin": "https://x.y"})
        self.assertEqual(st, 403)
        with self.assertRaises(ConnectionError):
            WSClient("ws://127.0.0.1:%d/ws?path=%s" % (self.server.port, urllib.parse.quote("/nope")))
        with self.assertRaises(ConnectionError):
            WSClient(self.server.ws_url(), headers={"Origin": "http://evil.example"})
        # missing / wrong token -> 401 everywhere except health
        st, _, _ = http(self.server.url("/api/sql"), "POST", sql, {"X-SFH-Path": self.html})
        self.assertEqual(st, 401)
        st, _, _ = http(self.server.url("/api/sql"), "POST", sql, h(**{"X-SFH-Token": "nope"}))
        self.assertEqual(st, 401)
        st, _, _ = http(self.server.url("/api/export", path=self.html))
        self.assertEqual(st, 401)
        for ep in ("/ws", "/pty"):
            with self.assertRaisesRegex(ConnectionError, "401"):
                WSClient(self.server.ws_url(ep, token="wrong"))
        # browser-style file:// origins are allowed
        st, _, _ = http(self.server.url("/api/sql"), "POST", sql, h(Origin="null"))
        self.assertEqual(st, 200)
        WSClient(self.server.ws_url(), headers={"Origin": "file://"}).close()

    def test_cors_preflight(self):
        st, h, _ = http(self.server.url("/api/sql"), "OPTIONS", headers={
            "Origin": "null", "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "content-type, x-sfh-path"})
        self.assertEqual(st, 204)
        h = {k.lower(): v for k, v in h.items()}
        self.assertEqual(h["access-control-allow-origin"], "null")
        self.assertIn("x-sfh-path", h["access-control-allow-headers"])
        self.assertIn("x-sfh-token", h["access-control-allow-headers"])
        self.assertIn("POST", h["access-control-allow-methods"])

    def test_sql(self):
        st, _, body = http(self.server.url("/api/sql"), "POST", {"sql": "SELECT ? + 1 AS n", "params": [41]},
                           self.server.headers())
        self.assertEqual(st, 200)
        self.assertEqual(json.loads(body), {"columns": ["n"], "rows": [[42]]})
        st, _, body = http(self.server.url("/api/sql"), "POST", {"sql": "DELETE FROM sessions"},
                           self.server.headers())
        self.assertEqual(st, 400)  # read-only without write: true
        st, _, _ = http(self.server.url("/api/sql"), "POST",
                        {"sql": "INSERT OR REPLACE INTO settings VALUES('editor', '\"vim\"')", "write": True},
                        self.server.headers())
        self.assertEqual(st, 200)
        self.assertEqual(self.c.call("settings.get")["editor"], "vim")
        self.c.call("settings.set", {"key": "editor", "value": "nvim"})

    def test_export(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "export me please"})
        wait_idle(self.c, sid)
        st, h, body = http(self.server.url("/api/export", path=self.html, view="session:" + sid,
                                           token=self.server.token))
        self.assertEqual(st, 200)
        self.assertIn("attachment", {k.lower(): v for k, v in h.items()}["content-disposition"])
        text = body.decode()
        self.assertIn("<p>test</p>", text)
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.deserialize(htmlfile.decode_data(text))
        self.assertEqual(json.loads(db.execute("SELECT value FROM settings WHERE key='ui.initial_view'")
                                    .fetchone()[0]), "session:" + sid)
        self.assertTrue(db.execute("SELECT COUNT(*) FROM messages WHERE session_id=?", (sid,)).fetchone()[0] >= 2)
        # the live file's setting is untouched
        self.assertIsNone(self.c.call("settings.get")["ui.initial_view"])
        # the copy has an empty token and a fresh offline snapshot; no credential warning
        self.assertEqual(htmlfile.decode_token(text), "")
        self.assertNotIn("x-sfh-warning", {k.lower() for k in h})
        off = json.loads(htmlfile.decode_offline(text))
        self.assertIn(sid, off["messages"])


class TestChat(Base):
    def test_hello(self):
        h = self.c.call("hello", {"client": "web"})
        self.assertEqual(h["path"], self.html)
        self.assertEqual(h["provider"], {"id": "mock", "type": "mock", "model": "mock", "cheap_model": "mock"})
        self.assertFalse(h["has_api_key"])
        self.assertIn("btw", [c["name"] for c in h["commands"]])
        self.assertIsNone(h["settings"]["model"])
        self.assertIsNone(h["settings"]["default_cwd"])
        provs = self.c.call("providers.list")
        self.assertEqual([p["id"] for p in provs], ["anthropic", "mock"])
        self.assertFalse(provs[0]["has_key"])
        self.assertIsInstance(h["sessions"], list)

    def test_send_stream_title(self):
        sid = self.new_session()
        r = self.c.call("chat.send", {"session_id": sid, "text": "How do python websockets handle auth"})
        self.assertEqual(r, {"queued": False})
        _, seen = wait_idle(self.c, sid)
        names = [e["event"] for e in seen]
        for n in ("message.added", "draft.start", "draft.block", "draft.delta", "draft.end"):
            self.assertIn(n, names)
        self.assertLess(names.index("draft.start"), names.index("draft.end"))
        assistant = [e["data"] for e in seen if e["event"] == "message.added" and e["data"]["role"] == "assistant"]
        self.assertEqual(len(assistant), 1)
        types = [b["type"] for b in assistant[0]["content"]]
        self.assertEqual(types, ["thinking", "text"])
        self.assertEqual(assistant[0]["content"][1]["text"],
                         "**Mock reply** to: How do python websockets handle auth")
        # message.added arrives just before draft.end
        self.assertEqual(names[names.index("draft.end") - 1], "message.added")
        titled = [e["data"] for e in seen if e["event"] == "session.updated" and e["data"]["title"]]
        if not titled:
            titled = [wait_event(self.c, lambda n, d: n == "session.updated" and d["title"])[0]["data"]]
        self.assertEqual(titled[-1]["title"], "How do python websockets handle auth")
        self.assertEqual(len(titled[-1]["subjects"]), 3)
        snap = self.c.call("sessions.subscribe", {"session_id": sid})
        self.assertEqual([m["role"] for m in snap["messages"]], ["user", "assistant"])
        self.assertIsNone(snap["draft"])

    def test_two_clients_live(self):
        sid = self.new_session()
        c2 = self.server.rpc()
        try:
            self.c.call("chat.send", {"session_id": sid, "text": "[[slow]] mirror this"})
            wait_event(self.c, lambda n, d: n == "draft.delta")
            snap = c2.call("sessions.subscribe", {"session_id": sid})  # late subscriber
            self.assertIsNotNone(snap["draft"])
            self.assertEqual(snap["session"]["status"], "running")
            _, seen2 = wait_event(c2, lambda n, d: n == "draft.end", 20)
            self.assertIn("draft.delta", [e["event"] for e in seen2])
            msg = [e["data"] for e in seen2 if e["event"] == "message.added"][-1]
            self.assertEqual(msg["content"][-1]["text"], "**Mock reply** to: [[slow]] mirror this")
        finally:
            c2.close()

    def test_bash_tool_loop(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "run [[bash:echo hi-from-tool && pwd]]"})
        wait_idle(self.c, sid)
        msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "user", "assistant"])
        tu = msgs[1]["content"][-1]
        self.assertEqual(tu["type"], "tool_use")
        self.assertEqual(tu["input"], {"command": "echo hi-from-tool && pwd"})
        tr = msgs[2]["content"][0]
        self.assertEqual(tr["tool_use_id"], tu["id"])
        self.assertIn("hi-from-tool", tr["content"])
        self.assertIn(self.workdir, tr["content"])
        self.assertTrue(msgs[3]["content"][-1]["text"].startswith("Tool said: hi-from-tool"))

    def test_stop_and_queue(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "[[slow]] first"})
        r = self.c.call("chat.send", {"session_id": sid, "text": "second"})
        self.assertTrue(r["queued"])
        wait_event(self.c, lambda n, d: n == "queue.updated" and d["queue"] == ["second"])
        wait_event(self.c, lambda n, d: n == "draft.delta")
        t0 = time.time()
        self.c.call("chat.stop", {"session_id": sid})
        self.assertLess(time.time() - t0, 3)
        wait_idle(self.c, sid)
        msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
        # queue cleared by stop; partial assistant output kept
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant"])
        self.assertLess(len(json.dumps(msgs[1]["content"])), 400)

    def test_queue_runs_after_turn(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "one"})
        self.assertTrue(self.c.call("chat.send", {"session_id": sid, "text": "two"})["queued"])
        deadline = time.time() + 15
        while time.time() < deadline:
            msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
            if len(msgs) == 4:
                break
            time.sleep(0.2)
        self.assertEqual([m["content"][-1]["text"] for m in msgs],
                         ["one", "**Mock reply** to: one", "two", "**Mock reply** to: two"])

    def test_edit(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "original"})
        wait_count(self.c, sid, 2)
        self.c.call("chat.send", {"session_id": sid, "text": "second"})
        wait_count(self.c, sid, 4)
        msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
        self.assertEqual(msgs[0]["parent_id"], None)
        self.assertEqual(msgs[1]["parent_id"], msgs[0]["id"])
        self.assertEqual(msgs[0]["versions"], 1)
        edit_id = msgs[2]["id"]
        self.c.call("chat.edit", {"session_id": sid, "message_id": edit_id, "text": "edited"})
        _, seen = wait_event(self.c, lambda n, d: n == "message.added" and d["role"] == "assistant")
        names = [e["event"] for e in seen]
        trunc = seen[names.index("messages.truncated")]
        self.assertEqual(trunc["data"], {"session_id": sid, "from_id": edit_id})
        added = [e for e in seen if e["event"] == "message.added" and e["data"]["role"] == "user"][0]
        self.assertLess(names.index("messages.truncated"), seen.index(added))
        self.assertEqual(added["data"]["versions"], 2)
        self.assertEqual(added["data"]["parent_id"], msgs[1]["id"])
        wait_idle(self.c, sid)
        msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
        self.assertEqual([m["content"][-1]["text"] for m in msgs],
                         ["original", "**Mock reply** to: original", "edited", "**Mock reply** to: edited"])
        self.assertEqual(msgs[2]["versions"], 2)
        # the old branch is kept in the db, but neither displayed nor counted
        cols, rows = self.c_sql("SELECT COUNT(*) FROM messages WHERE session_id=?", [sid])
        self.assertEqual(rows[0][0], 6)
        self.assertEqual(self.c.call("sessions.list")[0]["message_count"], 4)
        # editing the first message starts a new root branch
        self.c.call("chat.edit", {"session_id": sid, "message_id": msgs[0]["id"], "text": "root2"})
        wait_event(self.c, lambda n, d: n == "message.added" and d["role"] == "assistant")
        wait_idle(self.c, sid)
        msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
        self.assertEqual([m["content"][-1]["text"] for m in msgs], ["root2", "**Mock reply** to: root2"])
        self.assertEqual((msgs[0]["parent_id"], msgs[0]["versions"]), (None, 2))
        with self.assertRaises(Exception):  # not on the displayed branch any more
            self.c.call("chat.edit", {"session_id": sid, "message_id": edit_id, "text": "x"})

    def c_sql(self, sql, params=()):
        st, _, body = http(self.server.url("/api/sql"), "POST", {"sql": sql, "params": list(params)},
                           self.server.headers())
        self.assertEqual(st, 200, body)
        r = json.loads(body)
        return r["columns"], r["rows"]

    def test_btw_not_persisted(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "context"})
        wait_idle(self.c, sid)
        self.c.call("chat.send", {"session_id": sid, "text": "/btw what was that?"})
        ev, seen = wait_event(self.c, lambda n, d: n == "btw.end")
        self.assertNotIn("error", ev["data"])
        start = [e for e in seen if e["event"] == "btw.start"][0]["data"]
        self.assertEqual(start["question"], "what was that?")
        text = "".join(e["data"]["text"] for e in seen if e["event"] == "btw.delta")
        self.assertEqual(text, "**Mock reply** to: what was that?")
        msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
        self.assertEqual(len(msgs), 2)

    def test_commands(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "/title My Title"})
        wait_event(self.c, lambda n, d: n == "session.updated" and d["title"] == "My Title")
        self.c.call("chat.send", {"session_id": sid, "text": "/help"})
        ev, _ = wait_event(self.c, lambda n, d: n == "notice")
        self.assertIn("/retitle", ev["data"]["text"])
        self.c.call("chat.send", {"session_id": sid, "text": "/system"})
        ev, _ = wait_event(self.c, lambda n, d: n == "notice")
        self.assertIn("singlefile-harness", ev["data"]["text"])
        self.assertIn(self.workdir, ev["data"]["text"])
        skills = self.c.call("skills.list", {"session_id": sid})
        self.assertIn("singlefile-harness", [s["name"] for s in skills])

    def test_creds(self):
        info = self.c.call("creds.set", {"name": "TEST_TOKEN", "value": "sekrit-value"})
        self.assertEqual(info["storage"], "xdg")
        self.assertIn("TEST_TOKEN", info["names"])
        self.assertNotIn("sekrit-value", json.dumps(info))
        path = os.path.join(self.tmp, "cfg", "singlefile", "credentials")
        self.assertEqual(info["path"], path)
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        with open(path) as f:
            self.assertIn("TEST_TOKEN=sekrit-value", f.read())
        h = self.c.call("hello", {"client": "web"})
        self.assertNotIn("sekrit-value", json.dumps(h))
        # xdg credentials never travel with an export
        st, _, body = http(self.server.url("/api/export", path=self.html, token=self.server.token))
        self.assertEqual(st, 200)
        self.assertNotIn(b"sekrit-value", body)
        # embedded mode: moves into the db; export includes it, with a warning header
        info = self.c.call("creds.storage", {"mode": "embedded"})
        self.assertEqual(info["storage"], "embedded")
        self.assertIn("TEST_TOKEN", info["names"])
        st, h, body = http(self.server.url("/api/export", path=self.html, token=self.server.token))
        self.assertEqual({k.lower(): v for k, v in h.items()}["x-sfh-warning"],
                         "export contains embedded credentials")
        import sqlite3
        db = sqlite3.connect(":memory:")
        db.deserialize(htmlfile.decode_data(body.decode()))
        self.assertEqual(db.execute("SELECT value FROM credentials WHERE name='TEST_TOKEN'").fetchone()[0],
                         "sekrit-value")
        self.c.call("creds.storage", {"mode": "xdg"})
        self.c.call("creds.set", {"name": "TEST_TOKEN", "value": ""})

    def test_pty_shell(self):
        ws = WSClient(self.server.ws_url("/pty", cmd="shell", cwd=self.workdir, cols=80, rows=24))
        try:
            ws.send_json({"type": "input", "data": "echo hi-$((40+2))\n"})
            out = ""
            deadline = time.time() + 10
            while "hi-42" not in out and time.time() < deadline:
                m = ws.recv_json(0.5)
                if m and m["type"] == "output":
                    out += m["data"]
            self.assertIn("hi-42", out)
            ws.send_json({"type": "input", "data": "exit\n"})
            deadline = time.time() + 10
            while time.time() < deadline:
                m = ws.recv_json(0.5)
                if m and m["type"] == "exit":
                    break
            else:
                self.fail("no exit frame")
        finally:
            ws.close()


class TestCwdModelRetry(Base):
    """Session cwd defaults/changes, per-message cwd/model/links, model switching, provider retries."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.other = os.path.join(cls.tmp, "other")
        os.makedirs(os.path.join(cls.other, "sub"))
        with open(os.path.join(cls.other, "notes.md"), "w") as f:
            f.write("hi\n")

    def test_1_default_cwd(self):
        info = self.c.call("cwd.info")
        self.assertEqual(info["default"], ROOT)          # nothing started yet: where the server runs
        self.assertEqual(info["server_cwd"], ROOT)
        self.c.call("settings.set", {"key": "default_cwd", "value": self.other})
        try:
            self.assertEqual(self.c.call("cwd.info")["default"], self.other)   # the fallback setting
        finally:
            self.c.call("settings.set", {"key": "default_cwd", "value": None})
        self.c.call("sessions.create", {"cwd": self.workdir})
        self.assertEqual(self.c.call("sessions.create", {})["cwd"], self.workdir)  # last new-session cwd
        self.c.call("sessions.create", {"cwd": self.other})
        info = self.c.call("cwd.info")
        self.assertEqual(info["default"], self.other)
        self.assertEqual([r["path"] for r in info["recent"]][:2], [self.other, self.workdir])  # most recent first

    def test_fs_list(self):
        r = self.c.call("fs.list", {"path": self.other})
        self.assertEqual((r["path"], r["dirs"], r["parent"]), (self.other, ["sub"], self.tmp))
        r = self.c.call("fs.list", {"path": os.path.join(self.other, "notes.md")})   # a file: its directory
        self.assertEqual(r["path"], self.other)
        self.assertFalse(self.c.call("fs.list", {"path": os.path.join(self.tmp, "nope")})["exists"])

    def test_set_cwd_and_message_cwd_and_links(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "look at `notes.md` please"})
        wait_idle(self.c, sid)
        s = self.c.call("sessions.set_cwd", {"session_id": sid, "cwd": "../other"})  # relative to the old cwd
        self.assertEqual(s["cwd"], self.other)
        with self.assertRaises(Exception):
            self.c.call("sessions.set_cwd", {"session_id": sid, "cwd": "/definitely/not/here"})
        _, seen = wait_event(self.c, lambda e, d: e == "notice" and "Working directory changed" in d["text"])
        self.c.call("chat.send", {"session_id": sid, "text": "look at `notes.md` again"})
        wait_count(self.c, sid, 4)
        msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
        self.assertEqual([m["cwd"] for m in msgs], [self.workdir, self.workdir, self.other, self.other])
        # the mock reply quotes the prompt: `notes.md` only existed in the second message's cwd
        self.assertEqual(msgs[1]["links"], {})
        self.assertEqual(msgs[3]["links"], {"notes.md": {"path": os.path.join(self.other, "notes.md"), "dir": False}})
        self.c.call("chat.send", {"session_id": sid, "text": "/cd sub"})
        self.assertEqual(self.c.call("sessions.subscribe", {"session_id": sid})["session"]["cwd"],
                         os.path.join(self.other, "sub"))

    def test_model_switch_is_recorded_per_message(self):
        sid = self.new_session()
        s = self.c.call("sessions.subscribe", {"session_id": sid})["session"]
        self.assertEqual((s["provider"], s["model"], s["provider_override"]), ("mock", "mock", None))
        self.c.call("chat.send", {"session_id": sid, "text": "one"})
        wait_idle(self.c, sid)
        self.c.call("settings.set", {"key": "providers", "value": [
            {"id": "alt", "type": "mock"}]})
        try:
            s = self.c.call("sessions.set_model", {"session_id": sid, "provider": "alt"})
            self.assertEqual((s["provider"], s["provider_override"]), ("alt", "alt"))
            with self.assertRaises(Exception):
                self.c.call("sessions.set_model", {"session_id": sid, "provider": "nope"})
            self.c.call("chat.send", {"session_id": sid, "text": "two"})
            wait_count(self.c, sid, 4)
            msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
            self.assertEqual([m["provider"] for m in msgs], ["mock", "mock", "alt", "alt"])
            self.assertTrue(all(m["model"] == "mock" for m in msgs))
            r = self.c.call("models.list", {"session_id": sid})
            self.assertEqual((r["provider"], r["models"]), ("alt", [{"id": "mock", "context_window": 200000, "efforts": ["low", "medium", "high", "max"]}]))
            # back to the default: with provider "auto" the first configured provider is the default
            s = self.c.call("sessions.set_model", {"session_id": sid, "provider": None})
            self.assertEqual((s["provider"], s["provider_override"]), ("alt", None))
        finally:
            self.c.call("settings.set", {"key": "providers", "value": []})

    def test_retry_then_success(self):
        self.c.call("settings.set", {"key": "retry_base_delay", "value": 0.01})
        try:
            sid = self.new_session()
            self.c.call("chat.send", {"session_id": sid, "text": "[[flaky:2]] hello"})
            _, seen = wait_idle(self.c, sid)
            notices = [e["data"]["text"] for e in seen if e["event"] == "notice"]
            self.assertEqual(sum(1 for n in notices if "retrying" in n), 2, notices)
            msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
            self.assertEqual(len(msgs), 2)            # failed attempts left no partial messages behind
            self.assertIn("Mock reply", msgs[1]["content"][-1]["text"])
            sid = self.new_session()
            self.c.call("chat.send", {"session_id": sid, "text": "[[flaky:9]] hello"})
            _, seen = wait_idle(self.c, sid)
            errors = [e["data"]["text"] for e in seen if e["event"] == "notice" and e["data"]["level"] == "error"]
            self.assertTrue(errors and "gave up after 4 attempts" in errors[0], errors)
        finally:
            self.c.call("settings.set", {"key": "retry_base_delay", "value": 2.0})


class TestEffortAndDurations(Base):
    """Per-session effort levels (recorded per message, refused levels dropped) and timing of blocks/tools."""

    def test_effort_is_sent_and_recorded_per_message(self):
        sid = self.new_session()
        s = self.c.call("sessions.subscribe", {"session_id": sid})["session"]
        self.assertIsNone(s["effort"])
        self.assertIn("high", s["efforts"])
        with self.assertRaises(Exception):
            self.c.call("sessions.set_model", {"session_id": sid, "effort": "turbo"})
        self.c.call("chat.send", {"session_id": sid, "text": "one"})
        wait_count(self.c, sid, 2)
        s = self.c.call("sessions.set_model", {"session_id": sid, "effort": "high"})
        self.assertEqual((s["effort"], s["effort_override"], s["model"]), ("high", "high", "mock"))
        self.c.call("chat.send", {"session_id": sid, "text": "two"})
        wait_count(self.c, sid, 4)
        msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
        self.assertEqual([m.get("effort") for m in msgs], [None, None, None, "high"])
        self.assertIn("(effort high)", msgs[3]["content"][0]["thinking"])
        # the mock model lists low/medium/high/max: a level it doesn't take is clamped to the next lower one
        s = self.c.call("sessions.set_model", {"session_id": sid, "effort": "xhigh"})
        self.assertEqual((s["effort"], s["effort_wanted"], s["efforts_known"]), ("high", "xhigh", True))
        self.assertEqual(s["efforts"], ["low", "medium", "high", "max"])
        s = self.c.call("sessions.set_model", {"session_id": sid, "effort": None})
        self.assertIsNone(s["effort_override"])
        # the global default applies to sessions without their own
        self.c.call("settings.set", {"key": "effort", "value": "low"})
        try:
            self.assertEqual(self.c.call("sessions.subscribe", {"session_id": sid})["session"]["effort"], "low")
        finally:
            self.c.call("settings.set", {"key": "effort", "value": None})

    def test_refused_effort_is_dropped(self):
        self.c.call("settings.set", {"key": "providers", "value": [{"id": "noeff", "type": "mock"}]})
        try:
            sid = self.new_session()
            self.c.call("sessions.set_model", {"session_id": sid, "provider": "noeff", "effort": "medium"})
            self.c.call("chat.send", {"session_id": sid, "text": "[[noeffort]] hello"})
            s, seen = wait_count(self.c, sid, 2)
            notices = [e["data"]["text"] for e in seen if e["event"] == "notice"]
            self.assertTrue(any("refused the effort level" in n for n in notices), notices)
            self.assertFalse([e for e in seen if e["event"] == "notice" and e["data"]["level"] == "error"])
            msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
            self.assertIsNone(msgs[1].get("effort"))
            self.assertIn("Mock reply", msgs[1]["content"][-1]["text"])
            self.assertEqual((s["data"]["effort"], s["data"]["efforts"], s["data"]["effort_wanted"]),
                             (None, [], "medium"))
        finally:
            self.c.call("settings.set", {"key": "providers", "value": []})

    def test_durations(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "[[slow]] [[bash:sleep 0.3; echo done]]"})
        _, seen = wait_count(self.c, sid, 4)
        names = [e["event"] for e in seen]
        start = next(e["data"] for e in seen if e["event"] == "tool.start")
        end = next(e["data"] for e in seen if e["event"] == "tool.end")
        self.assertLess(names.index("tool.start"), names.index("tool.end"))
        self.assertEqual(start["tool_use_id"], end["tool_use_id"])
        self.assertGreaterEqual(end["ended_at"] - end["started_at"], 300)
        msgs = self.c.call("sessions.subscribe", {"session_id": sid})["messages"]
        first = msgs[1]
        self.assertEqual(first["role"], "assistant")
        self.assertLessEqual(first["started_at"], first["created_at"])
        think = first["content"][0]
        self.assertEqual(think["type"], "thinking")
        self.assertLess(think["started_at"], think["ended_at"])   # several deltas, apart ([[slow]])
        result = msgs[2]["content"][0]
        self.assertEqual(result["type"], "tool_result")
        self.assertGreaterEqual(result["ended_at"] - result["started_at"], 300)
        text = msgs[3]["content"][-1]
        self.assertEqual(text["type"], "text")
        self.assertLessEqual(text["started_at"], text["ended_at"])


class TestUsageAndSubjects(Base):
    def test_cost_estimated_from_provider_pricing(self):
        """A provider that reports usage but no cost: the cost comes from its /settings/api/pricing."""
        import http.server
        import threading
        hits = []

        class H(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                hits.append(self.path)
                if self.path != "/settings/api/pricing":
                    self.send_response(404)
                    self.end_headers()
                    return
                body = json.dumps({"builtin": {"priced-model": {"inputPerMTok": 2, "outputPerMTok": 10}},
                                   "overrides": {"priced-model": {"outputPerMTok": 20}}}).encode()
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(body)

            def do_POST(self):
                req = json.loads(self.rfile.read(int(self.headers["content-length"])))
                self.send_response(200)
                if not req.get("stream"):  # title generation
                    self.send_header("content-type", "application/json")
                    self.end_headers()
                    self.wfile.write(json.dumps({"choices": [{"message": {"content": "{}"}}],
                                                 "usage": {"prompt_tokens": 100, "completion_tokens": 10}}).encode())
                    return
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for c in ({"choices": [{"index": 0, "delta": {"content": "hi"}, "finish_reason": "stop"}]},
                          {"choices": [], "usage": {"prompt_tokens": 1000, "completion_tokens": 500}}):
                    self.wfile.write(("data: %s\n\n" % json.dumps(c)).encode())
                self.wfile.write(b"data: [DONE]\n\n")

            def log_message(self, *a):
                pass

        httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        httpd.daemon_threads = True
        threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
        self.c.call("settings.set", {"key": "providers", "value": [
            {"id": "priced", "type": "openai", "base_url": "http://127.0.0.1:%d/v1" % httpd.server_port,
             "model": "vendor/priced-model"}]})
        try:
            sid = self.new_session()
            self.c.call("sessions.set_model", {"session_id": sid, "provider": "priced"})
            self.c.call("chat.send", {"session_id": sid, "text": "hello"})
            wait_count(self.c, sid, 2)
            snap = self.c.call("sessions.subscribe", {"session_id": sid})
            u = [m for m in snap["messages"] if m["role"] == "assistant"][0]["usage"]
            self.assertAlmostEqual(u["cost"], (1000 * 2 + 500 * 20) / 1e6)
            self.assertTrue(u["cost_estimated"])
            total = snap["session"]["usage"]
            self.assertGreaterEqual(total["estimated_calls"], 1)
            self.assertEqual(total["estimated_calls"], total["priced_calls"])
            self.assertIn("/settings/api/pricing", hits)
        finally:
            self.c.call("settings.set", {"key": "providers", "value": []})
            httpd.shutdown()

    def test_cost_is_tracked_per_response_and_session(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "[[bash:echo hi]]"})
        _, seen = wait_count(self.c, sid, 4)
        snap = self.c.call("sessions.subscribe", {"session_id": sid})
        assistant = [m for m in snap["messages"] if m["role"] == "assistant"]
        self.assertEqual(len(assistant), 2)
        self.assertTrue(all(m["usage"]["cost"] > 0 and m["usage"]["output_tokens"] > 0 for m in assistant))
        u = snap["session"]["usage"]
        self.assertEqual((u["calls"], u["priced_calls"]), (2, 2))
        self.assertAlmostEqual(u["cost"], sum(m["usage"]["cost"] for m in assistant))
        self.assertTrue(any(e["event"] == "session.updated" and (e["data"].get("usage") or {}).get("calls") == 1
                            for e in seen))   # live update after the first response

    def test_subjects_follow_recent_sessions_in_the_same_directory(self):
        proj = os.path.join(self.tmp, "proj")
        os.makedirs(proj, exist_ok=True)
        a = self.c.call("sessions.create", {"cwd": proj})["id"]
        self.c.call("sessions.subscribe", {"session_id": a})
        self.c.call("chat.send", {"session_id": a, "text": "refactor the websocket reconnection logic"})
        wait_event(self.c, lambda e, d: e == "session.updated" and d["id"] == a and d["subjects"])
        sa = self.c.call("sessions.subscribe", {"session_id": a})["session"]["subjects"]
        b = self.c.call("sessions.create", {"cwd": proj})["id"]
        self.c.call("sessions.subscribe", {"session_id": b})
        self.c.call("chat.send", {"session_id": b, "text": "write documentation for configuration options"})
        wait_event(self.c, lambda e, d: e == "session.updated" and d["id"] == b and d["subjects"])
        sb = self.c.call("sessions.subscribe", {"session_id": b})["session"]["subjects"]
        self.assertEqual(sb[:2], sa[:2])      # same project: same domain + topic
        self.assertNotEqual(sb, sa)

    def test_compact(self):
        sid = self.new_session()
        ctx = self.c.call("sessions.subscribe", {"session_id": sid})["session"]["context"]
        self.assertIsNone(ctx["used"])               # empty session: only the window is known
        with self.assertRaises(Exception):          # nothing to compact yet
            self.c.call("chat.send", {"session_id": sid, "text": "/compact"})
        for t in ("first question", "second question"):
            self.c.call("chat.send", {"session_id": sid, "text": t})
            wait_count(self.c, sid, 4 if t.startswith("second") else 2)
        s = self.c.call("sessions.subscribe", {"session_id": sid})["session"]
        self.assertEqual(s["context"]["window"], 200000)   # from the (mock) provider's model list
        self.assertFalse(s["context"]["estimated"])
        self.assertGreater(s["context"]["used"], 0)
        self.c.call("chat.send", {"session_id": sid, "text": "/compact keep it short"})
        _, seen = wait_event(self.c, lambda e, d: e == "notice" and "Compacted 4 messages" in d["text"])
        self.assertIn("draft.start", [e["event"] for e in seen])      # the summary streamed live
        snap = self.c.call("sessions.subscribe", {"session_id": sid})
        last = snap["messages"][-1]
        self.assertEqual((last["role"], last["content"][0]["compaction"], last["content"][0]["compacted"]),
                         ("user", True, 4))
        self.assertIn("Additional instructions from the user: keep it short", last["content"][0]["text"])
        self.assertTrue(snap["session"]["context"]["estimated"])
        # the model now sees: the summary, then the new prompt
        self.c.call("chat.send", {"session_id": sid, "text": "[[history]] what now"})
        wait_count(self.c, sid, 7)
        reply = self.c.call("sessions.subscribe", {"session_id": sid})["messages"][-1]["content"][-1]["text"]
        self.assertTrue(reply.startswith("History: 2 messages; first: <conversation-summary>"), reply)
        self.assertEqual(self.c.call("sessions.subscribe", {"session_id": sid})["session"]["message_count"], 7)

    def test_context_window_override(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "hello"})
        wait_count(self.c, sid, 2)
        ctx = self.c.call("sessions.subscribe", {"session_id": sid})["session"]["context"]
        self.assertEqual(ctx["window"], 200000)
        self.assertNotIn("window_override", ctx)
        try:
            self.c.call("settings.set", {"key": "model_overrides", "value": {"mock": {"mock": {"context_window": 50000}}}})
            ev, _ = wait_event(self.c, lambda e, d: e == "session.updated" and d["id"] == sid
                               and d["context"]["window"] == 50000)
            self.assertTrue(ev["data"]["context"]["window_override"])
            r = self.c.call("models.list", {"provider": "mock"})
            self.assertEqual(r["models"], [{"id": "mock", "context_window": 200000, "efforts": ["low", "medium", "high", "max"], "context_window_override": 50000}])
            self.c.call("chat.send", {"session_id": sid, "text": "/models"})
            ev, _ = wait_event(self.c, lambda e, d: e == "notice" and "Models of" in d["text"])
            self.assertIn("50k context (override; provider: 200k)", ev["data"]["text"])
        finally:
            self.c.call("settings.set", {"key": "model_overrides", "value": {}})
        self.assertEqual(self.c.call("sessions.subscribe", {"session_id": sid})["session"]["context"]["window"], 200000)

    def test_context_estimated_without_usage(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "hello " * 20})
        wait_count(self.c, sid, 2)
        self.c_sql_write("UPDATE messages SET usage=NULL WHERE session_id=?", [sid])  # as if recorded before v5
        ctx = self.c.call("sessions.subscribe", {"session_id": sid})["session"]["context"]
        self.assertTrue(ctx["estimated"])
        self.assertGreater(ctx["used"], 2 * 120 // 4)  # prompt + mock reply (which repeats it)

    def c_sql_write(self, sql, params):
        st, _, body = http(self.server.url("/api/sql"), "POST", {"sql": sql, "params": params, "write": True},
                           self.server.headers())
        self.assertEqual(st, 200, body)

    def test_title_regenerated_after_the_first_exchange(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "[[slow]] tune the database indexes"})
        wait_event(self.c, lambda e, d: e == "session.updated" and d["id"] == sid and d["title"])
        self.c.call("sessions.rename", {"session_id": sid, "title": "manual"})   # while the first turn runs
        wait_event(self.c, lambda e, d: e == "session.updated" and d["id"] == sid and d["status"] == "idle"
                   and d["message_count"] >= 2, 30)
        ev, _ = wait_event(self.c, lambda e, d: e == "session.updated" and d["id"] == sid and d["title"] != "manual")
        self.assertIn("tune the database indexes", ev["data"]["title"])
        # later exchanges do not retitle automatically
        self.c.call("sessions.rename", {"session_id": sid, "title": "manual 2"})
        self.c.call("chat.send", {"session_id": sid, "text": "second"})
        wait_count(self.c, sid, 4)
        time.sleep(0.3)
        self.assertEqual(self.c.call("sessions.subscribe", {"session_id": sid})["session"]["title"], "manual 2")

    def test_models_filter(self):
        sid = self.new_session()
        self.c.call("chat.send", {"session_id": sid, "text": "/models moc"})
        ev, _ = wait_event(self.c, lambda e, d: e == "notice" and "Models of" in d["text"])
        self.assertIn("matching `moc`", ev["data"]["text"])
        self.assertIn("`mock`", ev["data"]["text"])
        self.c.call("chat.send", {"session_id": sid, "text": "/models zzz"})
        ev, _ = wait_event(self.c, lambda e, d: e == "notice" and "Models of" in d["text"])
        self.assertIn("(none)", ev["data"]["text"])

    def test_subjects_prompt_has_context(self):
        from sfh import agent
        ctx = [{"title": "Fix login", "cwd": "/p", "subjects": ["software development", "acme app", "login bug"],
                "same_cwd": True}]
        p = agent.subjects_prompt(3, "user: hi", "/p", ctx)
        self.assertIn("[domain, topic, specific]", p)
        self.assertIn("Fix login | /p | - | software development > acme app > login bug", p)
        self.assertIn("reuse that domain EXACTLY", p)

    def test_subjects_follow_the_project_not_the_cwd(self):
        """Sessions all started in one root folder: what they touched (stored as sessions.signals) decides
        whose domain/topic is reused (here by the no-LLM fallback), not the shared cwd."""
        root = tempfile.mkdtemp(dir=self.tmp, prefix="root-")
        for p in ("alpha/.git", "beta/.git"):
            os.makedirs(os.path.join(root, p))

        def run(text):
            s = self.c.call("sessions.create", {"cwd": root})
            self.c.call("sessions.subscribe", {"session_id": s["id"]})
            self.c.call("chat.send", {"session_id": s["id"], "text": text})
            wait_count(self.c, s["id"], 4)  # prompt, tool call, result, reply
            deadline = time.time() + 10
            while time.time() < deadline:  # the retitle after the first exchange stored its signals
                st, _, body = http(self.server.url("/api/sql"), "POST", {
                    "sql": "SELECT subjects, signals FROM sessions WHERE id=?", "params": [s["id"]]},
                    self.server.headers())
                row = json.loads(body)["rows"][0]
                if row[1]:
                    return json.loads(row[0]), json.loads(row[1])
                time.sleep(0.1)
            self.fail("no signals stored")

        a, sig = run('zebra quokka [[tool:read:{"path": "alpha/notes.md"}]]')
        self.assertEqual(sig["projects"], [os.path.join(root, "alpha")])
        b, _ = run('walrus narwhal [[tool:read:{"path": "beta/notes.md"}]]')
        self.assertNotEqual(b[:2], a[:2])  # same cwd, other project: own subjects
        c, _ = run('pelican [[tool:read:{"path": "alpha/other.md"}]]')
        self.assertEqual(c[:2], a[:2])  # same project as a: its domain + topic


class TestEditorResolve(unittest.TestCase):
    def resolve(self, editor, env, have):
        return pty_ws.resolve_editor(editor, env, which=lambda p, path=None: p in have)

    def test_order(self):
        allp = {"code", "vim", "nano", "pico", "vi"}
        self.assertEqual(self.resolve("code -w", {"EDITOR": "vim"}, allp), ["code", "-w"])
        self.assertEqual(self.resolve("", {"EDITOR": "vim"}, allp), ["vim"])
        self.assertEqual(self.resolve(None, {}, allp), ["nano"])
        self.assertEqual(self.resolve(None, {}, {"pico", "vi"}), ["pico"])
        self.assertEqual(self.resolve(None, {}, {"vi"}), ["vi"])

    def test_missing_falls_through(self):
        self.assertEqual(self.resolve("nvim", {"EDITOR": "hx"}, {"vi"}), ["vi"])

    def test_none_found(self):
        with self.assertRaises(pty_ws.EditorNotFound) as cm:
            self.resolve("nvim", {"EDITOR": "vim"}, set())
        self.assertIn("nvim, vim, nano, pico, vi", str(cm.exception))

    def test_server_reports_missing_editor(self):
        tmp = tempfile.mkdtemp(prefix="sfh-test-")
        try:
            html = os.path.join(tmp, "harness.html")
            with open(html, "w") as f:
                f.write(EMPTY_HTML)
            env = dict(test_env(tmp), PATH=os.path.join(tmp, "empty-bin"))
            env.pop("EDITOR", None)
            server = ServerProc(html, env).start()
            try:
                ws = WSClient(server.ws_url("/pty", cmd="editor", file="x.txt", cwd=tmp))
                try:
                    frames, deadline = [], time.time() + 10
                    while time.time() < deadline and not any(f["type"] == "exit" for f in frames):
                        m = ws.recv_json(0.5)
                        if m:
                            frames.append(m)
                finally:
                    ws.close()
            finally:
                server.stop()
            out = "".join(f["data"] for f in frames if f["type"] == "output")
            self.assertIn("no editor found on PATH (tried: nano, pico, vi)", out)
            self.assertEqual([f["code"] for f in frames if f["type"] == "exit"], [127])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_build_argv(self):
        argv = pty_ws.build_argv("editor", "sh -c true", "f.txt", 3)
        self.assertEqual(argv, ["sh", "-c", "true", "+3", "f.txt"])


class TestPersistence(unittest.TestCase):
    def test_restart_keeps_data(self):
        tmp = tempfile.mkdtemp(prefix="sfh-test-")
        try:
            html = os.path.join(tmp, "p.html")
            with open(html, "w") as f:
                f.write(EMPTY_HTML)
            os.chmod(html, 0o755)
            env = test_env(tmp)
            srv = ServerProc(html, env).start()
            # a second server for the same file refuses to start
            p2 = subprocess.run([sys.executable, "-c", "import sys; from sfh.server import serve; "
                                 "serve(sys.argv[1], [])", html], env=env, cwd=ROOT, capture_output=True, timeout=10)
            self.assertEqual(p2.returncode, 1)
            self.assertIn(b"already running", p2.stdout)
            c = srv.rpc()
            s = c.call("sessions.create", {"cwd": tmp})
            c.call("sessions.subscribe", {"session_id": s["id"]})
            c.call("chat.send", {"session_id": s["id"], "text": "remember me"})
            wait_idle(c, s["id"])
            c.call("settings.set", {"key": "thinking_budget", "value": 0})
            c.close()
            deadline = time.time() + 5   # debounced save
            while not htmlfile.read_data(html) and time.time() < deadline:
                time.sleep(0.05)
            self.assertTrue(htmlfile.read_data(html))
            out = srv.stop()
            self.assertIn("file://" + html, out)
            self.assertEqual(stat.S_IMODE(os.stat(html).st_mode), 0o755)
            with open(html) as f:
                src = f.read()
            self.assertTrue(src.startswith('<!DOCTYPE html><html><head><meta name="sfh-token" content="'))
            self.assertIn("<p>test</p>", src)
            token = htmlfile.decode_token(src)
            off = json.loads(htmlfile.decode_offline(src))
            self.assertEqual([x["id"] for x in off["sessions"]], [s["id"]])
            self.assertEqual(off["messages"][s["id"]][0]["content"][0]["text"], "remember me")
            self.assertIn("ui.initial_view", off["settings"])
            self.assertNotIn("system_prompt", off["settings"])
            srv = ServerProc(html, env).start()
            self.assertEqual(srv.token, token)  # kept across restarts, no pairing window
            st, _, body = http(srv.url("/api/health", path=html))
            self.assertNotIn("pairing_token", json.loads(body))
            c = srv.rpc()
            h = c.call("hello", {"client": "tui"})
            self.assertEqual(h["settings"]["thinking_budget"], 0)
            self.assertEqual([x["id"] for x in h["sessions"]], [s["id"]])
            msgs = c.call("sessions.subscribe", {"session_id": s["id"]})["messages"]
            self.assertEqual(msgs[0]["content"][0]["text"], "remember me")
            c.close()
            srv.stop()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class TestAuth(unittest.TestCase):
    def test_regenerate_closes_websockets(self):
        tmp = tempfile.mkdtemp(prefix="sfh-test-")
        html = os.path.join(tmp, "a.html")
        with open(html, "w") as f:
            f.write(EMPTY_HTML.replace('content=""', 'content="preset-token_1"'))
        srv = ServerProc(html, test_env(tmp)).start()
        try:
            self.assertEqual(srv.token, "preset-token_1")
            st, _, body = http(srv.url("/api/health", path=html))
            self.assertNotIn("pairing_token", json.loads(body))  # the file already had a token
            c1, c2 = srv.rpc(), srv.rpc()
            info = c1.call("auth.get")
            self.assertEqual(info["token"], "preset-token_1")
            closes = []
            orig = c2.ws._handle

            def spy(fin, op, payload):
                if op == 0x8:
                    closes.append(payload)
                return orig(fin, op, payload)
            c2.ws._handle = spy
            self.assertTrue(c1.call("auth.regenerate"))
            for c in (c1, c2):
                with self.assertRaises(ConnectionError):
                    deadline = time.time() + 5
                    while time.time() < deadline:
                        c.ws.recv_text(0.2)
            self.assertEqual(closes[0][:2], (4001).to_bytes(2, "big"))
            self.assertEqual(closes[0][2:], b"token-changed")
            new = srv.token
            self.assertNotEqual(new, "preset-token_1")
            with self.assertRaisesRegex(ConnectionError, "401"):
                WSClient(srv.ws_url(token="preset-token_1"))
            c3 = srv.rpc()
            g = c3.call("auth.get")
            self.assertEqual(g["token"], new)
            self.assertIsInstance(g["generated_at"], int)
            c3.close()
        finally:
            srv.stop()
            shutil.rmtree(tmp, ignore_errors=True)


def fake_openai_server(seen):
    """A tiny OpenAI-compatible endpoint: GET /v1/models and non-streaming POST /v1/chat/completions."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def reply(self, obj):
            body = json.dumps(obj).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            seen.setdefault("auth", []).append(self.headers.get("authorization"))
            self.reply({"data": [{"id": "small-1"}, {"id": "big-2"}]})

        def do_POST(self):
            seen.setdefault("auth", []).append(self.headers.get("authorization"))
            seen["body"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
            self.reply({"choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"},
                                     "finish_reason": "stop"}]})

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
    return httpd


def set_meta(html, key, value):
    """Writes meta(key)=value into the file's embedded db (no server running)."""
    from sfh import main as sfhmain
    con = sfhmain.load_db(html)
    con.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
    con.execute("INSERT OR REPLACE INTO meta(key, value) VALUES(?, ?)", (key, value))
    sfhmain.save_db(html, con)


def get_meta(html, key):
    from sfh import main as sfhmain
    row = sfhmain.load_db(html).execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row[0] if row else None


class TestOnboarding(unittest.TestCase):
    """questions/034: no silent mock, the H/P signals, provider setup, token fingerprint."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="sfh-test-")
        self.html = os.path.join(self.tmp, "o.html")
        with open(self.html, "w") as f:
            f.write(EMPTY_HTML)
        self.env = test_env(self.tmp)
        del self.env["SFH_MOCK"]   # a real user's environment: no mock provider

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_no_provider_then_setup(self):
        seen = {}
        httpd = fake_openai_server(seen)
        srv = ServerProc(self.html, self.env).start()
        try:
            c = srv.rpc()
            h = c.call("hello", {"client": "web"})
            self.assertEqual(h["onboarding"], {"has_history": False, "has_provider": False, "moved": False})
            self.assertIn("what", h["about"])
            self.assertNotIn("mock", [p["id"] for p in c.call("providers.list")])
            s = c.call("sessions.create", {"cwd": self.tmp})
            ev, _ = wait_event(c, lambda n, d: n == "onboarding")
            self.assertEqual(ev["data"], {"has_history": True, "has_provider": False, "moved": False})
            with self.assertRaisesRegex(Exception, "No LLM provider"):
                c.call("chat.send", {"session_id": s["id"], "text": "hi"})
            c.call("sessions.subscribe", {"session_id": s["id"]})
            c.call("chat.send", {"session_id": s["id"], "text": "/retitle"})   # LLM command: a notice instead
            ev, _ = wait_event(c, lambda n, d: n == "notice")
            self.assertIn("No LLM provider", ev["data"]["text"])
            c.call("chat.send", {"session_id": s["id"], "text": "/help"})      # other commands still work
            wait_event(c, lambda n, d: n == "notice" and "/help" in d["text"])
            base = "http://127.0.0.1:%d/v1" % httpd.server_port
            spec = {"type": "openai", "base_url": base, "token": "sk-test"}
            self.assertEqual([m["id"] for m in c.call("providers.probe", spec)["models"]], ["big-2", "small-1"])
            r = c.call("providers.test", dict(spec, model="small-1"))
            self.assertEqual(r, {"ok": True, "reply": "OK"})
            self.assertEqual(seen["body"]["model"], "small-1")
            self.assertEqual(seen["auth"][-1], "Bearer sk-test")
            r = c.call("providers.setup", dict(spec, model="small-1"))
            self.assertEqual(r["id"], "127.0.0.1")
            ev, _ = wait_event(c, lambda n, d: n == "onboarding" and d["has_provider"])
            settings = c.call("settings.get")
            self.assertEqual(settings["provider"], "127.0.0.1")
            self.assertEqual(settings["providers"][0]["api_key_credential"], "PROVIDER_127_0_0_1_KEY")
            with open(os.path.join(self.tmp, "cfg", "singlefile", "credentials")) as f:
                self.assertIn("PROVIDER_127_0_0_1_KEY=sk-test", f.read())
            self.assertNotIn("sk-test", htmlfile.read(self.html))
            c.close()
        finally:
            out = srv.stop()
            httpd.shutdown()
        self.assertIn("none configured", out)

    def test_fingerprint(self):
        fp = common.fingerprint(self.html)
        # first start: a token and the fingerprint of this host/user/path
        srv = ServerProc(self.html, self.env).start()
        tok1 = srv.token
        srv.stop()
        self.assertTrue(tok1)
        self.assertEqual(get_meta(self.html, "fingerprint"), fp)
        # restart in the same place: same token
        srv = ServerProc(self.html, self.env).start()
        self.assertEqual(srv.token, tok1)
        srv.stop()
        # copied from elsewhere, nothing set up yet (no H, no P): always a new token
        set_meta(self.html, "fingerprint", "elsewhere")
        srv = ServerProc(self.html, self.env).start()
        tok2 = srv.token
        c = srv.rpc()
        self.assertFalse(c.call("hello", {})["onboarding"]["moved"])
        c.call("sessions.create", {"cwd": self.tmp})
        c.close()
        time.sleep(0.3)  # debounced save
        srv.stop()
        self.assertNotEqual(tok2, tok1)
        self.assertEqual(get_meta(self.html, "fingerprint"), fp)
        # copied from elsewhere with history: keep the token, but say so (moved) until regenerated
        set_meta(self.html, "fingerprint", "elsewhere")
        srv = ServerProc(self.html, self.env).start()
        try:
            self.assertEqual(srv.token, tok2)
            c = srv.rpc()
            self.assertTrue(c.call("hello", {})["onboarding"]["moved"])
            try:
                c.call("auth.regenerate")
            except ConnectionError:
                pass
            c.close()
            c = srv.rpc()
            self.assertFalse(c.call("hello", {})["onboarding"]["moved"])
            c.close()
        finally:
            out = srv.stop()
        self.assertIn("different host/user/path", out)
        self.assertEqual(get_meta(self.html, "fingerprint"), fp)


class TestMigration(unittest.TestCase):
    def test_v1_db_gets_branches(self):
        import sqlite3
        from sfh import store
        db = sqlite3.connect(":memory:")
        db.executescript(store.MIGRATIONS[0])
        db.execute("INSERT INTO meta VALUES('schema_version', '1')")
        db.execute("INSERT INTO sessions VALUES('s_1', 't', '/', 1, 1, '[]', NULL)")
        db.execute("INSERT INTO sessions VALUES('s_2', 't', '/', 1, 1, '[]', NULL)")
        for sid, role in (("s_1", "user"), ("s_2", "user"), ("s_1", "assistant"), ("s_1", "user")):
            db.execute("INSERT INTO messages(session_id, role, content, created_at) VALUES(?, ?, '[]', 1)",
                       (sid, role))
        db.commit()
        tmp = tempfile.mkdtemp(prefix="sfh-test-")
        try:
            html = os.path.join(tmp, "m.html")
            with open(html, "w") as f:
                f.write(htmlfile.encode_data(EMPTY_HTML, db.serialize()))
            st = store.Store(html)
            self.assertEqual(st.query("SELECT id, parent_id FROM messages ORDER BY id"),
                             [(1, None), (2, None), (3, 1), (4, 3)])
            self.assertEqual(st.query("SELECT id, head_id FROM sessions ORDER BY id"), [("s_1", 4), ("s_2", 2)])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


def fake_sse_server(chunks, seen, path_check=None):
    """A one-shot HTTP server answering every POST with the given SSE `data:` payloads."""
    import http.server
    import threading

    class H(http.server.BaseHTTPRequestHandler):
        def do_POST(self):
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
            seen["headers"] = {k.lower(): v for k, v in self.headers.items()}
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.end_headers()
            for c in chunks:
                self.wfile.write(("data: %s\n\n" % (c if isinstance(c, str) else json.dumps(c))).encode())

        def log_message(self, *a):
            pass

    httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
    return httpd


class TestOpenAIStream(unittest.TestCase):
    """Parses a canned Chat Completions SSE stream (text, reasoning, tool_calls) from a fake server."""

    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in SECRET_ENV if k in os.environ}

    def tearDown(self):
        os.environ.update(self.saved)

    def chunk(self, delta, finish=None):
        return {"id": "c", "object": "chat.completion.chunk",
                "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}

    def test_sse(self):
        from sfh import llm
        ch = self.chunk
        chunks = [ch({"role": "assistant", "content": None, "reasoning_content": "Let me "}),
                  ch({"reasoning_content": "think."}),
                  ch({"reasoning": " more"}),
                  ch({"content": "Hello"}), ch({"content": " world"}),
                  ch({"tool_calls": [{"index": 0, "id": "call_a", "type": "function",
                                      "function": {"name": "bash", "arguments": ""}}]}),
                  ch({"tool_calls": [{"index": 0, "function": {"arguments": "{\"command\":"}}]}),
                  ch({"tool_calls": [{"index": 1, "id": "call_b", "function": {"name": "read",
                                                                               "arguments": "{\"path\":\"x\"}"}}]}),
                  ch({"tool_calls": [{"index": 0, "function": {"arguments": " \"ls\"}"}}]}),
                  ch({}, "tool_calls"),
                  {"id": "c", "choices": [], "usage": {"prompt_tokens": 1}},
                  "[DONE]"]
        seen = {}
        httpd = fake_sse_server(chunks, seen)
        os.environ.update(AI_ENDPOINT="http://127.0.0.1:%d/v1" % httpd.server_port, AI_MODEL="big-model",
                          AI_TOKEN="tok-123")
        try:
            creds = {"AI_TOKEN": os.environ["AI_TOKEN"]}
            prov = llm.resolve_provider({"provider": "auto", "model": None, "cheap_model": None}, creds)
            self.assertEqual((prov["id"], prov["type"], prov["model"], prov["cheap_model"]),
                             ("env", "openai", "big-model", "big-model"))
            msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
                    {"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": ""},
                                                      {"type": "text", "text": "ok"},
                                                      {"type": "tool_use", "id": "t1", "name": "bash",
                                                       "input": {"command": "pwd"}}]},
                    {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "/x",
                                                  "is_error": False}]},
                    {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
                    {"role": "user", "content": [{"type": "text", "text": "again"}]}]
            tools = [{"name": "bash", "description": "run", "input_schema": {"type": "object"}}]
            out = list(llm.stream(prov, creds, None, "sys", msgs, tools, 1000, 2000))
        finally:
            httpd.shutdown()
        self.assertEqual(seen["path"], "/v1/chat/completions")
        self.assertEqual(seen["headers"]["authorization"], "Bearer tok-123")
        body = seen["body"]
        self.assertTrue(body["stream"])
        self.assertEqual(body["model"], "big-model")
        self.assertEqual(body["tools"], [{"type": "function", "function": {
            "name": "bash", "description": "run", "parameters": {"type": "object"}}}])
        self.assertEqual(body["messages"], [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "ok", "tool_calls": [
                {"id": "t1", "type": "function", "function": {"name": "bash", "arguments": '{"command": "pwd"}'}}]},
            {"role": "tool", "tool_call_id": "t1", "content": "/x"},
            {"role": "assistant", "content": "done"},
            {"role": "user", "content": "again"}])
        blocks = [e for e in out if e[0] == "block"]
        self.assertEqual(blocks, [("block", 0, {"type": "thinking", "thinking": ""}),
                                  ("block", 1, {"type": "text", "text": ""}),
                                  ("block", 2, {"type": "tool_use", "id": "call_a", "name": "bash"}),
                                  ("block", 3, {"type": "tool_use", "id": "call_b", "name": "read"})])

        def joined(i, kind):
            return "".join(e[3] for e in out if e[0] == "delta" and e[1] == i and e[2] == kind)
        self.assertEqual(joined(0, "thinking"), "Let me think. more")
        self.assertEqual(joined(1, "text"), "Hello world")
        self.assertEqual(joined(2, "partial_json"), '{"command": "ls"}')
        self.assertEqual(joined(3, "partial_json"), '{"path":"x"}')
        self.assertEqual(out[-1], ("stop", "tool_use"))
        self.assertEqual(sum(1 for e in out if e[0] == "stop"), 1)

    def test_complete_and_overrides(self):
        import http.server
        import threading
        from sfh import llm
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["body"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
                out = json.dumps({"choices": [{"message": {"role": "assistant", "content": '{"title": "T"}'}}]})
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.end_headers()
                self.wfile.write(out.encode())

            def log_message(self, *a):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
        try:
            settings = {"provider": "auto", "cheap_model": "small",
                        "providers": [{"id": "mine", "type": "openai", "base_url":
                                       "http://127.0.0.1:%d/v1/" % httpd.server_port, "model": "m1",
                                       "api_key_credential": "MY_KEY"}]}
            prov = llm.resolve_provider(settings, {})
            self.assertEqual((prov["id"], prov["model"], prov["cheap_model"]), ("mine", "m1", "small"))
            self.assertEqual(llm.complete(prov, {}, "hello"), '{"title": "T"}')
            self.assertEqual(seen["body"]["model"], "small")
            ids = [p["id"] for p in llm.providers(settings, {"MY_KEY": "k"})]
            self.assertEqual(ids, ["mine", "anthropic"])   # no mock outside test mode
            self.assertTrue(llm.providers(settings, {"MY_KEY": "k"})[0]["has_key"])
            with self.assertRaises(llm.NoProviderError):
                llm.resolve_provider({"provider": "mock"}, {})
            with self.assertRaises(llm.NoProviderError):   # nothing configured: no silent mock fallback
                llm.resolve_provider({"provider": "auto"}, {})
            self.assertFalse(llm.configured({"provider": "auto"}, {}))
            self.assertTrue(llm.configured(settings, {}))    # the key is optional
            self.assertTrue(llm.configured({"provider": "auto"}, {"ANTHROPIC_API_KEY": "k"}))
            os.environ["SFH_MOCK"] = "1"
            try:
                self.assertEqual([p["id"] for p in llm.providers(settings, {})], ["mine", "anthropic", "mock"])
                self.assertEqual(llm.resolve_provider({"provider": "mock"}, {})["type"], "mock")
                self.assertEqual(llm.resolve_provider({"provider": "auto"}, {})["type"], "mock")
            finally:
                del os.environ["SFH_MOCK"]
            self.assertEqual(llm.resolve_provider({"provider": "auto"}, {"ANTHROPIC_API_KEY": "k"})["id"],
                             "anthropic")
            with self.assertRaises(llm.LLMError):
                llm.resolve_provider({"provider": "nope"}, {})
        finally:
            httpd.shutdown()


class TestAnthropicStream(unittest.TestCase):
    """Parses a canned SSE stream from a fake Messages API (via the ANTHROPIC_BASE_URL credential)."""

    def setUp(self):
        self.saved = {k: os.environ.pop(k) for k in SECRET_ENV if k in os.environ}

    def tearDown(self):
        os.environ.update(self.saved)

    def test_sse(self):
        import http.server
        import threading
        from sfh import llm
        events = [
            ("message_start", {"type": "message_start", "message": {"id": "m", "content": []}}),
            ("content_block_start", {"type": "content_block_start", "index": 0,
                                     "content_block": {"type": "thinking", "thinking": ""}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "thinking_delta", "thinking": "hmm"}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 0,
                                     "delta": {"type": "signature_delta", "signature": "sig"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 0}),
            ("ping", {"type": "ping"}),
            ("content_block_start", {"type": "content_block_start", "index": 1,
                                     "content_block": {"type": "tool_use", "id": "toolu_1", "name": "bash",
                                                       "input": {}}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                     "delta": {"type": "input_json_delta", "partial_json": "{\"command\":"}}),
            ("content_block_delta", {"type": "content_block_delta", "index": 1,
                                     "delta": {"type": "input_json_delta", "partial_json": " \"ls\"}"}}),
            ("content_block_stop", {"type": "content_block_stop", "index": 1}),
            ("message_delta", {"type": "message_delta", "delta": {"stop_reason": "tool_use"}}),
            ("message_stop", {"type": "message_stop"}),
        ]
        seen = {}

        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                seen["body"] = json.loads(self.rfile.read(int(self.headers["content-length"])))
                seen["headers"] = {k.lower(): v for k, v in self.headers.items()}
                self.send_response(200)
                self.send_header("content-type", "text/event-stream")
                self.end_headers()
                for name, data in events:
                    self.wfile.write(("event: %s\ndata: %s\n\n" % (name, json.dumps(data))).encode())

            def log_message(self, *a):
                pass

        httpd = http.server.HTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=httpd.serve_forever, args=(0.05,), daemon=True).start()
        creds = {"ANTHROPIC_API_KEY": "k", "ANTHROPIC_BASE_URL": "http://127.0.0.1:%d" % httpd.server_port}
        try:
            msgs = [{"role": "user", "content": [{"type": "text", "text": "hi"}]},
                    {"role": "assistant", "content": [{"type": "thinking", "thinking": "x", "signature": ""},
                                                      {"type": "text", "text": ""}]},
                    {"role": "user", "content": [{"type": "text", "text": "again"}]}]
            prov = llm.providers({}, creds)[0]
            self.assertEqual(prov["id"], "anthropic")
            out = list(llm.stream(prov, creds, "m1", "sys", msgs, [{"name": "bash"}], 1000, 2000))
        finally:
            httpd.shutdown()
        body = seen["body"]
        self.assertEqual(seen["headers"]["anthropic-version"], "2023-06-01")
        self.assertTrue(body["stream"])
        self.assertEqual(body["thinking"], {"type": "enabled", "budget_tokens": 2000})
        self.assertGreater(body["max_tokens"], 2000)
        # unsigned thinking/empty text dropped, consecutive user messages merged
        self.assertEqual([m["role"] for m in body["messages"]], ["user"])
        self.assertEqual(out[0], ("block", 0, {"type": "thinking", "thinking": "", "signature": ""}))
        self.assertIn(("delta", 0, "signature", "sig"), out)
        self.assertEqual(out[3], ("block", 1, {"type": "tool_use", "id": "toolu_1", "name": "bash"}))
        self.assertEqual("".join(e[3] for e in out if e[0] == "delta" and e[1] == 1), '{"command": "ls"}')
        self.assertEqual(out[-1], ("stop", "tool_use"))


if __name__ == "__main__":
    unittest.main()
