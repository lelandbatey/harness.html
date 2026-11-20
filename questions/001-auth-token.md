# Auth strength: path header only?

- status: done
- asked: 2026-11-18
- answered: 2026-11-18

## Question

The README design has the page send its unix path so the server knows the request comes from the right place (plus Origin must be `null`/`file://`/absent). Any other local `file://` page that knows the path could still drive the server, which runs shell commands. Add a secret token?

## Answer

Yes. If no token is set, the server generates one when it runs and hands it to clients during the first 5 minutes after startup; after that clients are expected to know it because it lives within themselves (the html file). There should be a way to see the token and regenerate it from the server, forcing all clients to restart/reconnect.

## Notes

Token stored in `<meta name="sfh-token">` in the html; pairing window via `/api/health` only when freshly generated; `auth.regenerate` closes all websockets with code 4001; clients reload. See docs/DESIGN.md "v2 changes".
