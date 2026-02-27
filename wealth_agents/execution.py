from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .broker import BrokerOrderRequest, MockBrokerClient


def execute_order_proposal(
    proposal_path: str,
    broker: str = "mock",
    mock_state_path: str = "data/broker/mock_state.json",
    dry_run: bool = False,
    output_path: str | None = None,
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
                "isin": str(row.get("isin") or ""),
                "name": str(row.get("name") or ""),
                "bucket": str(row.get("bucket") or ""),
            },
        )
        if dry_run:
            submitted.append(
                {
                    "index": idx,
                    "client_order_id": client_order_id,
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
        "submitted_count": len(submitted),
        "skipped_count": len(skipped),
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


def _now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
