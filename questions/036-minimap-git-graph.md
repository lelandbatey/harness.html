# Minimap as a git graph: design choices

- status: done
- asked: 2026-11-23
- answered: 2026-11-23

## Question

The README describes the minimap as a git graph (user prompts and turn-final agent messages are commits,
straight-line commits omitted). Open points:

1. Which nodes are kept?
2. Clicking a node in shared history: which thread opens?
3. How is "you are here" marked?
4. Where does ★ main go?
5. Reuse lanes like `git log --graph`? Colours per thread or per lane?
6. What does the minimal overlay show?
7. Tooltips with a message preview?
8. A subagent whose parent turn has no final message yet?
9. Counts of folded commits?

## Answer

1. The first commit, fork points, each thread's first and last commit (and, per the README example, the ↺ prompt
   that replaced an edited one). Lane 0 is main's whole line from the first commit; the parent's continuation after
   a promoted thread's fork point becomes a side line.
2. The viewed thread if the message is on it (just scroll); otherwise open that node's thread at the message.
3. A glow on the tip of the viewed thread.
4. Both: on main's tip and on main's titled first node.
5. Reuse lanes; colours stay tied to threads.
6. The same graph, minus labels.
7. Yes, show the preview in a tooltip.
8. Attach to the turn's user prompt until the final message arrives.
9. Only on hover.

Implemented in v19 (see docs/DESIGN.md).
