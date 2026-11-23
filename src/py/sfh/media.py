"""Files the model can view: images (png/jpeg/gif/webp) and PDFs.

Nothing of a file's content is ever stored. The `read` tool returns a POINTER block, and that is all that
goes into the message history (and so into the db):

    {"type": "image" | "document",
     "source": {"type": "path", "path": "/abs/file.png", "media_type": "image/png",
                "sha256": "…", "size": 12345},
     "width": 1280, "height": 800}            # images only, when the header says

Only when a request is built (llm.build_body / llm.openai_messages, per attempt, in memory) does inline()
read the file again, check it still has the hash it had when it was read, and base64 it for the provider.
A file that is gone or changed becomes a text note instead, so the model reads it again rather than being
shown something different from what it was told it saw."""
import base64
import hashlib
import os
import struct

IMAGE_TYPES = ("image/png", "image/jpeg", "image/gif", "image/webp")
PDF = "application/pdf"
# the API's per-image limit is 5 MB of base64; 3.75 MB of file stays under it
MAX_IMAGE_BYTES = 3750000
MAX_IMAGE_SIDE = 8000
MAX_PDF_BYTES = 24 * 1024 * 1024


class MediaError(Exception):
    pass


def sniff(head):
    """The media type of a file from its first bytes, or None when it's not one we can send."""
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "image/webp"
    if head.startswith(b"%PDF-"):
        return PDF
    return None


def image_size(data, media_type):
    """(width, height) from the image header, or None."""
    try:
        if media_type == "image/png":
            return struct.unpack(">II", data[16:24])
        if media_type == "image/gif":
            return struct.unpack("<HH", data[6:10])
        if media_type == "image/webp":
            kind = data[12:16]
            if kind == b"VP8 ":
                w, h = struct.unpack("<HH", data[26:30])
                return w & 0x3fff, h & 0x3fff
            if kind == b"VP8L":
                b = data[21:25]
                return 1 + (((b[1] & 0x3f) << 8) | b[0]), 1 + (((b[3] & 0xf) << 10) | (b[2] << 2) | ((b[1] & 0xc0) >> 6))
            if kind == b"VP8X":
                return (1 + int.from_bytes(data[24:27], "little"), 1 + int.from_bytes(data[27:30], "little"))
            return None
        if media_type == "image/jpeg":
            i = 2
            while i + 9 < len(data):
                if data[i] != 0xff:
                    i += 1
                    continue
                marker = data[i + 1]
                if marker in (0xd8, 0x01) or 0xd0 <= marker <= 0xd7 or marker == 0xff:
                    i += 1 if marker == 0xff else 2
                    continue
                seg = struct.unpack(">H", data[i + 2:i + 4])[0]
                if 0xc0 <= marker <= 0xcf and marker not in (0xc4, 0xc8, 0xcc):
                    h, w = struct.unpack(">HH", data[i + 5:i + 9])
                    return w, h
                i += 2 + seg
    except (struct.error, IndexError):
        pass
    return None


def is_media(block):
    return isinstance(block, dict) and block.get("type") in ("image", "document")


def pointer(path, data, media_type):
    """The block `read` returns for a viewable file (raises MediaError when it can't be sent)."""
    size = len(data)
    if media_type == PDF:
        if size > MAX_PDF_BYTES:
            raise MediaError("%s is a %d MB PDF; the limit is %d MB. Split it (e.g. `qpdf --split-pages`) or "
                             "extract text with `pdftotext`." % (path, size >> 20, MAX_PDF_BYTES >> 20))
        block = {"type": "document"}
    else:
        dims = image_size(data, media_type)
        if size > MAX_IMAGE_BYTES:
            raise MediaError("%s is %d KB; images can be at most %d KB. Make a smaller copy first, e.g. "
                             "`convert in.png -resize 1568x1568\\> -quality 85 /tmp/small.jpg`, and read that."
                             % (path, size >> 10, MAX_IMAGE_BYTES >> 10))
        if dims and max(dims) > MAX_IMAGE_SIDE:
            raise MediaError("%s is %d×%d pixels; images can be at most %d on a side. Make a smaller copy "
                             "(e.g. `convert in.png -resize 1568x1568 /tmp/small.png`) and read that."
                             % ((path,) + tuple(dims) + (MAX_IMAGE_SIDE,)))
        block = {"type": "image"}
        if dims:
            block["width"], block["height"] = dims
    block["source"] = {"type": "path", "path": path, "media_type": media_type,
                       "sha256": hashlib.sha256(data).hexdigest(), "size": size}
    return block


def describe(block):
    """One line about a pointer block, for people and as the text the model gets alongside it."""
    src = block.get("source") or {}
    what = "image" if block.get("type") == "image" else "PDF"
    bits = [src.get("media_type", "?")]
    if block.get("width") and block.get("height"):
        bits.append("%d×%d" % (block["width"], block["height"]))
    if src.get("size") is not None:
        bits.append("%.1f KB" % (src["size"] / 1024.0))
    return "%s %s (%s)" % (what, src.get("path", "?"), ", ".join(bits))


def note(block, why):
    return {"type": "text", "text": "[%s not attached: %s]" % (describe(block), why)}


def inline(block, enabled=True):
    """A pointer block -> the same block with its file's bytes as a base64 source (read now, never stored),
    or a text block saying why it can't be attached. Blocks already carrying data pass through."""
    src = block.get("source") or {}
    if src.get("type") != "path":
        return block
    if not enabled:
        return note(block, "this provider is configured without file support (media: false)")
    path = src.get("path") or ""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return note(block, "the file can't be read any more (%s)" % (e.strerror or e))
    if src.get("sha256") and hashlib.sha256(data).hexdigest() != src["sha256"]:
        return note(block, "the file changed since it was read; read it again to view it")
    out = {"type": block["type"], "source": {"type": "base64", "media_type": src.get("media_type"),
                                             "data": base64.b64encode(data).decode("ascii")}}
    if block["type"] == "document":
        out["title"] = os.path.basename(path)
    return out


def data_url(block):
    """An inlined (base64) block as a data: URL."""
    s = block["source"]
    return "data:%s;base64,%s" % (s["media_type"], s["data"])
