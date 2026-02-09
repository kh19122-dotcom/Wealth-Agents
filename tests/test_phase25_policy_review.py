import json
from pathlib import Path

import yaml

from wealth_agents.policy_review import review_policy


def _write_policy(path: Path) -> None:
    payload = {
        "policy_version": "2026-02-08",
        "created_at": "2026-02-08T21:15:14Z",
        "policy_hash": "phase25-policy-hash",
        "policy": {
            "target_allocation": [
                {"bucket": "global_equity", "pct": 60},
                {"bucket": "bonds_cashlike", "pct": 35},
                {"bucket": "optional_gold", "pct": 5},
            ]
        },
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _risk_off_aggregates() -> list[dict]:
    return [
        {
            "week": "2026-W04",
            "category_counts": {"macroeconomics": 0, "rates": 0, "real_estate": 0},
            "keyword_counts": {"inflation": 0, "rate hike": 0},
        },
        {
            "week": "2026-W05",
            "category_counts": {"macroeconomics": 2, "rates": 1, "real_estate": 0},
            "keyword_counts": {"inflation": 1, "rate hike": 0},
        },
        {
            "week": "2026-W06",
            "category_counts": {"macroeconomics": 4, "rates": 1, "real_estate": 0},
            "keyword_counts": {"inflation": 1, "rate hike": 1},
        },
    ]


def test_policy_review_generates_proposal_even_when_apply_locked_by_same_quarter_history(tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    aggregates_path = tmp_path / "data/meta/weekly_aggregates.jsonl"
    apply_history_path = tmp_path / "data/policy/policy_apply_history.jsonl"
    _write_policy(policy_path)
    _write_jsonl(aggregates_path, _risk_off_aggregates())
    _write_jsonl(
        apply_history_path,
        [
            {
                "applied_at": "2026-02-05T12:00:00Z",
                "week": "2026-W06",
                "base_policy_hash": "x",
                "new_policy_hash": "y",
                "proposal_id": "risk_off_shift_5pp",
            }
        ],
    )

    result = review_policy(
        week="2026-W06",
        policy_path=str(policy_path),
        weekly_aggregates_path=str(aggregates_path),
        report_dir=str(tmp_path / "reports"),
        patch_dir=str(tmp_path / "data/policy"),
        apply_history_path=str(apply_history_path),
    )

    assert result.report_path.exists()
    assert result.patch_path.exists()
    assert result.proposal_generated is True
    assert result.apply_guardrail.allowed is False
    assert len(result.patch_payload["proposals"]) == 1
    assert result.patch_payload["proposals"][0]["changes"] == [
        {"bucket": "global_equity", "delta_pct": -5},
        {"bucket": "bonds_cashlike", "delta_pct": +5},
    ]

    report_text = result.report_path.read_text(encoding="utf-8")
    assert "proposal_allowed: YES" in report_text
    assert "proposal_generated: YES" in report_text
    assert "apply_allowed: NO" in report_text
    assert "trigger_path: two_week_persistence" in report_text
    assert "last_apply_date: 2026-02-05" in report_text
    assert "last_apply_week: 2026-W06" in report_text


def test_policy_review_without_apply_history_marks_apply_allowed_yes(tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    aggregates_path = tmp_path / "data/meta/weekly_aggregates.jsonl"
    _write_policy(policy_path)
    _write_jsonl(aggregates_path, _risk_off_aggregates())

    result = review_policy(
        week="2026-W06",
        policy_path=str(policy_path),
        weekly_aggregates_path=str(aggregates_path),
        report_dir=str(tmp_path / "reports"),
        patch_dir=str(tmp_path / "data/policy"),
        apply_history_path=str(tmp_path / "data/policy/policy_apply_history.jsonl"),
    )

    assert result.proposal_generated is True
    assert result.apply_guardrail.allowed is True
    assert len(result.patch_payload["proposals"]) == 1

    report_text = result.report_path.read_text(encoding="utf-8")
    assert "proposal_allowed: YES" in report_text
    assert "proposal_generated: YES" in report_text
    assert "apply_allowed: YES" in report_text
    assert "trigger_path: two_week_persistence" in report_text


def test_policy_review_no_prev_and_bootstrap_not_met_has_no_proposal(tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    aggregates_path = tmp_path / "data/meta/weekly_aggregates.jsonl"
    _write_policy(policy_path)
    _write_jsonl(
        aggregates_path,
        [
            {
                "week": "2026-W06",
                "category_counts": {"macroeconomics": 2},
                "keyword_counts": {"inflation": 2, "rate hike": 0},
            }
        ],
    )

    result = review_policy(
        week="2026-W06",
        policy_path=str(policy_path),
        weekly_aggregates_path=str(aggregates_path),
        report_dir=str(tmp_path / "reports"),
        patch_dir=str(tmp_path / "data/policy"),
        apply_history_path=str(tmp_path / "data/policy/policy_apply_history.jsonl"),
    )

    assert result.proposal_generated is False
    assert result.patch_payload["proposals"] == []
    report_text = result.report_path.read_text(encoding="utf-8")
    assert "trigger_path: none" in report_text
    assert "Insufficient history; bootstrap threshold not met" in report_text


def test_policy_review_no_prev_and_bootstrap_met_generates_defensive_proposal(tmp_path: Path):
    policy_path = tmp_path / "data/policy/policy.yml"
    aggregates_path = tmp_path / "data/meta/weekly_aggregates.jsonl"
    _write_policy(policy_path)
    _write_jsonl(
        aggregates_path,
        [
            {
                "week": "2026-W06",
                "category_counts": {"macroeconomics": 3},
                "keyword_counts": {"inflation": 2, "rate hike": 1},
            }
        ],
    )

    result = review_policy(
        week="2026-W06",
        policy_path=str(policy_path),
        weekly_aggregates_path=str(aggregates_path),
        report_dir=str(tmp_path / "reports"),
        patch_dir=str(tmp_path / "data/policy"),
        apply_history_path=str(tmp_path / "data/policy/policy_apply_history.jsonl"),
    )

    assert result.proposal_generated is True
    proposals = result.patch_payload["proposals"]
    assert len(proposals) == 1
    assert proposals[0]["changes"] == [
        {"bucket": "global_equity", "delta_pct": -5},
        {"bucket": "bonds_cashlike", "delta_pct": +5},
    ]
    report_text = result.report_path.read_text(encoding="utf-8")
    assert "trigger_path: bootstrap_high_score" in report_text
