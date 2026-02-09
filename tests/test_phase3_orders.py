import json
from pathlib import Path

import yaml

from wealth_agents.orders import propose_monthly_orders


def _write_policy(path: Path) -> None:
    payload = {
        "policy_version": "2026-02-08",
        "created_at": "2026-02-08T21:15:14Z",
        "policy_hash": "phase3-smoke-hash",
        "selected_candidate": "balanced",
        "inputs_snapshot": {
            "base_currency": "EUR",
        },
        "policy": {
            "target_allocation": [
                {"bucket": "global_equity", "pct": 60},
                {"bucket": "bonds_cashlike", "pct": 35},
                {"bucket": "optional_gold", "pct": 5},
            ],
            "instruments": {
                "global_equity": [
                    {
                        "id": "sp500_acc",
                        "isin": "IE00B5BMR087",
                        "name": "iShares Core S&P 500 UCITS ETF (Acc)",
                        "weight_within_bucket": 0.70,
                    },
                    {
                        "id": "ex_us_equity",
                        "isin": "TBD_EXUS",
                        "name": "Ex-US Equity placeholder",
                        "weight_within_bucket": 0.30,
                    },
                ],
                "bonds_cashlike": [
                    {
                        "id": "xeon",
                        "isin": "LU0290358497",
                        "name": "Xtrackers II EUR Overnight Rate Swap UCITS ETF (XEON)",
                        "weight_within_bucket": 1.0,
                    }
                ],
                "optional_gold": [
                    {
                        "id": "xetra_gold",
                        "isin": "DE000A0S9GB0",
                        "name": "Xetra-Gold",
                        "weight_within_bucket": 1.0,
                    }
                ],
            },
            "contribution_schedule": {
                "type": "lump_sum_split",
                "months": 12,
                "planned_installment_eur": 2500.0,
            },
            "guardrails": {
                "min_trade_eur": 50,
            },
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def test_propose_monthly_orders_smoke_deterministic_allocation(tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    _write_policy(policy_path)

    orders_path, report_path, payload = propose_monthly_orders(
        month="2026-03",
        amount_eur=2500,
        policy_path=str(policy_path),
        orders_dir=str(tmp_path / "orders"),
        reports_dir=str(tmp_path / "reports"),
    )

    assert orders_path.exists()
    assert report_path.exists()

    persisted = json.loads(orders_path.read_text(encoding="utf-8"))
    assert persisted == payload
    assert persisted["month"] == "2026-03"
    assert persisted["currency"] == "EUR"
    assert persisted["budget_eur"] == 2500
    assert persisted["policy_hash"] == "phase3-smoke-hash"
    assert persisted["assumptions"]["buy_only"] is True
    assert persisted["assumptions"]["allow_sells"] is False
    assert "rounding_method" in persisted["assumptions"]

    assert sum(order["amount_eur"] for order in persisted["orders"]) == 2500
    assert all(order["side"] == "BUY" for order in persisted["orders"])

    amounts_by_isin = {order["isin"]: order["amount_eur"] for order in persisted["orders"]}
    assert amounts_by_isin["IE00B5BMR087"] == 1050
    assert amounts_by_isin["TBD_EXUS"] == 450
    assert amounts_by_isin["LU0290358497"] == 875
    assert amounts_by_isin["DE000A0S9GB0"] == 125
