# Retry timeouts: 30 s to start, 120 s of silence mid-stream

- status: open
- asked: 2026-11-19
- answered:

## Question

README: "if a provider isn't responding after ~30 seconds, retry up to 3 times with jitter". Applied
literally to the whole stream, a proxy that hides reasoning (see [023](023-proxy-no-reasoning.md))
can be silent for >30 s while the model thinks, and every such answer would be thrown away and
retried. So there are two settings: `request_timeout` = 30 s (connect + response start) and
`stream_idle_timeout` = 120 s (silence after the answer started). A retry discards the partial
output and streams the whole message again. OK, or should mid-stream stalls not be retried at all?

## Answer

