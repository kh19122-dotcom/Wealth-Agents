from __future__ import annotations

import csv
from datetime import date, datetime, timedelta
import math
from pathlib import Path
import time
from typing import Any, Iterable


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


def sync_yahoo_price_cache(
    cache_path: Path,
    ticker: str,
    start: date,
    end: date,
    max_retries: int = 3,
) -> dict[str, Any]:
    existing = read_price_cache(cache_path)
    missing_ranges = compute_missing_ranges(existing, start, end)

    downloaded: dict[date, float] = {}
    for range_start, range_end in missing_ranges:
        fetched = fetch_yahoo_adj_close(
            ticker=ticker,
            start=range_start,
            end=range_end,
            max_retries=max_retries,
        )
        downloaded.update(fetched)

    merged = dict(existing)
    merged.update(downloaded)
    if not merged:
        raise RuntimeError(
            f"No adjusted-close data was retrieved for ticker '{ticker}'. "
            "Verify ticker symbol/provider and requested date range."
        )

    rows_before = len(existing)
    rows_after = len(merged)
    rows_appended = max(0, rows_after - rows_before)
    if (not cache_path.exists()) or downloaded:
        write_price_cache(cache_path, merged)

    return {
        "ticker": ticker,
        "rows_total": rows_after,
        "rows_appended": rows_appended,
        "downloaded_rows": len(downloaded),
        "series": merged,
        "cache_path": str(cache_path),
    }


def fetch_ibkr_adj_close(
    contract_spec: dict[str, Any],
    start: date,
    end: date,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 37,
    timeout_sec: float = 8.0,
    max_retries: int = 2,
) -> dict[date, float]:
    if start > end:
        return {}

    try:
        from ib_insync import IB
    except ImportError as exc:
        raise RuntimeError(
            "ib_insync is required for IBKR price fetch. Install dependencies and retry."
        ) from exc

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        ib = IB()
        try:
            contract = build_ibkr_contract(contract_spec)
            ib.connect(
                host=str(host),
                port=int(port),
                clientId=int(client_id),
                timeout=float(timeout_sec),
                readonly=True,
            )
            qualified = ib.qualifyContracts(contract)
            if not qualified:
                raise RuntimeError("IBKR contract qualification returned no match.")
            resolved = qualified[0]

            duration_days = max(5, (end - start).days + 5)
            end_datetime = (end + timedelta(days=1)).strftime("%Y%m%d 00:00:00 UTC")
            what_to_show = str(contract_spec.get("what_to_show") or "TRADES")
            use_rth = bool(contract_spec.get("use_rth", True))
            bars = ib.reqHistoricalData(
                resolved,
                endDateTime=end_datetime,
                durationStr=f"{duration_days} D",
                barSizeSetting="1 day",
                whatToShow=what_to_show,
                useRTH=use_rth,
                formatDate=1,
            )

            series: dict[date, float] = {}
            for bar in bars or []:
                parsed_date = _coerce_ibkr_bar_date(getattr(bar, "date", None))
                if parsed_date is None or parsed_date < start or parsed_date > end:
                    continue
                close_value = getattr(bar, "close", None)
                if close_value is None:
                    continue
                px = float(close_value)
                if math.isnan(px):
                    continue
                series[parsed_date] = px

            if not series:
                if _range_has_weekday(start, end):
                    raise RuntimeError(
                        f"Received empty IBKR price data between {start.isoformat()} and {end.isoformat()}. "
                        "Verify IBKR contract mapping and market data permissions."
                    )
                return {}
            return series
        except Exception as exc:  # pragma: no cover - depends on IBKR runtime
            last_error = exc
            if attempt < max_retries:
                time.sleep(0.4 * (2 ** (attempt - 1)))
                continue
            break
        finally:
            try:
                ib.disconnect()
            except Exception:
                pass

    if last_error is not None:
        raise RuntimeError(f"Failed to fetch IBKR prices: {last_error}") from last_error
    raise RuntimeError("Failed to fetch IBKR prices.")


