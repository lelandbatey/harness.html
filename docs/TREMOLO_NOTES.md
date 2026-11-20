# Tremolo notes (vendor/tremolo, v0.5.3) — verified bundled + run through the `sh` polyglot

## Running: don't use `app.run()`, await `app.serve()` in your own loop

`run()` prints a banner/route table/separator lines, inspects `__main__.__file__` (absent under
`python3 -c`, handled but pointless) and only exists to manage processes. Instead:

```python
import asyncio, contextlib, signal, socket
from tremolo import Application   # == Tremolo

app = Application()

sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", port))          # raises OSError if taken -> try next candidate port

async def amain():
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, task.cancel)
    await app.serve("127.0.0.1", port, sock=sock, log_level="WARNING",
                    server_name="SFH", shutdown_timeout=2)   # never returns until cancelled

with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
    asyncio.run(amain())
# save db here (or in an on_worker_stop hook, which runs during shutdown)
```

- `serve()` calls `sock.listen()` itself; it prints exactly one line to stdout:
  `[date] SFH worker (pid N) is started at 127.0.0.1 port P`. `log_level="WARNING"` silences
  tremolo's logger (`Shutting down`, gc messages). Logo only comes from `run()`.
- Other useful options (kwargs to serve): `client_max_body_size` (default 2 MiB — raise it for
  `/api/sql` / big messages), `ws_max_payload_size` (default 2 MiB), `request_timeout` (30 s;
  websockets auto-ping every request_timeout/2), `keepalive_timeout`, `app_handler_timeout` (120 s),
  `thread_pool_size` (5).
- Hooks: `@app.on_worker_start` / `@app.on_worker_stop` — `async def f(**server)` (kwargs include
  `app`, `loop`, `context`, `logger`). `app.loop` is just `asyncio.get_event_loop()`.
- Background tasks: `app.create_task(coro)` (tracked, cancelled on shutdown) or
  `asyncio.get_running_loop().create_task(...)`. From threads: `loop.call_soon_threadsafe(...)` /
  `asyncio.run_coroutine_threadsafe(coro, loop)` with the loop captured in worker_start.

## Handlers

**Always `async def`.** A plain `def` handler is run in tremolo's thread pool with sync wrappers.

```python
@app.route("/api/health")           # matches /api/health and /api/health?..., ANY method
async def health(request, response, **server):
    request.method                   # b"GET" / b"POST" / b"OPTIONS" (bytes)
    request.path                     # b"/api/health" (bytes, no query)
    request.query                    # {"path": ["/abs/x.html"]} — values are LISTS, already url-decoded
    request.headers.get(b"x-sfh-path", [b""])[0].decode()   # header names lowercase bytes, values lists of bytes
    request.headers.get(b"origin")   # None when absent
    raw = await request.body()       # bytes (POST body)
    response.set_header("Access-Control-Allow-Origin", "null")   # str or bytes
    response.set_status(403, b"Forbidden")
    response.set_content_type(b"application/json")
    return json.dumps(obj)            # str/bytes body; returning b"" with 204 for preflight works
```

- OPTIONS preflight: routes accept every method, so just check `request.method == b"OPTIONS"`,
  set the CORS headers, `response.set_status(204, b"No Content")`, `return b""`.
- A global place for CORS/auth: `@app.on_request` / `@app.on_response` middlewares exist
  (`async def mw(request, response, **server)`; return a truthy value from on_request to stop),
  but per-handler checks are simpler and verified.
- Path params: `@app.route("^/x/(?P<id>[^/?]+)")` regex routes put groups into kwargs.
- 404 for unknown routes is automatic. `/` has a built-in index handler unless you add a route.
- Download as attachment: `response.set_header("Content-Disposition", 'attachment; filename="x.html"')`.

## Websockets

Declare a `websocket` parameter; tremolo injects a `WebSocket` only when the request is a valid
upgrade (otherwise it stays `None`):

```python
@app.route("/ws")
async def ws(request, websocket=None, **server):
    if websocket is None:
        return b"websocket required"          # or set_status(400)
    # check request.query / Origin BEFORE accept; to reject (unverified): response.set_status(403,...); return b"no"
    await websocket.accept()
    async for msg in websocket:               # str for text frames, bytearray for binary
        await websocket.send(json.dumps(...)) # str -> text frame, bytes -> binary
    # loop ends when the client closes
```

- `send()` may be called from other tasks concurrently (e.g. event broadcast) — verified with a
  task pushing while the handler loop awaits `recv`. Wrap in try/except: sending to a closed
  socket raises (`tremolo.exceptions.WebSocketClientClosed` / `WebSocketServerClosed`, or
  ConnectionError); drop the client from your subscriber set on error.
- `await websocket.close(code=1000)` to close server-side. `websocket.receive(timeout=…)` for a
  single message.

## Bundling

`tools/bundle.py` drops only `tremolo/__main__.py` (asgi_* are lazily imported inside
`serve()` so they're kept). Modules are loaded from memory with `__file__ = "<sfh>/tremolo/…"`;
tremolo's code-reload feature (`reload=True`) must stay off.

## Known cosmetic issue

Running `sh FILE` prints `FILE: 1: cannot open !--: No such file` to stderr (from the polyglot's
`<!--` being parsed as a redirection before `2>/dev/null` applies). Harmless; bash says
`line 1: !--: No such file or directory`.
