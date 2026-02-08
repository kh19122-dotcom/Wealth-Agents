import json
import logging
from pathlib import Path
from typing import Any, Iterable, Union

logger = logging.getLogger(__name__)


def ensure_parent(path: Union[Path, str]) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)


def read_jsonl(path: Union[Path, str]) -> list[dict]:
    path = Path(path)
    if not path.exists():
        return []

    rows: list[dict] = []
    with path.open("r", encoding="utf-8") as handle:
        for idx, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
                if isinstance(parsed, dict):
                    rows.append(parsed)
            except json.JSONDecodeError:
                logger.warning("Skipping invalid JSONL line %s in %s", idx, path)
    return rows


def append_jsonl(path: Union[Path, str], records: Iterable[dict]) -> int:
    path = Path(path)
    ensure_parent(path)
    count = 0
    with path.open("a", encoding="utf-8") as handle:
        for record in records:
            sanitized = sanitize_record(record)
            handle.write(json.dumps(sanitized, ensure_ascii=False) + "\n")
            count += 1
    return count


def sanitize_record(record: dict) -> dict:
    if not isinstance(record, dict):
        raise TypeError(f"append_jsonl expects dict records, got {type(record).__name__}")
    return _sanitize_json_value(record)


def _sanitize_json_value(value: Any) -> Any:
    if isinstance(value, str):
        return _sanitize_string(value)
    if isinstance(value, list):
        return [_sanitize_json_value(item) for item in value]
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            normalized_key = _sanitize_string(key) if isinstance(key, str) else key
            cleaned[normalized_key] = _sanitize_json_value(item)
        return cleaned
    return value


def _sanitize_string(value: str) -> str:
    normalized = value.replace("\r\n", "\n").replace("\r", "\n")
    # Replace invalid unicode/surrogate sequences with safe placeholders.
    return normalized.encode("utf-8", errors="replace").decode("utf-8", errors="replace")


def validate_jsonl(path: Union[Path, str]) -> int:
    data_path = Path(path)
    if not data_path.exists():
        return 0

    valid_lines = 0
    with data_path.open("r", encoding="utf-8") as handle:
        for idx, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line:
                continue
            try:
                parsed = json.loads(line)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Invalid JSONL in {data_path} at line {idx}: {exc.msg}"
                ) from exc
            if not isinstance(parsed, dict):
                raise RuntimeError(
                    f"Invalid JSONL in {data_path} at line {idx}: expected object, got {type(parsed).__name__}"
                )
            valid_lines += 1
    return valid_lines


def load_existing_ids(path: Union[Path, str]) -> set[str]:
    ids: set[str] = set()
    for item in read_jsonl(path):
        identifier = item.get("id")
        if identifier:
            ids.add(str(identifier))
    return ids


def append_unique_records(path: Union[Path, str], records: Iterable[dict]) -> int:
    path = Path(path)
    existing_ids = load_existing_ids(path)
    unique: list[dict] = []

    for item in records:
        identifier = str(item.get("id", ""))
        if not identifier or identifier in existing_ids:
            continue
        existing_ids.add(identifier)
        unique.append(item)

    appended = append_jsonl(path, unique)
    logger.info("Appended %s new records to %s", appended, path)
    return appended
