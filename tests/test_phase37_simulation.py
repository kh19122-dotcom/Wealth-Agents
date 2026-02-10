from __future__ import annotations

from datetime import date
import math
from pathlib import Path

import pytest
import yaml

from wealth_agents.market_prices import month_end_prices, write_price_cache
from wealth_agents.simulation import _compute_stats, resolve_fx_ticker, run_simulation


def _write_policy(
    path: Path,
    ticker_a: str = "AAA.DE",
    ticker_b: str = "BBB.DE",
    currency_a: str | None = None,
    currency_b: str | None = None,
) -> None:
    data_a = {"provider": "yahoo", "ticker": ticker_a}
    data_b = {"provider": "yahoo", "ticker": ticker_b}
    if currency_a:
        data_a["currency"] = currency_a
    if currency_b:
        data_b["currency"] = currency_b

    payload = {
        "policy_version": "2026-02-09",
        "created_at": "2026-02-09T00:00:00Z",
        "policy_hash": "phase37-test-hash",
        "inputs_snapshot": {"base_currency": "EUR"},
        "policy": {
            "target_allocation": [
                {"bucket": "bucket_a", "pct": 50},
                {"bucket": "bucket_b", "pct": 50},
            ],
            "instruments": {
                "bucket_a": [
                    {
                        "id": "asset_a",
                        "isin": "TEST_ISIN_A",
                        "name": "Asset A",
                        "weight_within_bucket": 1.0,
                        "data": data_a,
                    }
                ],
                "bucket_b": [
                    {
                        "id": "asset_b",
                        "isin": "TEST_ISIN_B",
                        "name": "Asset B",
                        "weight_within_bucket": 1.0,
                        "data": data_b,
                    }
                ],
            },
            "rebalance_rules": {"frequency": "quarterly", "band_pct": 5.0, "buy_only": False},
            "guardrails": {"min_trade_eur": 1},
            "contribution_schedule": {"planned_installment_eur": 100.0},
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_month_end_sampling_is_deterministic_for_missing_non_trading_days():
    daily = {
        date(2024, 1, 30): 10.0,
        date(2024, 1, 31): 11.0,
        date(2024, 2, 27): 12.0,
        date(2024, 2, 29): 13.0,
        date(2024, 3, 27): 14.0,
        date(2024, 3, 28): 15.0,
    }

    sampled = month_end_prices(daily, start_month="2024-01", end_month="2024-03")

    assert sampled == {
        "2024-01": 11.0,
        "2024-02": 13.0,
        "2024-03": 15.0,
    }


def test_simulation_smoke_with_monotonic_synthetic_prices(tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    _write_policy(policy_path)

    prices_root = tmp_path / "data/prices/yahoo"
    write_price_cache(
        prices_root / "AAA.DE.csv",
        {
            date(2024, 1, 31): 100.0,
            date(2024, 2, 29): 102.0,
            date(2024, 3, 28): 104.0,
            date(2024, 4, 30): 106.0,
            date(2024, 5, 31): 108.0,
            date(2024, 6, 28): 110.0,
        },
    )
    write_price_cache(
        prices_root / "BBB.DE.csv",
        {
            date(2024, 1, 31): 200.0,
            date(2024, 2, 29): 202.0,
            date(2024, 3, 28): 204.0,
            date(2024, 4, 30): 206.0,
            date(2024, 5, 31): 208.0,
            date(2024, 6, 28): 210.0,
        },
    )

    sim_path, report_path, payload = run_simulation(
        start="2024-01",
        end="2024-06",
        monthly=100.0,
        initial=0.0,
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
        sim_dir=str(tmp_path / "sim"),
        reports_dir=str(tmp_path / "reports"),
    )

    assert sim_path.exists()
    assert report_path.exists()
    report_text = report_path.read_text(encoding="utf-8")
    assert "CAGR (TWR, cashflow-adjusted)" in report_text
    assert "Annualized volatility (TWR)" in report_text
    assert "Max drawdown (TWR)" in report_text
    assert "## Events" in report_text
    snapshots = payload["snapshots"]
    assert len(snapshots) == 6

    for snapshot in snapshots:
        events = snapshot["events"]
        assert [event["type"] for event in events] == ["contribution", "rebalance", "orders"]
        assert events[0]["amount"] == 100.0
        assert events[1]["reason"] == "quarterly"
        assert events[2]["count"] == len(events[2]["orders"])

    holdings_a = [row["holdings"]["AAA.DE"] for row in snapshots]
    holdings_b = [row["holdings"]["BBB.DE"] for row in snapshots]
    assert all(holdings_a[idx + 1] >= holdings_a[idx] for idx in range(len(holdings_a) - 1))
    assert all(holdings_b[idx + 1] >= holdings_b[idx] for idx in range(len(holdings_b) - 1))

    total_values = [row["total_value"] for row in snapshots]
    assert all(total_values[idx + 1] + 1e-8 >= total_values[idx] for idx in range(len(total_values) - 1))

    stats = payload["stats"]
    assert stats["cagr"] is not None
    assert 0.0 < stats["cagr"] < 1.0
    assert stats["max_drawdown"] <= 0.0

    expected = _twr_stats_from_values(total_values=total_values, monthly_contribution=100.0)
    assert math.isclose(float(stats["max_drawdown"]), float(expected["max_drawdown"]), rel_tol=1e-9, abs_tol=1e-9)
    assert math.isclose(float(stats["annualized_volatility"]), float(expected["annualized_volatility"]), rel_tol=1e-9, abs_tol=1e-9)


def test_twr_drawdown_uses_growth_index_not_raw_total_value():
    total_values = [100.0, 210.0, 305.0, 420.0]
    # Total value is monotonic increasing, but TWR can still have negative month return after adjusting CF.
    assert all(total_values[idx + 1] >= total_values[idx] for idx in range(len(total_values) - 1))

    stats = _compute_stats(total_values=total_values, monthly_contribution=100.0)
    expected = _twr_stats_from_values(total_values=total_values, monthly_contribution=100.0)

    assert stats["cagr"] is not None
    assert stats["max_drawdown"] < 0.0
    assert math.isclose(float(stats["max_drawdown"]), float(expected["max_drawdown"]), rel_tol=1e-9, abs_tol=1e-9)


def test_resolve_fx_ticker_supports_direct_and_inverse_modes():
    assert resolve_fx_ticker("GBP") == ("GBPEUR=X", False)
    assert resolve_fx_ticker("USD", ticker_exists=lambda ticker: ticker == "USDEUR=X") == ("USDEUR=X", False)
    assert resolve_fx_ticker("CHF", ticker_exists=lambda ticker: ticker == "EURCHF=X") == ("EURCHF=X", True)
    with pytest.raises(ValueError, match="Unable to resolve Yahoo FX ticker"):
        resolve_fx_ticker("CHF", ticker_exists=lambda ticker: False)


def test_simulation_applies_fx_conversion_for_gbp_ticker(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    policy_path = tmp_path / "data/policy/policy.yml"
    _write_policy(policy_path, ticker_a="AAA.L", ticker_b="BBB.DE")

    prices_root = tmp_path / "data/prices/yahoo"
    write_price_cache(
        prices_root / "AAA.L.csv",
        {
            date(2024, 1, 31): 100.0,
            date(2024, 2, 29): 100.0,
            date(2024, 3, 29): 100.0,
        },
    )
    write_price_cache(
        prices_root / "BBB.DE.csv",
        {
            date(2024, 1, 31): 100.0,
            date(2024, 2, 29): 100.0,
            date(2024, 3, 29): 100.0,
        },
    )
    # Pre-populate FX cache to avoid network and guarantee deterministic month-end rates.
    write_price_cache(
        tmp_path / "data/prices/yahoo_fx/GBPEUR=X.csv",
        {
            date(2024, 1, 1): 1.0,
            date(2024, 1, 31): 1.0,
            date(2024, 2, 29): 1.2,
            date(2024, 3, 31): 1.4,
        },
    )

    monkeypatch.setattr(
        "wealth_agents.simulation.get_yahoo_ticker_currency",
        lambda ticker, max_retries=2: "GBP" if ticker == "AAA.L" else "EUR",
    )
    _, _, payload_fx = run_simulation(
        start="2024-01",
        end="2024-03",
        monthly=100.0,
        initial=0.0,
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
        sim_dir=str(tmp_path / "sim_fx"),
        reports_dir=str(tmp_path / "reports_fx"),
    )

    second_snapshot = payload_fx["snapshots"][1]
    assert second_snapshot["prices_local"]["AAA.L"] == 100.0
    assert second_snapshot["prices_eur"]["AAA.L"] == 120.0
    assert second_snapshot["fx_rates"]["GBP"] == 1.2
    assert payload_fx["fx"]["conversions"] == [{"currency": "GBP", "fx_ticker": "GBPEUR=X", "invert": False}]

    monkeypatch.setattr(
        "wealth_agents.simulation.get_yahoo_ticker_currency",
        lambda ticker, max_retries=2: "EUR",
    )
    _, _, payload_no_fx = run_simulation(
        start="2024-01",
        end="2024-03",
        monthly=100.0,
        initial=0.0,
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
        sim_dir=str(tmp_path / "sim_no_fx"),
        reports_dir=str(tmp_path / "reports_no_fx"),
    )

    assert payload_fx["snapshots"][-1]["total_value"] > payload_no_fx["snapshots"][-1]["total_value"]


def test_simulation_uses_policy_currency_override_over_metadata(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    policy_path = tmp_path / "data/policy/policy.yml"
    _write_policy(policy_path, ticker_a="AAA.L", ticker_b="BBB.DE", currency_a="GBP")

    prices_root = tmp_path / "data/prices/yahoo"
    write_price_cache(
        prices_root / "AAA.L.csv",
        {
            date(2024, 1, 31): 100.0,
            date(2024, 2, 29): 100.0,
            date(2024, 3, 29): 100.0,
        },
    )
    write_price_cache(
        prices_root / "BBB.DE.csv",
        {
            date(2024, 1, 31): 100.0,
            date(2024, 2, 29): 100.0,
            date(2024, 3, 29): 100.0,
        },
    )
    write_price_cache(
        tmp_path / "data/prices/yahoo_fx/GBPEUR=X.csv",
        {
            date(2024, 1, 31): 1.1,
            date(2024, 2, 29): 1.2,
            date(2024, 3, 31): 1.3,
        },
    )
    write_price_cache(
        tmp_path / "data/prices/yahoo_fx/USDEUR=X.csv",
        {
            date(2024, 1, 31): 0.8,
            date(2024, 2, 29): 0.9,
            date(2024, 3, 31): 1.0,
        },
    )

    metadata_calls: list[str] = []

    def fake_metadata_currency(ticker: str, max_retries: int = 2) -> str:
        metadata_calls.append(ticker)
        return "USD" if ticker == "AAA.L" else "EUR"

    monkeypatch.setattr("wealth_agents.simulation.get_yahoo_ticker_currency", fake_metadata_currency)

    _, report_path, payload = run_simulation(
        start="2024-01",
        end="2024-03",
        monthly=100.0,
        initial=0.0,
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
        sim_dir=str(tmp_path / "sim_policy_fx"),
        reports_dir=str(tmp_path / "reports_policy_fx"),
    )

    assert "AAA.L" not in metadata_calls
    assert payload["snapshots"][1]["prices_local"]["AAA.L"] == 100.0
    assert payload["snapshots"][1]["prices_eur"]["AAA.L"] == 120.0
    assert payload["snapshots"][1]["fx_rates"]["GBP"] == 1.2
    assert payload["fx"]["conversions"] == [{"currency": "GBP", "fx_ticker": "GBPEUR=X", "invert": False}]
    assert payload["fx"]["currency_sources"]["GBP"] == "policy"

    report_text = report_path.read_text(encoding="utf-8")
    assert "| GBP | GBPEUR=X | false | policy |" in report_text


def test_simulation_allow_short_history_adjusts_effective_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    policy_path = tmp_path / "data/policy/policy.yml"
    _write_policy(policy_path, ticker_a="AAA.DE", ticker_b="BBB.DE")

    prices_root = tmp_path / "data/prices/yahoo"
    write_price_cache(
        prices_root / "AAA.DE.csv",
        {
            date(2024, 1, 31): 100.0,
            date(2024, 2, 29): 101.0,
            date(2024, 3, 29): 102.0,
            date(2024, 4, 30): 103.0,
            date(2024, 5, 31): 104.0,
            date(2024, 6, 28): 105.0,
        },
    )
    # BBB starts later (no Jan/Feb), which should trigger effective_start adjustment.
    write_price_cache(
        prices_root / "BBB.DE.csv",
        {
            date(2024, 3, 29): 200.0,
            date(2024, 4, 30): 201.0,
            date(2024, 5, 31): 202.0,
            date(2024, 6, 28): 203.0,
        },
    )

    monkeypatch.setattr(
        "wealth_agents.simulation.get_yahoo_ticker_currency",
        lambda ticker, max_retries=2: "EUR",
    )

    with pytest.raises(ValueError, match="missing required month-end prices"):
        run_simulation(
            start="2024-01",
            end="2024-06",
            monthly=100.0,
            initial=0.0,
            policy_path=str(policy_path),
            prices_dir=str(tmp_path / "data/prices"),
            sim_dir=str(tmp_path / "sim_strict"),
            reports_dir=str(tmp_path / "reports_strict"),
        )

    sim_path, report_path, payload = run_simulation(
        start="2024-01",
        end="2024-06",
        monthly=100.0,
        initial=0.0,
        allow_short_history=True,
        policy_path=str(policy_path),
        prices_dir=str(tmp_path / "data/prices"),
        sim_dir=str(tmp_path / "sim_adjusted"),
        reports_dir=str(tmp_path / "reports_adjusted"),
    )

    assert sim_path.name == "portfolio_2024-03_2024-06.json"
    assert report_path.name == "sim_2024-03_2024-06.md"
    assert payload["start"] == "2024-03"
    assert payload["history_window"]["adjusted"] is True
    assert payload["history_window"]["effective_start"] == "2024-03"
    assert payload["history_window"]["first_available_by_ticker"]["AAA.DE"]["first_month"] == "2024-01"
    assert payload["history_window"]["first_available_by_ticker"]["BBB.DE"]["first_month"] == "2024-03"
    assert len(payload["snapshots"]) == 4

    report_text = report_path.read_text(encoding="utf-8")
    assert "Requested window: 2024-01 to 2024-06" in report_text
    assert "Effective window: 2024-03 to 2024-06" in report_text
    assert "| BBB.DE | 2024-03-29 | 2024-03 |" in report_text


def _twr_stats_from_values(total_values: list[float], monthly_contribution: float) -> dict[str, float | None]:
    returns: list[float] = []
    for idx in range(1, len(total_values)):
        prev = float(total_values[idx - 1])
        curr = float(total_values[idx])
        if prev <= 0:
            continue
        returns.append(((curr - float(monthly_contribution)) / prev) - 1.0)

    cagr: float | None = None
    if len(returns) >= 2:
        growth_end = 1.0
        for r in returns:
            growth_end *= (1.0 + r)
        if growth_end > 0:
            cagr = growth_end ** (12.0 / len(returns)) - 1.0

    if len(returns) < 2:
        annualized_volatility = 0.0
    else:
        mean = sum(returns) / len(returns)
        variance = sum((r - mean) ** 2 for r in returns) / len(returns)
        annualized_volatility = math.sqrt(variance) * math.sqrt(12.0)

    growth = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for r in returns:
        growth *= (1.0 + r)
        if growth > peak:
            peak = growth
        max_drawdown = min(max_drawdown, (growth / peak) - 1.0)

    return {
        "cagr": cagr,
        "annualized_volatility": annualized_volatility,
        "max_drawdown": max_drawdown,
    }
