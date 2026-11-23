"""In-memory sqlite db deserialized from the html data block, saved back (debounced) on change."""
import json
import os
import sqlite3
import sys
import threading
import time

from . import htmlfile

SAVE_DELAY = float(os.environ.get("SFH_SAVE_DELAY", 1.5))  # tests shorten it
SAVE_MAX_DELAY = 6.0

DEFAULT_SYSTEM_PROMPT = """\
You are a capable coding agent running inside a local harness. You help the user with software
engineering tasks: reading and changing code, running commands, investigating problems.

- Use the tools to inspect files and run commands instead of guessing. Paths are relative to the
  session's working directory unless absolute.
- Prefer small, targeted edits (the `edit` tool) over rewriting whole files.
- Before running a command that is destructive or hard to undo, explain it and ask first.
- Be concise. Use GitHub-flavored markdown. Refer to files as `path:line` so they are clickable.
- When a skill listed below matches the task, load it with the `skill` tool and follow it.
- The web UI renders HTML in your markdown (sanitized: no scripts, event handlers, <style> or forms).
  Use inline HTML/SVG for small static visuals such as diagrams, colored tables or <details> sections;
  style with attributes (`style="…"`, `fill=…`) and keep an HTML block free of blank lines (a single
  <svg>…</svg> may contain them).
- For anything interactive or needing scripts/CSS, emit an artifact: a fenced block with info string
  `artifact title="…"` holding one complete, self-contained HTML document. It runs in a sandboxed iframe
  (no access to the harness, network only via https). Use a longer fence (````) if the HTML contains ```.
  The TUI shows HTML as its source and only a one-line placeholder for an artifact."""

SETTINGS_DEFAULTS = {
    "provider": "auto",
    "providers": [],
    "model": None,
    "cheap_model": None,
    "max_tokens": 16000,
    "thinking_budget": 4000,
    "effort": None,  # default effort level for sessions without their own (None = don't send one)
    "model_overrides": {},  # {provider id or "*": {model id: {"context_window": int}}}
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "builtin_skill_enabled": True,
    "editor": "",  # empty: $EDITOR, then nano, pico, vi (see pty_ws.resolve_editor)
    "identicon_subjects": 3,
    "credentials_storage": "xdg",
    "default_cwd": None,
    "request_timeout": 30,
    "stream_idle_timeout": 120,
    "max_retries": 3,
    "retry_base_delay": 2.0,
    "ui.initial_view": None,
    "tui_mouse": True,  # TUI: mouse reporting on (wheel scrolls the transcript; Shift+drag selects text)
}

