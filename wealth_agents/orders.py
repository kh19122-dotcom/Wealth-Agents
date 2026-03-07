from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import re
from typing import Any

from .policy import read_yaml, validate_allocation_sum


DEFAULT_POLICY_PATH = "data/policy/policy.yml"
DEFAULT_ORDERS_DIR = "orders"
DEFAULT_REPORTS_DIR = "reports"

MONTH_PATTERN = re.compile(r"^\d{4}-\d{2}$")


@dataclass
class InstrumentAllocation:
    bucket: str
    instrument_id: str
    isin: str
    name: str
    ticker: str | None
    effective_weight: float
    amount_raw: float
    amount_eur: int


@dataclass
class OrderComputationResult:
    payload: dict[str, Any]
    min_trade_rollups: list[dict[str, Any]]


def propose_monthly_orders(
    month: str,
    amount_eur: float | None = None,
    policy_path: str = DEFAULT_POLICY_PATH,
    orders_dir: str = DEFAULT_ORDERS_DIR,
    reports_dir: str = DEFAULT_REPORTS_DIR,
) -> tuple[Path, Path, dict[str, Any]]:
    policy_doc = read_yaml(policy_path)
    computed = compute_monthly_order_payload(
        month=month,
        policy_doc=policy_doc,
        amount_eur=amount_eur,
        policy_path_for_errors=policy_path,
    )
    payload = computed.payload
    _validate_order_payload(payload)

    normalized_month = payload["month"]
    budget_eur = payload["budget_eur"]
    policy_hash = payload["policy_hash"]
    orders_path = Path(orders_dir) / f"proposed_{normalized_month}.json"
    reports_path = Path(reports_dir) / f"orders_{normalized_month}.md"
    _write_json(orders_path, payload)
    _write_report(
        path=reports_path,
        month=normalized_month,
        budget_eur=budget_eur,
        policy_hash=policy_hash,
        payload=payload,
        min_trade_rollups=computed.min_trade_rollups,
    )
    return orders_path, reports_path, payload


def compute_monthly_order_payload(
    month: str,
    policy_doc: dict[str, Any],
    amount_eur: float | None = None,
    policy_path_for_errors: str = DEFAULT_POLICY_PATH,
) -> OrderComputationResult:
    normalized_month = _validate_month(month)
    policy = _read_policy_section(policy_doc, policy_path_for_errors)
    policy_hash = _read_policy_hash(policy_doc, policy_path_for_errors)
    currency = _read_currency(policy_doc)
    policy_context = _extract_policy_context(policy_doc, policy)
    if currency != "EUR":
        raise ValueError("Phase 3 MVP currently supports EUR-only policies.")

    budget_eur = _resolve_budget_eur(policy, amount_eur)
    target_allocation = _read_target_allocation(policy)
    instruments = _read_instruments(policy, target_allocation)
    min_trade_eur = _read_min_trade_eur(policy)

    allocations = _build_allocations(
        budget_eur=budget_eur,
        target_allocation=target_allocation,
        instruments_by_bucket=instruments,
    )
    _apply_budget_remainder(allocations, budget_eur)
    min_trade_rollups = _enforce_min_trade(allocations, min_trade_eur)

    final_orders = _build_orders(allocations)
    if not final_orders:
        raise ValueError("No BUY orders were generated from the current policy and budget.")

    total_order_amount = sum(order["amount_eur"] for order in final_orders)
    if total_order_amount != budget_eur:
        raise RuntimeError(
            f"Internal allocation mismatch: budget={budget_eur}, order_total={total_order_amount}"
        )

    payload: dict[str, Any] = {
        "month": normalized_month,
        "currency": "EUR",
        "budget_eur": budget_eur,
        "policy_hash": policy_hash,
        "assumptions": {
            "rounding_method": "round_half_up_to_nearest_eur",
            "remainder_distribution": "assign_remaining_eur_to_highest_effective_weight_then_(bucket,instrument_id)",
            "buy_only": True,
            "allow_sells": False,
            "min_trade_eur": min_trade_eur,
        },
        "orders": final_orders,
    }
    if policy_context:
        payload["policy_context"] = policy_context
    return OrderComputationResult(payload=payload, min_trade_rollups=min_trade_rollups)


