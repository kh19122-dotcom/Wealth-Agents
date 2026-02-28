import json
from pathlib import Path

import pytest
import yaml

from wealth_agents.broker import BrokerOrderRequest, MockBrokerClient
from wealth_agents.execution import execute_order_proposal


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
                "isin": "IE00B5BMR087",
                "name": "iShares Core S&P 500 UCITS ETF (Acc)",
                "bucket": "global_equity",
                "amount_eur": 1500,
            },
            {
                "side": "BUY",
                "instrument_id": "xeon",
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
