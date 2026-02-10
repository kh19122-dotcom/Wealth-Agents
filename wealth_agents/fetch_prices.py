from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

from .market_prices import (
    compute_missing_ranges,
    fetch_yahoo_adj_close,
    parse_iso_date,
    read_price_cache,
    write_price_cache,
)
from .policy_instruments import load_policy_and_instruments


DEFAULT_POLICY_PATH = "data/policy/policy.yml"
DEFAULT_PRICES_DIR = "data/prices"


def fetch_prices_for_policy(
    start: str,
    end: str,
    provider: str = "yahoo",
    policy_path: str = DEFAULT_POLICY_PATH,
    prices_dir: str = DEFAULT_PRICES_DIR,
) -> dict[str, Any]:
    start_date = parse_iso_date(start, "start")
    end_date = parse_iso_date(end, "end")
    if start_date > end_date:
        raise ValueError("start must be less than or equal to end.")

    provider_name = str(provider).strip().lower()
    if provider_name != "yahoo":
        raise ValueError("Phase 3.7 currently supports only provider='yahoo'.")

    _, _, instruments = load_policy_and_instruments(policy_path)
    selected = sorted(
        [row for row in instruments if row.provider == provider_name],
        key=lambda row: row.ticker,
    )
    if not selected:
        raise ValueError(f"No instruments configured with data.provider='{provider_name}'.")

    provider_dir = Path(prices_dir) / provider_name
    ticker_summaries: list[dict[str, Any]] = []
    total_downloaded_rows = 0

    for instrument in selected:
        cache_path = provider_dir / f"{instrument.ticker}.csv"
        existing = read_price_cache(cache_path)
        missing_ranges = compute_missing_ranges(existing, start_date, end_date)

        downloaded: dict[date, float] = {}
        for range_start, range_end in missing_ranges:
            fetched = fetch_yahoo_adj_close(
                ticker=instrument.ticker,
                start=range_start,
                end=range_end,
                max_retries=3,
            )
            downloaded.update(fetched)

        merged = dict(existing)
        merged.update(downloaded)

        if not merged:
            raise RuntimeError(
                f"No adjusted-close data was retrieved for ticker '{instrument.ticker}'. "
                "Verify ticker symbol/provider and requested date range."
            )

        rows_before = len(existing)
        rows_after = len(merged)
        rows_appended = max(0, rows_after - rows_before)
        total_downloaded_rows += len(downloaded)

        if (not cache_path.exists()) or downloaded:
            write_price_cache(cache_path, merged)

        ticker_summaries.append(
            {
                "instrument_id": instrument.instrument_id,
                "ticker": instrument.ticker,
                "rows_total": rows_after,
                "rows_appended": rows_appended,
                "downloaded_rows": len(downloaded),
                "cache_path": str(cache_path),
            }
        )

    return {
        "provider": provider_name,
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "tickers_count": len(ticker_summaries),
        "downloaded_rows": total_downloaded_rows,
        "tickers": ticker_summaries,
    }