MIGRATIONS = [
    # 1: initial schema
    """
    CREATE TABLE IF NOT EXISTS sessions(id TEXT PRIMARY KEY, title TEXT, cwd TEXT, created_at INT,
                                        updated_at INT, subjects TEXT, model TEXT);
    CREATE TABLE IF NOT EXISTS messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT,
                                        content TEXT, created_at INT);
    CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id, id);
    CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS credentials(name TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS files(name TEXT PRIMARY KEY, mime TEXT, data BLOB, created_at INT);
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    """,
    # 2: edit-history branches; existing messages chain by id order per session
    """
    ALTER TABLE messages ADD COLUMN parent_id INTEGER;
    ALTER TABLE sessions ADD COLUMN head_id INTEGER;
    UPDATE messages SET parent_id = (SELECT MAX(m2.id) FROM messages m2
                                     WHERE m2.session_id = messages.session_id AND m2.id < messages.id);
    UPDATE sessions SET head_id = (SELECT MAX(id) FROM messages WHERE session_id = sessions.id);
    CREATE INDEX IF NOT EXISTS messages_parent ON messages(session_id, parent_id);
    """,
    # 3: subagent child sessions
    """
    ALTER TABLE sessions ADD COLUMN parent_id TEXT;
    ALTER TABLE sessions ADD COLUMN kind TEXT;
    """,
    # 4: per-session provider, per-message model/provider/cwd/links, recently used working directories
    """
    ALTER TABLE sessions ADD COLUMN provider TEXT;
    ALTER TABLE messages ADD COLUMN model TEXT;
    ALTER TABLE messages ADD COLUMN provider TEXT;
    ALTER TABLE messages ADD COLUMN cwd TEXT;
    ALTER TABLE messages ADD COLUMN links TEXT;
    UPDATE messages SET cwd = (SELECT cwd FROM sessions WHERE sessions.id = messages.session_id),
                        model = (SELECT model FROM sessions WHERE sessions.id = messages.session_id);
    CREATE TABLE IF NOT EXISTS recent_cwds(path TEXT PRIMARY KEY, used_at INT);
    INSERT OR REPLACE INTO recent_cwds(path, used_at)
        SELECT cwd, MAX(COALESCE(updated_at, created_at, 0)) FROM sessions
        WHERE cwd IS NOT NULL AND (kind IS NULL OR kind != 'subagent') GROUP BY cwd;
    """,
    # 5: token usage / cost per provider response and per session (running total of every call)
    """
    ALTER TABLE messages ADD COLUMN usage TEXT;
    ALTER TABLE sessions ADD COLUMN usage TEXT;
    """,
    # 6: what a session was about when its subjects were made (signals.extract), for identicon context
    """
    ALTER TABLE sessions ADD COLUMN signals TEXT;
    """,
    # 7: per-session effort level; per-message effort and the time its LLM request started (durations)
    """
    ALTER TABLE sessions ADD COLUMN effort TEXT;
    ALTER TABLE messages ADD COLUMN effort TEXT;
    ALTER TABLE messages ADD COLUMN started_at INT;
    """,
    # 8: lightweight conversation branching (/aside): named branch tips inside a session
    """
    CREATE TABLE IF NOT EXISTS branches(id TEXT PRIMARY KEY, session_id TEXT NOT NULL, name TEXT,
                                        origin_id INTEGER, head_id INTEGER, kind TEXT,
                                        created_at INT, updated_at INT);
    CREATE INDEX IF NOT EXISTS branches_session ON branches(session_id, updated_at);
    ALTER TABLE sessions ADD COLUMN active_branch_id TEXT;
    """,
    # 9: unified session threads (asides + subagents): explicit parent branch, UI anchor, fresh/fork context
    """
    ALTER TABLE branches ADD COLUMN parent_branch_id TEXT;
    ALTER TABLE branches ADD COLUMN spawned_at_id INTEGER;
    ALTER TABLE branches ADD COLUMN context_mode TEXT;
    UPDATE branches SET spawned_at_id = origin_id, context_mode = 'fork' WHERE context_mode IS NULL;
    """,
    # 10: main is a tag (sessions.main_branch_id), the first thread is kind 'root'; per-thread collapsed state
    """
    ALTER TABLE sessions ADD COLUMN main_branch_id TEXT;
    ALTER TABLE branches ADD COLUMN expanded INT;
    UPDATE sessions SET main_branch_id = (SELECT b.id FROM branches b WHERE b.session_id = sessions.id
                                          AND b.kind = 'main' ORDER BY b.created_at LIMIT 1);
    UPDATE branches SET kind = CASE WHEN origin_id IS NULL AND parent_branch_id IS NULL THEN 'root' ELSE 'aside' END
        WHERE kind = 'main';
    UPDATE branches SET kind = 'root' WHERE kind = 'aside' AND origin_id IS NULL AND parent_branch_id IS NULL;
    UPDATE branches SET name = NULL WHERE kind = 'root' AND name IN ('Main', 'Previous main');
    """,
]


def now_ms():
    return int(time.time() * 1000)


def open_db(data):
    db = sqlite3.connect(":memory:", check_same_thread=False, isolation_level=None)
    if data:
        db.deserialize(data)
    migrate(db)
    return db


def migrate(db):
    has_meta = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'").fetchone()
    version = 0
    if has_meta:
        row = db.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
        version = int(row[0]) if row else 0
    for i in range(version, len(MIGRATIONS)):
        db.executescript(MIGRATIONS[i])
        db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)", (str(i + 1),))


