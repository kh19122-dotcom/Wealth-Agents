import json
from pathlib import Path
import sys
import types

import pytest
import yaml

from wealth_agents.broker import BrokerOrderRequest, IbkrBrokerClient, MockBrokerClient
from wealth_agents.execution import execute_order_proposal, get_broker_order_status, sync_broker_orders


def _write_proposal(path: Path) -> None:
    payload = {
        "month": "2026-03",
        "currency": "EUR",
        "budget_eur": 2500,
        "policy_hash": "test-hash",
        "orders": [
            {
                "side": "BUY",
                "instrument_id": "sp500_acc",
                "ticker": "CSPX.L",
                "isin": "IE00B5BMR087",
                "name": "iShares Core S&P 500 UCITS ETF (Acc)",
                "bucket": "global_equity",
                "amount_eur": 1500,
            },
            {
                "side": "BUY",
                "instrument_id": "xeon",
                "ticker": "XEON.DE",
                "isin": "LU0290358497",
                "name": "Xtrackers II EUR Overnight Rate Swap UCITS ETF (XEON)",
                "bucket": "bonds_cashlike",
                "amount_eur": 1000,
            },
        ],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_guardrails(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_ibkr_contracts(path: Path) -> None:
    payload = {
        "contracts": {
            "CSPX.L": {
                "symbol": "CSPX",
                "secType": "STK",
                "exchange": "SMART",
                "primaryExchange": "LSEETF",
                "currency": "USD",
            },
            "XEON.DE": {
                "symbol": "XEON",
                "secType": "STK",
                "exchange": "SMART",
                "primaryExchange": "IBIS",
                "currency": "EUR",
            },
        }
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _install_fake_ib_insync(monkeypatch: pytest.MonkeyPatch, *, price_usd: float = 100.0, price_eur: float = 50.0, eurusd: float = 1.2):
    class FakeContract:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
            if hasattr(self, "conId"):
                self.conId = int(self.conId)

    class FakeOrder:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    class FakeIB:
        _shared_trades: list = []
        _next_order_id: int = 7000

        def __init__(self):
            return None

        def connect(self, host: str, port: int, clientId: int, timeout: float, readonly: bool):
            return True

        def disconnect(self):
            return None

        def qualifyContracts(self, contract):
            if not getattr(contract, "exchange", None):
                contract.exchange = "SMART"
            if not getattr(contract, "primaryExchange", None):
                contract.primaryExchange = getattr(contract, "exchange", "SMART")
            if not getattr(contract, "currency", None):
                contract.currency = "EUR"
            if not getattr(contract, "secType", None):
                contract.secType = "STK"
            if not getattr(contract, "symbol", None):
                contract.symbol = "UNKNOWN"
            if not getattr(contract, "localSymbol", None):
                contract.localSymbol = getattr(contract, "symbol", "UNKNOWN")
            if not getattr(contract, "conId", None):
                contract.conId = 101001
            return [contract]

        def reqTickers(self, contract):
            if getattr(contract, "secType", "") == "CASH":
                price = eurusd if getattr(contract, "symbol", "") == "EUR" else (1.0 / eurusd)
                return [
                    types.SimpleNamespace(
                        ask=price,
                        bid=price,
                        last=price,
                        close=price,
                        marketPrice=lambda: price,
                    )
                ]

            symbol = getattr(contract, "symbol", "")
            price = price_usd if symbol == "CSPX" else price_eur
            return [
                types.SimpleNamespace(
                    ask=price,
                    bid=max(price - 0.5, 0.01),
                    last=price,
                    close=price,
                    marketPrice=lambda: price,
                )
            ]

        def placeOrder(self, contract, order):
            cls = type(self)
            cls._next_order_id += 1
            trade = types.SimpleNamespace(
                contract=contract,
                order=types.SimpleNamespace(
                    orderId=cls._next_order_id,
                    orderRef=getattr(order, "orderRef", ""),
                    totalQuantity=getattr(order, "totalQuantity", None),
                ),
                orderStatus=types.SimpleNamespace(
                    status="Submitted",
                    permId=cls._next_order_id + 90000,
                    filled=0,
                    remaining=getattr(order, "totalQuantity", None),
                    avgFillPrice=0,
                ),
            )
            cls._shared_trades = [
                row
                for row in cls._shared_trades
                if getattr(getattr(row, "order", None), "orderId", None) != trade.order.orderId
            ]
            cls._shared_trades.append(trade)
            return trade

        def whatIfOrder(self, contract, order):
            return types.SimpleNamespace(
                status="PreSubmitted",
                commission="1.25",
                commissionCurrency=getattr(contract, "currency", "EUR"),
                initMarginChange="1000",
                maintMarginChange="500",
                equityWithLoanChange="-1000",
            )

        def accountSummary(self):
            return [
                types.SimpleNamespace(tag="NetLiquidation", value="100000", currency="BASE"),
                types.SimpleNamespace(tag="AvailableFunds", value="25000", currency="BASE"),
            ]

        def positions(self):
            return [
                types.SimpleNamespace(
                    contract=types.SimpleNamespace(symbol="CSPX", localSymbol="CSPX.L"),
                    position=10,
                    avgCost=98.5,
                )
            ]

        def trades(self):
            return list(type(self)._shared_trades)

        def openTrades(self):
            return [
                trade
                for trade in type(self)._shared_trades
                if str(getattr(getattr(trade, "orderStatus", None), "status", "") or "").lower()
                not in {"filled", "cancelled", "canceled"}
            ]

        def reqCompletedOrders(self, apiOnly=False):
            return [
                trade
                for trade in type(self)._shared_trades
                if str(getattr(getattr(trade, "orderStatus", None), "status", "") or "").lower()
                in {"filled", "cancelled", "canceled"}
            ]

        def cancelOrder(self, order):
            order_id = getattr(order, "orderId", None)
            for trade in type(self)._shared_trades:
                if getattr(getattr(trade, "order", None), "orderId", None) == order_id:
                    trade.orderStatus.status = "Cancelled"
                    trade.orderStatus.remaining = 0
                    return trade
            return None

    fake_module = types.SimpleNamespace(IB=FakeIB, Contract=FakeContract, Order=FakeOrder)
    monkeypatch.setitem(sys.modules, "ib_insync", fake_module)


def test_mock_broker_submit_get_cancel_order(tmp_path: Path):
    state_path = tmp_path / "data/broker/mock_state.json"
    broker = MockBrokerClient(state_path=str(state_path), initial_cash_eur=50000)

    request = BrokerOrderRequest(
        symbol="sp500_acc",
        side="BUY",
        order_type="cash_amount",
        cash_amount_eur=1000,
        client_order_id="2026-03:sp500_acc:1",
    )
    accepted = broker.submit_order(request)
    assert accepted.order_id.startswith("MOCK-")
    assert accepted.status == "accepted"
    fetched = broker.get_order(accepted.order_id)
    assert fetched.client_order_id == "2026-03:sp500_acc:1"
    canceled = broker.cancel_order(accepted.order_id)
    assert canceled.status == "canceled"


def test_execute_order_proposal_dry_run(tmp_path: Path):
    proposal_path = tmp_path / "orders/proposed_2026-03.json"
    _write_proposal(proposal_path)
    state_path = tmp_path / "data/broker/mock_state.json"
    output_path = tmp_path / "orders/execution_dry_run.json"

    result = execute_order_proposal(
        proposal_path=str(proposal_path),
        broker="mock",
        mock_state_path=str(state_path),
        dry_run=True,
        output_path=str(output_path),
    )
    assert result["dry_run"] is True
    assert result["submitted_count"] == 2
    assert result["skipped_count"] == 0
    assert result["submitted"][0]["status"] == "dry_run"
    assert output_path.exists()
    assert state_path.exists() is False


def test_execute_order_proposal_submits_to_mock_broker(tmp_path: Path):
    proposal_path = tmp_path / "orders/proposed_2026-03.json"
    _write_proposal(proposal_path)
    state_path = tmp_path / "data/broker/mock_state.json"

    result = execute_order_proposal(
        proposal_path=str(proposal_path),
        broker="mock",
        mock_state_path=str(state_path),
        dry_run=False,
    )
    assert result["dry_run"] is False
    assert result["submitted_count"] == 2
    assert result["skipped_count"] == 0
    assert result["submitted"][0]["status"] == "accepted"
    assert state_path.exists()

    broker = MockBrokerClient(state_path=str(state_path))
    status = broker.get_order(result["submitted"][0]["order_id"])
    assert status.status == "accepted"


def test_execute_order_proposal_blocks_on_guardrail_violation(tmp_path: Path):
    proposal_path = tmp_path / "orders/proposed_2026-03.json"
    _write_proposal(proposal_path)
    state_path = tmp_path / "data/broker/mock_state.json"
    guardrails_path = tmp_path / "config/execution_guardrails.yml"
    _write_guardrails(
        guardrails_path,
        {
            "enabled": True,
            "fail_on_violation": True,
            "max_order_amount_eur": 1000,
        },
    )

    with pytest.raises(ValueError, match="Execution guardrails violated"):
        execute_order_proposal(
            proposal_path=str(proposal_path),
            broker="mock",
            mock_state_path=str(state_path),
            dry_run=True,
            guardrails_path=str(guardrails_path),
        )


def test_execute_order_proposal_guardrail_advisory_skips_violating_orders(tmp_path: Path):
    proposal_path = tmp_path / "orders/proposed_2026-03.json"
    _write_proposal(proposal_path)
    state_path = tmp_path / "data/broker/mock_state.json"
    guardrails_path = tmp_path / "config/execution_guardrails.yml"
    _write_guardrails(
        guardrails_path,
        {
            "enabled": True,
            "fail_on_violation": False,
            "max_order_amount_eur": 1200,
        },
    )

    result = execute_order_proposal(
        proposal_path=str(proposal_path),
        broker="mock",
        mock_state_path=str(state_path),
        dry_run=True,
        guardrails_path=str(guardrails_path),
    )
    assert result["submitted_count"] == 1
    assert result["skipped_count"] == 1
    assert "guardrail(advisory)" in result["skipped"][0]["reason"]
    assert result["guardrails"]["enabled"] is True
    assert result["guardrails"]["passed"] is False


def test_execute_order_proposal_guardrail_whitelist_enforced(tmp_path: Path):
    proposal_path = tmp_path / "orders/proposed_2026-03.json"
    _write_proposal(proposal_path)
    state_path = tmp_path / "data/broker/mock_state.json"
    guardrails_path = tmp_path / "config/execution_guardrails.yml"
    _write_guardrails(
        guardrails_path,
        {
            "enabled": True,
            "fail_on_violation": True,
            "allowed_instrument_ids": ["sp500_acc"],
        },
    )

    with pytest.raises(ValueError, match="allowed_instrument_ids"):
        execute_order_proposal(
            proposal_path=str(proposal_path),
            broker="mock",
            mock_state_path=str(state_path),
            dry_run=True,
            guardrails_path=str(guardrails_path),
        )


def test_ibkr_broker_snapshot_and_positions(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _install_fake_ib_insync(monkeypatch)
    broker = IbkrBrokerClient(state_path=str(tmp_path / "data/broker/ibkr_state.json"))

    snapshot = broker.get_account_snapshot()
    positions = broker.get_positions()

    assert snapshot.broker == "ibkr"
    assert snapshot.net_liquidation == 100000.0
    assert snapshot.cash_available == 25000.0
    assert positions[0].symbol == "CSPX.L"
    assert positions[0].quantity == 10.0


def test_execute_order_proposal_submits_to_ibkr_with_quantity_conversion(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _install_fake_ib_insync(monkeypatch, price_usd=100.0, price_eur=50.0, eurusd=1.2)
    proposal_path = tmp_path / "orders/proposed_2026-03.json"
    contracts_path = tmp_path / "config/ibkr_contracts.yml"
    state_path = tmp_path / "data/broker/ibkr_state.json"
    _write_proposal(proposal_path)
    _write_ibkr_contracts(contracts_path)

    result = execute_order_proposal(
        proposal_path=str(proposal_path),
        broker="ibkr",
        dry_run=False,
        ibkr_contracts_path=str(contracts_path),
        ibkr_state_path=str(state_path),
        ibkr_limit_buffer_pct=0.0,
    )

    assert result["broker"] == "ibkr"
    assert result["dry_run"] is False
    assert result["what_if"] is False
    assert result["submitted_count"] == 2
    assert result["submitted"][0]["symbol"] == "CSPX.L"
    assert result["submitted"][0]["quantity"] == 18.0
    assert result["submitted"][0]["trade_currency"] == "USD"
    assert result["submitted"][0]["fx_rate"] == 1.2
    assert result["submitted"][1]["symbol"] == "XEON.DE"
    assert result["submitted"][1]["quantity"] == 20.0
    assert state_path.exists()


def test_execute_order_proposal_ibkr_what_if(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _install_fake_ib_insync(monkeypatch)
    proposal_path = tmp_path / "orders/proposed_2026-03.json"
    contracts_path = tmp_path / "config/ibkr_contracts.yml"
    state_path = tmp_path / "data/broker/ibkr_state.json"
    _write_proposal(proposal_path)
    _write_ibkr_contracts(contracts_path)

    result = execute_order_proposal(
        proposal_path=str(proposal_path),
        broker="ibkr",
        dry_run=False,
        ibkr_contracts_path=str(contracts_path),
        ibkr_state_path=str(state_path),
        ibkr_what_if=True,
        ibkr_limit_buffer_pct=0.0,
    )

    assert result["what_if"] is True
    assert result["submitted_count"] == 2
    assert result["submitted"][0]["status"] == "what_if"
    assert result["submitted"][0]["what_if"]["commission"] == "1.25"


def test_ibkr_get_order_reconciles_live_status(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _install_fake_ib_insync(monkeypatch)
    proposal_path = tmp_path / "orders/proposed_2026-03.json"
    contracts_path = tmp_path / "config/ibkr_contracts.yml"
    state_path = tmp_path / "data/broker/ibkr_state.json"
    _write_proposal(proposal_path)
    _write_ibkr_contracts(contracts_path)

    submit_result = execute_order_proposal(
        proposal_path=str(proposal_path),
        broker="ibkr",
        dry_run=False,
        ibkr_contracts_path=str(contracts_path),
        ibkr_state_path=str(state_path),
        ibkr_limit_buffer_pct=0.0,
    )

    order_id = submit_result["submitted"][0]["order_id"]
    status = get_broker_order_status(
        order_id=order_id,
        broker="ibkr",
        ibkr_state_path=str(state_path),
    )

    assert status["order_id"] == order_id
    assert status["status"] == "Submitted"
    assert status["client_order_id"].startswith("2026-03:sp500_acc:")


def test_ibkr_cancel_order_updates_local_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _install_fake_ib_insync(monkeypatch)
    proposal_path = tmp_path / "orders/proposed_2026-03.json"
    contracts_path = tmp_path / "config/ibkr_contracts.yml"
    state_path = tmp_path / "data/broker/ibkr_state.json"
    _write_proposal(proposal_path)
    _write_ibkr_contracts(contracts_path)

    submit_result = execute_order_proposal(
        proposal_path=str(proposal_path),
        broker="ibkr",
        dry_run=False,
        ibkr_contracts_path=str(contracts_path),
        ibkr_state_path=str(state_path),
        ibkr_limit_buffer_pct=0.0,
    )

    broker = IbkrBrokerClient(state_path=str(state_path))
    canceled = broker.cancel_order(submit_result["submitted"][0]["order_id"])

    assert canceled.status == "Cancelled"
    refreshed = broker.get_order(submit_result["submitted"][0]["order_id"])
    assert refreshed.status == "Cancelled"


def test_sync_broker_orders_reads_ids_from_state(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    _install_fake_ib_insync(monkeypatch)
    proposal_path = tmp_path / "orders/proposed_2026-03.json"
    contracts_path = tmp_path / "config/ibkr_contracts.yml"
    state_path = tmp_path / "data/broker/ibkr_state.json"
    _write_proposal(proposal_path)
    _write_ibkr_contracts(contracts_path)

    execute_order_proposal(
        proposal_path=str(proposal_path),
        broker="ibkr",
        dry_run=False,
        ibkr_contracts_path=str(contracts_path),
        ibkr_state_path=str(state_path),
        ibkr_limit_buffer_pct=0.0,
    )

    result = sync_broker_orders(
        broker="ibkr",
        ibkr_state_path=str(state_path),
    )

    assert result["refreshed_count"] == 2
    assert result["failed_count"] == 0
