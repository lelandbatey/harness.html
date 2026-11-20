"""Minimal stdlib RFC6455 websocket client (ws:// only) + a JSON-RPC helper for the /ws protocol."""
import base64
import collections
import hashlib
import json
import os
import select
import socket
import struct
import time
import urllib.parse

from sfh import htmlfile

_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class RPCError(Exception):
    pass


class AuthError(ConnectionError):
    """The server refused the access token (handshake 401, or close code 4001 after a regenerate)."""


def encode_frame(opcode, payload, mask=True, fin=True):
    head = bytearray([(0x80 if fin else 0) | opcode])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head.append(mbit | n)
    elif n < 65536:
        head.append(mbit | 126)
        head += struct.pack("!H", n)
    else:
        head.append(mbit | 127)
        head += struct.pack("!Q", n)
    if not mask:
        return bytes(head) + payload
    key = os.urandom(4)
    return bytes(head) + key + _xor(payload, key)


def _xor(data, key):
    if not data:
        return b""
    k = (key * (len(data) // 4 + 1))[:len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(k, "big")).to_bytes(len(data), "big")


def parse_frame(buf):
    """Parses one frame from the start of buf. Returns (fin, opcode, payload, consumed) or None."""
    if len(buf) < 2:
        return None
    b0, b1 = buf[0], buf[1]
    n = b1 & 0x7F
    pos = 2
    if n == 126:
        if len(buf) < 4:
            return None
        n = struct.unpack("!H", bytes(buf[2:4]))[0]
        pos = 4
    elif n == 127:
        if len(buf) < 10:
            return None
        n = struct.unpack("!Q", bytes(buf[2:10]))[0]
        pos = 10
    key = None
    if b1 & 0x80:
        if len(buf) < pos + 4:
            return None
        key = bytes(buf[pos:pos + 4])
        pos += 4
    if len(buf) < pos + n:
        return None
    payload = bytes(buf[pos:pos + n])
    if key:
        payload = _xor(payload, key)
    return bool(b0 & 0x80), b0 & 0x0F, payload, pos + n


class WSClient:
    def __init__(self, url, headers=None, timeout=10):
        u = urllib.parse.urlsplit(url)
        if u.scheme != "ws":
            raise ValueError("only ws:// urls are supported")
        host, port = u.hostname, u.port or 80
        resource = (u.path or "/") + ("?" + u.query if u.query else "")
        self.sock = socket.create_connection((host, port), timeout=timeout)
        try:
            key = base64.b64encode(os.urandom(16)).decode()
            lines = ["GET %s HTTP/1.1" % resource, "Host: %s:%d" % (host, port), "Upgrade: websocket",
                     "Connection: Upgrade", "Sec-WebSocket-Key: " + key, "Sec-WebSocket-Version: 13"]
            for k, v in (headers or {}).items():
                lines.append("%s: %s" % (k, v))
            self.sock.sendall(("\r\n".join(lines) + "\r\n\r\n").encode())
            resp = b""
            deadline = time.time() + timeout
            while b"\r\n\r\n" not in resp:
                if time.time() > deadline:
                    raise ConnectionError("websocket handshake timed out")
                chunk = self.sock.recv(4096)
                if not chunk:
                    raise ConnectionError("connection closed during handshake")
                resp += chunk
            head, _, rest = resp.partition(b"\r\n\r\n")
            hlines = head.decode("latin-1").split("\r\n")
            if " 401 " in hlines[0] + " ":
                raise AuthError("access token rejected: " + hlines[0])
            if " 101 " not in hlines[0] + " ":
                raise ConnectionError("websocket handshake failed: " + hlines[0])
            hdrs = {}
            for ln in hlines[1:]:
                k, _, v = ln.partition(":")
                hdrs[k.strip().lower()] = v.strip()
            want = base64.b64encode(hashlib.sha1((key + _GUID).encode()).digest()).decode()
            if hdrs.get("sec-websocket-accept") != want:
                raise ConnectionError("bad Sec-WebSocket-Accept")
        except BaseException:
            self.sock.close()
            raise
        self.sock.settimeout(None)
        self.buf = bytearray(rest)
        self._frag_op = None
        self._frag = []
        self.closed = False
        self.close_code = None

    def fileno(self):
        return self.sock.fileno()

    def _send(self, opcode, payload):
        if self.closed:
            raise ConnectionError("websocket closed")
        self.sock.sendall(encode_frame(opcode, payload))

    def send_text(self, s):
        self._send(OP_TEXT, s.encode("utf-8"))

    def send_json(self, obj):
        self.send_text(json.dumps(obj))

    def pending(self):
        """True if a complete frame may already be buffered (select() won't report it)."""
        return parse_frame(self.buf) is not None

    def recv_text(self, timeout=None):
        """Returns the next text message, None on timeout; raises ConnectionError on close."""
        deadline = None if timeout is None else time.time() + timeout
        while True:
            while True:
                fr = parse_frame(self.buf)
                if fr is None:
                    break
                fin, op, payload, used = fr
                del self.buf[:used]
                msg = self._handle(fin, op, payload)
                if msg is not None:
                    return msg
            if self.closed:
                raise ConnectionError("websocket closed")
            wait = None if deadline is None else max(0, deadline - time.time())
            r, _, _ = select.select([self.sock], [], [], wait)
            if not r:
                return None
            try:
                chunk = self.sock.recv(65536)
            except OSError as e:
                self.closed = True
                raise ConnectionError(str(e))
            if not chunk:
                self.closed = True
                raise ConnectionError("websocket closed")
            self.buf += chunk

    def recv_json(self, timeout=None):
        s = self.recv_text(timeout)
        return None if s is None else json.loads(s)

    def _handle(self, fin, op, payload):
        if op == OP_PING:
            try:
                self._send(OP_PONG, payload)
            except OSError:
                pass
            return None
        if op == OP_PONG:
            return None
        if op == OP_CLOSE:
            if len(payload) >= 2:
                self.close_code = struct.unpack("!H", payload[:2])[0]
            if not self.closed:
                try:
                    self._send(OP_CLOSE, payload[:2])
                except OSError:
                    pass
            self.closed = True
            if self.close_code == 4001:
                raise AuthError("access token changed")
            raise ConnectionError("websocket closed by server")
        if op in (OP_TEXT, OP_BIN):
            if fin:
                return payload.decode("utf-8", "replace")
            self._frag_op, self._frag = op, [payload]
            return None
        if op == OP_CONT:
            self._frag.append(payload)
            if fin:
                data = b"".join(self._frag)
                self._frag, self._frag_op = [], None
                return data.decode("utf-8", "replace")
        return None

    def close(self):
        if not self.closed:
            try:
                self._send(OP_CLOSE, struct.pack("!H", 1000))
            except OSError:
                pass
            self.closed = True
        try:
            self.sock.close()
        except OSError:
            pass


class RPC:
    """Request/response over a WSClient; events arriving meanwhile are queued for poll_events()."""

    def __init__(self, ws):
        self.ws = ws
        self.next_id = 1
        self.events = collections.deque()

    @classmethod
    def connect(cls, port, html_path, timeout=10, token=None):
        q = {"path": html_path}
        if token is None:
            token = htmlfile.read_token(html_path)
        if token:
            q["token"] = token
        url = "ws://127.0.0.1:%d/ws?%s" % (port, urllib.parse.urlencode(q))
        return cls(WSClient(url, timeout=timeout))

    def fileno(self):
        return self.ws.fileno()

    def call(self, method, params=None, timeout=30):
        rid = self.next_id
        self.next_id += 1
        self.ws.send_json({"type": "rpc", "id": rid, "method": method, "params": params or {}})
        deadline = time.time() + timeout
        while True:
            left = deadline - time.time()
            if left <= 0:
                raise TimeoutError("rpc %s timed out" % method)
            msg = self.ws.recv_json(left)
            if msg is None:
                continue
            t = msg.get("type")
            if t in ("rpc_result", "rpc_error") and msg.get("id") == rid:
                if t == "rpc_error":
                    raise RPCError(msg.get("error"))
                return msg.get("result")
            if t == "event":
                self.events.append(msg)

    def poll_events(self, timeout=0):
        """Returns a list of queued/arriving event messages ({"event","data"})."""
        if not self.events:
            msg = self.ws.recv_json(timeout)
            if msg is not None and msg.get("type") == "event":
                self.events.append(msg)
        while self.ws.pending():
            msg = self.ws.recv_json(0)
            if msg is not None and msg.get("type") == "event":
                self.events.append(msg)
        out = list(self.events)
        self.events.clear()
        return out

    def has_pending(self):
        return bool(self.events) or self.ws.pending()

    def close(self):
        self.ws.close()
