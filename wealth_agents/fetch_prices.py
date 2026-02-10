from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .market_prices import (
    parse_iso_date,
    read_price_cache,
    sync_yahoo_price_cache,
)
from .policy_instruments import load_policy_and_instruments


DEFAULT_POLICY_PATH = "data/policy/policy.yml"
DEFAULT_PRICES_DIR = "data/prices"
LOG = logging.getLogger(__name__)


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
    tickers_succeeded = 0
    tickers_skipped_empty = 0
    tickers_failed = 0
    tickers_with_existing_cache = 0

    for instrument in selected:
        cache_path = provider_dir / f"{instrument.ticker}.csv"
        had_existing_cache = cache_path.exists()
        existing_rows = 0
        if had_existing_cache:
            try:
                existing_rows = len(read_price_cache(cache_path))
                tickers_with_existing_cache += 1
            except ValueError as exc:
                tickers_failed += 1
                LOG.warning(
                    "Price fetch failed: ticker=%s invalid existing cache=%s detail=%s",
                    instrument.ticker,
                    cache_path,
                    exc,
                )
                ticker_summaries.append(
                    {
                        "instrument_id": instrument.instrument_id,
                        "ticker": instrument.ticker,
                        "status": "failed",
                        "rows_total": 0,
                        "rows_appended": 0,
                        "downloaded_rows": 0,
                        "cache_path": str(cache_path),
                        "had_existing_cache": True,
                        "error": str(exc),
                    }
                )
                continue

        try:
            synced = sync_yahoo_price_cache(
                cache_path=cache_path,
                ticker=instrument.ticker,
                start=start_date,
                end=end_date,
                max_retries=3,
            )
            tickers_succeeded += 1
            total_downloaded_rows += int(synced["downloaded_rows"])
            ticker_summaries.append(
                {
                    "instrument_id": instrument.instrument_id,
                    "ticker": instrument.ticker,
                    "status": "succeeded",
                    "rows_total": synced["rows_total"],
                    "rows_appended": synced["rows_appended"],
                    "downloaded_rows": synced["downloaded_rows"],
                    "cache_path": synced["cache_path"],
                    "had_existing_cache": had_existing_cache,
                }
            )
            continue
        except RuntimeError as exc:
            if _is_empty_download_error(exc):
                tickers_skipped_empty += 1
                LOG.warning(
                    "Price fetch skipped (empty data): ticker=%s had_existing_cache=%s detail=%s",
                    instrument.ticker,
                    had_existing_cache,
                    exc,
                )
                ticker_summaries.append(
                    {
                        "instrument_id": instrument.instrument_id,
                        "ticker": instrument.ticker,
                        "status": "skipped_empty",
                        "rows_total": existing_rows,
                        "rows_appended": 0,
                        "downloaded_rows": 0,
                        "cache_path": str(cache_path),
                        "had_existing_cache": had_existing_cache,
                    }
                )
                continue

            tickers_failed += 1
            LOG.warning(
                "Price fetch failed: ticker=%s detail=%s",
                instrument.ticker,
                exc,
            )
            ticker_summaries.append(
                {
                    "instrument_id": instrument.instrument_id,
                    "ticker": instrument.ticker,
                    "status": "failed",
                    "rows_total": existing_rows,
                    "rows_appended": 0,
                    "downloaded_rows": 0,
                    "cache_path": str(cache_path),
                    "had_existing_cache": had_existing_cache,
                    "error": str(exc),
                }
            )
            continue
        except Exception as exc:
            tickers_failed += 1
            LOG.warning(
                "Price fetch failed: ticker=%s detail=%s",
                instrument.ticker,
                exc,
            )
            ticker_summaries.append(
                {
                    "instrument_id": instrument.instrument_id,
                    "ticker": instrument.ticker,
                    "status": "failed",
                    "rows_total": existing_rows,
                    "rows_appended": 0,
                    "downloaded_rows": 0,
                    "cache_path": str(cache_path),
                    "had_existing_cache": had_existing_cache,
                    "error": str(exc),
                }
            )
            continue

    if tickers_succeeded == 0 and tickers_with_existing_cache == 0:
        raise RuntimeError(
            "Price fetch failed: no ticker produced data and no usable existing cache was found. "
            f"tickers_succeeded={tickers_succeeded} "
            f"tickers_skipped_empty={tickers_skipped_empty} "
            f"tickers_failed={tickers_failed} "
            f"total_rows_downloaded={total_downloaded_rows}"
        )

    return {
        "provider": provider_name,
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
        "tickers_count": len(ticker_summaries),
        "tickers_succeeded": tickers_succeeded,
        "tickers_skipped_empty": tickers_skipped_empty,
        "tickers_failed": tickers_failed,
        "total_rows_downloaded": total_downloaded_rows,
        "downloaded_rows": total_downloaded_rows,
        "tickers": ticker_summaries,
    }


def _is_empty_download_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "empty price data" in text
        or "empty adjusted-close data" in text
        or "no adjusted-close data was retrieved" in text
    )
