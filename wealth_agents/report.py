from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from difflib import SequenceMatcher
import json
from pathlib import Path
import re
from typing import Any, Optional

from .rules import item_categories, load_rules, score_item, summarize_item
from .storage import read_jsonl
from .utils import canonicalize_url, sanitize_html_summary, sentence_from_title

DEFAULT_TITLE_STOPWORDS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "to",
    "with",
}
TOPIC_SIGNATURE_STOPWORDS = DEFAULT_TITLE_STOPWORDS | {
    "news",
    "report",
    "reports",
    "update",
    "weekly",
    "market",
    "markets",
    "today",
    "analysis",
    "says",
    "said",
}
LOW_QUALITY_PDF_SUMMARY = (
    "PDF text extraction is low-quality; please add a manual summary in a .md file with the same base name."
)
LOW_QUALITY_PDF_SCORE_THRESHOLD = 0.55
DEFAULT_WEEKLY_AGGREGATES_PATH = "data/meta/weekly_aggregates.jsonl"
NON_ALNUM_RE = re.compile(r"[^a-z0-9가-힣\s]+")
MULTISPACE_RE = re.compile(r"\s+")


def weekly_filename(week: str) -> str:
    return f"weekly_{week}.md"


def _parse_week(week: str) -> tuple[datetime, datetime]:
    year_str, week_str = week.split("-W")
    year = int(year_str)
    week_num = int(week_str)
    week_start_date = date.fromisocalendar(year, week_num, 1)
    week_end_date = date.fromisocalendar(year, week_num, 7)
    week_start = datetime.combine(week_start_date, datetime.min.time(), tzinfo=timezone.utc)
    week_end = datetime.combine(week_end_date, datetime.max.time(), tzinfo=timezone.utc)
    return week_start, week_end


def _parse_iso8601_utc(value: object) -> Optional[datetime]:
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


def _item_timestamp(item: dict) -> Optional[datetime]:
    for field in ("published_at", "fetched_at"):
        value = item.get(field)
        parsed = _parse_iso8601_utc(value)
        if parsed is not None:
            return parsed
    return None


def _item_tags(item: dict) -> set[str]:
    tags = item.get("tags") or []
    normalized: set[str] = set()
    for tag in tags:
        if isinstance(tag, str):
            normalized.add(tag.strip().lower())
    return normalized


def _dedup_settings(rules: dict) -> dict:
    dedup = rules.get("dedup") or {}
    preference = dedup.get("preference") or {}
    custom_stopwords = dedup.get("stopwords") or []

    stopwords = set(DEFAULT_TITLE_STOPWORDS)
    for word in custom_stopwords:
        if isinstance(word, str) and word.strip():
            stopwords.add(word.strip().lower())

    return {
        "token_jaccard_threshold": float(dedup.get("token_jaccard_threshold", dedup.get("jaccard_threshold", 0.8))),
        "ngram_jaccard_threshold": float(dedup.get("ngram_jaccard_threshold", 0.45)),
        "sequence_threshold": float(dedup.get("sequence_threshold", 0.9)),
        "similarity_threshold": float(dedup.get("similarity_threshold", 0.72)),
        "summary_weight": float(preference.get("summary_weight", 1.0)),
        "published_bonus": float(preference.get("published_bonus", 200.0)),
        "non_manual_bonus": float(preference.get("non_manual_bonus", 100.0)),
        "stopwords": stopwords,
    }


def _tokenize_text(text: str, stopwords: set[str]) -> list[str]:
    cleaned = NON_ALNUM_RE.sub(" ", text.lower())
    return [token for token in cleaned.split() if token and token not in stopwords]


def _normalized_text(text: str) -> str:
    return MULTISPACE_RE.sub(" ", NON_ALNUM_RE.sub(" ", text.lower())).strip()


def _jaccard_similarity(left: set[str] | frozenset[str], right: set[str] | frozenset[str]) -> float:
    union = left | right
    if not union:
        return 0.0
    return len(left & right) / len(union)


def _token_and_ngram_sets(text: str, stopwords: set[str], n: int = 2) -> tuple[set[str], set[str]]:
    tokens = _tokenize_text(text, stopwords)
    token_set = set(tokens)
    if len(tokens) < n:
        return token_set, set()
    return token_set, {" ".join(tokens[idx : idx + n]) for idx in range(0, len(tokens) - n + 1)}


def _text_similarity_metrics(
    text_a: str,
    text_b: str,
    stopwords: set[str],
) -> tuple[float, float, float]:
    tokens_a, ngrams_a = _token_and_ngram_sets(text_a, stopwords, n=2)
    tokens_b, ngrams_b = _token_and_ngram_sets(text_b, stopwords, n=2)
    token_jaccard = _jaccard_similarity(tokens_a, tokens_b)
    ngram_jaccard = _jaccard_similarity(ngrams_a, ngrams_b)
    sequence_ratio = SequenceMatcher(None, _normalized_text(text_a), _normalized_text(text_b)).ratio()
    return token_jaccard, ngram_jaccard, sequence_ratio


