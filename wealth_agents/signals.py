from __future__ import annotations

from typing import Any

RISK_OFF_CATEGORY_WEIGHTS: dict[str, int] = {
    "macroeconomics": 1,
    "rates": 1,
}
RISK_OFF_KEYWORD_WEIGHTS: dict[str, int] = {
    "inflation": 1,
    "rate hike": 1,
    "recession": 1,
    "war": 1,
    "volatility": 1,
}
RISK_ON_CATEGORY_WEIGHTS: dict[str, int] = {
    "equities": 1,
}
RISK_ON_KEYWORD_WEIGHTS: dict[str, int] = {
    "growth": 1,
    "earnings": 1,
    "rate cut": 1,
    "soft landing": 1,
    "disinflation": 1,
}


def as_int_map(raw: Any) -> dict[str, int]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, int] = {}
    for key, value in raw.items():
        if not isinstance(key, str):
            continue
        try:
            out[key.strip().lower()] = int(value)
        except (TypeError, ValueError):
            continue
    return out


def risk_off_score(aggregate: dict[str, Any] | None) -> int:
    if not isinstance(aggregate, dict):
        return 0
    categories = as_int_map(aggregate.get("category_counts"))
    keywords = as_int_map(aggregate.get("keyword_counts"))
    score = 0
    for term, weight in RISK_OFF_CATEGORY_WEIGHTS.items():
        score += int(categories.get(term, 0)) * int(weight)
    for term, weight in RISK_OFF_KEYWORD_WEIGHTS.items():
        score += int(keywords.get(term, 0)) * int(weight)
    return int(score)


def risk_on_score(aggregate: dict[str, Any] | None) -> int:
    if not isinstance(aggregate, dict):
        return 0
    categories = as_int_map(aggregate.get("category_counts"))
    keywords = as_int_map(aggregate.get("keyword_counts"))
    score = 0
    for term, weight in RISK_ON_CATEGORY_WEIGHTS.items():
        score += int(categories.get(term, 0)) * int(weight)
    for term, weight in RISK_ON_KEYWORD_WEIGHTS.items():
        score += int(keywords.get(term, 0)) * int(weight)
    return int(score)


def phase25_risk_off_score(aggregate: dict[str, Any] | None) -> int:
    # Keep Phase 2.5 behavior backward-compatible while sharing the same parser.
    if not isinstance(aggregate, dict):
        return 0
    keywords = as_int_map(aggregate.get("keyword_counts"))
    return int(keywords.get("inflation", 0)) + int(keywords.get("rate hike", 0))


def top_regime_drivers(aggregate: dict[str, Any] | None, limit: int = 5) -> list[dict[str, Any]]:
    if not isinstance(aggregate, dict):
        return []
    categories = as_int_map(aggregate.get("category_counts"))
    keywords = as_int_map(aggregate.get("keyword_counts"))
    rows: list[dict[str, Any]] = []

    for term, weight in RISK_OFF_CATEGORY_WEIGHTS.items():
        count = int(categories.get(term, 0))
        if count <= 0:
            continue
        rows.append(
            {
                "kind": "category",
                "term": term,
                "direction": "risk_off",
                "count": count,
                "contribution": count * int(weight),
            }
        )
    for term, weight in RISK_OFF_KEYWORD_WEIGHTS.items():
        count = int(keywords.get(term, 0))
        if count <= 0:
            continue
        rows.append(
            {
                "kind": "keyword",
                "term": term,
                "direction": "risk_off",
                "count": count,
                "contribution": count * int(weight),
            }
        )
    for term, weight in RISK_ON_CATEGORY_WEIGHTS.items():
        count = int(categories.get(term, 0))
        if count <= 0:
            continue
        rows.append(
            {
                "kind": "category",
                "term": term,
                "direction": "risk_on",
                "count": count,
                "contribution": count * int(weight),
            }
        )
    for term, weight in RISK_ON_KEYWORD_WEIGHTS.items():
        count = int(keywords.get(term, 0))
        if count <= 0:
            continue
        rows.append(
            {
                "kind": "keyword",
                "term": term,
                "direction": "risk_on",
                "count": count,
                "contribution": count * int(weight),
            }
        )

    rows.sort(
        key=lambda row: (
            -int(row["contribution"]),
            str(row["direction"]),
            str(row["kind"]),
            str(row["term"]),
        )
    )
    return rows[: max(0, int(limit))]
