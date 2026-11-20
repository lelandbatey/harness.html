# Automatic compaction near the context limit?

- status: open
- asked: 2026-11-19
- answered:

## Question

`/compact` is manual, and the context gauge turns amber at 80% / red at 95% of the model's window.
Should the harness also compact automatically (e.g. before a turn when the last response used
> 90% of the window), and should `/compact` use the session's model (current: better summaries, costs
more) or the cheap model?

## Answer