def _title_similarity(title_a: str, title_b: str, settings: dict) -> bool:
    token_jaccard, ngram_jaccard, sequence_ratio = _text_similarity_metrics(
        title_a,
        title_b,
        settings["stopwords"],
    )
    if (
        token_jaccard >= settings["token_jaccard_threshold"]
        and ngram_jaccard >= settings["ngram_jaccard_threshold"]
    ):
        return True
    if sequence_ratio >= settings["sequence_threshold"]:
        return True
    combined = 0.55 * token_jaccard + 0.30 * ngram_jaccard + 0.15 * sequence_ratio
    return combined >= settings["similarity_threshold"]


def _record_preference_score(item: dict, settings: dict) -> float:
    summary = str(item.get("summary") or item.get("body") or "")
    score = len(summary) * settings["summary_weight"]
    if item.get("published_at"):
        score += settings["published_bonus"]
    if not _is_manual_item(item):
        score += settings["non_manual_bonus"]
    return score


def _is_near_duplicate_story(item_a: dict, item_b: dict, settings: dict) -> bool:
    url_a = canonicalize_url(str(item_a.get("url") or ""))
    url_b = canonicalize_url(str(item_b.get("url") or ""))
    if url_a and url_b and url_a == url_b:
        return True

    title_a = str(item_a.get("title") or "")
    title_b = str(item_b.get("title") or "")
    if not title_a or not title_b:
        return False
    if _title_similarity(title_a, title_b, settings):
        return True

    text_a = f"{title_a} {item_a.get('summary') or ''}"
    text_b = f"{title_b} {item_b.get('summary') or ''}"
    token_jaccard, ngram_jaccard, sequence_ratio = _text_similarity_metrics(
        text_a,
        text_b,
        settings["stopwords"],
    )
    combined = 0.60 * token_jaccard + 0.30 * ngram_jaccard + 0.10 * sequence_ratio
    return combined >= settings["similarity_threshold"]


def _deduplicate_weekly_items(items: list[dict], rules: dict) -> list[dict]:
    settings = _dedup_settings(rules)
    deduped: list[dict] = []

    for item in items:
        match_index = None
        for idx, existing in enumerate(deduped):
            if _is_near_duplicate_story(item, existing, settings):
                match_index = idx
                break
        if match_index is None:
            deduped.append(item)
            continue

        preferred = deduped[match_index]
        if _record_preference_score(item, settings) > _record_preference_score(preferred, settings):
            deduped[match_index] = item

    return deduped


def _top10_settings(rules: dict) -> dict:
    top10 = rules.get("top10") or {}
    dedup_stopwords = set(_dedup_settings(rules)["stopwords"])
    return {
        "cluster_cap": max(1, int(top10.get("cluster_cap", 3))),
        "quota_korea": max(0, int(top10.get("quota_korea", 1))),
        "quota_germany": max(0, int(top10.get("quota_germany", 1))),
        "cluster_similarity_threshold": float(top10.get("cluster_similarity_threshold", 0.45)),
        "stopwords": TOPIC_SIGNATURE_STOPWORDS | dedup_stopwords,
    }


def _topic_similarity_score(item_a: dict, item_b: dict, stopwords: set[str]) -> float:
    text_a = f"{item_a.get('title', '')} {item_a.get('summary', '')}"
    text_b = f"{item_b.get('title', '')} {item_b.get('summary', '')}"
    token_jaccard, ngram_jaccard, sequence_ratio = _text_similarity_metrics(text_a, text_b, stopwords)
    return 0.60 * token_jaccard + 0.30 * ngram_jaccard + 0.10 * sequence_ratio


def _build_topic_cluster_ids(items: list[dict], settings: dict) -> dict[int, int]:
    clusters: list[dict] = []
    cluster_ids: dict[int, int] = {}
    for item in items:
        best_cluster = -1
        best_score = 0.0
        for cluster_idx, cluster in enumerate(clusters):
            score = _topic_similarity_score(item, cluster["representative"], settings["stopwords"])
            if score > best_score:
                best_cluster = cluster_idx
                best_score = score
        if best_cluster >= 0 and best_score >= settings["cluster_similarity_threshold"]:
            cluster_ids[id(item)] = best_cluster
            continue
        clusters.append({"representative": item})
        cluster_ids[id(item)] = len(clusters) - 1
    return cluster_ids


def _required_region_counts(items: list[dict], settings: dict, limit: int) -> dict[str, int]:
    targets = {
        "korea": int(settings["quota_korea"]),
        "germany": int(settings["quota_germany"]),
    }
    required: dict[str, int] = {}
    for region, quota in targets.items():
        if quota <= 0:
            required[region] = 0
            continue
        pool_size = sum(1 for item in items if region in _item_tags(item))
        required[region] = min(quota, pool_size, limit)
    return required


def _is_regional_or_manual(item: dict) -> bool:
    tags = _item_tags(item)
    return _is_manual_item(item) or "korea" in tags or "germany" in tags


