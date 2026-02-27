from datetime import date, datetime, timezone
import json
from pathlib import Path
import re
from typing import Any, Optional

from .policy import (
    append_policy_history,
    read_yaml,
    stable_policy_hash,
    to_jsonable_copy,
    validate_allocation_sum,
    validate_band_pct,
    write_yaml,
)
from .signals import risk_off_score, risk_on_score, top_regime_drivers
from .storage import read_jsonl


DEFAULT_INPUT_PATH = "data/policy/ips_inputs.yml"
DEFAULT_DRAFT_PATH = "data/policy/policy_draft.yml"
DEFAULT_POLICY_PATH = "data/policy/policy.yml"
DEFAULT_HISTORY_PATH = "data/policy/policy_history.jsonl"
DEFAULT_QUESTIONS_PATH = "reports/ips_questions.md"
DEFAULT_REPORT_DIR = "reports"
DEFAULT_WEEKLY_AGGREGATES_PATH = "data/meta/weekly_aggregates.jsonl"
DEFAULT_SIGNAL_MAX_TILT_PCT = 5
ISO_WEEK_PATTERN = re.compile(r"^\d{4}-W\d{2}$")


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
    weekly_aggregates_path: Optional[str] = None,
    week: Optional[str] = None,
    max_signal_tilt_pct: int = DEFAULT_SIGNAL_MAX_TILT_PCT,
    simulation_feedback_path: Optional[str] = None,
) -> tuple[Path, Path]:
    user_input = read_yaml(input_path)
    merged = _merge_defaults(user_input, _default_inputs())
    _validate_inputs(merged)
    calibrated_max_tilt, calibration = _calibrate_signal_tilt_cap(
        max_signal_tilt_pct=max_signal_tilt_pct,
        simulation_feedback_path=simulation_feedback_path,
    )
    signal_overlay = _build_signal_overlay(
        weekly_aggregates_path=weekly_aggregates_path,
        week=week,
        max_signal_tilt_pct=calibrated_max_tilt,
        calibration=calibration,
    )

    candidates = _build_candidates(merged, signal_overlay=signal_overlay)
    draft_doc = {
        "generated_at": _now_iso8601(),
        "inputs_snapshot": merged,
        "signal_overlay": signal_overlay,
        "candidates": candidates,
    }
    out_draft = write_yaml(draft_path, draft_doc)

    report_path = Path(report_dir) / f"ips_{date.today().isoformat()}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(_render_ips_report(merged, candidates, signal_overlay=signal_overlay), encoding="utf-8")
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
    signal_overlay = draft_doc.get("signal_overlay")
    signal_overlay_copy = to_jsonable_copy(signal_overlay) if isinstance(signal_overlay, dict) else None

    # Thesis satellite sleeve (default: disabled)
    mode = str(inputs_snapshot.get("mode", "")).strip().lower()
    review_m = inputs_snapshot.get("review_after_months")
    try:
        review_m = int(review_m) if review_m is not None else 6
    except (TypeError, ValueError):
        review_m = 6

    thesis = {
        "enabled": False,
        "target_pct": 0,
        "max_pct": 10,
        "review_after_months": review_m,
        "allowed_types": ["thematic_equity", "industrial_manufacturing", "infrastructure"],
        # Rule: when enabled, reduce global_equity first to keep total at 100%
        "funding_rule": {"source_bucket": "global_equity"},
        "notes": "Satellite (thesis) sleeve reserved for later activation; default OFF in scout.",
    }
    # Attach into the finalized policy document
    selected.setdefault("thesis_sleeve", thesis)


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
    if signal_overlay_copy:
        policy_doc["signal_overlay"] = signal_overlay_copy

    # Optional: annotate finalized policy notes with IPS meta (e.g., scout mode)
    mode = str(inputs_snapshot.get("mode", "")).strip().lower()
    review_months = inputs_snapshot.get("review_after_months")

    if mode == "scout":
        try:
            m = int(review_months) if review_months is not None else 6
        except (TypeError, ValueError):
            m = 6

        notes = policy_doc["policy"].setdefault("notes", {})
        if not isinstance(notes, dict):
            policy_doc["policy"]["notes"] = {"rationale": str(notes)}
            notes = policy_doc["policy"]["notes"]

        notes["scout_mode"] = f"Scout allocation: initial budget; review after {m} months."

    out_policy = write_yaml(policy_path, policy_doc)
    # Also write a human-readable IPS report for quick review
    try:
        report_md = _render_ips_report_md(policy_doc, draft_doc=draft_doc)
        report_path = Path("reports") / "ips_report.md"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(report_md, encoding="utf-8")
    except Exception:
        # Report is a convenience output; do not block policy finalization
        pass

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
        "mode": "standard",
        "review_after_months": None,
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


