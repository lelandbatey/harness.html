"""Terminal-in-page: bridges a websocket to a process running in a pty."""
import asyncio
import codecs
import fcntl
import json
import os
import pty
import shlex
import shutil
import signal
import struct
import termios
import time


def set_size(fd, cols, rows):
    try:
        fcntl.ioctl(fd, termios.TIOCSWINSZ, struct.pack("HHHH", max(1, int(rows)), max(1, int(cols)), 0, 0))
    except (OSError, ValueError, TypeError):
        pass


FALLBACK_EDITORS = ("nano", "pico", "vi")


class EditorNotFound(LookupError):
    pass


def resolve_editor(editor=None, env=None, which=shutil.which):
    """First runnable editor command (as argv) out of: the `editor` setting, $EDITOR, then
    FALLBACK_EDITORS. Candidates whose program isn't found on PATH are skipped; raises
    EditorNotFound if none is."""
    env = os.environ if env is None else env
    tried = []
    for cand in (editor, env.get("EDITOR"), *FALLBACK_EDITORS):
        try:
            argv = shlex.split(cand or "")
        except ValueError:
            argv = []
        if not argv:
            continue
        if which(argv[0], path=env.get("PATH")):
            return argv
        tried.append(argv[0])
    raise EditorNotFound("no editor found on PATH (tried: %s); set the Editor setting or $EDITOR"
                         % ", ".join(dict.fromkeys(tried)))


def build_argv(cmd, editor, file=None, line=None):
    if cmd == "editor":
        argv = resolve_editor(editor)
        if line:
            argv.append("+%d" % int(line))
        if file:
            argv.append(file)
        return argv
    if cmd == "shell":
        return [os.environ.get("SHELL") or "/bin/sh"]
    raise ValueError("cmd must be editor or shell")


async def bridge(websocket, argv, cwd, cols=80, rows=24):
    loop = asyncio.get_running_loop()
    if not cwd or not os.path.isdir(cwd):
        cwd = os.path.expanduser("~")
    pid, fd = pty.fork()
    if pid == 0:  # child
        try:
            os.chdir(cwd)
            env = dict(os.environ, TERM="xterm-256color", COLORTERM="truecolor")
            os.execvpe(argv[0], argv, env)
        except BaseException as e:
            os.write(2, ("cannot run %s: %s\r\n" % (argv[0], e)).encode())
        finally:
            os._exit(127)
    set_size(fd, cols, rows)
    out = asyncio.Queue()
    decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

    def readable():
        try:
            data = os.read(fd, 65536)
        except OSError:
            data = b""
        if not data:
            loop.remove_reader(fd)
            out.put_nowait(None)
            return
        out.put_nowait(decoder.decode(data))

    loop.add_reader(fd, readable)

    async def writer():
        while True:
            chunk = await out.get()
            if chunk is None:
                break
            while not out.empty():  # coalesce bursts
                nxt = out.get_nowait()
                if nxt is None:
                    out.put_nowait(None)
                    break
                chunk += nxt
            await websocket.send(json.dumps({"type": "output", "data": chunk}))
        code = await loop.run_in_executor(None, _wait, pid)
        await websocket.send(json.dumps({"type": "exit", "code": code}))

    wtask = asyncio.ensure_future(writer())
    rtask = asyncio.ensure_future(_reader(websocket, fd))
    try:
        await asyncio.wait([wtask, rtask], return_when=asyncio.FIRST_COMPLETED)
    finally:
        for t in (wtask, rtask):
            t.cancel()
        try:
            await asyncio.gather(wtask, rtask, return_exceptions=True)
        except BaseException:
            pass
        try:
            loop.remove_reader(fd)
        except Exception:
            pass
        _kill(pid)
        try:
            os.close(fd)
        except OSError:
            pass


async def _reader(websocket, fd):
    while True:
        msg = await websocket.receive(timeout=86400)
        try:
            m = json.loads(msg)
        except (ValueError, TypeError):
            continue
        if m.get("type") == "input" and isinstance(m.get("data"), str):
            os.write(fd, m["data"].encode("utf-8"))
        elif m.get("type") == "resize":
            set_size(fd, m.get("cols", 80), m.get("rows", 24))


def _wait(pid):
    try:
        _, status = os.waitpid(pid, 0)
        return os.waitstatus_to_exitcode(status)
    except ChildProcessError:
        return 0


def _kill(pid):
    try:
        if os.waitpid(pid, os.WNOHANG) != (0, 0):
            return
    except ChildProcessError:
        return
    for sig in (signal.SIGHUP, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except ProcessLookupError:
            return
        for _ in range(20):
            try:
                if os.waitpid(pid, os.WNOHANG) != (0, 0):
                    return
            except ChildProcessError:
                return
            time.sleep(0.025)