def _select_top10_diversified(scored_items: list[dict], rules: dict, limit: int = 10) -> list[dict]:
    settings = _top10_settings(rules)
    cluster_ids = _build_topic_cluster_ids(scored_items, settings)
    cluster_cap = int(settings["cluster_cap"])
    selected: list[dict] = []
    cluster_counts: dict[int, int] = {}

    for index, item in enumerate(scored_items):
        cluster = cluster_ids.get(id(item), -1)
        already = cluster_counts.get(cluster, 0)

        if already >= cluster_cap:
            has_alternative = any(
                (
                    cluster_ids.get(id(candidate), -1) != cluster
                    and cluster_counts.get(cluster_ids.get(id(candidate), -1), 0) < cluster_cap
                )
                for candidate in scored_items[index + 1 :]
            )
            if has_alternative:
                continue

        selected.append(item)
        cluster_counts[cluster] = already + 1
        if len(selected) >= limit:
            break

    required_regions = _required_region_counts(scored_items, settings, limit=limit)
    selected_ids = {id(item) for item in selected}
    selected_region_counts = {
        region: sum(1 for item in selected if region in _item_tags(item))
        for region in required_regions
    }

    for region in ("korea", "germany"):
        required = int(required_regions.get(region, 0))
        if required <= 0:
            continue
        while selected_region_counts.get(region, 0) < required:
            candidate = None
            for item in scored_items:
                if id(item) in selected_ids:
                    continue
                if region not in _item_tags(item):
                    continue
                candidate = item
                break
            if candidate is None:
                break

            candidate_cluster = cluster_ids.get(id(candidate), -1)
            replace_index = None
            for idx in range(len(selected) - 1, -1, -1):
                existing = selected[idx]
                existing_tags = _item_tags(existing)
                if region in existing_tags:
                    continue

                can_remove = True
                for quota_region, quota_required in required_regions.items():
                    if quota_required <= 0:
                        continue
                    if quota_region in existing_tags and selected_region_counts.get(quota_region, 0) - 1 < quota_required:
                        can_remove = False
                        break
                if not can_remove:
                    continue

                existing_cluster = cluster_ids.get(id(existing), -1)
                candidate_cluster_count = cluster_counts.get(candidate_cluster, 0)
                if existing_cluster != candidate_cluster and candidate_cluster_count >= cluster_cap:
                    continue
                replace_index = idx
                break

            if replace_index is None:
                break

            removed = selected[replace_index]
            removed_cluster = cluster_ids.get(id(removed), -1)
            cluster_counts[removed_cluster] = max(0, cluster_counts.get(removed_cluster, 0) - 1)
            if cluster_counts[removed_cluster] == 0:
                cluster_counts.pop(removed_cluster, None)

            selected[replace_index] = candidate
            selected_ids.discard(id(removed))
            selected_ids.add(id(candidate))
            cluster_counts[candidate_cluster] = cluster_counts.get(candidate_cluster, 0) + 1

            removed_tags = _item_tags(removed)
            candidate_tags = _item_tags(candidate)
            for quota_region in required_regions:
                if quota_region in removed_tags:
                    selected_region_counts[quota_region] = max(0, selected_region_counts.get(quota_region, 0) - 1)
                if quota_region in candidate_tags:
                    selected_region_counts[quota_region] = selected_region_counts.get(quota_region, 0) + 1

    selected.sort(key=lambda row: float(row.get("_score") or 0.0), reverse=True)

    return selected


def _is_manual_item(item: dict) -> bool:
    tags = _item_tags(item)
    return item.get("source") == "manual" or "manual" in tags


def _manual_pdf_summary(item: dict) -> str:
    summary_text = str(item.get("summary") or "").strip()
    has_pdf_attachment = bool(str(item.get("attachment_path") or "").strip()) or "pdf" in _item_tags(item)
    tags = _item_tags(item)

    if "md_override" in tags:
        preferred = summary_text or str(item.get("body") or "").strip()
        if preferred:
            return re.sub(r"\s+", " ", preferred).strip()
        return sentence_from_title(str(item.get("title") or "Untitled"))

    if summary_text.startswith(LOW_QUALITY_PDF_SUMMARY):
        scaffold_path = str(item.get("summary_scaffold_path") or "").strip()
        scaffold_created = bool(item.get("summary_scaffold_created"))
        if scaffold_created and scaffold_path and not _is_placeholder_scaffold_path(scaffold_path):
            return f"Summary needed. Scaffold created at: {scaffold_path}"
        return summary_text

    if has_pdf_attachment:
        raw = str(summary_text or item.get("body") or "").strip()
    else:
        raw = str(item.get("body") or summary_text).strip()
    if not raw:
        return sentence_from_title(str(item.get("title") or "Untitled"))

    text = _clean_manual_pdf_text(raw)
    heading_para = _paragraph_after_heading(text)
    if heading_para:
        cleaned = re.sub(r"\s+", " ", heading_para).strip()
        return cleaned

    dense = _keyword_density_sentence_summary(text)
    if dense:
        return dense

    sentence_chunks = [s.strip() for s in re.split(r"(?<=[.!?])\s+", re.sub(r"\s+", " ", text)) if s.strip()]
    if not sentence_chunks:
        fallback = re.sub(r"\s+", " ", text).strip()
        return fallback if fallback else sentence_from_title(str(item.get("title") or "Untitled"))

    count = 5 if len(sentence_chunks) >= 5 else len(sentence_chunks)
    if count < 3:
        count = len(sentence_chunks)
    rendered = " ".join(sentence_chunks[:count]).strip()
    return rendered


def _is_low_quality_pdf_item(item: dict) -> bool:
    attachment_path = str(item.get("attachment_path") or "").strip()
    if not attachment_path:
        return False

    summary = str(item.get("summary") or "").strip().lower()
    if summary.startswith(LOW_QUALITY_PDF_SUMMARY.lower()):
        return True

    score = item.get("readability_score")
    try:
        return float(score) < LOW_QUALITY_PDF_SCORE_THRESHOLD
    except (TypeError, ValueError):
        return False


