from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import re
import sys
from typing import Any

from .policy import read_yaml, stable_policy_hash, validate_allocation_sum, write_yaml
from .signals import phase25_risk_off_score
from .storage import append_jsonl, read_jsonl


DEFAULT_POLICY_PATH = "data/policy/policy.yml"
DEFAULT_AGGREGATES_PATH = "data/meta/weekly_aggregates.jsonl"
DEFAULT_REPORTS_DIR = "reports"
DEFAULT_PATCH_DIR = "data/policy"
DEFAULT_APPLY_HISTORY_PATH = "data/policy/policy_apply_history.jsonl"

WEEK_PATTERN = re.compile(r"^\d{4}-W\d{2}$")
RISK_OFF_THIS_WEEK_THRESHOLD = 2
RISK_OFF_PREV_WEEK_THRESHOLD = 1
RISK_OFF_BOOTSTRAP_THRESHOLD = 3
SHIFT_PCT = 5


@dataclass(frozen=True)
class ApplyGuardrail:
    allowed: bool
    reason: str
    last_apply_date: str | None
    last_apply_week: str | None


@dataclass(frozen=True)
class PolicyReviewResult:
    week: str
    report_path: Path
    patch_path: Path
    patch_payload: dict[str, Any]
    proposal_generated: bool
    proposal_reason: str
    trigger_path: str
    apply_guardrail: ApplyGuardrail


def review_policy(
    week: str,
    policy_path: str = DEFAULT_POLICY_PATH,
    weekly_aggregates_path: str = DEFAULT_AGGREGATES_PATH,
    report_dir: str = DEFAULT_REPORTS_DIR,
    patch_dir: str = DEFAULT_PATCH_DIR,
    apply_history_path: str = DEFAULT_APPLY_HISTORY_PATH,
) -> PolicyReviewResult:
    normalized_week, requested_week_start = _validate_week(week)
    policy_doc = read_yaml(policy_path)
    policy_hash = _read_policy_hash(policy_doc, policy_path)
    policy = _read_policy(policy_doc, policy_path)
    allocation_by_bucket = _read_allocation(policy)

    by_week = _load_aggregates(weekly_aggregates_path)
    current = by_week.get(normalized_week)
    if current is None:
        raise ValueError(f"Weekly aggregate not found for week {normalized_week} in {weekly_aggregates_path}.")

    previous_week = _previous_week_key(normalized_week)
    previous = by_week.get(previous_week)
    two_weeks_ago = by_week.get(_previous_week_key(previous_week)) if previous else None

    top_signals = _top_category_signals(current=current, previous=previous)
    apply_guardrail = _apply_guardrail_decision(
        requested_week_start=requested_week_start,
        apply_history_path=apply_history_path,
    )
    this_score = _keyword_risk_score(current)
    prev_score = _keyword_risk_score(previous) if previous else None

    proposal: dict[str, Any] | None = None
    proposal_reason = ""
    trigger_path = "none"

    if previous is None:
        if this_score < RISK_OFF_BOOTSTRAP_THRESHOLD:
            proposal_reason = (
                "Insufficient history; bootstrap threshold not met "
                f"(this_score={this_score}, requires >={RISK_OFF_BOOTSTRAP_THRESHOLD})."
            )
        elif allocation_by_bucket["global_equity"] < SHIFT_PCT:
            proposal_reason = "global_equity allocation is below 5%, cannot apply equity-to-bonds shift."
        else:
            trigger_path = "bootstrap_high_score"
            proposal = _build_shift_proposal(
                trigger_path=trigger_path,
                this_score=this_score,
                prev_score=None,
            )
    elif this_score < RISK_OFF_THIS_WEEK_THRESHOLD or int(prev_score or 0) < RISK_OFF_PREV_WEEK_THRESHOLD:
        proposal_reason = (
            "Two-week risk-off threshold not met "
            f"(this_score={this_score}, prev_score={int(prev_score or 0)}; "
            f"requires this>={RISK_OFF_THIS_WEEK_THRESHOLD}, prev>={RISK_OFF_PREV_WEEK_THRESHOLD})."
        )
    elif allocation_by_bucket["global_equity"] < SHIFT_PCT:
        proposal_reason = "global_equity allocation is below 5%, cannot apply equity-to-bonds shift."
    else:
        trigger_path = "two_week_persistence"
        proposal = _build_shift_proposal(
            trigger_path=trigger_path,
            this_score=this_score,
            prev_score=int(prev_score or 0),
        )

    patch_payload: dict[str, Any] = {
        "week": normalized_week,
        "policy_hash_base": policy_hash,
        "cadence": "quarterly",
        "proposals": [] if proposal is None else [proposal],
    }

    patch_path = Path(patch_dir) / f"policy_patch_{normalized_week}.yml"
    report_path = Path(report_dir) / f"policy_review_{normalized_week}.md"
    write_yaml(str(patch_path), patch_payload)
    _write_report(
        path=report_path,
        week=normalized_week,
        policy_hash=policy_hash,
        top_signals=top_signals,
        apply_guardrail=apply_guardrail,
        allocation_by_bucket=allocation_by_bucket,
        proposal=proposal,
        proposal_reason=proposal_reason,
        trigger_path=trigger_path,
        this_score=this_score,
        prev_score=prev_score,
    )
    return PolicyReviewResult(
        week=normalized_week,
        report_path=report_path,
        patch_path=patch_path,
        patch_payload=patch_payload,
        proposal_generated=proposal is not None,
        proposal_reason=proposal_reason,
        trigger_path=trigger_path,
        apply_guardrail=apply_guardrail,
    )


