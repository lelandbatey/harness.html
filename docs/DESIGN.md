# Design / build contract

This is the contract the implementation is built against (README.md is the product description;
this file is the engineering detail). Questions and answers are tracked in `questions/`.

## Repo layout

```
Makefile                 make build (keeps data) | make fresh | make test | make clean (never deletes dist/harness.html)
tools/bundle.py          AST bundler: python packages -> one python source file
tools/build.py           assembles dist/harness.html from template + bundle + web assets
src/template.html        HTML skeleton with {{...}} placeholders (shell header, LLM header, blocks)
src/py/sfh/              the program (python package, stdlib only + vendored tremolo)
  __main__.py            dev entry: PYTHONPATH=src/py:vendor python3 -m sfh <html-path> [cmd ...]
  main.py                main(argv): argv[0] = html path, then subcommand dispatch
  common.py              paths, port hashing, version, auth check helpers
  htmlfile.py            read/splice blocks of the .html (data block), atomic writes
  store.py               in-memory sqlite (deserialized from data block), schema, debounced save
  server.py              tremolo app: HTTP + websocket routes, RPC dispatch, event broadcast
  agent.py               session turn loop: LLM streaming, tool execution, queueing, stop
  llm.py                 Anthropic Messages streaming client + deterministic mock provider
  tools.py               agent tools (bash, read, write, edit, glob, grep, skill)
  skills.py              AGENTS.md + skills discovery, built-in self-edit skill
  commands.py            slash commands (/help /retitle /btw /title /new ...)
  creds.py               credentials ($XDG_CONFIG_HOME/singlefile/credentials or embedded)
  pty_ws.py              terminal-in-page: pty <-> websocket bridge
  wsclient.py            minimal stdlib websocket client (used by tui + cli)
  tui.py                 the minimal TUI client
src/web/                 browser client (plain JS, no frameworks, no build step besides concat)
  body.html              markup inserted into <body>
  app.css
  markdown.js            streaming-friendly GFM renderer + raw-HTML sanitizer -> window.SFHMarkdown
  identicon.js           semantic identicons             -> window.SFHIdenticon
  app.js                 the client app (loaded last)
vendor/tremolo/          vendored tremolo package (MIT, LICENSE.txt kept)
vendor/xterm/            vendored @xterm/xterm (xterm.js, xterm.css, LICENSE) + addon-fit
test/                    python unittest (test_*.py) + node/puppeteer browser tests; test/run.py runs all in parallel
```

Not allowed: node at build time, npm deps, JS frameworks. Node + puppeteer are allowed only for the
optional browser test (borrow `../local_sqlite_selfcontained_html/node_modules/puppeteer-core`).

## The built file: dist/harness.html

```
<!-- 2>/dev/null; exec python3 -c '<bootstrap>' "$0" "$@"
-->
<!DOCTYPE html>
<html><head>
<meta charset="utf-8"><title>…</title>
<!--  LLM-HEADER: what this file is, how to run it, how to extract/inspect things
      (`sh FILE help`, `sh FILE extract-program DIR`, `sh FILE sql "SELECT …"`, …) -->
<style>{{CSS}}</style>
</head><body>
{{BODY}}
<script type="text/x-python" id="sfh-program">
#--SFH-PROGRAM-BEGIN--
{{BUNDLED PYTHON}}
#--SFH-PROGRAM-END--
</script>
<script>{{JS: vendor/xterm/xterm.js, addon-fit.js, markdown.js, identicon.js, app.js}}</script>
<!--SFH-DATA-BEGIN--><script type="application/octet-stream" id="sfh-data" data-encoding="gzip+base64">BASE64</script><!--SFH-DATA-END-->
</body></html>
```

- The bootstrap (inside the leading HTML comment; must not contain `-->` or `'`) reads `$0`, takes
  the text between the LAST `#--SFH-PROGRAM-BEGIN--`/`#--SFH-PROGRAM-END--` lines, and `exec`s it
  with `__name__ == "__main__"` and `sys.argv = [html_path, *args]`. stdin stays a TTY (TUI needs it).
- Data block = `base64(gzip(serialized sqlite db))`, located by the LAST
  `<!--SFH-DATA-BEGIN-->`/`<!--SFH-DATA-END-->` pair. Empty content = empty db.
- Inline-safety: no `</script`, `<!--`, `<script` inside inline scripts; build fails if found.
  (Python sources: bundler escapes these in string literals; JS: build splits them.)
- File mode 0755 so `./harness.html` also works via sh (no shebang: `sh harness.html` is canonical).

## Program CLI (`sh harness.html [cmd]`)

| cmd | |
|---|---|
| *(none)* / `serve` | run the server (foreground). Exits with a message if this file's server already runs. |
| `tui [--list] [--resume ID] [--cwd DIR]` | TUI client; starts a background server if none is running |
| `help` | usage, including the extraction instructions |
| `info` | path, port, db size, session count, whether server running |
| `sql "QUERY" [--write]` | run SQL. If server is running, goes through it (`POST /api/sql`) so in-memory state stays authoritative; else operates on the embedded db directly and (with --write) saves |
| `db-extract OUT.sqlite` / `db-inject IN.sqlite` | raw db in/out (inject refuses while server runs) |
| `extract-program DIR` | writes the unbundled python sources (`sfh/…`, `tremolo/…`) + web assets to DIR |
| `settings [get KEY \| set KEY JSON]` | read/write settings (via server if running) |
| `export OUT.html [--view session:ID]` | self-export with current data |

## Ports and auth

- `path` = absolute unix path of the html file. Browser: `decodeURIComponent(location.pathname)`;
  Python: `os.path.abspath(html)`.
- FNV-1a 32-bit over UTF-8 bytes of path → `h`. Candidate ports: `47100 + ((h + i) % 800)` for
  i in 0..7. Server binds the first free candidate. Clients probe candidates in order with
  `GET /api/health?path=…` and use the first whose JSON response `{"app":"singlefile-llm-harness",
  "path": …}` matches their path.
- Every request must identify the path: header `X-SFH-Path` (HTTP from the browser; triggers a
  CORS preflight which the server answers) or query `?path=` (websocket, health, export). Mismatch → 403.
- `Origin` must be absent (CLI/TUI), `null`, or `file://`; anything else → 403.
- CORS response headers: `Access-Control-Allow-Origin: null` (echo the request Origin when it is
  `null`/`file://`), `Access-Control-Allow-Headers: content-type, x-sfh-path`,
  `Access-Control-Allow-Methods: GET, POST, OPTIONS`.

## HTTP endpoints

- `GET /api/health?path=` → `{"app":"singlefile-llm-harness","path","pid","version","port"}`
- `GET /api/export?path=&view=session:ID` → `text/html` attachment, a copy of the current file
  with the current db in its data block and the setting `ui.initial_view` set to `view`
  (credentials are never included unless `credentials_storage == "embedded"`... see QUESTIONS).
- `POST /api/sql` body `{"sql": "...", "params": [], "write": bool}` → `{"columns": [...], "rows": [...]}`
- `WS /ws?path=` → the RPC + event channel below
- `WS /pty?path=&cmd=editor|shell&file=&line=&cwd=&cols=&rows=` → terminal bridge

## Websocket protocol (`/ws`)

All frames are JSON text.

Client → server: `{"type":"rpc","id":N,"method":"…","params":{…}}`
Server → client: `{"type":"rpc_result","id":N,"result":…}` | `{"type":"rpc_error","id":N,"error":"msg"}`
Server → client: `{"type":"event","event":"…","data":{…}}`

### Objects

Session summary:
```json
{"id":"s_ab12cd34","title":"…","cwd":"/abs","created_at":1731900000000,"updated_at":…,
 "subjects":["python","websockets","auth"],"status":"idle"|"running","message_count":12,"model":"…"}
```
Message (content blocks use Anthropic Messages API shapes):
```json
{"id":42,"session_id":"s_…","role":"user"|"assistant","created_at":…,
 "content":[{"type":"text","text":"…"},
            {"type":"thinking","thinking":"…","signature":"…"},
            {"type":"tool_use","id":"toolu_…","name":"bash","input":{…}},
            {"type":"tool_result","tool_use_id":"toolu_…","content":"text","is_error":false}]}
```
A user message whose content is only `tool_result` blocks is a tool-output message: clients render
those results under the matching `tool_use`, not as a user bubble.

Draft (in-progress assistant message): `{"turn_id":"t_…","content":[blocks so far]}`; for a
`tool_use` block being streamed, `input` is absent and `partial_json` holds the raw string so far.

### RPC methods

| method | params | result |
|---|---|---|
| `hello` | `{client:"web"\|"tui", cwd?}` | `{version, path, port, settings, sessions:[summary], initial_view, provider, has_api_key, commands:[{name,description}]}` |
| `sessions.list` | | `[summary]` (newest updated first) |
| `sessions.create` | `{cwd?}` | summary (default cwd: the dir of the html file… see QUESTIONS; tui passes its cwd) |
| `sessions.subscribe` | `{session_id}` | `{session, messages:[message], draft:draft\|null, queue:[text]}` |
| `sessions.unsubscribe` | `{session_id}` | `true` |
| `sessions.rename` | `{session_id, title}` | summary |
| `sessions.delete` | `{session_id}` | `true` |
| `chat.send` | `{session_id, text}` | `{queued:bool}`; `/command` texts are handled as commands |
| `chat.edit` | `{session_id, message_id, text}` | `true`: deletes that user message and everything after, then sends `text` |
| `chat.stop` | `{session_id}` | `true` |
| `settings.get` | | settings object |
| `settings.set` | `{key, value}` | settings object |
| `creds.get` | | `{storage:"xdg"\|"embedded", path, names:[…]}` (never values) |
| `creds.set` | `{name, value}` (value null/"" deletes) | same as creds.get |
| `creds.storage` | `{mode:"xdg"\|"embedded"}` | same as creds.get (moves existing creds) |
| `skills.list` | `{session_id}` | `[{name, description, source, path}]` |
| `fs.resolve` | `{session_id, path}` | `{path:abs, exists, is_dir}` (for clickable file links) |

### Events

| event | to | data |
|---|---|---|
| `session.updated` | all clients | summary |
| `session.deleted` | all clients | `{session_id}` |
| `settings.updated` | all clients | settings |
| `message.added` | subscribers | message |
| `messages.truncated` | subscribers | `{session_id, from_id}` (delete messages with id >= from_id) |
| `draft.start` | subscribers | `{session_id, turn_id}` |
| `draft.block` | subscribers | `{session_id, turn_id, index, block}` (new block; text/thinking empty, tool_use without input) |
| `draft.delta` | subscribers | `{session_id, turn_id, index, text?, thinking?, partial_json?}` (append) |
| `draft.end` | subscribers | `{session_id, turn_id}` (final message arrives as message.added just before) |
| `queue.updated` | subscribers | `{session_id, queue:[text]}` |
| `btw.start` / `btw.delta` / `btw.end` | subscribers | `{session_id, btw_id, question?, text?, error?}` — ephemeral, not stored |
| `notice` | subscribers (or the requesting client) | `{session_id?, level:"info"\|"error", text}` (markdown) |

