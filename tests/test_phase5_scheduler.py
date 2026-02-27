from datetime import datetime, timezone
import json
from pathlib import Path

import pytest

from wealth_agents.scheduler import run_scheduler_loop, run_scheduler_once


def test_run_scheduler_once_runs_when_due_and_records_state(tmp_path: Path):
    captured: dict[str, object] = {}

    def fake_run_cycle(**kwargs):
        captured.update(kwargs)
        return {
            "status": "completed",
            "checkpoint_path": str(tmp_path / "runs" / "checkpoint.json"),
        }

    state_path = tmp_path / "runs" / "scheduler_state.json"
    result = run_scheduler_once(
        cadence="weekly",
        state_path=str(state_path),
        cycle_dir=str(tmp_path / "runs"),
        simulate_monthly=2500,
        simulate_initial=0,
        skip_execution=True,
        now=datetime(2026, 2, 27, 9, 0, 0, tzinfo=timezone.utc),
        run_cycle_fn=fake_run_cycle,
    )

    assert result["ran"] is True
    assert result["cadence"] == "weekly"
    assert result["period_key"] == "2026-W09"
    assert result["week"] == "2026-W09"
    assert result["month"] == "2026-02"
    assert result["simulate_start"] == "2025-01"
    assert result["simulate_end"] == "2026-02"

    assert captured["week"] == "2026-W09"
    assert captured["month"] == "2026-02"
    assert captured["simulate_start"] == "2025-01"
    assert captured["simulate_end"] == "2026-02"
    assert captured["skip_execution"] is True

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert state["last_completed"]["weekly"] == "2026-W09"
    assert len(state["runs"]) == 1
    assert state["runs"][0]["status"] == "completed"
    assert state["runs"][0]["period_key"] == "2026-W09"


def test_run_scheduler_once_skips_if_period_already_completed(tmp_path: Path):
    state_path = tmp_path / "runs" / "scheduler_state.json"
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(
        json.dumps(
            {
                "last_completed": {"weekly": "2026-W09"},
                "runs": [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    called = {"count": 0}

    def fake_run_cycle(**kwargs):
        called["count"] += 1
        return {"status": "completed", "checkpoint_path": ""}

    result = run_scheduler_once(
        cadence="weekly",
        state_path=str(state_path),
        cycle_dir=str(tmp_path / "runs"),
        simulate_monthly=2500,
        now=datetime(2026, 2, 27, 9, 0, 0, tzinfo=timezone.utc),
        run_cycle_fn=fake_run_cycle,
    )

    assert result["ran"] is False
    assert result["reason"] == "already_completed"
    assert called["count"] == 0


def test_run_scheduler_once_records_failure_and_raises(tmp_path: Path):
    state_path = tmp_path / "runs" / "scheduler_state.json"

    def fake_run_cycle(**kwargs):
        raise RuntimeError("cycle failed")

    with pytest.raises(RuntimeError, match="cycle failed"):
        run_scheduler_once(
            cadence="weekly",
            state_path=str(state_path),
            cycle_dir=str(tmp_path / "runs"),
            simulate_monthly=2500,
            now=datetime(2026, 2, 27, 9, 0, 0, tzinfo=timezone.utc),
            run_cycle_fn=fake_run_cycle,
        )

    state = json.loads(state_path.read_text(encoding="utf-8"))
    assert "weekly" not in state["last_completed"]
    assert len(state["runs"]) == 1
    assert state["runs"][0]["status"] == "failed"
    assert "cycle failed" in state["runs"][0]["error"]


def test_run_scheduler_loop_stops_after_max_runs():
    calls = {"count": 0}
    sleeps: list[float] = []

    def fake_run_once(**kwargs):
        calls["count"] += 1
        return {
            "ran": True,
            "reason": "triggered",
        }

    summary = run_scheduler_loop(
        cadence="weekly",
        state_path="unused.json",
        cycle_dir="runs",
        poll_seconds=1,
        max_runs=2,
        simulate_monthly=2500,
        run_once_fn=fake_run_once,
        sleep_fn=lambda seconds: sleeps.append(seconds),
    )

    assert summary["successful_runs"] == 2
    assert summary["ticks"] == 2
    assert calls["count"] == 2
    assert sleeps == [1]
