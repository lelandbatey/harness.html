# Peer sessions: scope, waking and storage choices

- status: open
- asked: 2026-11-19
- answered:

## Question

Choices made while building v10 (peer awareness, claims, messaging) that the owner may want to change:

1. **Who is a peer**: same git repo (or same cwd outside a repo) and active in the last 15 minutes.
   Should sessions in *different* repos be able to message each other?
2. **Waking**: questions wake idle *subagents* (max 5 wakes / 10 min); idle top-level sessions hold the
   question until the user clicks "Let it answer" or sends a prompt. Should top-level sessions also wake
   automatically?
3. **Storage**: claims, activity and the inbox are in memory only (a server restart forgets them);
   delivered messages are stored in the recipient's transcript. Persist claims?
4. **Refusals**: only `write` is refused (lost update by another session); `edit` only warns. Stricter?
5. **`@` mentions** from the human composer to another session are not built yet.

## Answer
