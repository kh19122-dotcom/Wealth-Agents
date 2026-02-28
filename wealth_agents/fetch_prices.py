from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from .market_prices import (
    fetch_yahoo_adj_close,
    parse_iso_date,
    read_price_cache,
    sync_ibkr_price_cache,
    sync_yahoo_price_cache,
)
from .policy import read_yaml
from .policy_instruments import load_policy_and_instruments


DEFAULT_POLICY_PATH = "data/policy/policy.yml"
DEFAULT_PRICES_DIR = "data/prices"
SUPPORTED_POLICY_PROVIDER = "yahoo"
SUPPORTED_SOURCES = {"yahoo", "ibkr", "auto"}
PRICE_QUALITY_GATE_PROFILES: dict[str, dict[str, float | int]] = {
    "off": {},
    "standard": {
        "max_abs_pct_diff_pct": 5.0,
        "min_overlap_days": 3,
    },
    "strict": {
        "max_abs_pct_diff_pct": 2.0,
        "min_overlap_days": 5,
    },
}
LOG = logging.getLogger(__name__)


def fetch_prices_for_policy(
    start: str,
    end: str,
    provider: str = SUPPORTED_POLICY_PROVIDER,
    policy_path: str = DEFAULT_POLICY_PATH,
    prices_dir: str = DEFAULT_PRICES_DIR,
    prefer_source: str = "yahoo",
    ibkr_contracts_path: str | None = None,
    ibkr_host: str = "127.0.0.1",
    ibkr_port: int = 7497,
    ibkr_client_id: int = 37,
    ibkr_timeout_sec: float = 8.0,
    ibkr_max_retries: int = 2,
    allow_source_fallback: bool = True,
    price_quality_gate_profile: str = "off",
) -> dict[str, Any]:
    start_date = parse_iso_date(start, "start")
    end_date = parse_iso_date(end, "end")
    if start_date > end_date:
        raise ValueError("start must be less than or equal to end.")

    provider_name = str(provider).strip().lower()
    if provider_name != SUPPORTED_POLICY_PROVIDER:
        raise ValueError("Phase 3.7 currently supports only provider='yahoo'.")

    preferred_source = str(prefer_source or "").strip().lower()
    if preferred_source not in SUPPORTED_SOURCES:
        raise ValueError("prefer_source must be one of: yahoo, ibkr, auto.")
    source_order = _resolve_source_order(preferred_source, allow_source_fallback=allow_source_fallback)
    ibkr_contracts = load_ibkr_contract_specs(ibkr_contracts_path) if "ibkr" in source_order else {}
    gate_profile = _normalize_price_quality_gate_profile(price_quality_gate_profile)

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
    price_quality_gate_evaluated = 0
    price_quality_gate_passed = 0
    price_quality_gate_failed = 0

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

        source_attempts: list[str] = []
        source_errors: dict[str, str] = {}
        nonempty_error_seen = False
        saw_empty_error = False
        synced: dict[str, Any] | None = None
        source_used: str | None = None
        price_quality_gate: dict[str, Any] | None = None

        for source_name in source_order:
            source_attempts.append(source_name)
            try:
                synced = _sync_price_cache_from_source(
                    source_name=source_name,
                    ticker=instrument.ticker,
                    cache_path=cache_path,
                    start=start_date,
                    end=end_date,
                    ibkr_contracts=ibkr_contracts,
                    ibkr_host=ibkr_host,
                    ibkr_port=ibkr_port,
                    ibkr_client_id=ibkr_client_id,
                    ibkr_timeout_sec=ibkr_timeout_sec,
                    ibkr_max_retries=ibkr_max_retries,
                )
                source_used = source_name
                if source_name == "ibkr":
                    price_quality_gate = _evaluate_ibkr_price_quality_gate(
                        profile=gate_profile,
                        ticker=instrument.ticker,
                        start=start_date,
                        end=end_date,
                        ibkr_series=synced.get("series"),
                    )
                    if bool(price_quality_gate.get("available")):
                        price_quality_gate_evaluated += 1
                        if bool(price_quality_gate.get("passed")):
                            price_quality_gate_passed += 1
                        else:
                            price_quality_gate_failed += 1
                    if gate_profile == "strict" and not bool(price_quality_gate.get("passed")):
                        detail = str(price_quality_gate.get("reason") or "unknown")
                        raise RuntimeError(
                            f"IBKR price quality gate failed for ticker '{instrument.ticker}': {detail}"
                        )
                else:
                    price_quality_gate = {
                        "profile": gate_profile,
                        "available": False,
                        "passed": None,
                        "reason": "quality gate not evaluated for non-IBKR source",
                        "metrics": {},
                    }
                source_used = source_name
                break
            except RuntimeError as exc:
                source_errors[source_name] = str(exc)
                if _is_empty_download_error(exc):
                    saw_empty_error = True
                else:
                    nonempty_error_seen = True
                LOG.warning(
                    "Price fetch source failed: ticker=%s source=%s detail=%s",
                    instrument.ticker,
                    source_name,
                    exc,
                )
            except Exception as exc:
                source_errors[source_name] = str(exc)
                nonempty_error_seen = True
                LOG.warning(
                    "Price fetch source failed: ticker=%s source=%s detail=%s",
                    instrument.ticker,
                    source_name,
                    exc,
                )

        if synced is not None:
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
                    "source_used": source_used,
                    "sources_attempted": source_attempts,
                    "fallback_used": len(source_attempts) > 1 and source_used != source_attempts[0],
                    "price_quality_gate": price_quality_gate,
                }
            )
            continue

        if saw_empty_error and not nonempty_error_seen:
            tickers_skipped_empty += 1
            LOG.warning(
                "Price fetch skipped (empty data): ticker=%s had_existing_cache=%s attempts=%s",
                instrument.ticker,
                had_existing_cache,
                ",".join(source_attempts),
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
                    "source_used": None,
                    "sources_attempted": source_attempts,
                    "fallback_used": False,
                    "price_quality_gate": price_quality_gate,
                }
            )
            continue

        tickers_failed += 1
        error_message = _compose_source_error_message(source_errors)
        LOG.warning(
            "Price fetch failed: ticker=%s attempts=%s detail=%s",
            instrument.ticker,
            ",".join(source_attempts),
            error_message,
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
                "source_used": None,
                "sources_attempted": source_attempts,
                "fallback_used": False,
                "error": error_message,
                "price_quality_gate": price_quality_gate,
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
        "preferred_source": preferred_source,
        "source_order": source_order,
        "price_quality_gate_profile": gate_profile,
        "price_quality_gate_evaluated": price_quality_gate_evaluated,
        "price_quality_gate_passed": price_quality_gate_passed,
        "price_quality_gate_failed": price_quality_gate_failed,
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


def _resolve_source_order(preferred_source: str, allow_source_fallback: bool) -> list[str]:
    source = str(preferred_source or "").strip().lower()
    if source == "yahoo":
        return ["yahoo"]
    if source in {"ibkr", "auto"}:
        if allow_source_fallback:
            return ["ibkr", "yahoo"]
        return ["ibkr"]
    raise ValueError("prefer_source must be one of: yahoo, ibkr, auto.")


def _normalize_price_quality_gate_profile(profile: str) -> str:
    normalized = str(profile or "").strip().lower() or "off"
    if normalized not in PRICE_QUALITY_GATE_PROFILES:
        raise ValueError(
            "price_quality_gate_profile must be one of: off, standard, strict."
        )
    return normalized


def _sync_price_cache_from_source(
    source_name: str,
    ticker: str,
    cache_path: Path,
    start,
    end,
    ibkr_contracts: dict[str, dict[str, Any]],
    ibkr_host: str,
    ibkr_port: int,
    ibkr_client_id: int,
    ibkr_timeout_sec: float,
    ibkr_max_retries: int,
) -> dict[str, Any]:
    if source_name == "yahoo":
        return sync_yahoo_price_cache(
            cache_path=cache_path,
            ticker=ticker,
            start=start,
            end=end,
            max_retries=3,
        )

    if source_name == "ibkr":
        contract_spec = ibkr_contracts.get(ticker)
        if not contract_spec:
            raise RuntimeError(
                f"Missing IBKR contract mapping for ticker '{ticker}'. "
                "Add --ibkr-contracts mapping or use --prefer-source yahoo."
            )
        return sync_ibkr_price_cache(
            cache_path=cache_path,
            ticker=ticker,
            contract_spec=contract_spec,
            start=start,
            end=end,
            host=ibkr_host,
            port=ibkr_port,
            client_id=ibkr_client_id,
            timeout_sec=ibkr_timeout_sec,
            max_retries=ibkr_max_retries,
        )

    raise ValueError(f"Unsupported source_name '{source_name}'.")


def load_ibkr_contract_specs(path: str | None) -> dict[str, dict[str, Any]]:
    if not path:
        return {}

    payload = read_yaml(path)
    raw_contracts: Any = payload
    if isinstance(payload.get("contracts"), dict):
        raw_contracts = payload.get("contracts")
    elif isinstance(payload.get("ibkr"), dict) and isinstance(payload["ibkr"].get("contracts"), dict):
        raw_contracts = payload["ibkr"]["contracts"]

    if not isinstance(raw_contracts, dict):
        raise ValueError(
            f"Invalid IBKR contracts file '{path}': expected mapping or root.contracts mapping."
        )

    contracts: dict[str, dict[str, Any]] = {}
    for raw_ticker, raw_spec in raw_contracts.items():
        ticker = str(raw_ticker or "").strip()
        if not ticker:
            continue
        if not isinstance(raw_spec, dict):
            raise ValueError(f"Invalid IBKR contract spec for ticker '{ticker}': expected mapping.")
        contracts[ticker] = dict(raw_spec)
    return contracts


def _compose_source_error_message(source_errors: dict[str, str]) -> str:
    if not source_errors:
        return "unknown source error"
    parts: list[str] = []
    for source_name in sorted(source_errors):
        parts.append(f"{source_name}: {source_errors[source_name]}")
    return "; ".join(parts)


def _is_empty_download_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return (
        "empty price data" in text
        or "empty adjusted-close data" in text
        or "no adjusted-close data was retrieved" in text
        or "empty ibkr price data" in text
        or "no ibkr adjusted-close data was retrieved" in text
    )


def _evaluate_ibkr_price_quality_gate(
    *,
    profile: str,
    ticker: str,
    start,
    end,
    ibkr_series: Any,
) -> dict[str, Any]:
    normalized_profile = _normalize_price_quality_gate_profile(profile)
    if normalized_profile == "off":
        return {
            "profile": normalized_profile,
            "available": False,
            "passed": None,
            "reason": "quality gate disabled",
            "metrics": {},
        }

    if not isinstance(ibkr_series, dict):
        return {
            "profile": normalized_profile,
            "available": False,
            "passed": False,
            "reason": "ibkr series is unavailable for quality comparison",
            "metrics": {},
        }

    try:
        yahoo_series = fetch_yahoo_adj_close(
            ticker=ticker,
            start=start,
            end=end,
            max_retries=2,
        )
    except Exception as exc:
        return {
            "profile": normalized_profile,
            "available": False,
            "passed": False,
            "reason": f"unable to fetch yahoo reference: {exc}",
            "metrics": {},
        }

    overlap = sorted(set(ibkr_series.keys()) & set(yahoo_series.keys()))
    thresholds = PRICE_QUALITY_GATE_PROFILES[normalized_profile]
    min_overlap = int(thresholds["min_overlap_days"])
    max_abs_pct_diff_limit = float(thresholds["max_abs_pct_diff_pct"])
    if len(overlap) < min_overlap:
        return {
            "profile": normalized_profile,
            "available": True,
            "passed": False,
            "reason": (
                f"insufficient overlap days for comparison: overlap={len(overlap)} min_required={min_overlap}"
            ),
            "metrics": {
                "overlap_days": len(overlap),
                "min_overlap_days_required": min_overlap,
                "max_abs_pct_diff_pct_limit": max_abs_pct_diff_limit,
            },
        }

    diffs: list[float] = []
    for dt in overlap:
        yv = float(yahoo_series[dt])
        iv = float(ibkr_series[dt])
        if yv <= 0:
            continue
        diffs.append(abs((iv - yv) / yv) * 100.0)

    if not diffs:
        return {
            "profile": normalized_profile,
            "available": True,
            "passed": False,
            "reason": "no comparable positive yahoo price points in overlap window",
            "metrics": {
                "overlap_days": len(overlap),
                "min_overlap_days_required": min_overlap,
                "max_abs_pct_diff_pct_limit": max_abs_pct_diff_limit,
            },
        }

    max_abs_pct_diff = max(diffs)
    sorted_diffs = sorted(diffs)
    mid = len(sorted_diffs) // 2
    if len(sorted_diffs) % 2 == 1:
        median_abs_pct_diff = sorted_diffs[mid]
    else:
        median_abs_pct_diff = (sorted_diffs[mid - 1] + sorted_diffs[mid]) / 2.0

    passed = max_abs_pct_diff <= max_abs_pct_diff_limit
    if passed:
        reason = "within configured divergence threshold"
    else:
        reason = (
            f"max_abs_pct_diff_pct={max_abs_pct_diff:.4f} exceeds limit={max_abs_pct_diff_limit:.4f}"
        )
    return {
        "profile": normalized_profile,
        "available": True,
        "passed": passed,
        "reason": reason,
        "metrics": {
            "overlap_days": len(overlap),
            "comparable_points": len(diffs),
            "max_abs_pct_diff_pct": round(max_abs_pct_diff, 6),
            "median_abs_pct_diff_pct": round(median_abs_pct_diff, 6),
            "max_abs_pct_diff_pct_limit": max_abs_pct_diff_limit,
            "min_overlap_days_required": min_overlap,
        },
    }
