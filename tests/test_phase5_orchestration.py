import json
from pathlib import Path

import pytest

from wealth_agents.orchestration import (
    DEFAULT_STEP_SEQUENCE,
    _run_propose_orders,
    _run_report,
    run_cycle,
)


def test_run_cycle_writes_checkpoint_and_completes_all_steps(tmp_path: Path, monkeypatch):
    counts = {
        "collect": 0,
        "append_unique": 0,
        "validate_jsonl": 0,
        "report": 0,
        "ips_draft": 0,
        "ips_finalize": 0,
        "propose_orders": 0,
        "simulate": 0,
        "execute_orders": 0,
    }

    def fake_collect_from_feeds_with_stats(config_path: str, health_meta_path: str):
        counts["collect"] += 1
        return (
            [
                {
                    "id": "record-1",
                    "source": "test",
                    "title": "title",
                    "url": "https://example.com",
                    "published_at": "2026-02-27T00:00:00Z",
                    "fetched_at": "2026-02-27T00:00:00Z",
                    "summary": "summary",
                    "tags": ["korea"],
                }
            ],
            {"status": "success", "fetched": 1},
        )

    def fake_append_unique_records(path: str, records):
        counts["append_unique"] += 1
        return len(records)

    def fake_validate_jsonl(path: str):
        counts["validate_jsonl"] += 1
        return 1

    def fake_generate_weekly_report(**kwargs):
        counts["report"] += 1
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / f"weekly_{kwargs['week']}.md"
        out.write_text("# report\n", encoding="utf-8")
        return out

    def fake_draft_policy(**kwargs):
        counts["ips_draft"] += 1
        draft_path = Path(kwargs["draft_path"])
        report_dir = Path(kwargs["report_dir"])
        draft_path.parent.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        draft_path.write_text("generated_at: test\ncandidates: {}\n", encoding="utf-8")
        report_path = report_dir / "ips_fake.md"
        report_path.write_text("# ips\n", encoding="utf-8")
        return draft_path, report_path

    def fake_finalize_policy(**kwargs):
        counts["ips_finalize"] += 1
        policy_path = Path(kwargs["policy_path"])
        policy_path.parent.mkdir(parents=True, exist_ok=True)
        policy_path.write_text(
            "policy_version: '2026-02-27'\npolicy_hash: fake-hash\npolicy:\n  target_allocation: []\n",
            encoding="utf-8",
        )
        return policy_path, "fake-hash"

    def fake_propose_monthly_orders(**kwargs):
        counts["propose_orders"] += 1
        orders_dir = Path(kwargs["orders_dir"])
        report_dir = Path(kwargs["reports_dir"])
        orders_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        orders_path = orders_dir / "proposed_2026-03.json"
        report_path = report_dir / "orders_2026-03.md"
        payload = {
            "month": "2026-03",
            "currency": "EUR",
            "budget_eur": 2500,
            "policy_hash": "fake-hash",
            "orders": [
                {
                    "side": "BUY",
                    "instrument_id": "sp500_acc",
                    "isin": "IE00B5BMR087",
                    "name": "ETF",
                    "bucket": "global_equity",
                    "amount_eur": 2500,
                }
            ],
        }
        orders_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_path.write_text("# orders\n", encoding="utf-8")
        return orders_path, report_path, payload

    def fake_run_simulation(**kwargs):
        counts["simulate"] += 1
        sim_dir = Path(kwargs["sim_dir"])
        report_dir = Path(kwargs["reports_dir"])
        sim_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        sim_path = sim_dir / "portfolio_2025-01_2026-02.json"
        report_path = report_dir / "sim_2025-01_2026-02.md"
        payload = {"snapshots": [{"date": "2026-03"}], "stats": {"cagr": 0.1}}
        sim_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_path.write_text("# sim\n", encoding="utf-8")
        return sim_path, report_path, payload

    def fake_execute_order_proposal(**kwargs):
        counts["execute_orders"] += 1
        output_path = kwargs.get("output_path")
        if output_path:
            out = Path(output_path)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text("{\"ok\": true}\n", encoding="utf-8")
        return {
            "submitted_count": 1,
            "skipped_count": 0,
            "dry_run": bool(kwargs.get("dry_run")),
        }

    monkeypatch.setattr("wealth_agents.orchestration.collect_from_feeds_with_stats", fake_collect_from_feeds_with_stats)
    monkeypatch.setattr("wealth_agents.orchestration.append_unique_records", fake_append_unique_records)
    monkeypatch.setattr("wealth_agents.orchestration.validate_jsonl", fake_validate_jsonl)
    monkeypatch.setattr("wealth_agents.orchestration.generate_weekly_report", fake_generate_weekly_report)
    monkeypatch.setattr("wealth_agents.orchestration.draft_policy", fake_draft_policy)
    monkeypatch.setattr("wealth_agents.orchestration.finalize_policy", fake_finalize_policy)
    monkeypatch.setattr("wealth_agents.orchestration.propose_monthly_orders", fake_propose_monthly_orders)
    monkeypatch.setattr("wealth_agents.orchestration.run_simulation", fake_run_simulation)
    monkeypatch.setattr("wealth_agents.orchestration.execute_order_proposal", fake_execute_order_proposal)

    result = run_cycle(
        week="2026-W09",
        month="2026-03",
        simulate_start="2025-01",
        simulate_end="2026-02",
        simulate_monthly=2500,
        cycle_id="test-cycle",
        cycle_dir=str(tmp_path / "runs"),
        execute_dry_run=True,
    )

    assert result["status"] == "completed"
    checkpoint_path = Path(result["checkpoint_path"])
    assert checkpoint_path.exists()
    checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
    assert checkpoint["status"] == "completed"
    for step in DEFAULT_STEP_SEQUENCE:
        assert checkpoint["steps"][step]["status"] == "completed"

    assert counts["collect"] == 1
    assert counts["append_unique"] == 1
    assert counts["report"] == 1
    assert counts["ips_draft"] == 1
    assert counts["ips_finalize"] == 1
    assert counts["propose_orders"] == 1
    assert counts["simulate"] == 1
    assert counts["execute_orders"] == 1


