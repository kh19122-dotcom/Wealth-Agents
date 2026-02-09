# Phase 3 MVP Design Notes

Phase 3 adds a policy-based monthly order proposal step with manual execution.

## Scope

- Input policy: `data/policy/policy.yml`
- Command: `wealth_agents propose-orders --month YYYY-MM [--amount EUR]`
- Output JSON: `orders/proposed_YYYY-MM.json`
- Output report: `reports/orders_YYYY-MM.md`
- BUY-only proposal (no broker integration, no SELL generation)

## Policy Source Of Truth

Phase 3 reads these fields from `data/policy/policy.yml`:

- `policy_hash` (top-level)
- `policy.target_allocation` (`[{bucket, pct}]`, where `pct` is integer percent)
- `policy.contribution_schedule.planned_installment_eur` (default budget)
- `policy.guardrails.min_trade_eur` (minimum trade threshold)
- `policy.instruments` (bucket-to-instrument mapping)

`policy.instruments` example:

```yaml
policy:
  instruments:
    global_equity:
      - id: sp500_acc
        isin: IE00B5BMR087
        name: iShares Core S&P 500 UCITS ETF (Acc)
        weight_within_bucket: 0.70
```

## Deterministic Allocation Rules

1. Budget comes from `--amount` if provided, otherwise `policy.contribution_schedule.planned_installment_eur`.
2. Convert target bucket `pct` to fractions (`pct / 100.0`).
3. Compute bucket amount, then split within bucket by `weight_within_bucket`.
4. Round each instrument amount to nearest EUR (half-up).
5. If rounded total differs from budget, distribute remainder deterministically:
   - positive remainder: highest effective weight first
   - stable tie-break: `(bucket, instrument_id)`
6. Enforce `min_trade_eur`: any order below threshold is rolled into the largest order deterministically.

## Output JSON Fields

- `month`
- `currency` (`EUR`)
- `budget_eur`
- `policy_hash`
- `assumptions` (rounding, remainder, buy-only, allow_sells=false, min_trade_eur)
- `orders` (`[{side, instrument_id, isin, name, bucket, amount_eur}]`)
