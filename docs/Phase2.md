# Phase 2 Design Notes

## Goals

Phase 2 focuses on deterministic signal quality and decision usefulness:

1. Signals v2 with week-over-week (WoW) change tracking.
2. Better Top10 diversification with similarity clustering and regional quota constraints.
3. Improved manual/PDF summarization without LLM usage.
4. Reliability and coverage metadata in the weekly report.

## Data Model Changes

### Weekly Aggregate Store

- Path: `data/meta/weekly_aggregates.jsonl`
- One JSON object per week (`week` is the upsert key).
- Written during `report` generation.

Record schema:

```json
{
  "week": "2026-W06",
  "generated_at": "2026-02-07T12:00:00Z",
  "item_count": 42,
  "manual_items_count": 8,
  "duplicates_removed_count": 5,
  "category_counts": {
    "macroeconomics": 14,
    "rates": 9
  },
  "keyword_counts": {
    "inflation": 10,
    "rate hike": 6
  },
  "regional_keyword_counts": {
    "korea": {
      "inflation": 4
    },
    "germany": {
      "inflation": 2
    }
  }
}
```

WoW signals are computed against the previous week in this store when present; if missing, report falls back to deriving prior-week aggregate from raw items.

## Report Changes

- `Signals & Watchlist` now includes:
  - concise WoW bullets (category deltas, keyword spikes, Korea/Germany spikes),
  - markdown table: `keyword | this_week | last_week | delta`.
- Header now includes:
  - `korea_share`, `germany_share`, `kr_de_share`,
  - existing feed success/fail, manual count, and duplicates removed.
- Korea/Germany focus sections now show explicit empty reasons:
  - no items,
  - feed failures,
  - no manual input.

## Top10 Diversification

- Clustering uses token + bigram similarity over title/summary text.
- Per-cluster cap enforced (default `3`).
- Regional quotas are explicit and configurable (defaults: Korea `1`, Germany `1`, when available).

Optional `config/rules.yml` keys:

```yaml
top10:
  cluster_cap: 3
  quota_korea: 1
  quota_germany: 1
  cluster_similarity_threshold: 0.45

dedup:
  token_jaccard_threshold: 0.8
  ngram_jaccard_threshold: 0.45
  sequence_threshold: 0.9
  similarity_threshold: 0.72
```

## Manual/PDF Summary Heuristic Updates

- Heading priority expanded (`Executive Summary`, `Key Takeaways`, `Conclusion`, etc.).
- More aggressive cleanup:
  - headers/footers,
  - page markers,
  - repeated short lines,
  - punctuation-only boilerplate lines.
- Summary remains deterministic: scored sentence selection with a 3-5 sentence output window.

## Usage

Run the normal flow:

```bash
uv run python -m wealth_agents ingest --path inputs --source manual
uv run python -m wealth_agents collect --config config/feeds.yml
uv run python -m wealth_agents report --week 2026-W06
```

Run Phase 2 smoke checks:

```bash
make phase2-smoke
```
