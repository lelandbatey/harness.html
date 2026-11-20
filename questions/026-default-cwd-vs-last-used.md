# `default_cwd` setting vs. "last new-session cwd"

- status: open
- asked: 2026-11-19
- answered:

## Question

The README now says new sessions default to the last new-session cwd, falling back to the server's
cwd. That replaces the answer of [003](003-default-cwd.md) (the html file's directory, or the
`default_cwd` setting). I kept `default_cwd` as the *fallback* (used only before any session was
started; blank = the server's cwd). Should it instead win over "last used", or be removed?

## Answer

