# Is the whole db re-gzipped and rewritten on every change?

- status: answered
- asked: 2026-11-18
- answered: 2026-11-18

## Question

Is the whole db re-gzipped and rewritten on every change?

## Answer

That's OK for now. Future hope: replace it with something more efficient, e.g. a custom SQLite VFS that maps onto raw base64 in the file to allow seek+rewrite of changed pages.

## Notes

Yes: every change marks the db dirty; saves are debounced (1.5 s after the last change, at most 6 s after the first unsaved one), and each save re-serializes + gzips + rewrites the whole file. Future-work idea recorded here.
