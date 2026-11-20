import base64
import hashlib
import json
import os
import socket
import sys
import threading
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src", "py"))

from sfh import wsclient  # noqa: E402
from sfh.wsclient import OP_CLOSE, OP_CONT, OP_PING, OP_PONG, OP_TEXT, encode_frame, parse_frame  # noqa: E402


class TinyServer:
    """Single-connection websocket server. Commands (text messages) drive special behaviour."""

    def __init__(self):
        self.lsock = socket.socket()
        self.lsock.bind(("127.0.0.1", 0))
        self.lsock.listen(1)
        self.port = self.lsock.getsockname()[1]
        self.request_line = None
        self.headers = {}
        self.pongs = []
        self.t = threading.Thread(target=self.run, daemon=True)
        self.t.start()

    def send(self, conn, op, payload, fin=True):
        conn.sendall(encode_frame(op, payload, mask=False, fin=fin))

    def run(self):
        conn, _ = self.lsock.accept()
        self.lsock.close()
        with conn:
            self.serve(conn)

    def serve(self, conn):
        data = b""
        while b"\r\n\r\n" not in data:
            data += conn.recv(4096)
        head = data.split(b"\r\n\r\n")[0].decode()
        lines = head.split("\r\n")
        self.request_line = lines[0]
        for ln in lines[1:]:
            k, _, v = ln.partition(":")
            self.headers[k.strip().lower()] = v.strip()
        acc = base64.b64encode(hashlib.sha1((self.headers["sec-websocket-key"] + wsclient._GUID).encode()).digest())
        conn.sendall(b"HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                     b"Sec-WebSocket-Accept: " + acc + b"\r\n\r\n")
        buf = bytearray()
        while True:
            fr = parse_frame(buf)
            if fr is None:
                chunk = conn.recv(65536)
                if not chunk:
                    return
                buf += chunk
                continue
            fin, op, payload, used = fr
            del buf[:used]
            if op == OP_PONG:
                self.pongs.append(payload)
                continue
            if op == OP_CLOSE:
                self.send(conn, OP_CLOSE, payload)
                conn.close()
                return
            text = payload.decode()
            if text == "frag":
                self.send(conn, OP_TEXT, b"hel", fin=False)
                self.send(conn, OP_PING, b"pp")
                self.send(conn, OP_CONT, "lo ".encode(), fin=False)
                self.send(conn, OP_CONT, "wörld".encode(), fin=True)
            elif text == "dribble":
                raw = encode_frame(OP_TEXT, b"x" * 300, mask=False)
                for i in range(len(raw)):
                    conn.sendall(raw[i:i + 1])
            elif text == "revoke":
                self.send(conn, OP_CLOSE, b"\x0f\xa1token-changed")
            elif text == "bye":
                self.send(conn, OP_CLOSE, b"\x03\xe8")
            elif text.startswith("{"):
                msg = json.loads(text)
                if msg.get("type") == "rpc":
                    self.send(conn, OP_TEXT, json.dumps({"type": "event", "event": "e1", "data": {}}).encode())
                    if msg["method"] == "fail":
                        out = {"type": "rpc_error", "id": msg["id"], "error": "nope"}
                    else:
                        out = {"type": "rpc_result", "id": msg["id"], "result": msg["params"]}
                    self.send(conn, OP_TEXT, json.dumps(out).encode())
                    self.send(conn, OP_TEXT, json.dumps({"type": "event", "event": "e2", "data": {}}).encode())
                else:
                    self.send(conn, OP_TEXT, payload)
            else:
                self.send(conn, OP_TEXT, payload)


class FrameTests(unittest.TestCase):
    def test_roundtrip_lengths(self):
        for n in (0, 5, 125, 126, 65535, 65536, 200000):
            p = os.urandom(n)
            raw = encode_frame(OP_TEXT, p)
            fin, op, payload, used = parse_frame(raw)
            self.assertEqual((fin, op, payload, used), (True, OP_TEXT, p, len(raw)))
            self.assertIsNone(parse_frame(raw[:-1]) if n else None)

    def test_client_frames_masked(self):
        raw = encode_frame(OP_TEXT, b"abc")
        self.assertTrue(raw[1] & 0x80)


class ClientTests(unittest.TestCase):
    def setUp(self):
        self.srv = TinyServer()
        self.ws = wsclient.WSClient("ws://127.0.0.1:%d/ws?path=%%2Fa%%20b" % self.srv.port)

    def tearDown(self):
        self.ws.close()

    def test_handshake_and_echo(self):
        self.ws.send_json({"a": 1})
        self.assertEqual(self.ws.recv_json(5), {"a": 1})
        self.assertEqual(self.srv.request_line, "GET /ws?path=%2Fa%20b HTTP/1.1")
        self.assertNotIn("origin", self.srv.headers)

    def test_large(self):
        s = "y" * 100000
        self.ws.send_text(s)
        self.assertEqual(self.ws.recv_text(5), s)

    def test_fragmented_with_ping(self):
        self.ws.send_text("frag")
        self.assertEqual(self.ws.recv_text(5), "hello wörld")
        self.ws.send_text("sync")
        self.assertEqual(self.ws.recv_text(5), "sync")
        self.assertEqual(self.srv.pongs, [b"pp"])

    def test_partial_frames(self):
        self.ws.send_text("dribble")
        self.assertEqual(self.ws.recv_text(5), "x" * 300)

    def test_timeout_returns_none(self):
        self.assertIsNone(self.ws.recv_json(0.1))

    def test_close(self):
        self.ws.send_text("bye")
        with self.assertRaises(ConnectionError):
            self.ws.recv_text(5)

    def test_close_4001_is_auth_error(self):
        self.ws.send_text("revoke")
        with self.assertRaises(wsclient.AuthError):
            self.ws.recv_text(5)
        self.assertEqual(self.ws.close_code, 4001)

    def test_rpc(self):
        rpc = wsclient.RPC(self.ws)
        self.assertEqual(rpc.call("x", {"k": 2}, timeout=5), {"k": 2})
        with self.assertRaises(wsclient.RPCError):
            rpc.call("fail", {}, timeout=5)
        evs = []
        for _ in range(20):
            evs += rpc.poll_events(0.2)
            if len(evs) >= 4:
                break
        self.assertEqual([e["event"] for e in evs], ["e1", "e2", "e1", "e2"])


class HandshakeRejectTests(unittest.TestCase):
    def test_401_is_auth_error(self):
        lsock = socket.socket()
        lsock.bind(("127.0.0.1", 0))
        lsock.listen(1)
        port = lsock.getsockname()[1]
        seen = {}

        def run():
            conn, _ = lsock.accept()
            with conn:
                data = b""
                while b"\r\n\r\n" not in data:
                    data += conn.recv(4096)
                seen["line"] = data.split(b"\r\n")[0].decode()
                conn.sendall(b"HTTP/1.1 401 Unauthorized\r\nContent-Length: 0\r\n\r\n")
            lsock.close()

        t = threading.Thread(target=run, daemon=True)
        t.start()
        with self.assertRaises(wsclient.AuthError):
            wsclient.RPC.connect(port, "/x y.html", timeout=5, token="tok_1")
        t.join(5)
        self.assertEqual(seen["line"], "GET /ws?path=%2Fx+y.html&token=tok_1 HTTP/1.1")


if __name__ == "__main__":
    unittest.main()
