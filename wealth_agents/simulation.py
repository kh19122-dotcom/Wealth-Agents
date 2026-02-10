from __future__ import annotations

from datetime import date, timedelta
import json
import math
from pathlib import Path
from statistics import pstdev
from typing import Any, Callable

from .market_prices import (
    get_yahoo_ticker_currency,
    month_end_prices,
    month_strings_inclusive,
    parse_iso_month,
    read_price_cache,
    sync_yahoo_price_cache,
)
from .orders import compute_monthly_order_payload
from .policy import validate_allocation_sum, validate_band_pct
from .policy_instruments import PolicyInstrument, load_policy_and_instruments


DEFAULT_POLICY_PATH = "data/policy/policy.yml"
DEFAULT_PRICES_DIR = "data/prices"
DEFAULT_SIM_DIR = "sim"
DEFAULT_REPORTS_DIR = "reports"
SUPPORTED_PROVIDER = "yahoo"
FX_CACHE_DIRNAME = "yahoo_fx"


def run_simulation(
    start: str,
    end: str,
    monthly: float,
    initial: float = 0.0,
    allow_short_history: bool = False,
    policy_path: str = DEFAULT_POLICY_PATH,
    prices_dir: str = DEFAULT_PRICES_DIR,
    sim_dir: str = DEFAULT_SIM_DIR,
    reports_dir: str = DEFAULT_REPORTS_DIR,
) -> tuple[Path, Path, dict[str, Any]]:
    requested_start = start
    requested_end = end
    _validate_month_range(start, end)
    if monthly < 0:
        raise ValueError("monthly must be non-negative.")
    if initial < 0:
        raise ValueError("initial must be non-negative.")

    policy_doc, policy, instruments = load_policy_and_instruments(policy_path)
    _validate_eur_policy(policy_doc, policy_path)
    provider_instruments = _provider_instruments(instruments, provider=SUPPORTED_PROVIDER)
    if not provider_instruments:
        raise ValueError("No yahoo instruments found in policy. Add data.provider='yahoo' entries first.")

    instrument_by_id = {row.instrument_id: row for row in provider_instruments}
    target_weights = _effective_target_weights(policy, provider_instruments)

    sorted_instruments = sorted(provider_instruments, key=lambda item: item.ticker)
    price_window = _load_month_end_prices(
        start=start,
        end=end,
        instruments=provider_instruments,
        prices_dir=Path(prices_dir) / SUPPORTED_PROVIDER,
        allow_short_history=allow_short_history,
    )
    effective_start = price_window["effective_start"]
    prices_by_ticker_by_month = price_window["prices"]
    first_available_by_ticker = price_window["first_available_by_ticker"]
    adjusted_window = effective_start != requested_start

    month_index = month_strings_inclusive(effective_start, requested_end)
    start_date = parse_iso_month(effective_start, "effective_start")
    end_date = _month_end_date(parse_iso_month(requested_end, "end"))

    rebalance_frequency, band_pct = _read_rebalance_settings(policy_doc, policy)
    warnings: list[str] = []
    if adjusted_window:
        warnings.append(
            f"Adjusted simulation start from {requested_start} to {effective_start} due to limited ticker history."
        )
    currency_by_ticker, currency_source_by_ticker = _detect_ticker_currencies(
        instruments=sorted_instruments,
        warnings=warnings,
    )
    currency_source_by_currency = _summarize_currency_sources(
        currency_by_ticker=currency_by_ticker,
        currency_source_by_ticker=currency_source_by_ticker,
    )
    fx_context = _build_fx_context(
        start=effective_start,
        end=requested_end,
        start_date=start_date,
        end_date=end_date,
        currencies=sorted({currency_by_ticker[row.ticker] for row in sorted_instruments}),
        currency_source_by_currency=currency_source_by_currency,
        prices_dir=Path(prices_dir),
        warnings=warnings,
    )

    holdings_by_id: dict[str, float] = {instrument_id: 0.0 for instrument_id in sorted(instrument_by_id)}
    cash = float(initial)
    contribution_to_date = 0.0
    snapshots: list[dict[str, Any]] = []

    for month in month_index:
        month_events: list[dict[str, Any]] = []
        cash += float(monthly)
        contribution_to_date += float(monthly)
        month_events.append(
            {
                "type": "contribution",
                "amount": _round_float(float(monthly), 8),
            }
        )
        month_prices_local = {
            row.ticker: prices_by_ticker_by_month[row.ticker][month]
            for row in sorted_instruments
        }
        month_fx_rates = _month_fx_rates(
            month=month,
            month_prices_local=month_prices_local,
            currency_by_ticker=currency_by_ticker,
            fx_context=fx_context,
            warnings=warnings,
        )
        month_prices_eur = {
            ticker: float(local_price) * float(month_fx_rates[currency_by_ticker[ticker]])
            for ticker, local_price in month_prices_local.items()
        }

        budget_whole = int(cash)
        month_order_details: list[dict[str, Any]] = []
        rebalance_due = _is_rebalance_month(month, rebalance_frequency)
        rebalance_triggered = False
        rebalance_underweights: list[dict[str, Any]] = []
        if budget_whole > 0:
            if rebalance_due:
                rebalance_weights = _underweight_rebalance_weights(
                    holdings_by_id=holdings_by_id,
                    month_prices=month_prices_eur,
                    instrument_by_id=instrument_by_id,
                    target_weights=target_weights,
                    band_pct=band_pct,
                )
                rebalance_underweights = _format_underweights_for_event(
                    rebalance_weights=rebalance_weights,
                    instrument_by_id=instrument_by_id,
                )
                rebalance_triggered = bool(rebalance_weights)
                if rebalance_weights:
                    try:
                        rebalance_orders = _build_orders_for_weights(
                            month=month,
                            budget_eur=budget_whole,
                            policy_doc=policy_doc,
                            instrument_by_id=instrument_by_id,
                            allocation_weights=rebalance_weights,
                            policy_path=policy_path,
                        )
                    except ValueError as exc:
                        message = str(exc)
                        if "min_trade_eur" in message and "Budget is too small" in message:
                            warnings.append(
                                f"{month}: skipped rebalance buys because budget {budget_whole} EUR is below min-trade constraints."
                            )
                            rebalance_orders = []
                        else:
                            raise
                    cash_box = [cash]
                    executed_rebalance_orders = _execute_orders(
                        holdings_by_id=holdings_by_id,
                        cash_ref=cash_box,
                        orders=rebalance_orders,
                        month_prices=month_prices_eur,
                        instrument_by_id=instrument_by_id,
                    )
                    month_order_details.extend(executed_rebalance_orders)
                    cash = cash_box[0]

            budget_whole = int(cash)
            if budget_whole > 0:
                monthly_orders = _build_monthly_orders(
                    month=month,
                    budget_eur=budget_whole,
                    policy_doc=policy_doc,
                    policy_path=policy_path,
                    warnings=warnings,
                )
                cash_box = [cash]
                executed_monthly_orders = _execute_orders(
                    holdings_by_id=holdings_by_id,
                    cash_ref=cash_box,
                    orders=monthly_orders,
                    month_prices=month_prices_eur,
                    instrument_by_id=instrument_by_id,
                )
                month_order_details.extend(executed_monthly_orders)
                cash = cash_box[0]

        month_events.append(
            {
                "type": "rebalance",
                "reason": rebalance_frequency,
                "band_pct": _round_float(float(band_pct), 8),
                "scheduled": bool(rebalance_due),
                "triggered": bool(rebalance_triggered),
                "underweights": rebalance_underweights,
            }
        )
        month_order_details = sorted(
            month_order_details,
            key=lambda row: (str(row.get("ticker") or ""), str(row.get("instrument_id") or "")),
        )
        month_events.append(
            {
                "type": "orders",
                "count": len(month_order_details),
                "total_spent": _round_float(
                    sum(float(row.get("amount_eur") or 0.0) for row in month_order_details),
                    8,
                ),
                "orders": month_order_details,
            }
        )

        snapshot = _build_snapshot(
            month=month,
            holdings_by_id=holdings_by_id,
            instrument_by_id=instrument_by_id,
            month_prices_local=month_prices_local,
            month_prices_eur=month_prices_eur,
            month_fx_rates=month_fx_rates,
            cash=cash,
            contribution_to_date=contribution_to_date,
            events=month_events,
        )
        snapshots.append(snapshot)

    stats = _compute_stats(
        total_values=[row["total_value"] for row in snapshots],
        monthly_contribution=float(monthly),
    )
    payload = {
        "start": effective_start,
        "end": requested_end,
        "requested_start": requested_start,
        "requested_end": requested_end,
        "effective_start": effective_start,
        "effective_end": requested_end,
        "monthly_contribution_eur": float(monthly),
        "initial_cash_eur": float(initial),
        "provider": SUPPORTED_PROVIDER,
        "policy_hash": str(policy_doc.get("policy_hash") or ""),
        "history_window": {
            "requested_start": requested_start,
            "requested_end": requested_end,
            "effective_start": effective_start,
            "effective_end": requested_end,
            "adjusted": adjusted_window,
            "first_available_by_ticker": first_available_by_ticker,
        },
        "rebalance": {
            "frequency": rebalance_frequency,
            "band_pct": float(band_pct),
            "buy_only": True,
        },
        "fx": {
            "base_currency": "EUR",
            "conversions": fx_context["conversions"],
            "unresolved_currencies": fx_context["unresolved_currencies"],
            "currency_sources": fx_context["currency_sources"],
        },
        "warnings": warnings,
        "stats": stats,
        "snapshots": snapshots,
    }

    sim_path = Path(sim_dir) / f"portfolio_{effective_start}_{requested_end}.json"
    report_path = Path(reports_dir) / f"sim_{effective_start}_{requested_end}.md"
    sim_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    sim_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    report_path.write_text(_render_report(payload), encoding="utf-8")
    return sim_path, report_path, payload