def test_run_cycle_resume_skips_completed_steps(tmp_path: Path, monkeypatch):
    counts = {"collect": 0}

    def fake_collect_from_feeds_with_stats(config_path: str, health_meta_path: str):
        counts["collect"] += 1
        return ([], {"status": "success", "fetched": 0})

    monkeypatch.setattr("wealth_agents.orchestration.collect_from_feeds_with_stats", fake_collect_from_feeds_with_stats)
    monkeypatch.setattr("wealth_agents.orchestration.append_unique_records", lambda path, records: 0)
    monkeypatch.setattr("wealth_agents.orchestration.validate_jsonl", lambda path: 0)
    monkeypatch.setattr(
        "wealth_agents.orchestration.generate_weekly_report",
        lambda **kwargs: Path(kwargs["output_dir"]) / f"weekly_{kwargs['week']}.md",
    )
    monkeypatch.setattr(
        "wealth_agents.orchestration.draft_policy",
        lambda **kwargs: (Path(kwargs["draft_path"]), Path(kwargs["report_dir"]) / "ips_fake.md"),
    )
    monkeypatch.setattr(
        "wealth_agents.orchestration.finalize_policy",
        lambda **kwargs: (Path(kwargs["policy_path"]), "hash"),
    )
    monkeypatch.setattr(
        "wealth_agents.orchestration.propose_monthly_orders",
        lambda **kwargs: (Path(kwargs["orders_dir"]) / "proposed_2026-03.json", Path(kwargs["reports_dir"]) / "orders.md", {"orders": []}),
    )
    monkeypatch.setattr(
        "wealth_agents.orchestration.run_simulation",
        lambda **kwargs: (Path(kwargs["sim_dir"]) / "sim.json", Path(kwargs["reports_dir"]) / "sim.md", {"snapshots": []}),
    )
    monkeypatch.setattr(
        "wealth_agents.orchestration.execute_order_proposal",
        lambda **kwargs: {"submitted_count": 0, "skipped_count": 0, "dry_run": True},
    )

    cycle_dir = tmp_path / "runs"
    run_cycle(
        week="2026-W09",
        month="2026-03",
        simulate_start="2025-01",
        simulate_end="2026-02",
        simulate_monthly=2500,
        cycle_id="resume-cycle",
        cycle_dir=str(cycle_dir),
        execute_dry_run=True,
    )
    assert counts["collect"] == 1

    run_cycle(
        week="2026-W09",
        month="2026-03",
        simulate_start="2025-01",
        simulate_end="2026-02",
        simulate_monthly=2500,
        cycle_id="resume-cycle",
        cycle_dir=str(cycle_dir),
        execute_dry_run=True,
        resume=True,
    )
    assert counts["collect"] == 1