## Server behaviour

- One server per html file, any number of clients, any number of concurrently running sessions.
- asyncio (tremolo, `worker_num=0` single-process). Blocking work (LLM HTTP, tools, sqlite saves
  of large dbs) runs in threads; results are marshalled back with `loop.call_soon_threadsafe`.
- One turn at a time per session; `chat.send` while running appends to that session's in-memory
  queue (processed FIFO after the turn). `chat.stop` cancels the running turn (stream closed, partial
  assistant output kept as the message, tool_use blocks without results get an
  "interrupted" tool_result so history stays valid) and clears the queue.
- Turn loop: stream LLM → on `stop_reason == "tool_use"` run tools, store tool_result user message,
  loop (max 300 iterations: "tool iterations are limited to 300 unattended turns").
- System prompt = settings `system_prompt` (+ env info: cwd, date, platform, html path) + AGENTS.md
  files (walk from session cwd up to `/`, outermost first) + skill index (name + description +
  how to load via `skill` tool). Viewable/editable in settings UI.
- Skills: directories `<cwd>/skills/*/SKILL.md`, `<cwd>/.opencode/skills/*/SKILL.md`,
  `<cwd>/.claude/skills/*/SKILL.md` in that precedence (first name wins). YAML-ish frontmatter
  `name:` / `description:`. Plus built-in `singlefile-harness` skill (self-edit instructions;
  disabled when setting `builtin_skill_enabled` is false).
- Title + subjects: after the first user message of a session, call `cheap_model` once asking for
  JSON `{"title": "...", "subjects": ["broad domain", "narrower topic", "specific thing"]}` (3
  subjects ordered general → specific; count = setting `identicon_subjects`). `/retitle` redoes it
  with the whole conversation. Fallback (no key / failure): first ~6 words as title, keyword
  extraction as subjects.
- Persistence: db changes mark dirty; save to the html (splice data block, atomic rename,
  preserve mode) debounced 1.5 s, and on shutdown. Save re-reads the file and only splices the data
  block, so a rebuilt program in the file is kept.

### Settings (stored in db `settings` table, JSON values)

| key | default |
|---|---|
| `provider` | `"auto"` (anthropic if an API key exists, else `mock`) — also `"anthropic"`, `"mock"` |
| `model` | `"claude-sonnet-5-5"` |
| `cheap_model` | `"claude-haiku-4-5-20251001"` |
| `max_tokens` | `16000` |
| `thinking_budget` | `4000` (0 disables extended thinking) |
| `model_overrides` | `{}` — per-model overrides, see v9 |
| `system_prompt` | a sensible coding-agent prompt |
| `builtin_skill_enabled` | `true` |
| `editor` | `""` (empty: `$EDITOR`, then `nano`, `pico`, `vi`) |
| `identicon_subjects` | `3` |
| `credentials_storage` | `"xdg"` |
| `ui.initial_view` | `null` (e.g. `"session:s_ab12cd34"` or `"sessions"`) |
| `tui_mouse` | `true` — TUI mouse reporting (wheel scrolls the transcript), see v13 |

### Mock provider (deterministic, used by tests and when no API key)

- Streams a thinking block `"Considering: <last user text>"`, then a text block
  `"**Mock reply** to: <last user text>"` in small chunks (~20ms apart).
- If the last user text contains `[[bash:CMD]]`, it first emits a `tool_use` bash `{command: CMD}`
  (streamed partial_json), stop_reason `tool_use`; after the tool result it replies with text
  `"Tool said: <first 200 chars of result>"`.
- If it contains `[[slow]]`, chunks are 300ms apart (for stop/queue/live-sync tests).
- Title: first 6 words; subjects: keyword extraction.

### Credentials

`$XDG_CONFIG_HOME/singlefile/credentials` (default `~/.config/…`), mode 0600, `KEY=value` lines
(e.g. `ANTHROPIC_API_KEY=…`). Environment variables override file values. With
`credentials_storage == "embedded"` they live in the db `credentials` table instead (so they travel
with the file). Never sent to clients.

## DB schema

```sql
CREATE TABLE sessions(id TEXT PRIMARY KEY, title TEXT, cwd TEXT, created_at INT, updated_at INT,
                      subjects TEXT /*json*/, model TEXT);
CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT /*json*/,
                      created_at INT);
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT /*json*/);
CREATE TABLE credentials(name TEXT PRIMARY KEY, value TEXT);
CREATE TABLE files(name TEXT PRIMARY KEY, mime TEXT, data BLOB, created_at INT);
CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);  -- schema_version
```

## Terminal-in-page (`/pty`)

`cmd=editor` runs `<editor> +<line> <file>` in a pty. The editor is the first of: the `editor`
setting, `$EDITOR`, `nano`, `pico`, `vi` whose program is on `PATH`; if none is, the terminal shows
an error and exits 127 without spawning anything
(`pty.fork`), cwd = given cwd. `cmd=shell` runs `$SHELL`. All frames are JSON text.
Server → client: `{"type":"output","data":"…"}` (utf-8, errors=replace) and
`{"type":"exit","code":N}`. Client → server: `{"type":"input","data":"…"}` /
`{"type":"resize","cols":N,"rows":N}`.

## Browser client

- Opened as `file://`. On load: compute candidate ports, probe health; if not found, show the
  "not running" screen with the copyable command `sh '<path>'` and keep probing every 2 s.
- Connected: left sidebar (collapsible with Ctrl/Cmd+B, resizable by dragging, v14) listing sessions (identicon 16px + title + status dot),
  main chat view, "All sessions" grid view, settings view (all settings incl. system prompt,
  credentials management, export button). Hash routing: `#session/<id>`, `#sessions`, `#settings`.
- Chat: messages rendered with SFHMarkdown; thinking blocks in a distinct (italic/mono, dimmed)
  collapsible style; tool calls as collapsible cards showing input + result. Draft streams live.
- Composer: textarea, Enter sends, Shift+Enter newline, ArrowUp in empty composer edits the last
  user message; each user message has an "edit" action → inline edit → `chat.edit`. Esc stops.
  `/` at start shows command completion from `hello.commands`.
- Live sync: everything is driven by server events, so two tabs on the same session mirror.
- Local file links (absolute paths, `file://` links, relative paths resolved against session cwd,
  `path:line` in inline code) → click opens bottom terminal panel (xterm.js) running the editor via
  `/pty`. Panel closable/resizable.
- Export: settings → "Export" downloads `/api/export?view=<current view>`.

## TUI client

- `sh harness.html tui` → connects (spawning `sh harness.html serve` detached in background with
  output to `$XDG_STATE_HOME/singlefile/<hash>.log` if needed), creates a session with cwd = current
  directory, alternate screen, scrolling transcript + prompt editor at bottom.
- Line editor keys: ctrl-a/ctrl-e line start/end, ctrl-j newline, Enter submit, arrows move,
  Up on first line → previous history message, Down on last line → next, ctrl-c (empty prompt) /
  ctrl-d exit, Esc stop running turn, backspace/delete, ctrl-k/ctrl-u kill, PgUp/PgDn scroll, mouse
  wheel scroll (v13), ctrl-z suspend.
- Thinking always shown, in a different "font": dim + italic SGR. Tool calls shown compactly.
- `/sessions` lists, `/resume ID` switches, `/edit` edits previous user message (loads it into the
  prompt; submit sends `chat.edit`). Server-side slash commands pass through.

## Deviations / clarifications (server)

- (Superseded by v2 "Export with embedded credentials": `/api/export` keeps the `credentials`
  table only when `credentials_storage == "embedded"`, and then always sends `X-SFH-Warning`, even
  if the table is empty. `X-SFH-Warning` is listed in `Access-Control-Expose-Headers`.)
