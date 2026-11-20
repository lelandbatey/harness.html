# Self-explaining startup / onboarding states

- status: done
- asked: 2026-11-20
- answered: 2026-11-20

## Question

README "Universal Features": on startup the harness detects its own state (history? provider?
hosted on a public web page?) and guides the user. The signals live in different places (provider
config is only visible to the server), the mock fallback hides "no provider", Ctrl-S can mangle the
file, and a published copy carries its access token. How should each case behave?

## Answer

Signals: **H** = a non-mock session exists (derived, no "onboarded" flag; deleting everything
returns to first run). **P** = a non-mock provider with its credential present (no liveness check).
**W** = page served from anything other than `file://`.

- **Not W, no H, no P**: one simple screen, two steps: (1) start the server (`sh …`),
  (2) set up a provider. Step 2 has a small provider form (endpoint/model/token + Test); env vars
  (`AI_ENDPOINT`/`AI_MODEL`/`AI_TOKEN`, then restart) shown as the alternative. Tokens go to the
  credentials file, never the HTML. When P becomes true the page switches to the normal UI live
  (websocket push).
- **Not W, H, no P**: normal UI, read-only = everything that calls an LLM is disabled (send, edit &
  resend, retitle, compact, …); everything else (settings, terminal, delete, browsing) works. A
  banner links to provider setup; dismissable, but returns on reload / new tab. Only covers "none
  configured"; a broken provider shows normal errors in chat.
- **W, no H**: landing ("hero") page: what it is, a **Download** button (fetch own bytes → blob →
  `harness.html`), mention Ctrl-S works only with "Webpage, HTML only", then how to run it: open the
  downloaded file in a browser and it shows the exact `sh` command.
- **W, H**: read-only offline view + "install your own" banner.
- **W never** tries to reach a local server.
- **TUI**: same rules as the web UI (full-screen text page for no H + no P; read-only + warning
  with H).
- **`sh harness.html` server start** with no P: print one or two lines on how to configure one.
- **Mock**: no longer a silent fallback and does not count as a provider; remove it, or keep it
  only for tests.
- **Explain itself generally**: one source of "what is this" text used by onboarding, an always
  available About (`?`) page, TUI `/help`, and `--help`. The built-in `singlefile-harness` skill is
  enough for the LLM side.

### Access token on a new machine / copy

The file stores a fingerprint `hash(username + file path)` (written at build and whenever the
token is set). On server start, if the current fingerprint differs ("running in a different
scenario than last time"), settings suggest regenerating the token with a "click here" button. In
the **no H + no P** state the server **always** generates a new token + fingerprint, so every
first-time setup (e.g. a downloaded published copy) gets a fresh token.

### Follow-ups (2026-11-20)

- G1 mock: only exists when the server starts with `SFH_MOCK=1` / `--mock`; hidden from the UI and
  model pickers otherwise; never counts toward H or P.
- G2 P = a non-mock provider with endpoint + model; token optional (local endpoints). Test button
  surfaces bad keys.
- G3 W = any non-`file://` page (localhost and raw IPs included).
- G4 no H + no P: regenerate the token only when the fingerprint differs or the token is empty (so
  restarts during setup don't keep breaking tabs; a fresh download always differs).
- G5 fingerprint mismatch with H: *suggest* regeneration (confirm button), don't force it, so moving
  a file on the same machine doesn't break open tabs.
- G6 fingerprint = sha256(hostname + user + realpath), stored next to the token in a meta tag.
- G7 `make publish`: clean copy (no token, fingerprint, data); warn/refuse if it would overwrite an
  existing file holding data.
- G8 provider form: openai-compatible + anthropic, model list from `/models`, Test = tiny
  completion; provider -> `providers` setting, token -> credentials file.
- G9 "what is this" text lives in source; owner edits later.

## Notes

Implemented; see docs/DESIGN.md "v15". Deviations / choices made while building:

- In test mode (`SFH_MOCK=1`) the mock counts as P, so tests exercise the normal UI.
- The fingerprint lives in the db's `meta` table (inside the file), not a separate meta tag; files from
  before this change silently adopt the current fingerprint on their next start.
- `providers.setup` names the provider after the endpoint's host (e.g. `127.0.0.1`, `api.openai.com`) and
  resets the global `model` setting so the new provider's model applies.
- `/help` (web and TUI) starts with the "what is this" text.
- The TUI's first-run screen has its own minimal wizard (URL → key → model → test → save); leaving it
  unconfigured stops a background server the TUI started, so exported `AI_*` vars apply on the next run.
- `build.py --fresh` (so `make fresh` too) now refuses to overwrite a file holding data unless
  `--force` / `FORCE=1`.
