import json
import sys
from pathlib import Path

import pytest
import yaml

from wealth_agents.cli import main as cli_main
from wealth_agents.ingest import LOW_QUALITY_PDF_SUMMARY, ingest_manual_inputs
from wealth_agents.storage import append_jsonl, read_jsonl, validate_jsonl


def _run_cli(monkeypatch: pytest.MonkeyPatch, args: list[str]) -> int:
    monkeypatch.setattr(sys, "argv", ["wealth_agents"] + args)
    return cli_main()


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


def test_jsonl_multiline_text_and_invalid_unicode_stays_valid(tmp_path: Path):
    out = tmp_path / "data/raw/news.jsonl"
    append_jsonl(
        out,
        [
            {
                "id": "multiline-1",
                "title": "Line A\r\nLine B\rLine C",
                "summary": "First line\nSecond line",
                "body": "Bad surrogate: \ud800 end",
            }
        ],
    )

    assert validate_jsonl(out) == 1
    raw_lines = out.read_text(encoding="utf-8").splitlines()
    assert len(raw_lines) == 1

    parsed = json.loads(raw_lines[0])
    assert parsed["title"] == "Line A\nLine B\nLine C"
    parsed["body"].encode("utf-8")


def test_validate_command_fails_for_corrupt_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_path = tmp_path / "data/raw/news.jsonl"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text('{"id":"ok"}\nnot-json\n', encoding="utf-8")

    rc = _run_cli(monkeypatch, ["validate", "--data", str(data_path)])
    assert rc == 1


def test_collect_runs_validation_gate_and_fails_on_corrupt_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
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
    config_path.write_text(
        yaml.safe_dump({"feeds": [{"source": "local_feed", "url": feed_path.as_uri(), "tags": ["global"]}]}, sort_keys=False),
        encoding="utf-8",
    )

    output_path = tmp_path / "data/raw/news.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text('{"id":"seed"}\nBROKEN_LINE\n', encoding="utf-8")
    meta_path = tmp_path / "data/meta/last_collect.json"
    health_path = tmp_path / "data/meta/feed_health.json"

    rc = _run_cli(
        monkeypatch,
        [
            "collect",
            "--config",
            str(config_path),
            "--output",
            str(output_path),
            "--meta-output",
            str(meta_path),
            "--health-meta-output",
            str(health_path),
        ],
    )
    assert rc == 1


def test_ingest_runs_validation_gate_and_fails_on_corrupt_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    md_path = tmp_path / "inputs/global/manual_note.md"
    md_path.parent.mkdir(parents=True, exist_ok=True)
    md_path.write_text(
        "### Manual item\n"
        "This is a sufficiently long edited manual entry with concrete details for validation coverage.\n",
        encoding="utf-8",
    )

    output_path = tmp_path / "data/raw/news.jsonl"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("BAD\n", encoding="utf-8")

    rc = _run_cli(
        monkeypatch,
        [
            "ingest",
            "--path",
            str(tmp_path / "inputs"),
            "--source",
            "manual",
            "--output",
            str(output_path),
        ],
    )
    assert rc == 1


def test_report_command_fails_fast_on_corrupt_jsonl(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data_path = tmp_path / "data/raw/news.jsonl"
    data_path.parent.mkdir(parents=True, exist_ok=True)
    data_path.write_text('{"id":"ok"}\nBROKEN\n', encoding="utf-8")
    rules_path = tmp_path / "rules.yml"
    rules_path.write_text(
        yaml.safe_dump(
            {
                "top_terms": ["inflation"],
                "categories": {"macroeconomics": ["inflation"]},
                "signals": {},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )

    rc = _run_cli(
        monkeypatch,
        [
            "report",
            "--week",
            "2026-W06",
            "--data",
            str(data_path),
            "--rules",
            str(rules_path),
            "--output-dir",
            str(tmp_path / "reports"),
        ],
    )
    assert rc == 1


def test_ingest_garbled_pdf_writes_placeholder_but_valid_jsonl(tmp_path: Path):
    pdf_path = tmp_path / "inputs/korea/2026-02-06_garbled_integrity.pdf"
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
    assert validate_jsonl(output_path) == 1
    assert len(rows) == 1
    row = rows[0]
    assert row["summary"].startswith(LOW_QUALITY_PDF_SUMMARY)
    assert row["body"].startswith("Low-quality PDF extraction")
    assert "%%%%" not in row["body"]
    assert "needs_summary" in set(row["tags"])
    assert Path(str(row["summary_scaffold_path"])).suffix == ".md"