- v2 server notes:
  - Files without a `sfh-token` meta tag (built before v2) get one inserted into `<head>` on the
    first save. Every server start does one save (token + offline block written immediately).
  - Pairing token is only returned when health is called WITH a matching `?path=` (and an ok Origin).
  - `auth.get.generated_at` is stored in `meta.token_generated_at` (null for tokens the server did
    not generate). Regenerate also closes `/pty` websockets with 4001.
  - Exported copies get a freshly computed offline block (not the live file's last one).
  - Offline `settings` = `model, cheap_model, identicon_subjects, editor, default_cwd, ui.initial_view`.
  - `message_count` in summaries counts the displayed branch only. `versions` counts sibling user
    prompts (tool-output messages are excluded). Editing a message that is not on the displayed
    branch is an error. Editing the first message re-runs title generation (thread has no user msg).
  - Providers: besides `env` and configured ones, `providers.list` always includes built-ins
    `anthropic` (key credential `ANTHROPIC_API_KEY`, base `ANTHROPIC_BASE_URL`) and `mock`, so
    `provider: "anthropic"`/`"mock"` keep working. Configured providers' `type` defaults to `openai`.
    Unknown provider id → turns fail with an error notice; `hello.provider` then carries `error`.
  - OpenAI type: `thinking_budget` is not sent (no portable switch); `max_tokens` is sent. Tool
    calls with `finish_reason: "stop"` are still treated as `tool_use`. Error tool results are sent
    as `"ERROR: <text>"` tool messages.
  - `AI_TOKEN` is read through creds (env wins, else a stored credential named `AI_TOKEN`).
- `creds.storage` xdg → embedded copies the values into the db but leaves the xdg file alone (it is
  shared by every harness file on the machine); embedded → xdg merges into the file and empties the table.
- `chat.send` texts starting with `/` that are not a known command are sent as normal chat text.
  `/help`, `/skills`, `/system`, `/model` (no arg) notices go only to the requesting client.
- Any `POST /api/sql` with `write: true` broadcasts `settings.updated` (so `sh FILE settings set`
  reaches open clients).
- `Access-Control-Allow-Private-Network: true` is added to CORS responses (Chrome PNA preflights).

## v2 changes (owner answers, see questions/)

### Access token
- The token lives in the html itself: `<meta name="sfh-token" content="TOKEN">` (first occurrence,
  in `<head>`). Helpers: `htmlfile.decode_token/encode_token/read_token`.
- Server start: if the file's token is empty, generate `secrets.token_urlsafe(24)`, splice it into
  the file, and open a **pairing window** of 5 minutes. During the window `GET /api/health?path=P`
  (path matching, origin ok) also returns `"pairing_token": TOKEN`, so a page that was loaded before
  the token existed can still connect. No pairing window when the file already had a token.
- Every endpoint except health requires the token: header `X-SFH-Token` (HTTP) or query `token=`
  (websocket, export link). Wrong/missing → 401 (websocket: refuse the upgrade with 401).
  CORS `Access-Control-Allow-Headers` adds `x-sfh-token`.
- Browser: token = meta content, else health's `pairing_token`; if neither → screen "This page
  has no access token yet — reload the page" (the file now contains it). A 401 or a websocket close
  with code 4001 → screen "Access token changed — reload this page".
- RPC `auth.get` → `{token, generated_at}`; RPC `auth.regenerate` → writes a new token into the
  file, replies `true`, then closes every websocket (including the caller) with code 4001 reason
  `token-changed`. No pairing window after a regenerate (clients reload the file to get it).
- CLI/TUI read the token from the file (`htmlfile.read_token`) and send it. CLI: `sh FILE token`
  prints it, `sh FILE token regenerate` (via server if running, else directly in the file).
- Export: the copy gets an EMPTY token (it generates its own on first run).

### Default cwd
- Setting `default_cwd` (default `null` = directory of the html file). Used by `sessions.create`
  without cwd. Editable in settings UI and via `settings set`.

### Edit history (branches)
- `messages.parent_id INTEGER` (previous message in its branch; NULL for the first) and
  `sessions.head_id INTEGER` (last message of the displayed branch). Migration: existing messages
  chain by id order per session.
- The displayed thread = walk from head_id via parent_id. `chat.edit` no longer deletes: it adds a
  new user message whose parent is the edited message's parent, sets head to it and runs a turn.
  Old branch stays in the db. Protocol to clients is unchanged (`messages.truncated {from_id}` still
  means "drop displayed messages with id >= from_id", then new ones arrive via `message.added`).
- Message objects gain `parent_id` and, for user messages, `versions` (count of sibling user
  messages sharing the same parent, incl. itself) so clients can show "edited · 2 versions".
  Switching branches in the UI is future work.

### Read-only offline view
- On every save the server also writes an offline snapshot block between the LAST
  `<!--SFH-OFFLINE-BEGIN-->`/`<!--SFH-OFFLINE-END-->` markers (just before the data block):
  `base64(gzip(JSON))`, JSON = `{"saved_at", "version", "sessions":[summary], "messages":
  {session_id: [message, ...displayed branch]}, "settings": {non-secret ui-relevant settings}}`.
  Helpers: `htmlfile.encode_offline/decode_offline`. Data + offline + token splices happen in one
  atomic write.
- Browser, when the server isn't reachable: decode the block with `DecompressionStream("gzip")`
  and show the sidebar/sessions/chat views read-only (composer hidden, banner "Offline — read only,
  start the server with `sh '<path>'`"), keep probing; switch to live when the server appears.

### Export with embedded credentials
- If `credentials_storage == "embedded"`, export now INCLUDES the credentials table, and warns:
  response header `X-SFH-Warning: export contains embedded credentials`; web UI asks to confirm
  before downloading; CLI prints the warning to stderr. (xdg-stored credentials are never exported.)

### Providers (OpenAI-compatible is the default)
- Provider types: `openai` (Chat Completions API, streaming), `anthropic` (Messages API), `mock`.
- Built-in provider `env`, present when `$AI_ENDPOINT` and `$AI_MODEL` are set: type openai,
  `base_url=$AI_ENDPOINT` (e.g. `http://127.0.0.1:3456/v1`), `model=$AI_MODEL`, token `$AI_TOKEN`,
  `cheap_model=$AI_CHEAP_MODEL` or else `$AI_MODEL`. Not stored; shown read-only in settings.
- Setting `providers`: list of user-configured providers
  `{"id","type","base_url","model","cheap_model","api_key_credential"}` (the key itself lives in
  credentials under that name; never in settings).
- Setting `provider`: `"auto"` (env → first configured provider → anthropic if
  `ANTHROPIC_API_KEY` → mock) or a provider id. Settings `model`/`cheap_model` become overrides
  (default `null` = the provider's).
- OpenAI streaming: map `delta.content` → text, `delta.reasoning_content` / `delta.reasoning` /
  `delta.thinking` → thinking block (no signature), `delta.tool_calls[]` (index, id,
  function.name, function.arguments fragments) → tool_use blocks with partial_json;
  `finish_reason "tool_calls"` → stop_reason tool_use. History conversion: tool_use →
  assistant `tool_calls`, tool_result → `role: "tool"` messages, thinking dropped; system prompt as
  first `system` message. Tools as `{"type":"function","function":{name,description,parameters}}`.
- `hello` result `provider` becomes `{"id","type","model","cheap_model"}`; RPC `providers.list` →
  `[{id,type,base_url,model,cheap_model,builtin:bool,has_key:bool}]`.

### Small things
- Identicons are plain squares (no rounded tile).
- TUI `/resume` with no argument opens an interactive picker (arrow keys/enter/esc, type to
  filter, identicon-ish colored marker optional).

## v3: subagents + webfetch tools (README "Tools")

### `subagent` tool
- Input: `{"description": "3-5 words", "prompt": "full task", "session_id"?: "continue an existing
  child"}`. Returns IMMEDIATELY with `{"session_id", "status": "started"}` text; the child runs
  concurrently in the background (no limit on parallel children).
- A child is a normal session with `sessions.parent_id` (and `sessions.kind = "subagent"`), same
  cwd and provider, same tools EXCEPT `subagent` (no recursion). Its system prompt adds a note that
  it is a subagent and its final message is returned to the parent.
- When a child's turn finishes (idle, queue empty) the parent is signalled: a user message is
  appended to the parent's displayed branch with text
  `<subagent-result session_id="…" description="…" status="completed|error|stopped">FINAL TEXT</subagent-result>`
  (content block `{"type":"text","text":…,"subagent_result":true}` meta allowed), delivered through
  the parent's queue mechanism: if the parent is idle a new parent turn starts; if busy, it runs
  after the current turn. Stopping the parent does not stop children; `chat.stop` on a child works.
- Session summary gains `parent_id` and `kind` ("chat"|"subagent"), and `children` count.
  `sessions.list` includes children (clients nest them under the parent).
- Events: existing `session.updated` covers child status changes.

### `webfetch` tool
- Input: `{"url", "format": "markdown"|"text"|"html" (default markdown), "timeout"?: seconds (max 120)}`.
  http/https only. urllib with a browser-ish UA; follows redirects; decodes charset; HTML → markdown
  via a stdlib `html.parser` converter (headings, links, lists, code/pre, tables roughly; drops
  script/style/nav noise); output capped ~100 KB with a truncation note. Non-HTML text returned as
  is; binary → error.

## v4: model switching, retries, file links, session working directories (README additions)

### Models and providers per session
- `sessions.provider` (NULL = follow the global `provider` setting) and `sessions.model` (NULL = that
  provider's default). Changing the provider without a model resets the model. `llm.resolve_provider(
  settings, creds, provider_id)`: the global `model`/`cheap_model` overrides only apply to the
  globally selected provider. Choices apply at the next LLM call (also mid-turn); title generation and
  `/btw` use the session's provider. Subagent children inherit the parent's choice.
- Summary: `provider`, `model` = effective values; `provider_override`, `model_override` = the stored
  choice; `provider_error` when the stored provider no longer exists. Changing provider settings or
  credentials re-broadcasts `session.updated` for every session.
- Every message records `model`, `provider` (assistant: what generated it; user/tool results: what
  the session used then). Migration 4 backfills `messages.model` from `sessions.model`.
- Signed thinking blocks from a different model are dropped from the history sent to Anthropic.
- RPC `sessions.set_model {session_id, provider?, model?}` → summary (keys that are absent are kept;
  null = default). RPC `models.list {provider? | session_id?, refresh?}` → `{provider, models:[{id,
  name?}], cached, error?}`: `GET {base_url}/models` (OpenAI-compatible) or `/v1/models` (Anthropic),
  cached 5 min per provider; errors are returned, not raised (the UI still allows typing an id).
- Commands: `/model [id|default]`, `/provider [id|default]`, `/models`.

### Timeouts and retries
- Settings `request_timeout` (30 s: connect + wait for the response to start), `stream_idle_timeout`
  (120 s: silence once the answer is streaming; longer because proxies may send nothing while the
  model thinks), `max_retries` (3), `retry_base_delay` (2 s).
- Retryable: timeouts, connection failures/resets, HTTP 408/409/425/429/5xx/529, Anthropic stream
  `overloaded_error`/`api_error`/`rate_limit_error`. Not retryable: other 4xx (bad request, auth).
- `llm.RetryingStream`: retry n waits `min(30, base·2^(n-1)) · uniform(0.5, 1.5)`; before waiting it
  yields `("retry", n, max, delay, error)`. A failed attempt's partial output is discarded: the agent
  emits `draft.end` + a new `draft.start` and an info `notice` (`retry: true`); `/btw` sends
  `btw.delta {reset: true}`. After the last retry the error says "gave up after N attempts".
  Title generation uses the same policy (`llm.call_with_retries`). `chat.stop` interrupts a wait.
- Mock: `[[flaky:N]]` makes the first N attempts fail mid-stream (after the thinking block).

### Local file links
- When a message is stored, `links.for_content` (src/py/sfh/links.py) scans its text and thinking
  blocks for inline code that could be a path and for markdown link/autolink/reference targets,
  resolves them against the message's cwd, and stores the ones that exist in `messages.links`:
  `{path-as-written: {"path": abs, "dir": bool}}` (a bare name in inline code only counts when it is a
  file, so `src` is not a link but `Makefile` is). Keys use markdown.js's normalisation (line suffix
  `:12`/`#L12` split off, `file://` stripped, hrefs percent-decoded).
- `SFHMarkdown.render(text, {links})`: with a map, only paths in it become `.local-path` links
  (`data-abs`, `data-dir`); local markdown links to missing paths render as inert
  `a.local-missing`. Drafts render with `links: {}` (links appear once the message is stored).
  Messages from before v4 have no `links` and keep the old heuristic + resolve-on-click behaviour.
- Clicking resolves via `fs.resolve {session_id, path, cwd}` with the message's cwd (toast when the
  file was deleted since) and opens the editor with that cwd.

### Working directories
- `messages.cwd`: the session's cwd when the message was stored (migration 4 backfills it).
- RPC `sessions.set_cwd {session_id, cwd}` (relative to the current cwd; must be a directory) →
  summary + an info notice. Applies from the next LLM call / tool run (the system prompt's env info
  and tools use it). Command `/cd <dir>`. The chat view shows a divider where the cwd or the model
  changes between messages; each message's tooltip shows its model, provider, cwd and time.
- New-session default (`cwd.info.default`, used by `sessions.create` without cwd): the cwd of the
  last session a client started (`meta.last_new_session_cwd`; subagents don't count), else the
  `default_cwd` setting, else the directory the server process runs in (`server_cwd`).
- `recent_cwds(path, used_at)` table, updated by `sessions.create` and `sessions.set_cwd`; RPC
  `cwd.info` → `{default, server_cwd, home, recent:[{path, used_at, exists}]}` (most recent first).
- RPC `fs.list {path?, hidden?}` → `{path, parent, exists, dirs:[name], truncated, hidden_count,
  error?}` (sub-directories only; a file path lists its directory; default path: the new-session
  default).
- Browser: a folder-picker dialog (places + recent on the left, path bar, folder list with keyboard
  navigation, "Use this folder") is used by the new-session view (Browse… + recent chips), the chat
  header (click the cwd) and the `default_cwd` setting. A model-picker dialog (provider select +
  filterable model list from `models.list`, free-text ids) is used by the chat header's model badge
  and the new-session view.

## v5: cost tracking, 300 iterations, data-preserving rebuilds, live favicon, identicon context

### Usage and cost
- Streams emit `("usage", u)` before `("stop", …)`; `u = {input_tokens (uncached), output_tokens,
  cache_read_tokens, cache_write_tokens, cost?}` (`llm.normalize_usage`). Anthropic: `message_start`
  + cumulative `message_delta` usage. OpenAI-compatible: requests send `stream_options:
  {include_usage: true}`; the final chunk's `usage` (`prompt_tokens`, `completion_tokens`,
  `prompt_tokens_details.cached_tokens/cache_write_tokens`). `cost` (USD) only when the provider
  reports one (`usage.cost`/`total_cost`/`cost_usd`, e.g. OpenRouter); there is no price table. The mock
  reports $1 per million tokens so the UI path is testable.
- `messages.usage` = that response's usage (assistant messages). `sessions.usage` = running total of
  every provider call made for the session (turn steps incl. abandoned branches, title generation,
  `/btw`): `{…token keys, cost, calls, priced_calls}` (`llm.add_usage`). Summary: `usage`, plus
  `usage_subagents` (sum of the children's totals). Each call re-broadcasts `session.updated`.
- Display (web: bottom-right of the chat view, under the composer; TUI: right end of the footer):
  `$0.0123 · 12.3k in · 2.1k out` incl. subagents; `cost n/a` when no response reported a cost, a
  trailing `+` when only some did. Web tooltip: breakdown; message tooltips show that response's usage.

### Limits
- `MAX_ITERATIONS = 300` LLM calls per user message ("tool iterations are limited to 300 unattended turns").

### Rebuilds keep data
- `tools/build.py -o OUT`: when OUT exists and has data, the new file gets its data block, offline
  snapshot and access token; the old file is kept as `OUT.prev`. Unreadable data → the build fails
  instead of overwriting. `--fresh` (`make fresh`) builds an empty file. `make build` always rebuilds
  (a running server rewrites OUT, so mtimes are meaningless). A running server keeps working; its next
  save splices its in-memory db into the new file (program changes need a restart).
- Tests never touch `dist/harness.html`: test_build and the browser test build their own empty copies.

### Favicon while running
- `SFHIdenticon.svg(session, size, {spin: 0..1})` adds a bright segment travelling around the tile's
  edge; the identicon is unchanged. While the open session is running the web client cycles 12
  precomputed frames every 120 ms (background tabs are throttled to ~1/s, still visibly changing).

### Identicon subjects: domain > topic > specific, with context
- The title/subjects prompt asks for `[domain, topic, specific]` (n = `identicon_subjects`, default 3)
  and lists up to 25 recent other sessions (title | cwd | subjects; same cwd first), instructing the
  model to reuse a related session's domain exactly, and its topic too for the same project/directory.
- Fallback without an LLM: if a recent session in the same cwd has subjects, its domain + topic are
  reused and the specific subject comes from keyword extraction.

## v6: /compact and the context gauge

### `/compact [instructions]`
- Allowed when the session is idle and there is something since the last compaction. Runs as the
  session's task (status `running`, `chat.send` queues, `chat.stop` cancels and changes nothing): the
  session's own provider/model gets the history the model currently sees plus a summarization prompt
  (`agent.COMPACT_PROMPT`, + "Additional instructions from the user: …"), no tools, no thinking,
  retried like any stream. The summary streams as a normal draft (`draft.start/block/delta/end`).
- Result: a marker message on the displayed branch, `role: "user"`, content
  `[{"type": "text", "text": SUMMARY, "compaction": true, "compacted": N}]` (N = messages it replaces),
  then a notice. Its usage counts towards the session's cost. Queued messages run afterwards.
- `agent.llm_history(sid)` = what every later LLM call (turns, `/btw`, the next `/compact`) gets: the
  displayed branch from the LAST marker on, the marker turned into a user message
  `<conversation-summary>…</conversation-summary>`. Clients still show everything: web renders the
  marker as a collapsible "Conversation compacted" card and dims the messages above it; the TUI shows
  a box with the first lines. Markers are not prompts: no "edit", not counted as versions, skipped by
  ↑/`/edit`. Editing a message above a marker branches off without it (full history again).
- Mock: `[[history]]` makes the reply "History: N messages; first: …" (what the model was given).

### Context gauge
- Summary `context: {used, window, estimated}` (absent when the provider is unknown). `used` = the
  last response's input + cache + output tokens since the last compaction; right after a compaction
  it is estimated (summary length/4 + the first response's input as the system prompt + tools size),
  `estimated: true`; without any response that reported usage (messages recorded before v5) it is
  estimated from the history's JSON length/4; `used: null` only for an empty session (shown as
  `context – / 1M`). `window` = the model's `context_window` from the provider's model list
  (`list_models` reads `context_window`/`context_length`/`max_context_length`/`max_input_tokens`/
  `top_provider.context_length`; ids also match with a provider prefix, `anthropic/claude-x` ↔
  `claude-x`). The server fetches a provider's list in the background the first time it is needed and
  re-broadcasts that provider's sessions (failures: retried after 5 min). Mock window: 200k.
- Display next to the cost: web bottom-right `context 45.2k / 200k (23%)` (amber ≥ 80%, red ≥ 95%,
  tooltip suggests `/compact`); TUI footer `ctx 45k/200k 23%`. `~` marks estimates.

## v7: second automatic title, model filtering everywhere

- Titles and identicon subjects are generated at the first user message and again once that first
  exchange is over (the run loop that started with the first message ends and control is back with
  the user; also after a stop or an error). Later exchanges don't retitle (`/retitle` does). Each
  `retitle` call takes a per-session generation number and only the newest one stores its result, so
  a slow first-message call can't overwrite the post-exchange one. Editing the first message starts a
  new root branch, which counts as a first exchange again.
- Choosing from the provider's models with a filter: web model picker (filter box); TUI `/model` with
  no argument opens an interactive picker (type to filter id or display name, ↑/↓, Enter, Esc; the
  first entry is "provider default"); `/models [filter]` lists matching models in any client.

## v8: cost estimates from the provider's price list

- When a response's usage has no cost, the harness prices it from the provider's price list:
  `GET {origin of base_url}/settings/api/pricing` (then `{base_url}/settings/api/pricing`). A provider
  can set `pricing_url` in the `providers` setting (`""` turns the lookup off); `$AI_PRICING_URL` does the
  same for the env provider.
- `llm.parse_pricing` reads `{"builtin": {model: {inputPerMTok, outputPerMTok, cacheReadPerMTok,
  cacheWritePerMTok}}, "overrides": {...}}` (overrides win field by field), and also accepts a flat map or a
  list of `{model|id, ...}`. `llm.find_price` matches the id exactly, then without a `vendor/` prefix, a
  `[1m]` suffix or a `-YYYYMMDD` date suffix (case-insensitive). If a cache price is missing, the input
  price is used.
- `Server.pricing(provider)` caches the table per provider: 1 h, or 5 min after a failure or an empty
  table. Concurrent callers share one fetch. The cache is cleared when the provider settings change. A
  turn starts the fetch alongside the model request, so pricing adds no latency.
- `Agent.price(provider, model, u)` sets `u.cost` and `u.cost_estimated = true`. The cost is stored on the
  message, and the session total counts `estimated_calls`. This covers turn steps, titles, `/btw` and
  `/compact`.
- Display: `~$0.12` (web footer, TUI footer, message tooltip) when part of the total is estimated, plus a
  tooltip line saying how many responses were estimated. Responses recorded before this change keep
  "no cost"; the totals are not backfilled.

## v9: per-model overrides (context window)

- README: "the user can browse the list of models by each provider and selectively choose to override
  certain configuration", e.g. a lower token limit for a 1M-context model that costs more above 200k.
- Setting `model_overrides` = `{provider id or "*": {model id: {"context_window": int}}}`. `"*"` applies to
  that model id with any provider; the provider's own entry wins. Ids match like the context window
  lookup (exact, then with a `vendor/` prefix on either side). Invalid or non-positive values are ignored.
  `context_window` is the only overridable field so far; the per-model object leaves room for more.
- `llm.model_override(settings, provider_id, model)` → `{"context_window": n}` or `{}`;
  `llm.find_model(models, id)` is the shared id matcher.
- `Server.context_window(provider_id, model)` → `(window, overridden)`: the override, else the provider's
  model list. The session summary's `context` gets `window_override: true` when the override was used.
  Changing the setting re-broadcasts every session so gauges update live.
- `models.list` entries carry `context_window_override` where set (the models cache keeps the provider's
  values). `/models` shows `· 200k context` / `· 50k context (override; provider: 200k)`.
- The override only changes what the harness measures against (context gauge colours at 80 % / 95 %, its
  tooltip). Nothing is truncated or refused at the limit; automatic compaction is question 031.
- Web settings → "Model overrides": a list of all overrides (Remove), and a browser: provider selector
  (configured providers + `*`), a filter, the provider's models (overridden first, max 50 shown) with the
  provider's window as placeholder and an input that takes `200000`, `200k` or `1M` (empty removes). A
  filter text that is not in the list becomes a "custom model id" row.


## v10: peer sessions — awareness, file claims, messaging

README: "Agents can communicate with other running agents in order to negotiate locks, priorities, ask
questions, etc". Owner's emphasis: agents can talk (mainly to ask questions), but the priority is **not
stepping on each other**; tool descriptions lean that way. All sessions live in one server process, so
everything is in-process (`sfh/peers.py`, one `Hub` per agent); no IPC.

### Peers
- A session's **repo** = the nearest ancestor of its cwd containing `.git` (else the cwd itself).
- **Peers** of S = other sessions with the same repo that have messages and are running or were updated in
  the last 15 min. Relation: `parent` / `child` / `sibling` (same parent_id) / `repo`.
- Per-session in-memory state (`Runtime`, never stored): `activity` (`{tool, summary, at}` of the last tool
  call), `edited` (`{abs path: ms}`, edit/write tool successes), `seen` (`{abs path: mtime}` after this
  session's last read/edit/write), `inbox` (undelivered peer messages), `peer_sig`, `wakes`.
- Session summary gains `repo`, `activity`, `edited` (last 15 min, ≤ 10, paths relative to the repo),
  `claims` (`[{path, note, auto, expires_at}]`) and `inbox` (`[{id, from, from_title, kind, text, at}]`).
  Clients compute peer lists from summaries (same `repo`, active); `session.updated` covers every change.

### Claims (advisory locks, in memory)
- `{abs path → {session_id, note, auto, at, expires_at}}`; a claim on a directory covers what is inside it.
- `claim(paths, note, minutes=30)` / `release(paths?)` tools. A path held by another session is not taken;
  the tool reports the holder and its note.
- **Auto claims**: every successful `edit`/`write` claims that file for 10 min (refreshed on each edit),
  so sessions that never call `claim` are still visible. Claims vanish when they expire or the session is
  deleted; a server restart forgets them.

### Edit/write checks (warn, rarely refuse)
- After `edit`/`write`: if another session holds a claim covering the file or edited it in the last 15
  min, a `⚠` warning naming the session, its note and what to do next is appended to the tool result
  (the change is kept).
- Before `write` (whole-file overwrite) only: refused when the last edit of the file was by another
  session and it happened after this session's last read/write of it (or this session never read it) —
  a real lost update. The error says to `read` it first. `edit` is not refused: its exact `old_string`
  match already guards against stale content.
- Changes made through `bash` are not tracked.

### Messages
- `send_message(session_id, text, kind?, reply_to?)`; kind `question` | `reply` | `info` (default: `reply`
  with reply_to, else `question` if the text contains "?", else `info`). Allowed to sessions in the same
  repo or the same family (parent/child/sibling); not to itself.
- Delivery always goes through the recipient's `inbox`, drained (stored as one user message) at the
  recipient's next **step boundary** (after tool results, before the next LLM call) or before its next
  prompt. Block: `{"type":"text","text":"<peer-message id=… from=… title=… kind=…>…</peer-message>\n<note>…</note>",
  "peer_message": {id, from, from_title, kind, reply_to, text}}`. The note tells the model to answer
  briefly with `send_message(reply_to=…)` (questions) and to continue its own task.
- **Waking**: a `question` to an idle *subagent* starts a turn for it (`_run(sid, None)`); max 5 wakes per
  session per 10 min. Replies and info never wake (no ping-pong). A question to an idle top-level
  session (owned by the user) waits; the UI shows it and offers "Let it answer" (`peers.deliver` RPC,
  which delivers and starts the turn). A question that arrives during a recipient's last step makes its
  run loop continue with another turn (same wake limit).
- A turn started only by a wake (no user prompt) does not send a `<subagent-result>` to the parent.

### Passive awareness
- Before each prompt and at each step boundary, if the set of *relevant* peer ids changed since the
  session last saw it (relevant = running, or holding claims / edits in the last 15 min; not the session's
  own parent or children, which it knows about), a `<peer-sessions>` block (meta `peer_sessions: [ids]`) is stored with the inbox delivery:
  each peer's title, status, activity, claims and recent edits, plus the guidance "avoid conflicts first:
  check `peers`/claims before editing, prefer files nobody else is working on, claim before large edits,
  message only to ask what you need or settle a conflict". When peers go away, one short "no other
  sessions are active" block. Not in the system prompt (keeps the prompt cache).
- Peer blocks are not prompts (`is_prompt` false: not editable, no versions), are skipped by title
  generation, and are ignored by the mock provider's prompt echo.

### Tools (order: peers, claim, release, send_message)
`peers` (no input; lists peers, their activity, latest prompt, claims and recent edits, and this session's
own claims), `claim`, `release`, `send_message`. Subagents get all four.

### RPC / commands
- `peers.list {session_id}` → the `peers` tool's data as JSON; `peers.deliver {session_id}` → delivers
  the held inbox now (starts a turn if idle). `/peers` shows the peers as a notice.

### Mock provider
- `[[tool:NAME:{json}]]` emits that tool call (tests of peer tools; any tool).
- Consecutive stored user messages are merged before deciding "tool results or prompt" (peer blocks
  follow tool results as their own message).

### Web UI
- Sidebar: `⇄N` badge when a session has active peers (tooltip: their titles + activity); `?N` badge when
  questions wait in its inbox.
- Chat header: a peer strip (identicon, title, running dot, activity, claim count; click opens the peer)
  and a conflict line when a peer claimed or recently edited a file this session edited/claimed.
- Transcript: incoming peer messages as cards (sender identicon + link, kind, reply-to); the
  `<peer-sessions>` block as a collapsed muted line; `send_message` / `claim` / `release` / `peers` tool
  cards decorated (target link, paths).
- Above the composer: held inbox messages with "Let it answer".
- TUI: peer messages as boxes, the peer-sessions block as one dim line.

## v11: title style, identicon subjects from what a session is about

### Title style (README "Titles … most specific first, and not starting with a verb")
- One prompt for every title (first message, end of the first exchange, `/retitle`):
  `agent.subjects_prompt`, whose title rules are `agent.TITLE_RULES`: as brief as possible (<= 8
  words), most specific thing first, never starting with a verb, with examples ("Apply and merge
  foo-project PR 231" → "foo-project PR 231 apply and merge"). The reply's title is trimmed of quotes
  and a trailing period, max 120 chars.

### Signals: a shared cwd is weak, the project worked on is strong
- `signals.extract(messages, cwd)` → `{"projects": [abs…] (≤ 8), "paths": [abs…] (≤ 12)}`, most used
  first. Inputs: `path`/`file_path`/`cwd` of tool calls, `cd`/`pushd`/`-C` targets and path-like words
  of bash commands (relative ones resolved after the command's first `cd`), path-like words of user
  messages (weight 3), subfolders of the cwd (two levels) whose name the user wrote (weight 3 × count),
  per-message cwds that differ from the session's. Relative paths and words from prose/commands count
  only if they (or their folder, not `/`) exist; URLs, `$VARS`, globs, `/dev` `/proc` `/sys` are ignored.
  Peer blocks are skipped.
- Project of a path: the nearest folder with a marker (`.git`, `package.json`, `pyproject.toml`,
  `go.mod`, `Cargo.toml`, `Makefile`, …; not hidden folders, not inside `node_modules`/`vendor`/
  `site-packages`/`.venv`) below the cwd (anywhere, outside it); else the cwd itself when it has a
  marker; else the first non-generic (`src`, `docs`, …), non-hidden folder under the cwd. Home, `/`
  and temp folders are no projects. A project whose sub-projects got at least as much use is dropped
  (a root folder that is a repo of repos).
- `sessions.signals` (JSON, migration 6) = the signals the stored subjects were made from, written by
  `retitle`. Older sessions get theirs computed (and stored) when they first appear in another
  session's context.
- Prompt: this session's projects + paths (relative to its cwd) as strong signals, its cwd marked as
  a WEAK signal; recent sessions as `title | cwd | top 3 projects | subjects`, sessions sharing a
  project first, then same cwd, then the rest (recency within each group, 25 max); the rule: decide by
  the project/subfolder actually worked on, never the cwd alone — reuse a domain for the same field,
  a topic only for the same project.
- No-LLM fallback: reuse domain + topic of the most recent session whose main (first) project is this
  session's main project; if this session touched no project, of one in the same cwd that touched none.

## v12: HTML in markdown, artifacts

README: "Agents should be able to embed HTML into the markdown as well, in order to show diagrams etc." and
"Agents can display "artifacts", self contained HTML files in a message such as for an interactive
diagram/explanation."

### Raw HTML (markdown.js)
- Recognized like CommonMark: inline tags/comments (`RE_HTML_INLINE`) and HTML blocks. A block starting
  with a "type 6" tag (`div`, `table`, `details`, `p`, `h1`…) or any lone complete tag ("type 7", can't
  interrupt a paragraph) runs until a blank line, so `<details>` + blank line + markdown + `</details>`
  works. `<svg>`, `<pre>` (and `<script>`/`<style>`/`<textarea>`, dropped) blocks run until their end tag
  (nesting counted) and may contain blank lines. Code spans/fences/indented code stay text.
- Everything goes through `sanitizeHtml` (no DOM needed; node-testable): HTML is tokenized and
  re-serialized. Text is escaped; comments, doctype and processing instructions are dropped.
  - Tags: allowlist `SAFE_TAGS` (presentational HTML: text-level tags, headings, lists, tables,
    `details`/`summary`, `figure`, `img`, `meter`/`progress`, …; inline SVG: shapes, `text`/`tspan`,
    `defs`, `marker`, gradients, `clipPath`/`mask`/`pattern`/`symbol`/`use`, filters, `animate*`).
    `DROP_TAGS` (`script style iframe object embed frame(set) applet noscript noembed noframes
    template textarea xmp plaintext select`) are removed with their content; other tags (`form`,
    `input`, `button`, `base`, `meta`, `link`, `html`, `body`, `math`, `foreignObject`, `set`, custom
    elements, …) are unwrapped (tag dropped, content kept).
  - Attributes: allowlist `SAFE_ATTRS` (`class style title lang dir role hidden id`, `aria-*`, table/list/
    img presentational attributes, SVG geometry/paint/text/marker/gradient/filter/animation attributes).
    No `on*`, `data-*`, `name`, `target`, `srcdoc`, `action`, … Values are entity-decoded (incl.
    `&colon;` `&Tab;` `&NewLine;`) before checks and re-escaped.
  - URLs: `<a href>` goes through `linkOpen` like markdown links (`safeUrl` scheme allowlist, http(s)
    gets `target=_blank rel=noopener noreferrer`, local paths become `.local-path` links resolved against
    the message's `links`); `<img src>` and SVG `<image href>` use the image rules (http(s), relative,
    `data:image/png|gif|jpeg|webp`); other SVG `href`/`xlink:href` must be `#fragment`. `animate*` whose
    `attributeName` mentions `href` or starts with `on` is dropped.
  - `style`: per declaration; dropped: `position` other than `static`/`relative` (no covering the app),
    `url(` other than `url(#id)`; the whole attribute is dropped on `\`, `expression(`, `javascript:`,
    `@import`, `behavior`, `-moz-binding`.
  - `id`s are prefixed `mdu-` (no collisions with/hijacking of the app's ids); `url(#x)` in attributes
    and styles and `href="#x"` are rewritten to match. Clicks on `a[href^="#mdu-"]` scroll in-page
    (app.js) instead of changing the `#route`.
  - Balanced output: each inline run and each block container keeps a stack of the tags it opened;
    stray end tags are dropped, unclosed tags are closed at the end of the paragraph/container.
- `render(text, {html: false})` restores the old behaviour (all raw HTML escaped).

### Streaming
- `render(text, {streaming: true})` (the web client's draft rendering): a half-received tag at the end of
  the last block is held back (not shown as text); an unclosed `<svg>`/`<pre>` block shows
  "rendering svg…" until its end tag arrives; an unclosed type-6 block is shown with its trailing partial
  tag cut. Without `streaming`, an incomplete tag is just text (CommonMark).
- app.js `setDraftHtml`: the draft's text block is updated by replacing only the top-level nodes from the
  first one that changed, so finished parts (and a finished artifact's iframe) are not re-created on
  every token. (An artifact nested in a list item that is still growing is re-created; the final stored
  message renders once more.)

### Artifacts
- Syntax: a fenced code block whose info string's first word is `artifact`, optionally
  `artifact title="Sorting demo"` (or `title='…'`, `title=word`, or just `artifact Some title`),
  containing one self-contained HTML document. Use a longer fence (```` or ~~~) if the HTML contains ```.
- markdown.js renders `<div class="artifact" data-title>`: a header (title; Source, Fullscreen, Open,
  Download buttons with `data-artifact-act`), the source as a hidden `<pre class="artifact-src">` (the
  "view source" toggle and the place app.js reads the document from) and an empty `.artifact-frame`.
  While streaming and the fence is still open: `<div class="artifact pending">` "building artifact… N
  lines" instead (no iframe until the fence closes). A stored message with an unclosed fence renders
  the artifact anyway.
- app.js `mountArtifacts(root)` (from `mdDiv` and `setDraftHtml`) puts
  `<iframe sandbox="allow-scripts allow-popups allow-modals" srcdoc=…>` into each empty frame: no
  `allow-same-origin`, so the artifact has an opaque origin and can't touch the page, its token,
  localStorage or the DOM. `SFHMarkdown.artifactDoc(src)` builds the srcdoc: a CSP `<meta>` inserted
  after the doctype (`default-src 'none'`; scripts/styles inline + https; img/font/media/connect/frame
  only https/data/blob), which keeps the artifact off the local harness server (`http://127.0.0.1` is
  not https, and the server accepts `Origin: null` requests); plus a small script that posts
  `{sfhArtifactHeight}` to the parent on load/resize. The parent matches `e.source` to one of its
  artifact iframes and sets the frame height (60 px … 80 % of the window; default 200 px; the frame is
  `resize: vertical`).
- Buttons: Source toggles the `<pre>`; Fullscreen = `requestFullscreen` on the frame; Open = a blob page
  that embeds the artifact in the same sandbox (a blob: URL of the document itself would run with the
  app page's origin); Download = the raw source as `<title>.html`.
- TUI: `render_markdown` shows `[artifact: <title> — open in web UI]` for the whole fence (also while
  streaming). Raw HTML is shown as source.
- The default system prompt (`store.DEFAULT_SYSTEM_PROMPT`) tells agents: small static visuals → inline
  HTML/SVG (attributes, no blank lines inside a block); interactive/scripted → an artifact.

### Tests
- `test/markdown.test.mjs` (node --test; now part of `make test`): HTML pass-through, blocks, code stays
  text, sanitizer cases (tags, handlers, URL tricks, styles, balancing, ids), streaming hold-back (every
  prefix of an HTML-heavy document never shows a tag as text), artifact markup/titles/srcdoc.
- `test/test_tui.py` `test_artifact_placeholder`.

## v13: TUI mouse wheel scrolling

README: "Scrolling the mouse wheel on the TUI scrolls backwards through session output".

### Terminal modes (tui.py)
- Mouse reporting = button-event tracking `\e[?1000h` + SGR encoding `\e[?1006h` (`MOUSE_ON`); no motion
  tracking (1002/1003). `MOUSE_OFF` = `\e[?1006l\e[?1000l`.
- `App.enter_terminal()` / `leave_terminal(saved_tty)` are the only places that switch raw mode, the
  alternate screen, bracketed paste and mouse reporting; `term_active` says which side we're on.
  `leave_terminal` always sends `MOUSE_OFF` (whatever the current state), before leaving the alternate
  screen. Used by `run()` (enter; `finally` → leave, so exit, exceptions, ctrl-c/ctrl-d,
  SIGTERM/SIGHUP and a lost connection all restore the terminal) and by `suspend()`.
- ctrl-z: ISIG is off, so it arrives as a key; `suspend()` leaves the terminal, sends SIGTSTP to its
  own process group with the default handler (the shell sees a stopped job), and on resume re-enters
  (mouse back on if wanted), resets the key parser and redraws. An external `kill -TSTP` goes through
  the same path (SIGTSTP handler). The TUI hands the terminal to no other program (no $EDITOR/pager;
  the terminal-in-page is web-only), so there are no other handoff points.

### Input (KeyParser)
- SGR `\e[<b;x;yM` (press) / `…m` (release) → `("mouse", b, x, y, pressed)`; `b` keeps its modifier
  bits (4 shift, 8 alt, 16 ctrl, 32 motion). Malformed SGR reports are swallowed. Legacy X10
  `\e[M` + 3 bytes is decoded too (defensive; coordinates > 223 aren't representable there).
- Split reports never become text: an incomplete CSI just waits for more input. On ESC_TIMEOUT
  silence a lone ESC is still the Esc key, but `\e[`, `\e[<digits;…` and `\e[M`+<3 bytes are kept
  for up to `MOUSE_PATIENCE` (10) flushes (~0.5 s, slow links) before the mouse fragment is dropped
  silently. A CSI interrupted by a new ESC is dropped instead of being parsed as text.

### Behaviour (App.mouse_key)
- Wheel up (64) scrolls the transcript back `WHEEL_LINES` (3) lines per notch, wheel down (65)
  forward; with shift/alt/ctrl a page (like PgUp/PgDn; many terminals keep shift+wheel for
  themselves). Same `App.scroll` (lines from the bottom) as PgUp/PgDn. In the session/model picker
  the wheel moves the selection. Clicks, releases, drags and horizontal wheel are ignored.
- Scrolled up: the last transcript row is `↓ N more lines · scroll down / PgDn to return`. `frame()`
  remembers `(width, line count)`; while `scroll > 0` and the width is unchanged, lines added below
  (streaming, new messages) are added to `scroll`, so the view doesn't move and N grows. At the bottom
  (`scroll == 0`) the view follows output as before; sending a prompt returns to the bottom.

### Setting / command
- Setting `tui_mouse` (default `true`): read from `hello` at connect; a `settings.updated` that changes
  it switches mouse reporting live.
- `/mouse` toggles, `/mouse on|off` sets, for this TUI only (local command, footer lists it). With
  mouse reporting on, most terminals need Shift+drag (Option/Fn+drag in some macOS terminals) to select
  text; `/mouse off` or `tui_mouse: false` gives plain drag selection back (scroll with PgUp/PgDn).

### Tests
- `test/test_tui.py`: `KeyParserTests` (SGR, malformed, every split point with a pause, fragment
  dropped after patience, X10), `MouseTests` (wheel → scroll offset and indicator, page with
  modifier, clicks ignored, view held while output arrives and following at the bottom, picker,
  setting/`/mouse`/`settings.updated`, enable/disable sequences around enter/leave/ctrl-z), and the pty
  end-to-end test (`MOUSE_ON` sent, wheel report split across writes shows the indicator and leaves the
  prompt clean, ctrl-z leaves/re-enters with mouse off before the alternate screen is left, mouse off
  on exit).

## v14: resizable, collapsible sidebar; `tui_mouse` in the web settings

README: "This sidebar is resizeable by dragging, and can be collapsed by a button."

### Width (app.js `initSbResize`, app.css `.sb-resize`)
- A handle `#sb-resize` (8px wide, straddling the sidebar's right border; `role="separator"`,
  `aria-valuenow/min/max`) shows a `col-resize` cursor, a grey line on hover and an accent line while
  dragging or keyboard-focused.
- Drag: `pointerdown` (primary button) → `setPointerCapture`, so mouse, pen and touch work and the drag
  keeps going outside the handle; `touch-action: none`; `preventDefault()` on pointerdown so no text gets
  selected and focus stays where it was. While dragging, `.app.sb-resizing` turns off the width
  transition and text selection and keeps the cursor. `pointerup` / `pointercancel` /
  `lostpointercapture` end the drag.
- Width = `--sb-width` set inline on `#app` (`--sb-default` 264px in app.css = `SB_DEFAULT` in app.js),
  clamped to 160px … max(160, 50% of the window width). A window resize re-clamps the shown width but
  keeps the stored one. Double-click on the handle resets to the default. Focused handle: ←/→ 16px
  (Shift: 64px), Home = min, End = max.
- Persistence: localStorage `sfh:sbWidth` (px, or null = default), like the existing `sfh:sbCollapsed`
  and `sfh:termHeight`: a per-browser view preference, so it is not a server setting (no new settings
  key; an exported copy opens with the default width). It is written once at the end of a drag (and
  per arrow key), never per `pointermove`; nothing is sent to the server and nothing re-renders, so
  dragging can't move focus (compare the providers-form focus fix).

### Collapse
- The existing `#sb-toggle` button (top of the sidebar) collapses it to the 50px rail of icons
  (identicons, nav icons, running dots); the button stays in the rail to expand again. Shortcut
  Ctrl/Cmd+B (unchanged; no other shortcut uses it). Its title/`aria-label` say "Collapse"/"Expand"
  and `aria-expanded` follows the state. Stored in localStorage `sfh:sbCollapsed` as before; the
  handle is hidden while collapsed and the stored width comes back on expand.
- Narrow windows (≤ 760px): unchanged overlay mode (the page opens collapsed; expanded, the sidebar
  floats over the content). The overlay uses `--sb-default`, not the dragged width, and has no handle.
- Identicons stay 16px (`flex: none`) at every width; titles ellipsize (`min-width: 0`).

### `tui_mouse` in the web settings
- Settings → Behaviour → "TUI mouse wheel scrolling" (checkbox, `tui_mouse`, see v13), saved through
  `settings.set` like `builtin_skill_enabled`; running TUIs follow via `settings.updated`.

### Tests
- `test/browser.mjs` step "sidebar: drag resize (clamped), keyboard, collapse/expand, reload, reset;
  tui_mouse setting": drags the handle with the mouse (+100px, then far right → 50% clamp, far left →
  160px), ArrowRight on the focused handle, checks the identicon is 16px inside the sidebar at the
  minimum, collapses/expands with the button, reloads (width and collapsed state kept), checks a 700px
  viewport uses the overlay at the default width without the handle, double-click resets, and toggles `tui_mouse` off and back on (checked through `settings.get`).

## v15: the harness explains itself; onboarding states (questions/034)

### Signals

- **H** (`Server.has_history`): a session exists. Sessions touched by the mock provider don't count outside
  test mode. Derived from data only (no "onboarded" flag): deleting everything returns to first run.
- **P** (`llm.configured`): the global provider resolves and has a model (+ base URL for openai). The key is
  optional (local endpoints); the built-in `anthropic` provider needs `ANTHROPIC_API_KEY`. No liveness check.
- **W** (web client): `location.protocol !== "file:"`. A W page never probes or contacts a local server.
- `hello` returns `onboarding: {has_history, has_provider, moved}` and `about` (see below); the server
  pushes the `onboarding` event whenever one of them changes (provider/creds settings, session
  create/delete, token regenerate). The offline snapshot carries `has_history`.

### Mock provider: tests only

`mock` only exists with `$SFH_MOCK=1` (or `sh FILE --mock ...`); configured providers of type `mock` are
dropped otherwise. With nothing configured, `resolve_provider` raises `NoProviderError` instead of falling
back to mock. In test mode the mock counts as P. All test harnesses set `SFH_MOCK=1`.

### No provider: read only

`chat.send` (non-command), `chat.edit` and `peers.deliver` fail with the `NO_PROVIDER` message; `/btw`,
`/retitle`, `/compact` answer with an error notice; everything else (settings, terminal, delete, other
commands) works. A provider that is configured but broken gives the normal errors in the chat.

### Provider setup RPCs (unsaved spec `{type: openai|anthropic, base_url, token, model}`)

| method | result |
|---|---|
| `providers.probe` | `{models, error?}`: the endpoint's model list |
| `providers.test` | `{ok, reply?, error?}`: a 16-token completion |
| `providers.setup` | `{id, onboarding}`: appends/replaces `providers[id]` (id from the URL host), key → credential `PROVIDER_<ID>_KEY` (credentials file, never the html), sets `provider` = id and `model` = null |
| `onboarding.get` | the onboarding object |

### Web client

| state | shows |
|---|---|
| file://, not running, snapshot without history | `#connect`: step 1 (start the server) active, step 2 pending |
| running, no H, no P | `#connect`: step 1 done, step 2 = provider form (+ env vars); switches to the app on the `onboarding` event |
| H, no P | normal app, read only: send button "Set up a provider →", no Edit / "Let it answer"; `#notice-provider` banner (dismissable, back on reload) |
| moved | `#notice-moved` banner + a note in Settings → Access token with a regenerate button |
| W with history | offline snapshot; banner "A shared copy…" with Install your own (→ landing) and Download |
| W without history | `#landing`: what it is, Download (fetch own URL → blob → `harness.html`), Ctrl-S "HTML only", how to run |

Routes `#setup` (the provider form as a page) and `#about` (sidebar "About").

### TUI

No H + no P: a full-screen setup page (base URL or `anthropic` → key → model from the probed list → test →
save); continues into a session once P is true (also when set up elsewhere). Leaving it unconfigured stops a
server the TUI itself started (so exported `AI_*` vars are seen next time). H + no P: read only (prompt kept,
footer warning); `/help` and other non-LLM commands work.

### "What is this" text

`sfh/about.py` (`ABOUT` dict) is the only source. The build injects it as `window.SFH_ABOUT`
(`about.gen.js`, before app.js) for screens shown before a server runs; `hello.about`, `/help`, the TUI and
`sh FILE help` read it from the program.

### Access token fingerprint

`meta.fingerprint` = sha256(hostname, user, realpath)[:32], written with every new token (server and offline
`token regenerate`). Server start: no token → new token; fingerprint differs and no H, no P → new token (a
downloaded/copied first-time setup); no stored fingerprint (older files) → adopt the current one;
fingerprint differs otherwise → `moved` (suggest regenerating; never forced, so moving a file on the same
machine doesn't break open tabs). The web client takes the pairing token whenever the server offers one.

### Builds

`build.py --fresh` refuses to replace an output holding data (db, snapshot or token) unless `--force`
(`make fresh FORCE=1`). `make publish` writes a clean `dist/public/harness.html` the same way.

### Tests

`test_server.TestOnboarding`, `test_tui.FirstRunTest` + `test_read_only_without_provider`,
`test/browser_onboarding.mjs` (fake OpenAI endpoint; file:// steps 1–2, read only, #setup/#about, http copy
with and without history, Download bytes identical, no local probes).

## v16: lightweight conversation branching (`/aside`), branch minimap, agent branch tools (questions/035)

README: `/aside` starts a lightweight branch from the current message without copying the whole session;
users can switch between branches; an aside can become the new main branch if the user keeps chatting there;
agents see side-branch metadata (encouraged not to read whole branches without asking) and can ask one-shot
questions (`ask_branch`, like `/btw`) or delegate turns (`message_branch`, like `subagent`); HTML shows
per-message branch indicators and a branch minimap.

### Storage & concurrency (`sfh/branches.py`, migration 8)
- `branches(id, session_id, name, origin_id, head_id, kind, created_at, updated_at)` + `sessions.active_branch_id`.
  `messages.parent_id` already stores the message tree; `branches` tracks named tips (`kind`: `main` | `aside` | `edit`).
  `branches.ensure(store, sid)` lazily ensures a `Main` branch (and registers pre-v16 edit leaves as `edit` branches).
- Per-branch execution runtime `BranchRuntime` (`agent.brt[bid]`): each branch has its own `task`, `queue`,
  `draft`, `stream`, `cancel`, `tool_ctx`, `tools`, `notify_branch`, so side-branches run concurrently with
  the active branch (e.g. via `message_branch` or while the user switches away). `agent.rt[sid]` keeps
  session-level peer state.
- `sessions.head_id` stays synced with the active branch's `head_id`. Session summary gains
  `active_branch_id`, `active_branch_name`, `active_branch_kind`, `branch_count`, `active_status` (`status`
  is `"running"` if any branch is running). Per-branch events carry `branch_id` (except `messages.truncated`).
- `chat.edit` registers the replaced tip as an `edit` branch before rewinding, keeping existing edit semantics intact.
- Auto-titling: aside branches get an immediate fallback title from their first prompt, then a cheap-model
  title (`branches.retitle`) after the first message and again after the first exchange.
- Auto-promotion (`branches.maybe_auto_promote`): an `aside` automatically becomes the new `main` when the
  user has sent ≥ 3 own prompts on it and it is strictly deeper past the LCA than the current `main` (old
  `main` becomes an `aside`, kept intact; info notice emitted). Manual promotion is always available.

### RPCs, events & commands
- RPCs: `branches.list`, `branches.create {session_id, from_message_id?, title?, prompt?, switch?}`,
  `branches.switch {session_id, branch_id}`, `branches.rename {session_id, branch_id, title}`,
  `branches.promote {session_id, branch_id}`, `branches.delete {session_id, branch_id}` (prunes messages
  unreferenced by remaining branches).
- Events: `branches.updated {session_id, branches}` and `branch.switched` (full snapshot; all subscribed
  clients switch together). `sessions.subscribe` includes `branch_id` and `branches` (`tree_info`:
  `branches[]` + `forks{msg_id|"root": [chips]}`).
- Commands: `/aside [message]`, `/branch [id|name|main|promote|rename <title>]`, `/branches`.

### Agent awareness & tools (`sfh/branches.py`, `sfh/tools.py`)
- Passive metadata: when other `main`/`aside` branches with messages (or running) exist,
  `branches.system_note` appends a concise `# Session branches (metadata only)` section to the system prompt
  (no contents, no volatile timestamps) instructing the model NOT to read/query side-branches unless asked or
  approved by the user.
- Tools: `branches` (live metadata only), `ask_branch(branch_id, question)` (one-shot `/btw`-style call on
  target branch's `llm_history`, no mutation, usage tracked), `message_branch(branch_id, prompt)` (starts a
  background turn on target branch like `subagent` and delivers `<branch-result>` back to the caller branch).

### Web UI, offline view & TUI
- Thinking blocks stay collapsed by default while streaming (`thinkingEl("", false)` in `flushDraft`).
- Web: hover `Aside` action on user & assistant messages; inline `.branch-fork` strip below each fork message
  showing count, lane-colored chips with title/status/counts (click switches sideways with subtle slide), and
  `+ Aside`; `.branch-origin-div` on aside branches with `Set as main` / `Rename`; collapsible right-hand
  `.branch-minimap` rail (hidden when `branches.length <= 1`, moves to top bar on `<= 960px`) with an SVG
  git-graph and quick actions; header `⑂` branch pill; sidebar & grid `⑂N` badge; `<branch-result>` cards.
- Offline snapshots (`Server.offline_snapshot`) carry deduplicated `branches: {sid: {info, threads, messages}}`
  for branched sessions, so offline/exported HTML copies support full read-only branch switching and minimap.
- TUI: `/branch` without arg opens `BranchPicker`; header shows `⑂ <name> (N)` when branched; transcript
  renders inline `⑂` fork lines, aside origin dividers, and `<branch-result>` boxes.

### Tests
`test/test_branches.py` (aside CRUD, mid-conversation forks, auto-promotion, metadata-only prompt & tool,
`ask`, `message` + `<thread-message>`, per-tab views, offline snapshot), `test/test_subagents.py` (fresh &
forked subagent threads, concurrent spawning, stopping, anti-recursion), `test/test_peers.py` (immediate
read-only `ask` on idle peers, peer-to-subagent messaging), and `test/test_tui.py` `BranchTests`.

## v17: unified session threads, per-tab views, unified tools, sidebar tree & two-mode minimap

Owner's refinements on v16 (see [questions/035-branching-aside.md](../questions/035-branching-aside.md)):

### Unified thread model (migration 9)
- `branches` gains `parent_branch_id TEXT`, `spawned_at_id INTEGER`, `context_mode TEXT` (`'fork'` | `'fresh'`).
- Subagents are now `kind = 'subagent'` threads inside the same session rather than separate child sessions:
  - `context='fresh'` (default): `origin_id = NULL` (clean isolated LLM history), `spawned_at_id` = assistant
    message that called `spawn` (visual anchor in UI/minimap, rendered with dashed lines & hollow nodes).
  - `context='fork'`: `origin_id` = parent message before the `tool_use` turn (so no dangling `tool_use` is
    inherited), `spawned_at_id` = assistant message (solid Bezier line, shared context).
- Session summaries carry compact `threads[]` so the left sidebar renders an expandable tree under every
  multi-thread session without extra RPCs.

### Unified agent tools (`sfh/tools.py`, `sfh/branches.py`)
- Replaced the 6 overlapping tools (`subagent`, `branches`, `ask_branch`, `message_branch`, `peers`,
  `send_message`) with 4 unified tools (old names kept as backwards-compatible aliases in `tools.execute`):
  - `threads`: unified roster of this session's threads (metadata-only for user asides) + repo peers & claims.
  - `spawn(description, prompt, context='fresh'|'fork')`: spawns a background subagent thread (disallowed
    inside subagent threads to prevent recursion). Reports back via `<thread-message from=… kind=… status=…>`.
  - `ask(target_id, question)`: synchronous one-shot read-only `/btw` query against ANY thread — local
    branch/subagent OR active/idle peer session in the same repo (triggers an immediate response without
    requiring "Let it answer" and without mutating the peer's transcript).
  - `message(target_id, text, kind?, reply_to?)`: async delivery to ANY thread (runs a turn on local
    branches/subagents and delivers `<thread-message>` back; routes peer sessions via `peers.Hub` or wakes a
    peer's subagent thread directly).

### Per-tab views (`#session/<sid>/<tid>`)
- Each `/ws` `Client` tracks `branch_of[sid]`. `sessions.subscribe {session_id, branch_id?}`, `chat.send`,
  `chat.edit`, `chat.stop`, `branches.create`, `branches.list`, and `branches.switch` all scope to that
  client's viewed thread. `branches.emit_updated` sends each subscribed client a `tree_info` tailored to its
  own viewed thread; switching threads in one tab never moves another tab.

### Left sidebar tree & two-mode right-hand minimap (`app.js`, `app.css`)
- **Left sidebar**: expandable tree under each multi-thread session showing Main, Asides (`⑂`), Fresh
  Subagents (`◌`), Forked Subagents (`⑂ fork`), and Edits (`↺`) linking to `#session/<sid>/<tid>`.
- **Right-hand minimap (two displays)**:
  - **Minimal (default)**: Sublime Text–style translucent floating overlay on the right showing a miniature
    SVG graph (solid curves for forked context, dashed curves for fresh subagents, proportional mini-bars,
    active glow ring). Clicking anywhere on it expands the full graph sidebar.
  - **Expanded**: full `260px` right-hand graph sidebar showing the whole tree + thread titles, kind/context
    tags, `↗` open-in-new-tab links, `★` promote, `✎` rename, `✕` delete, `+ Aside`, and `⇥` minimize back
    to the translucent minimap.

## v18: main as a tag, spatial thread view, collapsed inherited history (questions/035, second round)

README: the conversation UI shows a "spacial display" of connected branches (parent on the left, children on the
right, small previews beside the fork message, adaptive to the free width, fast sideways animation); side branches
use the same conventions as the sidebar tree/minimap (dashed = fresh subagent, solid = fork, same icons).

### Main is a tag (migration 10)
- `sessions.main_branch_id` is the tag; `branches.kind` no longer carries it. The session's first thread has
  `kind = 'root'` (was `'main'`); kinds are `root | aside | subagent | edit`. Migration 10 moves the tag from the
  `kind='main'` row into `sessions.main_branch_id` and re-kinds that row `root` (or `aside` when it was a promoted
  aside, i.e. it has an `origin_id`/`parent_branch_id`). `sessions.active_branch_id` is no longer used.
- `branches.promote` only moves the tag (no renames, no re-kinding); `maybe_auto_promote` keeps its rule (asides
  only, ≥ 3 own prompts, deeper past the LCA than main). Deleting the main thread moves the tag to its parent
  thread (else the root/oldest thread).
- `branches.ensure` returns the main thread: it is the default thread for `sessions.subscribe` without `branch_id`,
  `chat.send` from clients without a viewed thread, `/branch main`, `sessions.head_id` (kept in sync with main's
  head) and therefore the session title / message count. Switching or creating threads is per tab only and no
  longer touches the session row.
- Unnamed threads display as `Main` (root while it holds the tag) / `Original` (root after the tag moved) /
  `New aside` / `Subagent`. Thread lists are ordered by creation (root first), never "main first", so nothing
  moves when the tag moves; lane colours are by creation order with the root in lane 0.
- Session summary: `main_branch_id`; `active_branch_id/_name/_kind` and `active_status` are kept as aliases that
  now describe the main thread.
- Web: `★` marks main in the minimap, the sidebar tree and fork previews; the sidebar tree pins a duplicate
  `★ <main title>` entry at the top of a branched session's thread list (the minimap does not duplicate it).

### Collapsed inherited history (migration 10: `branches.expanded`)
- `branches.expanded INT` (NULL/0 = collapsed) per thread; `branches.set_expanded {session_id, branch_id,
  expanded}` stores it and emits `branches.updated`, so every tab viewing that thread follows live. Offline
  copies toggle locally only.
- Web: messages before the thread's fork message (`origin_id`) render inside a hidden `.inherited` block behind a
  `.inherit-bar` ("⋯ N earlier messages from *Parent* · Show"); the fork message and the thread's own messages
  follow. Expanding keeps the scroll position anchored on the bar. Root threads and fresh-context subagents have
  nothing inherited.

### Spatial view (web only)
- `tree_info` adds per thread `preview: {prompt, reply}` (first own user prompt and the start of the first reply)
  and `expanded`, and for the viewed thread a `parent_link {branch_id, name, kind, context_mode, at, preview}`
  where `at` is the fork message id (or `"root"`); the parent's preview is its continuation after the fork point.
  `forks{msg_id|"root": [chips]}` no longer contains the parent's group; chips carry `preview`, `created_at`, and
  are ordered by `created_at`, then title. For a fresh-context thread, `"root"` chips are limited to its own
  children.
- `renderSpatial()` replaces the inline `.branch-fork` strips: an absolutely positioned `.spatial` layer inside
  `.chat-inner` with one group per fork message (children, right gutter) and one parent group (left gutter, at the
  fork boundary, or the top for fresh threads). Groups are laid out top-down (pushed below the previous group when
  they would overlap) with an elbow connector from the conversation edge; dashed for fresh subagents.
- Modes per side from the free gutter width ((scroll width − conversation width) / 2): `full` (≥ 220px: card with
  icon, title, first prompt, and the reply when ≥ 300px), `title` (≥ 90px: low-contrast title text), `node`
  (otherwise: a small dot on the conversation edge with a hover card). The left side needs 80px more for each mode,
  so the parent degrades first. Recomputed on resize (ResizeObserver) and after content changes.
- Up to 2 chips per fork message are shown stacked; `+N` expands the stack in place (per tab, not stored).
- Clicking a preview navigates via the hash; `applyBranchSwitch` plays a ~140ms slide: from the right when going to
  a child, from the left when going to the parent.

### Tests
`test/test_branches.py`: promotion moves only the tag (kinds/names unchanged, `head_id` follows main, default
thread for new subscribers is main), deleting main moves the tag to the parent, `set_expanded` persists and is
broadcast to other tabs, `parent_link`/`preview`/ordering in `tree_info`. `test/browser.mjs`: the v17 subagent step
updated to threads (spawn card → thread link, `<thread-message>` card, sidebar thread tree), plus collapsed
inherited history, spatial previews (modes by width), `+N`, and the pinned `★` main entry.

## v19: the minimap as a git graph (questions/036)

README: the minimap shows the session like `git log --graph`: every user prompt and every turn's final agent message
is a commit, straight-line commits are folded away, branch starts carry the thread title.

### Graph (`branches._graph`, part of `tree_info` as `graph`)
- Commits: user prompts (`_is_user_prompt`) and assistant messages without tool calls (`_is_turn_final`). A commit's
  parent is the previous commit on any path through it (the message tree makes this a tree).
- Kept nodes: the first commit; fork points (≥ 2 child commits); each thread's first own commit (`start`, labelled
  with the thread's title); each thread's tip (`tips: [thread ids]`); later versions of an edited prompt (`edit`,
  ↺; the oldest version under a parent is the original). A thread with no commits of its own yet gets a placeholder
  node `t:<thread id>`. Every other commit is folded into the edge above the next kept node (`hidden` = count).
- Subagents attach to the final message of the parent turn that spawned them, or to that turn's prompt while the
  turn is still running; fresh ones are `fresh` (dashed).
- Ownership (`thread`): the first thread (creation order) holding the commit after its fork point; for starts and
  placeholders, the thread itself.
- Rows: time order (message `created_at`), never above a parent (heap-ordered topological sort).
- Lanes: chains of nodes. Main's line (root to main's tip) is lane 0 and only continues with main's own commits;
  other chains continue with the same thread's next node, else the oldest child, and every other child starts a new
  chain. Chains get the lowest lane free since their fork row, so ended lines are reused (like `git log --graph`).
  Colours stay tied to threads (creation order).
- Offline snapshots carry the graph once (`info.graph`); the per-thread `views` drop it.

### Web (`buildGraphSvg`, `renderMinimap`)
- Both displays draw the same graph. Minimal: uniform 10px rows, no labels, click expands. Expanded: a row per node,
  36px titled rows for thread starts (★ for main, kind glyph, kind tag, message count, running dot) and 20px untitled
  rows for other nodes (★ on main's tip, ↺ on edits).
- The viewed thread's tip gets the glow ring; its path from the first commit is drawn thicker and its rows tinted.
- Edges leave the parent, bend into the child's lane, and run straight down. Folded counts (`·N`) appear only while
  hovering the graph; every node has a tooltip with the thread, the message preview and the folded count.
- Clicking a node scrolls to its message when it is on the viewed thread (opening collapsed inherited history if
  needed); otherwise it opens the node's thread and scrolls there (`pendingScrollMid`). Hovering any node of a thread
  shows that thread's controls (↗ ★ ✎ ✕) on all of its rows.
- The `⇥` button stops its click from bubbling into the re-rendered minimal strip (which re-expanded the sidebar).

### Tests
`test/test_graph.py` (the README example: kept nodes, folding, ↺, lane reuse, main on lane 0 after promotion,
subagent anchors while running and after, placeholder tips, multiple edits, asides from main's tip).
`test/browser.mjs`: rows match the server's nodes, node clicks switch thread or scroll, `⇥` minimizes.

### Thread titles (README: "Threads each have titles calculated automatically, same as sessions")
- `branches.retitle` titles threads of every kind (root, aside, subagent, edit) from their own messages (cheap
  model with `BRANCH_TITLE_RULES`, else `fallback_branch_name` of the first own prompt). It runs after a thread's
  first own prompt and first exchange (a subagent's task counts, though it arrives from its parent thread) and when
  an edit creates an `edit` thread. Subagents' spawn descriptions are replaced by the generated title.
- An unnamed root thread (sessions from before this) shows the session title instead of `Main`/`Original`.
- `/retitle` on the main thread regenerates both the session title/subjects and the thread's title; on any other
  thread only that thread's. Moving the main tag never renames anything.

## v20: nested subagents, `/subsession`

### Nested subagents (README: "Sub-agents are also agents, so can also start sub-agents")
- Subagent threads may `spawn` subagents. `branches.subagent_depth` counts the `kind='subagent'` threads up the
  `parent_branch_id` chain (0 on root/aside/edit threads). `MAX_SUBAGENT_DEPTH = 5`: at depth 5 `spawn` is not
  offered (`tool_defs`) and refused; `MAX_RUNNING_SUBAGENTS = 50` subagent threads may run at once across all
  sessions of the server (`running_subagents`), else `spawn` fails with a tool error.
- The subagent system note tells the model its depth and when spawning is worth it (or that it is at the limit).
- Reporting: a subagent that ends a run while subagents it spawned still run does not report yet (`rt.awaiting`).
  Reports of its subagents start new runs on it; `turn_finished`'s delivery re-arms its `notify_branch` to its
  parent thread, so the run handling the last report sends the (one) report up. Runs started by other threads or
  peer questions still don't report to the parent.
- Stopping cascades: `stop_branch` first stops the thread's subagents recursively, which then don't report
  (`suppress_notify`), so a stopped parent isn't restarted by their `stopped` reports. An idle subagent that was
  waiting for its stopped subagents reports `status="stopped"` itself. `stop_all` (session delete) suppresses all.
- Mock: `[[nest:N]]` spawns a chain of N nested subagents (`[[nest:N:slow]]`: the last one is slow).

### Subsessions (README: `/subsession`)
- `/subsession [message]` (or RPC `sessions.create {parent_id}`) creates a child session (`sessions.parent_id`,
  `kind='subsession'`) with an empty context in the parent's cwd, with its provider/model/effort choice. It is a
  full session the user converses with; its cwd and model are its own afterwards. The requesting client gets a
  `session.open {session_id}` event and switches to it (web: route; TUI: `open_session` after draining events).
- Its system prompt notes the parent session (`subsessions.system_note`); the parent is treated like a peer.
- Summaries: `children` count, `kind`; `usage_subsessions` (was `usage_subagents`) sums all descendant sessions,
  and `add_usage` re-broadcasts every ancestor. Sidebar/grid/TUI label child sessions "subsessions" (old child
  sessions of kind `subagent` still show as such).
- Peers: subsessions are never woken by peer questions (user-owned; `may_wake` only wakes old `subagent`
  sessions) and appear in each other's `<peer-sessions>` conflict notes (only old subagent parent/child pairs
  are left out).
- Deleting a session keeps its subsessions as top-level sessions (`parent_id` cleared).
- Removed `subagents.py`: launching subagents as child sessions was unreachable since v17 (every tool context has
  `ctx.branch`, so the `subagent` alias always went to `spawn`). Old `<subagent-result>` messages still render.

### Tests
`test/test_subagents.py`: nesting to depth 5 and the refusal at depth 5, one report after the nested report,
stop cascades (a mid-level stop, stopping main silently). `test/test_subsessions.py`: the command, inherited
model, own cwd/model, nested subsessions, cost roll-up, parent delete.
