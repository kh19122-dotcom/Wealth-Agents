import logging
import os
import socket
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config_loader import load_config
from .feed_health import (
    default_entry,
    load_feed_health,
    save_feed_health,
    should_skip_quarantined,
    update_entry_on_result,
)

from .utils import now_iso8601, sanitize_html_summary, stable_item_id

logger = logging.getLogger(__name__)

VALID_FEED_STATUSES = {"ok", "http_error", "parse_error", "timeout", "dns_error"}


def _time_struct_to_iso(struct_time: Any) -> Optional[str]:
    if not struct_time:
        return None
    try:
        dt = datetime(*struct_time[:6], tzinfo=timezone.utc)
        return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except Exception:
        return None


def load_feeds_config(path: str) -> list[dict]:
    data = load_config(path)
    feeds = data.get("feeds", [])
    normalized: list[dict] = []

    if isinstance(feeds, list):
        # Backward compatibility: simple feed list is treated as global.
        for item in feeds:
            if not isinstance(item, dict):
                continue
            source = item.get("source") or item.get("name")
            url = item.get("url")
            if not source or not url:
                continue
            tags = list(item.get("tags") or [])
            if not tags:
                tags = ["global"]
            normalized.append(
                {
                    "source": str(source),
                    "url": str(url),
                    "tags": tags,
                }
            )
        return normalized

    if isinstance(feeds, dict):
        for bucket, entries in feeds.items():
            if not isinstance(entries, list):
                continue
            region_tag = _bucket_region_tag(str(bucket))
            for item in entries:
                if not isinstance(item, dict):
                    continue
                source = item.get("source") or item.get("name")
                url = item.get("url")
                if not source or not url:
                    continue
                tags = list(item.get("tags") or [])
                if region_tag not in tags:
                    tags.append(region_tag)
                normalized.append(
                    {
                        "source": str(source),
                        "url": str(url),
                        "tags": tags,
                    }
                )
    return normalized


def fetch_feed_records(feed_url: str, source: str, tags: Optional[list[str]] = None) -> list[dict]:
    records, _ = _fetch_feed_records_with_status(feed_url=feed_url, source=source, tags=tags)
    return records


def _fetch_feed_records_with_status(
    feed_url: str,
    source: str,
    tags: Optional[list[str]] = None,
) -> tuple[list[dict], dict]:
    timeout = float(os.getenv("REQUEST_TIMEOUT", "10"))
    fetched_at = now_iso8601()
    tags = tags or []

    try:
        req = Request(feed_url, headers={"User-Agent": "wealth_agents/0.1"})
        with urlopen(req, timeout=timeout) as response:
            body = response.read()
    except HTTPError as exc:
        logger.warning("Request failed for %s: %s", feed_url, exc)
        etype = "http_error"
        return [], {"ok": False, "status": etype, "error_type": etype, "error_message": str(exc)}
    except URLError as exc:
        logger.warning("Request failed for %s: %s", feed_url, exc)
        etype = _classify_url_error(exc)
        return [], {
            "ok": False,
            "status": etype,
            "error_type": etype,
            "error_message": str(exc),
        }
    except TimeoutError as exc:
        logger.warning("Request timed out for %s: %s", feed_url, exc)
        return [], {"ok": False, "status": "timeout", "error_type": "timeout", "error_message": str(exc)}

    try:
        root = ET.fromstring(body)
    except Exception as exc:
        logger.warning("Feed parse failed for %s: %s", feed_url, exc)
        return [], {"ok": False, "status": "parse_error", "error_type": "parse_error", "error_message": str(exc)}

    records: list[dict] = []
    items = root.findall(".//item")
    if not items:
        items = root.findall(".//{http://www.w3.org/2005/Atom}entry")

    for entry in items:
        title = _find_text(entry, ["title", "{http://www.w3.org/2005/Atom}title"])
        url = _find_link(entry)
        summary_raw = _find_text(
            entry,
            [
                "description",
                "summary",
                "{http://www.w3.org/2005/Atom}summary",
                "{http://www.w3.org/2005/Atom}content",
            ],
        )
        summary = sanitize_html_summary(summary_raw, title_fallback=title)
        published_raw = _find_text(
            entry,
            [
                "pubDate",
                "published",
                "updated",
                "{http://www.w3.org/2005/Atom}published",
                "{http://www.w3.org/2005/Atom}updated",
            ],
        )
        published_at = _parse_published_text(published_raw)
        identifier = stable_item_id(url=url, title=title, published_at=published_at)

        record = {
            "id": identifier,
            "source": source,
            "feed_url": feed_url,
            "title": title,
            "url": url,
            "published_at": published_at,
            "fetched_at": fetched_at,
            "summary": summary,
            "tags": tags,
        }
        records.append(record)

    logger.info("Fetched %s items from %s", len(records), feed_url)
    return records, {"ok": True, "status": "ok", "error_type": None, "error_message": ""}


def collect_from_feeds(config_path: str) -> list[dict]:
    records, _ = collect_from_feeds_with_stats(config_path)
    return records


