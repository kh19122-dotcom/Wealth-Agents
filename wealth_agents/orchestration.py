from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

from .execution import execute_order_proposal
from .ips import draft_policy, finalize_policy
from .orders import propose_monthly_orders
from .report import generate_weekly_report
from .rss import collect_from_feeds_with_stats
from .simulation import run_simulation
from .storage import append_unique_records, validate_jsonl


DEFAULT_CYCLE_DIR = "runs"
DEFAULT_POLICY_CHOICE = "balanced"
DEFAULT_COLLECT_CONFIG = "config/feeds.yml"
DEFAULT_RULES_PATH = "config/rules.yml"
DEFAULT_IPS_INPUT_PATH = "data/policy/ips_inputs.yml"
DEFAULT_PRICES_DIR = "data/prices"
DEFAULT_EXECUTE_BROKER = "mock"
DEFAULT_STEP_SEQUENCE = (
    "collect",
    "report",
    "ips_draft",
    "ips_finalize",
    "propose_orders",
    "simulate",
    "execute_orders",
)


def run_cycle(
    *,
    week: str,
    month: str,
    simulate_start: str,
    simulate_end: str,
    simulate_monthly: float,
    simulate_initial: float = 0.0,
    cycle_id: str | None = None,
    cycle_dir: str = DEFAULT_CYCLE_DIR,
    collect_config: str = DEFAULT_COLLECT_CONFIG,
    rules_path: str = DEFAULT_RULES_PATH,
    ips_input_path: str = DEFAULT_IPS_INPUT_PATH,
    policy_choice: str = DEFAULT_POLICY_CHOICE,
    max_signal_tilt_pct: int = 5,
    simulation_feedback_path: str | None = None,
    prices_dir: str = DEFAULT_PRICES_DIR,
    allow_short_history: bool = False,
    execute_broker: str = DEFAULT_EXECUTE_BROKER,
    execute_dry_run: bool = True,
    skip_execution: bool = False,
    resume: bool = False,
) -> dict[str, Any]:
    current_cycle_id = cycle_id or _default_cycle_id()
    root = Path(cycle_dir) / current_cycle_id
    checkpoint_path = root / "checkpoint.json"
    checkpoint = _load_or_init_checkpoint(
        checkpoint_path=checkpoint_path,
        cycle_id=current_cycle_id,
        root=root,
    )
    checkpoint["status"] = "running"
    checkpoint["updated_at"] = _now_iso8601()
    _save_checkpoint(checkpoint_path, checkpoint)

    artifacts = checkpoint.setdefault("artifacts", {})
    if not isinstance(artifacts, dict):
        artifacts = {}
        checkpoint["artifacts"] = artifacts

    data_path = root / "data/raw/news.jsonl"
    collect_meta_path = root / "data/meta/last_collect.json"
    health_meta_path = root / "data/meta/feed_health.json"
    weekly_aggregates_path = root / "data/meta/weekly_aggregates.jsonl"
    report_dir = root / "reports"
    draft_path = root / "data/policy/policy_draft.yml"
    policy_path = root / "data/policy/policy.yml"
    policy_history_path = root / "data/policy/policy_history.jsonl"
    orders_dir = root / "orders"
    sim_dir = root / "sim"
    execution_output_path = root / "orders" / f"execution_{month}.json"

    def _run_step(step_name: str, fn: Any) -> dict[str, Any]:
        if resume and _is_step_completed(checkpoint, step_name):
            step_row = _step_row(checkpoint, step_name)
            assert isinstance(step_row, dict)
            return dict(step_row.get("output") or {})

        _mark_step_started(checkpoint, step_name)
        _save_checkpoint(checkpoint_path, checkpoint)
        try:
            output = fn()
        except Exception as exc:
            _mark_step_failed(checkpoint, step_name, str(exc))
            checkpoint["status"] = "failed"
            checkpoint["updated_at"] = _now_iso8601()
            checkpoint["error"] = {
                "step": step_name,
                "message": str(exc),
                "failed_at": checkpoint["updated_at"],
            }
            _save_checkpoint(checkpoint_path, checkpoint)
            raise
        _mark_step_completed(checkpoint, step_name, output=output)
        checkpoint["updated_at"] = _now_iso8601()
        _save_checkpoint(checkpoint_path, checkpoint)
        return output

    collect_out = _run_step(
        "collect",
        lambda: _run_collect(
            collect_config=collect_config,
            data_path=data_path,
            collect_meta_path=collect_meta_path,
            health_meta_path=health_meta_path,
        ),
    )
    artifacts.update(
        {
            "data_path": str(data_path),
            "collect_meta_path": str(collect_meta_path),
            "health_meta_path": str(health_meta_path),
            "collect_status": collect_out.get("status"),
        }
    )

    report_out = _run_step(
        "report",
        lambda: _run_report(
            week=week,
            data_path=data_path,
            rules_path=rules_path,
            report_dir=report_dir,
            collect_meta_path=collect_meta_path,
            weekly_aggregates_path=weekly_aggregates_path,
        ),
    )
    artifacts.update(
        {
            "weekly_report_path": report_out.get("report_path"),
            "weekly_aggregates_path": str(weekly_aggregates_path),
        }
    )

    draft_out = _run_step(
        "ips_draft",
        lambda: _run_ips_draft(
            ips_input_path=ips_input_path,
            draft_path=draft_path,
            report_dir=report_dir,
            weekly_aggregates_path=weekly_aggregates_path,
            week=week,
            max_signal_tilt_pct=max_signal_tilt_pct,
            simulation_feedback_path=simulation_feedback_path,
        ),
    )
    artifacts.update(
        {
            "policy_draft_path": draft_out.get("draft_path"),
            "ips_draft_report_path": draft_out.get("report_path"),
        }
    )

    finalize_out = _run_step(
        "ips_finalize",
        lambda: _run_ips_finalize(
            policy_choice=policy_choice,
            draft_path=draft_path,
            policy_path=policy_path,
            policy_history_path=policy_history_path,
        ),
    )
    artifacts.update(
        {
            "policy_path": finalize_out.get("policy_path"),
            "policy_hash": finalize_out.get("policy_hash"),
        }
    )

    propose_out = _run_step(
        "propose_orders",
        lambda: _run_propose_orders(
            month=month,
            policy_path=policy_path,
            orders_dir=orders_dir,
            report_dir=report_dir,
        ),
    )
    artifacts.update(
        {
            "orders_path": propose_out.get("orders_path"),
            "orders_report_path": propose_out.get("report_path"),
        }
    )

    simulate_out = _run_step(
        "simulate",
        lambda: _run_simulate(
            simulate_start=simulate_start,
            simulate_end=simulate_end,
            simulate_monthly=simulate_monthly,
            simulate_initial=simulate_initial,
            allow_short_history=allow_short_history,
            policy_path=policy_path,
            prices_dir=prices_dir,
            sim_dir=sim_dir,
            report_dir=report_dir,
        ),
    )
    artifacts.update(
        {
            "simulation_path": simulate_out.get("simulation_path"),
            "simulation_report_path": simulate_out.get("report_path"),
        }
    )

    if skip_execution:
        _mark_step_completed(checkpoint, "execute_orders", {"skipped": True, "reason": "skip_execution=True"})
    else:
        execute_out = _run_step(
            "execute_orders",
            lambda: _run_execute_orders(
                month=month,
                proposal_path=Path(str(propose_out["orders_path"])),
                execute_broker=execute_broker,
                execute_dry_run=execute_dry_run,
                root=root,
                output_path=execution_output_path,
            ),
        )
        artifacts["execution_result_path"] = execute_out.get("output_path")

    checkpoint["status"] = "completed"
    checkpoint["finished_at"] = _now_iso8601()
    checkpoint["updated_at"] = checkpoint["finished_at"]
    _save_checkpoint(checkpoint_path, checkpoint)
    return {
        "cycle_id": current_cycle_id,
        "status": checkpoint["status"],
        "root": str(root),
        "checkpoint_path": str(checkpoint_path),
        "artifacts": dict(artifacts),
        "steps": checkpoint.get("steps", {}),
    }


