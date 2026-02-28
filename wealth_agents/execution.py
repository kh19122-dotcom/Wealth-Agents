from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .broker import BrokerOrderRequest, MockBrokerClient
from .policy import read_yaml


DEFAULT_GUARDRAILS_PATH = "config/execution_guardrails.yml"


def execute_order_proposal(
    proposal_path: str,
    broker: str = "mock",
    mock_state_path: str = "data/broker/mock_state.json",
    dry_run: bool = False,
    output_path: str | None = None,
    guardrails_path: str | None = DEFAULT_GUARDRAILS_PATH,
    enable_guardrails: bool = True,
) -> dict[str, Any]:
    proposal = _read_proposal(proposal_path)
    month = str(proposal.get("month") or "")
    raw_orders = proposal.get("orders") or []
    if not isinstance(raw_orders, list):
        raise ValueError("orders proposal must contain an orders list.")

    broker_name = str(broker or "").strip().lower()
    if broker_name != "mock":
        raise ValueError("execute-order currently supports only broker='mock'.")

    client = None if dry_run else MockBrokerClient(state_path=mock_state_path)
    submitted: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    prepared_orders: list[dict[str, Any]] = []

    for idx, row in enumerate(raw_orders, start=1):
        if not isinstance(row, dict):
            skipped.append({"index": idx, "reason": "order row is not an object"})
            continue
        side = str(row.get("side") or "").strip().upper()
        if side != "BUY":
            skipped.append({"index": idx, "reason": f"unsupported side={side}"})
            continue
        try:
            amount_eur = float(row.get("amount_eur"))
        except (TypeError, ValueError):
            skipped.append({"index": idx, "reason": "invalid amount_eur"})
            continue
        if amount_eur <= 0:
            skipped.append({"index": idx, "reason": "amount_eur must be positive"})
            continue

        instrument_id = str(row.get("instrument_id") or "").strip()
        if not instrument_id:
            skipped.append({"index": idx, "reason": "missing instrument_id"})
            continue

        isin = str(row.get("isin") or "").strip().upper()
        bucket = str(row.get("bucket") or "").strip()
        client_order_id = f"{month}:{instrument_id}:{idx}"
        request = BrokerOrderRequest(
            symbol=instrument_id,
            side=side,
            order_type="cash_amount",
            cash_amount_eur=amount_eur,
            currency="EUR",
            client_order_id=client_order_id,
            metadata={
                "month": month,
                "isin": isin,
                "name": str(row.get("name") or ""),
                "bucket": bucket,
            },
        )
        prepared_orders.append(
            {
                "index": idx,
                "instrument_id": instrument_id,
                "isin": isin,
                "bucket": bucket,
                "amount_eur": amount_eur,
                "request": request,
            }
        )

    guardrails = load_execution_guardrails(
        path=guardrails_path,
        enabled=enable_guardrails,
    )
    guardrail_result = evaluate_execution_guardrails(
        guardrails=guardrails,
        proposal=proposal,
        orders=prepared_orders,
    )
    if bool(guardrail_result.get("enabled")) and not bool(guardrail_result.get("passed")):
        if bool(guardrail_result.get("fail_on_violation")):
            raise ValueError(_format_guardrail_violation_message(guardrail_result))
        prepared_orders, dropped = apply_guardrail_advisory_filter(prepared_orders, guardrail_result)
        skipped.extend(dropped)

    for order in prepared_orders:
        idx = int(order["index"])
        request = order["request"]
        assert isinstance(request, BrokerOrderRequest)
        if dry_run:
            submitted.append(
                {
                    "index": idx,
                    "client_order_id": request.client_order_id,
                    "symbol": request.symbol,
                    "side": request.side,
                    "order_type": request.order_type,
                    "cash_amount_eur": request.cash_amount_eur,
                    "status": "dry_run",
                }
            )
            continue

        assert client is not None
        status = client.submit_order(request)
        submitted.append(
            {
                "index": idx,
                "order_id": status.order_id,
                "client_order_id": status.client_order_id,
                "symbol": status.symbol,
                "side": status.side,
                "order_type": status.order_type,
                "cash_amount_eur": status.cash_amount_eur,
                "status": status.status,
                "submitted_at": status.submitted_at,
            }
        )

    payload = {
        "proposal_path": str(Path(proposal_path)),
        "broker": broker_name,
        "dry_run": bool(dry_run),
        "month": month,
        "orders_in_proposal": len(raw_orders),
        "orders_after_validation": len(prepared_orders),
        "submitted_count": len(submitted),
        "skipped_count": len(skipped),
        "guardrails": guardrail_result,
        "submitted": submitted,
        "skipped": skipped,
        "executed_at": _now_iso8601(),
    }
    if output_path:
        out = Path(output_path)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return payload


