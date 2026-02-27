from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import time
from typing import Any, Callable

from .market_prices import parse_iso_month
from .orchestration import (
    DEFAULT_COLLECT_CONFIG,
    DEFAULT_CYCLE_DIR,
    DEFAULT_EXECUTE_BROKER,
    DEFAULT_IPS_INPUT_PATH,
    DEFAULT_POLICY_CHOICE,
    DEFAULT_PRICES_DIR,
    DEFAULT_RULES_PATH,
    run_cycle,
)


VALID_CADENCES = {"weekly", "monthly"}
DEFAULT_STATE_PATH = "runs/scheduler_state.json"
DEFAULT_SCHEDULER_QUALITY_GATE_PROFILE = "standard"


def run_scheduler_once(
    *,
    cadence: str = "weekly",
    state_path: str = DEFAULT_STATE_PATH,
    cycle_dir: str = DEFAULT_CYCLE_DIR,
    week: str | None = None,
    month: str | None = None,
    simulate_monthly: float,
    simulate_initial: float = 0.0,
    simulate_lookback_months: int = 14,
    simulate_end_offset_months: int = 0,
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
    quality_gate_profile: str = DEFAULT_SCHEDULER_QUALITY_GATE_PROFILE,
    force: bool = False,
    now: datetime | None = None,
    run_cycle_fn: Callable[..., dict[str, Any]] = run_cycle,
) -> dict[str, Any]:
    cadence_value = str(cadence).strip().lower()
    if cadence_value not in VALID_CADENCES:
        raise ValueError(f"cadence must be one of {sorted(VALID_CADENCES)}.")
    if simulate_lookback_months < 1:
        raise ValueError("simulate_lookback_months must be >= 1.")

    current_now = _as_utc(now)
    effective_week = _validate_iso_week(week) if week else _iso_week(current_now)
    effective_month = _validate_iso_month(month) if month else current_now.strftime("%Y-%m")
    period_key = effective_week if cadence_value == "weekly" else effective_month

    state_file = Path(state_path)
    state = _load_state(state_file)
    last_completed_map = state.get("last_completed")
    if not isinstance(last_completed_map, dict):
        last_completed_map = {}
        state["last_completed"] = last_completed_map
    if not isinstance(state.get("runs"), list):
        state["runs"] = []

    last_completed = str(last_completed_map.get(cadence_value) or "")
    if (not force) and last_completed == period_key:
        state["updated_at"] = _now_iso8601(current_now)
        _save_state(state_file, state)
        return {
            "ran": False,
            "reason": "already_completed",
            "cadence": cadence_value,
            "period_key": period_key,
            "week": effective_week,
            "month": effective_month,
        }

    simulate_end = _shift_month(effective_month, simulate_end_offset_months)
    simulate_start = _shift_month(simulate_end, -(simulate_lookback_months - 1))
    cycle_id = f"scheduler_{cadence_value}_{period_key}_{current_now.strftime('%Y%m%dT%H%M%SZ')}"

    try:
        cycle_result = run_cycle_fn(
            week=effective_week,
            month=effective_month,
            simulate_start=simulate_start,
            simulate_end=simulate_end,
            simulate_monthly=simulate_monthly,
            simulate_initial=simulate_initial,
            cycle_id=cycle_id,
            cycle_dir=cycle_dir,
            collect_config=collect_config,
            rules_path=rules_path,
            ips_input_path=ips_input_path,
            policy_choice=policy_choice,
            max_signal_tilt_pct=max_signal_tilt_pct,
            simulation_feedback_path=simulation_feedback_path,
            prices_dir=prices_dir,
            allow_short_history=allow_short_history,
            execute_broker=execute_broker,
            execute_dry_run=execute_dry_run,
            skip_execution=skip_execution,
            resume=resume,
            quality_gate_profile=quality_gate_profile,
        )
    except Exception as exc:
        _append_run(
            state=state,
            cadence=cadence_value,
            period_key=period_key,
            cycle_id=cycle_id,
            week=effective_week,
            month=effective_month,
            simulate_start=simulate_start,
            simulate_end=simulate_end,
            status="failed",
            error=str(exc),
            checkpoint_path=None,
            when=current_now,
        )
        state["updated_at"] = _now_iso8601(current_now)
        _save_state(state_file, state)
        raise

    last_completed_map[cadence_value] = period_key
    _append_run(
        state=state,
        cadence=cadence_value,
        period_key=period_key,
        cycle_id=cycle_id,
        week=effective_week,
        month=effective_month,
        simulate_start=simulate_start,
        simulate_end=simulate_end,
        status=str(cycle_result.get("status") or "completed"),
        error=None,
        checkpoint_path=str(cycle_result.get("checkpoint_path") or ""),
        when=current_now,
    )
    state["updated_at"] = _now_iso8601(current_now)
    _save_state(state_file, state)
    return {
        "ran": True,
        "reason": "triggered",
        "cadence": cadence_value,
        "period_key": period_key,
        "week": effective_week,
        "month": effective_month,
        "simulate_start": simulate_start,
        "simulate_end": simulate_end,
        "cycle_id": cycle_id,
        "checkpoint_path": str(cycle_result.get("checkpoint_path") or ""),
        "status": str(cycle_result.get("status") or "completed"),
    }


