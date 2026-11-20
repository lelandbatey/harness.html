# File links: resolved when the message is stored

- status: open
- asked: 2026-11-19
- answered:

## Question

"Only clickable if the file existed when the LLM generated the link": the server checks paths when
the message is stored (relative to that message's cwd) and saves the result with the message, so
links work in the offline view too. Consequences: while a reply is still streaming its paths are not
clickable yet; a file the agent creates *later in the same turn* is not a link in earlier messages;
messages stored before this change keep the old behaviour (clickable if they look like a path,
checked on click). Since existence now decides, inline code like `Makefile` (no slash/extension) is
also a link when that file exists. OK?

## Answer