def _build_signal_overlay(
    weekly_aggregates_path: Optional[str],
    week: Optional[str],
    max_signal_tilt_pct: int,
    calibration: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    max_tilt = max(0, int(max_signal_tilt_pct))
    base = {
        "enabled": False,
        "week": None,
        "previous_week": None,
        "state": "neutral",
        "tilt_pct": 0,
        "max_tilt_pct": max_tilt,
        "risk_off_score": 0,
        "risk_on_score": 0,
        "net_score": 0,
        "drivers": [],
        "calibration": calibration or {
            "enabled": False,
            "requested_max_tilt_pct": max_tilt,
            "effective_max_tilt_pct": max_tilt,
            "reason": "No simulation feedback calibration applied.",
        },
        "reason": "No weekly aggregate signal source configured.",
    }

    if not weekly_aggregates_path:
        return base

    aggregate_path = Path(weekly_aggregates_path)
    if not aggregate_path.exists():
        base["reason"] = f"Weekly aggregates not found: {aggregate_path}"
        return base

    by_week: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(aggregate_path):
        if not isinstance(row, dict):
            continue
        normalized = _try_normalize_week(row.get("week"))
        if not normalized:
            continue
        by_week[normalized] = row
    if not by_week:
        base["reason"] = f"No valid weekly aggregate rows found in {aggregate_path}."
        return base

    target_week = _normalize_week_or_raise(week) if week else sorted(by_week.keys())[-1]
    current = by_week.get(target_week)
    if current is None:
        raise ValueError(f"Requested week {target_week} not found in weekly aggregates: {aggregate_path}")
    previous_week = _previous_week_key(target_week)
    previous = by_week.get(previous_week)

    risk_off = risk_off_score(current)
    risk_on = risk_on_score(current)
    net_score = risk_off - risk_on
    state = "neutral"
    tilt = 0
    if net_score >= 2:
        state = "risk_off"
        tilt = 4 if net_score >= 6 else 2
    elif net_score <= -2:
        state = "risk_on"
        tilt = 4 if net_score <= -6 else 2

    if previous is None:
        tilt = min(tilt, 2)
    else:
        prev_net_score = risk_off_score(previous) - risk_on_score(previous)
        if tilt > 0 and ((net_score > 0 and prev_net_score > 0) or (net_score < 0 and prev_net_score < 0)):
            tilt += 1

    item_count = int(current.get("item_count") or 0)
    if item_count > 0 and item_count < 8:
        tilt = max(0, tilt - 1)
    tilt = min(max_tilt, tilt)
    if tilt == 0:
        state = "neutral"

    reason = "Signal threshold not met; no allocation tilt applied."
    if state == "risk_off":
        reason = (
            f"Risk-off signal detected from weekly aggregates (risk_off_score={risk_off}, risk_on_score={risk_on}); "
            f"apply defensive tilt of {tilt}pp from global_equity to bonds_cashlike."
        )
    elif state == "risk_on":
        reason = (
            f"Risk-on signal detected from weekly aggregates (risk_off_score={risk_off}, risk_on_score={risk_on}); "
            f"apply pro-risk tilt of {tilt}pp from bonds_cashlike to global_equity."
        )

    return {
        "enabled": tilt > 0,
        "week": target_week,
        "previous_week": previous_week if previous is not None else None,
        "state": state,
        "tilt_pct": int(tilt),
        "max_tilt_pct": max_tilt,
        "risk_off_score": int(risk_off),
        "risk_on_score": int(risk_on),
        "net_score": int(net_score),
        "drivers": top_regime_drivers(current, limit=5),
        "calibration": base.get("calibration"),
        "reason": reason,
    }


def _calibrate_signal_tilt_cap(
    max_signal_tilt_pct: int,
    simulation_feedback_path: Optional[str],
) -> tuple[int, dict[str, Any]]:
    requested_cap = max(0, int(max_signal_tilt_pct))
    calibration = {
        "enabled": False,
        "source_path": simulation_feedback_path,
        "requested_max_tilt_pct": requested_cap,
        "effective_max_tilt_pct": requested_cap,
        "reason": "No simulation feedback calibration applied.",
        "stats": {},
    }
    if not simulation_feedback_path:
        return requested_cap, calibration

    path = Path(simulation_feedback_path)
    if not path.exists():
        calibration["reason"] = f"Simulation feedback file not found: {path}"
        return requested_cap, calibration

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        calibration["reason"] = f"Failed to parse simulation feedback JSON: {path}"
        return requested_cap, calibration

    if not isinstance(payload, dict):
        calibration["reason"] = f"Simulation feedback payload is not a JSON object: {path}"
        return requested_cap, calibration
    stats_raw = payload.get("stats")
    if not isinstance(stats_raw, dict):
        calibration["reason"] = f"Simulation feedback is missing stats object: {path}"
        return requested_cap, calibration

    cagr = _as_float_or_none(stats_raw.get("cagr"))
    annualized_vol = _as_float_or_none(stats_raw.get("annualized_volatility"))
    max_drawdown = _as_float_or_none(stats_raw.get("max_drawdown"))
    calibration["enabled"] = True
    calibration["stats"] = {
        "cagr": cagr,
        "annualized_volatility": annualized_vol,
        "max_drawdown": max_drawdown,
    }

    effective_cap = requested_cap
    reason = "Simulation feedback within guardrail range; keep requested signal tilt cap."
    if max_drawdown is not None and max_drawdown <= -0.25:
        effective_cap = min(effective_cap, 1)
        reason = (
            "Severe drawdown in backtest (max_drawdown <= -25%); cap signal tilt to 1pp "
            "to prioritize report/proposal stability."
        )
    elif (annualized_vol is not None and annualized_vol >= 0.25) or (
        max_drawdown is not None and max_drawdown <= -0.18
    ):
        effective_cap = min(effective_cap, 2)
        reason = (
            "High volatility/drawdown regime in backtest; cap signal tilt to 2pp to reduce regime overreaction."
        )
    elif (annualized_vol is not None and annualized_vol >= 0.18) or (
        max_drawdown is not None and max_drawdown <= -0.12
    ):
        effective_cap = min(effective_cap, 3)
        reason = "Moderate volatility/drawdown in backtest; cap signal tilt to 3pp."
    elif cagr is not None and cagr < 0:
        effective_cap = min(effective_cap, 2)
        reason = "Negative TWR CAGR in backtest; cap signal tilt to 2pp."

    calibration["effective_max_tilt_pct"] = int(effective_cap)
    calibration["reason"] = reason
    return int(effective_cap), calibration


def _as_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_week_or_raise(value: Optional[str]) -> str:
    text = str(value or "").strip()
    if not ISO_WEEK_PATTERN.match(text):
        raise ValueError("week must be in ISO format YYYY-Www (e.g., 2026-W06).")
    try:
        year_str, week_str = text.split("-W")
        year = int(year_str)
        iso_week = int(week_str)
        start = date.fromisocalendar(year, iso_week, 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("week must be a valid ISO week in format YYYY-Www.") from exc
    return f"{start.isocalendar().year}-W{start.isocalendar().week:02d}"


def _try_normalize_week(value: object) -> Optional[str]:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return _normalize_week_or_raise(text)
    except ValueError:
        return None


def _previous_week_key(week: str) -> str:
    normalized = _normalize_week_or_raise(week)
    year_str, week_str = normalized.split("-W")
    start = date.fromisocalendar(int(year_str), int(week_str), 1)
    previous = start.fromordinal(start.toordinal() - 7)
    iso = previous.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _apply_signal_overlay_to_weights(
    weights: dict[str, float],
    signal_overlay: Optional[dict[str, Any]],
) -> dict[str, float]:
    if not signal_overlay:
        return dict(weights)
    state = str(signal_overlay.get("state") or "neutral").strip().lower()
    tilt = int(signal_overlay.get("tilt_pct") or 0)
    if tilt <= 0 or state not in {"risk_off", "risk_on"}:
        return dict(weights)

    adjusted = dict(weights)
    equity = float(adjusted.get("global_equity", 0.0))
    bonds = float(adjusted.get("bonds_cashlike", 0.0))
    if state == "risk_off":
        move = min(float(tilt), max(0.0, equity))
        adjusted["global_equity"] = equity - move
        adjusted["bonds_cashlike"] = bonds + move
        return adjusted

    move = min(float(tilt), max(0.0, bonds))
    adjusted["global_equity"] = equity + move
    adjusted["bonds_cashlike"] = bonds - move
    return adjusted


def _build_candidates(
    inputs: dict[str, Any],
    signal_overlay: Optional[dict[str, Any]] = None,
) -> dict[str, dict[str, Any]]:
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
        base_weights = {
            "global_equity": profile["global_equity"],
            "bonds_cashlike": profile["bonds_cashlike"],
            "optional_gold": profile["optional_gold"],
        }
        signal_adjusted_weights = _apply_signal_overlay_to_weights(base_weights, signal_overlay)
        adjusted = _adjust_weights_for_allowed_assets(
            signal_adjusted_weights,
            allowed_assets,
        )
        allocation = [{"bucket": bucket, "pct": pct} for bucket, pct in adjusted.items() if pct > 0]
        validate_allocation_sum(allocation)
        notes = {
            "rationale": profile["pros"],
            "risks": profile["risks"],
            "pros": profile["pros"],
            "cons": profile["cons"],
        }
        if signal_overlay and int(signal_overlay.get("tilt_pct") or 0) > 0:
            notes["signal_overlay"] = str(signal_overlay.get("reason") or "")
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
            "notes": notes,
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


def _render_ips_report(
    inputs: dict[str, Any],
    candidates: dict[str, dict[str, Any]],
    signal_overlay: Optional[dict[str, Any]] = None,
) -> str:
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
    lines.append("## Signal Overlay")
    lines.append("")
    overlay = signal_overlay or {}
    lines.append(f"- state: {overlay.get('state', 'neutral')}")
    lines.append(f"- week: {overlay.get('week')}")
    lines.append(f"- previous_week: {overlay.get('previous_week')}")
    lines.append(f"- tilt_pct: {overlay.get('tilt_pct', 0)}")
    lines.append(f"- risk_off_score: {overlay.get('risk_off_score', 0)}")
    lines.append(f"- risk_on_score: {overlay.get('risk_on_score', 0)}")
    calibration = overlay.get("calibration")
    if isinstance(calibration, dict):
        lines.append(f"- requested_max_tilt_pct: {calibration.get('requested_max_tilt_pct')}")
        lines.append(f"- effective_max_tilt_pct: {calibration.get('effective_max_tilt_pct')}")
        lines.append(f"- calibration_reason: {calibration.get('reason')}")
    lines.append(f"- reason: {overlay.get('reason', 'No overlay signal.')}")
    drivers = list(overlay.get("drivers") or [])
    if drivers:
        lines.append("")
        lines.append("| direction | kind | term | count | contribution |")
        lines.append("| --- | --- | --- | ---: | ---: |")
        for row in drivers:
            lines.append(
                f"| {row.get('direction')} | {row.get('kind')} | {row.get('term')} | "
                f"{row.get('count')} | {row.get('contribution')} |"
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

def _fmt_pct(x: Any) -> str:
    try:
        return f"{float(x):.0f}%"
    except Exception:
        return str(x)

def _get_bucket_pct(target_allocation: Any, bucket: str) -> Any:
    if not isinstance(target_allocation, list):
        return None
    for row in target_allocation:
        if isinstance(row, dict) and row.get("bucket") == bucket:
            return row.get("pct")
    return None

def _render_ips_report_md(policy_doc: dict[str, Any], draft_doc: dict[str, Any] | None = None) -> str:
    """
    Deterministic human-readable IPS summary for review/archiving.
    """
    inputs = policy_doc.get("inputs_snapshot") or {}
    pol = policy_doc.get("policy") or {}
    selected_name = policy_doc.get("selected_candidate", "unknown")
    notes = (pol.get("notes") or {}) if isinstance(pol.get("notes"), dict) else {}
    signal_overlay = policy_doc.get("signal_overlay")
    if not isinstance(signal_overlay, dict):
        signal_overlay = (draft_doc or {}).get("signal_overlay")
    if not isinstance(signal_overlay, dict):
        signal_overlay = {}

    investable = inputs.get("investable_amount_eur")
    cash_buf = inputs.get("cash_buffer_eur")
    horizon = inputs.get("horizon_years")
    risk = inputs.get("risk_tolerance")
    reb = inputs.get("rebalance") or {}
    constraints = inputs.get("constraints") or {}

    ta = pol.get("target_allocation") or []
    eq = _get_bucket_pct(ta, "global_equity")
    bd = _get_bucket_pct(ta, "bonds_cashlike")
    au = _get_bucket_pct(ta, "optional_gold")

    # Candidate comparison table (if draft present)
    candidates = (draft_doc or {}).get("candidates") or {}
    rows = []
    if isinstance(candidates, dict) and candidates:
        for name, c in candidates.items():
            alloc = (c or {}).get("target_allocation") or []
            r_eq = _get_bucket_pct(alloc, "global_equity")
            r_bd = _get_bucket_pct(alloc, "bonds_cashlike")
            r_au = _get_bucket_pct(alloc, "optional_gold")
            g = (c or {}).get("guardrails") or {}
            max_single = g.get("max_single_asset_pct")
            rows.append((name, r_eq, r_bd, r_au, max_single))

    # Confirmation questions (deterministic)
    q = []
    # Scout mode question
    mode = str(inputs.get("mode", "")).strip().lower()
    if mode == "scout":
        m = inputs.get("review_after_months") or 6
        q.append(f"Scout mode: review after {m} months (performance/volatility). Proceed?")
    # Sell allowed question
    if constraints.get("sell_allowed") is True:
        q.append("Sell allowed: rebalancing may include sells. OK with taxes/fees impact?")
    else:
        q.append("Buy-only: rebalancing uses buys only. Keep this constraint?")
    # Gold optional
    if _get_bucket_pct(ta, "optional_gold") not in (None, 0, 0.0):
        q.append("Gold allocation: keep optional gold weight as-is? (Yes/No)")

    # Build markdown
    lines: list[str] = []
    lines.append(f"# IPS Report  {policy_doc.get('policy_version','')}")
    lines.append("")
    lines.append("## Policy Snapshot")
    lines.append(f"- Selected candidate: **{selected_name}**")
    lines.append(f"- Policy hash: `{policy_doc.get('policy_hash','')}`")
    lines.append(f"- Base currency: **{inputs.get('base_currency','')}**")
    lines.append(f"- Investable amount: **{investable}**")
    lines.append(f"- Cash buffer: **{cash_buf}**")
    lines.append(f"- Horizon: **{horizon} years**")
    lines.append(f"- Risk tolerance: **{risk}**")
    lines.append(f"- Rebalance: **{reb.get('frequency','')}**, band **{reb.get('band_pct','')}%**")
    lines.append(f"- Constraints: no_leverage={constraints.get('no_leverage')}, no_short={constraints.get('no_short')}, no_crypto={constraints.get('no_crypto')}, sell_allowed={constraints.get('sell_allowed')}")
    thesis = (pol.get("thesis_sleeve") or {}) if isinstance(pol, dict) else {}
    if thesis:
        lines.append(
            f"- Thesis sleeve: enabled={thesis.get('enabled')}, target_pct={thesis.get('target_pct')}%, "
            f"max_pct={thesis.get('max_pct')}%, review_after_months={thesis.get('review_after_months')}"
        )
    lines.append("")
    lines.append("## Target Allocation")
    lines.append(f"- Global equity: **{_fmt_pct(eq)}**")
    lines.append(f"- Bonds/Cash-like: **{_fmt_pct(bd)}**")
    lines.append(f"- Optional gold: **{_fmt_pct(au)}**")
    lines.append("")
    lines.append("## Rebalance & Contributions")
    rr = pol.get("rebalance_rules") or {}
    cs = pol.get("contribution_schedule") or {}
    lines.append(f"- Rebalance rule: frequency={rr.get('frequency')}, band_pct={rr.get('band_pct')}, buy_only={rr.get('buy_only')}")
    lines.append(f"- Contribution plan: type={cs.get('type')}, months={cs.get('months')}, planned_installment_eur={cs.get('planned_installment_eur')}")
    lines.append("")
    lines.append("## Rationale & Risks")
    if isinstance(notes, dict):
        for k in ("rationale", "pros", "cons", "risks", "scout_mode"):
            if k in notes and notes.get(k):
                lines.append(f"- **{k}**: {notes.get(k)}")
    lines.append("")
    lines.append("## Signal Context")
    lines.append(f"- state: {signal_overlay.get('state', 'neutral')}")
    lines.append(f"- week: {signal_overlay.get('week')}")
    lines.append(f"- previous_week: {signal_overlay.get('previous_week')}")
    lines.append(f"- tilt_pct: {signal_overlay.get('tilt_pct', 0)}")
    lines.append(f"- risk_off_score: {signal_overlay.get('risk_off_score', 0)}")
    lines.append(f"- risk_on_score: {signal_overlay.get('risk_on_score', 0)}")
    calibration = signal_overlay.get("calibration")
    if isinstance(calibration, dict):
        lines.append(f"- requested_max_tilt_pct: {calibration.get('requested_max_tilt_pct')}")
        lines.append(f"- effective_max_tilt_pct: {calibration.get('effective_max_tilt_pct')}")
        lines.append(f"- calibration_reason: {calibration.get('reason')}")
    lines.append(f"- reason: {signal_overlay.get('reason', 'No overlay signal.')}")
    drivers = signal_overlay.get("drivers")
    if isinstance(drivers, list) and drivers:
        lines.append("")
        lines.append("| direction | kind | term | count | contribution |")
        lines.append("|---|---|---|---:|---:|")
        for row in drivers:
            if not isinstance(row, dict):
                continue
            lines.append(
                f"| {row.get('direction')} | {row.get('kind')} | {row.get('term')} | "
                f"{row.get('count')} | {row.get('contribution')} |"
            )
    lines.append("")
    if rows:
        lines.append("## Candidate Comparison")
        lines.append("| candidate | equity | bonds/cashlike | gold | max_single_asset_pct |")
        lines.append("|---|---:|---:|---:|---:|")
        for name, r_eq, r_bd, r_au, max_single in rows:
            lines.append(f"| {name} | {_fmt_pct(r_eq)} | {_fmt_pct(r_bd)} | {_fmt_pct(r_au)} | {max_single} |")
        lines.append("")
    lines.append("## Confirmation Questions")
    for i, item in enumerate(q, 1):
        lines.append(f"{i}. {item}")
    lines.append("")
    return "\n".join(lines)
