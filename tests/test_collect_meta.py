import json
import sys
from pathlib import Path

import yaml

from wealth_agents.cli import main as cli_main
from wealth_agents.report import generate_weekly_report
from wealth_agents.rss import collect_from_feeds_with_stats
from wealth_agents.storage import append_jsonl


def _run_collect(
    monkeypatch,
    config_path: Path,
    output_path: Path,
    meta_path: Path,
    health_path: Path | None = None,
) -> int:
    if health_path is None:
        health_path = meta_path.parent / "feed_health.json"
    argv = [
        "wealth_agents",
        "collect",
        "--config",
        str(config_path),
        "--output",
        str(output_path),
        "--meta-output",
        str(meta_path),
        "--health-meta-output",
        str(health_path),
    ]
    monkeypatch.setattr(sys, "argv", argv)
    return cli_main()


def _write_rules(path: Path) -> None:
    payload = {
        "top_terms": ["inflation", "rate hike"],
        "categories": {
            "macroeconomics": ["inflation"],
            "equities": ["stock"],
            "rates": ["rate hike", "fed"],
            "real_estate": ["housing"],
            "germany": ["germany"],
            "korea": ["korea"],
            "personal_finance": ["savings"],
        },
        "signals": {"rate_pressure": ["rate hike"]},
    }
    path.write_text(yaml.safe_dump(payload, sort_keys=False), encoding="utf-8")


def _write_config(path: Path, feeds: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"feeds": feeds}, sort_keys=False), encoding="utf-8")


def test_collect_writes_last_collect_json_on_success(tmp_path: Path, monkeypatch):
    feed_path = tmp_path / "sample_feed.xml"
    feed_path.write_text(
        "<rss><channel><item>"
        "<title>Local feed item</title>"
        "<link>https://example.com/item</link>"
        "<description>Sample summary</description>"
        "<pubDate>Sat, 07 Feb 2026 10:00:00 +0000</pubDate>"
        "</item></channel></rss>",
        encoding="utf-8",
    )
    config_path = tmp_path / "feeds.yml"
    _write_config(config_path, [{"source": "local_feed", "url": feed_path.as_uri(), "tags": ["global"]}])
    output_path = tmp_path / "data/raw/news.jsonl"
    meta_path = tmp_path / "data/meta/last_collect.json"
    health_path = tmp_path / "data/meta/feed_health.json"

    rc = _run_collect(monkeypatch, config_path, output_path, meta_path, health_path)
    assert rc == 0
    assert meta_path.exists()

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "success"
    assert "timestamp" in meta
    assert meta["feeds_total"] == 1
    assert meta["feeds_success"] == 1
    assert meta["feeds_ok"] == 1
    assert meta["feeds_failed"] == 0
    assert meta["fetched"] == 1
    assert meta["appended"] == 1
    assert meta["error_samples"] == []


def test_collect_writes_last_collect_json_on_failure(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "feeds.yml"
    _write_config(config_path, [{"source": "broken_feed", "url": "https://nonexistent.invalid/rss.xml"}])
    output_path = tmp_path / "data/raw/news.jsonl"
    meta_path = tmp_path / "data/meta/last_collect.json"
    health_path = tmp_path / "data/meta/feed_health.json"

    rc = _run_collect(monkeypatch, config_path, output_path, meta_path, health_path)
    assert rc == 0
    assert meta_path.exists()

    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["status"] == "failed"
    assert meta["feeds_total"] == 1
    assert meta["feeds_success"] == 0
    assert meta["feeds_ok"] == 0
    assert meta["feeds_failed"] == 1
    assert meta["fetched"] == 0
    assert meta["appended"] == 0
    assert meta["error_type"] in {"dns_error", "timeout", "http_error", "parse_error"}
    assert 1 <= len(meta["error_samples"]) <= 5


def test_report_header_includes_collect_status_when_meta_exists(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "h1",
                "source": "manual",
                "feed_url": "inputs/korea",
                "title": "Korea test item",
                "url": "",
                "published_at": None,
                "fetched_at": "2026-02-06T12:00:00Z",
                "summary": "korea inflation signal",
                "tags": ["manual", "korea"],
            }
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    meta_path = tmp_path / "data/meta/last_collect.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "status": "failed",
                "timestamp": "2026-02-07T12:00:00Z",
                "feeds_total": 3,
                "feeds_success": 1,
                "feeds_ok": 1,
                "feeds_failed": 2,
                "fetched": 0,
                "appended": 0,
                "error_type": "dns_error",
                "error_samples": ["https://example.invalid/rss: dns failure"],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
        collect_meta_path=str(meta_path),
    )
    text = report_path.read_text(encoding="utf-8")
    assert "- rss_collect_status: partial" in text
    assert "- feeds_success_total: 1/3" in text
    assert "- feeds_failed_total: 2/3" in text
    assert "rss_collect_note: last collect failed and fetched 0 items." not in text


