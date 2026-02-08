import json
from pathlib import Path
import re

import pytest
import yaml

from wealth_agents.ingest import ingest_manual_inputs
from wealth_agents.rss import fetch_feed_records
from wealth_agents.report import generate_weekly_report
from wealth_agents.rss import load_feeds_config
from wealth_agents.storage import append_jsonl, read_jsonl
from wealth_agents.utils import canonicalize_url


def _write_rules(path: Path) -> None:
    path.write_text(
        yaml.safe_dump(
            {
                "top_terms": ["inflation", "rate hike", "deepseek", "ai"],
                "categories": {
                    "macroeconomics": ["inflation"],
                    "equities": ["stock", "equity", "ai", "deepseek"],
                    "rates": ["rate hike", "rate cut", "fed"],
                    "real_estate": ["housing"],
                    "germany": ["germany"],
                    "korea": ["korea"],
                    "personal_finance": ["savings"],
                },
                "signals": {"rate_pressure": ["rate hike"]},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )


def test_feeds_bucket_parse_and_backward_compatibility(tmp_path: Path):
    bucketed = {
        "feeds": {
            "global": [{"name": "g1", "url": "https://example.com/global.xml"}],
            "germany_de": [{"name": "de1", "url": "https://example.com/de.xml"}],
            "korea_ko": [{"name": "kr1", "url": "https://example.com/kr.xml"}],
        }
    }
    bucketed_path = tmp_path / "feeds_bucketed.yml"
    bucketed_path.write_text(yaml.safe_dump(bucketed, sort_keys=False), encoding="utf-8")

    parsed_bucketed = load_feeds_config(str(bucketed_path))
    tags_map = {row["source"]: set(row["tags"]) for row in parsed_bucketed}
    assert tags_map["g1"] == {"global"}
    assert "germany" in tags_map["de1"]
    assert "korea" in tags_map["kr1"]

    legacy = {
        "feeds": [
            {"source": "legacy1", "url": "https://example.com/legacy.xml"},
            {"source": "legacy2", "url": "https://example.com/legacy2.xml", "tags": ["markets"]},
        ]
    }
    legacy_path = tmp_path / "feeds_legacy.yml"
    legacy_path.write_text(yaml.safe_dump(legacy, sort_keys=False), encoding="utf-8")

    parsed_legacy = load_feeds_config(str(legacy_path))
    legacy_tags = {row["source"]: set(row["tags"]) for row in parsed_legacy}
    assert legacy_tags["legacy1"] == {"global"}
    assert legacy_tags["legacy2"] == {"markets"}


def test_ingest_md_single_entry_to_one_record_with_tags(tmp_path: Path):
    base = tmp_path / "inputs/korea"
    base.mkdir(parents=True)
    (base / "single.md").write_text(
        "### Korea industrial output update\n"
        "url: https://example.com/korea-output\n"
        "published_at: 2026-02-05T10:00:00Z\n"
        "Korea exports and output surprised to the upside.\n",
        encoding="utf-8",
    )

    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 1
    assert len(rows) == 1
    assert rows[0]["source"] == "manual"
    assert rows[0]["feed_url"].endswith("inputs/korea")
    assert set(rows[0]["tags"]) == {"manual", "korea"}


def test_ingest_md_multiple_entries_split_by_heading(tmp_path: Path):
    base = tmp_path / "inputs/germany"
    base.mkdir(parents=True)
    (base / "multi.md").write_text(
        "### Germany PMI cools\n"
        "url: https://example.com/pmi\n"
        "Factory activity softened this month.\n"
        "### DAX earnings outlook\n"
        "Corporate earnings guidance was mixed.\n",
        encoding="utf-8",
    )

    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 2
    assert len(rows) == 2
    assert all("germany" in row["tags"] for row in rows)


def test_ingest_csv_records_parsed_and_appended(tmp_path: Path):
    base = tmp_path / "inputs/global"
    base.mkdir(parents=True)
    (base / "items.csv").write_text(
        "title,url,published_at,summary\n"
        "Global equities rally,https://example.com/a,2026-02-04T12:00:00Z,Stocks rose on growth data\n"
        "Rate outlook shifts,https://example.com/b,2026-02-05T12:00:00Z,Central bank path repriced\n",
        encoding="utf-8",
    )

    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 2
    assert len(rows) == 2
    assert rows[0]["title"] == "Global equities rally"
    assert rows[1]["title"] == "Rate outlook shifts"


def test_manual_dedup_same_content_twice(tmp_path: Path):
    base = tmp_path / "inputs/korea"
    base.mkdir(parents=True)
    (base / "dup.md").write_text(
        "### Same item\n"
        "url: https://example.com/same\n"
        "Body text for dedup test.\n",
        encoding="utf-8",
    )

    output_path = tmp_path / "data/raw/news.jsonl"
    first = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    second = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert first == 1
    assert second == 0
    assert len(rows) == 1


def test_report_includes_korea_and_manual_focus_sections(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "k1",
                "source": "manual",
                "feed_url": "inputs/korea",
                "title": "Korea inflation watch",
                "url": "https://example.com/korea",
                "published_at": None,
                "fetched_at": "2026-02-06T12:00:00Z",
                "summary": "Korea inflation and rates pressure.",
                "tags": ["manual", "korea"],
            },
            {
                "id": "g1",
                "source": "manual",
                "feed_url": "inputs/germany",
                "title": "Germany industrial slowdown",
                "url": "https://example.com/germany",
                "published_at": None,
                "fetched_at": "2026-02-05T12:00:00Z",
                "summary": "Germany output and inflation narrative.",
                "tags": ["manual", "germany"],
            },
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)

    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")

    assert "## Korea Focus" in text
    assert "## Germany Focus" in text
    assert "## Manual Inputs" in text
    assert "Korea inflation watch" in text


def _make_pdf(path: Path, lines: list[str]) -> None:
    pytest.importorskip("pypdf")
    reportlab = pytest.importorskip("reportlab")
    _ = reportlab
    from reportlab.pdfgen import canvas

    path.parent.mkdir(parents=True, exist_ok=True)
    c = canvas.Canvas(str(path))
    y = 800
    for line in lines:
        c.drawString(72, y, line)
        y -= 20
    c.save()


def test_ingest_pdf_text_based_file_with_attachment_and_tags(tmp_path: Path):
    pdf_path = tmp_path / "inputs/global/2026-02-05_market_note.pdf"
    _make_pdf(
        pdf_path,
        [
            "Global PDF Headline",
            "This is a known paragraph from the PDF for ingestion testing.",
            "Another line follows for summary extraction checks.",
        ],
    )

    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 1
    assert len(rows) == 1
    row = rows[0]
    assert row["source"] == "manual"
    assert set(row["tags"]) == {"manual", "pdf", "global"}
    assert row["attachment_path"].endswith("2026-02-05_market_note.pdf")
    assert row["published_at"] == "2026-02-05T00:00:00Z"
    assert "Global PDF Headline" in row["title"]
    assert len(row["summary"]) > 0
    assert len(row["summary"]) <= 2000
    assert len(row["body"]) <= 20000


def test_ingest_pdf_empty_text_logs_ocr_warning(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    pdf_path = tmp_path / "inputs/global/blank.pdf"
    _make_pdf(pdf_path, [])
    caplog.set_level("WARNING")

    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 1
    assert len(rows) == 1
    assert rows[0]["summary"].startswith("PDF text extraction is low-quality")
    assert "needs_summary" in set(rows[0]["tags"])
    assert "OCR is not implemented" in caplog.text


def test_ingest_pdf_garbled_text_uses_low_quality_placeholder(tmp_path: Path):
    pdf_path = tmp_path / "inputs/korea/2026-02-06_garbled_note.pdf"
    _make_pdf(
        pdf_path,
        [
            "%%%% #### $$$$ @@@@ !!!!",
            "//// \\\\\\\\ |||| #### $$$$",
            "!!!! ???? **** ;;;; ::::",
        ],
    )
    output_path = tmp_path / "data/raw/news.jsonl"

    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 1
    assert len(rows) == 1
    row = rows[0]
    assert row["attachment_path"].endswith("2026-02-06_garbled_note.pdf")
    assert "low-quality" in row["summary"].lower()
    assert {"manual", "pdf", "korea", "needs_summary"}.issubset(set(row["tags"]))


def test_low_quality_pdf_triggers_scaffold_creation(tmp_path: Path):
    pdf_path = tmp_path / "inputs/korea/2026-02-06_scaffold_me.pdf"
    _make_pdf(
        pdf_path,
        [
            "%%%% #### $$$$ @@@@ !!!!",
            "//// \\\\\\\\ |||| #### $$$$",
            "!!!! ???? **** ;;;; ::::",
        ],
    )
    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 1
    assert len(rows) == 1
    row = rows[0]
    scaffold_path = pdf_path.with_suffix(".md")
    assert scaffold_path.exists()
    scaffold_text = scaffold_path.read_text(encoding="utf-8")
    assert 'tags: ["korea","pdf","needs_summary"]' in scaffold_text
    assert "## TL;DR (fill in)" in scaffold_text
    assert "## Key points (fill in)" in scaffold_text
    assert row.get("summary_scaffold_created") is True
    assert str(row.get("summary_scaffold_path") or "").endswith("2026-02-06_scaffold_me.md")


def test_existing_scaffold_md_is_not_overwritten(tmp_path: Path):
    pdf_path = tmp_path / "inputs/korea/2026-02-06_keep_existing.pdf"
    _make_pdf(
        pdf_path,
        [
            "%%%% #### $$$$ @@@@ !!!!",
            "//// \\\\\\\\ |||| #### $$$$",
        ],
    )
    scaffold_path = pdf_path.with_suffix(".md")
    original = "---\ntitle: \"Existing\"\n---\n\nkeep this file\n"
    scaffold_path.parent.mkdir(parents=True, exist_ok=True)
    scaffold_path.write_text(original, encoding="utf-8")

    output_path = tmp_path / "data/raw/news.jsonl"
    ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    assert scaffold_path.read_text(encoding="utf-8") == original


def test_placeholder_scaffold_md_is_skipped_and_not_rendered(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    pdf_path = tmp_path / "inputs/korea/foo.pdf"
    _make_pdf(
        pdf_path,
        [
            "%%%% #### $$$$ @@@@ !!!!",
            "//// \\\\\\\\ |||| #### $$$$",
        ],
    )
    md_path = tmp_path / "inputs/korea/foo.md"
    md_path.write_text(
        "---\n"
        'title: "foo"\n'
        'published_at: "2026-02-06T00:00:00Z"\n'
        'source: "manual"\n'
        'tags: ["korea","pdf","needs_summary"]\n'
        'attachment_path: "inputs/korea/foo.pdf"\n'
        "---\n\n"
        "## TL;DR (fill in)\n"
        "- ...\n\n"
        "## Key points (fill in)\n"
        "- ...\n",
        encoding="utf-8",
    )
    caplog.set_level("INFO")
    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 1
    assert len(rows) == 1
    row = rows[0]
    assert "md_override" not in set(row.get("tags") or [])
    assert row["attachment_path"].endswith("/foo.pdf")
    assert "placeholder_scaffold" in caplog.text
    assert str(md_path) in caplog.text

    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(output_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    assert "## TL;DR (fill in)" not in text
    assert "needs_summary" not in text


def test_markdown_yaml_only_with_needs_summary_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    md_path = tmp_path / "inputs/korea/yaml_only.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        "---\n"
        'title: "Need summary"\n'
        'tags: ["korea","needs_summary"]\n'
        "---\n",
        encoding="utf-8",
    )
    caplog.set_level("INFO")
    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 0
    assert rows == []
    assert "reason=placeholder_scaffold" in caplog.text
    assert "detail=needs_summary_tag" in caplog.text
    assert str(md_path) in caplog.text


def test_markdown_with_fill_in_marker_is_skipped(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    md_path = tmp_path / "inputs/korea/fillin_note.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        "### Draft note\n"
        "This section is still (fill in) and should not be ingested yet.\n"
        "Additional text is present to avoid short-body filtering.\n",
        encoding="utf-8",
    )
    caplog.set_level("INFO")
    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 0
    assert rows == []
    assert "reason=placeholder_scaffold" in caplog.text
    assert "detail=fill_in_marker" in caplog.text
    assert str(md_path) in caplog.text


def test_edited_markdown_without_placeholders_is_ingested_and_rendered(tmp_path: Path):
    md_path = tmp_path / "inputs/korea/edited_note.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        "### Edited korea note\n"
        "published_at: 2026-02-06T09:00:00Z\n"
        "Analyst edited summary with concrete points on inflation, rates, and exports across multiple sectors.\n",
        encoding="utf-8",
    )
    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 1
    assert len(rows) == 1
    assert rows[0]["title"] == "Edited korea note"
    assert "PDF text extraction is low-quality" not in rows[0]["summary"]

    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(output_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    assert "Analyst edited summary with concrete points" in text


def test_ingest_korea_pdf_low_hangul_ratio_uses_fallback_summary(tmp_path: Path):
    pdf_path = tmp_path / "inputs/korea/2026-02-06_low_hangul_ratio.pdf"
    _make_pdf(
        pdf_path,
        [
            "Korea market deck",
            "%%%% #### $$$$ @@@@ !!!!",
            "Policy update with unreadable fragments and symbols.",
        ],
    )
    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 1
    assert len(rows) == 1
    row = rows[0]
    assert row["attachment_path"].endswith("2026-02-06_low_hangul_ratio.pdf")
    assert row["summary"].startswith("PDF text extraction is low-quality")
    assert {"manual", "pdf", "korea", "needs_summary"}.issubset(set(row["tags"]))


def test_garbled_pdf_fallback_summary_rendered_in_focus_and_manual_sections(tmp_path: Path):
    pdf_path = tmp_path / "inputs/korea/2026-02-06_garbled_render.pdf"
    _make_pdf(
        pdf_path,
        [
            "%%%% #### $$$$ @@@@ !!!!",
            "//// \\\\\\\\ |||| #### $$$$",
            "!!!! ???? **** ;;;; ::::",
        ],
    )
    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    assert appended == 1

    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(output_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    korea_section = _extract_section(text, "## Korea Focus")
    manual_section = _extract_section(text, "## Manual Inputs")

    expected = "PDF text extraction is low-quality; please add a manual summary in a .md file with the same base name."
    assert expected in korea_section
    assert expected in manual_section
    assert "%%%% #### $$$$" not in korea_section


def test_report_references_scaffold_path_for_low_quality_pdf(tmp_path: Path):
    pdf_path = tmp_path / "inputs/korea/2026-02-06_scaffold_reference.pdf"
    _make_pdf(
        pdf_path,
        [
            "%%%% #### $$$$ @@@@ !!!!",
            "//// \\\\\\\\ |||| #### $$$$",
            "!!!! ???? **** ;;;; ::::",
        ],
    )
    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    assert appended == 1

    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(output_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    assert "PDF text extraction is low-quality; please add a manual summary in a .md file with the same base name." in text


def test_md_override_prefers_markdown_when_pdf_sibling_exists(tmp_path: Path):
    pdf_path = tmp_path / "inputs/korea/foo.pdf"
    _make_pdf(
        pdf_path,
        [
            "%%%% #### $$$$ @@@@ !!!!",
            "//// \\\\\\\\ |||| #### $$$$",
        ],
    )
    md_path = tmp_path / "inputs/korea/foo.md"
    md_path.write_text(
        "### Korea markdown override note\n"
        "published_at: 2026-02-06T09:00:00Z\n"
        "This markdown summary should override PDF fallback in reporting.\n",
        encoding="utf-8",
    )

    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)

    assert appended == 1
    assert len(rows) == 1
    row = rows[0]
    assert row["title"] == "Korea markdown override note"
    assert "md_override" in set(row["tags"])
    assert "korea" in set(row["tags"])
    assert row["attachment_path"].endswith("/foo.pdf")
    assert "override PDF fallback" in row["summary"]

    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(output_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    assert "This markdown summary should override PDF fallback in reporting." in text
    assert "PDF text extraction is low-quality" not in text


def test_slide_pdf_summary_is_capped_without_agenda_spam(tmp_path: Path):
    pdf_path = tmp_path / "inputs/korea/2026-02-05_slide_note.pdf"
    _make_pdf(
        pdf_path,
        [
            "Market Strategy Deck",
            "Agenda",
            "Page 1 / 20",
            "Header 2026 Market Outlook",
            "Header 2026 Market Outlook",
            "Header 2026 Market Outlook",
            "Korea semiconductor exports accelerated this quarter as demand recovered across major regions.",
            "Policy makers signaled a cautious path for rate changes while inflation remains above target levels.",
            "Germany industrial momentum softened and shipping volumes pointed to weaker near-term demand.",
            "Funding spreads widened in credit markets and refinancing pressure increased for leveraged issuers.",
            "Portfolio managers highlighted valuation gaps and selective opportunities in quality global equities.",
        ],
    )
    output_path = tmp_path / "data/raw/news.jsonl"
    appended = ingest_manual_inputs(path=str(tmp_path / "inputs"), source="manual", output_path=str(output_path))
    rows = read_jsonl(output_path)
    assert appended == 1
    assert len(rows) == 1
    assert "agenda" not in rows[0]["summary"].lower()

    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(output_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    korea_section = _extract_section(text, "## Korea Focus")
    top_section = _extract_section(text, "## Top 10")

    focus_summary_match = re.search(r"summary:\s*(.+)", korea_section)
    top_summary_match = re.search(r"- summary:\s*(.+)", top_section)
    assert focus_summary_match is not None
    assert top_summary_match is not None

    focus_summary = focus_summary_match.group(1).strip()
    top_summary = top_summary_match.group(1).strip()
    assert "agenda" not in focus_summary.lower()
    assert len(focus_summary) <= 600
    assert len(top_summary) <= 400


def _extract_section(text: str, heading: str) -> str:
    start = text.find(heading)
    if start < 0:
        return ""
    rest = text[start + len(heading) :]
    next_heading_index = rest.find("\n## ")
    if next_heading_index < 0:
        return rest
    return rest[:next_heading_index]


def test_report_includes_attachment_line_when_attachment_path_exists(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "pdf-1",
                "source": "manual",
                "feed_url": "inputs/korea",
                "title": "Korea PDF Note",
                "url": "",
                "published_at": None,
                "fetched_at": "2026-02-06T12:00:00Z",
                "summary": "Summary heading text.",
                "body": "Executive Summary\nThis paragraph should be used for summary rendering.",
                "attachment_path": "inputs/korea/sample.pdf",
                "tags": ["manual", "pdf", "korea"],
            }
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)

    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    assert "Attachment: inputs/korea/sample.pdf" in text


def test_manual_korea_ranked_above_non_manual_in_korea_focus(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "nonmanual-korea",
                "source": "rss_source",
                "feed_url": "https://example.com/rss",
                "title": "Korea market update",
                "url": "https://example.com/nonmanual",
                "published_at": "2026-02-05T12:00:00Z",
                "fetched_at": "2026-02-05T12:01:00Z",
                "summary": "korea inflation and ai trends",
                "tags": ["korea"],
            },
            {
                "id": "manual-korea",
                "source": "manual",
                "feed_url": "inputs/korea",
                "title": "Manual Korea policy memo",
                "url": "",
                "published_at": None,
                "fetched_at": "2026-02-05T12:02:00Z",
                "summary": "Key Points\nManual korea insight.",
                "body": "Key Points\nManual korea insight with guidance.",
                "tags": ["manual", "korea"],
            },
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)

    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    korea_section = _extract_section(text, "## Korea Focus")

    manual_pos = korea_section.find("Manual Korea policy memo")
    nonmanual_pos = korea_section.find("Korea market update")
    assert manual_pos >= 0
    assert nonmanual_pos >= 0
    assert manual_pos < nonmanual_pos


def test_doordash_personal_finance_precedence_over_real_estate_in_report(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "doordash-1",
                "source": "manual",
                "feed_url": "inputs/global",
                "title": "DoorDash restaurant subscription plan expands",
                "url": "",
                "published_at": "2026-02-06T10:00:00Z",
                "fetched_at": "2026-02-06T10:01:00Z",
                "summary": "DoorDash household budgeting and subscription savings update.",
                "tags": ["manual", "global"],
            }
        ],
    )
    rules_path = tmp_path / "rules.yml"
    rules_path.write_text(
        yaml.safe_dump(
            {
                "top_terms": ["doordash", "subscription", "rent"],
                "categories": {
                    "macroeconomics": ["inflation"],
                    "equities": ["stock"],
                    "rates": ["rate hike"],
                    "real_estate": [
                        "immobilien",
                        "miete",
                        "rent",
                        "mortgage",
                        "housing",
                        "house prices",
                        "reit",
                        "property",
                        "wohnung",
                        "bauzinsen",
                    ],
                    "germany": ["germany"],
                    "korea": ["korea"],
                    "personal_finance": ["doordash", "subscription", "budgeting", "household finance"],
                },
                "signals": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    by_category = _extract_section(text, "## By Category")
    personal_section = by_category.split("### personal_finance", 1)[1].split("###", 1)[0]
    real_estate_section = by_category.split("### real_estate", 1)[1].split("###", 1)[0]

    assert "DoorDash restaurant subscription plan expands" in personal_section
    assert "DoorDash restaurant subscription plan expands" not in real_estate_section
    assert "- No items" in real_estate_section


def test_low_quality_pdf_fallback_item_excluded_from_top10_when_alternatives_exist(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "pdf-low-quality",
                "source": "manual",
                "feed_url": "inputs/korea",
                "title": "korea inflation rate hike garbled",
                "url": "",
                "published_at": "2026-02-06T08:00:00Z",
                "fetched_at": "2026-02-06T08:01:00Z",
                "summary": "PDF text extraction is low-quality; please add a manual summary in a .md file with the same base name.",
                "attachment_path": "inputs/korea/garbled.pdf",
                "tags": ["manual", "pdf", "korea"],
            },
            {
                "id": "normal-1",
                "source": "feed_a",
                "feed_url": "https://example.com/a.xml",
                "title": "Korea policy update with inflation easing",
                "url": "https://example.com/a",
                "published_at": "2026-02-06T09:00:00Z",
                "fetched_at": "2026-02-06T09:01:00Z",
                "summary": "Inflation eased and policy stance remains cautious.",
                "tags": ["korea"],
            },
            {
                "id": "normal-2",
                "source": "feed_b",
                "feed_url": "https://example.com/b.xml",
                "title": "Germany industrial output declines",
                "url": "https://example.com/b",
                "published_at": "2026-02-06T10:00:00Z",
                "fetched_at": "2026-02-06T10:01:00Z",
                "summary": "Output data weakened across major sectors.",
                "tags": ["germany"],
            },
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    top10_section = _extract_section(text, "## Top 10")
    korea_section = _extract_section(text, "## Korea Focus")

    assert "korea inflation rate hike garbled" not in top10_section
    assert "Korea policy update with inflation easing" in top10_section
    assert "korea inflation rate hike garbled" in korea_section


def test_url_canonicalization_removes_tracking_params():
    raw = "HTTPS://Example.com/News/?utm_source=x&ref=abc&source=rss&share=1&id=42"
    assert canonicalize_url(raw) == "https://example.com/News?id=42"


def test_near_duplicate_titles_collapse_to_one_item_in_week(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "dup-a",
                "source": "feed_a",
                "feed_url": "https://a.example.com/rss",
                "title": "South Korea inflation slows in January 2026",
                "url": "https://example.com/a",
                "published_at": "2026-02-06T10:00:00Z",
                "fetched_at": "2026-02-06T10:01:00Z",
                "summary": "Inflation trend turned lower.",
                "tags": ["korea"],
            },
            {
                "id": "dup-b",
                "source": "feed_b",
                "feed_url": "https://b.example.com/rss",
                "title": "South Korea inflation slows in January, 2026",
                "url": "https://example.com/b",
                "published_at": "2026-02-06T10:02:00Z",
                "fetched_at": "2026-02-06T10:03:00Z",
                "summary": "Another feed reporting the same macro story.",
                "tags": ["korea"],
            },
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)

    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    assert "number_of_items_considered: 1" in text


def test_dedup_keeps_record_with_richer_summary(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "short",
                "source": "feed_a",
                "feed_url": "https://a.example.com/rss",
                "title": "ECB keeps rates steady in June",
                "url": "https://example.com/story?id=1&utm_source=rss",
                "published_at": "2026-02-04T09:00:00Z",
                "fetched_at": "2026-02-04T09:01:00Z",
                "summary": "Short summary only.",
                "tags": ["rates"],
            },
            {
                "id": "long",
                "source": "feed_b",
                "feed_url": "https://b.example.com/rss",
                "title": "ECB keeps rates steady in June!",
                "url": "https://example.com/story?id=1&ref=homepage",
                "published_at": "2026-02-04T09:02:00Z",
                "fetched_at": "2026-02-04T09:03:00Z",
                "summary": "Longer summary sentence one. Sentence two with more detail. Sentence three extends context.",
                "tags": ["rates"],
            },
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)

    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    assert "number_of_items_considered: 1" in text
    assert "Sentence two with more detail" in text


def _top10_titles(report_text: str) -> list[str]:
    section = _extract_section(report_text, "## Top 10")
    titles: list[str] = []
    for line in section.splitlines():
        if line.startswith("### "):
            titles.append(line[4:].strip())
    return titles


def test_top10_reduces_topic_bias_with_signature_clustering(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    ai_titles = [
        "AI DeepSeek Nvidia demand outlook",
        "AI DeepSeek AMD supply chain signal",
        "AI DeepSeek TSMC capacity planning note",
        "AI DeepSeek Broadcom networking outlook",
        "AI DeepSeek Qualcomm inference push",
        "AI DeepSeek ASML lithography demand",
        "AI DeepSeek Micron memory cycle shift",
        "AI DeepSeek Samsung foundry strategy",
        "AI DeepSeek Intel accelerator roadmap",
        "AI DeepSeek Arm ecosystem expansion",
    ]
    diverse_titles = [
        "ECB policy briefing for rates",
        "Housing affordability trend in Europe",
        "Oil inventory surprise this week",
        "Labor market cooling indicators",
        "Japan trade balance update",
        "Consumer confidence rebounds",
        "Fiscal package vote outlook",
        "Corporate bond spreads tighten",
        "Shipping index signals slowdown",
        "Retail sales momentum improves",
    ]

    rows = []
    for idx, title in enumerate(ai_titles):
        rows.append(
            {
                "id": f"ai-{idx}",
                "source": "feed_ai",
                "feed_url": "https://ai.example.com/rss",
                "title": title,
                "url": f"https://example.com/ai-{idx}",
                "published_at": f"2026-02-0{(idx % 6) + 1}T10:00:00Z",
                "fetched_at": f"2026-02-0{(idx % 6) + 1}T10:01:00Z",
                "summary": "AI deepseek semiconductor demand remains strong.",
                "tags": ["equities"],
            }
        )
    for idx, title in enumerate(diverse_titles):
        rows.append(
            {
                "id": f"div-{idx}",
                "source": "feed_diverse",
                "feed_url": "https://div.example.com/rss",
                "title": title,
                "url": f"https://example.com/div-{idx}",
                "published_at": f"2026-02-0{(idx % 6) + 1}T11:00:00Z",
                "fetched_at": f"2026-02-0{(idx % 6) + 1}T11:01:00Z",
                "summary": "Macro and market signals remain mixed.",
                "tags": ["global"],
            }
        )
    append_jsonl(data_path, rows)

    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    titles = _top10_titles(report_path.read_text(encoding="utf-8"))
    ai_count = sum(1 for title in titles if title.startswith("AI DeepSeek"))

    assert len(titles) == 10
    assert ai_count <= 3


def test_top10_includes_regional_quota_when_available(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    rows = []
    core_titles = [
        "Global AI cloud spending jumps",
        "Semiconductor equipment demand rises",
        "Data-center networking orders accelerate",
        "Enterprise software margins improve",
        "Biotech funding activity rebounds",
        "Energy utilities raise capex guidance",
        "Consumer staples pricing power fades",
        "Aerospace backlog expands this quarter",
        "Shipping logistics rates normalize",
        "Telecom infrastructure cycle stabilizes",
    ]
    for idx, title in enumerate(core_titles):
        rows.append(
            {
                "id": f"core-{idx}",
                "source": "feed_core",
                "feed_url": "https://core.example.com/rss",
                "title": title,
                "url": f"https://example.com/core-{idx}",
                "published_at": f"2026-02-0{(idx % 6) + 1}T09:00:00Z",
                "fetched_at": f"2026-02-0{(idx % 6) + 1}T09:01:00Z",
                "summary": "AI deepseek semiconductor capital spending trend.",
                "tags": ["equities"],
            }
        )
    rows.extend(
        [
            {
                "id": "kr-regional",
                "source": "feed_kr",
                "feed_url": "https://kr.example.com/rss",
                "title": "Korea export policy update",
                "url": "https://example.com/kr-regional",
                "published_at": "2026-02-05T08:00:00Z",
                "fetched_at": "2026-02-05T08:01:00Z",
                "summary": "Korea policy and inflation signals.",
                "tags": ["korea"],
            },
            {
                "id": "de-regional",
                "source": "feed_de",
                "feed_url": "https://de.example.com/rss",
                "title": "Germany industrial production update",
                "url": "https://example.com/de-regional",
                "published_at": "2026-02-05T08:05:00Z",
                "fetched_at": "2026-02-05T08:06:00Z",
                "summary": "Germany growth and rates outlook.",
                "tags": ["germany"],
            },
        ]
    )
    append_jsonl(data_path, rows)

    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    titles = _top10_titles(report_path.read_text(encoding="utf-8"))

    regional_titles = {"Korea export policy update", "Germany industrial production update"}
    regional_count = sum(1 for title in titles if title in regional_titles)
    assert len(titles) == 10
    assert regional_count >= 2


def test_rss_summary_html_img_removed(monkeypatch: pytest.MonkeyPatch):
    xml = """
<rss><channel><item>
  <title>Sample Story</title>
  <link>https://example.com/story</link>
  <description><![CDATA[<img src="x.jpg"/>Alpha &amp; Beta<script>alert(1)</script>]]></description>
</item></channel></rss>
""".strip().encode("utf-8")

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return xml

    def _fake_urlopen(*args, **kwargs):
        return _Response()

    monkeypatch.setattr("wealth_agents.rss.urlopen", _fake_urlopen)
    rows = fetch_feed_records("https://example.com/rss.xml", "test_source", tags=["global"])
    assert len(rows) == 1
    summary = rows[0]["summary"]
    assert "<img" not in summary
    assert "<script" not in summary
    assert "Alpha & Beta" in summary


def test_pdf_summary_cleaned_and_capped(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    long_sentence = "Clean insight sentence. " * 80
    append_jsonl(
        data_path,
        [
            {
                "id": "pdf-clean",
                "source": "manual",
                "feed_url": "inputs/korea",
                "title": "PDF manual item",
                "url": "",
                "published_at": None,
                "fetched_at": "2026-02-06T12:00:00Z",
                "summary": "",
                "body": "Updated Jan 31, 2026\nDefinition\nSource\nExecutive Summary\n"
                + long_sentence
                + "\ue123",
                "attachment_path": "inputs/korea/sample.pdf",
                "tags": ["manual", "pdf", "korea"],
            }
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    korea_section = _extract_section(text, "## Korea Focus")
    top_section = _extract_section(text, "## Top 10")

    focus_summary_match = re.search(r"summary:\s*(.+)", korea_section)
    top_summary_match = re.search(r"- summary:\s*(.+)", top_section)
    assert focus_summary_match is not None
    assert top_summary_match is not None

    focus_summary = focus_summary_match.group(1).strip()
    top_summary = top_summary_match.group(1).strip()
    assert "Updated" not in focus_summary
    assert "Definition" not in focus_summary
    assert "\ue123" not in focus_summary
    assert len(focus_summary) <= 600
    assert len(top_summary) <= 400


def test_signals_include_week_over_week_delta_when_prior_week_exists(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "prev-week",
                "source": "feed_prev",
                "feed_url": "u",
                "title": "Inflation trend prior week",
                "url": "https://example.com/prev",
                "published_at": "2026-01-28T12:00:00Z",
                "fetched_at": "2026-01-28T12:01:00Z",
                "summary": "inflation only",
                "tags": ["global"],
            },
            {
                "id": "curr-week-1",
                "source": "feed_curr",
                "feed_url": "u",
                "title": "Inflation and rate hike this week",
                "url": "https://example.com/curr1",
                "published_at": "2026-02-04T12:00:00Z",
                "fetched_at": "2026-02-04T12:01:00Z",
                "summary": "inflation and rate hike pressure",
                "tags": ["global"],
            },
            {
                "id": "curr-week-2",
                "source": "feed_curr",
                "feed_url": "u",
                "title": "Rate hike expectations rise",
                "url": "https://example.com/curr2",
                "published_at": "2026-02-05T12:00:00Z",
                "fetched_at": "2026-02-05T12:01:00Z",
                "summary": "rate hike rate hike",
                "tags": ["global"],
            },
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    report_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
    )
    text = report_path.read_text(encoding="utf-8")
    signals_section = _extract_section(text, "## Signals & Watchlist")
    assert "vs prior week" in signals_section
    assert "baseline not available" not in signals_section.lower()


def test_report_header_contains_metadata_fields(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "meta-korea",
                "source": "manual",
                "feed_url": "inputs/korea",
                "title": "Metadata Korea item",
                "url": "",
                "published_at": None,
                "fetched_at": "2026-02-06T12:00:00Z",
                "summary": "korea manual note",
                "tags": ["manual", "korea"],
            }
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    meta_path = tmp_path / "data/meta/last_collect.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(
        '{"status":"success","feeds_total":9,"feeds_ok":7,"feeds_failed":2,"fetched":20,"appended":10,"error_type":null,"error_samples":[]}\n',
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
    assert "- rss_collect_status:" in text
    assert "- feeds_failed_total:" in text
    assert "- manual_items_count:" in text
    assert "- korea_items_count:" in text
    assert "- germany_items_count:" in text
    assert "- korea_share:" in text
    assert "- germany_share:" in text
    assert "- kr_de_share:" in text
    assert "- duplicates_removed_count:" in text


def test_report_persists_weekly_aggregates_and_renders_keyword_delta_table(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "prev-kr-1",
                "source": "feed_prev",
                "feed_url": "u",
                "title": "Korea inflation watch prior week",
                "url": "https://example.com/prev-kr-1",
                "published_at": "2026-01-28T12:00:00Z",
                "fetched_at": "2026-01-28T12:01:00Z",
                "summary": "inflation",
                "tags": ["korea"],
            },
            {
                "id": "curr-kr-1",
                "source": "feed_curr",
                "feed_url": "u",
                "title": "Korea inflation accelerates this week",
                "url": "https://example.com/curr-kr-1",
                "published_at": "2026-02-04T12:00:00Z",
                "fetched_at": "2026-02-04T12:01:00Z",
                "summary": "inflation and rate hike pressure",
                "tags": ["korea"],
            },
            {
                "id": "curr-kr-2",
                "source": "feed_curr",
                "feed_url": "u",
                "title": "Korea inflation and rate hike outlook",
                "url": "https://example.com/curr-kr-2",
                "published_at": "2026-02-05T12:00:00Z",
                "fetched_at": "2026-02-05T12:01:00Z",
                "summary": "inflation rate hike",
                "tags": ["korea"],
            },
            {
                "id": "curr-de-1",
                "source": "feed_curr",
                "feed_url": "u",
                "title": "Germany inflation update",
                "url": "https://example.com/curr-de-1",
                "published_at": "2026-02-06T12:00:00Z",
                "fetched_at": "2026-02-06T12:01:00Z",
                "summary": "inflation in germany",
                "tags": ["germany"],
            },
        ],
    )
    rules_path = tmp_path / "rules.yml"
    _write_rules(rules_path)
    aggregates_path = tmp_path / "data/meta/weekly_aggregates.jsonl"

    generate_weekly_report(
        week="2026-W05",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
        weekly_aggregates_path=str(aggregates_path),
    )
    week6_path = generate_weekly_report(
        week="2026-W06",
        data_path=str(data_path),
        rules_path=str(rules_path),
        output_dir=str(tmp_path / "reports"),
        weekly_aggregates_path=str(aggregates_path),
    )
    week6_text = week6_path.read_text(encoding="utf-8")
    aggregates = read_jsonl(aggregates_path)
    by_week = {row["week"]: row for row in aggregates}

    assert "2026-W05" in by_week
    assert "2026-W06" in by_week
    assert by_week["2026-W06"]["item_count"] == 3
    assert "### Top Rising Keywords (WoW)" in week6_text
    assert "| keyword | this_week | last_week | delta |" in week6_text
    assert "| inflation |" in week6_text
    assert "Korea spike" in week6_text


def test_korea_focus_empty_reason_explains_feed_failure_and_missing_manual(tmp_path: Path):
    data_path = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        data_path,
        [
            {
                "id": "de-only",
                "source": "feed_de",
                "feed_url": "u",
                "title": "Germany only item",
                "url": "https://example.com/de-only",
                "published_at": "2026-02-06T12:00:00Z",
                "fetched_at": "2026-02-06T12:01:00Z",
                "summary": "germany inflation",
                "tags": ["germany"],
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
                "status": "partial",
                "feeds_total": 2,
                "feeds_success": 1,
                "feeds_failed": 1,
                "fetched": 1,
                "feed_statuses": [
                    {
                        "source": "kr_feed",
                        "feed_url": "https://example.com/kr.xml",
                        "tags": ["korea"],
                        "status": "dns_error",
                        "skipped": False,
                        "fetched": 0,
                    },
                    {
                        "source": "de_feed",
                        "feed_url": "https://example.com/de.xml",
                        "tags": ["germany"],
                        "status": "ok",
                        "skipped": False,
                        "fetched": 1,
                    },
                ],
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
    korea_section = _extract_section(text, "## Korea Focus")
    assert "region feeds failed this week and no manual inputs were provided" in korea_section
