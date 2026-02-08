import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from .storage import append_jsonl


def read_yaml(path: str) -> dict[str, Any]:
    loaded = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if loaded is None:
        return {}
    if not isinstance(loaded, dict):
        raise ValueError(f"Expected YAML object at root in {path}")
    return loaded


def write_yaml(path: str, data: dict[str, Any]) -> Path:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    rendered = yaml.safe_dump(
        data,
        sort_keys=False,
        default_flow_style=False,
        allow_unicode=False,
    )
    out_path.write_text(rendered, encoding="utf-8")
    return out_path


def normalized_yaml(data: dict[str, Any]) -> str:
    # Canonical representation used for stable hashing across runs.
    rendered = yaml.safe_dump(
        data,
        sort_keys=True,
        default_flow_style=False,
        allow_unicode=False,
    )
    return rendered.strip() + "\n"


def stable_policy_hash(data: dict[str, Any]) -> str:
    return hashlib.sha256(normalized_yaml(data).encode("utf-8")).hexdigest()


def validate_band_pct(value: Any) -> float:
    try:
        band = float(value)
    except (TypeError, ValueError):
        raise ValueError("rebalance.band_pct must be a number in (0, 20].")
    if band <= 0 or band > 20:
        raise ValueError("rebalance.band_pct must be in the range (0, 20].")
    return band


def validate_allocation_sum(target_allocation: list[dict[str, Any]]) -> None:
    total = 0.0
    for bucket in target_allocation:
        try:
            total += float(bucket["pct"])
        except (KeyError, TypeError, ValueError):
            raise ValueError("Each target allocation entry must include numeric pct.")
    if abs(total - 100.0) > 1e-6:
        raise ValueError(f"Target allocation must sum to 100, found {total}.")


def append_policy_history(path: str, entry: dict[str, Any]) -> None:
    append_jsonl(path, [entry])


def to_jsonable_copy(data: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(data))
