# Phase 2.5 Policy Review Engine (MVP)

Phase 2.5 adds a deterministic policy review step that produces a proposal patch and markdown review.
Proposal generation and apply cadence are separated:

- proposal generation depends only on signal rules
- apply cadence is guarded quarterly

## Command

```bash
wealth_agents policy-review --week YYYY-Www
```

Optional apply mode:

```bash
wealth_agents policy-review --week YYYY-Www --apply --yes
```

## Inputs

- Weekly aggregates: `data/meta/weekly_aggregates.jsonl`
- Policy: `data/policy/policy.yml`
  - `policy_hash`
  - `policy.target_allocation`
- Apply history: `data/policy/policy_apply_history.jsonl`

## Outputs

- Report: `reports/policy_review_YYYY-Www.md`
- Patch: `data/policy/policy_patch_YYYY-Www.yml`

Patch schema:

```yaml
week: 2026-W06
policy_hash_base: <policy_hash>
cadence: quarterly
proposals: []
```

When a proposal is triggered, `proposals` contains exactly one `risk_off_shift_5pp` entry:

- `global_equity: -5`
- `bonds_cashlike: +5`
- `optional_gold`: unchanged

## Guardrails

- `proposal_allowed` is always `YES` in MVP.
- `apply_allowed` is evaluated from `policy_apply_history.jsonl` (quarterly lockout).
- If apply history is missing, `apply_allowed` is treated as `YES`.
- Never changes constraints or rebalance rules automatically.
- Patch file is always emitted even when `apply_allowed` is `NO`.

## Trigger Logic (Rule-Based)

- Computes deterministic `risk_off_score` from weekly aggregates:
  - `risk_off_score = keyword_counts["inflation"] + keyword_counts["rate hike"]` (missing keys treated as `0`)
- Default (two-week) trigger:
  - `this_week_score >= 2` and `previous_week_score >= 1`
- Bootstrap trigger (only when previous week record is missing):
  - `this_week_score >= 3`
- If trigger condition holds, emits a single defensive proposal (`global_equity -5pp`, `bonds_cashlike +5pp`).

## Apply History Schema

When `--apply` succeeds, append one JSON line to `data/policy/policy_apply_history.jsonl`:

```json
{"applied_at":"2026-04-15T12:00:00Z","week":"2026-W15","base_policy_hash":"...","new_policy_hash":"...","proposal_id":"risk_off_shift_5pp"}
```
