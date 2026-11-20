# Export with embedded credentials

- status: done
- asked: 2026-11-18
- answered: 2026-11-18

## Question

v1 stripped credentials from exports.

## Answer

Export should include embedded credentials, but with a warning.

## Notes

Only when `credentials_storage = embedded`; UI confirm, CLI stderr warning, `X-SFH-Warning` header. XDG-stored credentials never travel.
