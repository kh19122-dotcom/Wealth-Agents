from __future__ import annotations

from datetime import date
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
