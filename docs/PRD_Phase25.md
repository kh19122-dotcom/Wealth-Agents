# PRD — Phase 2.5: Policy Review Engine (Proposal + Quarterly Apply Guardrail)

## Goal
Bridge Phase 1 signals → Phase 2 policy via **proposal-only** review.
Generate recommended allocation adjustments from weekly aggregates.
**Applying** changes to policy is restricted to **once per quarter**.

## Inputs
- `data/meta/weekly_aggregates.jsonl` (Phase 1)
- `data/policy/policy.yml` (Phase 2)

## Outputs
- `reports/policy_review_YYYY-Www.md`
- `data/policy/policy_patch_YYYY-Www.yml`
- (Optional if `--apply` is implemented) `data/policy/policy_apply_history.jsonl`

## Guardrails
- Proposal generation: always allowed (subject to signal thresholds)
- Apply: max once per quarter (based on apply history)
- Allowed changes (MVP):
  - shift only between `global_equity` and `bonds_cashlike`
  - max magnitude: ±5 percentage points per proposal/apply
- `optional_gold` fixed in MVP (no changes)
- Never auto-change constraints (`no_crypto`, `no_leverage`, `sell_allowed`, etc.)

## Signal Logic (Rule-based; no LLM)
- Use weekly aggregates (`category_counts`, `keyword_counts`)
- Default trigger requires persistence (>=2 consecutive weeks)
- If previous week is missing (bootstrap phase), proposals are not generated (or can be enabled with a higher threshold in a follow-up)
- Emit rationale and signal summary in the report

## CLI
- `wealth_agents policy-review --week YYYY-Www`
- Optional future:
  - `--apply` + explicit confirmation + quarterly lockout + history append

## Report Content
- week, generated_at, policy_hash
- Top Signals (WoW)
- Guardrails Check:
  - `proposal_allowed`, `proposal_generated`
  - `apply_allowed` (+ reason; based on apply history)
- Proposals section: 0–1 proposal, or explicit reason for none

## Tests
- Report reflects proposal/apply separation
- Patch format stable and deterministic
- Missing previous week yields no proposal (current behavior)