def sync_ibkr_price_cache(
    cache_path: Path,
    ticker: str,
    contract_spec: dict[str, Any],
    start: date,
    end: date,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 37,
    timeout_sec: float = 8.0,
    max_retries: int = 2,
) -> dict[str, Any]:
    existing = read_price_cache(cache_path)
    missing_ranges = compute_missing_ranges(existing, start, end)

    downloaded: dict[date, float] = {}
    for range_start, range_end in missing_ranges:
        fetched = fetch_ibkr_adj_close(
            contract_spec=contract_spec,
            start=range_start,
            end=range_end,
            host=host,
            port=port,
            client_id=client_id,
            timeout_sec=timeout_sec,
            max_retries=max_retries,
        )
        downloaded.update(fetched)

    merged = dict(existing)
    merged.update(downloaded)
    if not merged:
        raise RuntimeError(
            f"No IBKR adjusted-close data was retrieved for ticker '{ticker}'. "
            "Verify IBKR contract mapping and requested date range."
        )

    rows_before = len(existing)
    rows_after = len(merged)
    rows_appended = max(0, rows_after - rows_before)
    if (not cache_path.exists()) or downloaded:
        write_price_cache(cache_path, merged)

    return {
        "ticker": ticker,
        "rows_total": rows_after,
        "rows_appended": rows_appended,
        "downloaded_rows": len(downloaded),
        "series": merged,
        "cache_path": str(cache_path),
    }


def get_yahoo_ticker_currency(
    ticker: str,
    max_retries: int = 2,
) -> str | None:
    try:
        import yfinance as yf
    except ImportError:
        return None

    for attempt in range(1, max_retries + 1):
        try:
            instrument = yf.Ticker(ticker)
            currency = _extract_currency(getattr(instrument, "fast_info", None))
            if currency:
                return currency

            info = getattr(instrument, "info", None)
            if isinstance(info, dict):
                currency = _normalize_currency(info.get("currency"))
                if currency:
                    return currency
            return None
        except Exception:  # pragma: no cover - depends on provider/network behavior
            if attempt < max_retries:
                time.sleep(0.3 * (2 ** (attempt - 1)))
                continue
            return None
    return None


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


def build_ibkr_contract(contract_spec: dict[str, Any]) -> Any:
    if not isinstance(contract_spec, dict):
        raise ValueError("IBKR contract spec must be a mapping.")

    try:
        from ib_insync import Contract
    except ImportError as exc:
        raise RuntimeError(
            "ib_insync is required for IBKR price fetch. Install dependencies and retry."
        ) from exc

    raw_conid = contract_spec.get("conid", contract_spec.get("conId"))
    symbol = str(contract_spec.get("symbol") or "").strip()
    sec_type = str(contract_spec.get("sec_type", contract_spec.get("secType", "STK"))).strip() or "STK"
    exchange = str(contract_spec.get("exchange") or "SMART").strip() or "SMART"
    currency = str(contract_spec.get("currency") or "").strip().upper()
    primary_exchange = str(
        contract_spec.get("primary_exchange", contract_spec.get("primaryExchange", ""))
    ).strip()

    kwargs: dict[str, Any] = {
        "secType": sec_type,
        "exchange": exchange,
    }
    if symbol:
        kwargs["symbol"] = symbol
    if currency:
        kwargs["currency"] = currency
    if primary_exchange:
        kwargs["primaryExchange"] = primary_exchange

    if raw_conid is not None and str(raw_conid).strip():
        try:
            kwargs["conId"] = int(raw_conid)
        except (TypeError, ValueError) as exc:
            raise ValueError("IBKR contract conid/conId must be an integer.") from exc
    elif not symbol:
        raise ValueError("IBKR contract must include either conid/conId or symbol.")

    return Contract(**kwargs)


def _extract_currency(source: Any) -> str | None:
    if source is None:
        return None

    if isinstance(source, dict):
        return _normalize_currency(source.get("currency"))

    getter = getattr(source, "get", None)
    if callable(getter):
        try:
            candidate = getter("currency")
        except Exception:  # pragma: no cover - defensive for third-party object behavior
            candidate = None
        currency = _normalize_currency(candidate)
        if currency:
            return currency

    try:
        candidate = source["currency"]
    except Exception:  # pragma: no cover - defensive for third-party object behavior
        candidate = None
    currency = _normalize_currency(candidate)
    if currency:
        return currency

    return _normalize_currency(getattr(source, "currency", None))


def _normalize_currency(value: Any) -> str | None:
    text = str(value or "").strip().upper()
    if len(text) != 3:
        return None
    return text


def _range_has_weekday(start: date, end: date) -> bool:
    cursor = start
    while cursor <= end:
        if cursor.weekday() < 5:
            return True
        cursor += timedelta(days=1)
    return False


def _coerce_ibkr_bar_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value

    text = str(value).strip()
    if not text:
        return None

    if len(text) >= 8 and text[:8].isdigit():
        try:
            return datetime.strptime(text[:8], "%Y%m%d").date()
        except ValueError:
            pass
    if len(text) >= 10:
        head = text[:10]
        try:
            return datetime.strptime(head, "%Y-%m-%d").date()
        except ValueError:
            pass
    return None