def run_scheduler_loop(
    *,
    cadence: str = "weekly",
    state_path: str = DEFAULT_STATE_PATH,
    cycle_dir: str = DEFAULT_CYCLE_DIR,
    poll_seconds: int = 300,
    max_runs: int | None = None,
    stop_on_error: bool = False,
    week: str | None = None,
    month: str | None = None,
    simulate_monthly: float,
    simulate_initial: float = 0.0,
    simulate_lookback_months: int = 14,
    simulate_end_offset_months: int = 0,
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
    quality_gate_profile: str = DEFAULT_SCHEDULER_QUALITY_GATE_PROFILE,
    force: bool = False,
    run_once_fn: Callable[..., dict[str, Any]] = run_scheduler_once,
    sleep_fn: Callable[[float], Any] = time.sleep,
) -> dict[str, Any]:
    if poll_seconds <= 0:
        raise ValueError("poll_seconds must be > 0.")
    if max_runs is not None and max_runs <= 0:
        raise ValueError("max_runs must be > 0 when provided.")

    successful_runs = 0
    ticks = 0
    last_result: dict[str, Any] | None = None
    while True:
        ticks += 1
        try:
            result = run_once_fn(
                cadence=cadence,
                state_path=state_path,
                cycle_dir=cycle_dir,
                week=week,
                month=month,
                simulate_monthly=simulate_monthly,
                simulate_initial=simulate_initial,
                simulate_lookback_months=simulate_lookback_months,
                simulate_end_offset_months=simulate_end_offset_months,
                collect_config=collect_config,
                rules_path=rules_path,
                ips_input_path=ips_input_path,
                policy_choice=policy_choice,
                max_signal_tilt_pct=max_signal_tilt_pct,
                simulation_feedback_path=simulation_feedback_path,
                prices_dir=prices_dir,
                allow_short_history=allow_short_history,
                execute_broker=execute_broker,
                execute_dry_run=execute_dry_run,
                skip_execution=skip_execution,
                resume=resume,
                quality_gate_profile=quality_gate_profile,
                force=force,
            )
        except Exception as exc:
            if stop_on_error:
                raise
            result = {
                "ran": False,
                "reason": "error",
                "error": str(exc),
            }

        last_result = result
        if bool(result.get("ran")):
            successful_runs += 1
        if max_runs is not None and successful_runs >= max_runs:
            break
        sleep_fn(poll_seconds)

    return {
        "ticks": ticks,
        "successful_runs": successful_runs,
        "last_result": last_result or {},
    }


def _append_run(
    *,
    state: dict[str, Any],
    cadence: str,
    period_key: str,
    cycle_id: str,
    week: str,
    month: str,
    simulate_start: str,
    simulate_end: str,
    status: str,
    error: str | None,
    checkpoint_path: str | None,
    when: datetime,
) -> None:
    runs = state.setdefault("runs", [])
    if not isinstance(runs, list):
        runs = []
        state["runs"] = runs
    row = {
        "ran_at": _now_iso8601(when),
        "cadence": cadence,
        "period_key": period_key,
        "cycle_id": cycle_id,
        "week": week,
        "month": month,
        "simulate_start": simulate_start,
        "simulate_end": simulate_end,
        "status": status,
    }
    if checkpoint_path:
        row["checkpoint_path"] = checkpoint_path
    if error:
        row["error"] = error
    runs.append(row)


def _load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {
            "last_completed": {},
            "runs": [],
        }
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid scheduler state JSON: {path}") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid scheduler state payload: {path}")
    return payload


def _save_state(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _shift_month(month: str, offset: int) -> str:
    current = parse_iso_month(month, "month")
    total = current.year * 12 + (current.month - 1) + offset
    year = total // 12
    month_num = total % 12 + 1
    return f"{year:04d}-{month_num:02d}"


def _validate_iso_month(value: str | None) -> str:
    if value is None:
        raise ValueError("month must be in YYYY-MM format.")
    parsed = parse_iso_month(value, "month")
    return parsed.strftime("%Y-%m")


def _validate_iso_week(value: str | None) -> str:
    if value is None:
        raise ValueError("week must be in ISO format YYYY-Www.")
    text = str(value).strip()
    try:
        year_text, week_text = text.split("-W")
        year = int(year_text)
        week = int(week_text)
        datetime.fromisocalendar(year, week, 1)
    except (ValueError, TypeError) as exc:
        raise ValueError("week must be a valid ISO week in format YYYY-Www.") from exc
    return f"{year:04d}-W{week:02d}"


def _iso_week(value: datetime) -> str:
    year, week, _ = value.isocalendar()
    return f"{year:04d}-W{week:02d}"


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _now_iso8601(value: datetime) -> str:
    return value.replace(microsecond=0).isoformat().replace("+00:00", "Z")
