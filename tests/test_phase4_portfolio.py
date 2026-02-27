from __future__ import annotations

from datetime import date
import json
from pathlib import Path

import pytest
import yaml

from wealth_agents.market_prices import write_price_cache
from wealth_agents.portfolio import (
    generate_portfolio_drift_report,
    import_portfolio_trades,
    init_live_portfolio,
)


def _write_policy(path: Path) -> None:
    payload = {
        "policy_version": "2026-02-10",
        "created_at": "2026-02-10T00:00:00Z",
        "policy_hash": "phase4-test-hash",
        "inputs_snapshot": {"base_currency": "EUR"},
        "policy": {
            "target_allocation": [
                {"bucket": "bucket_a", "pct": 50},
                {"bucket": "bucket_b", "pct": 30},
                {"bucket": "bucket_c", "pct": 20},
            ],
            "instruments": {
                "bucket_a": [
                    {
                        "id": "asset_a",
                        "isin": "ISIN_A",
                        "name": "Asset A",
                        "weight_within_bucket": 1.0,
                        "data": {"provider": "yahoo", "ticker": "AAA.DE", "currency": "EUR"},
                    }
                ],
                "bucket_b": [
                    {
                        "id": "asset_b",
                        "isin": "ISIN_B",
                        "name": "Asset B",
                        "weight_within_bucket": 1.0,
                        "data": {"provider": "yahoo", "ticker": "BBB.DE", "currency": "EUR"},
                    }
                ],
                "bucket_c": [
                    {
                        "id": "asset_c",
                        "isin": "ISIN_C",
                        "name": "Asset C",
                        "weight_within_bucket": 1.0,
                        "data": {"provider": "yahoo", "ticker": "CCC.DE", "currency": "EUR"},
                    }
                ],
            },
            "guardrails": {"min_trade_eur": 1},
            "contribution_schedule": {"planned_installment_eur": 100},
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_phase4_portfolio_init_creates_file(tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    live_path = tmp_path / "data/portfolio/live.json"
    _write_policy(policy_path)

    out_path, payload, initialized = init_live_portfolio(
        asof="2026-02-10",
        cash_eur=250.0,
        policy_path=str(policy_path),
        live_path=str(live_path),
    )

    assert initialized is True
    assert out_path == live_path
    assert out_path.exists()
    persisted = json.loads(out_path.read_text(encoding="utf-8"))
    assert persisted == payload
    assert persisted["base_currency"] == "EUR"
    assert persisted["asof"] == "2026-02-10"
    assert persisted["cash_eur"] == 250.0
    assert persisted["trade_count"] == 0
    assert persisted["holdings"] == {
        "AAA.DE": {"shares": 0.0},
        "BBB.DE": {"shares": 0.0},
        "CCC.DE": {"shares": 0.0},
    }


def test_phase4_import_trades_updates_holdings_and_cash(tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    live_path = tmp_path / "data/portfolio/live.json"
    _write_policy(policy_path)
    init_live_portfolio(
        asof="2026-02-10",
        cash_eur=1000.0,
        policy_path=str(policy_path),
        live_path=str(live_path),
    )

    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        "date;ticker;side;shares;price;currency;fee\n"
        "2026-02-01;AAA.DE;BUY;2;100;EUR;1\n"
        "2026-02-02;AAA.DE;SELL;0.5;110;EUR;1\n",
        encoding="utf-8",
    )

    _, payload, summary = import_portfolio_trades(
        csv_path=str(csv_path),
        live_path=str(live_path),
        prices_dir=str(tmp_path / "data/prices"),
    )

    assert summary["imported_trades"] == 2
    assert payload["trade_count"] == 2
    assert payload["asof"] == "2026-02-02"
    assert payload["holdings"]["AAA.DE"]["shares"] == 1.5
    assert payload["cash_eur"] == 853.0
    assert summary["negative_cash"] is False


def test_phase4_report_computes_drift(tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    live_path = tmp_path / "data/portfolio/live.json"
    _write_policy(policy_path)

    live_payload = {
        "base_currency": "EUR",
        "asof": "2026-02-10",
        "cash_eur": 0.0,
        "holdings": {
            "AAA.DE": {"shares": 1.0},
            "BBB.DE": {"shares": 1.0},
            "CCC.DE": {"shares": 0.0},
        },
        "trade_count": 2,
        "notes": "",
    }
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text(json.dumps(live_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    prices_root = tmp_path / "data/prices/yahoo"
    write_price_cache(
        prices_root / "AAA.DE.csv",
        {
            date(2026, 2, 7): 99.0,
            date(2026, 2, 10): 100.0,
        },
    )
    write_price_cache(
        prices_root / "BBB.DE.csv",
        {
            date(2026, 2, 10): 300.0,
        },
    )
    write_price_cache(
        prices_root / "CCC.DE.csv",
        {
            date(2026, 2, 10): 50.0,
        },
    )

    report_path, summary = generate_portfolio_drift_report(
        asof="2026-02-10",
        policy_path=str(policy_path),
        live_path=str(live_path),
        prices_dir=str(tmp_path / "data/prices"),
        reports_dir=str(tmp_path / "reports"),
    )

    assert report_path.exists()
    assert summary["asof"] == "2026-02-10"
    assert summary["invested_value_eur"] == 400.0
    assert summary["total_value_eur"] == 400.0
    assert summary["biggest_drift_ticker"] == "BBB.DE"
    assert summary["biggest_drift_pct"] == 0.45

    text = report_path.read_text(encoding="utf-8")
    assert "# Portfolio Drift Report  2026-02-10" in text
    assert "Weights are computed on invested assets only (cash excluded)." in text
    assert "| BBB.DE | 1 | 300.00 | 300.00 | 30.00% | 75.00% | 75.00% | 45.00% |" in text


def test_phase4_report_uses_invested_only_weights_with_negative_cash(tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    live_path = tmp_path / "data/portfolio/live.json"
    _write_policy(policy_path)

    live_payload = {
        "base_currency": "EUR",
        "asof": "2026-02-10",
        "cash_eur": -90.0,
        "holdings": {
            "AAA.DE": {"shares": 1.0},
            "BBB.DE": {"shares": 0.0},
            "CCC.DE": {"shares": 0.0},
        },
        "trade_count": 1,
        "notes": "",
    }
    live_path.parent.mkdir(parents=True, exist_ok=True)
    live_path.write_text(json.dumps(live_payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    prices_root = tmp_path / "data/prices/yahoo"
    write_price_cache(prices_root / "AAA.DE.csv", {date(2026, 2, 10): 100.0})
    write_price_cache(prices_root / "BBB.DE.csv", {date(2026, 2, 10): 300.0})
    write_price_cache(prices_root / "CCC.DE.csv", {date(2026, 2, 10): 50.0})

    report_path, summary = generate_portfolio_drift_report(
        asof="2026-02-10",
        policy_path=str(policy_path),
        live_path=str(live_path),
        prices_dir=str(tmp_path / "data/prices"),
        reports_dir=str(tmp_path / "reports"),
    )

    assert report_path.exists()
    assert summary["invested_value_eur"] == 100.0
    assert summary["total_value_eur"] == 10.0
    assert summary["biggest_drift_ticker"] == "AAA.DE"
    assert summary["biggest_drift_pct"] == 0.5

    text = report_path.read_text(encoding="utf-8")
    assert "| AAA.DE | 1 | 100.00 | 100.00 | 50.00% | 100.00% | 1000.00% | 50.00% |" in text
    assert "| BBB.DE | 0 | 300.00 | 0.00 | 30.00% | 0.00% | 0.00% | -30.00% |" in text


def test_phase4_import_trades_no_negative_cash_guard_leaves_file_unchanged(tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    live_path = tmp_path / "data/portfolio/live.json"
    _write_policy(policy_path)
    init_live_portfolio(
        asof="2026-02-10",
        cash_eur=10.0,
        policy_path=str(policy_path),
        live_path=str(live_path),
    )

    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        "date,ticker,side,shares,price,currency,fee\n"
        "2026-02-10,AAA.DE,BUY,1,100,EUR,0\n",
        encoding="utf-8",
    )
    before = live_path.read_text(encoding="utf-8")

    with pytest.raises(ValueError, match="negative cash"):
        import_portfolio_trades(
            csv_path=str(csv_path),
            live_path=str(live_path),
            prices_dir=str(tmp_path / "data/prices"),
            no_negative_cash=True,
        )

    after = live_path.read_text(encoding="utf-8")
    assert after == before


def test_phase4_import_trades_allows_negative_cash_with_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    policy_path = tmp_path / "data/policy/policy.yml"
    live_path = tmp_path / "data/portfolio/live.json"
    _write_policy(policy_path)
    init_live_portfolio(
        asof="2026-02-10",
        cash_eur=10.0,
        policy_path=str(policy_path),
        live_path=str(live_path),
    )

    csv_path = tmp_path / "trades.csv"
    csv_path.write_text(
        "date,ticker,side,shares,price,currency,fee\n"
        "2026-02-10,AAA.DE,BUY,1,100,EUR,0\n",
        encoding="utf-8",
    )

    with caplog.at_level("WARNING", logger="wealth_agents.portfolio"):
        _, payload, summary = import_portfolio_trades(
            csv_path=str(csv_path),
            live_path=str(live_path),
            prices_dir=str(tmp_path / "data/prices"),
            no_negative_cash=False,
        )

    assert payload["cash_eur"] == -90.0
    assert summary["negative_cash"] is True
    assert any("Portfolio cash is negative after trade import" in rec.message for rec in caplog.records)
