from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from .policy import (
    append_policy_history,
    read_yaml,
    stable_policy_hash,
    to_jsonable_copy,
    validate_allocation_sum,
    validate_band_pct,
    write_yaml,
)


DEFAULT_INPUT_PATH = "data/policy/ips_inputs.yml"
DEFAULT_DRAFT_PATH = "data/policy/policy_draft.yml"
DEFAULT_POLICY_PATH = "data/policy/policy.yml"
DEFAULT_HISTORY_PATH = "data/policy/policy_history.jsonl"
DEFAULT_QUESTIONS_PATH = "reports/ips_questions.md"
DEFAULT_REPORT_DIR = "reports"


def init_ips_files(
    input_path: str = DEFAULT_INPUT_PATH,
    questions_path: str = DEFAULT_QUESTIONS_PATH,
) -> tuple[Path, Path]:
    template = {
        "base_currency": "EUR",
        "investable_amount_eur": 40000,
        "cash_buffer_eur": 10000,
        "horizon_years": 10,
        "risk_tolerance": "medium",
        "contribution_plan": {
            "type": "lump_sum_split",
            "months": 12,
        },
        "allowed_assets": {
            "equities": True,
            "bonds_or_cashlike": True,
            "gold_optional": True,
        },
        "constraints": {
            "no_leverage": True,
            "no_short": True,
            "no_crypto": True,
            "sell_allowed": False,
        },
        "rebalance": {
            "frequency": "quarterly",
            "band_pct": 5,
        },
    }

    questions_md = """# IPS Questions Checklist

Use this checklist to complete `data/policy/ips_inputs.yml` before drafting your policy.

## Core Profile

- Confirm your `base_currency` (default: EUR).
- Confirm `investable_amount_eur` and `cash_buffer_eur`.
- Confirm `horizon_years`.
- Choose `risk_tolerance`: low / medium / high.

## Contribution Plan

- Select `contribution_plan.type`: `lump_sum_split` or `monthly`.
- Set `contribution_plan.months` (e.g., 6, 12).

## Allowed Assets

- Decide if equities are allowed.
- Decide if bonds or cash-like instruments are allowed.
- Decide if optional gold is allowed.

## Constraints

- Confirm no leverage.
- Confirm no shorting.
- Confirm crypto exclusion.
- Confirm if selling is allowed (`sell_allowed`) or buy-only.

## Rebalancing

- Confirm rebalance frequency (default: quarterly).
- Confirm rebalance band percent (default: 5).
"""

    out_input = write_yaml(input_path, template)
    out_questions = Path(questions_path)
    out_questions.parent.mkdir(parents=True, exist_ok=True)
    out_questions.write_text(questions_md, encoding="utf-8")
    return out_input, out_questions


def draft_policy(
    input_path: str = DEFAULT_INPUT_PATH,
    draft_path: str = DEFAULT_DRAFT_PATH,
    report_dir: str = DEFAULT_REPORT_DIR,
) -> tuple[Path, Path]:
    user_input = read_yaml(input_path)
    merged = _merge_defaults(user_input, _default_inputs())
    _validate_inputs(merged)

    candidates = _build_candidates(merged)
    draft_doc = {
        "generated_at": _now_iso8601(),
        "inputs_snapshot": merged,
        "candidates": candidates,
    }
    out_draft = write_yaml(draft_path, draft_doc)

    report_path = Path(report_dir) / f"ips_{date.today().isoformat()}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_ips_report(merged, candidates), encoding="utf-8")
    return out_draft, report_path