class Store:
    def __init__(self, html_path):
        self.path = html_path
        self.lock = threading.RLock()
        self.save_lock = threading.Lock()
        self.db = open_db(htmlfile.read_data(html_path))
        self.token = None          # when set, written into the file's sfh-token meta tag on save
        self.offline = None        # callable -> JSON bytes for the offline snapshot block
        self.dirty = False
        self._timer = None
        self._first_dirty = 0.0

    # --- queries -------------------------------------------------------------------------------

    def query(self, sql, params=()):
        with self.lock:
            return self.db.execute(sql, params).fetchall()

    def query_one(self, sql, params=()):
        with self.lock:
            return self.db.execute(sql, params).fetchone()

    def execute(self, sql, params=()):
        """Runs a write; returns lastrowid."""
        with self.lock:
            cur = self.db.execute(sql, params)
            self.mark_dirty()
            return cur.lastrowid

    def run_sql(self, sql, params=(), write=False):
        """Arbitrary SQL for the /api/sql endpoint and the cli. Returns (columns, rows)."""
        with self.lock:
            if not write:
                self.db.execute("PRAGMA query_only=ON")
            try:
                cur = self.db.execute(sql, params)
                cols = [d[0] for d in cur.description] if cur.description else []
                rows = cur.fetchall()
            finally:
                if not write:
                    self.db.execute("PRAGMA query_only=OFF")
            if write:
                self.mark_dirty()
            return cols, rows

    # --- settings ------------------------------------------------------------------------------

    def settings(self):
        out = dict(SETTINGS_DEFAULTS)
        for k, v in self.query("SELECT key, value FROM settings"):
            try:
                out[k] = json.loads(v)
            except ValueError:
                out[k] = v
        return out

    def setting(self, key):
        row = self.query_one("SELECT value FROM settings WHERE key=?", (key,))
        if row is None:
            return SETTINGS_DEFAULTS.get(key)
        try:
            return json.loads(row[0])
        except ValueError:
            return row[0]

    def set_setting(self, key, value):
        if key not in SETTINGS_DEFAULTS and not key.startswith("ui."):
            raise ValueError("unknown setting: %s" % key)
        self.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (key, json.dumps(value)))

    # --- persistence ---------------------------------------------------------------------------

    def serialize(self):
        with self.lock:
            return self.db.serialize()

    def mark_dirty(self):
        with self.lock:
            t = time.monotonic()
            if not self.dirty:
                self._first_dirty = t
            self.dirty = True
            if self._timer:
                self._timer.cancel()
            delay = min(SAVE_DELAY, max(0.0, self._first_dirty + SAVE_MAX_DELAY - t))
            self._timer = threading.Timer(delay, self._timer_save)
            self._timer.daemon = True
            self._timer.start()

    def _timer_save(self):
        try:
            self.save()
        except Exception as e:  # keep the server alive; the next change retries
            print("sfh: saving data into %s failed: %s" % (self.path, e), file=sys.stderr)

    def save(self, force=False):
        with self.save_lock:
            with self.lock:
                if not (self.dirty or force):
                    return False
                if self._timer:
                    self._timer.cancel()
                    self._timer = None
                self.dirty = False
                data = self.db.serialize()
            try:
                offline = self.offline() if self.offline else None
                src = htmlfile.encode_data(htmlfile.read(self.path), data)
                if offline is not None:
                    src = htmlfile.encode_offline(src, offline)
                if self.token is not None:
                    src = set_token(src, self.token)
                htmlfile.write_atomic(self.path, src)
            except Exception:
                with self.lock:
                    self.dirty = True
                raise
            return True

    def export_bytes(self, overrides=None, strip_credentials=False):
        """A serialized copy of the db with settings overridden, optionally without credentials."""
        copy = open_db(self.serialize())
        try:
            for k, v in (overrides or {}).items():
                copy.execute("INSERT OR REPLACE INTO settings(key, value) VALUES(?, ?)", (k, json.dumps(v)))
            if strip_credentials:
                copy.execute("DELETE FROM credentials")
                copy.execute("VACUUM")
            return copy.serialize()
        finally:
            copy.close()


def set_token(src, token):
    """encode_token, inserting the meta tag into <head> for files built before it existed."""
    if htmlfile.TOKEN_RE.search(src):
        return htmlfile.encode_token(src, token)
    tag = '<meta name="sfh-token" content="%s">' % token
    i = src.find("<head>")
    if i >= 0:
        i += len("<head>")
    else:
        i = src.find("<body")
        i = i if i >= 0 else 0
        tag = "<head>" + tag + "</head>"
    return src[:i] + tag + src[i:]
