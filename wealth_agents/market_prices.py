from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
import math
from pathlib import Path
import time
from typing import Iterable


def parse_iso_date(value: str, field_name: str) -> date:
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be in YYYY-MM-DD format.") from exc


def parse_iso_month(value: str, field_name: str) -> date:
    text = str(value).strip()
    try:
        return datetime.strptime(text, "%Y-%m").date().replace(day=1)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be in YYYY-MM format.") from exc


def month_strings_inclusive(start_month: str, end_month: str) -> list[str]:
    start = parse_iso_month(start_month, "start")
    end = parse_iso_month(end_month, "end")
    if start > end:
        raise ValueError("start must be less than or equal to end.")

    out: list[str] = []
    cursor = start
    while cursor <= end:
        out.append(cursor.strftime("%Y-%m"))
        if cursor.month == 12:
            cursor = cursor.replace(year=cursor.year + 1, month=1)
        else:
            cursor = cursor.replace(month=cursor.month + 1)
    return out


def read_price_cache(path: Path) -> dict[date, float]:
    if not path.exists():
        return {}

    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != ["date", "adj_close"]:
            raise ValueError(f"Invalid price cache format in {path}: expected columns ['date', 'adj_close'].")

        prices: dict[date, float] = {}
        for idx, row in enumerate(reader, start=2):
            raw_date = str(row.get("date") or "").strip()
            raw_price = str(row.get("adj_close") or "").strip()
            if not raw_date or not raw_price:
                continue
            try:
                parsed_date = datetime.strptime(raw_date, "%Y-%m-%d").date()
                parsed_price = float(raw_price)
            except ValueError as exc:
                raise ValueError(f"Invalid price row in {path}:{idx}.") from exc
            prices[parsed_date] = parsed_price
    return prices


def write_price_cache(path: Path, prices: dict[date, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "adj_close"])
        for dt in sorted(prices):
            writer.writerow([dt.isoformat(), _format_price(prices[dt])])


def merge_price_series(*parts: Iterable[tuple[date, float]]) -> dict[date, float]:
    merged: dict[date, float] = {}
    for series in parts:
        for dt, px in series:
            merged[dt] = float(px)
    return merged


def compute_missing_ranges(
    existing_prices: dict[date, float],
    start: date,
    end: date,
) -> list[tuple[date, date]]:
    if start > end:
        raise ValueError("start must be less than or equal to end.")

    if not existing_prices:
        return [(start, end)]

    existing_dates = sorted(existing_prices)
    first_existing = existing_dates[0]
    last_existing = existing_dates[-1]

    missing: list[tuple[date, date]] = []
    if start < first_existing:
        missing.append((start, first_existing - timedelta(days=1)))
    if end > last_existing:
        missing.append((last_existing + timedelta(days=1), end))
    return [row for row in missing if row[0] <= row[1]]


def fetch_yahoo_adj_close(
    ticker: str,
    start: date,
    end: date,
    max_retries: int = 3,
) -> dict[date, float]:
    if start > end:
        return {}

    try:
        import yfinance as yf
    except ImportError as exc:
        raise RuntimeError("yfinance is required for fetch-prices. Install project dependencies and retry.") from exc

    start_iso = start.isoformat()
    end_exclusive_iso = (end + timedelta(days=1)).isoformat()
    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            history = yf.Ticker(ticker).history(
                start=start_iso,
                end=end_exclusive_iso,
                auto_adjust=False,
                actions=False,
            )
        except Exception as exc:  # pragma: no cover - depends on network/provider behavior
            last_error = exc
            if attempt < max_retries:
                time.sleep(0.4 * (2 ** (attempt - 1)))
                continue
            raise RuntimeError(f"Failed to fetch prices for ticker '{ticker}': {exc}") from exc

        if history is None or history.empty:
            if _range_has_weekday(start, end):
                raise RuntimeError(
                    f"Received empty price data for ticker '{ticker}' between {start_iso} and {end.isoformat()}. "
                    "Verify ticker symbol/provider and try again."
                )
            return {}

        if "Adj Close" not in history.columns:
            raise RuntimeError(f"Ticker '{ticker}' response did not include 'Adj Close'.")

        series: dict[date, float] = {}
        adj_close = history["Adj Close"].dropna()
        for idx, value in adj_close.items():
            dt = idx
            if hasattr(dt, "tz_localize"):
                dt = dt.tz_localize(None)
            parsed_date = dt.date() if hasattr(dt, "date") else datetime.fromisoformat(str(dt)).date()
            px = float(value)
            if math.isnan(px):
                continue
            series[parsed_date] = px

        if not series:
            if _range_has_weekday(start, end):
                raise RuntimeError(
                    f"Received empty adjusted-close data for ticker '{ticker}' between {start_iso} and {end.isoformat()}. "
                    "Verify ticker symbol/provider and try again."
                )
            return {}
        return series

    if last_error is not None:  # pragma: no cover - defensive, loop returns or raises above
        raise RuntimeError(f"Failed to fetch prices for ticker '{ticker}': {last_error}") from last_error
    raise RuntimeError(f"Failed to fetch prices for ticker '{ticker}'.")


def month_end_prices(
    daily_prices: dict[date, float],
    start_month: str,
    end_month: str,
) -> dict[str, float]:
    months = month_strings_inclusive(start_month, end_month)
    by_month: dict[str, tuple[date, float]] = {}
    for dt in sorted(daily_prices):
        month = dt.strftime("%Y-%m")
        by_month[month] = (dt, daily_prices[dt])

    out: dict[str, float] = {}
    for month in months:
        row = by_month.get(month)
        if row is None:
            raise ValueError(f"Missing month-end price data for month {month}.")
        out[month] = float(row[1])
    return out


def _format_price(value: float) -> str:
    return f"{float(value):.10f}"


def _range_has_weekday(start: date, end: date) -> bool:
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            return True
        cursor += timedelta(days=1)
    return False