def _is_placeholder_scaffold_path(path: str) -> bool:
    if not path:
        return False
    md_path = Path(path)
    if not md_path.is_absolute():
        md_path = Path.cwd() / md_path
    if not md_path.exists():
        return False
    try:
        text = md_path.read_text(encoding="utf-8")
    except Exception:
        return False

    lowered = text.lower()
    if "(fill in)" in lowered:
        return True
    front_matter = _extract_front_matter(text)
    if "needs_summary" in front_matter.lower():
        return True
    body = _strip_front_matter(text)
    if len(re.sub(r"\s+", "", body)) < 50:
        return True
    return False


def _extract_front_matter(text: str) -> str:
    match = re.match(r"\A---\s*\n(.*?)\n---\s*(?:\n|$)", text, flags=re.DOTALL)
    if not match:
        return ""
    return str(match.group(1) or "")


def _strip_front_matter(text: str) -> str:
    return re.sub(r"\A---\s*\n.*?\n---\s*(?:\n|$)", "", text, flags=re.DOTALL)


def _paragraph_after_heading(text: str) -> str:
    headings = {
        "summary",
        "executive summary",
        "key points",
        "key takeaways",
        "takeaways",
        "conclusion",
        "conclusions",
        "key findings",
        "first take",
    }
    lines = text.split("\n")
    for idx, line in enumerate(lines):
        stripped = _normalized_heading_key(line)
        if stripped not in headings:
            continue

        collected: list[str] = []
        for next_line in lines[idx + 1 :]:
            candidate = next_line.strip()
            if not candidate:
                if collected:
                    break
                continue
            lowered = _normalized_heading_key(candidate)
            if lowered in headings:
                break
            collected.append(candidate)
        if collected:
            return " ".join(collected)
    return ""


def _normalized_heading_key(value: str) -> str:
    normalized = re.sub(r"\s+", " ", value).strip().lower().rstrip(":")
    normalized = re.sub(r"^\d+[\).\-\s]+", "", normalized)
    normalized = re.sub(r"^(section|slide)\s+\d+[\:\-\s]*", "", normalized)
    return normalized


def _clean_manual_pdf_text(text: str) -> str:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    normalized_counts: dict[str, int] = {}
    normalized_lines: list[tuple[str, str]] = []
    for line in lines:
        cleaned = re.sub(r"[\uE000-\uF8FF]", " ", line)
        cleaned = re.sub(r"[^\S\n]+", " ", cleaned).strip()
        normalized = re.sub(r"[^a-z0-9가-힣]+", "", cleaned.lower())
        normalized_lines.append((cleaned, normalized))
        if len(normalized) >= 4:
            normalized_counts[normalized] = normalized_counts.get(normalized, 0) + 1

    filtered: list[str] = []
    boilerplate = re.compile(
        r"^(updated|next release|definition|source|copyright|all rights reserved|confidential)\b",
        flags=re.IGNORECASE,
    )
    slide_noise = re.compile(r"\b(agenda|table of contents|contents)\b", flags=re.IGNORECASE)
    page_pattern = re.compile(r"^(page\s*)?\d{1,3}(\s*/\s*\d{1,3}|\s+of\s+\d{1,3})?$", flags=re.IGNORECASE)
    section_pattern = re.compile(r"^(section|slide)\s+\d+\b", flags=re.IGNORECASE)
    date_pattern = re.compile(
        r"^((20\d{2})[-/.](0?[1-9]|1[0-2])[-/.](0?[1-9]|[12]\d|3[01])|"
        r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\s+\d{1,2},?\s+20\d{2})$",
        flags=re.IGNORECASE,
    )
    punctuation_pattern = re.compile(r"^[^a-z0-9가-힣]{3,}$", flags=re.IGNORECASE)
    for cleaned, normalized in normalized_lines:
        if not cleaned:
            filtered.append("")
            continue
        if boilerplate.match(cleaned):
            continue
        if slide_noise.search(cleaned):
            continue
        if page_pattern.match(cleaned):
            continue
        if section_pattern.match(cleaned):
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
        filtered.append(cleaned)
    compact = "\n".join(filtered)
    compact = re.sub(r"\n{3,}", "\n\n", compact)
    return compact.strip()


def _keyword_density_sentence_summary(text: str) -> str:
    sentence_candidates = _pdf_sentence_candidates(text)
    if not sentence_candidates:
        return ""

    keyword_counts: dict[str, int] = {}
    for token in _pdf_summary_tokens(re.sub(r"\s+", " ", text).lower()):
        keyword_counts[token] = keyword_counts.get(token, 0) + 1
    keywords = [token for token, count in sorted(keyword_counts.items(), key=lambda row: (-row[1], row[0])) if count >= 2][:12]
    if not keywords:
        keywords = [token for token, _ in sorted(keyword_counts.items(), key=lambda row: (-row[1], row[0]))[:8]]
    keyword_set = set(keywords)

    scored: list[tuple[float, int, str]] = []
    for idx, sentence in enumerate(sentence_candidates):
        tokens = _pdf_summary_tokens(sentence.lower())
        if not tokens:
            continue
        hits = sum(1 for token in tokens if token in keyword_set)
        density = hits / len(tokens)
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
    return re.sub(r"\s+", " ", " ".join(sentence for _, _, sentence in selected)).strip()


