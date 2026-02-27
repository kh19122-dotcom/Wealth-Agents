import json
from pathlib import Path

import pytest
import yaml

from wealth_agents.ips import draft_policy, finalize_policy, init_ips_files
from wealth_agents.policy import stable_policy_hash, validate_allocation_sum


def _write_inputs(path: Path, overrides: dict | None = None) -> None:
    base = {
        "base_currency": "EUR",
        "investable_amount_eur": 40000,
        "cash_buffer_eur": 10000,
        "horizon_years": 12,
        "risk_tolerance": "medium",
        "contribution_plan": {"type": "lump_sum_split", "months": 12},
        "allowed_assets": {
            "equities": True,
            "bonds_or_cashlike": True,
            "gold_optional": True,
        },
        "constraints": {
            "no_leverage": True,
            "no_short": True,
            "no_crypto": True,
            "sell_allowed": False,
        },
        "rebalance": {"frequency": "quarterly", "band_pct": 5},
    }
    if overrides:
        for key, val in overrides.items():
            if isinstance(val, dict) and isinstance(base.get(key), dict):
                base[key].update(val)
            else:
                base[key] = val
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(base, sort_keys=False), encoding="utf-8")


def _write_weekly_aggregates(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _write_simulation_payload(path: Path, stats: dict) -> None:
    payload = {
        "start": "2024-01",
        "end": "2025-12",
        "stats": stats,
        "snapshots": [],
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _bucket_pct(candidate: dict, bucket: str) -> int:
    for row in candidate.get("target_allocation") or []:
        if str(row.get("bucket") or "").strip() == bucket:
            return int(row.get("pct"))
    return 0


def test_ips_init_creates_expected_files_with_valid_yaml(tmp_path: Path):
    input_path = tmp_path / "data/policy/ips_inputs.yml"
    questions_path = tmp_path / "reports/ips_questions.md"

    out_input, out_questions = init_ips_files(str(input_path), str(questions_path))

    assert out_input.exists()
    assert out_questions.exists()
    loaded = yaml.safe_load(out_input.read_text(encoding="utf-8"))
    assert isinstance(loaded, dict)
    assert loaded["base_currency"] == "EUR"
    assert "contribution_plan" in loaded
    assert "allowed_assets" in loaded


def test_ips_draft_produces_three_candidates_with_100_sum(tmp_path: Path):
    input_path = tmp_path / "data/policy/ips_inputs.yml"
    _write_inputs(input_path)

    draft_path, report_path = draft_policy(
        input_path=str(input_path),
        draft_path=str(tmp_path / "data/policy/policy_draft.yml"),
        report_dir=str(tmp_path / "reports"),
    )

    assert draft_path.exists()
    assert report_path.exists()
    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    candidates = draft["candidates"]
    assert set(candidates.keys()) == {"conservative", "balanced", "aggressive"}
    for candidate in candidates.values():
        total = sum(float(row["pct"]) for row in candidate["target_allocation"])
        assert abs(total - 100.0) < 1e-6


def test_ips_finalize_writes_policy_and_history(tmp_path: Path):
    input_path = tmp_path / "data/policy/ips_inputs.yml"
    _write_inputs(input_path)
    draft_path, _ = draft_policy(
        input_path=str(input_path),
        draft_path=str(tmp_path / "data/policy/policy_draft.yml"),
        report_dir=str(tmp_path / "reports"),
    )

    policy_path = tmp_path / "data/policy/policy.yml"
    history_path = tmp_path / "data/policy/policy_history.jsonl"
    out_policy, out_hash = finalize_policy(
        choice="balanced",
        draft_path=str(draft_path),
        policy_path=str(policy_path),
        history_path=str(history_path),
    )

    assert out_policy.exists()
    policy = yaml.safe_load(out_policy.read_text(encoding="utf-8"))
    assert policy["selected_candidate"] == "balanced"
    assert policy["policy_hash"] == out_hash

    lines = history_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["choice"] == "balanced"
    assert entry["policy_hash"] == out_hash


def test_policy_hash_stable_across_runs_for_same_inputs(tmp_path: Path):
    input_path = tmp_path / "data/policy/ips_inputs.yml"
    _write_inputs(input_path)
    draft_path, _ = draft_policy(
        input_path=str(input_path),
        draft_path=str(tmp_path / "data/policy/policy_draft.yml"),
        report_dir=str(tmp_path / "reports"),
    )

    p1, h1 = finalize_policy(
        choice="balanced",
        draft_path=str(draft_path),
        policy_path=str(tmp_path / "data/policy/policy_1.yml"),
        history_path=str(tmp_path / "data/policy/history_1.jsonl"),
    )
    p2, h2 = finalize_policy(
        choice="balanced",
        draft_path=str(draft_path),
        policy_path=str(tmp_path / "data/policy/policy_2.yml"),
        history_path=str(tmp_path / "data/policy/history_2.jsonl"),
    )

    assert p1.exists() and p2.exists()
    assert h1 == h2

    policy1 = yaml.safe_load(p1.read_text(encoding="utf-8"))
    policy2 = yaml.safe_load(p2.read_text(encoding="utf-8"))
    basis1 = {
        "selected_candidate": policy1["selected_candidate"],
        "inputs_snapshot": policy1["inputs_snapshot"],
        "policy": policy1["policy"],
    }
    basis2 = {
        "selected_candidate": policy2["selected_candidate"],
        "inputs_snapshot": policy2["inputs_snapshot"],
        "policy": policy2["policy"],
    }
    assert stable_policy_hash(basis1) == stable_policy_hash(basis2) == h1


def test_band_pct_and_allocation_sum_validation(tmp_path: Path):
    input_path = tmp_path / "data/policy/ips_inputs.yml"
    _write_inputs(input_path, overrides={"rebalance": {"band_pct": 25}})
    with pytest.raises(ValueError, match="band_pct"):
        draft_policy(
            input_path=str(input_path),
            draft_path=str(tmp_path / "data/policy/policy_draft.yml"),
            report_dir=str(tmp_path / "reports"),
        )

    with pytest.raises(ValueError, match="sum to 100"):
        validate_allocation_sum(
            [
                {"bucket": "global_equity", "pct": 60},
                {"bucket": "bonds_cashlike", "pct": 35},
            ]
        )


def test_invalid_risk_tolerance_fails_with_helpful_error(tmp_path: Path):
    input_path = tmp_path / "data/policy/ips_inputs.yml"
    _write_inputs(input_path, overrides={"risk_tolerance": "ultra"})

    with pytest.raises(ValueError, match="risk_tolerance"):
        draft_policy(
            input_path=str(input_path),
            draft_path=str(tmp_path / "data/policy/policy_draft.yml"),
            report_dir=str(tmp_path / "reports"),
        )


def test_ips_draft_applies_risk_off_signal_overlay_with_persistence(tmp_path: Path):
    input_path = tmp_path / "data/policy/ips_inputs.yml"
    _write_inputs(input_path)
    aggregates_path = tmp_path / "data/meta/weekly_aggregates.jsonl"
    _write_weekly_aggregates(
        aggregates_path,
        [
            {
                "week": "2026-W05",
                "item_count": 20,
                "category_counts": {"macroeconomics": 4, "rates": 3, "equities": 1},
                "keyword_counts": {"inflation": 2, "rate hike": 1, "growth": 0, "earnings": 0},
            },
            {
                "week": "2026-W06",
                "item_count": 24,
                "category_counts": {"macroeconomics": 6, "rates": 5, "equities": 1},
                "keyword_counts": {"inflation": 4, "rate hike": 2, "recession": 1},
            },
        ],
    )

    draft_path, _ = draft_policy(
        input_path=str(input_path),
        draft_path=str(tmp_path / "data/policy/policy_draft.yml"),
        report_dir=str(tmp_path / "reports"),
        weekly_aggregates_path=str(aggregates_path),
        week="2026-W06",
        max_signal_tilt_pct=5,
    )
    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    overlay = draft.get("signal_overlay") or {}
    assert overlay.get("state") == "risk_off"
    assert int(overlay.get("tilt_pct") or 0) == 5

    balanced = draft["candidates"]["balanced"]
    assert _bucket_pct(balanced, "global_equity") == 55
    assert _bucket_pct(balanced, "bonds_cashlike") == 40
    assert _bucket_pct(balanced, "optional_gold") == 5
    assert "signal_overlay" in balanced.get("notes", {})


def test_ips_draft_bootstrap_risk_on_overlay_is_capped(tmp_path: Path):
    input_path = tmp_path / "data/policy/ips_inputs.yml"
    _write_inputs(input_path)
    aggregates_path = tmp_path / "data/meta/weekly_aggregates.jsonl"
    _write_weekly_aggregates(
        aggregates_path,
        [
            {
                "week": "2026-W06",
                "item_count": 30,
                "category_counts": {"equities": 10, "macroeconomics": 1, "rates": 1},
                "keyword_counts": {"growth": 6, "earnings": 5, "rate cut": 2},
            }
        ],
    )

    draft_path, _ = draft_policy(
        input_path=str(input_path),
        draft_path=str(tmp_path / "data/policy/policy_draft.yml"),
        report_dir=str(tmp_path / "reports"),
        weekly_aggregates_path=str(aggregates_path),
        week="2026-W06",
        max_signal_tilt_pct=5,
    )
    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    overlay = draft.get("signal_overlay") or {}
    assert overlay.get("state") == "risk_on"
    # Bootstrap mode (no previous week) caps tilt to 2pp for stability.
    assert int(overlay.get("tilt_pct") or 0) == 2

    balanced = draft["candidates"]["balanced"]
    assert _bucket_pct(balanced, "global_equity") == 62
    assert _bucket_pct(balanced, "bonds_cashlike") == 33
    assert _bucket_pct(balanced, "optional_gold") == 5


def test_finalize_policy_carries_signal_overlay_context(tmp_path: Path):
    input_path = tmp_path / "data/policy/ips_inputs.yml"
    _write_inputs(input_path)
    aggregates_path = tmp_path / "data/meta/weekly_aggregates.jsonl"
    _write_weekly_aggregates(
        aggregates_path,
        [
            {
                "week": "2026-W05",
                "item_count": 22,
                "category_counts": {"macroeconomics": 3, "rates": 2, "equities": 1},
                "keyword_counts": {"inflation": 2, "rate hike": 1},
            },
            {
                "week": "2026-W06",
                "item_count": 25,
                "category_counts": {"macroeconomics": 5, "rates": 4, "equities": 1},
                "keyword_counts": {"inflation": 4, "rate hike": 2, "recession": 1},
            },
        ],
    )
    draft_path, _ = draft_policy(
        input_path=str(input_path),
        draft_path=str(tmp_path / "data/policy/policy_draft.yml"),
        report_dir=str(tmp_path / "reports"),
        weekly_aggregates_path=str(aggregates_path),
        week="2026-W06",
        max_signal_tilt_pct=5,
    )

    out_policy, _ = finalize_policy(
        choice="balanced",
        draft_path=str(draft_path),
        policy_path=str(tmp_path / "data/policy/policy.yml"),
        history_path=str(tmp_path / "data/policy/policy_history.jsonl"),
    )
    policy_doc = yaml.safe_load(out_policy.read_text(encoding="utf-8"))
    overlay = policy_doc.get("signal_overlay") or {}
    assert overlay.get("state") == "risk_off"
    assert overlay.get("week") == "2026-W06"
    assert int(overlay.get("tilt_pct") or 0) == 5
    assert isinstance(overlay.get("drivers"), list)


def test_ips_draft_caps_tilt_from_simulation_feedback_risk_guardrails(tmp_path: Path):
    input_path = tmp_path / "data/policy/ips_inputs.yml"
    _write_inputs(input_path)
    aggregates_path = tmp_path / "data/meta/weekly_aggregates.jsonl"
    _write_weekly_aggregates(
        aggregates_path,
        [
            {
                "week": "2026-W05",
                "item_count": 20,
                "category_counts": {"macroeconomics": 4, "rates": 3, "equities": 1},
                "keyword_counts": {"inflation": 2, "rate hike": 1},
            },
            {
                "week": "2026-W06",
                "item_count": 24,
                "category_counts": {"macroeconomics": 6, "rates": 5, "equities": 1},
                "keyword_counts": {"inflation": 4, "rate hike": 2, "recession": 1},
            },
        ],
    )
    sim_path = tmp_path / "sim/portfolio_2024-01_2025-12.json"
    _write_simulation_payload(
        sim_path,
        {
            "cagr": 0.04,
            "annualized_volatility": 0.27,
            "max_drawdown": -0.21,
        },
    )

    draft_path, _ = draft_policy(
        input_path=str(input_path),
        draft_path=str(tmp_path / "data/policy/policy_draft.yml"),
        report_dir=str(tmp_path / "reports"),
        weekly_aggregates_path=str(aggregates_path),
        week="2026-W06",
        max_signal_tilt_pct=5,
        simulation_feedback_path=str(sim_path),
    )
    draft = yaml.safe_load(draft_path.read_text(encoding="utf-8"))
    overlay = draft.get("signal_overlay") or {}
    calibration = overlay.get("calibration") or {}

    assert overlay.get("state") == "risk_off"
    assert int(calibration.get("requested_max_tilt_pct") or 0) == 5
    assert int(calibration.get("effective_max_tilt_pct") or 0) == 2
    assert int(overlay.get("tilt_pct") or 0) == 2
    balanced = draft["candidates"]["balanced"]
    assert _bucket_pct(balanced, "global_equity") == 58
    assert _bucket_pct(balanced, "bonds_cashlike") == 37
