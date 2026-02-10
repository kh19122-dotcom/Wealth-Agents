from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import pstdev
from typing import Any

from .market_prices import month_end_prices, month_strings_inclusive, parse_iso_month, read_price_cache
from .orders import compute_monthly_order_payload
from .policy import validate_allocation_sum, validate_band_pct
from .policy_instruments import PolicyInstrument, load_policy_and_instruments


DEFAULT_POLICY_PATH = "data/policy/policy.yml"
DEFAULT_PRICES_DIR = "data/prices"
DEFAULT_SIM_DIR = "sim"
DEFAULT_REPORTS_DIR = "reports"
SUPPORTED_PROVIDER = "yahoo"


def run_simulation(
    start: str,
    end: str,
    monthly: float,
    initial: float = 0.0,
    policy_path: str = DEFAULT_POLICY_PATH,
    prices_dir: str = DEFAULT_PRICES_DIR,
    sim_dir: str = DEFAULT_SIM_DIR,
    reports_dir: str = DEFAULT_REPORTS_DIR,
) -> tuple[Path, Path, dict[str, Any]]:
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

    month_index = month_strings_inclusive(start, end)
    prices_by_ticker_by_month = _load_month_end_prices(
        start=start,
        end=end,
        instruments=provider_instruments,
        prices_dir=Path(prices_dir) / SUPPORTED_PROVIDER,
    )

    rebalance_frequency, band_pct = _read_rebalance_settings(policy_doc, policy)
    warnings: list[str] = []
    non_eur = _non_eur_tickers({row.ticker for row in provider_instruments})
    if non_eur:
        warnings.append(
            "FX conversion is ignored for MVP. Non-EUR ticker pricing is used as-is: "
            + ", ".join(non_eur)
        )

    holdings_by_id: dict[str, float] = {instrument_id: 0.0 for instrument_id in sorted(instrument_by_id)}
    cash = float(initial)
    contribution_to_date = 0.0
    snapshots: list[dict[str, Any]] = []

    for month in month_index:
        cash += float(monthly)
        contribution_to_date += float(monthly)
        month_prices = {
            row.ticker: prices_by_ticker_by_month[row.ticker][month]
            for row in sorted(provider_instruments, key=lambda item: item.ticker)
        }

        budget_whole = int(cash)
        if budget_whole > 0:
            if _is_rebalance_month(month, rebalance_frequency):
                rebalance_weights = _underweight_rebalance_weights(
                    holdings_by_id=holdings_by_id,
                    month_prices=month_prices,
                    instrument_by_id=instrument_by_id,
                    target_weights=target_weights,
                    band_pct=band_pct,
                )
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
                    _execute_orders(
                        holdings_by_id=holdings_by_id,
                        cash_ref=cash_box,
                        orders=rebalance_orders,
                        month_prices=month_prices,
                        instrument_by_id=instrument_by_id,
                    )
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
                _execute_orders(
                    holdings_by_id=holdings_by_id,
                    cash_ref=cash_box,
                    orders=monthly_orders,
                    month_prices=month_prices,
                    instrument_by_id=instrument_by_id,
                )
                cash = cash_box[0]

        snapshot = _build_snapshot(
            month=month,
            holdings_by_id=holdings_by_id,
            instrument_by_id=instrument_by_id,
            month_prices=month_prices,
            cash=cash,
            contribution_to_date=contribution_to_date,
        )
        snapshots.append(snapshot)

    stats = _compute_stats(
        total_values=[row["total_value"] for row in snapshots],
        monthly_contribution=float(monthly),
    )
    payload = {
        "start": start,
        "end": end,
        "monthly_contribution_eur": float(monthly),
        "initial_cash_eur": float(initial),
        "provider": SUPPORTED_PROVIDER,
        "policy_hash": str(policy_doc.get("policy_hash") or ""),
        "rebalance": {
            "frequency": rebalance_frequency,
            "band_pct": float(band_pct),
            "buy_only": True,
        },
        "warnings": warnings,
        "stats": stats,
        "snapshots": snapshots,
    }

    sim_path = Path(sim_dir) / f"portfolio_{start}_{end}.json"
    report_path = Path(reports_dir) / f"sim_{start}_{end}.md"
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
) -> dict[str, dict[str, float]]:
    unique_tickers = sorted({row.ticker for row in instruments})
    missing_files = [ticker for ticker in unique_tickers if not (prices_dir / f"{ticker}.csv").exists()]
    if missing_files:
        raise ValueError(
            "Missing cached price files for tickers: "
            + ", ".join(missing_files)
            + ". Run fetch-prices first."
        )

    by_ticker: dict[str, dict[str, float]] = {}
    for ticker in unique_tickers:
        daily = read_price_cache(prices_dir / f"{ticker}.csv")
        try:
            by_ticker[ticker] = month_end_prices(daily, start, end)
        except ValueError as exc:
            raise ValueError(
                f"Ticker '{ticker}' is missing required month-end prices between {start} and {end}. "
                "Run fetch-prices for a wider range."
            ) from exc
    return by_ticker


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


