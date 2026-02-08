import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

FAILURE_THRESHOLD = 3
QUARANTINE_DAYS = 7


def load_feed_health(path: str) -> dict:
    meta_path = Path(path)
    if not meta_path.exists():
        return {}
    try:
        loaded = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_feed_health(path: str, data: dict) -> None:
    meta_path = Path(path)
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def parse_iso8601_utc(value: object) -> Optional[datetime]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def default_entry(source: str, url: str) -> dict:
    return {
        "source": source,
        "url": url,
        "status": "ok",
        "last_ok_at": None,
        "last_error_at": None,
        "consecutive_failures": 0,
        "last_error_type": None,
        "quarantined_until": None,
    }


def should_skip_quarantined(entry: dict, now: Optional[datetime] = None) -> bool:
    if not entry:
        return False
    if int(entry.get("consecutive_failures", 0)) < FAILURE_THRESHOLD:
        return False
    until = parse_iso8601_utc(entry.get("quarantined_until"))
    if until is None:
        return False
    current = now or datetime.now(timezone.utc)
    return current < until


def update_entry_on_result(entry: dict, ok: bool, error_type: Optional[str]) -> dict:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    entry = dict(entry)
    if ok:
        entry["status"] = "ok"
        entry["last_ok_at"] = now.isoformat().replace("+00:00", "Z")
        entry["consecutive_failures"] = 0
        entry["last_error_type"] = None
        entry["quarantined_until"] = None
        return entry

    failures = int(entry.get("consecutive_failures", 0)) + 1
    entry["consecutive_failures"] = failures
    entry["last_error_at"] = now.isoformat().replace("+00:00", "Z")
    entry["last_error_type"] = error_type

    if failures >= FAILURE_THRESHOLD:
        entry["status"] = "quarantined"
        until = now + timedelta(days=QUARANTINE_DAYS)
        entry["quarantined_until"] = until.isoformat().replace("+00:00", "Z")
    else:
        entry["status"] = "failing"
    return entry


def format_health_table(health: dict) -> str:
    rows = []
    for source, entry in sorted(health.items()):
        rows.append(
            {
                "source": source,
                "status": str(entry.get("status", "")),
                "failures": str(entry.get("consecutive_failures", 0)),
                "last_error_type": str(entry.get("last_error_type", "")),
                "last_ok_at": str(entry.get("last_ok_at", "")),
                "last_error_at": str(entry.get("last_error_at", "")),
                "quarantined_until": str(entry.get("quarantined_until", "")),
            }
        )

    headers = ["source", "status", "failures", "last_error_type", "last_ok_at", "last_error_at", "quarantined_until"]
    if not rows:
        return "No feed health records found."

    widths = {header: len(header) for header in headers}
    for row in rows:
        for header in headers:
            widths[header] = max(widths[header], len(row[header]))

    def _fmt(values: dict) -> str:
        return " | ".join(values[h].ljust(widths[h]) for h in headers)

    header_row = _fmt({h: h for h in headers})
    separator = "-+-".join("-" * widths[h] for h in headers)
    lines = [header_row, separator]
    for row in rows:
        lines.append(_fmt(row))
    return "\n".join(lines)
