# Lightweight conversation branching (`/aside`) & unified threads: design choices

- status: done
- asked: 2026-11-22
- answered: 2026-11-22, 2026-11-23 (second round: main as a tag, spatial view, collapsed inherited history)

## Question

Choices and assumptions made while designing and implementing v16/v17 (unified session threads for `/aside` + subagents, per-tab views, unified `threads`/`spawn`/`ask`/`message` tools, expandable sidebar tree, and two-mode right-hand minimap):

1. **Auto-promotion threshold**: an `aside` automatically becomes `main` once the user has sent **3 prompts** of
   their own on it *and* it is strictly deeper past the fork point (LCA) than the current `main`. Subagent threads
   never auto-promote (manual `Set as main` / `★` is always available). Is 3 prompts + deeper-than-main right?
2. **Per-tab views?**
3. **Unifying subagents & branches in UI and storage?**
4. **Read-only `ask` across idle peer sessions without approval?**
5. **Forked subagents (`spawn(..., context="fork" | "fresh")`)?**
6. **Session title when an aside becomes main; edited messages as `edit` threads?**

Second round (2026-11-23), about the README's "spacial display" of branches and the agent tools:

7. What is "directly connected" (parent/children only, or also siblings, grandchildren, grandparents)?
8. What does the left-hand parent preview show, given the centre already shows the inherited messages?
9. Where do previews sit vertically; what is in a preview; what if many threads fork at one message?
10. How do fresh subagents and `edit` threads appear; does left/right follow the fork structure or the main label?
11. How do the animation, the existing minimap, the left session sidebar, breakpoints and the TUI fit in?
12. Is an agent-made fork a subagent or an aside? What does `message` add to whose context? Is "ask the user before
    reading their asides" enforced by the harness or only guidance?

## Answer

1. Keep auto-promotion with the same rule (≥ 3 own prompts on an **aside** and deeper past the fork point than
   main); it only ever applies to asides. Manual promotion works for any thread, subagents included.
2. Per-tab views (`#session/<sid>/<tid>`).
3. Left sidebar shows an expandable tree-style view of threads; right-hand minimap defaults to a Sublime Text–like
   minimal translucent view and opens the full graph sidebar when clicked directly.
4. `ask` triggers an immediate response without requiring user approval. For user asides, "ask the user first" is
   prompt guidance only; the harness does not enforce it.
5. Agents can spawn both fresh-context and forked-context subagents; an agent-made fork is always a `subagent`
   thread (never an aside).
6. Edits stay `edit` threads, hidden from the model's metadata. Editing a message on main keeps the `★` on the
   edited (new) version; the original becomes the `↺` edit thread.
7. **Main is a tag, like a git tag.** Promoting (manual or automatic) only moves the `★`: no thread is renamed or
   re-kinded ("the old main does not become an aside") and nothing moves around in the tree or minimap. The main
   thread is the one a session opens on when the URL names no thread, the target of `/branch main`, and the
   thread the session title/preview come from. The left sidebar tree shows a duplicate `★` entry for main pinned at
   the top (the minimap does not duplicate it, it only stars the node).
8. **Spatial view (web only; out of scope for the TUI).** Think of it as a git graph where every user message is a
   commit; the centre column is the straight line of ancestor messages down to the viewed thread.
   - Only one level each way: the **parent** thread on the **left** and **children** on the **right**. Siblings,
     grandchildren and grandparents are not shown spatially (the minimap is the way to reach them). Every thread
     that diverges from a message on the displayed line is a "child" of that message, including forks off ancestor
     messages inside the inherited history.
   - Left/right follows the fork structure (`parent_branch_id`), never the main tag.
   - Each preview sits beside its fork message and scrolls with the transcript. A preview shows the title, the
     first prompt and, if there is room, the first bit of the response.
   - More than 2 forks at one message: show 2 stacked, plus a `+N` that expands the stack in place.
   - Ordering when shown side by side / stacked: by creation time (earlier first / further left), equal timestamps
     alphabetically by title.
   - Fresh subagents attach at the message that spawned them, drawn dashed; forks are solid; same icons as the
     sidebar tree. `edit` threads only show their left link to the parent (siblings are not shown).
   - Adaptive to the free space left and right of the conversation: wide → previews; narrower → a low-contrast bit
     of the title; narrowest → a small node that needs hover to show its text. The parent preview degrades/goes
     away first. Not a resizable column with visible boundaries.
   - Clicking a side preview runs a very fast animation: the clicked thread slides into the centre while the one we
     were on moves out to the left or right accordingly.
   - The spatial view does **not** replace the minimap (the minimap is the whole map, the spatial view a zoomed-in
     one); the floating minimap sits on top of the right-hand previews.
9. **Collapsed inherited history.** A thread's inherited messages (everything up to its fork point, across all
   ancestors) start collapsed behind a "⋯ N earlier messages from *Parent*" bar, followed by the fork message and
   then the thread's own messages. Expanding shows all ancestors' messages in one straight line; the left link to
   the direct parent stays at the boundary (only the direct parent gets a link). The collapsed/expanded state is
   stored per thread on the server and syncs live to every open tab.
10. `message` to a local thread runs a turn there and auto-replies to the sender with the target's final text as a
    `<thread-message>`; peer sessions do not auto-reply.
