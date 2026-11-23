"""Images/PDFs: `read` returns a pointer (path + sha256) that is stored; the file's bytes are read and sent only
while a request body is built, never stored. Unit tests of sfh.media / llm bodies, and an end-to-end turn with
the mock provider."""
import base64
import json
import os
import shutil
import struct
import sys
import tempfile
import time
import unittest
import zlib

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path[:0] = [HERE, os.path.join(ROOT, "src", "py"), os.path.join(ROOT, "vendor")]

from sfh import llm, media, tools  # noqa: E402


def png(w=3, h=2):
    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xffffffff)
    raw = b"".join(b"\0" + b"\xff\0\0" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


JPEG = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        b"\xff\xc0\x00\x11\x08\x00\x20\x00\x40\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xd9")
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


class MediaTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="sfh-media-")
        self.ctx = tools.Context(self.dir)

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def put(self, name, data):
        with open(os.path.join(self.dir, name), "wb") as f:
            f.write(data)
        return os.path.join(self.dir, name)

    def read(self, name):
        out, err = tools.execute("read", {"path": name}, self.ctx)
        self.assertFalse(err, out)
        return out

    def history(self, result):
        return [{"role": "user", "content": [{"type": "text", "text": "look"}]},
                {"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read",
                                                   "input": {"path": "a.png"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": result}]}]

    def test_sniff_and_size(self):
        self.assertEqual(media.image_size(png(5, 7), "image/png"), (5, 7))
        self.assertEqual(media.sniff(JPEG[:16]), "image/jpeg")
        self.assertEqual(media.image_size(JPEG, "image/jpeg"), (64, 32))
        self.assertEqual(media.sniff(PDF[:16]), "application/pdf")
        self.assertIsNone(media.sniff(b"hello"))

    def test_read_returns_pointer_without_bytes(self):
        data = png()
        path = self.put("a.png", data)
        out = self.read("a.png")
        self.assertEqual(out[0]["type"], "text")
        block = out[1]
        self.assertEqual(block["type"], "image")
        self.assertEqual((block["width"], block["height"]), (3, 2))
        self.assertEqual(block["source"]["type"], "path")
        self.assertEqual(block["source"]["path"], path)
        self.assertEqual(block["source"]["size"], len(data))
        self.assertNotIn(base64.b64encode(data).decode(), json.dumps(out))  # only a pointer is ever stored

    def test_anthropic_body_inlines_and_leaves_history_alone(self):
        data = png()
        self.put("a.png", data)
        hist = self.history(self.read("a.png"))
        before = json.dumps(hist)
        body = llm.build_body("m", "", hist)
        img = body["messages"][2]["content"][0]["content"][1]
        self.assertEqual(img, {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                            "data": base64.b64encode(data).decode()}})
        self.assertEqual(json.dumps(hist), before)

    def test_changed_or_missing_file_becomes_a_note(self):
        path = self.put("a.png", png())
        hist = self.history(self.read("a.png"))
        self.put("a.png", png(4, 4))
        part = llm.build_body("m", "", hist)["messages"][2]["content"][0]["content"][1]
        self.assertEqual(part["type"], "text")
        self.assertIn("changed since it was read", part["text"])
        os.remove(path)
        part = llm.build_body("m", "", hist)["messages"][2]["content"][0]["content"][1]
        self.assertIn("can't be read", part["text"])

    def test_media_disabled(self):
        self.put("a.png", png())
        hist = self.history(self.read("a.png"))
        part = llm.build_body("m", "", hist, media_ok=False)["messages"][2]["content"][0]["content"][1]
        self.assertIn("media: false", part["text"])

    def test_pdf(self):
        self.put("d.pdf", PDF)
        out = self.read("d.pdf")
        self.assertEqual(out[1]["type"], "document")
        hist = self.history(out)
        doc = llm.build_body("m", "", hist)["messages"][2]["content"][0]["content"][1]
        self.assertEqual((doc["type"], doc["title"], doc["source"]["media_type"]), ("document", "d.pdf", "application/pdf"))
        tool_msg = llm.build_openai_body("m", "", hist)["messages"][2]
        self.assertEqual(tool_msg["content"][1]["type"], "file")
        self.assertEqual(tool_msg["content"][1]["file"]["filename"], "d.pdf")
        self.assertTrue(tool_msg["content"][1]["file"]["file_data"].startswith("data:application/pdf;base64,"))

    def test_openai_inline_in_tool_message(self):
        data = png()
        self.put("a.png", data)
        msgs = llm.build_openai_body("m", "sys", self.history(self.read("a.png")))["messages"]
        tool_msg = msgs[-1]
        self.assertEqual(tool_msg["role"], "tool")
        self.assertEqual(tool_msg["content"][0]["type"], "text")
        self.assertEqual(tool_msg["content"][1], {"type": "image_url", "image_url": {
            "url": "data:image/png;base64," + base64.b64encode(data).decode()}})

    def test_openai_files_deferred_to_user_message(self):
        self.put("a.png", png())
        msgs = llm.build_openai_body("m", "", self.history(self.read("a.png")), media_in_tool_results=False)["messages"]
        self.assertEqual([m["role"] for m in msgs], ["user", "assistant", "tool", "user"])
        self.assertIsInstance(msgs[2]["content"], str)
        self.assertIn("attached in the next user message", msgs[2]["content"])
        self.assertEqual([p["type"] for p in msgs[3]["content"]], ["text", "image_url"])

    def test_limits(self):
        self.put("big.png", png() + b"\0" * media.MAX_IMAGE_BYTES)
        out, err = tools.execute("read", {"path": "big.png"}, self.ctx)
        self.assertTrue(err)
        self.assertIn("smaller copy", out)
        out, err = tools.execute("read", {"path": self.put("x.bin", b"\0\1\2")}, self.ctx)
        self.assertTrue(err)
        self.assertIn("binary", out)

    def test_providers_options(self):
        ps = llm.providers({"providers": [{"id": "meridian", "base_url": "http://x/v1", "model": "m"},
                                          {"id": "oa", "base_url": "http://y/v1", "model": "m",
                                           "media_in_tool_results": False, "media": False}]}, {})
        by = {p["id"]: p for p in ps}
        self.assertEqual((by["meridian"]["media"], by["meridian"]["media_in_tool_results"]), (True, True))
        self.assertEqual((by["oa"]["media"], by["oa"]["media_in_tool_results"]), (False, False))


class MediaEndToEnd(unittest.TestCase):
    """A mock turn that reads an image: the provider gets the bytes, the stored history only the pointer."""

    @classmethod
    def setUpClass(cls):
        from test_server import EMPTY_HTML, SECRET_ENV, ServerProc
        cls.tmp = tempfile.mkdtemp(prefix="sfh-media-e2e-")
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

    def test_read_image_turn(self):
        c = self.server.rpc()
        c.call("hello", {"client": "tui"})
        cwd = tempfile.mkdtemp(dir=self.tmp)
        data = png(6, 4)
        with open(os.path.join(cwd, "pic.png"), "wb") as f:
            f.write(data)
        sid = c.call("sessions.create", {"cwd": cwd})["id"]
        c.call("sessions.subscribe", {"session_id": sid})
        c.call("chat.send", {"session_id": sid, "text": '[[tool:read:{"path": "pic.png"}]]'})
        deadline = time.time() + 15
        while True:
            c.poll_events(0.05)
            msgs = c.call("sessions.subscribe", {"session_id": sid})["messages"]
            summary = next(s for s in c.call("sessions.list") if s["id"] == sid)
            if len(msgs) >= 4 and summary["status"] == "idle":
                break
            self.assertLess(time.time(), deadline)
            time.sleep(0.1)
        c.close()
        reply = "".join(b.get("text", "") for b in msgs[-1]["content"] if b.get("type") == "text")
        self.assertIn("[saw image/png, %d bytes]" % len(data), reply)
        result = next(b for m in msgs for b in m["content"] if b.get("type") == "tool_result")
        self.assertEqual(result["content"][1]["source"]["type"], "path")
        self.assertNotIn(base64.b64encode(data).decode(), json.dumps(msgs))


if __name__ == "__main__":
    unittest.main()