def _validate_month_range(start: str, end: str) -> None:
    start_month = parse_iso_month(start, "start")
    end_month = parse_iso_month(end, "end")
    if start_month > end_month:
        raise ValueError("start must be less than or equal to end.")


def _validate_eur_policy(policy_doc: dict[str, Any], policy_path: str) -> None:
    inputs_snapshot = policy_doc.get("inputs_snapshot") or {}
    base_currency = "EUR"
    if isinstance(inputs_snapshot, dict):
        base_currency = str(inputs_snapshot.get("base_currency") or "EUR").strip().upper()
    if base_currency != "EUR":
        raise ValueError(f"Simulation currently supports EUR base currency only. Found {base_currency} in {policy_path}.")


def _provider_instruments(instruments: list[PolicyInstrument], provider: str) -> list[PolicyInstrument]:
    provider_key = provider.strip().lower()
    non_provider = [row for row in instruments if row.provider != provider_key]
    if non_provider:
        sample = ", ".join(sorted(f"{row.instrument_id}:{row.provider}" for row in non_provider))
        raise ValueError(
            f"Simulation currently supports only provider='{provider_key}'. "
            f"Found non-{provider_key} instruments: {sample}."
        )
    return sorted(instruments, key=lambda row: row.instrument_id)


def resolve_fx_ticker(
    currency: str,
    ticker_exists: Callable[[str], bool] | None = None,
) -> tuple[str, bool]:
    normalized = str(currency or "").strip().upper()
    if normalized == "EUR":
        return "EUR", False
    if len(normalized) != 3:
        raise ValueError(f"Invalid currency code '{currency}'.")

    direct = f"{normalized}EUR=X"
    inverse = f"EUR{normalized}=X"
    if ticker_exists is None:
        return direct, False
    if ticker_exists(direct):
        return direct, False
    if ticker_exists(inverse):
        return inverse, True
    raise ValueError(
        f"Unable to resolve Yahoo FX ticker for currency '{normalized}'. "
        f"Tried '{direct}' and '{inverse}'."
    )