def _read_proposal(path: str) -> dict[str, Any]:
    proposal_path = Path(path)
    if not proposal_path.exists():
        raise ValueError(f"orders proposal file not found: {proposal_path}")
    try:
        payload = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid orders proposal JSON: {proposal_path}") from exc
    if not isinstance(payload, dict):
        raise ValueError("orders proposal payload must be a JSON object.")
    return payload


def load_execution_guardrails(path: str | None, enabled: bool = True) -> dict[str, Any]:
    if not enabled or not path:
        return {
            "enabled": False,
            "path": path,
            "fail_on_violation": False,
            "max_order_amount_eur": None,
            "max_total_amount_eur": None,
            "max_orders_count": None,
            "allowed_instrument_ids": [],
            "allowed_isins": [],
            "allowed_buckets": [],
            "reason": "disabled",
        }

    config_path = Path(path)
    if not config_path.exists():
        if str(path) == DEFAULT_GUARDRAILS_PATH:
            return {
                "enabled": False,
                "path": str(config_path),
                "fail_on_violation": False,
                "max_order_amount_eur": None,
                "max_total_amount_eur": None,
                "max_orders_count": None,
                "allowed_instrument_ids": [],
                "allowed_isins": [],
                "allowed_buckets": [],
                "reason": "default_guardrails_file_not_found",
            }
        raise ValueError(f"execution guardrails file not found: {config_path}")

    payload = read_yaml(str(config_path))
    guardrails = payload.get("guardrails")
    if isinstance(guardrails, dict):
        source = guardrails
    else:
        source = payload
    if not isinstance(source, dict):
        raise ValueError(f"Invalid execution guardrails config: {config_path}")

    enabled_value = bool(source.get("enabled", True))
    return {
        "enabled": enabled_value,
        "path": str(config_path),
        "fail_on_violation": bool(source.get("fail_on_violation", True)),
        "max_order_amount_eur": _positive_float_or_none(source.get("max_order_amount_eur")),
        "max_total_amount_eur": _positive_float_or_none(source.get("max_total_amount_eur")),
        "max_orders_count": _positive_int_or_none(source.get("max_orders_count")),
        "allowed_instrument_ids": _normalize_string_list(source.get("allowed_instrument_ids")),
        "allowed_isins": _normalize_string_list(source.get("allowed_isins"), upper=True),
        "allowed_buckets": _normalize_string_list(source.get("allowed_buckets")),
        "reason": "loaded",
    }


