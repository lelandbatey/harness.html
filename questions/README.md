# Questions & answers

Design questions raised while building, with the owner's answers, kept so they can be revisited.
One file per question: `NNN-slug.md` with `status`, `asked`, `answered` lines, then
`## Question`, `## Answer` and optional `## Notes` sections.

Statuses: `open` (needs an answer), `answered` (no work needed), `in-progress` (answer being
implemented), `done` (implemented), `deferred`.

To add one: copy the next number, fill in the question, leave `answered:` blank and status `open`,
and add a row below.

| # | question | status |
|---|---|---|
| [001](001-auth-token.md) | Auth strength: path header only? | done |
| [002](002-tool-approvals.md) | Tool permission prompts? | answered |
| [003](003-default-cwd.md) | Default working directory for sessions created from the browser | done |
| [004](004-edit-history.md) | Editing a prior user message: truncate or keep history? | done |
| [005](005-offline-view.md) | Offline view when the server isn't running? | done |
| [006](006-export-embedded-creds.md) | Export with embedded credentials | done |
| [007](007-providers.md) | Which LLM providers? | done |
| [008](008-data-growth.md) | Is the whole db re-gzipped and rewritten on every change? | answered |
| [009](009-identicon-shape.md) | Identicon design OK? | done |
| [010](010-stray-stderr.md) | Stray `cannot open !--` stderr line | answered |
| [011](011-file-size.md) | File size (~770 KiB empty) | answered |
| [012](012-system-prompt-scope.md) | What does a custom system prompt replace? | answered |
| [013](013-stop-mid-turn.md) | Server stop mid-turn loses in-progress output | answered |
| [014](014-tui-misc.md) | TUI: model shown in mock mode, /resume picker, /edit only last message | done |
| [015](015-web-misc.md) | Web: `#new` route, no reset-to-default buttons, `window.SFHApp` debug hook | answered |
| [016](016-real-provider-test.md) | Test against a real provider | done |
| [017](017-nvim-127.md) | nvim exited 127 in the subagent environment | done |
| [018](018-subagent-child-titles.md) | Should subagent child sessions be auto-titled? | open |
| [019](019-subagent-results-on-stop.md) | Keep pending subagent results when the parent is stopped? | open |
| [020](020-subagent-error-status.md) | Subagent status when an earlier queued turn failed | open |
| [021](021-subagent-delete-running.md) | Deleting a running child | open |
| [022](022-subagent-continue-queued.md) | Continuing a running child | open |
| [023](023-proxy-no-reasoning.md) | No thinking blocks via the OpenAI-compatible proxy | open |
| [024](024-export-warning-empty.md) | Export warning when embedded storage has no credentials | open |
| [025](025-cheap-model-env.md) | Cheap model for the env provider | open |
| [026](026-default-cwd-vs-last-used.md) | `default_cwd` setting vs. "last new-session cwd" | open |
| [027](027-retry-timeouts.md) | Retry timeouts: 30 s to start, 120 s silence mid-stream | open |
| [028](028-file-links-existence.md) | File links resolved when the message is stored | open |
| [029](029-cost-without-provider-prices.md) | Cost when the provider doesn't report one (price table?) | answered |
| [030](030-stream-options.md) | `stream_options.include_usage` on OpenAI-compatible endpoints | open |
| [031](031-auto-compaction.md) | Automatic compaction near the context limit? Which model compacts? | open |
| [032](032-pricing-backfill.md) | Re-price sessions recorded before price lists were used? | open |
| [033](033-peer-sessions.md) | Peer sessions: scope, waking, storage, refusals, @ mentions | open |
| [034](034-self-explaining-onboarding.md) | Self-explaining startup / onboarding states | done |
| [035](035-branching-aside.md) | Lightweight conversation branching (`/aside`): main as a tag, spatial view, collapsed history | done |
