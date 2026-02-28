import json
from pathlib import Path

from wealth_agents.dashboard import build_dashboard


def test_build_dashboard_renders_cycles_and_scheduler(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    preflight_dir = tmp_path / "reports"
    cycle_ok = runs_dir / "cycle_ok"
    cycle_fail = runs_dir / "cycle_fail"
    cycle_ok.mkdir(parents=True, exist_ok=True)
    cycle_fail.mkdir(parents=True, exist_ok=True)
    preflight_dir.mkdir(parents=True, exist_ok=True)

    (cycle_ok / "checkpoint.json").write_text(
        json.dumps(
            {
                "cycle_id": "cycle_ok",
                "status": "completed",
                "updated_at": "2026-02-28T00:10:00Z",
                "steps": {
                    "collect": {"status": "completed"},
                    "report": {"status": "completed"},
                    "propose_orders": {"status": "completed"},
                    "execute_orders": {
                        "status": "completed",
                        "output": {
                            "guardrails_enabled": True,
                            "guardrails_passed": True,
                            "dry_run": True,
                            "submitted_count": 3,
                            "skipped_count": 0,
                        },
                    },
                },
                "artifacts": {
                    "weekly_report_path": "/tmp/wa_runs/cycle_ok/reports/weekly_2026-W09.md",
                    "orders_path": "/tmp/wa_runs/cycle_ok/orders/proposed_2026-03.json",
                    "report_quality_gate": {
                        "profile": "standard",
                        "passed": True,
                        "metrics": {"item_count": 14, "distinct_categories": 5},
                    },
                    "orders_quality_gate": {
                        "profile": "standard",
                        "passed": True,
                        "metrics": {"orders_count": 3, "unique_buckets": 3},
                    },
                },
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (cycle_fail / "checkpoint.json").write_text(
        json.dumps(
            {
                "cycle_id": "cycle_fail",
                "status": "failed",
                "updated_at": "2026-02-28T00:20:00Z",
                "steps": {
                    "collect": {"status": "completed"},
                    "report": {"status": "failed"},
                    "execute_orders": {
                        "status": "completed",
                        "output": {
                            "guardrails_enabled": True,
                            "guardrails_passed": False,
                            "dry_run": True,
                            "submitted_count": 0,
                            "skipped_count": 2,
                        },
                    },
                },
                "error": {"message": "Report quality gate failed"},
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    state_path = runs_dir / "scheduler_state.json"
    state_path.write_text(
        json.dumps(
            {
                "last_completed": {"weekly": "2026-W09"},
                "updated_at": "2026-02-28T00:30:00Z",
                "runs": [
                    {
                        "ran_at": "2026-02-28T00:11:00Z",
                        "cadence": "weekly",
                        "period_key": "2026-W09",
                        "status": "completed",
                        "cycle_id": "cycle_ok",
                    },
                    {
                        "ran_at": "2026-02-28T00:21:00Z",
                        "cadence": "weekly",
                        "period_key": "2026-W09",
                        "status": "failed",
                        "cycle_id": "cycle_fail",
                        "error": "Report quality gate failed",
                    },
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    (preflight_dir / "ibkr_preflight_2026-02-28.md").write_text(
        (
            "# IBKR Preflight Report\n\n"
            "## Summary\n"
            "- started_at: 2026-02-28T00:00:00Z\n"
            "- finished_at: 2026-02-28T00:10:00Z\n"
            "- passed: False\n"
            "- tickers_checked: 6\n"
            "- failed_count: 2\n"
            "- status_ok: 4\n"
            "- status_missing_contract: 2\n"
        ),
        encoding="utf-8",
    )

    out = build_dashboard(
        runs_dir=str(runs_dir),
        state_path=str(state_path),
        output_path=str(runs_dir / "dashboard.html"),
        preflight_dir=str(preflight_dir),
        limit=20,
        title="Test Dashboard",
    )
    html = out.read_text(encoding="utf-8")

    assert "Test Dashboard" in html
    assert "cycle_ok" in html
    assert "cycle_fail" in html
    assert "report gate" in html
    assert "orders gate" in html
    assert "standard/pass" in html
    assert "exec guardrail" in html
    assert "on/pass" in html
    assert "on/fail" in html
    assert "Report quality gate failed" in html
    assert "IBKR Preflight" in html
    assert "ibkr_preflight_2026-02-28.md" in html
    assert "tickers_checked: 6" in html
    assert "2026-W09" in html


def test_build_dashboard_handles_missing_inputs(tmp_path: Path):
    runs_dir = tmp_path / "runs"
    out = build_dashboard(
        runs_dir=str(runs_dir),
        state_path=str(runs_dir / "scheduler_state.json"),
        output_path=str(runs_dir / "dashboard.html"),
        preflight_dir=str(tmp_path / "reports_missing"),
        limit=5,
    )
    html = out.read_text(encoding="utf-8")

    assert "No cycles found." in html
    assert "Scheduler state file not found." in html
    assert "No IBKR preflight reports found." in html
