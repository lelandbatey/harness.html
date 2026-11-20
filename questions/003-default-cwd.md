# Default working directory for sessions created from the browser

- status: done
- asked: 2026-11-18
- answered: 2026-11-18

## Question

Assumed the directory containing the html file.

## Answer

Current default is good, but there should be a way to change the default for the running server.

## Notes

Setting `default_cwd` (null = html dir), editable in the settings UI and via `sh FILE settings set default_cwd '"/path"'`.