def _pdf_sentence_candidates(text: str) -> list[str]:
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    candidates: list[str] = []
    for chunk in chunks:
        cleaned = re.sub(r"\s+", " ", chunk).strip()
        if not cleaned:
            continue
        if len(cleaned) < 40 or len(cleaned) > 320:
            continue
        candidates.append(cleaned)
    return candidates


def _pdf_summary_tokens(text: str) -> list[str]:
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
    tokens = re.findall(r"[a-z]{3,}|[0-9]{2,}|[가-힣]{2,}", text)
    return [token for token in tokens if token not in stopwords]


def _truncate_summary(text: str, max_chars: int) -> str:
    text = text.strip()
    if len(text) <= max_chars:
        return text
    clipped = text[: max_chars - 1].rstrip()
    return clipped + "…"


def _report_summary(item: dict, max_chars: int) -> str:
    if _is_manual_item(item) or "pdf" in _item_tags(item):
        return _truncate_summary(_manual_pdf_summary(item), max_chars=max_chars)

    item_copy = dict(item)
    item_copy["summary"] = sanitize_html_summary(str(item.get("summary") or ""))
    rendered = summarize_item(item_copy)
    return _truncate_summary(rendered, max_chars=max_chars)


def _focus_priority(item: dict, region: str) -> tuple[int, float]:
    tags = _item_tags(item)
    score = float(item.get("_score") or 0.0)
    if _is_manual_item(item) and region in tags:
        return (0, -score)
    if _is_manual_item(item):
        return (1, -score)
    return (2, -score)


def _render_focus_section(lines: list[str], heading: str, items: list[dict], empty_note: str) -> None:
    lines.append(heading)
    lines.append("")
    if not items:
        lines.append(empty_note)
        lines.append("")
        return
    for item in items[:5]:
        lines.append(f"- {item.get('title', 'Untitled')} ({item.get('source', 'unknown')})")
        attachment_path = str(item.get("attachment_path") or "").strip()
        if attachment_path:
            lines.append(f"  Attachment: {attachment_path}")
        lines.append(f"  published_at: {item.get('published_at')}")
        if item.get("url"):
            lines.append(f"  url: {item.get('url')}")
        lines.append(f"  summary: {_report_summary(item, max_chars=600)}")
    lines.append("")


def _filter_week_items(
    all_items: list[dict],
    target_year: int,
    target_week: int,
    rules: dict,
    week_end_ts: float,
) -> list[dict]:
    selected: list[dict] = []
    for item in all_items:
        ts = _item_timestamp(item)
        if ts is None:
            continue
        iso = ts.isocalendar()
        if iso.year != target_year or iso.week != target_week:
            continue
        item_copy = dict(item)
        item_copy["_item_date"] = ts.isoformat().replace("+00:00", "Z")
        item_copy["_score"] = score_item(item_copy, rules=rules, week_end_ts=week_end_ts)
        selected.append(item_copy)
    return selected


def _week_key(year: int, week: int) -> str:
    return f"{year}-W{week:02d}"


def _category_counts(items: list[dict], rules: dict) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in items:
        for category in item_categories(item, rules):
            counts[category] = counts.get(category, 0) + 1
    return counts


def _keywords_from_rules(rules: dict) -> list[str]:
    seen: set[str] = set()
    keywords: list[str] = []
    for raw in rules.get("top_terms") or []:
        keyword = str(raw).strip().lower()
        if not keyword or keyword in seen:
            continue
        seen.add(keyword)
        keywords.append(keyword)
    return keywords


def _keyword_match(text: str, keyword: str) -> bool:
    if not keyword:
        return False
    pattern = re.compile(rf"(?<!\w){re.escape(keyword)}(?!\w)")
    return pattern.search(text) is not None


def _keyword_counts(items: list[dict], rules: dict, region: Optional[str] = None) -> dict[str, int]:
    keywords = _keywords_from_rules(rules)
    counts = {keyword: 0 for keyword in keywords}
    for item in items:
        if region and region not in _item_tags(item):
            continue
        text = f"{item.get('title', '')} {item.get('summary', '')}".lower()
        for keyword in keywords:
            if _keyword_match(text, keyword):
                counts[keyword] += 1
    return counts


def _build_weekly_aggregate(
    week: str,
    items: list[dict],
    rules: dict,
    generated_at: str,
    duplicates_removed_count: int,
) -> dict[str, Any]:
    return {
        "week": week,
        "generated_at": generated_at,
        "item_count": len(items),
        "manual_items_count": sum(1 for item in items if _is_manual_item(item)),
        "duplicates_removed_count": int(duplicates_removed_count),
        "category_counts": _category_counts(items, rules),
        "keyword_counts": _keyword_counts(items, rules),
        "regional_keyword_counts": {
            "korea": _keyword_counts(items, rules, region="korea"),
            "germany": _keyword_counts(items, rules, region="germany"),
        },
    }


def _load_weekly_aggregates(path: str) -> dict[str, dict]:
    rows = read_jsonl(Path(path))
    by_week: dict[str, dict] = {}
    for row in rows:
        week = str(row.get("week") or "").strip()
        if not week:
            continue
        by_week[week] = row
    return by_week


