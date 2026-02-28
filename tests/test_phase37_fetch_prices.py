from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pytest
import yaml

from wealth_agents.fetch_prices import fetch_prices_for_policy
from wealth_agents.market_prices import write_price_cache


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
        "policy_hash": "phase37-fetch-test-hash",
        "policy": {
            "instruments": {
                "bucket_a": instruments,
            }
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_ibkr_contracts(path: Path, tickers: list[str]) -> None:
    contracts = {}
    for ticker in tickers:
        contracts[ticker] = {
            "symbol": ticker.split(".")[0],
            "secType": "STK",
            "exchange": "SMART",
        }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"contracts": contracts}, sort_keys=False), encoding="utf-8")


def test_fetch_prices_partial_success_with_empty_ticker(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    _write_policy(policy_path, ["AAA.DE", "BBB.DE"])

    def fake_sync(cache_path: Path, ticker: str, start, end, max_retries: int = 3):
        if ticker == "AAA.DE":
            return {
                "ticker": ticker,
                "rows_total": 3,
                "rows_appended": 3,
                "downloaded_rows": 3,
                "series": {},
                "cache_path": str(cache_path),
            }
        raise RuntimeError(
            "Received empty price data for ticker 'BBB.DE' between 2024-02-01 and 2024-02-29. "
            "Verify ticker symbol/provider and try again."
        )

    monkeypatch.setattr("wealth_agents.fetch_prices.sync_yahoo_price_cache", fake_sync)

    summary = fetch_prices_for_policy(
        start="2024-02-01",
        end="2024-02-29",
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
    )

    assert summary["tickers_succeeded"] == 1
    assert summary["tickers_skipped_empty"] == 1
    assert summary["tickers_failed"] == 0
    assert summary["total_rows_downloaded"] == 3

    by_ticker = {row["ticker"]: row for row in summary["tickers"]}
    assert by_ticker["AAA.DE"]["status"] == "succeeded"
    assert by_ticker["BBB.DE"]["status"] == "skipped_empty"
    assert by_ticker["BBB.DE"]["had_existing_cache"] is False


def test_fetch_prices_empty_with_existing_cache_is_nonfatal(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    _write_policy(policy_path, ["BBB.DE"])

    cache_path = tmp_path / "data/prices/yahoo/BBB.DE.csv"
    write_price_cache(
        cache_path,
        {
            date(2025, 1, 30): 101.0,
        },
    )

    def fake_sync(cache_path: Path, ticker: str, start, end, max_retries: int = 3):
        raise RuntimeError(
            "Received empty adjusted-close data for ticker 'BBB.DE' between 2024-02-01 and 2024-12-31. "
            "Verify ticker symbol/provider and try again."
        )

    monkeypatch.setattr("wealth_agents.fetch_prices.sync_yahoo_price_cache", fake_sync)

    summary = fetch_prices_for_policy(
        start="2024-02-01",
        end="2024-12-31",
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
    )

    assert summary["tickers_succeeded"] == 0
    assert summary["tickers_skipped_empty"] == 1
    assert summary["tickers_failed"] == 0
    assert summary["total_rows_downloaded"] == 0
    assert summary["tickers"][0]["rows_total"] == 1
    assert summary["tickers"][0]["had_existing_cache"] is True


def test_fetch_prices_fails_when_all_empty_and_no_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    _write_policy(policy_path, ["AAA.DE", "BBB.DE"])

    def fake_sync(cache_path: Path, ticker: str, start, end, max_retries: int = 3):
        raise RuntimeError(
            f"Received empty price data for ticker '{ticker}' between 2024-02-01 and 2024-02-29. "
            "Verify ticker symbol/provider and try again."
        )

    monkeypatch.setattr("wealth_agents.fetch_prices.sync_yahoo_price_cache", fake_sync)

    with pytest.raises(RuntimeError, match="no ticker produced data and no usable existing cache"):
        fetch_prices_for_policy(
            start="2024-02-01",
            end="2024-02-29",
            policy_path=str(policy_path),
            prices_dir=str(tmp_path / "data/prices"),
        )


def test_fetch_prices_all_failed_but_existing_cache_succeeds(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    _write_policy(policy_path, ["AAA.DE", "BBB.DE"])

    write_price_cache(
        tmp_path / "data/prices/yahoo/BBB.DE.csv",
        {
            date(2025, 1, 31): 100.0,
        },
    )

    def fake_sync(cache_path: Path, ticker: str, start, end, max_retries: int = 3):
        raise RuntimeError(f"Network error for {ticker}")

    monkeypatch.setattr("wealth_agents.fetch_prices.sync_yahoo_price_cache", fake_sync)

    summary = fetch_prices_for_policy(
        start="2024-02-01",
        end="2024-02-29",
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
    )

    assert summary["tickers_succeeded"] == 0
    assert summary["tickers_failed"] == 2
    assert summary["tickers_skipped_empty"] == 0
    by_ticker = {row["ticker"]: row for row in summary["tickers"]}
    assert by_ticker["BBB.DE"]["had_existing_cache"] is True


def test_fetch_prices_prefer_ibkr_uses_ibkr_when_available(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    contracts_path = tmp_path / "config/ibkr_contracts.yml"
    _write_policy(policy_path, ["AAA.DE"])
    _write_ibkr_contracts(contracts_path, ["AAA.DE"])

    called = {"ibkr": 0, "yahoo": 0}

    def fake_ibkr(cache_path: Path, ticker: str, contract_spec, start, end, **kwargs):
        called["ibkr"] += 1
        return {
            "ticker": ticker,
            "rows_total": 5,
            "rows_appended": 5,
            "downloaded_rows": 5,
            "series": {},
            "cache_path": str(cache_path),
        }

    def fake_yahoo(cache_path: Path, ticker: str, start, end, max_retries: int = 3):
        called["yahoo"] += 1
        return {
            "ticker": ticker,
            "rows_total": 1,
            "rows_appended": 1,
            "downloaded_rows": 1,
            "series": {},
            "cache_path": str(cache_path),
        }

    monkeypatch.setattr("wealth_agents.fetch_prices.sync_ibkr_price_cache", fake_ibkr)
    monkeypatch.setattr("wealth_agents.fetch_prices.sync_yahoo_price_cache", fake_yahoo)

    summary = fetch_prices_for_policy(
        start="2025-01-01",
        end="2025-01-31",
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
        prefer_source="ibkr",
        ibkr_contracts_path=str(contracts_path),
    )

    assert summary["tickers_succeeded"] == 1
    assert called["ibkr"] == 1
    assert called["yahoo"] == 0
    row = summary["tickers"][0]
    assert row["source_used"] == "ibkr"
    assert row["sources_attempted"] == ["ibkr"]
    assert row["fallback_used"] is False


def test_fetch_prices_prefer_ibkr_falls_back_to_yahoo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    contracts_path = tmp_path / "config/ibkr_contracts.yml"
    _write_policy(policy_path, ["AAA.DE"])
    _write_ibkr_contracts(contracts_path, ["AAA.DE"])

    def fake_ibkr(cache_path: Path, ticker: str, contract_spec, start, end, **kwargs):
        raise RuntimeError("IBKR connection failed")

    def fake_yahoo(cache_path: Path, ticker: str, start, end, max_retries: int = 3):
        return {
            "ticker": ticker,
            "rows_total": 2,
            "rows_appended": 2,
            "downloaded_rows": 2,
            "series": {},
            "cache_path": str(cache_path),
        }

    monkeypatch.setattr("wealth_agents.fetch_prices.sync_ibkr_price_cache", fake_ibkr)
    monkeypatch.setattr("wealth_agents.fetch_prices.sync_yahoo_price_cache", fake_yahoo)

    summary = fetch_prices_for_policy(
        start="2025-01-01",
        end="2025-01-31",
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
        prefer_source="ibkr",
        ibkr_contracts_path=str(contracts_path),
    )

    assert summary["tickers_succeeded"] == 1
    row = summary["tickers"][0]
    assert row["source_used"] == "yahoo"
    assert row["sources_attempted"] == ["ibkr", "yahoo"]
    assert row["fallback_used"] is True


def test_fetch_prices_prefer_ibkr_without_mapping_can_fallback(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    _write_policy(policy_path, ["AAA.DE"])

    def fake_yahoo(cache_path: Path, ticker: str, start, end, max_retries: int = 3):
        return {
            "ticker": ticker,
            "rows_total": 4,
            "rows_appended": 4,
            "downloaded_rows": 4,
            "series": {},
            "cache_path": str(cache_path),
        }

    monkeypatch.setattr("wealth_agents.fetch_prices.sync_yahoo_price_cache", fake_yahoo)

    summary = fetch_prices_for_policy(
        start="2025-01-01",
        end="2025-01-31",
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
        prefer_source="ibkr",
    )

    assert summary["tickers_succeeded"] == 1
    row = summary["tickers"][0]
    assert row["source_used"] == "yahoo"
    assert row["sources_attempted"] == ["ibkr", "yahoo"]


def test_fetch_prices_strict_quality_gate_can_trigger_yahoo_fallback(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    policy_path = tmp_path / "data/policy/policy.yml"
    contracts_path = tmp_path / "config/ibkr_contracts.yml"
    _write_policy(policy_path, ["AAA.DE"])
    _write_ibkr_contracts(contracts_path, ["AAA.DE"])

    start_dt = date(2025, 1, 1)
    ibkr_series = {start_dt + timedelta(days=i): 120.0 for i in range(7)}
    yahoo_series = {start_dt + timedelta(days=i): 100.0 for i in range(7)}

    def fake_ibkr(cache_path: Path, ticker: str, contract_spec, start, end, **kwargs):
        return {
            "ticker": ticker,
            "rows_total": len(ibkr_series),
            "rows_appended": len(ibkr_series),
            "downloaded_rows": len(ibkr_series),
            "series": ibkr_series,
            "cache_path": str(cache_path),
        }

    def fake_yahoo_sync(cache_path: Path, ticker: str, start, end, max_retries: int = 3):
        return {
            "ticker": ticker,
            "rows_total": 3,
            "rows_appended": 3,
            "downloaded_rows": 3,
            "series": yahoo_series,
            "cache_path": str(cache_path),
        }

    def fake_yahoo_reference(ticker: str, start, end, max_retries: int = 2):
        return yahoo_series

    monkeypatch.setattr("wealth_agents.fetch_prices.sync_ibkr_price_cache", fake_ibkr)
    monkeypatch.setattr("wealth_agents.fetch_prices.sync_yahoo_price_cache", fake_yahoo_sync)
    monkeypatch.setattr("wealth_agents.fetch_prices.fetch_yahoo_adj_close", fake_yahoo_reference)

    summary = fetch_prices_for_policy(
        start="2025-01-01",
        end="2025-01-31",
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
        prefer_source="ibkr",
        ibkr_contracts_path=str(contracts_path),
        price_quality_gate_profile="strict",
    )

    row = summary["tickers"][0]
    assert row["source_used"] == "yahoo"
    assert row["sources_attempted"] == ["ibkr", "yahoo"]
    assert row["fallback_used"] is True
    assert summary["price_quality_gate_evaluated"] == 1
    assert summary["price_quality_gate_failed"] == 1


def test_fetch_prices_standard_quality_gate_records_failure_without_blocking(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    policy_path = tmp_path / "data/policy/policy.yml"
    contracts_path = tmp_path / "config/ibkr_contracts.yml"
    _write_policy(policy_path, ["AAA.DE"])
    _write_ibkr_contracts(contracts_path, ["AAA.DE"])

    start_dt = date(2025, 1, 1)
    ibkr_series = {start_dt + timedelta(days=i): 120.0 for i in range(7)}
    yahoo_series = {start_dt + timedelta(days=i): 100.0 for i in range(7)}

    def fake_ibkr(cache_path: Path, ticker: str, contract_spec, start, end, **kwargs):
        return {
            "ticker": ticker,
            "rows_total": len(ibkr_series),
            "rows_appended": len(ibkr_series),
            "downloaded_rows": len(ibkr_series),
            "series": ibkr_series,
            "cache_path": str(cache_path),
        }

    def fake_yahoo_reference(ticker: str, start, end, max_retries: int = 2):
        return yahoo_series

    monkeypatch.setattr("wealth_agents.fetch_prices.sync_ibkr_price_cache", fake_ibkr)
    monkeypatch.setattr("wealth_agents.fetch_prices.fetch_yahoo_adj_close", fake_yahoo_reference)

    summary = fetch_prices_for_policy(
        start="2025-01-01",
        end="2025-01-31",
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
        prefer_source="ibkr",
        ibkr_contracts_path=str(contracts_path),
        price_quality_gate_profile="standard",
    )

    row = summary["tickers"][0]
    gate = row["price_quality_gate"]
    assert row["source_used"] == "ibkr"
    assert gate["profile"] == "standard"
    assert gate["available"] is True
    assert gate["passed"] is False
    assert summary["price_quality_gate_evaluated"] == 1
    assert summary["price_quality_gate_failed"] == 1
