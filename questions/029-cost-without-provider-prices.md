# Cost when the provider doesn't report one

- status: answered
- asked: 2026-11-19
- answered: 2026-11-19

## Question

The README says to track cost "if it's returned by the provider". Your proxy (`$AI_ENDPOINT`) returns
token usage but no cost, so the session shows `cost n/a · 12.3k in · 2.1k out`. Should the harness
also compute a cost from a price table, e.g. a per-provider/per-model `prices` setting ($/M input,
output, cache read, cache write)?

## Answer

Answered by the README (2026-11-19): use the provider's `/settings/api/pricing` API to estimate
costs when the completion API doesn't return them. Implemented (DESIGN v8). Estimates show with `~`.
A local price-table setting was not added; a provider can point `pricing_url` at another price list.
