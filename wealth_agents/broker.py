from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class BrokerAccountSnapshot:
    broker: str
    currency: str
    cash_available: float
    net_liquidation: float
    asof: str


@dataclass(frozen=True)
class BrokerPosition:
    symbol: str
    quantity: float
    average_price: float | None = None
    market_value: float | None = None


@dataclass(frozen=True)
class BrokerOrderRequest:
    symbol: str
    side: str
    order_type: str
    quantity: float | None = None
    cash_amount_eur: float | None = None
    currency: str = "EUR"
    client_order_id: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class BrokerOrderStatus:
    broker: str
    order_id: str
    client_order_id: str
    symbol: str
    side: str
    order_type: str
    quantity: float | None
    cash_amount_eur: float | None
    status: str
    submitted_at: str
    updated_at: str
    metadata: dict[str, Any]


class BrokerClient(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def get_account_snapshot(self) -> BrokerAccountSnapshot:
        raise NotImplementedError

    @abstractmethod
    def get_positions(self) -> list[BrokerPosition]:
        raise NotImplementedError

    @abstractmethod
    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrderStatus:
        raise NotImplementedError

    @abstractmethod
    def get_order(self, order_id: str) -> BrokerOrderStatus:
        raise NotImplementedError

    @abstractmethod
    def cancel_order(self, order_id: str) -> BrokerOrderStatus:
        raise NotImplementedError


class MockBrokerClient(BrokerClient):
    def __init__(self, state_path: str = "data/broker/mock_state.json", initial_cash_eur: float = 100000.0):
        self._state_path = Path(state_path)
        self._initial_cash_eur = float(initial_cash_eur)
        self._state = self._load_state()

    @property
    def name(self) -> str:
        return "mock"

    def get_account_snapshot(self) -> BrokerAccountSnapshot:
        state = self._state
        cash = float(state.get("account", {}).get("cash_eur") or 0.0)
        asof = str(state.get("updated_at") or _now_iso8601())
        return BrokerAccountSnapshot(
            broker=self.name,
            currency="EUR",
            cash_available=cash,
            net_liquidation=cash,
            asof=asof,
        )

    def get_positions(self) -> list[BrokerPosition]:
        raw = self._state.get("positions") or {}
        if not isinstance(raw, dict):
            return []
        out: list[BrokerPosition] = []
        for symbol in sorted(raw):
            row = raw.get(symbol)
            if not isinstance(row, dict):
                continue
            out.append(
                BrokerPosition(
                    symbol=str(symbol),
                    quantity=float(row.get("quantity") or 0.0),
                    average_price=_as_float_or_none(row.get("average_price")),
                    market_value=_as_float_or_none(row.get("market_value")),
                )
            )
        return out

    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrderStatus:
        _validate_order_request(request)
        now = _now_iso8601()
        next_id = int(self._state.get("next_order_id") or 1)
        order_id = f"MOCK-{next_id:08d}"
        client_order_id = str(request.client_order_id or order_id)
        record = {
            "broker": self.name,
            "order_id": order_id,
            "client_order_id": client_order_id,
            "symbol": str(request.symbol),
            "side": str(request.side).upper(),
            "order_type": str(request.order_type),
            "quantity": _as_float_or_none(request.quantity),
            "cash_amount_eur": _as_float_or_none(request.cash_amount_eur),
            "status": "accepted",
            "submitted_at": now,
            "updated_at": now,
            "metadata": request.metadata or {},
        }
        orders = self._state.setdefault("orders", {})
        if not isinstance(orders, dict):
            self._state["orders"] = {}
            orders = self._state["orders"]
        orders[order_id] = record
        self._state["next_order_id"] = next_id + 1
        self._state["updated_at"] = now
        self._save_state()
        return _to_order_status(record)

    def get_order(self, order_id: str) -> BrokerOrderStatus:
        row = self._order_row(order_id)
        return _to_order_status(row)

    def cancel_order(self, order_id: str) -> BrokerOrderStatus:
        row = self._order_row(order_id)
        if row.get("status") not in {"filled", "canceled"}:
            row["status"] = "canceled"
            row["updated_at"] = _now_iso8601()
            self._state["updated_at"] = row["updated_at"]
            self._save_state()
        return _to_order_status(row)

    def _order_row(self, order_id: str) -> dict[str, Any]:
        key = str(order_id or "").strip()
        if not key:
            raise ValueError("order_id is required.")
        orders = self._state.get("orders") or {}
        if not isinstance(orders, dict):
            raise ValueError("Mock broker state is corrupted: orders must be a mapping.")
        row = orders.get(key)
        if not isinstance(row, dict):
            raise ValueError(f"Order '{key}' not found in mock broker state.")
        return row

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            state = {
                "account": {"cash_eur": float(self._initial_cash_eur)},
                "positions": {},
                "orders": {},
                "next_order_id": 1,
                "updated_at": _now_iso8601(),
            }
            self._write_state(state)
            return state
        try:
            loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid mock broker state file: {self._state_path}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid mock broker state file: {self._state_path}")
        loaded.setdefault("account", {"cash_eur": float(self._initial_cash_eur)})
        loaded.setdefault("positions", {})
        loaded.setdefault("orders", {})
        loaded.setdefault("next_order_id", 1)
        loaded.setdefault("updated_at", _now_iso8601())
        return loaded

    def _save_state(self) -> None:
        self._write_state(self._state)

    def _write_state(self, payload: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        self._state_path.write_text(rendered, encoding="utf-8")


def _validate_order_request(request: BrokerOrderRequest) -> None:
    side = str(request.side or "").strip().upper()
    if side not in {"BUY", "SELL"}:
        raise ValueError("Order side must be BUY or SELL.")
    order_type = str(request.order_type or "").strip()
    if order_type not in {"cash_amount", "quantity"}:
        raise ValueError("order_type must be one of: cash_amount, quantity.")
    if order_type == "cash_amount":
        amount = _as_float_or_none(request.cash_amount_eur)
        if amount is None or amount <= 0:
            raise ValueError("cash_amount_eur must be positive for cash_amount orders.")
    if order_type == "quantity":
        quantity = _as_float_or_none(request.quantity)
        if quantity is None or quantity <= 0:
            raise ValueError("quantity must be positive for quantity orders.")
    symbol = str(request.symbol or "").strip()
    if not symbol:
        raise ValueError("symbol is required.")


def _to_order_status(row: dict[str, Any]) -> BrokerOrderStatus:
    return BrokerOrderStatus(
        broker=str(row.get("broker") or "mock"),
        order_id=str(row.get("order_id") or ""),
        client_order_id=str(row.get("client_order_id") or ""),
        symbol=str(row.get("symbol") or ""),
        side=str(row.get("side") or ""),
        order_type=str(row.get("order_type") or ""),
        quantity=_as_float_or_none(row.get("quantity")),
        cash_amount_eur=_as_float_or_none(row.get("cash_amount_eur")),
        status=str(row.get("status") or ""),
        submitted_at=str(row.get("submitted_at") or ""),
        updated_at=str(row.get("updated_at") or ""),
        metadata=_as_dict(row.get("metadata")),
    )


def _as_float_or_none(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    return {}


def _now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
