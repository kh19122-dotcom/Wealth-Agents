from __future__ import annotations

from datetime import datetime, timezone
from html import escape
import json
from pathlib import Path
from typing import Any


DEFAULT_RUNS_DIR = "runs"
DEFAULT_STATE_PATH = "runs/scheduler_state.json"
DEFAULT_OUTPUT_PATH = "runs/dashboard.html"
DEFAULT_PREFLIGHT_DIR = "reports"
DEFAULT_LIMIT = 20
DEFAULT_TITLE = "Wealth Agents Dashboard"


def build_dashboard(
    *,
    runs_dir: str = DEFAULT_RUNS_DIR,
    state_path: str = DEFAULT_STATE_PATH,
    output_path: str = DEFAULT_OUTPUT_PATH,
    preflight_dir: str | None = DEFAULT_PREFLIGHT_DIR,
    limit: int = DEFAULT_LIMIT,
    title: str = DEFAULT_TITLE,
) -> Path:
    if limit <= 0:
        raise ValueError("limit must be > 0.")

    runs_root = Path(runs_dir)
    state_file = Path(state_path)
    output_file = Path(output_path)

    cycles = _load_cycle_rows(runs_root, limit=limit)
    scheduler_state = _load_scheduler_state(state_file)
    preflight_state = _load_latest_preflight_summary(
        preflight_dir=(Path(preflight_dir) if preflight_dir else None),
        runs_root=runs_root,
    )

    html = _render_dashboard(
        title=title,
        generated_at=_now_iso8601(),
        runs_root=runs_root,
        state_file=state_file,
        cycles=cycles,
        scheduler_state=scheduler_state,
        preflight_state=preflight_state,
        limit=limit,
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(html, encoding="utf-8")
    return output_file


def _load_cycle_rows(runs_root: Path, *, limit: int) -> list[dict[str, Any]]:
    if not runs_root.exists():
        return []

    rows: list[dict[str, Any]] = []
    for child in runs_root.iterdir():
        if not child.is_dir():
            continue
        checkpoint_path = child / "checkpoint.json"
        if not checkpoint_path.exists():
            continue
        payload = _read_json_dict(checkpoint_path)
        if payload is None:
            continue
        rows.append(_normalize_cycle_row(payload=payload, checkpoint_path=checkpoint_path))

    rows.sort(
        key=lambda row: _parse_iso8601(row.get("updated_at")) or datetime.fromtimestamp(0, tz=timezone.utc),
        reverse=True,
    )
    return rows[:limit]


def _normalize_cycle_row(*, payload: dict[str, Any], checkpoint_path: Path) -> dict[str, Any]:
    steps = payload.get("steps")
    step_counts = _step_status_counts(steps if isinstance(steps, dict) else {})
    artifacts = payload.get("artifacts")
    artifact_map = artifacts if isinstance(artifacts, dict) else {}
    status = str(payload.get("status") or "unknown")

    report_gate = _quality_gate_summary(artifact_map.get("report_quality_gate"))
    orders_gate = _quality_gate_summary(artifact_map.get("orders_quality_gate"))
    execution_guardrail = _execution_guardrail_summary(
        steps=steps if isinstance(steps, dict) else {},
        checkpoint_path=checkpoint_path,
    )
    error_obj = payload.get("error")
    if isinstance(error_obj, dict):
        error_text = str(error_obj.get("message") or "")
    else:
        error_text = ""

    return {
        "cycle_id": str(payload.get("cycle_id") or checkpoint_path.parent.name),
        "status": status,
        "started_at": str(payload.get("started_at") or ""),
        "finished_at": str(payload.get("finished_at") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
        "step_counts": step_counts,
        "step_total": len(steps) if isinstance(steps, dict) else 0,
        "checkpoint_path": str(checkpoint_path),
        "weekly_report_path": str(artifact_map.get("weekly_report_path") or ""),
        "orders_path": str(artifact_map.get("orders_path") or ""),
        "simulation_path": str(artifact_map.get("simulation_path") or ""),
        "report_gate": report_gate,
        "orders_gate": orders_gate,
        "execution_guardrail": execution_guardrail,
        "error": error_text,
    }


def _step_status_counts(steps: dict[str, Any]) -> dict[str, int]:
    counts: dict[str, int] = {
        "completed": 0,
        "running": 0,
        "failed": 0,
        "other": 0,
    }
    for row in steps.values():
        if not isinstance(row, dict):
            counts["other"] += 1
            continue
        status = str(row.get("status") or "").strip().lower()
        if status in counts:
            counts[status] += 1
        else:
            counts["other"] += 1
    return counts


def _quality_gate_summary(raw: Any) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {
            "profile": "",
            "passed": None,
            "metrics": {},
            "available": False,
        }
    metrics = raw.get("metrics")
    return {
        "profile": str(raw.get("profile") or ""),
        "passed": bool(raw.get("passed")) if "passed" in raw else None,
        "metrics": metrics if isinstance(metrics, dict) else {},
        "available": True,
    }


def _execution_guardrail_summary(*, steps: dict[str, Any], checkpoint_path: Path) -> dict[str, Any]:
    step_row = steps.get("execute_orders")
    if not isinstance(step_row, dict):
        return {
            "available": False,
            "enabled": None,
            "passed": None,
            "dry_run": None,
            "submitted_count": None,
            "skipped_count": None,
            "skipped": False,
        }
    output = step_row.get("output")
    if not isinstance(output, dict):
        return {
            "available": False,
            "enabled": None,
            "passed": None,
            "dry_run": None,
            "submitted_count": None,
            "skipped_count": None,
            "skipped": False,
        }

    skipped = bool(output.get("skipped"))
    enabled = _parse_optional_bool(output.get("guardrails_enabled"))
    passed = _parse_optional_bool(output.get("guardrails_passed"))
    guardrails = output.get("guardrails")
    if isinstance(guardrails, dict):
        if enabled is None:
            enabled = _parse_optional_bool(guardrails.get("enabled"))
        if passed is None:
            passed = _parse_optional_bool(guardrails.get("passed"))

    output_path_value = str(output.get("output_path") or "")
    output_path = Path(output_path_value) if output_path_value else None
    if output_path and not output_path.is_absolute():
        output_path = checkpoint_path.parent / output_path
    if output_path and output_path.exists() and (enabled is None or passed is None):
        execution_payload = _read_json_dict(output_path)
        if isinstance(execution_payload, dict):
            payload_guardrails = execution_payload.get("guardrails")
            if isinstance(payload_guardrails, dict):
                if enabled is None:
                    enabled = _parse_optional_bool(payload_guardrails.get("enabled"))
                if passed is None:
                    passed = _parse_optional_bool(payload_guardrails.get("passed"))

    submitted_count = _as_int_or_none(output.get("submitted_count"))
    skipped_count = _as_int_or_none(output.get("skipped_count"))
    dry_run = _parse_optional_bool(output.get("dry_run"))
    available = skipped or enabled is not None or passed is not None
    return {
        "available": available,
        "enabled": enabled,
        "passed": passed,
        "dry_run": dry_run,
        "submitted_count": submitted_count,
        "skipped_count": skipped_count,
        "skipped": skipped,
    }


def _load_scheduler_state(path: Path) -> dict[str, Any]:
    payload = _read_json_dict(path)
    if payload is None:
        return {"available": False, "last_completed": {}, "runs": []}
    last_completed = payload.get("last_completed")
    runs = payload.get("runs")
    return {
        "available": True,
        "last_completed": last_completed if isinstance(last_completed, dict) else {},
        "runs": runs if isinstance(runs, list) else [],
        "updated_at": str(payload.get("updated_at") or ""),
    }


def _load_latest_preflight_summary(*, preflight_dir: Path | None, runs_root: Path) -> dict[str, Any]:
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _collect(pattern_root: Path) -> None:
        if not pattern_root.exists() or not pattern_root.is_dir():
            return
        for path in pattern_root.glob("ibkr_preflight_*.md"):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(path)

    if preflight_dir is not None:
        _collect(preflight_dir)
    _collect(runs_root / "reports")
    if runs_root.exists() and runs_root.is_dir():
        for path in runs_root.glob("*/reports/ibkr_preflight_*.md"):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            candidates.append(path)

    if not candidates:
        return {
            "available": False,
            "reason": "No IBKR preflight reports found.",
            "path": "",
            "passed": None,
            "tickers_checked": None,
            "failed_count": None,
            "status_counts": {},
            "started_at": "",
            "finished_at": "",
        }

    latest = max(candidates, key=lambda path: (_safe_mtime(path), path.name))
    parsed = _parse_preflight_summary(latest)
    if parsed is None:
        return {
            "available": False,
            "reason": f"Unable to parse preflight summary: {latest}",
            "path": str(latest),
            "passed": None,
            "tickers_checked": None,
            "failed_count": None,
            "status_counts": {},
            "started_at": "",
            "finished_at": "",
        }
    parsed["available"] = True
    parsed["reason"] = ""
    return parsed


def _parse_preflight_summary(path: Path) -> dict[str, Any] | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    values: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line.startswith("- "):
            continue
        key, sep, value = line[2:].partition(":")
        if not sep:
            continue
        values[key.strip()] = value.strip()

    started_at = str(values.get("started_at") or "")
    finished_at = str(values.get("finished_at") or "")
    passed = _parse_optional_bool(values.get("passed"))
    tickers_checked = _as_int_or_none(values.get("tickers_checked"))
    failed_count = _as_int_or_none(values.get("failed_count"))
    status_counts: dict[str, int] = {}
    for key, value in values.items():
        if not key.startswith("status_"):
            continue
        parsed = _as_int_or_none(value)
        if parsed is None:
            continue
        status_counts[key[len("status_") :]] = parsed

    has_summary = (
        bool(started_at)
        or bool(finished_at)
        or passed is not None
        or tickers_checked is not None
        or failed_count is not None
        or bool(status_counts)
    )
    if not has_summary:
        return None
    return {
        "path": str(path),
        "passed": passed,
        "tickers_checked": tickers_checked,
        "failed_count": failed_count,
        "status_counts": status_counts,
        "started_at": started_at,
        "finished_at": finished_at,
    }


def _render_dashboard(
    *,
    title: str,
    generated_at: str,
    runs_root: Path,
    state_file: Path,
    cycles: list[dict[str, Any]],
    scheduler_state: dict[str, Any],
    preflight_state: dict[str, Any],
    limit: int,
) -> str:
    total = len(cycles)
    completed = sum(1 for row in cycles if row["status"] == "completed")
    failed = sum(1 for row in cycles if row["status"] == "failed")
    running = sum(1 for row in cycles if row["status"] == "running")

    lines: list[str] = []
    lines.append("<!doctype html>")
    lines.append("<html lang='en'>")
    lines.append("<head>")
    lines.append("  <meta charset='utf-8' />")
    lines.append("  <meta name='viewport' content='width=device-width, initial-scale=1' />")
    lines.append(f"  <title>{escape(title)}</title>")
    lines.append("  <style>")
    lines.append("    :root {")
    lines.append("      --bg: #f6f7f9;")
    lines.append("      --panel: #ffffff;")
    lines.append("      --ink: #1f2937;")
    lines.append("      --muted: #6b7280;")
    lines.append("      --border: #d1d5db;")
    lines.append("      --ok: #0f766e;")
    lines.append("      --fail: #b42318;")
    lines.append("      --run: #1d4ed8;")
    lines.append("    }")
    lines.append("    body { margin: 0; font-family: 'Helvetica Neue', Arial, sans-serif; background: var(--bg); color: var(--ink); }")
    lines.append("    .wrap { max-width: 1200px; margin: 0 auto; padding: 20px; }")
    lines.append("    .panel { background: var(--panel); border: 1px solid var(--border); border-radius: 12px; padding: 16px; margin-bottom: 14px; }")
    lines.append("    h1, h2 { margin: 0 0 10px; }")
    lines.append("    .meta { color: var(--muted); font-size: 13px; margin-bottom: 8px; }")
    lines.append("    .grid { display: grid; grid-template-columns: repeat(4, minmax(120px, 1fr)); gap: 10px; }")
    lines.append("    .kpi { border: 1px solid var(--border); border-radius: 10px; padding: 10px; background: #f9fafb; }")
    lines.append("    .kpi .v { font-size: 22px; font-weight: 700; }")
    lines.append("    .kpi .k { color: var(--muted); font-size: 12px; }")
    lines.append("    table { width: 100%; border-collapse: collapse; font-size: 13px; }")
    lines.append("    th, td { border-bottom: 1px solid var(--border); text-align: left; padding: 8px; vertical-align: top; }")
    lines.append("    th { color: var(--muted); font-weight: 600; }")
    lines.append("    .status-completed { color: var(--ok); font-weight: 700; }")
    lines.append("    .status-failed { color: var(--fail); font-weight: 700; }")
    lines.append("    .status-running { color: var(--run); font-weight: 700; }")
    lines.append("    .mono { font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, 'Liberation Mono', 'Courier New', monospace; }")
    lines.append("    .small { color: var(--muted); font-size: 12px; }")
    lines.append("    .tag { display: inline-block; border: 1px solid var(--border); border-radius: 999px; padding: 2px 8px; margin-right: 6px; margin-bottom: 4px; }")
    lines.append("    @media (max-width: 840px) { .grid { grid-template-columns: repeat(2, minmax(120px, 1fr)); } }")
    lines.append("  </style>")
    lines.append("</head>")
    lines.append("<body>")
    lines.append("  <div class='wrap'>")
    lines.append(f"    <h1>{escape(title)}</h1>")
    lines.append(f"    <div class='meta'>generated_at={escape(generated_at)} | runs_dir={escape(str(runs_root))} | state={escape(str(state_file))}</div>")
    lines.append("    <div class='panel'>")
    lines.append("      <div class='grid'>")
    lines.append(f"        <div class='kpi'><div class='v'>{total}</div><div class='k'>cycles shown (limit {limit})</div></div>")
    lines.append(f"        <div class='kpi'><div class='v'>{completed}</div><div class='k'>completed</div></div>")
    lines.append(f"        <div class='kpi'><div class='v'>{failed}</div><div class='k'>failed</div></div>")
    lines.append(f"        <div class='kpi'><div class='v'>{running}</div><div class='k'>running</div></div>")
    lines.append("      </div>")
    lines.append("    </div>")

    lines.append("    <div class='panel'>")
    lines.append("      <h2>Cycles</h2>")
    if not cycles:
        lines.append("      <div class='small'>No cycles found.</div>")
    else:
        lines.append("      <table>")
        lines.append("        <thead><tr><th>cycle_id</th><th>status</th><th>steps</th><th>quality</th><th>updated_at</th><th>artifacts</th><th>error</th></tr></thead>")
        lines.append("        <tbody>")
        for row in cycles:
            status = escape(str(row["status"]))
            status_class = f"status-{status.lower()}" if status.lower() in {"completed", "failed", "running"} else ""
            steps = row["step_counts"]
            step_text = (
                f"done={steps['completed']} run={steps['running']} fail={steps['failed']} "
                f"other={steps['other']} total={row['step_total']}"
            )
            report_gate = _render_gate_cell(row["report_gate"], label="report")
            orders_gate = _render_gate_cell(row["orders_gate"], label="orders")
            execution_guardrail = _render_execution_guardrail_cell(row["execution_guardrail"])
            artifact_bits: list[str] = [f"<div class='mono small'>{escape(str(row['checkpoint_path']))}</div>"]
            for key in ("weekly_report_path", "orders_path", "simulation_path"):
                value = str(row.get(key) or "")
                if value:
                    artifact_bits.append(f"<div class='mono small'>{escape(value)}</div>")
            error_text = escape(str(row.get("error") or ""))

            lines.append("          <tr>")
            lines.append(f"            <td class='mono'>{escape(str(row['cycle_id']))}</td>")
            lines.append(f"            <td><span class='{status_class}'>{status}</span></td>")
            lines.append(f"            <td class='mono'>{escape(step_text)}</td>")
            lines.append(f"            <td>{report_gate}<br/>{orders_gate}<br/>{execution_guardrail}</td>")
            lines.append(f"            <td class='mono small'>{escape(str(row.get('updated_at') or ''))}</td>")
            lines.append(f"            <td>{''.join(artifact_bits)}</td>")
            lines.append(f"            <td class='small'>{error_text}</td>")
            lines.append("          </tr>")
        lines.append("        </tbody>")
        lines.append("      </table>")
    lines.append("    </div>")

    lines.append("    <div class='panel'>")
    lines.append("      <h2>IBKR Preflight</h2>")
    if not bool(preflight_state.get("available")):
        lines.append(f"      <div class='small'>{escape(str(preflight_state.get('reason') or 'No preflight summary available.'))}</div>")
    else:
        passed = preflight_state.get("passed")
        if passed is True:
            pass_label = "pass"
            pass_class = "status-completed"
        elif passed is False:
            pass_label = "fail"
            pass_class = "status-failed"
        else:
            pass_label = "n/a"
            pass_class = ""
        lines.append("      <div>")
        lines.append(f"        <span class='tag'>status: <span class='{pass_class}'>{escape(pass_label)}</span></span>")
        tickers_checked = _as_int_or_none(preflight_state.get("tickers_checked"))
        if tickers_checked is not None:
            lines.append(f"        <span class='tag mono'>tickers_checked: {tickers_checked}</span>")
        failed_count = _as_int_or_none(preflight_state.get("failed_count"))
        if failed_count is not None:
            lines.append(f"        <span class='tag mono'>failed_count: {failed_count}</span>")
        finished_at = str(preflight_state.get("finished_at") or "")
        if finished_at:
            lines.append(f"        <span class='tag mono'>finished_at: {escape(finished_at)}</span>")
        lines.append("      </div>")
        status_counts = preflight_state.get("status_counts")
        if isinstance(status_counts, dict) and status_counts:
            lines.append("      <div>")
            for status_key, count in sorted(status_counts.items()):
                lines.append(f"        <span class='tag mono'>status_{escape(str(status_key))}: {escape(str(count))}</span>")
            lines.append("      </div>")
        lines.append(f"      <div class='mono small'>{escape(str(preflight_state.get('path') or ''))}</div>")
    lines.append("    </div>")

    lines.append("    <div class='panel'>")
    lines.append("      <h2>Scheduler</h2>")
    if not bool(scheduler_state.get("available")):
        lines.append("      <div class='small'>Scheduler state file not found.</div>")
    else:
        last_completed = scheduler_state.get("last_completed") or {}
        if isinstance(last_completed, dict) and last_completed:
            lines.append("      <div>")
            for cadence, period_key in sorted(last_completed.items()):
                lines.append(
                    f"        <span class='tag mono'>{escape(str(cadence))}: {escape(str(period_key))}</span>"
                )
            lines.append("      </div>")
        else:
            lines.append("      <div class='small'>No completed periods recorded yet.</div>")

        runs = scheduler_state.get("runs") or []
        if not isinstance(runs, list) or not runs:
            lines.append("      <div class='small'>No scheduler runs recorded yet.</div>")
        else:
            lines.append("      <table>")
            lines.append("        <thead><tr><th>ran_at</th><th>cadence</th><th>period</th><th>status</th><th>cycle_id</th><th>error</th></tr></thead>")
            lines.append("        <tbody>")
            for run in reversed(runs[-20:]):
                if not isinstance(run, dict):
                    continue
                status_value = str(run.get("status") or "")
                status_class = (
                    "status-failed"
                    if status_value == "failed"
                    else ("status-completed" if status_value == "completed" else "")
                )
                lines.append("          <tr>")
                lines.append(f"            <td class='mono small'>{escape(str(run.get('ran_at') or ''))}</td>")
                lines.append(f"            <td class='mono'>{escape(str(run.get('cadence') or ''))}</td>")
                lines.append(f"            <td class='mono'>{escape(str(run.get('period_key') or ''))}</td>")
                lines.append(f"            <td><span class='{status_class}'>{escape(status_value)}</span></td>")
                lines.append(f"            <td class='mono'>{escape(str(run.get('cycle_id') or ''))}</td>")
                lines.append(f"            <td class='small'>{escape(str(run.get('error') or ''))}</td>")
                lines.append("          </tr>")
            lines.append("        </tbody>")
            lines.append("      </table>")
    lines.append("    </div>")

    lines.append("  </div>")
    lines.append("</body>")
    lines.append("</html>")
    return "\n".join(lines) + "\n"


def _render_gate_cell(gate: dict[str, Any], *, label: str) -> str:
    if not bool(gate.get("available")):
        return f"<div class='small'>{escape(label)} gate: n/a</div>"
    profile = str(gate.get("profile") or "")
    passed = gate.get("passed")
    pass_label = "pass" if passed else "fail"
    metrics = gate.get("metrics") or {}
    if not isinstance(metrics, dict):
        metrics = {}
    compact_metric = _first_metric(metrics)
    metric_text = f" | {compact_metric}" if compact_metric else ""
    return (
        f"<div class='small'>{escape(label)} gate: "
        f"<span class='mono'>{escape(profile)}/{escape(pass_label)}{escape(metric_text)}</span></div>"
    )


def _render_execution_guardrail_cell(summary: dict[str, Any]) -> str:
    if not bool(summary.get("available")):
        return "<div class='small'>exec guardrail: n/a</div>"
    if bool(summary.get("skipped")):
        return "<div class='small'>exec guardrail: <span class='mono'>skipped</span></div>"

    enabled = _parse_optional_bool(summary.get("enabled"))
    passed = _parse_optional_bool(summary.get("passed"))
    if enabled is False:
        base = "off"
    elif enabled is True:
        if passed is True:
            base = "on/pass"
        elif passed is False:
            base = "on/fail"
        else:
            base = "on/unknown"
    else:
        base = "unknown"

    parts = [base]
    dry_run = _parse_optional_bool(summary.get("dry_run"))
    if dry_run is True:
        parts.append("dry_run=1")
    elif dry_run is False:
        parts.append("dry_run=0")
    submitted_count = _as_int_or_none(summary.get("submitted_count"))
    if submitted_count is not None:
        parts.append(f"submitted={submitted_count}")
    skipped_count = _as_int_or_none(summary.get("skipped_count"))
    if skipped_count is not None:
        parts.append(f"skipped={skipped_count}")
    return f"<div class='small'>exec guardrail: <span class='mono'>{escape(' | '.join(parts))}</span></div>"


def _first_metric(metrics: dict[str, Any]) -> str:
    preferred = (
        "item_count",
        "distinct_categories",
        "regional_items_count",
        "orders_count",
        "unique_buckets",
        "max_single_order_share_pct",
    )
    for key in preferred:
        if key in metrics:
            return f"{key}={metrics[key]}"
    for key, value in metrics.items():
        return f"{key}={value}"
    return ""


def _safe_mtime(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def _parse_optional_bool(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        if value == 0:
            return False
        if value == 1:
            return True
    text = str(value or "").strip().lower()
    if text in {"true", "yes", "y", "pass", "passed", "1"}:
        return True
    if text in {"false", "no", "n", "fail", "failed", "0"}:
        return False
    return None


def _as_int_or_none(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return int(float(text)) if "." in text else int(text)
    except ValueError:
        return None


def _read_json_dict(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return payload


def _parse_iso8601(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