def _month_end_date(month_start: date) -> date:
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1, day=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1, day=1)
    return next_month - timedelta(days=1)


def _detect_ticker_currencies(
    instruments: list[PolicyInstrument],
    warnings: list[str],
) -> tuple[dict[str, str], dict[str, str]]:
    policy_currency_by_ticker: dict[str, str] = {}
    for instrument in sorted(instruments, key=lambda row: (row.ticker, row.instrument_id)):
        policy_currency = str(instrument.currency or "").strip().upper()
        if not policy_currency:
            continue
        existing = policy_currency_by_ticker.get(instrument.ticker)
        if existing is not None and existing != policy_currency:
            raise ValueError(
                f"Ticker '{instrument.ticker}' has conflicting policy currency overrides: {existing} vs {policy_currency}."
            )
        policy_currency_by_ticker[instrument.ticker] = policy_currency

    by_ticker: dict[str, str] = {}
    source_by_ticker: dict[str, str] = {}
    for ticker in sorted({row.ticker for row in instruments}):
        policy_currency = policy_currency_by_ticker.get(ticker)
        if policy_currency:
            by_ticker[ticker] = policy_currency
            source_by_ticker[ticker] = "policy"
            continue

        currency = get_yahoo_ticker_currency(ticker)
        if currency is None:
            warnings.append(
                f"Ticker '{ticker}': unable to detect quote currency from policy or Yahoo metadata; assuming EUR."
            )
            by_ticker[ticker] = "EUR"
            source_by_ticker[ticker] = "fallback"
            continue

        by_ticker[ticker] = str(currency).upper()
        source_by_ticker[ticker] = "metadata"
    return by_ticker, source_by_ticker


def _summarize_currency_sources(
    currency_by_ticker: dict[str, str],
    currency_source_by_ticker: dict[str, str],
) -> dict[str, str]:
    by_currency: dict[str, set[str]] = {}
    for ticker in sorted(currency_by_ticker):
        currency = str(currency_by_ticker[ticker]).upper()
        source = str(currency_source_by_ticker.get(ticker) or "metadata")
        by_currency.setdefault(currency, set()).add(source)

    summarized: dict[str, str] = {}
    for currency in sorted(by_currency):
        source_set = by_currency[currency]
        if source_set == {"policy"}:
            summarized[currency] = "policy"
        elif source_set == {"metadata"}:
            summarized[currency] = "metadata"
        elif source_set == {"fallback"}:
            summarized[currency] = "fallback"
        elif "policy" in source_set:
            summarized[currency] = "mixed"
        elif "metadata" in source_set:
            summarized[currency] = "metadata"
        else:
            summarized[currency] = "fallback"
    return summarized


