# PRD — Phase 3: Policy-based Rebalancing Advisor (Brokerless)

## Goal
Convert `data/policy/policy.yml` into a monthly **buy-order proposal** (no broker integration).
Execution stays manual (human approval gate).

## Inputs
- `data/policy/policy.yml`
  - `policy.target_allocation` (list of `{bucket, pct}`)
  - `policy.contribution_schedule.planned_installment_eur`
  - `policy.guardrails.min_trade_eur`
  - `policy.instruments` (per bucket; `{id, isin, name, weight_within_bucket}`)

## Outputs
- `orders/proposed_YYYY-MM.json`
- `reports/orders_YYYY-MM.md`

## Core Behavior
- Monthly budget → bucket allocation → instrument split within each bucket
- Rounding: round-half-up to nearest EUR
- Remainder: assign to highest effective weight (deterministic)
- Buy-only proposals: `allow_sells=false`
- Respect `min_trade_eur`

## CLI
- `wealth_agents propose-orders --month YYYY-MM`

## Determinism
Same policy + month + budget ⇒ identical JSON/MD outputs.

## Non-goals
- Broker API execution
- Real-time price optimization (shares/partial fills)
- Tax-aware optimization

## Tests
- Smoke test verifies:
  - totals sum to budget
  - buy-only
  - expected allocations
  - `instrument_id` present in each order

## Next
- Phase 3.7: backtest/simulation using fetched prices
- Phase 4: tracking executed orders + snapshots + monitoring

