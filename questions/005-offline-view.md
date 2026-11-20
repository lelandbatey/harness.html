# Offline view when the server isn't running?

- status: done
- asked: 2026-11-18
- answered: 2026-11-18

## Question

v1 only showed a 'start the server' screen.

## Answer

Yes, read-only offline view of prior sessions.

## Notes

Server writes a gzipped JSON snapshot block (`SFH-OFFLINE` markers) alongside the sqlite data on each save; the page decodes it with DecompressionStream, so no sql.js/WASM is needed.
