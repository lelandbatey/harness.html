"""Reads and splices the embedded blocks of the .html file."""
import base64
import gzip
import os
import re

DATA_BEGIN = "<!--SFH-DATA-BEGIN-->"
DATA_END = "<!--SFH-DATA-END-->"
PROGRAM_BEGIN = "#--SFH-PROGRAM-BEGIN--"
PROGRAM_END = "#--SFH-PROGRAM-END--"
DATA_OPEN = '<script type="application/octet-stream" id="sfh-data" data-encoding="gzip+base64">'
DATA_CLOSE = "</script>"


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def write_atomic(path, text):
    tmp = "%s.%d.tmp" % (path, os.getpid())
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
    try:
        os.chmod(tmp, os.stat(path).st_mode & 0o7777)
    except FileNotFoundError:
        os.chmod(tmp, 0o755)
    os.replace(tmp, path)


def _data_span(src):
    """Returns (start, end) of the text between the LAST data marker pair."""
    b = src.rfind(DATA_BEGIN)
    e = src.rfind(DATA_END)
    if b < 0 or e < b:
        raise ValueError("no SFH-DATA block found")
    return b + len(DATA_BEGIN), e


def decode_data(src):
    s, e = _data_span(src)
    inner = src[s:e]
    if inner.startswith(DATA_OPEN):
        inner = inner[len(DATA_OPEN):]
    if inner.endswith(DATA_CLOSE):
        inner = inner[:-len(DATA_CLOSE)]
    inner = inner.strip()
    return gzip.decompress(base64.b64decode(inner)) if inner else b""


def encode_data(src, db_bytes):
    s, e = _data_span(src)
    payload = base64.b64encode(gzip.compress(db_bytes, 6)).decode("ascii") if db_bytes else ""
    return src[:s] + DATA_OPEN + payload + DATA_CLOSE + src[e:]


def read_data(path):
    """Raw sqlite bytes embedded in the file (b"" when empty)."""
    return decode_data(read(path))


def write_data(path, db_bytes):
    """Splices db_bytes into the file, leaving everything else untouched."""
    write_atomic(path, encode_data(read(path), db_bytes))


def program_source(src):
    b = src.rfind("\n" + PROGRAM_BEGIN + "\n")
    e = src.rfind("\n" + PROGRAM_END)
    if b < 0 or e < b:
        raise ValueError("no SFH-PROGRAM block found")
    return src[b + len(PROGRAM_BEGIN) + 2:e]


TOKEN_RE = re.compile(r'<meta name="sfh-token" content="([A-Za-z0-9_-]*)">')
OFFLINE_BEGIN = "<!--SFH-OFFLINE-BEGIN-->"
OFFLINE_END = "<!--SFH-OFFLINE-END-->"
OFFLINE_OPEN = '<script type="application/octet-stream" id="sfh-offline" data-encoding="gzip+base64">'


def decode_token(src):
    """The access token in the first sfh-token meta tag ("" when unset or missing)."""
    m = TOKEN_RE.search(src)
    return m.group(1) if m else ""


def encode_token(src, token):
    m = TOKEN_RE.search(src)
    if not m:
        raise ValueError("no sfh-token meta tag found")
    return src[:m.start(1)] + token + src[m.end(1):]


def read_token(path):
    return decode_token(read(path))


def encode_offline(src, json_bytes):
    """Splices the read-only offline snapshot (gzipped JSON) between the LAST offline markers.
    Files built before the offline block existed get one inserted before the data block."""
    b = src.rfind(OFFLINE_BEGIN)
    e = src.rfind(OFFLINE_END)
    payload = base64.b64encode(gzip.compress(json_bytes, 6)).decode("ascii") if json_bytes else ""
    block = OFFLINE_OPEN + payload + DATA_CLOSE
    if b < 0 or e < b:
        d = src.rfind(DATA_BEGIN)
        if d < 0:
            raise ValueError("no SFH-DATA block found")
        return src[:d] + OFFLINE_BEGIN + block + OFFLINE_END + "\n" + src[d:]
    return src[:b + len(OFFLINE_BEGIN)] + block + src[e:]


def decode_offline(src):
    b = src.rfind(OFFLINE_BEGIN)
    e = src.rfind(OFFLINE_END)
    if b < 0 or e < b:
        return b""
    inner = src[b + len(OFFLINE_BEGIN):e]
    if inner.startswith(OFFLINE_OPEN):
        inner = inner[len(OFFLINE_OPEN):]
    if inner.endswith(DATA_CLOSE):
        inner = inner[:-len(DATA_CLOSE)]
    inner = inner.strip()
    return gzip.decompress(base64.b64decode(inner)) if inner else b""
