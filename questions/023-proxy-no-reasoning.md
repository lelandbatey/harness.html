# No thinking blocks via the OpenAI-compatible proxy

- status: open
- asked: 2026-11-18
- answered:

## Question

The proxy at AI_ENDPOINT never sends reasoning fields and ignores `reasoning_effort`/`thinking`, so no thinking is displayed and `thinking_budget` does nothing for openai-type providers. Add an `anthropic`-type provider pointed at the proxy (if it serves /v1/messages) to get thinking?

## Answer