def _execute_orders(
    holdings_by_id: dict[str, float],
    cash_ref: list[float],
    orders: list[dict[str, Any]],
    month_prices: dict[str, float],
    instrument_by_id: dict[str, PolicyInstrument],
) -> None:
    cash = float(cash_ref[0])
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
        holdings_by_id[instrument_id] += amount_eur / price
        cash -= amount_eur

    if abs(cash) < 1e-12:
        cash = 0.0
    cash_ref[0] = cash


def _build_snapshot(
    month: str,
    holdings_by_id: dict[str, float],
    instrument_by_id: dict[str, PolicyInstrument],
    month_prices: dict[str, float],
    cash: float,
    contribution_to_date: float,
) -> dict[str, Any]:
    holdings_by_ticker: dict[str, float] = {}
    value_by_ticker: dict[str, float] = {}
    for instrument_id in sorted(holdings_by_id):
        ticker = instrument_by_id[instrument_id].ticker
        shares = float(holdings_by_id[instrument_id])
        holdings_by_ticker[ticker] = holdings_by_ticker.get(ticker, 0.0) + shares
        value_by_ticker[ticker] = value_by_ticker.get(ticker, 0.0) + shares * float(month_prices[ticker])

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
        "prices": {ticker: _round_float(month_prices[ticker], 8) for ticker in sorted(month_prices)},
        "total_value": _round_float(total_value, 8),
        "weights": {ticker: _round_float(weights[ticker], 10) for ticker in sorted(weights)},
        "contribution_to_date": _round_float(contribution_to_date, 8),
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

    lines: list[str] = []
    lines.append(f"# Simulation {start} to {end}")
    lines.append("")
    lines.append(f"- Monthly contribution (EUR): {payload.get('monthly_contribution_eur')}")
    lines.append(f"- Initial cash (EUR): {payload.get('initial_cash_eur')}")
    lines.append(f"- Provider: {payload.get('provider')}")
    lines.append("")
    lines.append("## Key Stats")
    lines.append("")
    lines.append(f"- CAGR (TWR, cashflow-adjusted): {_fmt_pct(stats.get('cagr'))}")
    lines.append(f"- Annualized volatility (TWR): {_fmt_pct(stats.get('annualized_volatility'))}")
    lines.append(f"- Max drawdown (TWR): {_fmt_pct(stats.get('max_drawdown'))}")
    lines.append("- Metrics are time-weighted and remove the effect of monthly contributions.")
    lines.append("")

    warnings = payload.get("warnings") or []
    if warnings:
        lines.append("## Warnings")
        lines.append("")
        for warning in warnings:
            lines.append(f"- {warning}")
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


def _non_eur_tickers(tickers: set[str]) -> list[str]:
    non_eur_suffixes = {
        ".L",
        ".SW",
        ".OL",
        ".CO",
        ".ST",
        ".TO",
        ".AX",
        ".HK",
        ".T",
        ".KS",
        ".KQ",
        ".NS",
        ".BO",
        ".SA",
        ".MX",
        ".TA",
    }
    flagged = []
    for ticker in sorted(tickers):
        upper = ticker.upper()
        if "." not in upper:
            continue
        suffix = "." + upper.split(".")[-1]
        if suffix in non_eur_suffixes:
            flagged.append(ticker)
    return flagged
