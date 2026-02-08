import json
from pathlib import Path

from wealth_agents.report import generate_weekly_report, weekly_filename
from wealth_agents.rules import generate_signals, item_categories
from wealth_agents.storage import append_jsonl, append_unique_records, read_jsonl
from wealth_agents.utils import stable_item_id


def _write_minimal_rules(path: Path) -> None:
    path.write_text(
        (
            '{'
            '"top_terms": ["inflation", "rate hike"],'
            '"categories": {'
            '"macroeconomics": ["inflation"],'
            '"equities": ["stock"],'
            '"rates": ["rate"],'
            '"real_estate": ["housing"],'
            '"germany": ["germany"],'
            '"korea": ["korea"],'
            '"personal_finance": ["savings"]'
            '},'
            '"signals": {"rate_pressure": ["rate hike"]}'
            '}'
            "\n"
        ),
        encoding="utf-8",
    )


def test_stable_id_hashing_is_stable_across_runs():
    url = "https://example.com/news?utm_source=x&id=12"
    title = "Inflation slows in January"
    published_at = "2026-02-05T10:00:00Z"

    first = stable_item_id(url=url, title=title, published_at=published_at)
    second = stable_item_id(url=url, title=title, published_at=published_at)
    assert first == second


def test_dedup_prevents_duplicates_when_collect_runs_twice(tmp_path: Path):
    out = tmp_path / "news.jsonl"
    record = {
        "id": "abc123",
        "source": "test_source",
        "feed_url": "https://example.com/rss",
        "title": "Title",
        "url": "https://example.com/1",
        "published_at": "2026-02-01T10:00:00Z",
        "fetched_at": "2026-02-01T10:01:00Z",
        "summary": "summary",
        "tags": [],
    }

    first = append_unique_records(out, [record])
    second = append_unique_records(out, [record])
    rows = read_jsonl(out)

    assert first == 1
    assert second == 0
    assert len(rows) == 1


def test_jsonl_append_preserves_valid_json_per_line(tmp_path: Path):
    out = tmp_path / "items.jsonl"
    records = [{"id": "1", "title": "A"}, {"id": "2", "title": "B"}]
    append_jsonl(out, records)

    raw_lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(raw_lines) == 2
    for line in raw_lines:
        parsed = json.loads(line)
        assert isinstance(parsed, dict)
        assert "id" in parsed


def test_weekly_filename_formatting():
    assert weekly_filename("2026-W06") == "weekly_2026-W06.md"


def test_category_assignment_for_sample_titles():
    rules = {
        "categories": {
            "rates": ["rate hike", "fed"],
            "real_estate": ["housing", "mortgage"],
            "korea": ["korea", "kospi"],
        }
    }

    item = {
        "title": "Fed signals rate hike while Korea housing mortgage stress rises",
        "summary": "",
    }
    cats = item_categories(item, rules)
    assert "rates" in cats
    assert "real_estate" in cats
    assert "korea" in cats


def test_report_generation_with_zero_items_creates_note(tmp_path: Path):
    data_path = tmp_path / "news.jsonl"
    data_path.write_text("", encoding="utf-8")

    rules_path = tmp_path / "rules.yml"
    _write_minimal_rules(rules_path)

    output_dir = tmp_path / "reports"
    week = "2026-W06"

    report_path = generate_weekly_report(
        week=week,
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(output_dir),
    )

    assert report_path.exists()
    text = report_path.read_text(encoding="utf-8")
    assert "No items available for this week." in text
    assert "number_of_items_considered: 0" in text


def test_fetched_at_used_when_published_at_missing_for_week_filter(tmp_path: Path):
    data_path = tmp_path / "news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "x1",
                "source": "src",
                "feed_url": "u",
                "title": "Fed rate hike watch",
                "url": "https://example.com/a",
                "published_at": None,
                "fetched_at": "2026-02-06T10:00:00Z",
                "summary": "rate hike mention",
                "tags": [],
            }
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_minimal_rules(rules_path)

    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    assert "number_of_items_considered: 1" in text
    assert "Fed rate hike watch" in text


def test_week_filter_for_2026_w06_uses_item_date_iso_week(tmp_path: Path):
    data_path = tmp_path / "news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "in-week",
                "source": "src",
                "feed_url": "u",
                "title": "Inflation in week 06",
                "url": "https://example.com/in",
                "published_at": "2026-02-03T09:00:00Z",
                "fetched_at": "2026-02-03T09:05:00Z",
                "summary": "inflation",
                "tags": [],
            },
            {
                "id": "out-week",
                "source": "src",
                "feed_url": "u",
                "title": "Inflation in week 07",
                "url": "https://example.com/out",
                "published_at": "2026-02-09T09:00:00Z",
                "fetched_at": "2026-02-09T09:05:00Z",
                "summary": "inflation",
                "tags": [],
            },
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_minimal_rules(rules_path)

    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    assert "number_of_items_considered: 1" in text
    assert "Inflation in week 06" in text
    assert "Inflation in week 07" not in text


def test_report_with_mixed_published_and_fetched_dates_not_zero(tmp_path: Path):
    data_path = tmp_path / "news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "m1",
                "source": "src",
                "feed_url": "u",
                "title": "Missing published date",
                "url": "https://example.com/m1",
                "published_at": None,
                "fetched_at": "2026-02-04T13:00:00Z",
                "summary": "housing pressure",
                "tags": [],
            },
            {
                "id": "m2",
                "source": "src",
                "feed_url": "u",
                "title": "Has published date",
                "url": "https://example.com/m2",
                "published_at": "2026-02-05T13:00:00Z",
                "fetched_at": "2026-02-05T13:05:00Z",
                "summary": "inflation and rate hike",
                "tags": [],
            },
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_minimal_rules(rules_path)

    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    assert "number_of_items_considered: 2" in text
    assert "No items available for this week." not in text


def test_signals_low_volume_does_not_repeat_generic_line():
    rules = {"signals": {}, "categories": {}}
    signals = generate_signals(items=[], rules=rules)
    assert len(signals) <= 2
    assert signals.count("Signal confidence is limited due to low item volume in this period.") <= 1
