# Dev Log — Wealth-Agents (Phase 3 + Phase 2.5)

## Summary
This cycle shipped Phase 3 (order proposal) and Phase 2.5 (policy review proposals) to connect:
Policy → monthly buy-order plan, and Signals → policy change suggestions (proposal-only).

---

## Phase 3 — Monthly Buy-Order Proposal

### What shipped
- CLI: `wealth_agents propose-orders --month YYYY-MM`
- Reads `data/policy/policy.yml`
- Outputs:
  - `orders/proposed_YYYY-MM.json`
  - `reports/orders_YYYY-MM.md`
- Deterministic allocation + rounding rules
- Buy-only proposal behavior
- Added `instrument_id` for traceability in order records

### Local validation
- `.venv` used as runtime environment
- PyYAML installed inside venv
- All tests passed (`pytest -q`)

### Notes
- Generated artifacts like `orders/` should be ignored from git (add to `.gitignore`)

---

## Phase 2.5 — Policy Review Engine

### What shipped
- CLI: `wealth_agents policy-review --week YYYY-Www`
- Outputs:
  - `reports/policy_review_YYYY-Www.md`
  - `data/policy/policy_patch_YYYY-Www.yml`

### Guardrails update (v1.1)
- Proposal generation separated from apply cadence:
  - `proposal_allowed: YES` always (threshold-based)
  - `apply_allowed: YES/NO` based on apply history (quarterly)
- Report reflects both decisions clearly

### First run observed
- Only one week of aggregates existed (W06), so previous-week persistence check failed
- Result: no proposal generated; apply allowed (no apply history)

### Next improvements
- Bootstrap rule when previous week is missing (high-threshold single-week trigger)
- Expand risk-on/risk-off mapping beyond initial keywords
- Optional `--apply` with confirmation + append-only apply history

---

## Operational recommendations
- Keep venv deterministic; add missing dependencies to `pyproject.toml` if needed
- Decide whether to commit generated reports/patches or treat them as runtime artifacts

