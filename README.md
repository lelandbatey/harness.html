
# Single-file LLM harness + HTML page

For info about the LLM harness, see further down.

This is a single HTML file that is both a runnable program AND the user-interface for interacting
with that runnable program. It can save it's own state and settings within itself for easy
portability, allowing you to move the entire program state somewhere else as a single file.

## Why isn't this just a program that you visit on localhost?

Because "hosting" through localhost stinks. If the host program isn't running then all you get is a
"cannot connect" page in your browser. Also, bookmarks and browser history are shockingly bad at
jogging your memory because every result is `http://127.0.0.1:9765/whatever`, the favicons stomp on
one another, it just kinda stinks.

## What this offers instead

You open the page as a `file://` and it detects it's not "running". Then it guides you to the next
step of telling you to open the terminal and run `sh <converted window.location.href to unix-style path>` to start the program.

From there, they can communicate over HTTP. The program has to have the appropriate CORS headers,
and the HTML has to send the "converted window.location.href" in a header so the server knows
its getting requests from the right place.

Now we have the HTML view in the browser, and the html as program running on the computer, and they
can communicate back and forth via e.g. websockets+http.

## Other features

- All(\*) its state lives within itself as SQLite dbs or files base64-d into the HTML itself
- Sensitive credentials go in `XDG_CONFIG_HOME/singlefile/credentials`
- No-conflict port allocation; port is chosen from limited set via hash of `converted window.location.href`
- Supports self-export, where "export" creates a new HTML file which by default opens to the view
  you're looking at with all the data (none of the credentials) loaded in.
- Tools for LLMs to interact with the data/files/configs/HTTP server/websocket server are all
  included. The tools are embedded within the HTML as scripts and are extractable. Instructions for
  extraction are written near the head of the HTML file for easy discovery by LLMs.
- No use of node or JS build tools; uses bash and python only; certain JS tools are optionally
  installed for testing, but such as for browser automation
- No node, no react, no JS frameworks; everything is in this repo or if a dependency is desperately
  needed, then it's vendored in
