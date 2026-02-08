import csv
import hashlib
import logging
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from .storage import append_unique_records, validate_jsonl
from .utils import canonicalize_url, normalize_text, now_iso8601, parse_datetime_to_iso

logger = logging.getLogger(__name__)

SUPPORTED_EXTENSIONS = {".md", ".txt", ".csv", ".pdf"}
REGIONS = ("korea", "germany", "global")
LOW_QUALITY_PDF_SUMMARY = (
    "PDF text extraction is low-quality; please add a manual summary in a .md file with the same base name."
)

def ingest_manual_inputs(path: str = "inputs", source: str = "manual", output_path: str = "data/raw/news.jsonl") -> int:
    records = collect_manual_records(path=path, source=source)
    appended = append_unique_records(path=output_path, records=records)
    validated_lines = validate_jsonl(output_path)
    logger.info("Validated JSONL file %s lines=%s", output_path, validated_lines)
    logger.info("Manual ingest complete: parsed=%s appended=%s", len(records), appended)
    return appended


def collect_manual_records(path: str = "inputs", source: str = "manual") -> list[dict]:
    root = Path(path)
    fetched_at = now_iso8601()
    records: list[dict] = []

    for region in REGIONS:
        region_dir = root / region
        if not region_dir.exists():
            continue
        feed_url = f"{root.as_posix().rstrip('/')}/{region}"

        file_paths = sorted(p for p in region_dir.rglob("*") if p.is_file())
        md_paths = {p.resolve() for p in file_paths if p.suffix.lower() == ".md"}
        pdf_paths = {p.resolve() for p in file_paths if p.suffix.lower() == ".pdf"}
        md_skip_reason_map: dict[Path, Optional[str]] = {}
        for md_path in md_paths:
            try:
                text = md_path.read_text(encoding="utf-8")
            except Exception:
                text = ""
            md_skip_reason_map[md_path] = _placeholder_scaffold_reason(text)

        for file_path in file_paths:
            if not file_path.is_file():
                continue
            suffix = file_path.suffix.lower()
            if suffix not in SUPPORTED_EXTENSIONS:
                continue
            resolved = file_path.resolve()
            if suffix == ".md" and md_skip_reason_map.get(resolved):
                detail = str(md_skip_reason_map.get(resolved) or "unknown")
                logger.info(
                    "Skipping markdown scaffold reason=placeholder_scaffold detail=%s path=%s",
                    detail,
                    file_path,
                )
                continue

            if (
                suffix == ".pdf"
                and resolved.with_suffix(".md") in md_paths
                and not md_skip_reason_map.get(resolved.with_suffix(".md"))
            ):
                logger.info("Skipping PDF %s because matching markdown override exists.", file_path)
                continue

            try:
                parsed_items = _parse_file(file_path, region=region)
            except Exception as exc:
                logger.warning("Failed to parse input file %s: %s", file_path, exc)
                continue

            override_pdf = None
            if (
                suffix == ".md"
                and resolved.with_suffix(".pdf") in pdf_paths
                and not md_skip_reason_map.get(resolved)
            ):
                override_pdf = resolved.with_suffix(".pdf")

            for item in parsed_items:
                item_payload = dict(item)
                if override_pdf is not None:
                    item_payload["attachment_path"] = _relative_to_cwd(override_pdf)
                    extra_tags = list(item_payload.get("tags") or [])
                    extra_tags.append("md_override")
                    item_payload["tags"] = extra_tags

                title = item_payload.get("title", "").strip()
                body = item_payload.get("body", "").strip()
                if not title or not body:
                    continue

                url = (item_payload.get("url") or "").strip()
                published_at = parse_datetime_to_iso(item_payload.get("published_at"))
                attachment_path = (item_payload.get("attachment_path") or "").strip()
                summary = (item_payload.get("summary") or body).strip()
                tags = _merge_tags(["manual", region], item_payload.get("tags") or [])

                if attachment_path:
                    identifier = stable_manual_pdf_item_id(
                        title=title,
                        attachment_path=attachment_path,
                        body=body,
                    )
                else:
                    identifier = stable_manual_item_id(
                        source=source,
                        title=title,
                        url=url,
                        body=body,
                        published_at=published_at,
                    )

                record = {
                    "id": identifier,
                    "source": source,
                    "feed_url": feed_url,
                    "title": title,
                    "url": url,
                    "published_at": published_at,
                    "fetched_at": fetched_at,
                    "summary": summary,
                    "body": body,
                    "tags": tags,
                }
                if attachment_path:
                    record["attachment_path"] = attachment_path
                if "readability_score" in item_payload:
                    record["readability_score"] = item_payload.get("readability_score")
                scaffold_path = str(item_payload.get("summary_scaffold_path") or "").strip()
                if scaffold_path:
                    record["summary_scaffold_path"] = scaffold_path
                if item_payload.get("summary_scaffold_created") is not None:
                    record["summary_scaffold_created"] = bool(item_payload.get("summary_scaffold_created"))
                records.append(record)
    return records


