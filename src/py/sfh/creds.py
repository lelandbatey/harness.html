"""Credentials: $XDG_CONFIG_HOME/singlefile/credentials (0600 KEY=value lines) or the db (embedded).

Environment variables override stored values. Values are never sent to clients."""
import os

from . import common

KNOWN = ("ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "AI_TOKEN")


def file_path():
    return os.path.join(common.config_dir(), "credentials")


def read_file(path=None):
    path = path or file_path()
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                out[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return out


def write_file(values, path=None):
    path = path or file_path()
    os.makedirs(os.path.dirname(path), mode=0o700, exist_ok=True)
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        for k in sorted(values):
            f.write("%s=%s\n" % (k, values[k]))
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


class Creds:
    def __init__(self, store):
        self.store = store

    def storage(self):
        return "embedded" if self.store.setting("credentials_storage") == "embedded" else "xdg"

    def _stored(self, mode=None):
        if (mode or self.storage()) == "embedded":
            return {k: v for k, v in self.store.query("SELECT name, value FROM credentials")}
        return read_file()

    def _write(self, values, mode=None):
        if (mode or self.storage()) == "embedded":
            with self.store.lock:
                self.store.execute("DELETE FROM credentials")
                for k, v in values.items():
                    self.store.execute("INSERT INTO credentials(name, value) VALUES(?, ?)", (k, v))
        else:
            write_file(values)

    def get(self, name):
        return os.environ.get(name) or self._stored().get(name) or None

    def set(self, name, value):
        name = (name or "").strip()
        if not name or "=" in name or "\n" in name:
            raise ValueError("bad credential name")
        values = self._stored()
        if value in (None, ""):
            values.pop(name, None)
        else:
            values[name] = str(value).strip()
        self._write(values)

    def set_storage(self, mode):
        if mode not in ("xdg", "embedded"):
            raise ValueError("mode must be xdg or embedded")
        old = self.storage()
        if mode == old:
            return
        if mode == "embedded":
            # the xdg file is shared by every harness file on this machine, so it is copied, not emptied
            self._write(self._stored("xdg"), "embedded")
        else:
            merged = self._stored("xdg")
            merged.update(self._stored("embedded"))
            self._write(merged, "xdg")
            self._write({}, "embedded")
        self.store.set_setting("credentials_storage", mode)

    def info(self):
        names = set(self._stored())
        names.update(k for k in KNOWN if os.environ.get(k))
        return {"storage": self.storage(), "path": file_path(), "names": sorted(names)}
