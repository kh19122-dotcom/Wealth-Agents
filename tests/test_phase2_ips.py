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
