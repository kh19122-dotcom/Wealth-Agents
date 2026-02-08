import json
from typing import Any


def load_config(path: str) -> dict[str, Any]:
    text = open(path, "r", encoding="utf-8").read()

    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(text)
        return loaded or {}
    except Exception:
        pass

    try:
        loaded = json.loads(text)
        if isinstance(loaded, dict):
            return loaded
    except json.JSONDecodeError:
        pass

    raise ValueError(f"Unable to parse config file as YAML or JSON: {path}")