def finalize_policy(
    choice: str,
    draft_path: str = DEFAULT_DRAFT_PATH,
    policy_path: str = DEFAULT_POLICY_PATH,
    history_path: str = DEFAULT_HISTORY_PATH,
) -> tuple[Path, str]:
    draft_doc = read_yaml(draft_path)
    candidates = draft_doc.get("candidates") or {}
    if not isinstance(candidates, dict) or not candidates:
        raise ValueError(f"No candidates found in draft file: {draft_path}")

    if choice not in candidates:
        available = ", ".join(sorted(candidates.keys()))
        raise ValueError(f"Invalid choice '{choice}'. Available candidates: {available}.")

    created_at = _now_iso8601()
    version = date.today().isoformat()
    selected = to_jsonable_copy(candidates[choice])
    inputs_snapshot = to_jsonable_copy(draft_doc.get("inputs_snapshot") or {})

    hash_basis = {
        "selected_candidate": choice,
        "inputs_snapshot": inputs_snapshot,
        "policy": selected,
    }
    policy_hash = stable_policy_hash(hash_basis)
    policy_doc = {
        "policy_version": version,
        "created_at": created_at,
        "policy_hash": policy_hash,
        "selected_candidate": choice,
        "inputs_snapshot": inputs_snapshot,
        "policy": selected,
    }

    out_policy = write_yaml(policy_path, policy_doc)
    append_policy_history(
        history_path,
        {
            "event": "finalize_policy",
            "created_at": created_at,
            "policy_version": version,
            "choice": choice,
            "policy_hash": policy_hash,
            "policy_path": str(out_policy),
        },
    )
    return out_policy, policy_hash


def _default_inputs() -> dict[str, Any]:
    return {
        "base_currency": "EUR",
        "investable_amount_eur": 40000,
        "cash_buffer_eur": 10000,
        "horizon_years": 10,
        "risk_tolerance": "medium",
        "contribution_plan": {
            "type": "lump_sum_split",
            "months": 12,
        },
        "allowed_assets": {
            "equities": True,
            "bonds_or_cashlike": True,
            "gold_optional": True,
        },
        "constraints": {
            "no_leverage": True,
            "no_short": True,
            "no_crypto": True,
            "sell_allowed": False,
        },
        "rebalance": {
            "frequency": "quarterly",
            "band_pct": 5,
        },
    }


def _merge_defaults(user_values: dict[str, Any], defaults: dict[str, Any]) -> dict[str, Any]:
    merged = {}
    for key, default_value in defaults.items():
        if key not in user_values:
            merged[key] = default_value
            continue
        user_value = user_values[key]
        if isinstance(default_value, dict) and isinstance(user_value, dict):
            merged[key] = _merge_defaults(user_value, default_value)
        else:
            merged[key] = user_value
    for key, value in user_values.items():
        if key not in merged:
            merged[key] = value
    return merged


def _validate_inputs(inputs: dict[str, Any]) -> None:
    currency = str(inputs.get("base_currency", "")).strip()
    if not currency:
        raise ValueError("base_currency is required.")

    _validate_positive_number("investable_amount_eur", inputs.get("investable_amount_eur"))
    _validate_nonnegative_number("cash_buffer_eur", inputs.get("cash_buffer_eur"))
    _validate_positive_int("horizon_years", inputs.get("horizon_years"))

    risk = str(inputs.get("risk_tolerance", "")).strip().lower()
    if risk not in {"low", "medium", "high"}:
        raise ValueError("risk_tolerance must be one of: low, medium, high.")

    contrib = inputs.get("contribution_plan") or {}
    if not isinstance(contrib, dict):
        raise ValueError("contribution_plan must be a mapping.")
    contrib_type = str(contrib.get("type", "")).strip()
    if contrib_type not in {"lump_sum_split", "monthly"}:
        raise ValueError("contribution_plan.type must be 'lump_sum_split' or 'monthly'.")
    _validate_positive_int("contribution_plan.months", contrib.get("months"))

    allowed = inputs.get("allowed_assets") or {}
    if not isinstance(allowed, dict):
        raise ValueError("allowed_assets must be a mapping.")
    flags = {
        "equities": bool(allowed.get("equities")),
        "bonds_or_cashlike": bool(allowed.get("bonds_or_cashlike")),
        "gold_optional": bool(allowed.get("gold_optional")),
    }
    if not any(flags.values()):
        raise ValueError("At least one allowed asset bucket must be enabled.")

    rebalance = inputs.get("rebalance") or {}
    if not isinstance(rebalance, dict):
        raise ValueError("rebalance must be a mapping.")
    validate_band_pct(rebalance.get("band_pct"))