def _validate_month(month: str) -> str:
    text = str(month).strip()
    if not MONTH_PATTERN.match(text):
        raise ValueError("month must be in YYYY-MM format.")
    try:
        datetime.strptime(text, "%Y-%m")
    except ValueError as exc:
        raise ValueError("month must be a valid calendar month in YYYY-MM format.") from exc
    return text


def _read_policy_section(policy_doc: dict[str, Any], policy_path: str) -> dict[str, Any]:
    policy = policy_doc.get("policy")
    if not isinstance(policy, dict):
        raise ValueError(f"Missing root 'policy' object in {policy_path}.")
    return policy


def _read_policy_hash(policy_doc: dict[str, Any], policy_path: str) -> str:
    policy_hash = str(policy_doc.get("policy_hash") or "").strip()
    if not policy_hash:
        raise ValueError(f"Missing top-level policy_hash in {policy_path}.")
    return policy_hash


def _read_currency(policy_doc: dict[str, Any]) -> str:
    inputs_snapshot = policy_doc.get("inputs_snapshot") or {}
    if isinstance(inputs_snapshot, dict):
        currency = str(inputs_snapshot.get("base_currency") or "EUR").strip().upper()
    else:
        currency = "EUR"
    return currency or "EUR"


def _extract_policy_context(policy_doc: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    selected_candidate_raw = policy_doc.get("selected_candidate")
    selected_candidate = str(selected_candidate_raw).strip() if selected_candidate_raw is not None else ""

    signal_overlay = _sanitize_signal_overlay(policy_doc.get("signal_overlay"))
    if signal_overlay is None:
        notes = policy.get("notes")
        if isinstance(notes, dict):
            reason = str(notes.get("signal_overlay") or "").strip()
            if reason:
                signal_overlay = {
                    "enabled": False,
                    "state": "unknown",
                    "week": None,
                    "previous_week": None,
                    "tilt_pct": 0,
                    "risk_off_score": None,
                    "risk_on_score": None,
                    "net_score": None,
                    "reason": reason,
                    "drivers": [],
                }

    context: dict[str, Any] = {}
    if selected_candidate:
        context["selected_candidate"] = selected_candidate
    if signal_overlay is not None:
        context["signal_overlay"] = signal_overlay
    return context


def _sanitize_signal_overlay(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None

    drivers_raw = raw.get("drivers")
    drivers: list[dict[str, Any]] = []
    if isinstance(drivers_raw, list):
        for row in drivers_raw:
            if not isinstance(row, dict):
                continue
            try:
                count = int(row.get("count"))
            except (TypeError, ValueError):
                count = 0
            try:
                contribution = int(row.get("contribution"))
            except (TypeError, ValueError):
                contribution = 0
            drivers.append(
                {
                    "direction": str(row.get("direction") or ""),
                    "kind": str(row.get("kind") or ""),
                    "term": str(row.get("term") or ""),
                    "count": count,
                    "contribution": contribution,
                }
            )

    def _as_int_or_none(value: Any) -> int | None:
        if value is None:
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None

    calibration_raw = raw.get("calibration")
    calibration: dict[str, Any] | None = None
    if isinstance(calibration_raw, dict):
        calibration = {
            "enabled": bool(calibration_raw.get("enabled")),
            "requested_max_tilt_pct": _as_int_or_none(calibration_raw.get("requested_max_tilt_pct")),
            "effective_max_tilt_pct": _as_int_or_none(calibration_raw.get("effective_max_tilt_pct")),
            "reason": str(calibration_raw.get("reason") or ""),
        }
        stats_raw = calibration_raw.get("stats")
        if isinstance(stats_raw, dict):
            stats: dict[str, float | None] = {}
            for key in ("cagr", "annualized_volatility", "max_drawdown"):
                value = stats_raw.get(key)
                if value is None:
                    stats[key] = None
                    continue
                try:
                    stats[key] = float(value)
                except (TypeError, ValueError):
                    stats[key] = None
            calibration["stats"] = stats

    sanitized = {
        "enabled": bool(raw.get("enabled")),
        "state": str(raw.get("state") or "neutral"),
        "week": raw.get("week"),
        "previous_week": raw.get("previous_week"),
        "tilt_pct": _as_int_or_none(raw.get("tilt_pct")) or 0,
        "risk_off_score": _as_int_or_none(raw.get("risk_off_score")),
        "risk_on_score": _as_int_or_none(raw.get("risk_on_score")),
        "net_score": _as_int_or_none(raw.get("net_score")),
        "reason": str(raw.get("reason") or ""),
        "drivers": drivers,
    }
    if calibration is not None:
        sanitized["calibration"] = calibration
    return sanitized


def _resolve_budget_eur(policy: dict[str, Any], amount_eur: float | None) -> int:
    if amount_eur is None:
        contribution = policy.get("contribution_schedule") or {}
        if not isinstance(contribution, dict):
            raise ValueError("policy.contribution_schedule must be a mapping.")
        raw_budget = contribution.get("planned_installment_eur")
    else:
        raw_budget = amount_eur

    if raw_budget is None:
        raise ValueError("Budget is required. Set --amount or policy.contribution_schedule.planned_installment_eur.")

    try:
        budget_float = float(raw_budget)
    except (TypeError, ValueError) as exc:
        raise ValueError("Budget must be numeric.") from exc

    if budget_float <= 0:
        raise ValueError("Budget must be positive.")

    budget_eur = _round_half_up(budget_float)
    if abs(budget_float - float(budget_eur)) > 1e-9:
        raise ValueError("Phase 3 MVP requires a whole-EUR budget.")
    return budget_eur


def _read_target_allocation(policy: dict[str, Any]) -> list[dict[str, Any]]:
    allocation = policy.get("target_allocation")
    if not isinstance(allocation, list) or not allocation:
        raise ValueError("policy.target_allocation must be a non-empty list.")
    validate_allocation_sum(allocation)
    return allocation


def _read_instruments(
    policy: dict[str, Any],
    target_allocation: list[dict[str, Any]],
) -> dict[str, list[dict[str, Any]]]:
    instruments = policy.get("instruments")
    if not isinstance(instruments, dict) or not instruments:
        raise ValueError("policy.instruments must be a non-empty mapping.")

    normalized: dict[str, list[dict[str, Any]]] = {}
    for row in target_allocation:
        bucket = str(row.get("bucket") or "").strip()
        if not bucket:
            raise ValueError("Each target_allocation row must include bucket.")

        try:
            pct = float(row.get("pct"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid pct for bucket '{bucket}'.") from exc
        if pct <= 0:
            continue

        raw_bucket_instruments = instruments.get(bucket)
        if not isinstance(raw_bucket_instruments, list) or not raw_bucket_instruments:
            raise ValueError(f"Missing instruments mapping for bucket '{bucket}'.")

        bucket_entries: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        total_weight = 0.0
        for item in raw_bucket_instruments:
            if not isinstance(item, dict):
                raise ValueError(f"Instrument entry in bucket '{bucket}' must be a mapping.")

            instrument_id = str(item.get("id") or "").strip()
            isin = str(item.get("isin") or "").strip()
            name = str(item.get("name") or "").strip()
            if not instrument_id:
                raise ValueError(f"Instrument in bucket '{bucket}' is missing id.")
            if instrument_id in seen_ids:
                raise ValueError(f"Duplicate instrument id '{instrument_id}' in bucket '{bucket}'.")
            if not isin:
                raise ValueError(f"Instrument '{instrument_id}' in bucket '{bucket}' is missing isin.")
            if not name:
                raise ValueError(f"Instrument '{instrument_id}' in bucket '{bucket}' is missing name.")

            try:
                raw_weight = float(item.get("weight_within_bucket"))
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    f"Instrument '{instrument_id}' in bucket '{bucket}' has invalid weight_within_bucket."
                ) from exc
            if raw_weight <= 0:
                raise ValueError(
                    f"Instrument '{instrument_id}' in bucket '{bucket}' must have positive weight_within_bucket."
                )

            seen_ids.add(instrument_id)
            total_weight += raw_weight
            bucket_entries.append(
                {
                    "id": instrument_id,
                    "isin": isin,
                    "name": name,
                    "raw_weight": raw_weight,
                    "ticker": _read_ticker_from_instrument(item),
                }
            )

        if total_weight <= 0:
            raise ValueError(f"Instrument weights in bucket '{bucket}' must sum to a positive value.")

        normalized[bucket] = [
            {
                "id": row["id"],
                "isin": row["isin"],
                "name": row["name"],
                "weight": row["raw_weight"] / total_weight,
                "ticker": row.get("ticker"),
            }
            for row in bucket_entries
        ]
    return normalized


def _read_min_trade_eur(policy: dict[str, Any]) -> int:
    guardrails = policy.get("guardrails") or {}
    if not isinstance(guardrails, dict):
        raise ValueError("policy.guardrails must be a mapping.")

    raw_min_trade = guardrails.get("min_trade_eur", 0)
    try:
        min_trade = float(raw_min_trade)
    except (TypeError, ValueError) as exc:
        raise ValueError("policy.guardrails.min_trade_eur must be numeric.") from exc

    if min_trade < 0:
        raise ValueError("policy.guardrails.min_trade_eur must be non-negative.")
    return _round_half_up(min_trade)


def _build_allocations(
    budget_eur: int,
    target_allocation: list[dict[str, Any]],
    instruments_by_bucket: dict[str, list[dict[str, Any]]],
) -> list[InstrumentAllocation]:
    allocations: list[InstrumentAllocation] = []
    seen_buckets: set[str] = set()

    for row in target_allocation:
        bucket = str(row.get("bucket") or "").strip()
        if not bucket:
            raise ValueError("Each target_allocation row must include bucket.")
        if bucket in seen_buckets:
            raise ValueError(f"Duplicate target allocation bucket '{bucket}'.")
        seen_buckets.add(bucket)

        pct = float(row["pct"])
        if pct <= 0:
            continue
        bucket_fraction = pct / 100.0
        bucket_amount = budget_eur * bucket_fraction

        instruments = instruments_by_bucket.get(bucket)
        if not instruments:
            raise ValueError(f"Missing instruments for bucket '{bucket}'.")

        for item in instruments:
            effective_weight = bucket_fraction * float(item["weight"])
            raw_amount = bucket_amount * float(item["weight"])
            allocations.append(
                InstrumentAllocation(
                    bucket=bucket,
                    instrument_id=str(item["id"]),
                    isin=str(item["isin"]),
                    name=str(item["name"]),
                    ticker=_as_optional_string(item.get("ticker")),
                    effective_weight=effective_weight,
                    amount_raw=raw_amount,
                    amount_eur=_round_half_up(raw_amount),
                )
            )

    if not allocations:
        raise ValueError("No allocations generated from policy.target_allocation.")
    return allocations


def _apply_budget_remainder(allocations: list[InstrumentAllocation], budget_eur: int) -> None:
    current_total = sum(item.amount_eur for item in allocations)
    delta = budget_eur - current_total
    if delta == 0:
        return

    if delta > 0:
        ranked = sorted(
            allocations,
            key=lambda item: (-item.effective_weight, item.bucket, item.instrument_id),
        )
        for idx in range(delta):
            ranked[idx % len(ranked)].amount_eur += 1
        return

    ranked = sorted(
        allocations,
        key=lambda item: (item.effective_weight, item.bucket, item.instrument_id),
    )
    remainder = -delta
    idx = 0
    max_iterations = len(ranked) * (budget_eur + len(ranked) + 1)
    while remainder > 0 and idx < max_iterations:
        candidate = ranked[idx % len(ranked)]
        if candidate.amount_eur > 0:
            candidate.amount_eur -= 1
            remainder -= 1
        idx += 1
    if remainder != 0:
        raise RuntimeError("Failed to reconcile rounded allocations to budget.")


def _enforce_min_trade(
    allocations: list[InstrumentAllocation],
    min_trade_eur: int,
) -> list[dict[str, Any]]:
    if min_trade_eur <= 0:
        return []

    rollups: list[dict[str, Any]] = []
    while True:
        positive = [item for item in allocations if item.amount_eur > 0]
        small = [item for item in positive if item.amount_eur < min_trade_eur]
        if not small:
            return rollups

        if len(positive) == 1:
            raise ValueError(
                f"Budget is too small to satisfy min_trade_eur={min_trade_eur}; only one order would remain."
            )

        anchor = sorted(
            positive,
            key=lambda item: (-item.amount_eur, item.bucket, item.instrument_id),
        )[0]
        moved = False
        for item in sorted(small, key=lambda row: (row.bucket, row.instrument_id)):
            if item is anchor:
                continue
            transfer_amount = item.amount_eur
            if transfer_amount <= 0:
                continue
            item.amount_eur = 0
            anchor.amount_eur += transfer_amount
            moved = True
            rollups.append(
                {
                    "from_bucket": item.bucket,
                    "from_instrument_id": item.instrument_id,
                    "to_bucket": anchor.bucket,
                    "to_instrument_id": anchor.instrument_id,
                    "amount_eur": transfer_amount,
                }
            )

        if not moved:
            raise ValueError(
                f"Budget is too small to satisfy min_trade_eur={min_trade_eur} after deterministic rollup."
            )


def _build_orders(allocations: list[InstrumentAllocation]) -> list[dict[str, Any]]:
    orders: list[dict[str, Any]] = []
    for item in sorted(allocations, key=lambda row: (row.bucket, row.instrument_id)):
        if item.amount_eur <= 0:
            continue
        orders.append(
            {
                "side": "BUY",
                "instrument_id": item.instrument_id,
                "isin": item.isin,
                "name": item.name,
                "bucket": item.bucket,
                "amount_eur": item.amount_eur,
            }
        )
        if item.ticker:
            orders[-1]["ticker"] = item.ticker
    return orders


def _validate_order_payload(payload: dict[str, Any]) -> None:
    raw_orders = payload.get("orders")
    if not isinstance(raw_orders, list) or not raw_orders:
        raise RuntimeError("Internal order payload error: orders must be a non-empty list.")

    seen_instrument_ids: set[str] = set()
    total_amount = 0
    for idx, order in enumerate(raw_orders, start=1):
        if not isinstance(order, dict):
            raise RuntimeError(f"Internal order payload error: order #{idx} must be a mapping.")
        if str(order.get("side") or "").upper() != "BUY":
            raise RuntimeError(f"Internal order payload error: order #{idx} has non-BUY side.")

        instrument_id = str(order.get("instrument_id") or "").strip()
        if not instrument_id:
            raise RuntimeError(f"Internal order payload error: order #{idx} missing instrument_id.")
        if instrument_id in seen_instrument_ids:
            raise RuntimeError(
                f"Internal order payload error: duplicate instrument_id '{instrument_id}' in final orders."
            )
        seen_instrument_ids.add(instrument_id)

        try:
            amount_eur = int(order.get("amount_eur"))
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"Internal order payload error: order #{idx} has invalid amount_eur."
            ) from exc
        if amount_eur <= 0:
            raise RuntimeError(
                f"Internal order payload error: order #{idx} must have positive amount_eur."
            )
        total_amount += amount_eur

    try:
        budget_eur = int(payload.get("budget_eur"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("Internal order payload error: budget_eur must be an integer.") from exc
    if total_amount != budget_eur:
        raise RuntimeError(
            f"Internal order payload error: sum(orders.amount_eur)={total_amount}, budget_eur={budget_eur}."
        )


def _sorted_orders_for_render(orders: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        orders,
        key=lambda order: (
            str(order.get("bucket") or ""),
            str(order.get("instrument_id") or ""),
            str(order.get("isin") or ""),
        ),
    )


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    path.write_text(rendered, encoding="utf-8")


def _read_ticker_from_instrument(item: dict[str, Any]) -> str | None:
    data = item.get("data")
    if not isinstance(data, dict):
        return None
    ticker = str(data.get("ticker") or "").strip()
    return ticker or None


def _as_optional_string(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _write_report(
    path: Path,
    month: str,
    budget_eur: int,
    policy_hash: str,
    payload: dict[str, Any],
    min_trade_rollups: list[dict[str, Any]],
) -> None:
    orders = _sorted_orders_for_render(payload["orders"])
    lines: list[str] = []
    lines.append(f"# Order Proposal {month}")
    lines.append("")
    lines.append(f"- month: {month}")
    lines.append(f"- budget_eur: {budget_eur}")
    lines.append(f"- policy_hash: `{policy_hash}`")
    lines.append(f"- currency: {payload.get('currency')}")
    lines.append("")
    policy_context = payload.get("policy_context")
    if isinstance(policy_context, dict) and policy_context:
        lines.append("## Policy Context")
        lines.append("")
        selected_candidate = policy_context.get("selected_candidate")
        if selected_candidate:
            lines.append(f"- selected_candidate: {selected_candidate}")
        signal_overlay = policy_context.get("signal_overlay")
        if isinstance(signal_overlay, dict):
            lines.append(f"- signal_state: {signal_overlay.get('state', 'neutral')}")
            lines.append(f"- signal_week: {signal_overlay.get('week')}")
            lines.append(f"- signal_tilt_pct: {signal_overlay.get('tilt_pct', 0)}")
            lines.append(f"- signal_reason: {signal_overlay.get('reason', '')}")
            calibration = signal_overlay.get("calibration")
            if isinstance(calibration, dict):
                lines.append(
                    f"- signal_tilt_cap: requested={calibration.get('requested_max_tilt_pct')} "
                    f"effective={calibration.get('effective_max_tilt_pct')}"
                )
                lines.append(f"- signal_calibration_reason: {calibration.get('reason', '')}")
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
    lines.append("## Allocation")
    lines.append("")
    lines.append("| bucket | instrument_id | isin | name | amount_eur |")
    lines.append("|---|---|---|---|---:|")
    for order in orders:
        lines.append(
            f"| {order['bucket']} | {order['instrument_id']} | {order['isin']} | {order['name']} | {order['amount_eur']} |"
        )
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append("- Manual execution gate: proposals are advisory only and require manual broker entry.")
    lines.append("- BUY-only MVP: the proposal never emits SELL orders.")
    lines.append(
        "- Rounding: per-instrument amounts are rounded to nearest EUR (half-up), then remainder is distributed to highest effective weight first with tie-break `(bucket, instrument_id)`."
    )
    lines.append(
        f"- Min trade rule: orders below `{payload['assumptions']['min_trade_eur']}` EUR are rolled into the largest order deterministically."
    )
    if min_trade_rollups:
        lines.append("")
        lines.append("## Min Trade Rollups")
        lines.append("")
        lines.append("| from_bucket | from_instrument_id | to_bucket | to_instrument_id | amount_eur |")
        lines.append("|---|---|---|---|---:|")
        for row in min_trade_rollups:
            lines.append(
                f"| {row['from_bucket']} | {row['from_instrument_id']} | {row['to_bucket']} | {row['to_instrument_id']} | {row['amount_eur']} |"
            )

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _round_half_up(value: float) -> int:
    return int(Decimal(str(value)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