def test_collect_counts_two_success_one_failure_by_feed_status(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "feeds.yml"
    _write_config(
        config_path,
        [
            {"source": "ok_a", "url": "https://example.com/a.xml"},
            {"source": "ok_b", "url": "https://example.com/b.xml"},
            {"source": "bad_c", "url": "https://example.com/c.xml"},
        ],
    )
    health_path = tmp_path / "data/meta/feed_health.json"

    def _fake_fetch(feed_url: str, source: str, tags=None):
        if source == "bad_c":
            return [], {"ok": False, "status": "dns_error", "error_type": "dns_error", "error_message": "dns fail"}
        return (
            [
                {
                    "id": f"{source}-id",
                    "source": source,
                    "feed_url": feed_url,
                    "title": f"{source} title",
                    "url": f"https://example.com/{source}",
                    "published_at": None,
                    "fetched_at": "2026-02-07T12:00:00Z",
                    "summary": "summary",
                    "tags": [],
                }
            ],
            {"ok": True, "status": "ok", "error_type": None, "error_message": ""},
        )

    monkeypatch.setattr("wealth_agents.rss._fetch_feed_records_with_status", _fake_fetch)
    _, stats = collect_from_feeds_with_stats(str(config_path), health_meta_path=str(health_path))
    assert stats["feeds_total"] == 3
    assert stats["feeds_success"] == 2
    assert stats["feeds_failed"] == 1
    assert stats["feeds_ok"] == 2
    assert stats["status"] == "partial"
    assert stats["fetched"] == 2
    assert len(stats["feed_statuses"]) == 3
    assert all(isinstance(row.get("tags"), list) for row in stats["feed_statuses"])


def test_collect_appended_zero_does_not_change_success_count(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "feeds.yml"
    _write_config(config_path, [{"source": "ok_feed", "url": "https://example.com/ok.xml"}])
    output_path = tmp_path / "data/raw/news.jsonl"
    meta_path = tmp_path / "data/meta/last_collect.json"
    health_path = tmp_path / "data/meta/feed_health.json"

    def _fake_fetch(feed_url: str, source: str, tags=None):
        return (
            [
                {
                    "id": "stable-dup-id",
                    "source": source,
                    "feed_url": feed_url,
                    "title": "Duplicate story",
                    "url": "https://example.com/story",
                    "published_at": "2026-02-07T10:00:00Z",
                    "fetched_at": "2026-02-07T12:00:00Z",
                    "summary": "same summary",
                    "tags": ["global"],
                }
            ],
            {"ok": True, "status": "ok", "error_type": None, "error_message": ""},
        )

    monkeypatch.setattr("wealth_agents.rss._fetch_feed_records_with_status", _fake_fetch)

    first_rc = _run_collect(monkeypatch, config_path, output_path, meta_path, health_path)
    assert first_rc == 0
    first_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert first_meta["feeds_success"] == 1
    assert first_meta["appended"] == 1

    second_rc = _run_collect(monkeypatch, config_path, output_path, meta_path, health_path)
    assert second_rc == 0
    second_meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert second_meta["feeds_success"] == 1
    assert second_meta["feeds_failed"] == 0
    assert second_meta["appended"] == 0