def _build_fx_context(
    start: str,
    end: str,
    start_date: date,
    end_date: date,
    currencies: list[str],
    currency_source_by_currency: dict[str, str],
    prices_dir: Path,
    warnings: list[str],
) -> dict[str, Any]:
    months = month_strings_inclusive(start, end)
    rates_by_currency: dict[str, dict[str, float]] = {"EUR": {month: 1.0 for month in months}}
    conversions: list[dict[str, Any]] = []
    unresolved_currencies: list[str] = []

    fx_cache_dir = prices_dir / FX_CACHE_DIRNAME
    for currency in sorted(set(currencies)):
        if currency == "EUR":
            continue
        resolved = _load_fx_rates_for_currency(
            currency=currency,
            start=start,
            end=end,
            start_date=start_date,
            end_date=end_date,
            fx_cache_dir=fx_cache_dir,
        )
        rates_by_currency[currency] = resolved["rates"]
        if resolved["resolved"]:
            conversions.append(
                {
                    "currency": currency,
                    "fx_ticker": resolved["fx_ticker"],
                    "invert": resolved["invert"],
                }
            )
        else:
            unresolved_currencies.append(currency)
            warnings.append(
                f"Currency '{currency}': unable to resolve Yahoo FX pair; using local prices as EUR (no conversion)."
            )

    return {
        "rates_by_currency": rates_by_currency,
        "conversions": conversions,
        "unresolved_currencies": sorted(unresolved_currencies),
        "currency_sources": dict(sorted(currency_source_by_currency.items())),
    }


def _load_fx_rates_for_currency(
    currency: str,
    start: str,
    end: str,
    start_date: date,
    end_date: date,
    fx_cache_dir: Path,
) -> dict[str, Any]:
    months = month_strings_inclusive(start, end)

    base_ticker, _ = resolve_fx_ticker(currency)
    inverse_ticker = f"EUR{currency}=X"
    candidates = [(base_ticker, False), (inverse_ticker, True)]

    for fx_ticker, invert in candidates:
        if fx_ticker == "EUR":
            continue
        try:
            cache_path = fx_cache_dir / f"{fx_ticker}.csv"
            daily_series = read_price_cache(cache_path)
            try:
                month_end = month_end_prices(daily_series, start, end)
            except ValueError:
                synced = sync_yahoo_price_cache(
                    cache_path=cache_path,
                    ticker=fx_ticker,
                    start=start_date,
                    end=end_date,
                    max_retries=3,
                )
                month_end = month_end_prices(synced["series"], start, end)
            rates: dict[str, float] = {}
            for month in months:
                value = float(month_end[month])
                if value <= 0:
                    raise ValueError(f"Invalid FX rate for {fx_ticker} in month {month}.")
                rates[month] = (1.0 / value) if invert else value
            return {
                "resolved": True,
                "fx_ticker": fx_ticker,
                "invert": invert,
                "rates": rates,
            }
        except Exception:
            continue

    return {
        "resolved": False,
        "fx_ticker": None,
        "invert": False,
        "rates": {month: 1.0 for month in months},
    }


def _month_fx_rates(
    month: str,
    month_prices_local: dict[str, float],
    currency_by_ticker: dict[str, str],
    fx_context: dict[str, Any],
    warnings: list[str],
) -> dict[str, float]:
    rates_by_currency = fx_context["rates_by_currency"]
    month_rates: dict[str, float] = {}
    for ticker in sorted(month_prices_local):
        currency = currency_by_ticker.get(ticker, "EUR")
        month_series = rates_by_currency.get(currency)
        if not isinstance(month_series, dict):
            warnings.append(f"{month}: missing FX context for currency '{currency}', using 1.0.")
            month_rates[currency] = 1.0
            continue
        rate = month_series.get(month)
        if rate is None:
            warnings.append(f"{month}: missing FX rate for currency '{currency}', using 1.0.")
            month_rates[currency] = 1.0
            continue
        month_rates[currency] = float(rate)
    if "EUR" not in month_rates:
        month_rates["EUR"] = 1.0
    return month_rates


