from collections import Counter
import re
from typing import Any

from .config_loader import load_config


def _text_blob(item: dict) -> str:
    return f"{item.get('title', '')} {item.get('summary', '')}".lower()


def load_rules(path: str) -> dict[str, Any]:
    return load_config(path)


def _keyword_match(text: str, keyword: str) -> bool:
    lowered = str(keyword or "").strip().lower()
    if not lowered:
        return False
    pattern = re.compile(rf"(?<!\w){re.escape(lowered)}(?!\w)")
    return pattern.search(text) is not None


def item_categories(item: dict, rules: dict) -> list[str]:
    text = _text_blob(item)
    categories: list[str] = []
    for category, keywords in (rules.get("categories") or {}).items():
        keys = [str(k).lower() for k in keywords or [] if str(k).strip()]
        if any(_keyword_match(text, key) for key in keys):
            categories.append(category)

    # Personal finance should take precedence over real-estate matches.
    if "personal_finance" in categories and "real_estate" in categories:
        categories = [category for category in categories if category != "real_estate"]
    return categories


def score_item(item: dict, rules: dict, week_end_ts: float) -> float:
    score = 0.0

    published_at = item.get("_item_date") or item.get("published_at") or item.get("fetched_at")
    ts = 0.0
    if published_at:
        from datetime import datetime

        try:
            ts = datetime.fromisoformat(str(published_at).replace("Z", "+00:00")).timestamp()
        except ValueError:
            ts = 0.0

    recency_component = max(0.0, 14 * 24 * 3600 - (week_end_ts - ts)) / (24 * 3600)
    score += recency_component

    text = _text_blob(item)
    terms = [t.lower() for t in (rules.get("top_terms") or [])]
    keyword_hits = sum(1 for term in terms if term in text)
    score += keyword_hits * 2.0

    if item.get("summary"):
        score += 0.5

    return score


def summarize_item(item: dict) -> str:
    title = (item.get("title") or "Untitled").strip()
    summary = (item.get("summary") or "").strip()

    if not summary:
        return f"This item covers: {title}."

    sentences = [s.strip() for s in summary.replace("\n", " ").split(".") if s.strip()]
    if not sentences:
        return f"This item covers: {title}."

    chosen = sentences[:2]
    rendered = ". ".join(chosen)
    if not rendered.endswith("."):
        rendered += "."
    return rendered


def generate_signals(items: list[dict], rules: dict) -> list[str]:
    text_blobs = [_text_blob(item) for item in items]

    signal_hits: Counter[str] = Counter()
    for signal_name, keywords in (rules.get("signals") or {}).items():
        kws = [k.lower() for k in keywords or []]
        for text in text_blobs:
            if any(kw in text for kw in kws):
                signal_hits[signal_name] += 1

    categories = Counter()
    for item in items:
        for category in item_categories(item, rules):
            categories[category] += 1

    rule_signals: list[str] = []

    for name, count in signal_hits.most_common():
        if count <= 0:
            continue
        label = name.replace("_", " ")
        rule_signals.append(f"{label} mentions appeared in {count} items.")
        if len(rule_signals) >= 3:
            break

    # Fallback to category-level themes if direct signal hits are sparse.
    if len(rule_signals) < 3:
        for cat, count in categories.most_common():
            if count <= 0:
                continue
            rule_signals.append(f"{cat.replace('_', ' ')} themes appeared in {count} items this week.")
            if len(rule_signals) >= 3:
                break

    generic_notes: list[str] = []
    if not rule_signals:
        generic_notes.append("No strong keyword-based signals were detected this week.")
    if len(items) < 10:
        generic_notes.append("Signal confidence is limited due to low item volume in this period.")

    return (rule_signals[:3] + generic_notes[:2])[:5]
