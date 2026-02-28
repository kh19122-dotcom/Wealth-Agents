from __future__ import annotations

from pathlib import Path
import sys
import types

import yaml

from wealth_agents.ibkr_preflight import run_ibkr_preflight


def _write_policy(path: Path, tickers: list[str]) -> None:
    instruments = []
    for idx, ticker in enumerate(tickers, start=1):
        instruments.append(
            {
                "id": f"inst_{idx}",
                "isin": f"TEST_ISIN_{idx}",
                "name": f"Instrument {idx}",
                "weight_within_bucket": 1.0,
                "data": {"provider": "yahoo", "ticker": ticker},
            }
        )

    payload = {
        "policy_hash": "ibkr-preflight-test-hash",
        "policy": {
            "instruments": {
                "bucket_a": instruments,
            }
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_ibkr_contracts(path: Path, mapping: dict[str, dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"contracts": mapping}, sort_keys=False), encoding="utf-8")


def _install_fake_ib_insync(monkeypatch, *, connect_error: str | None = None):
    class FakeContract:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)
            if hasattr(self, "conId"):
                setattr(self, "conId", int(getattr(self, "conId")))

    class FakeIB:
        def connect(self, host: str, port: int, clientId: int, timeout: float, readonly: bool):
            if connect_error:
                raise RuntimeError(connect_error)
            return True

        def disconnect(self):
            return None

        def qualifyContracts(self, contract):
            if not hasattr(contract, "conId"):
                contract.conId = 101001
            if not hasattr(contract, "symbol"):
                contract.symbol = "UNKNOWN"
            if not hasattr(contract, "exchange"):
                contract.exchange = "SMART"
            if not hasattr(contract, "primaryExchange"):
                contract.primaryExchange = "SMART"
            if not hasattr(contract, "currency"):
                contract.currency = "USD"
            return [contract]

        def reqHistoricalData(self, *args, **kwargs):
            return [
                types.SimpleNamespace(date="20250130", close=101.25),
                types.SimpleNamespace(date="20250131", close=102.50),
            ]

    fake_module = types.SimpleNamespace(IB=FakeIB, Contract=FakeContract)
    monkeypatch.setitem(sys.modules, "ib_insync", fake_module)


def test_ibkr_preflight_passes_with_valid_mapping(monkeypatch, tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    contracts_path = tmp_path / "config/ibkr_contracts.yml"
    _write_policy(policy_path, ["AAA.DE", "BBB.DE"])
    _write_ibkr_contracts(
        contracts_path,
        {
            "AAA.DE": {"symbol": "AAA", "secType": "STK", "exchange": "SMART"},
            "BBB.DE": {"symbol": "BBB", "secType": "STK", "exchange": "SMART"},
        },
    )
    _install_fake_ib_insync(monkeypatch)

    summary = run_ibkr_preflight(
        policy_path=str(policy_path),
        ibkr_contracts_path=str(contracts_path),
        report_dir=str(tmp_path / "reports"),
    )

    assert summary["passed"] is True
    assert summary["failed_count"] == 0
    assert summary["status_counts"].get("ok") == 2
    report_path = Path(summary["report_path"])
    assert report_path.exists()
    assert "IBKR Preflight Report" in report_path.read_text(encoding="utf-8")


def test_ibkr_preflight_fails_when_contract_mapping_is_missing(monkeypatch, tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    contracts_path = tmp_path / "config/ibkr_contracts.yml"
    _write_policy(policy_path, ["AAA.DE", "BBB.DE"])
    _write_ibkr_contracts(
        contracts_path,
        {
            "AAA.DE": {"symbol": "AAA", "secType": "STK", "exchange": "SMART"},
        },
    )
    _install_fake_ib_insync(monkeypatch)

    summary = run_ibkr_preflight(
        policy_path=str(policy_path),
        ibkr_contracts_path=str(contracts_path),
        report_dir=str(tmp_path / "reports"),
    )

    assert summary["passed"] is False
    assert summary["failed_count"] == 1
    assert summary["status_counts"].get("missing_contract") == 1


def test_ibkr_preflight_reports_connection_failure(monkeypatch, tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    contracts_path = tmp_path / "config/ibkr_contracts.yml"
    _write_policy(policy_path, ["AAA.DE", "BBB.DE"])
    _write_ibkr_contracts(
        contracts_path,
        {
            "AAA.DE": {"symbol": "AAA", "secType": "STK", "exchange": "SMART"},
            "BBB.DE": {"symbol": "BBB", "secType": "STK", "exchange": "SMART"},
        },
    )
    _install_fake_ib_insync(monkeypatch, connect_error="TWS is not running")

    summary = run_ibkr_preflight(
        policy_path=str(policy_path),
        ibkr_contracts_path=str(contracts_path),
        report_dir=str(tmp_path / "reports"),
    )

    assert summary["passed"] is False
    assert summary["failed_count"] == 2
    assert summary["status_counts"].get("connection_failed") == 2
