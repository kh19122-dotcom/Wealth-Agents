from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re
from typing import Any

from .execution import execute_order_proposal
from .ips import draft_policy, finalize_policy
from .orders import propose_monthly_orders
from .report import generate_weekly_report
from .rss import collect_from_feeds_with_stats
from .simulation import run_simulation
from .storage import append_unique_records, read_jsonl, validate_jsonl


DEFAULT_CYCLE_DIR = "runs"
DEFAULT_POLICY_CHOICE = "balanced"
DEFAULT_COLLECT_CONFIG = "config/feeds.yml"
DEFAULT_RULES_PATH = "config/rules.yml"
DEFAULT_IPS_INPUT_PATH = "data/policy/ips_inputs.yml"
DEFAULT_PRICES_DIR = "data/prices"
DEFAULT_EXECUTE_BROKER = "mock"
DEFAULT_EXECUTION_GUARDRAILS_PATH = "config/execution_guardrails.yml"
DEFAULT_EXECUTE_IBKR_CONTRACTS_PATH = "config/ibkr_contracts.yml"
DEFAULT_EXECUTE_IBKR_HOST = "127.0.0.1"
DEFAULT_EXECUTE_IBKR_PORT = 7497
DEFAULT_EXECUTE_IBKR_CLIENT_ID = 37
DEFAULT_EXECUTE_IBKR_TIMEOUT_SEC = 8.0
DEFAULT_EXECUTE_IBKR_LIMIT_BUFFER_PCT = 0.5
DEFAULT_QUALITY_GATE_PROFILE = "off"
QUALITY_GATE_PROFILES: dict[str, dict[str, Any]] = {
    "off": {
        "report": {},
        "orders": {},
    },
    "standard": {
        "report": {
            "min_items": 8,
            "min_distinct_categories": 3,
            "min_regional_items": 1,
            "max_duplicates_ratio": 0.90,
            "min_items_if_collect_failed": 12,
        },
        "orders": {
            "min_orders": 2,
            "min_unique_buckets": 2,
            "min_unique_instruments": 2,
            "max_single_order_share_pct": 80.0,
        },
    },
    "strict": {
        "report": {
            "min_items": 12,
            "min_distinct_categories": 4,
            "min_regional_items": 2,
            "max_duplicates_ratio": 0.80,
            "min_items_if_collect_failed": 16,
        },
        "orders": {
            "min_orders": 3,
            "min_unique_buckets": 3,
            "min_unique_instruments": 3,
            "max_single_order_share_pct": 65.0,
        },
    },
}
_INT_RE = re.compile(r"^-?\d+$")
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
    execution_guardrails_path: str = DEFAULT_EXECUTION_GUARDRAILS_PATH,
    enable_execution_guardrails: bool = True,
    execute_ibkr_contracts_path: str = DEFAULT_EXECUTE_IBKR_CONTRACTS_PATH,
    execute_ibkr_host: str = DEFAULT_EXECUTE_IBKR_HOST,
    execute_ibkr_port: int = DEFAULT_EXECUTE_IBKR_PORT,
    execute_ibkr_client_id: int = DEFAULT_EXECUTE_IBKR_CLIENT_ID,
    execute_ibkr_timeout_sec: float = DEFAULT_EXECUTE_IBKR_TIMEOUT_SEC,
    execute_ibkr_what_if: bool = False,
    execute_ibkr_limit_buffer_pct: float = DEFAULT_EXECUTE_IBKR_LIMIT_BUFFER_PCT,
    skip_execution: bool = False,
    resume: bool = False,
    quality_gate_profile: str = DEFAULT_QUALITY_GATE_PROFILE,
) -> dict[str, Any]:
    quality_profile = _normalize_quality_gate_profile(quality_gate_profile)
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
            quality_gate_profile=quality_profile,
        ),
    )
    artifacts.update(
        {
            "weekly_report_path": report_out.get("report_path"),
            "weekly_aggregates_path": str(weekly_aggregates_path),
            "report_quality_gate": report_out.get("quality_gate"),
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
            quality_gate_profile=quality_profile,
        ),
    )
    artifacts.update(
        {
            "orders_path": propose_out.get("orders_path"),
            "orders_report_path": propose_out.get("report_path"),
            "orders_quality_gate": propose_out.get("quality_gate"),
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
            execution_guardrails_path=execution_guardrails_path,
            enable_execution_guardrails=enable_execution_guardrails,
            execute_ibkr_contracts_path=execute_ibkr_contracts_path,
            execute_ibkr_host=execute_ibkr_host,
            execute_ibkr_port=execute_ibkr_port,
            execute_ibkr_client_id=execute_ibkr_client_id,
            execute_ibkr_timeout_sec=execute_ibkr_timeout_sec,
            execute_ibkr_what_if=execute_ibkr_what_if,
            execute_ibkr_limit_buffer_pct=execute_ibkr_limit_buffer_pct,
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
    quality_gate_profile: str = DEFAULT_QUALITY_GATE_PROFILE,
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
    quality_gate = _evaluate_report_quality(
        profile=quality_gate_profile,
        week=week,
        report_path=report_path,
        weekly_aggregates_path=weekly_aggregates_path,
    )
    return {
        "report_path": str(report_path),
        "weekly_aggregates_path": str(weekly_aggregates_path),
        "quality_gate": quality_gate,
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
    quality_gate_profile: str = DEFAULT_QUALITY_GATE_PROFILE,
) -> dict[str, Any]:
    out_orders, out_report, payload = propose_monthly_orders(
        month=month,
        policy_path=str(policy_path),
        orders_dir=str(orders_dir),
        reports_dir=str(report_dir),
    )
    quality_gate = _evaluate_orders_quality(
        profile=quality_gate_profile,
        payload=payload,
    )
    return {
        "orders_path": str(out_orders),
        "report_path": str(out_report),
        "orders_count": len(payload.get("orders") or []),
        "budget_eur": int(payload.get("budget_eur") or 0),
        "quality_gate": quality_gate,
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
    execution_guardrails_path: str,
    enable_execution_guardrails: bool,
    execute_ibkr_contracts_path: str,
    execute_ibkr_host: str,
    execute_ibkr_port: int,
    execute_ibkr_client_id: int,
    execute_ibkr_timeout_sec: float,
    execute_ibkr_what_if: bool,
    execute_ibkr_limit_buffer_pct: float,
    root: Path,
    output_path: Path,
) -> dict[str, Any]:
    result = execute_order_proposal(
        proposal_path=str(proposal_path),
        broker=execute_broker,
        mock_state_path=str(root / "data/broker/mock_state.json"),
        ibkr_state_path=str(root / "data/broker/ibkr_state.json"),
        dry_run=execute_dry_run,
        output_path=str(output_path),
        guardrails_path=execution_guardrails_path,
        enable_guardrails=enable_execution_guardrails,
        ibkr_contracts_path=execute_ibkr_contracts_path,
        ibkr_host=execute_ibkr_host,
        ibkr_port=execute_ibkr_port,
        ibkr_client_id=execute_ibkr_client_id,
        ibkr_timeout_sec=execute_ibkr_timeout_sec,
        ibkr_what_if=execute_ibkr_what_if,
        ibkr_limit_buffer_pct=execute_ibkr_limit_buffer_pct,
    )
    return {
        "output_path": str(output_path),
        "submitted_count": int(result.get("submitted_count") or 0),
        "skipped_count": int(result.get("skipped_count") or 0),
        "dry_run": bool(result.get("dry_run")),
        "what_if": bool(result.get("what_if")),
        "guardrails_enabled": bool((result.get("guardrails") or {}).get("enabled")),
        "guardrails_passed": bool((result.get("guardrails") or {}).get("passed")),
        "month": month,
    }


def _normalize_quality_gate_profile(profile: str) -> str:
    value = str(profile or DEFAULT_QUALITY_GATE_PROFILE).strip().lower()
    if value not in QUALITY_GATE_PROFILES:
        raise ValueError(f"quality_gate_profile must be one of {sorted(QUALITY_GATE_PROFILES)}.")
    return value


def _evaluate_report_quality(
    *,
    profile: str,
    week: str,
    report_path: Path,
    weekly_aggregates_path: Path,
) -> dict[str, Any]:
    normalized = _normalize_quality_gate_profile(profile)
    thresholds = dict((QUALITY_GATE_PROFILES.get(normalized) or {}).get("report") or {})

    report_meta = _read_report_metadata(report_path)
    aggregate_row = _read_weekly_aggregate(path=weekly_aggregates_path, week=week)
    category_counts = dict(aggregate_row.get("category_counts") or {})

    item_count = _as_int(report_meta.get("number_of_items_considered"))
    if item_count is None:
        item_count = _as_int(aggregate_row.get("item_count")) or 0

    duplicates_removed = _as_int(report_meta.get("duplicates_removed_count"))
    if duplicates_removed is None:
        duplicates_removed = _as_int(aggregate_row.get("duplicates_removed_count")) or 0

    korea_items = _as_int(report_meta.get("korea_items_count")) or 0
    germany_items = _as_int(report_meta.get("germany_items_count")) or 0
    regional_items = korea_items + germany_items
    distinct_categories = sum(1 for value in category_counts.values() if _as_int(value) and int(value) > 0)
    denominator = max(1, item_count + duplicates_removed)
    duplicates_ratio = float(duplicates_removed) / float(denominator)
    rss_collect_status = str(report_meta.get("rss_collect_status") or "").strip().lower()

    metrics = {
        "item_count": int(item_count),
        "duplicates_removed_count": int(duplicates_removed),
        "distinct_categories": int(distinct_categories),
        "korea_items_count": int(korea_items),
        "germany_items_count": int(germany_items),
        "regional_items_count": int(regional_items),
        "duplicates_ratio": round(duplicates_ratio, 4),
        "rss_collect_status": rss_collect_status or "unknown",
    }

    if normalized == "off":
        return {
            "profile": normalized,
            "passed": True,
            "thresholds": thresholds,
            "metrics": metrics,
            "violations": [],
        }

    violations: list[str] = []
    min_items = int(thresholds.get("min_items") or 0)
    min_distinct_categories = int(thresholds.get("min_distinct_categories") or 0)
    min_regional_items = int(thresholds.get("min_regional_items") or 0)
    max_duplicates_ratio = float(thresholds.get("max_duplicates_ratio") or 1.0)
    min_items_if_collect_failed = int(thresholds.get("min_items_if_collect_failed") or min_items)

    if item_count < min_items:
        violations.append(f"item_count {item_count} < min_items {min_items}")
    if distinct_categories < min_distinct_categories:
        violations.append(
            f"distinct_categories {distinct_categories} < min_distinct_categories {min_distinct_categories}"
        )
    if regional_items < min_regional_items:
        violations.append(f"regional_items_count {regional_items} < min_regional_items {min_regional_items}")
    if duplicates_ratio > max_duplicates_ratio:
        violations.append(
            f"duplicates_ratio {duplicates_ratio:.4f} > max_duplicates_ratio {max_duplicates_ratio:.4f}"
        )
    if rss_collect_status == "failed" and item_count < min_items_if_collect_failed:
        violations.append(
            "rss_collect_status failed requires "
            f"item_count >= {min_items_if_collect_failed}, got {item_count}"
        )

    if violations:
        raise ValueError(
            "Report quality gate failed "
            f"(profile={normalized}): " + "; ".join(violations)
        )
    return {
        "profile": normalized,
        "passed": True,
        "thresholds": thresholds,
        "metrics": metrics,
        "violations": [],
    }


def _evaluate_orders_quality(*, profile: str, payload: dict[str, Any]) -> dict[str, Any]:
    normalized = _normalize_quality_gate_profile(profile)
    thresholds = dict((QUALITY_GATE_PROFILES.get(normalized) or {}).get("orders") or {})

    orders_raw = payload.get("orders") or []
    orders = [row for row in orders_raw if isinstance(row, dict)]
    budget = _as_int(payload.get("budget_eur")) or 0
    amounts = [_as_int(row.get("amount_eur")) or 0 for row in orders]
    orders_count = len(orders)
    total_amount = sum(amounts)
    unique_buckets = len({str(row.get("bucket") or "").strip() for row in orders if str(row.get("bucket") or "").strip()})
    unique_instruments = len(
        {
            str(row.get("instrument_id") or row.get("isin") or row.get("name") or "").strip()
            for row in orders
            if str(row.get("instrument_id") or row.get("isin") or row.get("name") or "").strip()
        }
    )
    max_single_order_share_pct = 0.0
    if budget > 0 and amounts:
        max_single_order_share_pct = 100.0 * (max(amounts) / float(budget))

    metrics = {
        "orders_count": orders_count,
        "budget_eur": budget,
        "total_order_amount_eur": total_amount,
        "unique_buckets": unique_buckets,
        "unique_instruments": unique_instruments,
        "max_single_order_share_pct": round(max_single_order_share_pct, 2),
    }

    if normalized == "off":
        return {
            "profile": normalized,
            "passed": True,
            "thresholds": thresholds,
            "metrics": metrics,
            "violations": [],
        }

    violations: list[str] = []
    min_orders = int(thresholds.get("min_orders") or 0)
    min_unique_buckets = int(thresholds.get("min_unique_buckets") or 0)
    min_unique_instruments = int(thresholds.get("min_unique_instruments") or 0)
    max_single_order_share_threshold = float(thresholds.get("max_single_order_share_pct") or 100.0)

    if orders_count < min_orders:
        violations.append(f"orders_count {orders_count} < min_orders {min_orders}")
    if unique_buckets < min_unique_buckets:
        violations.append(f"unique_buckets {unique_buckets} < min_unique_buckets {min_unique_buckets}")
    if unique_instruments < min_unique_instruments:
        violations.append(
            f"unique_instruments {unique_instruments} < min_unique_instruments {min_unique_instruments}"
        )
    if max_single_order_share_pct > max_single_order_share_threshold:
        violations.append(
            "max_single_order_share_pct "
            f"{max_single_order_share_pct:.2f} > {max_single_order_share_threshold:.2f}"
        )
    if budget <= 0:
        violations.append(f"budget_eur must be positive, got {budget}")
    if total_amount != budget:
        violations.append(f"total_order_amount_eur {total_amount} != budget_eur {budget}")
    if any(amount <= 0 for amount in amounts):
        violations.append("all orders must have positive amount_eur")

    if violations:
        raise ValueError(
            "Orders quality gate failed "
            f"(profile={normalized}): " + "; ".join(violations)
        )
    return {
        "profile": normalized,
        "passed": True,
        "thresholds": thresholds,
        "metrics": metrics,
        "violations": [],
    }


def _read_report_metadata(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return {}

    metadata: dict[str, Any] = {}
    in_header = False
    for line in lines:
        if line.startswith("- "):
            in_header = True
            body = line[2:]
            if ":" not in body:
                continue
            key, value = body.split(":", 1)
            metadata[key.strip()] = _parse_scalar(value.strip())
            continue
        if in_header:
            if not line.strip():
                break
            break
    return metadata


def _read_weekly_aggregate(path: Path, week: str) -> dict[str, Any]:
    rows = read_jsonl(path)
    for row in rows:
        row_week = str(row.get("week") or "").strip()
        if row_week == week:
            return row
    return {}


def _parse_scalar(value: str) -> Any:
    text = str(value).strip()
    if _INT_RE.match(text):
        try:
            return int(text)
        except ValueError:
            return text
    return text


def _as_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not _INT_RE.match(text):
        return None
    try:
        return int(text)
    except ValueError:
        return None


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
