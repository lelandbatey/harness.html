# Lightweight conversation branching (`/aside`) & unified threads: design choices

- status: in-progress
- asked: 2026-11-22
- answered: 2026-11-22

## Question

Choices and assumptions made while designing and implementing v16/v17 (unified session threads for `/aside` + subagents, per-tab views, unified `threads`/`spawn`/`ask`/`message` tools, expandable sidebar tree, and two-mode right-hand minimap):

1. **Auto-promotion threshold**:
   - Currently an `aside` automatically promotes to become the new `main` branch once the user has sent **3 prompts** of their own on that aside *and* it has strictly more messages past the fork point (LCA) than the current `main` branch. Subagent threads never auto-promote (manual `Set as main` / `★` is always available).
   - Still open for tuning: is 3 prompts + deeper-than-main the right feel?

2. **Per-tab views**: *(answered: yes, per-tab views — implemented in v17 via `#session/<sid>/<tid>` and per-client `branch_of` subscriptions)*

3. **Unifying subagents & branches in UI and storage**: *(answered: expandable tree in the left sidebar + two-mode right-hand minimap with Sublime Text–style translucent default that opens the full graph sidebar on click — implemented in v17)*

4. **Read-only `ask` across idle peer sessions**: *(answered: yes, `ask` triggers an immediate read-only response without requiring user approval — implemented in v17)*

5. **Forked subagents (`spawn(..., context="fork" | "fresh")`)**: *(answered: yes — implemented in v17)*

6. **Remaining open polish questions**:
   - **Session title when an aside becomes main**: currently keeps the session's sidebar title & identicon as-is (`/retitle` always retitles from the active thread). Should promoting an aside also trigger an automatic session retitle?
   - **Edited messages (`chat.edit`)**: preserved as `kind="edit"` threads in the sidebar tree & minimap, omitted from passive LLM metadata so typo edits don't distract the model. Keep that?

## Answer

1. Left sidebar shows an expandable tree-style view of threads; right-hand minimap defaults to a Sublime Text–like minimal translucent view and opens the full graph sidebar when clicked directly.
2. Per-tab views (`#session/<sid>/<tid>`).
3. `ask` triggers an immediate response without requiring user approval.
4. Agents can spawn both fresh-context and forked-context subagents.