def _effective_target_weights(
    policy: dict[str, Any],
    instruments: list[PolicyInstrument],
) -> dict[str, float]:
    raw_target = policy.get("target_allocation")
    if not isinstance(raw_target, list) or not raw_target:
        raise ValueError("policy.target_allocation must be a non-empty list.")
    validate_allocation_sum(raw_target)

    by_bucket: dict[str, list[PolicyInstrument]] = {}
    for instrument in instruments:
        by_bucket.setdefault(instrument.bucket, []).append(instrument)

    weights: dict[str, float] = {}
    for row in raw_target:
        bucket = str(row.get("bucket") or "").strip()
        if not bucket:
            raise ValueError("Each target_allocation row must include bucket.")
        try:
            bucket_pct = float(row.get("pct"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid pct for bucket '{bucket}'.") from exc
        if bucket_pct <= 0:
            continue

        bucket_instruments = by_bucket.get(bucket)
        if not bucket_instruments:
            raise ValueError(f"Missing instruments for target bucket '{bucket}'.")

        bucket_total = sum(float(item.weight_within_bucket) for item in bucket_instruments)
        if bucket_total <= 0:
            raise ValueError(f"Bucket '{bucket}' has non-positive instrument weights.")
        bucket_fraction = bucket_pct / 100.0
        for instrument in bucket_instruments:
            instrument_fraction = float(instrument.weight_within_bucket) / bucket_total
            weights[instrument.instrument_id] = bucket_fraction * instrument_fraction

    if not weights:
        raise ValueError("No effective target weights were generated from policy.target_allocation.")
    return weights


def _load_month_end_prices(
    start: str,
    end: str,
    instruments: list[PolicyInstrument],
    prices_dir: Path,
    allow_short_history: bool = False,
) -> dict[str, Any]:
    unique_tickers = sorted({row.ticker for row in instruments})
    missing_files = [ticker for ticker in unique_tickers if not (prices_dir / f"{ticker}.csv").exists()]
    if missing_files:
        raise ValueError(
            "Missing cached price files for tickers: "
            + ", ".join(missing_files)
            + ". Run fetch-prices first."
        )

    first_available_by_ticker: dict[str, dict[str, str]] = {}
    daily_by_ticker: dict[str, dict[date, float]] = {}
    first_month_candidates: list[str] = []
    for ticker in unique_tickers:
        daily = read_price_cache(prices_dir / f"{ticker}.csv")
        if not daily:
            raise ValueError(
                f"Ticker '{ticker}' has an empty local price cache. Run fetch-prices first."
            )
        first_dt = min(daily)
        first_month = first_dt.strftime("%Y-%m")
        first_available_by_ticker[ticker] = {
            "first_date": first_dt.isoformat(),
            "first_month": first_month,
        }
        daily_by_ticker[ticker] = daily
        first_month_candidates.append(first_month)

    effective_start = start
    if allow_short_history and first_month_candidates:
        latest_first_month = max(first_month_candidates)
        if latest_first_month > effective_start:
            effective_start = latest_first_month

    if effective_start > end:
        raise ValueError(
            f"Effective start {effective_start} is after end {end}; insufficient overlapping history across tickers."
        )

    by_ticker: dict[str, dict[str, float]] = {}
    for ticker in unique_tickers:
        daily = daily_by_ticker[ticker]
        try:
            by_ticker[ticker] = month_end_prices(daily, effective_start, end)
        except ValueError as exc:
            if allow_short_history:
                raise ValueError(
                    f"Ticker '{ticker}' is missing required month-end prices between effective_start={effective_start} and {end}. "
                    "This is beyond initial start-history truncation; refresh caches or narrow range."
                ) from exc
            raise ValueError(
                f"Ticker '{ticker}' is missing required month-end prices between {start} and {end}. "
                "Run fetch-prices for a wider range."
            ) from exc
    return {
        "prices": by_ticker,
        "effective_start": effective_start,
        "first_available_by_ticker": first_available_by_ticker,
    }


def _read_rebalance_settings(policy_doc: dict[str, Any], policy: dict[str, Any]) -> tuple[str, float]:
    frequency = "quarterly"
    band_pct = 5.0

    rules = policy.get("rebalance_rules")
    if isinstance(rules, dict):
        raw_frequency = str(rules.get("frequency") or "").strip().lower()
        if raw_frequency:
            frequency = raw_frequency
        if "band_pct" in rules:
            band_pct = float(validate_band_pct(rules.get("band_pct")))

    if not isinstance(rules, dict):
        snapshot = policy_doc.get("inputs_snapshot") or {}
        if isinstance(snapshot, dict):
            rebalance = snapshot.get("rebalance") or {}
            if isinstance(rebalance, dict):
                raw_frequency = str(rebalance.get("frequency") or "").strip().lower()
                if raw_frequency:
                    frequency = raw_frequency
                if "band_pct" in rebalance:
                    band_pct = float(validate_band_pct(rebalance.get("band_pct")))

    if frequency not in {"quarterly", "monthly"}:
        frequency = "quarterly"
    return frequency, float(band_pct)


def _is_rebalance_month(month: str, frequency: str) -> bool:
    if frequency == "monthly":
        return True
    month_num = int(month.split("-")[1])
    return month_num in {3, 6, 9, 12}


def _underweight_rebalance_weights(
    holdings_by_id: dict[str, float],
    month_prices: dict[str, float],
    instrument_by_id: dict[str, PolicyInstrument],
    target_weights: dict[str, float],
    band_pct: float,
) -> dict[str, float]:
    asset_values: dict[str, float] = {}
    invested_total = 0.0
    for instrument_id, shares in holdings_by_id.items():
        ticker = instrument_by_id[instrument_id].ticker
        px = month_prices[ticker]
        value = float(shares) * float(px)
        asset_values[instrument_id] = value
        invested_total += value

    if invested_total <= 0:
        return {}

    band_fraction = float(band_pct) / 100.0
    deficits: dict[str, float] = {}
    for instrument_id, target_fraction in target_weights.items():
        current_fraction = asset_values.get(instrument_id, 0.0) / invested_total
        threshold = target_fraction - band_fraction
        if current_fraction + 1e-12 >= threshold:
            continue
        deficit_value = max(0.0, (target_fraction - current_fraction) * invested_total)
        if deficit_value > 0:
            deficits[instrument_id] = deficit_value
    return deficits


def _build_monthly_orders(
    month: str,
    budget_eur: int,
    policy_doc: dict[str, Any],
    policy_path: str,
    warnings: list[str],
) -> list[dict[str, Any]]:
    if budget_eur <= 0:
        return []
    try:
        result = compute_monthly_order_payload(
            month=month,
            policy_doc=policy_doc,
            amount_eur=float(budget_eur),
            policy_path_for_errors=policy_path,
        )
    except ValueError as exc:
        message = str(exc)
        if "min_trade_eur" in message and "Budget is too small" in message:
            warnings.append(f"{month}: skipped monthly buys because budget {budget_eur} EUR is below min-trade constraints.")
            return []
        raise
    return list(result.payload.get("orders", []))


def _build_orders_for_weights(
    month: str,
    budget_eur: int,
    policy_doc: dict[str, Any],
    instrument_by_id: dict[str, PolicyInstrument],
    allocation_weights: dict[str, float],
    policy_path: str,
) -> list[dict[str, Any]]:
    if budget_eur <= 0 or not allocation_weights:
        return []

    policy_hash = str(policy_doc.get("policy_hash") or "").strip()
    if not policy_hash:
        raise ValueError(f"Missing top-level policy_hash in {policy_path}.")

    guardrails = {}
    policy_section = policy_doc.get("policy")
    if isinstance(policy_section, dict):
        raw_guardrails = policy_section.get("guardrails")
        if isinstance(raw_guardrails, dict):
            guardrails = dict(raw_guardrails)
    if "min_trade_eur" not in guardrails:
        guardrails["min_trade_eur"] = 0

    normalized = _normalize_weights(allocation_weights)
    target_allocation = [
        {"bucket": instrument_id, "pct": pct}
        for instrument_id, pct in normalized.items()
    ]
    instruments = {
        instrument_id: [
            {
                "id": instrument_id,
                "isin": instrument_by_id[instrument_id].isin,
                "name": instrument_by_id[instrument_id].name,
                "weight_within_bucket": 1.0,
            }
        ]
        for instrument_id in normalized
    }
    pseudo_doc = {
        "policy_hash": policy_hash,
        "inputs_snapshot": policy_doc.get("inputs_snapshot") or {"base_currency": "EUR"},
        "policy": {
            "target_allocation": target_allocation,
            "instruments": instruments,
            "contribution_schedule": {"planned_installment_eur": float(budget_eur)},
            "guardrails": guardrails,
        },
    }
    result = compute_monthly_order_payload(
        month=month,
        policy_doc=pseudo_doc,
        amount_eur=float(budget_eur),
        policy_path_for_errors=policy_path,
    )
    return list(result.payload.get("orders", []))


def _normalize_weights(weights: dict[str, float]) -> dict[str, float]:
    positive = {k: float(v) for k, v in weights.items() if float(v) > 0}
    if not positive:
        return {}
    total = sum(positive.values())
    if total <= 0:
        return {}

    keys = sorted(positive)
    out: dict[str, float] = {}
    running = 0.0
    for key in keys[:-1]:
        pct = (positive[key] / total) * 100.0
        out[key] = pct
        running += pct
    out[keys[-1]] = max(0.0, 100.0 - running)
    return out


def _format_underweights_for_event(
    rebalance_weights: dict[str, float],
    instrument_by_id: dict[str, PolicyInstrument],
) -> list[dict[str, Any]]:
    payload: list[dict[str, Any]] = []
    for instrument_id in sorted(rebalance_weights):
        ticker = instrument_by_id[instrument_id].ticker
        payload.append(
            {
                "ticker": ticker,
                "instrument_id": instrument_id,
                "deficit_eur": _round_float(float(rebalance_weights[instrument_id]), 8),
            }
        )
    return payload


def _execute_orders(
    holdings_by_id: dict[str, float],
    cash_ref: list[float],
    orders: list[dict[str, Any]],
    month_prices: dict[str, float],
    instrument_by_id: dict[str, PolicyInstrument],
) -> list[dict[str, Any]]:
    cash = float(cash_ref[0])
    executed: list[dict[str, Any]] = []
    for order in orders:
        if str(order.get("side")) != "BUY":
            continue
        instrument_id = str(order.get("instrument_id") or "").strip()
        if instrument_id not in instrument_by_id:
            raise ValueError(f"Unknown instrument_id in order payload: {instrument_id}")
        ticker = instrument_by_id[instrument_id].ticker
        price = float(month_prices.get(ticker, 0.0))
        if price <= 0:
            raise ValueError(f"Invalid month-end price for ticker '{ticker}'.")

        amount_eur = float(order.get("amount_eur") or 0.0)
        if amount_eur <= 0:
            continue
        if amount_eur - cash > 1e-9:
            raise RuntimeError(f"Insufficient cash for order on {instrument_id}: need {amount_eur}, have {cash}.")
        shares = amount_eur / price
        holdings_by_id[instrument_id] += shares
        cash -= amount_eur
        executed.append(
            {
                "ticker": ticker,
                "instrument_id": instrument_id,
                "shares": _round_float(shares, 10),
                "price_eur": _round_float(price, 8),
                "amount_eur": _round_float(amount_eur, 8),
            }
        )

    if abs(cash) < 1e-12:
        cash = 0.0
    cash_ref[0] = cash
    return executed


def _build_snapshot(
    month: str,
    holdings_by_id: dict[str, float],
    instrument_by_id: dict[str, PolicyInstrument],
    month_prices_local: dict[str, float],
    month_prices_eur: dict[str, float],
    month_fx_rates: dict[str, float],
    cash: float,
    contribution_to_date: float,
    events: list[dict[str, Any]],
) -> dict[str, Any]:
    holdings_by_ticker: dict[str, float] = {}
    value_by_ticker: dict[str, float] = {}
    for instrument_id in sorted(holdings_by_id):
        ticker = instrument_by_id[instrument_id].ticker
        shares = float(holdings_by_id[instrument_id])
        holdings_by_ticker[ticker] = holdings_by_ticker.get(ticker, 0.0) + shares
        value_by_ticker[ticker] = value_by_ticker.get(ticker, 0.0) + shares * float(month_prices_eur[ticker])

    total_value = float(cash) + sum(value_by_ticker.values())
    weights = {}
    for ticker in sorted(holdings_by_ticker):
        if total_value <= 0:
            weights[ticker] = 0.0
        else:
            weights[ticker] = value_by_ticker[ticker] / total_value

    return {
        "date": month,
        "cash": _round_float(cash, 8),
        "holdings": {ticker: _round_float(holdings_by_ticker[ticker], 10) for ticker in sorted(holdings_by_ticker)},
        "prices": {ticker: _round_float(month_prices_eur[ticker], 8) for ticker in sorted(month_prices_eur)},
        "prices_local": {ticker: _round_float(month_prices_local[ticker], 8) for ticker in sorted(month_prices_local)},
        "prices_eur": {ticker: _round_float(month_prices_eur[ticker], 8) for ticker in sorted(month_prices_eur)},
        "fx_rates": {currency: _round_float(month_fx_rates[currency], 8) for currency in sorted(month_fx_rates)},
        "total_value": _round_float(total_value, 8),
        "weights": {ticker: _round_float(weights[ticker], 10) for ticker in sorted(weights)},
        "contribution_to_date": _round_float(contribution_to_date, 8),
        "events": events,
    }


def _compute_stats(
    total_values: list[float],
    monthly_contribution: float,
) -> dict[str, float | None]:
    if not total_values:
        return {"cagr": None, "annualized_volatility": None, "max_drawdown": None}

    returns: list[float] = []
    cashflow = float(monthly_contribution)
    for idx in range(1, len(total_values)):
        prev = float(total_values[idx - 1])
        curr = float(total_values[idx])
        if prev <= 0:
            continue
        returns.append(((curr - cashflow) / prev) - 1.0)

    cagr: float | None = None
    if len(returns) >= 2:
        growth_end = 1.0
        for monthly_return in returns:
            growth_end *= (1.0 + monthly_return)
        if growth_end > 0:
            cagr = growth_end ** (12.0 / len(returns)) - 1.0

    if len(returns) < 2:
        annualized_vol = 0.0
    else:
        annualized_vol = pstdev(returns) * math.sqrt(12.0)

    growth = 1.0
    peak = 1.0
    max_drawdown = 0.0
    for monthly_return in returns:
        growth *= (1.0 + monthly_return)
        if growth > peak:
            peak = growth
        if peak > 0:
            drawdown = (growth / peak) - 1.0
            if drawdown < max_drawdown:
                max_drawdown = drawdown

    return {
        "cagr": cagr,
        "annualized_volatility": annualized_vol,
        "max_drawdown": max_drawdown,
    }


def _render_report(payload: dict[str, Any]) -> str:
    start = payload["start"]
    end = payload["end"]
    stats = payload.get("stats") or {}
    snapshots = payload.get("snapshots") or []
    history_window = payload.get("history_window") or {}

    lines: list[str] = []
    lines.append(f"# Simulation {start} to {end}")
    lines.append("")
    lines.append(f"- Monthly contribution (EUR): {payload.get('monthly_contribution_eur')}")
    lines.append(f"- Initial cash (EUR): {payload.get('initial_cash_eur')}")
    lines.append(f"- Provider: {payload.get('provider')}")
    lines.append("")

    requested_start = str(history_window.get("requested_start") or payload.get("requested_start") or start)
    requested_end = str(history_window.get("requested_end") or payload.get("requested_end") or end)
    effective_start = str(history_window.get("effective_start") or payload.get("effective_start") or start)
    effective_end = str(history_window.get("effective_end") or payload.get("effective_end") or end)
    adjusted = bool(history_window.get("adjusted"))
    first_available_by_ticker = history_window.get("first_available_by_ticker") or {}

    lines.append("## History Window")
    lines.append("")
    lines.append(f"- Requested window: {requested_start} to {requested_end}")
    lines.append(f"- Effective window: {effective_start} to {effective_end}")
    if adjusted:
        lines.append(
            "- Output filenames use effective_start to prevent misleading artifacts when short history is allowed."
        )
    if isinstance(first_available_by_ticker, dict) and first_available_by_ticker:
        lines.append("")
        lines.append("| ticker | first_available_date | first_available_month |")
        lines.append("|---|---|---|")
        for ticker in sorted(first_available_by_ticker):
            row = first_available_by_ticker.get(ticker) or {}
            lines.append(
                f"| {ticker} | {row.get('first_date', '')} | {row.get('first_month', '')} |"
            )
    lines.append("")

    lines.append("## Key Stats")
    lines.append("")
    lines.append(f"- CAGR (TWR, cashflow-adjusted): {_fmt_pct(stats.get('cagr'))}")
    lines.append(f"- Annualized volatility (TWR): {_fmt_pct(stats.get('annualized_volatility'))}")
    lines.append(f"- Max drawdown (TWR): {_fmt_pct(stats.get('max_drawdown'))}")
    lines.append("- Metrics are time-weighted and remove the effect of monthly contributions.")
    lines.append("")

    fx = payload.get("fx") or {}
    conversions = fx.get("conversions") or []
    currency_sources = fx.get("currency_sources") or {}
    unresolved = fx.get("unresolved_currencies") or []
    lines.append("## FX Conversion")
    lines.append("")
    lines.append(f"- Base currency: {fx.get('base_currency', 'EUR')}")
    if conversions:
        lines.append("")
        lines.append("| currency | fx_ticker | invert | source |")
        lines.append("|---|---|---:|---|")
        for row in conversions:
            currency = str(row["currency"])
            source = str(currency_sources.get(currency) or row.get("currency_source") or "metadata")
            lines.append(
                f"| {currency} | {row['fx_ticker']} | {str(bool(row.get('invert'))).lower()} | {source} |"
            )
    else:
        lines.append("- No non-EUR conversions were required.")
    if unresolved:
        lines.append("")
        lines.append("- Unresolved currencies (fallback to local pricing): " + ", ".join(sorted(unresolved)))
    lines.append("")

    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in warnings:
            lines.append(f"- {warning}")
        lines.append("")

    lines.append("## Events")
    lines.append("")
    for row in snapshots:
        month = str(row.get("date") or "")
        events = row.get("events") or []
        contribution = _find_event(events, "contribution")
        rebalance = _find_event(events, "rebalance")
        orders = _find_event(events, "orders")

        contribution_amount = float((contribution or {}).get("amount") or 0.0)
        lines.append(f"- {month}: contribution +{contribution_amount:.2f} EUR")

        rebalance_triggered = bool((rebalance or {}).get("triggered"))
        reason = str((rebalance or {}).get("reason") or "")
        lines.append(f"- {month}: rebalance triggered={'yes' if rebalance_triggered else 'no'} ({reason})")

        order_rows = list((orders or {}).get("orders") or [])
        top_buys = sorted(
            order_rows,
            key=lambda item: (-float(item.get("amount_eur") or 0.0), str(item.get("ticker") or "")),
        )[:3]
        if top_buys:
            rendered = ", ".join(
                f"{str(item.get('ticker') or '')} {float(item.get('amount_eur') or 0.0):.2f} EUR"
                for item in top_buys
            )
            lines.append(f"- {month}: top buys {rendered}")
        else:
            lines.append(f"- {month}: top buys none")
    lines.append("")

    lines.append("## Monthly Portfolio Value")
    lines.append("")
    lines.append("| month | total_value | cash | contribution_to_date |")
    lines.append("|---|---:|---:|---:|")
    for row in snapshots:
        lines.append(
            f"| {row['date']} | {row['total_value']:.2f} | {row['cash']:.2f} | {row.get('contribution_to_date', 0.0):.2f} |"
        )
    return "\n".join(lines).strip() + "\n"


def _fmt_pct(value: Any) -> str:
    if value is None:
        return "N/A"
    return f"{float(value) * 100.0:.2f}%"


def _round_float(value: float, digits: int) -> float:
    return round(float(value), digits)


def _find_event(events: list[dict[str, Any]], event_type: str) -> dict[str, Any] | None:
    for event in events:
        if str(event.get("type") or "") == event_type:
            return event
    return None
