from __future__ import annotations

from datetime import date
import math
from pathlib import Path

import yaml

from wealth_agents.market_prices import month_end_prices, write_price_cache
from wealth_agents.simulation import _compute_stats, run_simulation


def _write_policy(path: Path) -> None:
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
                        "data": {"provider": "yahoo", "ticker": "AAA.DE"},
                    }
                ],
                "bucket_b": [
                    {
                        "id": "asset_b",
                        "isin": "TEST_ISIN_B",
                        "name": "Asset B",
                        "weight_within_bucket": 1.0,
                        "data": {"provider": "yahoo", "ticker": "BBB.DE"},
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
    snapshots = payload["snapshots"]
    assert len(snapshots) == 6

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
