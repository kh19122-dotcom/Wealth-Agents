from __future__ import annotations

from datetime import date
from pathlib import Path
import runpy


MODULE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "scripts/prepare_proxy_backtest.py")
)


def test_proxy_prices_root_is_namespaced_once():
    resolve_proxy_prices_root = MODULE["_resolve_proxy_prices_root"]

    assert resolve_proxy_prices_root(Path("/tmp/wa_prices_proxy")) == Path(
        "/tmp/wa_prices_proxy/proxy_stooq"
    )
    assert resolve_proxy_prices_root(Path("/tmp/wa_prices_proxy/proxy_stooq")) == Path(
        "/tmp/wa_prices_proxy/proxy_stooq"
    )


def test_proxy_policy_uses_dynamic_metadata_without_forcing_eur_currency():
    build_proxy_policy_doc = MODULE["_build_proxy_policy_doc"]

    policy_doc_a = build_proxy_policy_doc("2026-03-07T11:00:00Z")
    policy_doc_b = build_proxy_policy_doc("2026-03-08T11:00:00Z")

    instrument_data = policy_doc_a["policy"]["instruments"]["global_equity"][0]["data"]

    assert policy_doc_a["created_at"] == "2026-03-07T11:00:00Z"
    assert policy_doc_a["policy_hash"] == policy_doc_b["policy_hash"]
    assert "currency" not in instrument_data
    assert policy_doc_a["policy"]["rebalance_rules"]["buy_only"] is True
    assert (
        policy_doc_a["policy"]["notes"]["price_cache_namespace"] == "proxy_stooq"
    )


def test_simulation_examples_follow_requested_full_month_window():
    build_simulation_examples = MODULE["_build_simulation_examples"]

    examples = build_simulation_examples(
        start=date(2024, 1, 1),
        end=date(2026, 2, 28),
    )

    assert [(row.label, row.start_month, row.end_month, row.clipped) for row in examples] == [
        ("2y", "2024-03", "2026-02", False),
        ("5y", "2024-01", "2026-02", True),
        ("10y", "2024-01", "2026-02", True),
    ]


def test_simulation_examples_skip_partial_month_edges():
    build_simulation_examples = MODULE["_build_simulation_examples"]

    examples = build_simulation_examples(
        start=date(2024, 1, 15),
        end=date(2024, 3, 15),
    )

    assert [(row.label, row.start_month, row.end_month) for row in examples] == [
        ("2y", "2024-02", "2024-02"),
        ("5y", "2024-02", "2024-02"),
        ("10y", "2024-02", "2024-02"),
    ]


def test_simulation_examples_require_at_least_one_full_month():
    build_simulation_examples = MODULE["_build_simulation_examples"]

    assert build_simulation_examples(
        start=date(2024, 1, 15),
        end=date(2024, 1, 20),
    ) == []