- Makefile for builds
- Server implemented in Python; uses a vendored copy of tremolo that's AST-re-written at build time
  into a single file and used by the server-side.
  [Tremolo](https://github.com/nggit/tremolo/tree/main/tremolo)


### Sensitive credential management

Naively all state (creds, config, data, media, etc) could be stored inside the HTML as base64
encoded data, but that means your secret credentials are stored there as well, so if someone gets
that file (or you send it to someone so you can show them what you're talking about) then you've
leaked your credentials.

To prevent that, only one file is recorded to the broader system, a
`$XDG_CONFIG_HOME/singlefile/credentials` file containing credentials. If you want, the UI can be
used to turn this option off, such as if you want to migrate your install elsewhere.

## Limitations

- Can't hold unlimited data; practical limit is ~50MB


# LLM harness features

This harness runs in server mode with an HTTP/websocket API, and has two clients:
1. The HTML client
2. A minimal single-session-at-a-time TUI which doesn't use ncurses but does "take over the screen"


## Universal Features

- On startup, detects its own configuration+data and tries to onboard the user configuration/usage.
  Signals:
  - **H**: a session exists (derived from data; deleting everything returns to first run)
  - **P**: a provider is configured
  - **W**: the page is served over http(s) or somewhere other than `file://`
  - States:
    - no H, no P: a simple two-step screen: (1) start the server with `sh …`, (2) set up a provider (form in the page, or `$AI_*` env vars). Switches to the normal UI live once a provider exists.
    - H, no P: the normal UI, read-only (anything that calls an LLM is disabled, everything else works), with a banner explaining how to configure a provider. Dismissable, returns on reload.
    - W, no H: a landing page explaining what this is, a Download button (or Ctrl-S choosing "Webpage, HTML only"), and how to run the downloaded file.
    - W, H: the read-only view plus an "install your own" banner.
    - A W page never contacts a local server.
- The security access token is tied to `hash(hostname, user, path)`. A first-time setup always gets
  a fresh token; a copy running somewhere new offers to regenerate it.
- Sessions are stored in db and are resumable later
- If a session is open in multiple views, those views will see changes live simultaneously
- Sessions support
  - AGENTS.md
  - skills, searched for in the following order and precedence in case of duplicates
    - ./skills
    - ./.opencode/skills
    - ./.claude/skills
- Multiple parallel and concurrent session support; no limits to parallelism
- one server hosts any number of clients
- Titles of sessions are auto-generated automatically from the contents of the first message, but
  with a "cheap" model, whatever that is. They are then re-generated automatically AFTER the first
  user message -> llm -> hands back user control.
  - Titles for sessions are formatted as briefly as possible, most specific first, and not starting
    with a verb. e.g. instead of "Apply and merge foo-project PR 231" it should be "foo-project PR
    231 apply and merge"
  - Threads each have titles calculated automatically, same as sessions
- Support for `/` commands like `/retitle` which re-calculates a title via LLM call or `/btw` which
  gets a response that doesn't go directly into the chat context, but uses the context to answer
  - support for a "compaction" `/compact` command
  - `/retitle` in the "main" thread of a session updates the title for that thread AND the title for
    the session, while `/retitle` in a side thread updates the title of that thread
- Streaming response from the LLM
- The system prompt is visible in the settings and modifiable
- There's a built-in skill (which can be disabled) for asking the LLM to edit this harness, or any
  of its settings.
- Support for editing the prior user message to get slightly renewed/tweaked answer, in any client
- Providers can be configured via the env vars `$AI_ENDPOINT` `$AI_MODEL` and `$AI_TOKEN`, and more
  providers can be configured in the settings.
  - The harness should be able to query AI_ENDPOINT for the models supported, to allow the user to
    change models.
  - In the settings, the user can browse the list of models by each provider and selectively choose
    to override certain configuration. For example, the user could set a LOWER limit for a models
    token limit (e.g. in cases like "Opus 1m" where if your tokens go above 200k, then your prices
    go waaaaay up, so best to set a limit of 200k tokens)
- Cost-of-session is tracked; the cost of each response from a provider is tracked (if it's returned
  by the provider, not all do).
  - the total cost for a session is displayed in the bottom right corner of the session UI in
    whatever UI is used.
  - The harness checks the `/settings/api/pricing` API of a provider and tries to use that to
    estimate the running costs of a session, if the provider doesn't give direct costs in its
    completion API
- Size of the context window is shown all UIs
- Thinking/reasoning tokens are shown in the UI as a shortened "thinking" line, which can be
  expanded to show the full thinking/reasoning. By default, "thinking" lines are minimized so as not
  to dominate the entire screen with scrolling text.
- The user can "branch" conversations in a more "lightweight" way than forking an entire
  conversation as a copy. Instead the user can start an `/aside` to fork a new branch/thread from
  the current message, then switch between branches of the conversation. How that looks is UI
  defined.
  - An aside can turn into the new "main session" if the user just keeps chatting there. Changing
    which thread is the "main" thread does not change the title of any session, just changes which
    thread is pointed at by the "main" designation.
  - Agents can see the side-branches of their session, and can even talk to those branches like
    subagents, or ask them one-shot questions like users can with `/btw`
  - Agents are encouraged not to read the whole branches off to the side, only look at metadata and
    to ask the user if they should check in, or if the user proactively tells them to look.
- Agents in a session can launch sub agents
- Agents can communicate with other running agents in order to negotiate locks, priorities, ask
  questions, etc
- Durations for messages in a session indicate how long they took to run; how long thinking tokens
  were streaming in, or how long a tool took to run
- The user can change which effort level, model and provider they are using, even mid-conversation, so each
  message also needs to have what model it was recorded with. When changing the model, there should
  be a way to choose/filter their choices from the list of models available
- Tools:
  - bash
  - read
  - write
  - edit
  - glob
  - skill - loads a full SKILL.md of a skill by name
  - tools for interacting with the branching model of conversations, which includes subagents
    - `threads` for viewing other threads
    - `spawn`
      - `context='subagent'` to create a new thread with a "fresh context" and instructions
      - `context='fork'` to create a new thread that retains the context from the parent
    - `ask` lets an agent ask another thread (in the same session or even a different session in the
      same repo) a question and get an immediate answer. The response is not added to the context of
      the answering thread, so in concept this is like an agent using the `/btw` to get a quick
      answer from another agent.
    - `message` allows agents to talk to one another; these messages are added to the context of
      both the sender and receiver, allowing them to intercommunicate
  - webfetch
- Tool iterations are limited to 300 unattended steps
- Error handling is nice:
  - If a provider isn't responding after a while (~30 second timeout) then the harness retries up to
    3 times (configurable) with some jitter on the retry rate.
- Rebuilding this project when there's data in it from a prior run will preserve that old data in
  the rebuild.

## HTML client features

- A chat interface, w/ similar experience to the TUI, but with more "standard" keyboard shortcuts
  instead of emacs-style shortcuts. Text input is more like a textinput
- A view where you can see all sessions and jump into any of them
- Streaming markdown support when showing responses, with full GFM markdown support
  - Agents should be able to embed HTML into the markdown as well, in order to show diagrams etc.
- Agents can display "artifacts", self contained HTML files in a message such as for an interactive
  diagram/explanation.
- An expandable sidebar on the left side where you can see prior sessions and switch to them with a
  click. This sidebar is resizeable by dragging, and can be collapsed by a button.
- An identicon (https://en.wikipedia.org/wiki/Identicon) for every session, generated from an
  extraction of configurable semantic parts; this identicon needs to be able to be displayed really
  small (16x16 px), while being unique per session, but sessions about similar things should have
  similarities so that they are noticably related. I don't totally know how to do this, but I hope
  you'll give it a shot. Potentially try extracting a small set of subjects via an LLM call like the
  title generation LLM call.
  - The identicon in the favicon needs to have be slightly animated when a session is "in progress",
    so that a tab for a session that's in progress is clearly "doing something" at a glance, without
    messing up the overall view of the identicon
  - Identicons should use a three tier "domain > topic > specific" ontology, and calculation of the
    identicon should, if possible use the context of other recent identicons to inform a new
    identicon. For example, a session Y working on project 1 should probably have the same domain as
    session X that was also working on project 1. Ensure this context from recent other sessions is
    included when calculating new identicons so that existing trends can be propgated where it makes
    sense. We don't want every single session in the same folder to have the exact same domain/topic,
    since it's common to start all sessions in the exact same root folder but reference a project in
    a specific sub folder, and that project could be totally different session-to-session despite
    the sessions starting in the same directory. Interrogate that more thoroughly to figure it out.
  - Identicons should be generated automatically like titles, once after first title, then again
    automatically after the LLM first response hands back to the user for the first time.
- clicking links to local files should be able to crack them open in a terminal-in-page which opens
  that file in an editor (nvim for me)
  - links to files should only be clickable if they point to actual files that the program can see
    exists at the time the LLM generates the link, and relative files should be relative to the cwd
    of the message that the link is in.
- The current directory of a session can be changed after the fact; the cwd of an individual message should be tracked seperately from the cwd for the session.
  - When you start a session, you can choose the CWD with a default of the last new-session cwd, and
    a fallback default of "the same cwd as the server is running in" and the UI should show recent
    working directories in order of most recently used first.
  - Any time a current directory is chosen, the user should get a little file explorer in the
    browser to choose a folder in.
- The "branching" nature of sessions is represented in a lightweight way within the UI for a
  session. You can see that a branch started on a message, and you can see how many, and their
  titles. Clicking a branch goes "sideways" to that branch. The UI for a session with branches
  should show a kind of "branch minimap" so the user knows where they are.
  - In the conversation UI, there's a spacial display of other branches and their relationship to
    the current branch conversation. It's almost like a tree navigator, or evocative of one. Only
    branches directly connected to the current conversation are shown spacially. By "spacially" we
    mean that they are shown to the left and right of the conversation going down the middle of the
    page, with lines indicating the "break" in the messages where that new session began. Related
    branches aren't shown totally, just a small preview. Parent branches are shown on left, while
    child branches are shown on the right. When one of these "to the side" branches are clicked,
    there's a very fast animation which moves the clicked branch into being the "main branch" while
    the branch we were on moves to the left-or-right in accordance with the selected branch.
    - On wider viewports (e.g. a 4k screen), the spacial display shows much more of these connected
      branches to the left and right. In slightly narrower viewports, maybe only some low-contrast
      bit of the title is shown. And if the viewport is narrower even than that, just show a little
      "node" coming off the conversation, a node requiring hover to see the text of what it goes to.
    - These side branches should use the same UI conventions used for branching sessions:
      - dashed lines/outlines for subagents
      - solid lines for forks
      - Same icons as used for the different kinds
  - The minimap displays as a floating semi-transparent sublime text style minimap, and when clicked
    can be expanded into an interactable sidebar which can be closed back to a minimap
  - The minimap should display like a "git graph" with nodes for every branch point. Conceptually,
    think of this like a git graph, where every user message is a commit and every "final message
    in an agents turn" is also a commit on the git graph. however, all "straight-line" commits
    should be omitted for brevity. So a message history like this:
      ```
    * User message: Investigate XYZ
    |
    * Agent turn message: Summary of XYZ
    |
    * User message: What about consideration ABC
    |
    * Agent turn message: How ABC affects XYZ
    |
    * User message: Plan for solving XYZ
    |
    * Agent turn message: Here's a plan for solving XYZ
    | \
    |  |
    |  * User message: Iplament solution for XXZ  # first message sent, user misspelled so edits
    |
    * ↺ User message: Implement solution for XYZ # It's result of edit, so it gets ↺ symbol
    |
    * Agent turn message: Solution is implemented for XYZ
    | \
    |  |
    |  * User message: Could we consider a tweak PQR for the implementation here?
    |
    * User message: Review implementation for XYZ
      ```
    Would be displayed like this in the minimap:
      ```
    * User message: Investigate XYZ
    |
    * Agent turn message: Here's a plan for solving XYZ
    | \
    |  * User message: Iplament solution for XXZ  # first message sent, user misspelled so edits
    |
    * ↺ User message: Implement solution for XYZ # It's result of edit, so it gets ↺ symbol
    |
    * Agent turn message: Solution is implemented for XYZ
    | \
    |  * User message: Could we consider a tweak PQR for the implementation here?
    |
    * User message: Review implementation for XYZ
      ```
    - Note though that in the real implementation, the minimap view would not show actual excerpts
      from the messages. Instead, it'd show
      - The first message in a new branch would have the title of the thread per how titles are calculated.
      - a smaller no-title node display
    - Fresh subagents attach to the parent agents turns final message (in the minimap, spacial view
      has different rules)
    - Row order is to use whatever `git log --graph` uses, which should be time-order
    - Clicking a node in the minimap switches to its thread AND scrolls to that message
    - Per thread controls in the minimap are shown on hover over any node in that thread.
    - When `main` changes, the thread marked as `main` becomes the leftmost branch in the graph (the
      long straight one).


## Minimal TUI features

- Open a new fresh session in current directory by default, but connected to the server
- list sessions
- resume session
- line edit that supports following keyboard shortcuts for prompt editing:
  - ctrl-a start of line
  - ctrl-e end of line
  - ctrl-j starts a newline
  - arrow keys move arround
  - arrow key up on first line of prompt goes to prior message
  - arrow key down on last line of prompt goes to next message, if there is one
- thinking is displayed all the time, but in a different font than output text
- Scrolling the mouse wheel on the TUI scrolls backwards through session output




For the LLM: Never update this README file. Humans write what this project is, and the features
needed. No LLM should ever change this.