def _validate_positive_number(name: str, value: Any) -> None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive number.")
    if number <= 0:
        raise ValueError(f"{name} must be a positive number.")


def _validate_nonnegative_number(name: str, value: Any) -> None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a non-negative number.")
    if number < 0:
        raise ValueError(f"{name} must be a non-negative number.")


def _validate_positive_int(name: str, value: Any) -> None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a positive integer.")
    if number <= 0:
        raise ValueError(f"{name} must be a positive integer.")


def _build_candidates(inputs: dict[str, Any]) -> dict[str, dict[str, Any]]:
    profiles = {
        "conservative": {
            "global_equity": 30.0,
            "bonds_cashlike": 65.0,
            "optional_gold": 5.0,
            "max_single_asset_pct": 70,
            "pros": "Lower expected drawdowns and smoother capital preservation profile.",
            "cons": "May underperform in strong equity bull markets.",
            "risks": "Inflation may erode real returns if equity allocation is too low.",
        },
        "balanced": {
            "global_equity": 60.0,
            "bonds_cashlike": 35.0,
            "optional_gold": 5.0,
            "max_single_asset_pct": 80,
            "pros": "Balances growth potential with downside dampening from defensive assets.",
            "cons": "Can still experience meaningful losses during market stress.",
            "risks": "Sequence risk remains if major drawdowns occur early in horizon.",
        },
        "aggressive": {
            "global_equity": 80.0,
            "bonds_cashlike": 15.0,
            "optional_gold": 5.0,
            "max_single_asset_pct": 90,
            "pros": "Highest long-run growth potential among provided candidates.",
            "cons": "Largest volatility and drawdown profile of the three candidates.",
            "risks": "Behavioral risk: strategy may be hard to hold through sharp downturns.",
        },
    }

    contribution = inputs["contribution_plan"]
    rebalance = inputs["rebalance"]
    constraints = inputs.get("constraints") or {}
    allowed_assets = inputs["allowed_assets"]

    buy_only = not bool(constraints.get("sell_allowed", False))
    band_pct = float(validate_band_pct(rebalance.get("band_pct")))
    months = int(contribution.get("months"))
    investable_amount = float(inputs["investable_amount_eur"])
    scheduled = round(investable_amount / months, 2)

    out: dict[str, dict[str, Any]] = {}
    for name, profile in profiles.items():
        adjusted = _adjust_weights_for_allowed_assets(
            {
                "global_equity": profile["global_equity"],
                "bonds_cashlike": profile["bonds_cashlike"],
                "optional_gold": profile["optional_gold"],
            },
            allowed_assets,
        )
        allocation = [{"bucket": bucket, "pct": pct} for bucket, pct in adjusted.items() if pct > 0]
        validate_allocation_sum(allocation)
        out[name] = {
            "target_allocation": allocation,
            "rebalance_rules": {
                "frequency": rebalance.get("frequency", "quarterly"),
                "band_pct": band_pct,
                "buy_only": buy_only,
            },
            "contribution_schedule": {
                "type": contribution.get("type", "lump_sum_split"),
                "months": months,
                "planned_installment_eur": scheduled,
            },
            "guardrails": {
                "max_single_asset_pct": profile["max_single_asset_pct"],
                "min_trade_eur": 50,
            },
            "notes": {
                "rationale": profile["pros"],
                "risks": profile["risks"],
                "pros": profile["pros"],
                "cons": profile["cons"],
            },
        }
    return out