def _upsert_weekly_aggregate(path: str, aggregate: dict) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    rows = _load_weekly_aggregates(path)
    rows[str(aggregate["week"])] = aggregate
    ordered_weeks = sorted(rows.keys())
    with target.open("w", encoding="utf-8") as handle:
        for week in ordered_weeks:
            handle.write(json.dumps(rows[week], ensure_ascii=False) + "\n")


def _delta_rows(current: dict[str, int], prior: dict[str, int]) -> list[tuple[str, int, int, int]]:
    rows: list[tuple[str, int, int, int]] = []
    for key in sorted(set(current.keys()) | set(prior.keys())):
        this_week = int(current.get(key, 0))
        last_week = int(prior.get(key, 0))
        rows.append((key, this_week, last_week, this_week - last_week))
    return rows


def _choose_keyword_table_rows(
    keyword_deltas: list[tuple[str, int, int, int]],
    has_prior: bool,
    limit: int = 8,
) -> list[tuple[str, int, int, int]]:
    if not keyword_deltas:
        return []

    if has_prior:
        rising = [row for row in keyword_deltas if row[3] > 0]
        if rising:
            return sorted(rising, key=lambda row: (-row[3], -row[1], row[0]))[:limit]

    nonzero = [row for row in keyword_deltas if row[1] > 0 or row[2] > 0]
    ranked = nonzero if nonzero else keyword_deltas
    return sorted(ranked, key=lambda row: (-row[1], -row[3], row[0]))[:limit]


def _generate_wow_signals(current_aggregate: dict, prior_aggregate: Optional[dict]) -> tuple[list[str], list[tuple[str, int, int, int]]]:
    lines: list[str] = []
    current_cat = dict(current_aggregate.get("category_counts") or {})
    current_keywords = dict(current_aggregate.get("keyword_counts") or {})
    has_prior = prior_aggregate is not None

    if not has_prior:
        lines.append("Baseline not available for prior week; week-over-week deltas are unavailable.")
        for category, count in sorted(current_cat.items(), key=lambda row: row[1], reverse=True)[:3]:
            lines.append(f"{category.replace('_', ' ')} appeared in {count} items this week.")
        for keyword, count in sorted(current_keywords.items(), key=lambda row: row[1], reverse=True)[:5]:
            if count > 0:
                lines.append(f"Keyword spike monitor: '{keyword}' appeared in {count} items this week.")
        if len(lines) == 1:
            lines.append("No strong rule-based signals were detected this week.")
        table_rows = _choose_keyword_table_rows(
            _delta_rows(current_keywords, {}),
            has_prior=False,
        )
        return lines, table_rows

    prior_cat = dict((prior_aggregate or {}).get("category_counts") or {})
    category_rows = _delta_rows(current_cat, prior_cat)
    for category, current, previous, delta in sorted(
        category_rows,
        key=lambda row: (-abs(row[3]), -row[1], row[0]),
    )[:3]:
        lines.append(f"{category.replace('_', ' ')} items: {current} ({delta:+} vs prior week {previous}).")

    prior_keywords = dict((prior_aggregate or {}).get("keyword_counts") or {})
    keyword_rows = _delta_rows(current_keywords, prior_keywords)
    rising_keywords = [row for row in keyword_rows if row[3] > 0]
    for keyword, current, previous, delta in sorted(
        rising_keywords,
        key=lambda row: (-row[3], -row[1], row[0]),
    )[:3]:
        lines.append(f"Keyword spike '{keyword}': {current} ({delta:+} vs prior week {previous}).")

    for region in ("korea", "germany"):
        current_regional = dict((current_aggregate.get("regional_keyword_counts") or {}).get(region) or {})
        prior_regional = dict(((prior_aggregate or {}).get("regional_keyword_counts") or {}).get(region) or {})
        regional_rows = [row for row in _delta_rows(current_regional, prior_regional) if row[3] > 0]
        if not regional_rows:
            continue
        keyword, current, previous, delta = sorted(
            regional_rows,
            key=lambda row: (-row[3], -row[1], row[0]),
        )[0]
        lines.append(
            f"{region.title()} spike '{keyword}': {current} ({delta:+} vs prior week {previous})."
        )

    if not lines:
        lines.append("No strong rule-based signals were detected this week.")
    return lines, _choose_keyword_table_rows(keyword_rows, has_prior=True)


