# Which LLM providers?

- status: done
- asked: 2026-11-18
- answered: 2026-11-18

## Question

v1: Anthropic only, plus a mock.

## Answer

OpenAI-compatible as the default, with Anthropic as a maybe. README: configurable via `$AI_ENDPOINT`, `$AI_MODEL`, `$AI_TOKEN`, more providers via settings.

## Notes

Built-in `env` provider (openai type) from AI_* vars (+ optional `AI_CHEAP_MODEL`); `providers` setting for more; anthropic + mock kept.