def _run_collect(
    *,
    collect_config: str,
    data_path: Path,
    collect_meta_path: Path,
    health_meta_path: Path,
) -> dict[str, Any]:
    records, stats = collect_from_feeds_with_stats(
        config_path=collect_config,
        health_meta_path=str(health_meta_path),
    )
    appended = append_unique_records(path=data_path, records=records)
    stats["appended"] = appended
    _write_json(collect_meta_path, stats)
    validated_lines = validate_jsonl(data_path)
    return {
        "status": str(stats.get("status") or "success"),
        "fetched": int(stats.get("fetched") or 0),
        "appended": int(appended),
        "validated_lines": int(validated_lines),
        "data_path": str(data_path),
        "collect_meta_path": str(collect_meta_path),
        "health_meta_path": str(health_meta_path),
    }


def _run_report(
    *,
    week: str,
    data_path: Path,
    rules_path: str,
    report_dir: Path,
    collect_meta_path: Path,
    weekly_aggregates_path: Path,
) -> dict[str, Any]:
    validate_jsonl(data_path)
    report_path = generate_weekly_report(
        week=week,
        data_path=str(data_path),
        rules_path=rules_path,
        output_dir=str(report_dir),
        collect_meta_path=str(collect_meta_path),
        weekly_aggregates_path=str(weekly_aggregates_path),
    )
    return {
        "report_path": str(report_path),
        "weekly_aggregates_path": str(weekly_aggregates_path),
    }


def _run_ips_draft(
    *,
    ips_input_path: str,
    draft_path: Path,
    report_dir: Path,
    weekly_aggregates_path: Path,
    week: str,
    max_signal_tilt_pct: int,
    simulation_feedback_path: str | None,
) -> dict[str, Any]:
    out_draft, out_report = draft_policy(
        input_path=ips_input_path,
        draft_path=str(draft_path),
        report_dir=str(report_dir),
        weekly_aggregates_path=str(weekly_aggregates_path),
        week=week,
        max_signal_tilt_pct=max_signal_tilt_pct,
        simulation_feedback_path=simulation_feedback_path,
    )
    return {
        "draft_path": str(out_draft),
        "report_path": str(out_report),
    }