def apply_review_proposal(
    review_result: PolicyReviewResult,
    policy_path: str = DEFAULT_POLICY_PATH,
    apply_history_path: str = DEFAULT_APPLY_HISTORY_PATH,
    require_yes: bool = False,
) -> tuple[bool, str]:
    proposals = review_result.patch_payload.get("proposals") or []
    if not proposals:
        return False, "No proposals available to apply."

    if not review_result.apply_guardrail.allowed:
        return False, f"Apply blocked by quarterly cadence: {review_result.apply_guardrail.reason}"

    _confirm_apply(require_yes=require_yes)

    proposal = proposals[0]
    proposal_id = str(proposal.get("id") or "proposal")
    changes = proposal.get("changes")
    if not isinstance(changes, list) or not changes:
        raise ValueError("Proposal changes are missing or invalid.")

    policy_doc = read_yaml(policy_path)
    base_policy_hash = _read_policy_hash(policy_doc, policy_path)
    expected_base_hash = str(review_result.patch_payload.get("policy_hash_base") or "").strip()
    if expected_base_hash and expected_base_hash != base_policy_hash:
        raise ValueError(
            "Policy hash mismatch: base policy changed after patch generation. Re-run policy-review before --apply."
        )

    policy = _read_policy(policy_doc, policy_path)
    target_allocation = policy.get("target_allocation")
    if not isinstance(target_allocation, list):
        raise ValueError("policy.target_allocation must be a list.")

    by_bucket: dict[str, dict[str, Any]] = {}
    for row in target_allocation:
        if isinstance(row, dict) and row.get("bucket"):
            by_bucket[str(row["bucket"])] = row

    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("Each proposal change must be a mapping.")
        bucket = str(change.get("bucket") or "").strip()
        if bucket not in by_bucket:
            raise ValueError(f"Proposal bucket '{bucket}' does not exist in policy.target_allocation.")
        try:
            delta_pct = int(change.get("delta_pct"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid delta_pct for bucket '{bucket}'.") from exc

        try:
            current_pct = int(float(by_bucket[bucket].get("pct")))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid current pct for bucket '{bucket}'.") from exc

        updated_pct = current_pct + delta_pct
        if updated_pct < 0:
            raise ValueError(f"Applying proposal would set negative allocation for bucket '{bucket}'.")
        by_bucket[bucket]["pct"] = updated_pct

    validate_allocation_sum(target_allocation)

    now_iso = _now_iso8601()
    policy_doc["policy_version"] = now_iso[:10]
    policy_doc["created_at"] = now_iso
    hash_basis = {
        "selected_candidate": policy_doc.get("selected_candidate"),
        "inputs_snapshot": policy_doc.get("inputs_snapshot"),
        "policy": policy_doc.get("policy"),
    }
    new_policy_hash = stable_policy_hash(hash_basis)
    policy_doc["policy_hash"] = new_policy_hash
    write_yaml(policy_path, policy_doc)

    append_jsonl(
        apply_history_path,
        [
            {
                "applied_at": now_iso,
                "week": review_result.week,
                "base_policy_hash": base_policy_hash,
                "new_policy_hash": new_policy_hash,
                "proposal_id": proposal_id,
            }
        ],
    )
    return True, f"Applied proposal '{proposal_id}' and updated policy_hash to {new_policy_hash}."


def _confirm_apply(require_yes: bool) -> None:
    if require_yes:
        return
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise ValueError("Non-interactive apply requires explicit --yes.")
    response = input("Apply proposal to policy.yml? Type 'yes' to continue: ").strip().lower()
    if response != "yes":
        raise ValueError("Apply cancelled: confirmation was not 'yes'.")


def _validate_week(week: str) -> tuple[str, date]:
    text = str(week).strip()
    if not WEEK_PATTERN.match(text):
        raise ValueError("week must be in ISO format YYYY-Www (e.g., 2026-W06).")
    try:
        year_str, week_str = text.split("-W")
        year = int(year_str)
        iso_week = int(week_str)
        start = date.fromisocalendar(year, iso_week, 1)
    except (TypeError, ValueError) as exc:
        raise ValueError("week must be a valid ISO week in format YYYY-Www.") from exc
    return f"{start.isocalendar().year}-W{start.isocalendar().week:02d}", start


def _read_policy_hash(policy_doc: dict[str, Any], policy_path: str) -> str:
    policy_hash = str(policy_doc.get("policy_hash") or "").strip()
    if not policy_hash:
        raise ValueError(f"Missing top-level policy_hash in {policy_path}.")
    return policy_hash


def _read_policy(policy_doc: dict[str, Any], policy_path: str) -> dict[str, Any]:
    policy = policy_doc.get("policy")
    if not isinstance(policy, dict):
        raise ValueError(f"Missing root policy object in {policy_path}.")
    return policy


def _read_allocation(policy: dict[str, Any]) -> dict[str, int]:
    target_allocation = policy.get("target_allocation")
    if not isinstance(target_allocation, list) or not target_allocation:
        raise ValueError("policy.target_allocation must be a non-empty list.")
    validate_allocation_sum(target_allocation)

    result: dict[str, int] = {}
    for row in target_allocation:
        bucket = str(row.get("bucket") or "").strip()
        if not bucket:
            raise ValueError("Each target_allocation row must include bucket.")
        try:
            pct = int(float(row.get("pct")))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid allocation pct for bucket '{bucket}'.") from exc
        result[bucket] = pct

    for required in ("global_equity", "bonds_cashlike", "optional_gold"):
        if required not in result:
            raise ValueError(f"Required allocation bucket '{required}' not found in policy.target_allocation.")
    return result


def _load_aggregates(path: str) -> dict[str, dict[str, Any]]:
    by_week: dict[str, dict[str, Any]] = {}
    for row in read_jsonl(path):
        week = str(row.get("week") or "").strip()
        if not week:
            continue
        try:
            normalized, _ = _validate_week(week)
        except ValueError:
            continue
        by_week[normalized] = row
    return by_week


def _previous_week_key(week: str) -> str:
    _, start = _validate_week(week)
    previous = start - timedelta(days=7)
    iso = previous.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _top_category_signals(current: dict[str, Any], previous: dict[str, Any] | None) -> list[str]:
    current_counts = _as_int_map(current.get("category_counts"))
    previous_counts = _as_int_map(previous.get("category_counts") if previous else {})

    keys = sorted(set(current_counts) | set(previous_counts))
    scored: list[tuple[int, str]] = []
    for key in keys:
        delta = current_counts.get(key, 0) - previous_counts.get(key, 0)
        scored.append((abs(delta), key))
    scored.sort(key=lambda row: (-row[0], row[1]))

    lines: list[str] = []
    for _, key in scored[:3]:
        current_value = current_counts.get(key, 0)
        previous_value = previous_counts.get(key, 0)
        delta = current_value - previous_value
        lines.append(f"{key}: {current_value} ({delta:+} vs prior week {previous_value})")

    if not lines:
        lines.append("No category data available for this week.")
    while len(lines) < 3:
        lines.append("No additional category delta signal.")
    return lines


def _keyword_risk_score(current: dict[str, Any] | None) -> int:
    return phase25_risk_off_score(current)


def _apply_guardrail_decision(
    requested_week_start: date,
    apply_history_path: str,
) -> ApplyGuardrail:
    last = _load_last_apply_event(apply_history_path)
    requested_label = _quarter_label(requested_week_start)

    if last is None:
        return ApplyGuardrail(
            allowed=True,
            reason="No apply history found; apply is allowed this quarter.",
            last_apply_date=None,
            last_apply_week=None,
        )

    last_dt, last_week = last
    last_date = last_dt.date()
    last_label = _quarter_label(last_date)
    if _quarter_of(last_date) == _quarter_of(requested_week_start):
        return ApplyGuardrail(
            allowed=False,
            reason=(
                f"Quarterly apply lockout: last apply {last_date.isoformat()} "
                f"({last_label}, {last_week}) is in the same quarter as {requested_label}."
            ),
            last_apply_date=last_date.isoformat(),
            last_apply_week=last_week,
        )

    return ApplyGuardrail(
        allowed=True,
        reason=(
            f"Apply allowed: last apply {last_date.isoformat()} "
            f"({last_label}, {last_week}) is outside {requested_label}."
        ),
        last_apply_date=last_date.isoformat(),
        last_apply_week=last_week,
    )


def _load_last_apply_event(path: str) -> tuple[datetime, str] | None:
    latest: tuple[datetime, str] | None = None
    for row in read_jsonl(path):
        parsed_dt = _parse_apply_datetime(row)
        if parsed_dt is None:
            continue
        week = _normalize_week_from_apply_row(row, fallback_date=parsed_dt.date())
        if latest is None or parsed_dt > latest[0]:
            latest = (parsed_dt, week)
    return latest


def _parse_apply_datetime(row: dict[str, Any]) -> datetime | None:
    raw_applied_at = str(row.get("applied_at") or "").strip()
    if raw_applied_at:
        try:
            parsed = datetime.fromisoformat(raw_applied_at.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone(timezone.utc)
        except ValueError:
            pass

    raw_week = str(row.get("week") or "").strip()
    if raw_week:
        try:
            _, week_start = _validate_week(raw_week)
            return datetime.combine(week_start, datetime.min.time(), tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


def _normalize_week_from_apply_row(row: dict[str, Any], fallback_date: date) -> str:
    raw_week = str(row.get("week") or "").strip()
    if raw_week:
        try:
            normalized, _ = _validate_week(raw_week)
            return normalized
        except ValueError:
            pass
    return _date_to_week(fallback_date)


def _build_shift_proposal(
    trigger_path: str,
    this_score: int,
    prev_score: int | None,
) -> dict[str, Any]:
    prev_text = "n/a" if prev_score is None else str(prev_score)
    rationale = [
        (
            f"Trigger path: {trigger_path} "
            f"(this_score={this_score}, prev_score={prev_text})."
        ),
        (
            "Applying deterministic defensive tilt within MVP limits: "
            "global_equity -5pp / bonds_cashlike +5pp; optional_gold unchanged."
        ),
    ]
    return {
        "id": "risk_off_shift_5pp",
        "rationale": rationale,
        "changes": [
            {"bucket": "global_equity", "delta_pct": -5},
            {"bucket": "bonds_cashlike", "delta_pct": +5},
        ],
    }


def _write_report(
    path: Path,
    week: str,
    policy_hash: str,
    top_signals: list[str],
    apply_guardrail: ApplyGuardrail,
    allocation_by_bucket: dict[str, int],
    proposal: dict[str, Any] | None,
    proposal_reason: str,
    trigger_path: str,
    this_score: int,
    prev_score: int | None,
) -> None:
    generated_at = _now_iso8601()
    lines: list[str] = []
    lines.append(f"# Policy Review {week}")
    lines.append("")
    lines.append(f"- week: {week}")
    lines.append(f"- generated_at: {generated_at}")
    lines.append(f"- policy_hash: `{policy_hash}`")
    lines.append("")
    lines.append("## Top Signals (WoW)")
    lines.append("")
    for item in top_signals[:3]:
        lines.append(f"- {item}")
    lines.append("")
    lines.append("## Guardrails Check")
    lines.append("")
    lines.append("- proposal_allowed: YES")
    lines.append(f"- proposal_generated: {'YES' if proposal is not None else 'NO'}")
    lines.append(f"- apply_allowed: {'YES' if apply_guardrail.allowed else 'NO'}")
    lines.append(f"- apply_reason: {apply_guardrail.reason}")
    lines.append(f"- trigger_path: {trigger_path}")
    lines.append(
        f"- risk_off_score: this_week={this_score}, "
        f"prev_week={'n/a' if prev_score is None else int(prev_score)} "
        "(score = keyword_counts['inflation'] + keyword_counts['rate hike'])"
    )
    if apply_guardrail.last_apply_date:
        lines.append(f"- last_apply_date: {apply_guardrail.last_apply_date}")
    if apply_guardrail.last_apply_week:
        lines.append(f"- last_apply_week: {apply_guardrail.last_apply_week}")
    lines.append("")
    lines.append("## Proposals")
    lines.append("")
    if proposal is None:
        lines.append(f"No change proposed. Reason: {proposal_reason}")
    else:
        lines.append(f"- id: {proposal['id']}")
        for reason in proposal["rationale"]:
            lines.append(f"  - rationale: {reason}")
        before_equity = allocation_by_bucket["global_equity"]
        before_bonds = allocation_by_bucket["bonds_cashlike"]
        before_gold = allocation_by_bucket["optional_gold"]
        after_equity = before_equity - SHIFT_PCT
        after_bonds = before_bonds + SHIFT_PCT
        after_gold = before_gold
        lines.append("")
        lines.append("| bucket | before_pct | after_pct |")
        lines.append("|---|---:|---:|")
        lines.append(f"| global_equity | {before_equity} | {after_equity} |")
        lines.append(f"| bonds_cashlike | {before_bonds} | {after_bonds} |")
        lines.append(f"| optional_gold | {before_gold} | {after_gold} |")

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")


def _as_int_map(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        try:
            out[key] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def _quarter_of(value: date) -> tuple[int, int]:
    return value.year, ((value.month - 1) // 3) + 1


def _quarter_label(value: date) -> str:
    year, quarter = _quarter_of(value)
    return f"{year}-Q{quarter}"


def _date_to_week(value: date) -> str:
    iso = value.isocalendar()
    return f"{iso.year}-W{iso.week:02d}"


def _now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
