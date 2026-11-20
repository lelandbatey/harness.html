"""Shared helpers: paths, port selection, request auth checks."""
import json
import os
import urllib.request

APP_ID = "singlefile-llm-harness"
VERSION = "0.1.0"
PORT_BASE = 47100
PORT_SPAN = 800
PORT_TRIES = 8


def html_path(p):
    return os.path.abspath(p)


def fingerprint(path):
    """Where this file runs: sha256(hostname, user, real path), shortened. A different value than the one
    stored in the file means the file was copied/moved/downloaded since its access token was made."""
    import getpass
    import hashlib
    import socket
    try:
        user = getpass.getuser()
    except Exception:
        user = str(os.getuid())
    raw = "\0".join((socket.gethostname(), user, os.path.realpath(path)))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def fnv1a32(s):
    h = 0x811C9DC5
    for b in s.encode("utf-8"):
        h ^= b
        h = (h * 0x01000193) & 0xFFFFFFFF
    return h


def candidate_ports(path):
    h = fnv1a32(path)
    return [PORT_BASE + ((h + i) % PORT_SPAN) for i in range(PORT_TRIES)]


def config_dir():
    return os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"), "singlefile")


def state_dir():
    return os.path.join(os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"), "singlefile")


def origin_ok(origin):
    """Browsers on file:// send Origin "null" (fetch) or "file://" (websocket); CLI clients send none."""
    return origin in (None, "", "null", "file://")


def health(port, timeout=0.5):
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=timeout) as r:
            return json.loads(r.read())
    except Exception:
        return None


def find_server(path, timeout=0.5):
    """Returns the port of the running server for this html file, or None."""
    for port in candidate_ports(path):
        info = health(port, timeout)
        if info and info.get("app") == APP_ID and info.get("path") == path:
            return port
    return None


TOKEN_CHANGED = "access token changed (or missing) — re-run the command (the token is read from the file)"


def auth_headers(path, token=None):
    """X-SFH-Path plus X-SFH-Token (read from the html file unless given)."""
    from sfh import htmlfile
    if token is None:
        try:
            token = htmlfile.read_token(path)
        except OSError:
            token = ""
    h = {"X-SFH-Path": path}
    if token:
        h["X-SFH-Token"] = token
    return h


def api(port, path, method, endpoint, body=None, timeout=60, token=None):
    """Calls an HTTP endpoint of a running server as a CLI client."""
    data = json.dumps(body).encode() if body is not None else None
    headers = auth_headers(path, token)
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request("http://127.0.0.1:%d%s" % (port, endpoint), data=data, method=method,
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw else None