def _run_ips_finalize(
    *,
    policy_choice: str,
    draft_path: Path,
    policy_path: Path,
    policy_history_path: Path,
) -> dict[str, Any]:
    out_policy, policy_hash = finalize_policy(
        choice=policy_choice,
        draft_path=str(draft_path),
        policy_path=str(policy_path),
        history_path=str(policy_history_path),
    )
    return {
        "policy_path": str(out_policy),
        "policy_hash": str(policy_hash),
    }


def _run_propose_orders(
    *,
    month: str,
    policy_path: Path,
    orders_dir: Path,
    report_dir: Path,
) -> dict[str, Any]:
    out_orders, out_report, payload = propose_monthly_orders(
        month=month,
        policy_path=str(policy_path),
        orders_dir=str(orders_dir),
        reports_dir=str(report_dir),
    )
    return {
        "orders_path": str(out_orders),
        "report_path": str(out_report),
        "orders_count": len(payload.get("orders") or []),
        "budget_eur": int(payload.get("budget_eur") or 0),
    }


def _run_simulate(
    *,
    simulate_start: str,
    simulate_end: str,
    simulate_monthly: float,
    simulate_initial: float,
    allow_short_history: bool,
    policy_path: Path,
    prices_dir: str,
    sim_dir: Path,
    report_dir: Path,
) -> dict[str, Any]:
    sim_path, report_path, payload = run_simulation(
        start=simulate_start,
        end=simulate_end,
        monthly=simulate_monthly,
        initial=simulate_initial,
        allow_short_history=allow_short_history,
        policy_path=str(policy_path),
        prices_dir=prices_dir,
        sim_dir=str(sim_dir),
        reports_dir=str(report_dir),
    )
    return {
        "simulation_path": str(sim_path),
        "report_path": str(report_path),
        "snapshots": len(payload.get("snapshots") or []),
    }


def _run_execute_orders(
    *,
    month: str,
    proposal_path: Path,
    execute_broker: str,
    execute_dry_run: bool,
    root: Path,
    output_path: Path,
) -> dict[str, Any]:
    result = execute_order_proposal(
        proposal_path=str(proposal_path),
        broker=execute_broker,
        mock_state_path=str(root / "data/broker/mock_state.json"),
        dry_run=execute_dry_run,
        output_path=str(output_path),
    )
    return {
        "output_path": str(output_path),
        "submitted_count": int(result.get("submitted_count") or 0),
        "skipped_count": int(result.get("skipped_count") or 0),
        "dry_run": bool(result.get("dry_run")),
        "month": month,
    }


def _load_or_init_checkpoint(checkpoint_path: Path, cycle_id: str, root: Path) -> dict[str, Any]:
    if checkpoint_path.exists():
        try:
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Invalid checkpoint JSON: {checkpoint_path}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Invalid checkpoint payload: {checkpoint_path}")
        payload.setdefault("cycle_id", cycle_id)
        payload.setdefault("root", str(root))
        payload.setdefault("steps", {})
        return payload

    now = _now_iso8601()
    payload = {
        "cycle_id": cycle_id,
        "root": str(root),
        "status": "initialized",
        "started_at": now,
        "updated_at": now,
        "steps": {},
        "artifacts": {},
    }
    _save_checkpoint(checkpoint_path, payload)
    return payload


def _step_row(checkpoint: dict[str, Any], step_name: str) -> dict[str, Any]:
    steps = checkpoint.setdefault("steps", {})
    if not isinstance(steps, dict):
        steps = {}
        checkpoint["steps"] = steps
    row = steps.setdefault(step_name, {})
    if not isinstance(row, dict):
        row = {}
        steps[step_name] = row
    return row


def _is_step_completed(checkpoint: dict[str, Any], step_name: str) -> bool:
    row = _step_row(checkpoint, step_name)
    return str(row.get("status") or "") == "completed"


def _mark_step_started(checkpoint: dict[str, Any], step_name: str) -> None:
    row = _step_row(checkpoint, step_name)
    row["status"] = "running"
    row["started_at"] = _now_iso8601()
    row.pop("finished_at", None)
    row.pop("error", None)


def _mark_step_completed(checkpoint: dict[str, Any], step_name: str, output: dict[str, Any]) -> None:
    row = _step_row(checkpoint, step_name)
    row["status"] = "completed"
    row["finished_at"] = _now_iso8601()
    row["output"] = output
    row.pop("error", None)


def _mark_step_failed(checkpoint: dict[str, Any], step_name: str, message: str) -> None:
    row = _step_row(checkpoint, step_name)
    row["status"] = "failed"
    row["finished_at"] = _now_iso8601()
    row["error"] = {"message": message}


def _save_checkpoint(checkpoint_path: Path, payload: dict[str, Any]) -> None:
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    checkpoint_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _default_cycle_id() -> str:
    return datetime.now(timezone.utc).strftime("cycle_%Y%m%dT%H%M%SZ")