def evaluate_execution_guardrails(
    *,
    guardrails: dict[str, Any],
    proposal: dict[str, Any],
    orders: list[dict[str, Any]],
) -> dict[str, Any]:
    enabled = bool(guardrails.get("enabled"))
    result: dict[str, Any] = {
        "enabled": enabled,
        "path": str(guardrails.get("path") or ""),
        "fail_on_violation": bool(guardrails.get("fail_on_violation")),
        "passed": True,
        "violations": [],
        "metrics": {
            "orders_count": len(orders),
            "total_amount_eur": round(sum(float(row.get("amount_eur") or 0.0) for row in orders), 6),
            "proposal_budget_eur": _as_float_or_none(proposal.get("budget_eur")),
        },
    }
    if not enabled:
        return result

    violations: list[dict[str, Any]] = []
    max_orders_count = guardrails.get("max_orders_count")
    if isinstance(max_orders_count, int) and len(orders) > max_orders_count:
        violations.append(
            {
                "scope": "global",
                "type": "max_orders_count",
                "limit": int(max_orders_count),
                "actual": len(orders),
                "message": f"orders_count={len(orders)} exceeds max_orders_count={max_orders_count}",
            }
        )

    total_amount = float(result["metrics"]["total_amount_eur"])
    max_total = guardrails.get("max_total_amount_eur")
    if isinstance(max_total, float) and total_amount > max_total:
        violations.append(
            {
                "scope": "global",
                "type": "max_total_amount_eur",
                "limit": float(max_total),
                "actual": total_amount,
                "message": f"total_amount_eur={total_amount:.2f} exceeds max_total_amount_eur={max_total:.2f}",
            }
        )

    allowed_ids = set(guardrails.get("allowed_instrument_ids") or [])
    allowed_isins = set(guardrails.get("allowed_isins") or [])
    allowed_buckets = set(guardrails.get("allowed_buckets") or [])
    max_order = guardrails.get("max_order_amount_eur")

    for row in orders:
        idx = int(row.get("index") or 0)
        instrument_id = str(row.get("instrument_id") or "")
        isin = str(row.get("isin") or "").upper()
        bucket = str(row.get("bucket") or "")
        amount = float(row.get("amount_eur") or 0.0)

        if isinstance(max_order, float) and amount > max_order:
            violations.append(
                {
                    "scope": "order",
                    "index": idx,
                    "type": "max_order_amount_eur",
                    "limit": float(max_order),
                    "actual": amount,
                    "message": f"order index={idx} amount_eur={amount:.2f} exceeds max_order_amount_eur={max_order:.2f}",
                }
            )
        if allowed_ids and instrument_id not in allowed_ids:
            violations.append(
                {
                    "scope": "order",
                    "index": idx,
                    "type": "allowed_instrument_ids",
                    "actual": instrument_id,
                    "message": f"order index={idx} instrument_id='{instrument_id}' is not in allowed_instrument_ids",
                }
            )
        if allowed_isins and isin not in allowed_isins:
            violations.append(
                {
                    "scope": "order",
                    "index": idx,
                    "type": "allowed_isins",
                    "actual": isin,
                    "message": f"order index={idx} isin='{isin}' is not in allowed_isins",
                }
            )
        if allowed_buckets and bucket not in allowed_buckets:
            violations.append(
                {
                    "scope": "order",
                    "index": idx,
                    "type": "allowed_buckets",
                    "actual": bucket,
                    "message": f"order index={idx} bucket='{bucket}' is not in allowed_buckets",
                }
            )

    result["violations"] = violations
    result["passed"] = len(violations) == 0
    return result


def apply_guardrail_advisory_filter(
    orders: list[dict[str, Any]],
    guardrail_result: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    violations = list(guardrail_result.get("violations") or [])
    if not violations:
        return list(orders), []

    blocked_by_index: dict[int, list[str]] = {}
    global_reasons: list[str] = []
    for row in violations:
        scope = str(row.get("scope") or "")
        message = str(row.get("message") or "guardrail violation")
        if scope == "order":
            idx = _as_int_or_none(row.get("index"))
            if idx is None:
                global_reasons.append(message)
                continue
            blocked_by_index.setdefault(idx, []).append(message)
        else:
            global_reasons.append(message)

    if global_reasons:
        dropped = [
            {"index": int(order.get("index") or 0), "reason": f"guardrail(advisory): {'; '.join(global_reasons)}"}
            for order in orders
        ]
        return [], dropped

    kept: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    for order in orders:
        idx = int(order.get("index") or 0)
        reasons = blocked_by_index.get(idx)
        if reasons:
            dropped.append({"index": idx, "reason": f"guardrail(advisory): {'; '.join(reasons)}"})
            continue
        kept.append(order)
    return kept, dropped


def _format_guardrail_violation_message(result: dict[str, Any]) -> str:
    violations = list(result.get("violations") or [])
    if not violations:
        return "Execution guardrail violation."
    lines = ["Execution guardrails violated:"]
    for row in violations:
        lines.append(f"- {row.get('message')}")
    return "\n".join(lines)


def _positive_float_or_none(value: Any) -> float | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Guardrail value must be numeric: {value}") from exc
    if parsed <= 0:
        raise ValueError(f"Guardrail value must be positive: {value}")
    return parsed


def _positive_int_or_none(value: Any) -> int | None:
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Guardrail value must be integer: {value}") from exc
    if parsed <= 0:
        raise ValueError(f"Guardrail value must be positive integer: {value}")
    return parsed


def _normalize_string_list(value: Any, upper: bool = False) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("Guardrail allow-list values must be lists.")
    out: list[str] = []
    seen: set[str] = set()
    for raw in value:
        text = str(raw or "").strip()
        if not text:
            continue
        if upper:
            text = text.upper()
        if text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out


def _as_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float_or_none(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
