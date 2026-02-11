# wealth_agents

Phase 1 + Phase 2 implementation for RSS collection, weekly reporting, and an IPS (Investment Policy Statement) builder.

## Features

- RSS feed collection from `config/feeds.yml` (no scraping)
- Regional RSS buckets (`global`, `germany_de`, `korea_ko`) with backward-compatible feed parsing
- Deduplicated JSONL storage in `data/raw/news.jsonl`
- Strict JSONL integrity gate (`validate`) with automatic post-collect/post-ingest checks
- Manual input ingestion from local files (`.md`, `.txt`, `.csv`) into the same raw schema
- Weekly markdown report generation in `reports/weekly_<YYYY-WW>.md`
- Report quality controls:
  - RSS summary sanitization (HTML tag stripping + entity decoding)
  - manual/PDF summary cleanup with heading-aware extraction
  - persisted weekly aggregates (`data/meta/weekly_aggregates.jsonl`)
  - week-over-week signal deltas for categories and keyword spikes
  - keyword delta table (`keyword | this_week | last_week | delta`)
  - metadata header includes feed success/failure and dedup counts
- Top10 constraints to reduce bias:
  - topic-signature clustering cap (max 3 per cluster when alternatives exist)
  - regional quota (at least 1 Korea and 1 Germany item in Top10 when available; configurable)
- Dedup rules:
  - URL canonicalization removes tracking parameters before hashing
  - near-duplicate title collapse within a week (Jaccard/SequenceMatcher thresholds)
  - preference keeps richer summaries and avoids flooding from duplicate cross-source stories
- Rule-based categorization and signals from `config/rules.yml`
- IPS builder workflow:
  - `ips init` creates input template/checklist
  - `ips draft` generates conservative/balanced/aggressive policy candidates
  - `ips finalize` writes approved policy and audit history
- Phase 2.5 policy review workflow:
  - `policy-review` reads weekly aggregates + current policy and emits a proposal-only quarterly review
  - outputs markdown review in `reports/policy_review_<YYYY-Www>.md`
  - outputs patch artifact in `data/policy/policy_patch_<YYYY-Www>.yml`
  - optional `--apply` mutates policy only when quarterly apply guardrail allows (requires confirmation / `--yes`)
- Phase 3 order proposal workflow:
  - `propose-orders` reads `data/policy/policy.yml` and emits monthly BUY-only order proposals
  - outputs JSON proposal in `orders/proposed_<YYYY-MM>.json`
  - outputs human-readable report in `reports/orders_<YYYY-MM>.md`
- CLI commands:
  - `python -m wealth_agents collect --config config/feeds.yml`
  - `python -m wealth_agents ingest --path inputs --source manual`
  - `python -m wealth_agents validate --data data/raw/news.jsonl`
  - `python -m wealth_agents report --week 2026-W06`
  - `python -m wealth_agents ips init`
  - `python -m wealth_agents ips draft --input data/policy/ips_inputs.yml`
  - `python -m wealth_agents ips finalize --choice balanced`
  - `python -m wealth_agents policy-review --week 2026-W06`
  - `python -m wealth_agents policy-review --week 2026-W06 --apply --yes`
  - `python -m wealth_agents propose-orders --month 2026-03`

## Setup (uv)

```bash
uv sync --extra dev
```

### Portfolio (first-time setup)

The live portfolio state file is local-only (gitignored). Create it from the example:

```bash
cp data/portfolio/live.example.json data/portfolio/live.json

```md
Then import trades and generate a drift report:

```bash
uv run python -m wealth_agents portfolio import-trades --csv <path/to/trades.csv>
uv run python -m wealth_agents portfolio report --asof YYYY-MM-DD


## Usage

Collect latest feed items:

```bash
uv run python -m wealth_agents collect --config config/feeds.yml
```

Collection writes status metadata to `data/meta/last_collect.json` with per-feed run status (`ok`, `http_error`, `parse_error`, `timeout`, `dns_error`), `feeds_success`, `feeds_failed`, and sampled errors, which the weekly report header reads.
Per-feed health and quarantine state is persisted in `data/meta/feed_health.json`.

Ingest manual local inputs:

```bash
uv run python -m wealth_agents ingest --path inputs --source manual
```

Validate JSONL integrity manually:

```bash
uv run python -m wealth_agents validate --data data/raw/news.jsonl
```

`collect` and `ingest` automatically run the same validation step at the end of each run and return non-zero on invalid JSONL.

Inspect feed health and quarantine status:

```bash
uv run python -m wealth_agents feeds health
```

Generate a weekly report:

```bash
uv run python -m wealth_agents report --week 2026-W06
```

Phase 2 design details and schema notes: `docs/Phase2.md`.

Run a quick Phase 2 smoke test:

```bash
make phase2-smoke
```

## Manual Input Conventions

- Folders scanned by `ingest`:
  - `inputs/korea/`
  - `inputs/germany/`
  - `inputs/global/`
- Supported formats:
  - `.md` / `.txt`: one or multiple entries
  - `.csv`: columns `title`, `url`, `published_at`, and `summary` or `body`
  - `.pdf`: text-based PDFs (extracted with `pypdf`)
- Multiple markdown/text entries can be split using:
  - headings starting with `### `
  - separator lines `---`
- PDF behavior:
  - title from first non-empty extracted line (fallback: filename)
  - `attachment_path` stored in output record
  - extracted body text truncated to 20,000 chars for readable PDFs
  - summary prefers heading sections (for example: Executive Summary, Key Takeaways) and otherwise uses keyword-dense slide sentences
  - low-quality/garbled extraction stores a safe placeholder summary (`PDF text extraction is low-quality; ...`), adds `needs_summary` tag, and does not store raw extracted PDF text in the JSONL record
  - low-quality PDFs auto-generate a sibling scaffold markdown (`<same-basename>.md`) if missing
  - if PDF text extraction is empty, ingest logs an OCR/manual-summary warning and still creates the low-quality placeholder record

Example markdown input:

```md
### Korea exports rebound
url: https://example.com/korea-exports
published_at: 2026-02-07T08:30:00Z
Korea export momentum improved as semiconductor shipments recovered.

---

### Germany factory orders soften
Germany industrial orders declined month-over-month amid weak external demand.
```

Initialize IPS inputs and checklist:

```bash
uv run python -m wealth_agents ips init
```

Generate IPS draft candidates:

```bash
uv run python -m wealth_agents ips draft --input data/policy/ips_inputs.yml
```

Finalize one candidate:

```bash
uv run python -m wealth_agents ips finalize --choice balanced
```

Run proposal-only policy review (quarterly cadence guardrail):

```bash
uv run python -m wealth_agents policy-review --week 2026-W06
uv run python -m wealth_agents policy-review --week 2026-W06 --apply --yes
```

Propose monthly BUY orders from finalized policy:

```bash
uv run python -m wealth_agents propose-orders --month 2026-03
uv run python -m wealth_agents propose-orders --month 2026-03 --amount 2500
```

Phase 3 design details and output schema: `docs/Phase3.md`.
Phase 2.5 policy review design details: `docs/Phase25.md`.

## Project Structure

```
wealth_agents/
  __init__.py
  __main__.py
  cli.py
  ingest.py
  ips.py
  orders.py
  policy_review.py
  policy.py
  rss.py
  storage.py
  report.py
  rules.py
  utils.py
config/
  feeds.yml
  rules.yml
data/
  policy/
  raw/
orders/
reports/
tests/
```