def _adjust_weights_for_allowed_assets(
    weights: dict[str, float],
    allowed_assets: dict[str, Any],
) -> dict[str, int]:
    equities_allowed = bool(allowed_assets.get("equities"))
    bonds_allowed = bool(allowed_assets.get("bonds_or_cashlike"))
    gold_allowed = bool(allowed_assets.get("gold_optional"))

    values = dict(weights)
    if not gold_allowed and values["optional_gold"] > 0:
        transfer = values["optional_gold"]
        values["optional_gold"] = 0
        if bonds_allowed:
            values["bonds_cashlike"] += transfer
        elif equities_allowed:
            values["global_equity"] += transfer

    if not equities_allowed and values["global_equity"] > 0:
        transfer = values["global_equity"]
        values["global_equity"] = 0
        if bonds_allowed:
            values["bonds_cashlike"] += transfer
        elif gold_allowed:
            values["optional_gold"] += transfer

    if not bonds_allowed and values["bonds_cashlike"] > 0:
        transfer = values["bonds_cashlike"]
        values["bonds_cashlike"] = 0
        if equities_allowed:
            values["global_equity"] += transfer
        elif gold_allowed:
            values["optional_gold"] += transfer

    filtered = {
        "global_equity": values["global_equity"] if equities_allowed else 0.0,
        "bonds_cashlike": values["bonds_cashlike"] if bonds_allowed else 0.0,
        "optional_gold": values["optional_gold"] if gold_allowed else 0.0,
    }
    if sum(filtered.values()) <= 0:
        raise ValueError("No assets available after applying allowed_assets constraints.")
    return _normalize_to_100(filtered)


def _normalize_to_100(weights: dict[str, float]) -> dict[str, int]:
    total = sum(v for v in weights.values() if v > 0)
    if total <= 0:
        raise ValueError("Allocation total must be positive.")

    scaled = {k: (v / total) * 100.0 for k, v in weights.items()}
    ints = {k: int(v) for k, v in scaled.items()}
    remainder = 100 - sum(ints.values())
    fractions = sorted(
        ((scaled[k] - ints[k], k) for k in scaled),
        reverse=True,
    )
    idx = 0
    while remainder > 0 and fractions:
        _, key = fractions[idx % len(fractions)]
        ints[key] += 1
        remainder -= 1
        idx += 1
    return ints


def _render_ips_report(inputs: dict[str, Any], candidates: dict[str, dict[str, Any]]) -> str:
    lines: list[str] = []
    lines.append(f"# IPS Draft {date.today().isoformat()}")
    lines.append("")
    lines.append("## Inputs Summary")
    lines.append("")
    lines.append(f"- base_currency: {inputs.get('base_currency')}")
    lines.append(f"- investable_amount_eur: {inputs.get('investable_amount_eur')}")
    lines.append(f"- cash_buffer_eur: {inputs.get('cash_buffer_eur')}")
    lines.append(f"- horizon_years: {inputs.get('horizon_years')}")
    lines.append(f"- risk_tolerance: {inputs.get('risk_tolerance')}")
    lines.append(
        f"- contribution_plan: {inputs['contribution_plan']['type']} over {inputs['contribution_plan']['months']} months"
    )
    lines.append(
        f"- rebalance: {inputs['rebalance']['frequency']} with band {inputs['rebalance']['band_pct']}%"
    )
    lines.append("")
    lines.append("## Candidates")
    lines.append("")

    for name in ("conservative", "balanced", "aggressive"):
        candidate = candidates[name]
        lines.append(f"### {name.title()}")
        alloc = ", ".join(
            f"{entry['bucket']} {entry['pct']}%" for entry in candidate.get("target_allocation", [])
        )
        lines.append(f"- target_allocation: {alloc}")
        lines.append(
            f"- rebalance_rules: {candidate['rebalance_rules']['frequency']} / band {candidate['rebalance_rules']['band_pct']}% / buy_only={candidate['rebalance_rules']['buy_only']}"
        )
        lines.append(f"- pros: {candidate['notes']['pros']}")
        lines.append(f"- cons: {candidate['notes']['cons']}")
        lines.append(f"- risks: {candidate['notes']['risks']}")
        lines.append("")

    lines.append("## What You Should Decide Next")
    lines.append("")
    lines.append("- Choose one candidate policy: conservative, balanced, or aggressive.")
    lines.append("- Confirm that your cash buffer is enough for near-term needs.")
    lines.append("- Confirm whether buy-only rebalancing is acceptable.")
    lines.append("- Confirm whether optional gold should remain enabled.")
    lines.append("")
    lines.append("## Disclaimer")
    lines.append("")
    lines.append("This is a planning tool, not financial advice.")
    return "\n".join(lines).strip() + "\n"


def _now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