def stable_manual_item_id(source: str, title: str, url: str, body: str, published_at: Optional[str]) -> str:
    canonical_url = canonicalize_url(url) if url else ""
    body_head = normalize_text(body)[:200].lower()
    payload = "|".join(
        [
            source.strip().lower(),
            normalize_text(title).lower(),
            canonical_url,
            body_head,
            (published_at or "").strip(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def stable_manual_pdf_item_id(title: str, attachment_path: str, body: str) -> str:
    payload = "|".join(
        [
            normalize_text(title).lower(),
            str(Path(attachment_path).as_posix()).strip().lower(),
            normalize_text(body)[:500].lower(),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_file(path: Path, region: str) -> list[dict]:
    if path.suffix.lower() in {".md", ".txt"}:
        return _parse_text_file(path)
    if path.suffix.lower() == ".csv":
        return _parse_csv_file(path)
    if path.suffix.lower() == ".pdf":
        return _parse_pdf_file(path, region=region)
    return []


def _parse_text_file(path: Path) -> list[dict]:
    text = path.read_text(encoding="utf-8")
    blocks = _split_text_blocks(text)
    items: list[dict] = []
    for block in blocks:
        parsed = _parse_text_block(block)
        if parsed is not None:
            items.append(parsed)
    return items


def _split_text_blocks(text: str) -> list[list[str]]:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if line.startswith("### "):
            if current:
                blocks.append(current)
                current = []
            current.append(line)
            continue
        if line.strip() == "---":
            if current:
                blocks.append(current)
                current = []
            continue
        current.append(line)

    if current:
        blocks.append(current)
    return blocks


def _parse_text_block(lines: list[str]) -> Optional[dict]:
    cleaned = [line.rstrip() for line in lines]
    while cleaned and not cleaned[0].strip():
        cleaned.pop(0)
    while cleaned and not cleaned[-1].strip():
        cleaned.pop()
    if not cleaned:
        return None

    title = ""
    body_lines: list[str] = []
    url = ""
    published_at: Optional[str] = None

    index = 0
    if cleaned[0].startswith("### "):
        title = cleaned[0][4:].strip()
        index = 1

    for line in cleaned[index:]:
        stripped = line.strip()
        if not stripped:
            body_lines.append(line)
            continue

        url_match = re.match(r"^url\s*:\s*(\S+)\s*$", stripped, flags=re.IGNORECASE)
        if url_match:
            url = url_match.group(1).strip()
            continue

        published_match = re.match(r"^published_at\s*:\s*(.+)\s*$", stripped, flags=re.IGNORECASE)
        if published_match:
            published_at = published_match.group(1).strip()
            continue

        title_match = re.match(r"^title\s*:\s*(.+)\s*$", stripped, flags=re.IGNORECASE)
        if title_match and not title:
            title = title_match.group(1).strip()
            continue

        body_lines.append(line)

    if not title:
        for idx, line in enumerate(body_lines):
            if line.strip():
                title = line.strip()
                body_lines = body_lines[idx + 1 :]
                break

    body = "\n".join(body_lines).strip()
    if not title or not body:
        return None
    return {"title": title, "url": url, "published_at": published_at, "body": body}


def _parse_csv_file(path: Path) -> list[dict]:
    items: list[dict] = []
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            normalized = {str(k).strip().lower(): (v or "").strip() for k, v in row.items() if k}
            title = normalized.get("title", "").strip()
            url = normalized.get("url", "").strip()
            published_at = normalized.get("published_at", "").strip()
            body = normalized.get("summary", "").strip() or normalized.get("body", "").strip()
            if not title or not body:
                continue
            items.append(
                {
                    "title": title,
                    "url": url,
                    "published_at": published_at or None,
                    "body": body,
                    "summary": body,
                }
            )
    return items


def _parse_pdf_file(path: Path, region: str) -> list[dict]:
    try:
        from pypdf import PdfReader  # type: ignore
    except Exception as exc:
        raise ValueError("PDF ingest requires the 'pypdf' package.") from exc

    reader = PdfReader(str(path))
    chunks: list[str] = []
    for page in reader.pages:
        text = page.extract_text() or ""
        text = text.strip()
        if text:
            chunks.append(text)

    extracted = "\n\n".join(chunks).strip()
    if not extracted:
        logger.warning(
            "PDF %s has no extractable text. OCR is not implemented; add OCR output or manual summary.",
            path,
        )

    metrics = _pdf_readability_metrics(extracted, region=region)
    low_quality = _is_low_quality_pdf_text(extracted, metrics, region=region)
    if low_quality:
        title = path.stem.replace("_", " ").strip()
    else:
        title = _first_non_empty_line(extracted) or path.stem.replace("_", " ").strip()
    published_at = _published_at_from_filename(path.name)

    scaffold_path = ""
    scaffold_created = False
    if low_quality:
        scaffold_path, scaffold_created = _ensure_low_quality_pdf_scaffold(
            pdf_path=path,
            region=region,
            title=title,
            published_at=published_at,
        )
        summary = LOW_QUALITY_PDF_SUMMARY
        body = "Low-quality PDF extraction. Add a matching markdown summary file."
        tags = ["pdf", "needs_summary"]
    else:
        body = extracted[:20000]
        summary = _pdf_summary(body, region=region, metrics=metrics)
        tags = ["pdf"]

    return [
        {
            "title": title,
            "url": "",
            "published_at": published_at,
            "body": body,
            "summary": summary,
            "attachment_path": path.as_posix(),
            "tags": tags,
            "readability_score": metrics["readability_score"],
            "summary_scaffold_path": scaffold_path,
            "summary_scaffold_created": scaffold_created,
        }
    ]


def _first_non_empty_line(text: str) -> str:
    for line in text.splitlines():
        cleaned = line.strip()
        if cleaned:
            return cleaned[:200]
    return ""


def _pdf_summary(text: str, region: str, metrics: Optional[dict[str, float]] = None) -> str:
    metrics = metrics or _pdf_readability_metrics(text, region=region)
    if _is_low_quality_pdf_text(text, metrics, region=region):
        logger.warning(
            "PDF extract quality is low (score=%.2f printable_ratio=%.2f hangul_ratio=%.2f garbled_ratio=%.2f); using placeholder summary.",
            metrics["readability_score"],
            metrics["printable_ratio"],
            metrics["hangul_ratio"],
            metrics["garbled_ratio"],
        )
        return LOW_QUALITY_PDF_SUMMARY

    heading_paragraph = _paragraph_after_heading(text)
    if heading_paragraph:
        return _normalize_spaces(heading_paragraph)[:2000]

    cleaned = _remove_slide_noise(text)
    slide_summary = _keyword_density_summary(cleaned)
    if slide_summary:
        return slide_summary[:2000]

    paragraphs = [block.strip() for block in re.split(r"\n\s*\n+", cleaned) if block.strip()]
    if paragraphs:
        return _normalize_spaces(paragraphs[0])[:2000]
    return _normalize_spaces(cleaned)[:1500]


def _published_at_from_filename(filename: str) -> Optional[str]:
    match = re.search(r"(20\d{2})[-_](\d{2})[-_](\d{2})", filename)
    if not match:
        return None
    year, month, day = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    try:
        dt = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ensure_low_quality_pdf_scaffold(
    pdf_path: Path,
    region: str,
    title: str,
    published_at: Optional[str],
) -> tuple[str, bool]:
    md_path = pdf_path.with_suffix(".md")
    relative_pdf_path = _relative_to_cwd(pdf_path)
    relative_md_path = _relative_to_cwd(md_path)
    if md_path.exists():
        return relative_md_path, False

    published_value = published_at or now_iso8601()
    safe_title = title.replace('"', '\\"')
    scaffold = (
        "---\n"
        f'title: "{safe_title}"\n'
        f'published_at: "{published_value}"\n'
        'source: "manual"\n'
        f'tags: ["{region}","pdf","needs_summary"]\n'
        f'attachment_path: "{relative_pdf_path}"\n'
        "---\n\n"
        "## TL;DR (fill in)\n"
        "- ...\n\n"
        "## Key points (fill in)\n"
        "- ...\n"
        "- ...\n\n"
        "## Numbers / Data (optional)\n"
        "- ...\n\n"
        "## Actionable watchlist (optional)\n"
        "- ...\n"
    )
    md_path.write_text(scaffold, encoding="utf-8")
    logger.info("Created low-quality PDF scaffold at %s", md_path)
    return relative_md_path, True


def _relative_to_cwd(path: Path) -> str:
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def _placeholder_scaffold_reason(text: str) -> Optional[str]:
    front_matter = _extract_front_matter(text)
    body = _strip_front_matter(text)

    if front_matter and _front_matter_has_needs_summary(front_matter):
        return "needs_summary_tag"
    if "(fill in)" in text.lower():
        return "fill_in_marker"
    if len(re.sub(r"\s+", "", body)) < 50:
        return "short_body"
    return None


def _extract_front_matter(text: str) -> str:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", text, flags=re.DOTALL)
    if not match:
        return ""
    return str(match.group(1) or "")


def _strip_front_matter(text: str) -> str:
    return re.sub(r"\A---\s*\n.*?\n---\s*(?:\n|$)", "", text, flags=re.DOTALL)


def _front_matter_has_needs_summary(front_matter: str) -> bool:
    if "needs_summary" in front_matter.lower():
        return True
    try:
        import yaml  # type: ignore

        loaded = yaml.safe_load(front_matter)
    except Exception:
        return False
    if not isinstance(loaded, dict):
        return False
    tags = loaded.get("tags")
    if isinstance(tags, list):
        return any(str(tag).strip().lower() == "needs_summary" for tag in tags)
    if isinstance(tags, str):
        return "needs_summary" in tags.lower()
    return False


def _merge_tags(base_tags: list[str], extra_tags: list[Any]) -> list[str]:
    merged: list[str] = []
    for tag in base_tags + [str(tag) for tag in extra_tags if isinstance(tag, str)]:
        cleaned = tag.strip().lower()
        if cleaned and cleaned not in merged:
            merged.append(cleaned)
    return merged


def _pdf_readability_metrics(text: str, region: str) -> dict[str, float]:
    total = max(len(text), 1)
    printable = 0
    hangul = 0
    garbled = 0
    for ch in text:
        code = ord(ch)
        if ch.isalpha() or ch.isdigit() or ch.isspace():
            printable += 1
        if 0xAC00 <= code <= 0xD7A3:
            hangul += 1
        if ch == "\ufffd" or 0xE000 <= code <= 0xF8FF or (code < 32 and not ch.isspace()):
            garbled += 1
    compact_len = len(re.sub(r"\s+", "", text))
    printable_ratio = printable / total
    hangul_ratio = hangul / total if region == "korea" else 0.0
    readability_score = printable_ratio
    if region == "korea":
        readability_score = 0.7 * printable_ratio + 0.3 * min(1.0, hangul_ratio * 10)
    return {
        "printable_ratio": printable_ratio,
        "hangul_ratio": hangul_ratio,
        "garbled_ratio": garbled / total,
        "readability_score": readability_score,
        "compact_len": float(compact_len),
    }


def _is_low_quality_pdf_text(text: str, metrics: dict[str, float], region: str) -> bool:
    if metrics["compact_len"] < 60:
        return True
    if metrics["garbled_ratio"] >= 0.18:
        return True
    if metrics["printable_ratio"] < 0.6:
        return True
    if metrics.get("readability_score", 0.0) < 0.55:
        return True
    if region == "korea" and metrics["hangul_ratio"] < 0.05:
        return True
    stripped = _normalize_spaces(text)
    return len(stripped) < 60


def _paragraph_after_heading(text: str) -> str:
    headings = {
        "executive summary",
        "summary",
        "key points",
        "key takeaways",
        "takeaways",
        "conclusion",
        "conclusions",
        "key findings",
        "first take",
    }
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    for idx, line in enumerate(lines):
        normalized = _normalized_heading_key(line)
        if normalized not in headings:
            continue
        collected: list[str] = []
        for candidate in lines[idx + 1 :]:
            cleaned = _normalize_spaces(candidate)
            if not cleaned:
                if collected:
                    break
                continue
            if _normalized_heading_key(cleaned) in headings:
                break
            collected.append(cleaned)
        if collected:
            return " ".join(collected)
    return ""


def _normalized_heading_key(value: str) -> str:
    normalized = _normalize_spaces(value).lower().rstrip(":")
    normalized = re.sub(r"^\d+[\).\-\s]+", "", normalized)
    normalized = re.sub(r"^(section|slide)\s+\d+[\:\-\s]*", "", normalized)
    return normalized


def _remove_slide_noise(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized_counts: dict[str, int] = {}
    normalized_lines: list[tuple[str, str]] = []
    for line in lines:
        cleaned = _normalize_spaces(line)
        normalized = re.sub(r"[^a-z0-9가-힣]+", "", cleaned.lower())
        normalized_lines.append((cleaned, normalized))
        if len(normalized) >= 4:
            normalized_counts[normalized] = normalized_counts.get(normalized, 0) + 1

    cleaned_lines: list[str] = []
    agenda_pattern = re.compile(r"\b(agenda|table of contents|contents)\b", flags=re.IGNORECASE)
    page_pattern = re.compile(r"^(page\s*)?\d{1,3}(\s*/\s*\d{1,3}|\s+of\s+\d{1,3})?$", flags=re.IGNORECASE)
    section_pattern = re.compile(r"^(section|slide)\s+\d+\b", flags=re.IGNORECASE)
    boilerplate_pattern = re.compile(
        r"^(updated|definition|source|copyright|all rights reserved|confidential)\b",
        flags=re.IGNORECASE,
    )
    date_pattern = re.compile(
        r"^((20\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])|"
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+20\d{2})$",
        flags=re.IGNORECASE,
    )
    punctuation_pattern = re.compile(r"^[^a-z0-9가-힣]{3,}$", flags=re.IGNORECASE)

    for cleaned, normalized in normalized_lines:
        if not cleaned:
            cleaned_lines.append("")
            continue
        if agenda_pattern.search(cleaned):
            continue
        if page_pattern.match(cleaned):
            continue
        if section_pattern.match(cleaned):
            continue
        if boilerplate_pattern.match(cleaned):
            continue
        if date_pattern.match(cleaned):
            continue
        if punctuation_pattern.match(cleaned):
            continue
        if normalized:
            repeats = normalized_counts.get(normalized, 0)
            if repeats >= 3:
                continue
            if repeats >= 2 and len(normalized) <= 24:
                continue
        cleaned_lines.append(cleaned)

    compact = "\n".join(cleaned_lines)
    compact = re.sub(r"\n{3,}", "\n\n", compact)
    return compact.strip()


def _keyword_density_summary(text: str) -> str:
    normalized = _normalize_spaces(text)
    sentence_candidates = _sentence_candidates(text)
    if not sentence_candidates:
        return ""

    keyword_counts: dict[str, int] = {}
    for token in _summary_tokens(normalized):
        keyword_counts[token] = keyword_counts.get(token, 0) + 1
    ranked_keywords = [token for token, count in sorted(keyword_counts.items(), key=lambda row: (-row[1], row[0])) if count >= 2][:12]
    if not ranked_keywords:
        ranked_keywords = [token for token, _ in sorted(keyword_counts.items(), key=lambda row: (-row[1], row[0]))[:8]]
    keyword_set = set(ranked_keywords)

    scored: list[tuple[float, int, str]] = []
    for idx, sentence in enumerate(sentence_candidates):
        tokens = _summary_tokens(sentence)
        if not tokens:
            continue
        density_hits = sum(1 for token in tokens if token in keyword_set)
        density = density_hits / len(tokens)
        length_bonus = min(len(tokens), 40) / 40
        score = density * 10 + length_bonus
        scored.append((score, idx, sentence))

    if not scored:
        return ""

    ranked = sorted(scored, key=lambda row: (-row[0], row[1]))
    take_count = 5 if len(ranked) >= 5 else len(ranked)
    if take_count < 3:
        take_count = len(ranked)
    selected = sorted(ranked[:take_count], key=lambda row: row[1])
    return _normalize_spaces(" ".join(sentence for _, _, sentence in selected))


def _sentence_candidates(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    candidates: list[str] = []
    for chunk in chunks:
        sentence = _normalize_spaces(chunk)
        if not sentence:
            continue
        if len(sentence) < 40 or len(sentence) > 320:
            continue
        candidates.append(sentence)
    return candidates


def _summary_tokens(text: str) -> list[str]:
    stopwords = {
        "the",
        "and",
        "for",
        "that",
        "with",
        "from",
        "this",
        "into",
        "over",
        "under",
        "across",
        "agenda",
        "slide",
        "page",
        "summary",
    }
    tokens = re.findall(r"[a-z]{3,}|[0-9]{2,}|[가-힣]{2,}", text.lower())
    return [token for token in tokens if token not in stopwords]


def _normalize_spaces(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()
