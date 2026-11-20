# `stream_options.include_usage` on OpenAI-compatible endpoints

- status: open
- asked: 2026-11-19
- answered:

## Question

To get token usage from streaming Chat Completions, requests now send `"stream_options":
{"include_usage": true}` (standard OpenAI; your proxy, ollama and llama.cpp accept it). A server that
rejects unknown fields would fail with a 400. Add a per-provider switch to turn it off, or wait until
someone hits that?

## Answer

