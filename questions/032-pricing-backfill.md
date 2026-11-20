# Re-price sessions recorded before price lists were used?

- status: open
- asked: 2026-11-19
- answered:

## Question

Costs are now estimated from the provider's `/settings/api/pricing` when a response has no cost. Sessions
recorded earlier have token counts on each assistant message but no cost, and their totals say
`cost n/a`. Should the harness backfill them, i.e. price each stored message's usage with its recorded
model? Title, `/btw` and `/compact` calls only exist in the session total, without a model, so a backfill
would miss those. Also: should a model that isn't in the price list fall back to some default
(e.g. the provider's main model's price), or stay unpriced, as it does now?

## Answer