def collect_from_feeds_with_stats(
    config_path: str,
    health_meta_path: str = "data/meta/feed_health.json",
) -> tuple[list[dict], dict]:
    all_records: list[dict] = []
    health = load_feed_health(health_meta_path)
    stats = {
        "status": "success",
        "timestamp": now_iso8601(),
        "feeds_total": 0,
        "feeds_success": 0,
        # Backward-compatible alias used by some older tests/report fixtures.
        "feeds_ok": 0,
        "feeds_failed": 0,
        "feeds_skipped": 0,
        "fetched": 0,
        "error_type": None,
        "error_samples": [],
        "feed_statuses": [],
    }
    failure_types: dict[str, int] = {}
    for feed in load_feeds_config(config_path):
        stats["feeds_total"] += 1
        source = feed["source"]
        url = feed["url"]
        tags = feed["tags"]
        entry = dict(health.get(source) or default_entry(source, url))
        entry["url"] = url

        if should_skip_quarantined(entry):
            stats["feeds_skipped"] += 1
            entry["status"] = "quarantined"
            health[source] = entry
            skipped_status = _normalize_feed_status(entry.get("last_error_type"))
            stats["feed_statuses"].append(
                {
                    "source": source,
                    "feed_url": url,
                    "tags": list(tags),
                    "status": skipped_status,
                    "skipped": True,
                    "fetched": 0,
                }
            )
            failure_types[skipped_status] = failure_types.get(skipped_status, 0) + 1
            if len(stats["error_samples"]) < 5:
                until = str(entry.get("quarantined_until") or "")
                stats["error_samples"].append(f"{url}: skipped (quarantined until {until})")
            logger.warning(
                "Skipping quarantined feed source=%s until=%s",
                source,
                entry.get("quarantined_until"),
            )
            continue

        start = time.time()
        records, fetch_status = _fetch_feed_records_with_status(feed_url=url, source=source, tags=tags)
        feed_status = _normalize_feed_status(
            fetch_status.get("status") or ("ok" if fetch_status.get("ok") else fetch_status.get("error_type"))
        )
        if feed_status == "ok":
            stats["feeds_success"] += 1
            entry = update_entry_on_result(entry, ok=True, error_type=None)
        else:
            error_type = feed_status
            failure_types[error_type] = failure_types.get(error_type, 0) + 1
            if len(stats["error_samples"]) < 5:
                message = str(fetch_status.get("error_message") or "")
                stats["error_samples"].append(f"{url}: {message}")
            entry = update_entry_on_result(entry, ok=False, error_type=error_type)
        stats["feed_statuses"].append(
            {
                "source": source,
                "feed_url": url,
                "tags": list(tags),
                "status": feed_status,
                "skipped": False,
                "fetched": len(records),
            }
        )
        health[source] = entry
        elapsed = round(time.time() - start, 2)
        logger.info("Processed feed source=%s items=%s elapsed=%ss", source, len(records), elapsed)
        all_records.extend(records)

    stats["fetched"] = len(all_records)
    stats["feeds_ok"] = stats["feeds_success"]
    stats["feeds_failed"] = max(0, int(stats["feeds_total"]) - int(stats["feeds_success"]))
    if stats["feeds_failed"] == 0:
        stats["status"] = "success"
        stats["error_type"] = None
    elif int(stats["feeds_success"]) > 0:
        stats["status"] = "partial"
        stats["error_type"] = max(failure_types.items(), key=lambda row: row[1])[0] if failure_types else "parse_error"
    else:
        stats["status"] = "failed"
        stats["error_type"] = max(failure_types.items(), key=lambda row: row[1])[0] if failure_types else "parse_error"
    save_feed_health(health_meta_path, health)
    return all_records, stats




def _find_text(entry: ET.Element, candidates: list[str]) -> str:
    for name in candidates:
        node = entry.find(name)
        if node is not None and node.text:
            text = node.text.strip()
            if text:
                return text
    return ""


def _find_link(entry: ET.Element) -> str:
    link = _find_text(entry, ["link", "{http://www.w3.org/2005/Atom}link"])
    if link:
        return link

    atom_link = entry.find("{http://www.w3.org/2005/Atom}link")
    if atom_link is not None:
        href = atom_link.attrib.get("href", "").strip()
        if href:
            return href
    return ""


def _parse_published_text(value: str) -> Optional[str]:
    if not value:
        return None

    text = value.strip()
    try:
        if "," in text:
            dt = datetime.strptime(text, "%a, %d %b %Y %H:%M:%S %z")
            return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except ValueError:
        pass

    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    except ValueError:
        return None


def _bucket_region_tag(bucket: str) -> str:
    lowered = bucket.strip().lower()
    if lowered.startswith("korea"):
        return "korea"
    if lowered.startswith("germany"):
        return "germany"
    if lowered.startswith("global"):
        return "global"
    return lowered


def _classify_url_error(exc: URLError) -> str:
    reason = exc.reason
    if isinstance(reason, socket.gaierror):
        return "dns_error"
    if isinstance(reason, socket.timeout):
        return "timeout"
    reason_text = str(reason).lower()
    if "timed out" in reason_text or "timeout" in reason_text:
        return "timeout"
    if "nodename nor servname" in reason_text or "name or service not known" in reason_text:
        return "dns_error"
    return "parse_error"


def _normalize_feed_status(value: object) -> str:
    raw = str(value or "").strip().lower()
    if raw in VALID_FEED_STATUSES:
        return raw
    if raw in {"http", "http_error", "http_403", "http_404", "http_429", "http_500"}:
        return "http_error"
    if raw in {"dns", "dns_error"}:
        return "dns_error"
    if raw in {"timeout", "timed_out", "timedout"}:
        return "timeout"
    if raw in {"ok", "success"}:
        return "ok"
    return "parse_error"
