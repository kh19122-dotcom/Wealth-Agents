import json
import sys
from pathlib import Path

import yaml

from wealth_agents.cli import main as cli_main
from wealth_agents.feed_health import load_feed_health
from wealth_agents.rss import collect_from_feeds_with_stats


def _write_config(path: Path, feeds: list[dict]) -> None:
    path.write_text(yaml.safe_dump({"feeds": feeds}, sort_keys=False), encoding="utf-8")


def test_feed_health_persistence_and_quarantine_skip(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "feeds.yml"
    _write_config(config_path, [{"source": "alpha", "url": "https://alpha.invalid/rss.xml"}])
    health_path = tmp_path / "data/meta/feed_health.json"

    call_count = {"n": 0}

    def _fake_fetch(*args, **kwargs):
        call_count["n"] += 1
        return [], {"ok": False, "error_type": "dns_error", "error_message": "dns fail"}

    monkeypatch.setattr("wealth_agents.rss._fetch_feed_records_with_status", _fake_fetch)

    for _ in range(3):
        _, stats = collect_from_feeds_with_stats(str(config_path), health_meta_path=str(health_path))
        assert stats["feeds_failed"] == 1

    health = load_feed_health(str(health_path))
    entry = health["alpha"]
    assert entry["status"] == "quarantined"
    assert entry["consecutive_failures"] == 3
    assert entry["last_error_type"] == "dns_error"
    assert entry["quarantined_until"] is not None

    # next run should skip fetch while quarantined
    _, stats4 = collect_from_feeds_with_stats(str(config_path), health_meta_path=str(health_path))
    assert stats4["feeds_skipped"] == 1
    assert stats4["feeds_failed"] == 1
    assert call_count["n"] == 3


def test_feed_health_resets_after_success(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "feeds.yml"
    _write_config(config_path, [{"source": "beta", "url": "https://beta.invalid/rss.xml"}])
    health_path = tmp_path / "data/meta/feed_health.json"

    states = [
        ([], {"ok": False, "error_type": "timeout", "error_message": "timed out"}),
        (
            [
                {
                    "id": "x",
                    "source": "beta",
                    "feed_url": "https://beta.invalid/rss.xml",
                    "title": "ok",
                    "url": "https://example.com/ok",
                    "published_at": None,
                    "fetched_at": "2026-02-07T12:00:00Z",
                    "summary": "ok",
                    "tags": [],
                }
            ],
            {"ok": True, "error_type": None, "error_message": ""},
        ),
    ]

    def _fake_fetch(*args, **kwargs):
        return states.pop(0)

    monkeypatch.setattr("wealth_agents.rss._fetch_feed_records_with_status", _fake_fetch)
    collect_from_feeds_with_stats(str(config_path), health_meta_path=str(health_path))
    collect_from_feeds_with_stats(str(config_path), health_meta_path=str(health_path))

    health = load_feed_health(str(health_path))
    entry = health["beta"]
    assert entry["status"] == "ok"
    assert entry["consecutive_failures"] == 0
    assert entry["last_error_type"] is None
    assert entry["last_ok_at"] is not None


def test_cli_feeds_health_prints_table(tmp_path: Path, monkeypatch, capsys):
    meta_path = tmp_path / "data/meta/feed_health.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        json.dumps(
            {
                "alpha": {
                    "source": "alpha",
                    "url": "https://a.example/rss",
                    "status": "quarantined",
                    "last_ok_at": None,
                    "last_error_at": "2026-02-07T12:00:00Z",
                    "consecutive_failures": 3,
                    "last_error_type": "dns_error",
                    "quarantined_until": "2026-02-14T12:00:00Z",
                }
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["wealth_agents", "feeds", "health", "--meta", str(meta_path)],
    )
    rc = cli_main()
    out = capsys.readouterr().out
    assert rc == 0
    assert "source" in out
    assert "alpha" in out
    assert "quarantined" in out
