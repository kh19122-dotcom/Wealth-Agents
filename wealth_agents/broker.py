from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
from pathlib import Path
from typing import Any

from .market_prices import build_ibkr_contract


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
    time_in_force: str = "DAY"
    limit_price: float | None = None
    broker_contract: dict[str, Any] | None = None
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
        return _lookup_order_row(orders, key, "mock broker state")

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


class IbkrBrokerClient(BrokerClient):
    def __init__(
        self,
        state_path: str = "data/broker/ibkr_state.json",
        host: str = "127.0.0.1",
        port: int = 7497,
        client_id: int = 37,
        timeout_sec: float = 8.0,
        what_if: bool = False,
        limit_buffer_pct: float = 0.5,
    ):
        if float(limit_buffer_pct) < 0:
            raise ValueError("limit_buffer_pct must be non-negative.")
        self._state_path = Path(state_path)
        self._host = str(host)
        self._port = int(port)
        self._client_id = int(client_id)
        self._timeout_sec = float(timeout_sec)
        self._what_if = bool(what_if)
        self._limit_buffer_pct = float(limit_buffer_pct)
        self._state = self._load_state()

    @property
    def name(self) -> str:
        return "ibkr"

    def get_account_snapshot(self) -> BrokerAccountSnapshot:
        ib = self._connect(readonly=True)
        try:
            rows = list(ib.accountSummary())
        finally:
            self._disconnect(ib)

        net_liquidation, snapshot_currency = _summary_float(rows, "NetLiquidation")
        cash_available, cash_currency = _summary_float(rows, "AvailableFunds")
        if cash_available is None:
            cash_available, cash_currency = _summary_float(rows, "TotalCashValue")
        currency = snapshot_currency or cash_currency or "EUR"
        return BrokerAccountSnapshot(
            broker=self.name,
            currency=currency,
            cash_available=float(cash_available or 0.0),
            net_liquidation=float(net_liquidation or cash_available or 0.0),
            asof=_now_iso8601(),
        )

    def get_positions(self) -> list[BrokerPosition]:
        ib = self._connect(readonly=True)
        try:
            raw_positions = list(ib.positions())
        finally:
            self._disconnect(ib)

        out: list[BrokerPosition] = []
        for row in raw_positions:
            contract = getattr(row, "contract", None)
            symbol = _contract_symbol(contract)
            if not symbol:
                continue
            avg_cost = _as_float_or_none(getattr(row, "avgCost", None))
            quantity = _as_float_or_none(getattr(row, "position", None)) or 0.0
            out.append(
                BrokerPosition(
                    symbol=symbol,
                    quantity=float(quantity),
                    average_price=avg_cost,
                    market_value=None,
                )
            )
        return out

    def submit_order(self, request: BrokerOrderRequest) -> BrokerOrderStatus:
        _validate_order_request(request)
        if str(request.side or "").strip().upper() != "BUY":
            raise ValueError("IBKR execution currently supports BUY orders only.")

        ib_module = _load_ib_insync()
        ib = self._connect(readonly=False)
        try:
            resolved_contract = self._qualify_contract(ib, request)
            trade_currency = str(getattr(resolved_contract, "currency", "") or request.currency).strip().upper()
            if not trade_currency:
                trade_currency = str(request.currency or "EUR").strip().upper() or "EUR"

            quote = self._snapshot_price(ib, resolved_contract, side=request.side)
            limit_price = float(request.limit_price or self._limit_price_from_quote(quote, side=request.side))
            if limit_price <= 0:
                raise ValueError(f"Unable to determine a positive limit price for '{request.symbol}'.")

            fx_rate = self._resolve_fx_rate(
                ib=ib,
                base_currency=str(request.currency or "EUR").strip().upper() or "EUR",
                quote_currency=trade_currency,
            )

            if request.order_type == "quantity":
                quantity = _as_float_or_none(request.quantity)
                if quantity is None or quantity <= 0:
                    raise ValueError("quantity must be positive for IBKR quantity orders.")
                trade_cash = float(quantity) * float(limit_price)
            else:
                if request.order_type != "cash_amount":
                    raise ValueError("IBKR execution supports only cash_amount or quantity orders.")
                amount_in_request_currency = _as_float_or_none(request.cash_amount_eur)
                if amount_in_request_currency is None or amount_in_request_currency <= 0:
                    raise ValueError("cash_amount_eur must be positive for IBKR cash_amount orders.")
                trade_cash = float(amount_in_request_currency) * float(fx_rate)
                quantity = self._quantity_from_cash(
                    trade_cash=trade_cash,
                    limit_price=limit_price,
                    contract=resolved_contract,
                )

            order = ib_module.Order(
                action=str(request.side).upper(),
                orderType="LMT",
                totalQuantity=float(quantity),
                lmtPrice=float(limit_price),
                tif=str(request.time_in_force or "DAY").strip().upper() or "DAY",
                orderRef=str(request.client_order_id or ""),
                transmit=(not self._what_if),
            )

            if self._what_if:
                preview = self._run_what_if(ib, resolved_contract, order)
                record = self._build_order_record(
                    request=request,
                    order_id=str(request.client_order_id or f"WHATIF-{int(datetime.now(timezone.utc).timestamp())}"),
                    status="what_if",
                    quantity=float(quantity),
                    trade_currency=trade_currency,
                    quote_price=float(quote),
                    limit_price=float(limit_price),
                    fx_rate=float(fx_rate),
                    resolved_contract=resolved_contract,
                    submitted_at=_now_iso8601(),
                    updated_at=_now_iso8601(),
                    extra_metadata={"what_if": _serialize_what_if(preview)},
                )
                self._persist_order(record)
                return _to_order_status(record)

            trade = ib.placeOrder(resolved_contract, order)
            submitted_at = _now_iso8601()
            order_id = _extract_ibkr_order_id(trade=trade, order=order, fallback=request.client_order_id)
            status_name = _extract_trade_status(trade) or "Submitted"
            record = self._build_order_record(
                request=request,
                order_id=order_id,
                status=status_name,
                quantity=float(quantity),
                trade_currency=trade_currency,
                quote_price=float(quote),
                limit_price=float(limit_price),
                fx_rate=float(fx_rate),
                resolved_contract=resolved_contract,
                submitted_at=submitted_at,
                updated_at=submitted_at,
                extra_metadata={"trade": _serialize_trade(trade)},
            )
            self._persist_order(record)
            return _to_order_status(record)
        finally:
            self._disconnect(ib)

    def get_order(self, order_id: str) -> BrokerOrderStatus:
        row = self._order_row(order_id)
        if _status_is_terminal(str(row.get("status") or "")):
            return _to_order_status(row)
        ib = self._connect(readonly=False)
        try:
            refreshed = self._refresh_order_row(ib=ib, row=row)
        finally:
            self._disconnect(ib)
        if refreshed:
            self._state["updated_at"] = str(row.get("updated_at") or _now_iso8601())
            self._save_state()
        return _to_order_status(row)

    def cancel_order(self, order_id: str) -> BrokerOrderStatus:
        row = self._order_row(order_id)
        status = str(row.get("status") or "").strip()
        if str(status).strip().lower() in {"what_if", "canceled", "cancelled"}:
            if str(status).strip().lower() != "canceled":
                row["status"] = "canceled"
                row["updated_at"] = _now_iso8601()
                self._state["updated_at"] = row["updated_at"]
                self._save_state()
            return _to_order_status(row)
        if _status_is_terminal(status):
            return _to_order_status(row)

        ib = self._connect(readonly=False)
        try:
            trade = self._find_trade_for_row(ib=ib, row=row, include_completed=False)
            if trade is None:
                refreshed = self._refresh_order_row(ib=ib, row=row)
                if refreshed and _status_is_terminal(str(row.get("status") or "")):
                    self._state["updated_at"] = str(row.get("updated_at") or _now_iso8601())
                    self._save_state()
                    return _to_order_status(row)
                raise ValueError(f"Unable to locate live IBKR order '{order_id}' for cancellation.")

            order = getattr(trade, "order", None)
            if order is None:
                raise ValueError(f"IBKR live order '{order_id}' has no order payload to cancel.")

            canceled_trade = ib.cancelOrder(order)
            if canceled_trade is not None:
                self._update_row_from_trade(row=row, trade=canceled_trade)
            else:
                row["status"] = "PendingCancel"
                row["updated_at"] = _now_iso8601()

            self._refresh_order_row(ib=ib, row=row)
        finally:
            self._disconnect(ib)

        self._state["updated_at"] = str(row.get("updated_at") or _now_iso8601())
        self._save_state()
        return _to_order_status(row)

    def _build_order_record(
        self,
        *,
        request: BrokerOrderRequest,
        order_id: str,
        status: str,
        quantity: float,
        trade_currency: str,
        quote_price: float,
        limit_price: float,
        fx_rate: float,
        resolved_contract: Any,
        submitted_at: str,
        updated_at: str,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        metadata = dict(request.metadata or {})
        metadata.update(
            {
                "ticker": str(request.symbol or ""),
                "request_currency": str(request.currency or "EUR").strip().upper() or "EUR",
                "trade_currency": str(trade_currency or "").strip().upper(),
                "quote_price": round(float(quote_price), 8),
                "limit_price": round(float(limit_price), 8),
                "fx_rate": round(float(fx_rate), 8),
                "time_in_force": str(request.time_in_force or "DAY").strip().upper() or "DAY",
                "contract": _serialize_contract(resolved_contract),
            }
        )
        if extra_metadata:
            metadata.update(extra_metadata)
        return {
            "broker": self.name,
            "order_id": str(order_id),
            "client_order_id": str(request.client_order_id or order_id),
            "symbol": str(request.symbol),
            "side": str(request.side).upper(),
            "order_type": str(request.order_type),
            "quantity": float(quantity),
            "cash_amount_eur": _as_float_or_none(request.cash_amount_eur),
            "status": str(status),
            "submitted_at": submitted_at,
            "updated_at": updated_at,
            "metadata": metadata,
        }

    def _connect(self, *, readonly: bool) -> Any:
        ib_module = _load_ib_insync()
        ib = ib_module.IB()
        ib.connect(
            host=self._host,
            port=self._port,
            clientId=self._client_id,
            timeout=self._timeout_sec,
            readonly=readonly,
        )
        return ib

    def _disconnect(self, ib: Any) -> None:
        try:
            ib.disconnect()
        except Exception:
            pass

    def _qualify_contract(self, ib: Any, request: BrokerOrderRequest) -> Any:
        contract_spec = request.broker_contract or {}
        if contract_spec:
            contract = build_ibkr_contract(contract_spec)
        else:
            contract = build_ibkr_contract(
                {
                    "symbol": str(request.symbol or "").strip(),
                    "secType": "STK",
                    "exchange": "SMART",
                    "currency": str(request.currency or "EUR").strip().upper() or "EUR",
                }
            )
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            raise ValueError(f"IBKR contract qualification returned no match for '{request.symbol}'.")
        return qualified[0]

    def _snapshot_price(self, ib: Any, contract: Any, *, side: str) -> float:
        tickers = list(ib.reqTickers(contract) or [])
        if not tickers:
            raise ValueError(f"IBKR market data snapshot returned no ticker for '{_contract_symbol(contract)}'.")
        ticker = tickers[0]
        value = _ticker_price(ticker, side=side)
        if value is None or value <= 0:
            raise ValueError(f"Unable to resolve a positive market price for '{_contract_symbol(contract)}'.")
        return float(value)

    def _limit_price_from_quote(self, quote_price: float, *, side: str) -> float:
        side_value = str(side or "").strip().upper()
        buffer_fraction = self._limit_buffer_pct / 100.0
        if side_value == "BUY":
            price = float(quote_price) * (1.0 + buffer_fraction)
        else:
            price = float(quote_price) * max(0.0, 1.0 - buffer_fraction)
        return round(float(price), 8)

    def _resolve_fx_rate(self, *, ib: Any, base_currency: str, quote_currency: str) -> float:
        base = str(base_currency or "").strip().upper() or "EUR"
        quote = str(quote_currency or "").strip().upper() or base
        if base == quote:
            return 1.0

        ib_module = _load_ib_insync()
        direct = ib_module.Contract(
            secType="CASH",
            symbol=base,
            exchange="IDEALPRO",
            currency=quote,
        )
        rate = self._qualified_fx_rate(ib, direct)
        if rate is not None and rate > 0:
            return float(rate)

        inverse = ib_module.Contract(
            secType="CASH",
            symbol=quote,
            exchange="IDEALPRO",
            currency=base,
        )
        inverse_rate = self._qualified_fx_rate(ib, inverse)
        if inverse_rate is not None and inverse_rate > 0:
            return 1.0 / float(inverse_rate)

        raise ValueError(f"Unable to resolve IBKR FX rate for {base}/{quote}.")

    def _qualified_fx_rate(self, ib: Any, contract: Any) -> float | None:
        qualified = ib.qualifyContracts(contract)
        if not qualified:
            return None
        tickers = list(ib.reqTickers(qualified[0]) or [])
        if not tickers:
            return None
        return _ticker_price(tickers[0], side="BUY")

    def _quantity_from_cash(self, *, trade_cash: float, limit_price: float, contract: Any) -> float:
        if trade_cash <= 0 or limit_price <= 0:
            raise ValueError("trade_cash and limit_price must both be positive.")
        sec_type = str(getattr(contract, "secType", "") or "").strip().upper()
        if sec_type in {"STK", "ETF"}:
            quantity = math.floor((float(trade_cash) / float(limit_price)) + 1e-12)
        else:
            quantity = math.floor((float(trade_cash) / float(limit_price)) * 10000.0) / 10000.0
        if quantity <= 0:
            raise ValueError(
                f"Cash budget {round(float(trade_cash), 8)} {getattr(contract, 'currency', '')} "
                f"is too small to buy one unit of '{_contract_symbol(contract)}' at {round(float(limit_price), 8)}."
            )
        return float(quantity)

    def _run_what_if(self, ib: Any, contract: Any, order: Any) -> Any:
        if hasattr(ib, "whatIfOrder"):
            return ib.whatIfOrder(contract, order)
        setattr(order, "whatIf", True)
        return ib.placeOrder(contract, order)

    def _load_state(self) -> dict[str, Any]:
        if not self._state_path.exists():
            state = {
                "orders": {},
                "updated_at": _now_iso8601(),
            }
            self._write_state(state)
            return state
        try:
            loaded = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid IBKR broker state file: {self._state_path}") from exc
        if not isinstance(loaded, dict):
            raise ValueError(f"Invalid IBKR broker state file: {self._state_path}")
        loaded.setdefault("orders", {})
        loaded.setdefault("updated_at", _now_iso8601())
        return loaded

    def _save_state(self) -> None:
        self._write_state(self._state)

    def _write_state(self, payload: dict[str, Any]) -> None:
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        self._state_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def _persist_order(self, row: dict[str, Any]) -> None:
        orders = self._state.setdefault("orders", {})
        if not isinstance(orders, dict):
            self._state["orders"] = {}
            orders = self._state["orders"]
        orders[str(row.get("order_id") or "")] = row
        self._state["updated_at"] = str(row.get("updated_at") or _now_iso8601())
        self._save_state()

    def _order_row(self, order_id: str) -> dict[str, Any]:
        key = str(order_id or "").strip()
        if not key:
            raise ValueError("order_id is required.")
        orders = self._state.get("orders") or {}
        if not isinstance(orders, dict):
            raise ValueError("IBKR broker state is corrupted: orders must be a mapping.")
        return _lookup_order_row(orders, key, "IBKR broker state")

    def _refresh_order_row(self, *, ib: Any, row: dict[str, Any]) -> bool:
        trade = self._find_trade_for_row(ib=ib, row=row, include_completed=True)
        if trade is None:
            return False
        self._update_row_from_trade(row=row, trade=trade)
        return True

    def _find_trade_for_row(self, *, ib: Any, row: dict[str, Any], include_completed: bool) -> Any | None:
        identifiers = _row_identifiers(row)
        if not identifiers:
            return None
        for trade in _collect_ibkr_trades(ib, include_completed=include_completed):
            trade_ids = _trade_identifiers(trade)
            if identifiers.intersection(trade_ids):
                return trade
        return None

    def _update_row_from_trade(self, *, row: dict[str, Any], trade: Any) -> None:
        order = getattr(trade, "order", None)
        contract = getattr(trade, "contract", None)
        metadata = _as_dict(row.get("metadata"))
        metadata["trade"] = _serialize_trade(trade)
        if contract is not None:
            metadata["contract"] = _serialize_contract(contract)

        order_status = _extract_trade_status(trade)
        if order_status:
            row["status"] = order_status
        if order is not None:
            order_ref = str(getattr(order, "orderRef", "") or "").strip()
            if order_ref:
                row["client_order_id"] = order_ref
            total_quantity = _as_float_or_none(getattr(order, "totalQuantity", None))
            if total_quantity is not None and total_quantity > 0:
                row["quantity"] = total_quantity
        if contract is not None:
            symbol = _contract_symbol(contract)
            if symbol:
                row["symbol"] = symbol

        metadata.setdefault("ticker", str(row.get("symbol") or ""))
        row["metadata"] = metadata
        order_id = _extract_ibkr_order_id(trade=trade, order=order, fallback=row.get("order_id"))
        if order_id:
            row["order_id"] = order_id
        row["updated_at"] = _now_iso8601()


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
    time_in_force = str(request.time_in_force or "").strip().upper() or "DAY"
    if time_in_force not in {"DAY", "GTC"}:
        raise ValueError("time_in_force must be one of: DAY, GTC.")
    if request.limit_price is not None:
        limit_price = _as_float_or_none(request.limit_price)
        if limit_price is None or limit_price <= 0:
            raise ValueError("limit_price must be positive when provided.")
    if request.broker_contract is not None and not isinstance(request.broker_contract, dict):
        raise ValueError("broker_contract must be a mapping when provided.")


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


def _lookup_order_row(orders: dict[str, Any], key: str, label: str) -> dict[str, Any]:
    row = orders.get(key)
    if isinstance(row, dict):
        return row
    for candidate in orders.values():
        if not isinstance(candidate, dict):
            continue
        if str(candidate.get("client_order_id") or "").strip() == key:
            return candidate
    raise ValueError(f"Order '{key}' not found in {label}.")


def _summary_float(rows: list[Any], tag: str) -> tuple[float | None, str | None]:
    preferred: tuple[float | None, str | None] = (None, None)
    fallback: tuple[float | None, str | None] = (None, None)
    for row in rows:
        if str(getattr(row, "tag", "") or "") != str(tag):
            continue
        value = _as_float_or_none(getattr(row, "value", None))
        currency = str(getattr(row, "currency", "") or "").strip().upper() or None
        if value is None:
            continue
        if currency == "BASE":
            return float(value), currency
        if preferred[0] is None and currency == "EUR":
            preferred = (float(value), currency)
        elif fallback[0] is None:
            fallback = (float(value), currency)
    if preferred[0] is not None:
        return preferred
    return fallback


def _contract_symbol(contract: Any) -> str:
    if contract is None:
        return ""
    for field_name in ("localSymbol", "symbol"):
        value = str(getattr(contract, field_name, "") or "").strip()
        if value:
            return value
    return ""


def _serialize_contract(contract: Any) -> dict[str, Any]:
    return {
        "conid": _as_int_or_none(getattr(contract, "conId", None)),
        "symbol": str(getattr(contract, "symbol", "") or ""),
        "local_symbol": str(getattr(contract, "localSymbol", "") or ""),
        "exchange": str(getattr(contract, "exchange", "") or ""),
        "primary_exchange": str(getattr(contract, "primaryExchange", "") or ""),
        "currency": str(getattr(contract, "currency", "") or ""),
        "sec_type": str(getattr(contract, "secType", "") or ""),
    }


def _serialize_trade(trade: Any) -> dict[str, Any]:
    if trade is None:
        return {}
    order_status = getattr(trade, "orderStatus", None)
    order = getattr(trade, "order", None)
    return {
        "status": str(getattr(order_status, "status", "") or ""),
        "order_id": _as_int_or_none(getattr(order, "orderId", None)),
        "perm_id": _as_int_or_none(getattr(order_status, "permId", None)),
        "filled": _as_float_or_none(getattr(order_status, "filled", None)),
        "remaining": _as_float_or_none(getattr(order_status, "remaining", None)),
        "avg_fill_price": _as_float_or_none(getattr(order_status, "avgFillPrice", None)),
    }


def _serialize_what_if(preview: Any) -> dict[str, Any]:
    if preview is None:
        return {}
    fields = (
        "status",
        "commission",
        "commissionCurrency",
        "initMarginChange",
        "maintMarginChange",
        "equityWithLoanChange",
        "warningText",
    )
    out: dict[str, Any] = {}
    for field_name in fields:
        value = getattr(preview, field_name, None)
        if value is None or value == "":
            continue
        out[field_name] = value
    return out


def _ticker_price(ticker: Any, *, side: str) -> float | None:
    if ticker is None:
        return None
    market_price = getattr(ticker, "marketPrice", None)
    market_price_value = market_price() if callable(market_price) else market_price
    if str(side or "").strip().upper() == "BUY":
        candidates = (
            getattr(ticker, "ask", None),
            market_price_value,
            getattr(ticker, "last", None),
            getattr(ticker, "close", None),
            getattr(ticker, "bid", None),
        )
    else:
        candidates = (
            getattr(ticker, "bid", None),
            market_price_value,
            getattr(ticker, "last", None),
            getattr(ticker, "close", None),
            getattr(ticker, "ask", None),
        )
    for value in candidates:
        parsed = _as_float_or_none(value)
        if parsed is not None and parsed > 0:
            return float(parsed)
    return None


def _extract_ibkr_order_id(trade: Any, order: Any, fallback: str | None) -> str:
    candidates = (
        getattr(getattr(trade, "order", None), "orderId", None),
        getattr(getattr(trade, "orderStatus", None), "orderId", None),
        getattr(order, "orderId", None),
        getattr(getattr(trade, "orderStatus", None), "permId", None),
    )
    for value in candidates:
        parsed = _as_int_or_none(value)
        if parsed is not None:
            return str(parsed)
    fallback_value = str(fallback or "").strip()
    if fallback_value:
        return fallback_value
    return f"IBKR-{int(datetime.now(timezone.utc).timestamp())}"


def _extract_trade_status(trade: Any) -> str:
    order_status = getattr(trade, "orderStatus", None)
    status = str(getattr(order_status, "status", "") or "").strip()
    return status


def _status_is_terminal(status: str) -> bool:
    normalized = str(status or "").strip().lower()
    return normalized in {
        "filled",
        "cancelled",
        "canceled",
        "inactive",
        "api_cancelled",
        "what_if",
    }


def _row_identifiers(row: dict[str, Any]) -> set[str]:
    metadata = _as_dict(row.get("metadata"))
    trade_meta = _as_dict(metadata.get("trade"))
    identifiers: set[str] = set()
    for value in (
        row.get("order_id"),
        row.get("client_order_id"),
        trade_meta.get("order_id"),
        trade_meta.get("perm_id"),
    ):
        text = str(value or "").strip()
        if text:
            identifiers.add(text)
    return identifiers


def _trade_identifiers(trade: Any) -> set[str]:
    order = getattr(trade, "order", None)
    order_status = getattr(trade, "orderStatus", None)
    identifiers: set[str] = set()
    for value in (
        getattr(order, "orderId", None),
        getattr(order, "permId", None),
        getattr(order_status, "orderId", None),
        getattr(order_status, "permId", None),
        getattr(order, "orderRef", None),
    ):
        text = str(value or "").strip()
        if text:
            identifiers.add(text)
    return identifiers


def _collect_ibkr_trades(ib: Any, *, include_completed: bool) -> list[Any]:
    out: list[Any] = []
    seen: set[str] = set()

    for accessor_name in ("trades", "openTrades"):
        accessor = getattr(ib, accessor_name, None)
        if not callable(accessor):
            continue
        try:
            rows = list(accessor() or [])
        except Exception:
            continue
        for trade in rows:
            key = _trade_collection_key(trade)
            if key in seen:
                continue
            seen.add(key)
            out.append(trade)

    if include_completed:
        accessor = getattr(ib, "reqCompletedOrders", None)
        if callable(accessor):
            try:
                rows = list(accessor(apiOnly=False) or [])
            except TypeError:
                rows = list(accessor(False) or [])
            except Exception:
                rows = []
            for trade in rows:
                key = _trade_collection_key(trade)
                if key in seen:
                    continue
                seen.add(key)
                out.append(trade)
    return out


def _trade_collection_key(trade: Any) -> str:
    identifiers = sorted(_trade_identifiers(trade))
    if identifiers:
        return "|".join(identifiers)
    return f"trade:{id(trade)}"


def _as_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _load_ib_insync() -> Any:
    try:
        import ib_insync
    except ImportError as exc:
        raise RuntimeError("ib_insync is required for IBKR broker execution. Run: uv sync --extra dev --extra ibkr") from exc
    return ib_insync


def _now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