def _load_last_collect(path: str) -> dict:
    meta_path = Path(path)
    if not meta_path.exists():
        return {}
    try:
        parsed = json.loads(meta_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _coerce_int(value: object) -> Optional[int]:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _derive_collect_status(feeds_success: object, feeds_failed: object, fallback_status: object) -> str:
    success = _coerce_int(feeds_success)
    failed = _coerce_int(feeds_failed)
    if success is not None and failed is not None:
        if failed == 0:
            return "success"
        if success > 0 and failed > 0:
            return "partial"
        if success == 0:
            return "failed"
    return str(fallback_status or "n/a")


def _region_feed_health(collect_meta: dict, region: str) -> Optional[dict[str, int]]:
    statuses = collect_meta.get("feed_statuses")
    if not isinstance(statuses, list):
        return None
    region_rows = []
    for row in statuses:
        if not isinstance(row, dict):
            continue
        tags = row.get("tags")
        if not isinstance(tags, list):
            continue
        normalized_tags = {str(tag).strip().lower() for tag in tags}
        if region in normalized_tags:
            region_rows.append(row)
    if not region_rows:
        return None
    success = 0
    failed = 0
    for row in region_rows:
        status = str(row.get("status") or "").strip().lower()
        if status == "ok":
            success += 1
        else:
            failed += 1
    return {"total": len(region_rows), "success": success, "failed": failed}


def _focus_empty_reason(
    region: str,
    region_items_count: int,
    manual_region_items_count: int,
    collect_meta: dict,
) -> str:
    if region_items_count > 0:
        return f"No {region}-focused items available for this week."

    region_health = _region_feed_health(collect_meta, region)
    if region_health:
        if region_health["success"] == 0 and region_health["failed"] > 0:
            if manual_region_items_count == 0:
                return f"No {region} items: region feeds failed this week and no manual inputs were provided."
            return f"No {region} items: region feeds failed this week."
        if region_health["success"] > 0 and manual_region_items_count == 0:
            return f"No {region} items: feeds were reachable but no {region} matches and no manual inputs were provided."

    if manual_region_items_count == 0:
        return f"No {region} items: no items matched this week and no manual inputs were provided."
    return f"No {region}-focused items available for this week."


def _render_keyword_delta_table(lines: list[str], rows: list[tuple[str, int, int, int]]) -> None:
    lines.append("### Top Rising Keywords (WoW)")
    lines.append("")
    if not rows:
        lines.append("No configured keywords matched this week.")
        lines.append("")
        return

    lines.append("| keyword | this_week | last_week | delta |")
    lines.append("| --- | ---: | ---: | ---: |")
    for keyword, this_week, last_week, delta in rows:
        lines.append(f"| {keyword} | {this_week} | {last_week} | {delta:+} |")
    lines.append("")


def generate_weekly_report(
    week: str,
    data_path: str = "data/raw/news.jsonl",
    rules_path: str = "config/rules.yml",
    output_dir: str = "reports",
    collect_meta_path: str = "data/meta/last_collect.json",
    weekly_aggregates_path: str = DEFAULT_WEEKLY_AGGREGATES_PATH,
) -> Path:
    week_start, week_end = _parse_week(week)
    week_year, week_num = week_start.isocalendar().year, week_start.isocalendar().week
    previous_week_date = week_start - timedelta(days=7)
    prev_iso = previous_week_date.isocalendar()
    _, prev_week_end = _parse_week(_week_key(prev_iso.year, prev_iso.week))
    rules = load_rules(rules_path)

    all_items = read_jsonl(Path(data_path))
    weekly_raw_items = _filter_week_items(
        all_items=all_items,
        target_year=week_year,
        target_week=week_num,
        rules=rules,
        week_end_ts=week_end.timestamp(),
    )
    weekly_items = _deduplicate_weekly_items(weekly_raw_items, rules=rules)
    duplicates_removed_count = len(weekly_raw_items) - len(weekly_items)

    prior_raw_items = _filter_week_items(
        all_items=all_items,
        target_year=prev_iso.year,
        target_week=prev_iso.week,
        rules=rules,
        week_end_ts=prev_week_end.timestamp(),
    )
    prior_items = _deduplicate_weekly_items(prior_raw_items, rules=rules)
    prior_duplicates_removed = len(prior_raw_items) - len(prior_items)

    scored = sorted(weekly_items, key=lambda x: float(x.get("_score") or 0.0), reverse=True)
    top10_candidates = [item for item in scored if not _is_low_quality_pdf_item(item)]
    top10 = _select_top10_diversified(top10_candidates or scored, rules=rules, limit=10)

    korea_focus = sorted(
        [item for item in scored if "korea" in _item_tags(item)],
        key=lambda item: _focus_priority(item, "korea"),
    )[:5]
    germany_focus = sorted(
        [item for item in scored if "germany" in _item_tags(item)],
        key=lambda item: _focus_priority(item, "germany"),
    )[:5]
    manual_pool = [item for item in scored if item.get("source") == "manual" or "manual" in _item_tags(item)]
    manual_focus = sorted(
        manual_pool,
        key=lambda item: (
            0 if {"korea", "germany"} & _item_tags(item) else 1,
            0 if _is_manual_item(item) else 1,
            -float(item.get("_score") or 0.0),
        ),
    )[:5]

    categories = defaultdict(list)
    for item in weekly_items:
        cats = item_categories(item, rules)
        if not cats:
            continue
        for cat in cats:
            categories[cat].append(item)

    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    previous_week_key = _week_key(prev_iso.year, prev_iso.week)
    current_aggregate = _build_weekly_aggregate(
        week=week,
        items=weekly_items,
        rules=rules,
        generated_at=generated_at,
        duplicates_removed_count=duplicates_removed_count,
    )
    aggregate_history = _load_weekly_aggregates(weekly_aggregates_path)
    prior_aggregate = aggregate_history.get(previous_week_key)
    if prior_aggregate is None and prior_items:
        prior_aggregate = _build_weekly_aggregate(
            week=previous_week_key,
            items=prior_items,
            rules=rules,
            generated_at=generated_at,
            duplicates_removed_count=prior_duplicates_removed,
        )
    signals, keyword_delta_table = _generate_wow_signals(current_aggregate, prior_aggregate)
    _upsert_weekly_aggregate(weekly_aggregates_path, current_aggregate)

    collect_meta = _load_last_collect(collect_meta_path)
    status_fallback = collect_meta.get("status", "n/a")
    feeds_success = collect_meta.get("feeds_success")
    if feeds_success is None:
        feeds_success = collect_meta.get("feeds_ok", "n/a")
    feeds_failed = collect_meta.get("feeds_failed", "n/a")
    feeds_total = collect_meta.get("feeds_total", "n/a")
    if feeds_failed == "n/a":
        try:
            feeds_failed = int(feeds_total) - int(feeds_success)
        except (TypeError, ValueError):
            feeds_failed = "n/a"
    rss_collect_status = _derive_collect_status(feeds_success=feeds_success, feeds_failed=feeds_failed, fallback_status=status_fallback)
    fetched_last = collect_meta.get("fetched", "n/a")
    manual_items_count = sum(1 for item in weekly_items if _is_manual_item(item))
    korea_items_count = sum(1 for item in weekly_items if "korea" in _item_tags(item))
    germany_items_count = sum(1 for item in weekly_items if "germany" in _item_tags(item))
    manual_korea_items_count = sum(1 for item in weekly_items if _is_manual_item(item) and "korea" in _item_tags(item))
    manual_germany_items_count = sum(1 for item in weekly_items if _is_manual_item(item) and "germany" in _item_tags(item))
    weekly_total = len(weekly_items)
    korea_share = (korea_items_count / weekly_total * 100) if weekly_total else 0.0
    germany_share = (germany_items_count / weekly_total * 100) if weekly_total else 0.0
    kr_de_share = ((korea_items_count + germany_items_count) / weekly_total * 100) if weekly_total else 0.0

    lines: list[str] = []
    lines.append(f"# Weekly Report {week}")
    lines.append("")
    lines.append(f"- week: {week}")
    lines.append(f"- generated_at: {generated_at}")
    lines.append(f"- number_of_items_considered: {len(weekly_items)}")
    lines.append(f"- rss_collect_status: {rss_collect_status}")
    lines.append(f"- feeds_success_total: {feeds_success}/{feeds_total}")
    lines.append(f"- feeds_failed_total: {feeds_failed}/{feeds_total}")
    lines.append(f"- manual_items_count: {manual_items_count}")
    lines.append(f"- korea_items_count: {korea_items_count}")
    lines.append(f"- germany_items_count: {germany_items_count}")
    lines.append(f"- korea_share: {korea_share:.1f}% ({korea_items_count}/{weekly_total})")
    lines.append(f"- germany_share: {germany_share:.1f}% ({germany_items_count}/{weekly_total})")
    lines.append(f"- kr_de_share: {kr_de_share:.1f}% ({korea_items_count + germany_items_count}/{weekly_total})")
    lines.append(f"- duplicates_removed_count: {duplicates_removed_count}")
    if str(rss_collect_status).lower() == "failed" and str(fetched_last) == "0":
        lines.append("- rss_collect_note: last collect failed and fetched 0 items.")
    lines.append("")

    korea_empty_note = _focus_empty_reason(
        region="korea",
        region_items_count=korea_items_count,
        manual_region_items_count=manual_korea_items_count,
        collect_meta=collect_meta,
    )
    germany_empty_note = _focus_empty_reason(
        region="germany",
        region_items_count=germany_items_count,
        manual_region_items_count=manual_germany_items_count,
        collect_meta=collect_meta,
    )

    _render_focus_section(
        lines,
        "## Korea Focus",
        korea_focus,
        korea_empty_note,
    )
    _render_focus_section(
        lines,
        "## Germany Focus",
        germany_focus,
        germany_empty_note,
    )
    _render_focus_section(
        lines,
        "## Manual Inputs",
        manual_focus,
        "No manual input items available for this week. Add markdown/PDF notes under inputs/{korea,germany,global}.",
    )

    lines.append("## Top 10")
    lines.append("")
    if not top10:
        lines.append("No items available for this week.")
    else:
        for item in top10:
            lines.append(f"### {item.get('title') or 'Untitled'}")
            lines.append(f"- source: {item.get('source', 'unknown')}")
            lines.append(f"- published_at: {item.get('published_at')}")
            lines.append(f"- url: {item.get('url')}")
            attachment_path = str(item.get("attachment_path") or "").strip()
            if attachment_path:
                lines.append(f"- Attachment: {attachment_path}")
            lines.append(f"- summary: {_report_summary(item, max_chars=400)}")
            lines.append("")

    lines.append("## By Category")
    lines.append("")
    required_categories = [
        "macroeconomics",
        "equities",
        "rates",
        "real_estate",
        "germany",
        "korea",
        "personal_finance",
    ]

    for category in required_categories:
        lines.append(f"### {category}")
        cat_items = categories.get(category, [])
        if not cat_items:
            lines.append("- No items")
            continue
        for item in cat_items[:10]:
            lines.append(f"- {item.get('title', 'Untitled')} ({item.get('source', 'unknown')})")
        lines.append("")

    lines.append("## Signals & Watchlist")
    lines.append("")
    for signal in signals:
        lines.append(f"- {signal}")
    lines.append("")
    _render_keyword_delta_table(lines, keyword_delta_table)

    report_path = Path(output_dir) / weekly_filename(week)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    return report_path
