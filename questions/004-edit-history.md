# Editing a prior user message: truncate or keep history?

- status: done
- asked: 2026-11-18
- answered: 2026-11-18

## Question

v1 truncated everything after the edited message.

## Answer

Preserve edit history in the db (no truncation), but for now display the latest branch.

## Notes

`messages.parent_id` + `sessions.head_id`; displayed thread walks from head. Branch switching UI is future work.