def test_run_report_quality_gate_fails_when_report_is_too_thin(tmp_path: Path, monkeypatch):
    data_path = tmp_path / "data" / "raw" / "news.jsonl"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text(
        json.dumps({"id": "n1", "title": "a", "summary": "b"}, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    collect_meta_path = tmp_path / "data" / "meta" / "last_collect.json"
    collect_meta_path.parent.mkdir(parents=True, exist_ok=True)
    collect_meta_path.write_text("{}", encoding="utf-8")
    weekly_aggregates_path = tmp_path / "data" / "meta" / "weekly_aggregates.jsonl"
    weekly_aggregates_path.write_text(
        json.dumps(
            {
                "week": "2026-W09",
                "item_count": 4,
                "duplicates_removed_count": 8,
                "category_counts": {"macroeconomics": 2, "equities": 1},
            },
            ensure_ascii=False,
        )
        + "\n",
        encoding="utf-8",
    )

    def fake_generate_weekly_report(**kwargs):
        output_dir = Path(kwargs["output_dir"])
        output_dir.mkdir(parents=True, exist_ok=True)
        out = output_dir / "weekly_2026-W09.md"
        out.write_text(
            "\n".join(
                [
                    "# Weekly Report 2026-W09",
                    "",
                    "- week: 2026-W09",
                    "- number_of_items_considered: 4",
                    "- rss_collect_status: failed",
                    "- korea_items_count: 0",
                    "- germany_items_count: 0",
                    "- duplicates_removed_count: 8",
                    "",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        return out

    monkeypatch.setattr("wealth_agents.orchestration.generate_weekly_report", fake_generate_weekly_report)

    with pytest.raises(ValueError, match="Report quality gate failed"):
        _run_report(
            week="2026-W09",
            data_path=data_path,
            rules_path="config/rules.yml",
            report_dir=tmp_path / "reports",
            collect_meta_path=collect_meta_path,
            weekly_aggregates_path=weekly_aggregates_path,
            quality_gate_profile="strict",
        )


def test_run_propose_orders_quality_gate_fails_on_concentration(tmp_path: Path, monkeypatch):
    def fake_propose_monthly_orders(**kwargs):
        orders_dir = Path(kwargs["orders_dir"])
        report_dir = Path(kwargs["reports_dir"])
        orders_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        orders_path = orders_dir / "proposed_2026-03.json"
        report_path = report_dir / "orders_2026-03.md"
        payload = {
            "month": "2026-03",
            "currency": "EUR",
            "budget_eur": 2500,
            "policy_hash": "fake-hash",
            "orders": [
                {
                    "side": "BUY",
                    "instrument_id": "sp500_acc",
                    "isin": "IE00B5BMR087",
                    "name": "ETF",
                    "bucket": "global_equity",
                    "amount_eur": 2500,
                }
            ],
        }
        orders_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_path.write_text("# orders\n", encoding="utf-8")
        return orders_path, report_path, payload

    monkeypatch.setattr("wealth_agents.orchestration.propose_monthly_orders", fake_propose_monthly_orders)

    with pytest.raises(ValueError, match="Orders quality gate failed"):
        _run_propose_orders(
            month="2026-03",
            policy_path=tmp_path / "policy.yml",
            orders_dir=tmp_path / "orders",
            report_dir=tmp_path / "reports",
            quality_gate_profile="strict",
        )


def test_run_propose_orders_quality_gate_passes_standard(tmp_path: Path, monkeypatch):
    def fake_propose_monthly_orders(**kwargs):
        orders_dir = Path(kwargs["orders_dir"])
        report_dir = Path(kwargs["reports_dir"])
        orders_dir.mkdir(parents=True, exist_ok=True)
        report_dir.mkdir(parents=True, exist_ok=True)
        orders_path = orders_dir / "proposed_2026-03.json"
        report_path = report_dir / "orders_2026-03.md"
        payload = {
            "month": "2026-03",
            "currency": "EUR",
            "budget_eur": 2500,
            "policy_hash": "fake-hash",
            "orders": [
                {
                    "side": "BUY",
                    "instrument_id": "sp500_acc",
                    "isin": "IE00B5BMR087",
                    "name": "ETF 1",
                    "bucket": "global_equity",
                    "amount_eur": 1500,
                },
                {
                    "side": "BUY",
                    "instrument_id": "bond_eu_acc",
                    "isin": "IE00B3F81R35",
                    "name": "ETF 2",
                    "bucket": "bonds",
                    "amount_eur": 1000,
                },
            ],
        }
        orders_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        report_path.write_text("# orders\n", encoding="utf-8")
        return orders_path, report_path, payload

    monkeypatch.setattr("wealth_agents.orchestration.propose_monthly_orders", fake_propose_monthly_orders)

    out = _run_propose_orders(
        month="2026-03",
        policy_path=tmp_path / "policy.yml",
        orders_dir=tmp_path / "orders",
        report_dir=tmp_path / "reports",
        quality_gate_profile="standard",
    )
    quality = out["quality_gate"]
    assert quality["profile"] == "standard"
    assert quality["passed"] is True
