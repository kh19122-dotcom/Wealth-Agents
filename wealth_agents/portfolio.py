from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
import json
import logging
from pathlib import Path
from typing import Any

from .market_prices import (
    parse_iso_date,
    read_price_cache,
)
from .policy import validate_allocation_sum
from .policy_instruments import PolicyInstrument, load_policy_and_instruments
from .simulation import resolve_fx_ticker


DEFAULT_POLICY_PATH = "data/policy/policy.yml"
DEFAULT_LIVE_PATH = "data/portfolio/live.json"
DEFAULT_PRICES_DIR = "data/prices"
DEFAULT_REPORTS_DIR = "reports"
SUPPORTED_PROVIDER = "yahoo"
FX_CACHE_DIRNAME = "yahoo_fx"
LOG = logging.getLogger(__name__)


@dataclass(frozen=True)
class TradeRow:
    trade_date: date
    ticker: str
    side: str
    shares: float
    price: float
    currency: str
    fee: float
    fx_rate: float | None
    note: str | None


def init_live_portfolio(
    *,
    asof: str | None = None,
    cash_eur: float = 0.0,
    policy_path: str = DEFAULT_POLICY_PATH,
    live_path: str = DEFAULT_LIVE_PATH,
    force: bool = False,
) -> tuple[Path, dict[str, Any], bool]:
    live = Path(live_path)
    if live.exists() and not force:
        return live, load_live_portfolio(live_path), False

    asof_date = parse_iso_date(asof, "asof") if asof else date.today()
    try:
        initial_cash = float(cash_eur)
    except (TypeError, ValueError) as exc:
        raise ValueError("cash must be numeric.") from exc

    _, _, instruments = load_policy_and_instruments(policy_path)
    provider_instruments = _provider_instruments(instruments, provider=SUPPORTED_PROVIDER)
    tickers = sorted({row.ticker for row in provider_instruments})
    payload = _new_live_payload(
        asof=asof_date.isoformat(),
        cash_eur=initial_cash,
        tickers=tickers,
    )
    _write_live_portfolio(live, payload)
    return live, payload, True


def import_portfolio_trades(
    *,
    csv_path: str,
    broker: str = "generic",
    asof: str | None = None,
    live_path: str = DEFAULT_LIVE_PATH,
    prices_dir: str = DEFAULT_PRICES_DIR,
    no_negative_cash: bool = False,
) -> tuple[Path, dict[str, Any], dict[str, Any]]:
    broker_name = str(broker or "").strip().lower()
    if broker_name != "generic":
        raise ValueError("portfolio import-trades currently supports only --broker generic.")

    live_file = Path(live_path)
    live_payload = load_live_portfolio(live_path)
    trades = _read_trade_csv(csv_path)
    if not trades:
        raise ValueError("No valid trade rows were found in the input CSV.")

    holdings = _ordered_holdings(live_payload.get("holdings") or {})
    cash_before = float(live_payload.get("cash_eur") or 0.0)
    cash = cash_before
    max_trade_date: date | None = None

    for trade in trades:
        if max_trade_date is None or trade.trade_date > max_trade_date:
            max_trade_date = trade.trade_date

        price_eur = _convert_amount_to_eur(
            amount=trade.price,
            currency=trade.currency,
            asof=trade.trade_date,
            fx_rate=trade.fx_rate,
            prices_dir=Path(prices_dir),
            require_cache=True,
        )
        fee_eur = _convert_amount_to_eur(
            amount=trade.fee,
            currency=trade.currency,
            asof=trade.trade_date,
            fx_rate=trade.fx_rate,
            prices_dir=Path(prices_dir),
            require_cache=True,
        )

        position = holdings.setdefault(trade.ticker, {"shares": 0.0})
        current_shares = float(position.get("shares") or 0.0)

        gross_eur = float(trade.shares) * float(price_eur)
        if trade.side == "BUY":
            new_shares = current_shares + float(trade.shares)
            cash -= gross_eur + fee_eur
        else:
            new_shares = current_shares - float(trade.shares)
            if new_shares < -1e-9:
                raise ValueError(
                    f"Trade would make holdings negative for ticker '{trade.ticker}' "
                    f"(current={current_shares}, sell={trade.shares})."
                )
            if abs(new_shares) < 1e-12:
                new_shares = 0.0
            cash += gross_eur - fee_eur

        position["shares"] = _round_float(new_shares, 10)

    if asof:
        asof_date = parse_iso_date(asof, "asof")
    else:
        if max_trade_date is None:
            raise ValueError("No trade dates found in CSV input.")
        asof_date = max_trade_date

    if cash < -1e-9:
        if no_negative_cash:
            raise ValueError(
                f"Trade import would result in negative cash (cash_eur={cash:.2f}). "
                "Re-run without --no-negative-cash to allow this or reduce BUY size."
            )
        LOG.warning(
            "Portfolio cash is negative after trade import: cash_eur=%.2f. "
            "Use --no-negative-cash to reject imports that make cash negative.",
            cash,
        )

    previous_trade_count = int(live_payload.get("trade_count") or 0)
    live_payload["asof"] = asof_date.isoformat()
    live_payload["cash_eur"] = _round_float(cash, 8)
    live_payload["holdings"] = _ordered_holdings(holdings)
    live_payload["trade_count"] = previous_trade_count + len(trades)

    _write_live_portfolio(live_file, live_payload)
    summary = {
        "imported_trades": len(trades),
        "cash_delta_eur": _round_float(cash - cash_before, 8),
        "cash_eur": _round_float(cash, 8),
        "asof": asof_date.isoformat(),
        "negative_cash": bool(cash < -1e-9),
    }
    return live_file, live_payload, summary


def generate_portfolio_drift_report(
    *,
    asof: str | None = None,
    policy_path: str = DEFAULT_POLICY_PATH,
    live_path: str = DEFAULT_LIVE_PATH,
    prices_dir: str = DEFAULT_PRICES_DIR,
    reports_dir: str = DEFAULT_REPORTS_DIR,
    prices_start: str | None = None,
    prices_end: str | None = None,
) -> tuple[Path, dict[str, Any]]:
    live_payload = load_live_portfolio(live_path)
    if str(live_payload.get("base_currency") or "EUR").strip().upper() != "EUR":
        raise ValueError("portfolio report currently supports EUR base currency only.")

    if asof:
        valuation_date = parse_iso_date(asof, "asof")
    else:
        live_asof = str(live_payload.get("asof") or "").strip()
        if not live_asof:
            raise ValueError("Missing asof date in live portfolio. Run portfolio init or pass --asof.")
        valuation_date = parse_iso_date(live_asof, "live.asof")

    price_start = parse_iso_date(prices_start, "prices_start") if prices_start else None
    price_end = parse_iso_date(prices_end, "prices_end") if prices_end else None
    if price_start and price_end and price_start > price_end:
        raise ValueError("prices_start must be less than or equal to prices_end.")

    _, policy, instruments = load_policy_and_instruments(policy_path)
    provider_instruments = _provider_instruments(instruments, provider=SUPPORTED_PROVIDER)
    target_weights = _target_weight_by_ticker(policy=policy, instruments=provider_instruments)

    holdings = _ordered_holdings(live_payload.get("holdings") or {})
    cash_eur = float(live_payload.get("cash_eur") or 0.0)
    tickers = sorted(set(target_weights) | set(holdings))

    warnings: list[str] = []
    if price_start and valuation_date < price_start:
        warnings.append(
            f"Valuation date {valuation_date.isoformat()} is before prices-start {price_start.isoformat()}."
        )
    if price_end and valuation_date > price_end:
        warnings.append(
            f"Valuation date {valuation_date.isoformat()} is after prices-end {price_end.isoformat()}."
        )

    currency_by_ticker = _detect_ticker_currencies(
        tickers=tickers,
        instruments=provider_instruments,
        warnings=warnings,
    )

    rows: list[dict[str, Any]] = []
    missing_prices: list[str] = []
    for ticker in tickers:
        shares = float((holdings.get(ticker) or {}).get("shares") or 0.0)
        currency = str(currency_by_ticker.get(ticker) or "EUR").upper()
        price_row = _load_latest_ticker_price(
            ticker=ticker,
            asof=valuation_date,
            prices_dir=Path(prices_dir),
            start_date=price_start,
            end_date=price_end,
        )

        price_eur: float | None = None
        if price_row is None:
            missing_prices.append(ticker)
            warnings.append(
                f"Ticker '{ticker}': missing cached price on or before {valuation_date.isoformat()}."
            )
        else:
            _, local_price = price_row
            rate = _report_fx_rate(
                currency=currency,
                asof=valuation_date,
                prices_dir=Path(prices_dir),
                warnings=warnings,
            )
            price_eur = float(local_price) * float(rate)

        value_eur = float(shares) * float(price_eur or 0.0)
        rows.append(
            {
                "ticker": ticker,
                "shares": float(shares),
                "price_eur": price_eur,
                "value_eur": value_eur,
                "target_weight": float(target_weights.get(ticker, 0.0)),
            }
        )

    invested_value_eur = sum(float(row["value_eur"]) for row in rows)
    total_value_eur = cash_eur + invested_value_eur
    cash_pct_of_total: float | None
    if abs(total_value_eur) <= 1e-12:
        cash_pct_of_total = None
    else:
        cash_pct_of_total = cash_eur / total_value_eur

    if invested_value_eur <= 0:
        warnings.append("No invested assets yet.")

    for row in rows:
        if invested_value_eur <= 0:
            actual_weight_invested = 0.0
        else:
            actual_weight_invested = float(row["value_eur"]) / float(invested_value_eur)
        if abs(total_value_eur) <= 1e-12:
            actual_weight_total = 0.0
        else:
            actual_weight_total = float(row["value_eur"]) / float(total_value_eur)
        row["actual_weight"] = actual_weight_invested
        row["actual_weight_invested"] = actual_weight_invested
        row["actual_weight_total"] = actual_weight_total
        row["drift_pct"] = actual_weight_invested - float(row["target_weight"])

    top_drifts = sorted(
        rows,
        key=lambda row: (-abs(float(row["drift_pct"])), str(row["ticker"])),
    )
    biggest = top_drifts[0] if top_drifts else None

    report_path = Path(reports_dir) / f"portfolio_{valuation_date.isoformat()}.md"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        _render_report(
            asof=valuation_date.isoformat(),
            cash_eur=cash_eur,
            invested_value_eur=invested_value_eur,
            total_value_eur=total_value_eur,
            cash_pct_of_total=cash_pct_of_total,
            rows=rows,
            top_drifts=top_drifts[:5],
            warnings=warnings,
            missing_prices=missing_prices,
        ),
        encoding="utf-8",
    )

    summary = {
        "asof": valuation_date.isoformat(),
        "cash_eur": _round_float(cash_eur, 8),
        "invested_value_eur": _round_float(invested_value_eur, 8),
        "total_value_eur": _round_float(total_value_eur, 8),
        "cash_pct_of_total": None if cash_pct_of_total is None else _round_float(cash_pct_of_total, 8),
        "tickers": len(rows),
        "biggest_drift_ticker": biggest["ticker"] if biggest else None,
        "biggest_drift_pct": _round_float(float(biggest["drift_pct"]), 8) if biggest else 0.0,
        "missing_prices": sorted(set(missing_prices)),
    }
    return report_path, summary


def load_live_portfolio(path: str = DEFAULT_LIVE_PATH) -> dict[str, Any]:
    live_path = Path(path)
    if not live_path.exists():
        raise ValueError(f"Live portfolio state not found at {live_path}. Run `portfolio init` first.")

    try:
        payload = json.loads(live_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in live portfolio state: {live_path}") from exc

    if not isinstance(payload, dict):
        raise ValueError(f"Invalid live portfolio state in {live_path}: root must be a JSON object.")

    base_currency = str(payload.get("base_currency") or "EUR").strip().upper()
    if base_currency != "EUR":
        raise ValueError("Live portfolio currently supports EUR base currency only.")

    asof = str(payload.get("asof") or "").strip()
    if asof:
        parse_iso_date(asof, "live.asof")

    cash_eur = _safe_float(payload.get("cash_eur"), field_name="live.cash_eur", default=0.0)
    trade_count = _safe_int(payload.get("trade_count"), field_name="live.trade_count", default=0)
    if trade_count < 0:
        raise ValueError("live.trade_count must be non-negative.")

    holdings = _ordered_holdings(payload.get("holdings") or {})
    notes = str(payload.get("notes") or "")
    normalized = {
        "base_currency": base_currency,
        "asof": asof,
        "cash_eur": _round_float(cash_eur, 8),
        "holdings": holdings,
        "trade_count": trade_count,
        "notes": notes,
    }
    return normalized


def _new_live_payload(
    *,
    asof: str,
    cash_eur: float,
    tickers: list[str],
) -> dict[str, Any]:
    holdings = {ticker: {"shares": 0.0} for ticker in sorted(set(tickers))}
    return {
        "base_currency": "EUR",
        "asof": asof,
        "cash_eur": _round_float(float(cash_eur), 8),
        "holdings": holdings,
        "trade_count": 0,
        "notes": "",
    }


def _write_live_portfolio(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    normalized = dict(payload)
    normalized["holdings"] = _ordered_holdings(payload.get("holdings") or {})
    path.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _read_trade_csv(path: str) -> list[TradeRow]:
    csv_path = Path(path)
    if not csv_path.exists():
        raise ValueError(f"CSV file not found: {csv_path}")

    delimiter = _detect_delimiter(csv_path)
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if reader.fieldnames is None:
            raise ValueError(f"Trade CSV {csv_path} is missing a header row.")

        normalized_headers = {_normalize_header(name) for name in reader.fieldnames if name is not None}
        required = {"date", "ticker", "side", "shares", "price", "currency"}
        missing = sorted(required - normalized_headers)
        if missing:
            raise ValueError(f"Trade CSV {csv_path} is missing required columns: {', '.join(missing)}")

        trades: list[TradeRow] = []
        for line_no, row in enumerate(reader, start=2):
            normalized = {_normalize_header(key): value for key, value in row.items() if key is not None}
            trade = _parse_trade_row(normalized, line_no=line_no)
            if trade is not None:
                trades.append(trade)
    return trades


def _parse_trade_row(row: dict[str, Any], *, line_no: int) -> TradeRow | None:
    raw_date = str(row.get("date") or "").strip()
    raw_ticker = str(row.get("ticker") or "").strip()
    raw_side = str(row.get("side") or "").strip().upper()
    raw_shares = str(row.get("shares") or "").strip()
    raw_price = str(row.get("price") or "").strip()
    raw_currency = str(row.get("currency") or "").strip()
    if not any([raw_date, raw_ticker, raw_side, raw_shares, raw_price, raw_currency]):
        return None

    trade_date = parse_iso_date(raw_date, f"csv row {line_no} date")
    if not raw_ticker:
        raise ValueError(f"csv row {line_no}: ticker is required.")
    if raw_side not in {"BUY", "SELL"}:
        raise ValueError(f"csv row {line_no}: side must be BUY or SELL.")

    shares = _parse_positive_float(raw_shares, field_name=f"csv row {line_no} shares")
    price = _parse_positive_float(raw_price, field_name=f"csv row {line_no} price")
    currency = _normalize_currency(raw_currency)

    raw_fee = str(row.get("fee") or "").strip()
    fee = 0.0 if not raw_fee else _parse_non_negative_float(raw_fee, field_name=f"csv row {line_no} fee")

    raw_fx_rate = str(row.get("fx_rate") or "").strip()
    fx_rate = None
    if raw_fx_rate:
        fx_rate = _parse_positive_float(raw_fx_rate, field_name=f"csv row {line_no} fx_rate")

    raw_note = str(row.get("note") or "").strip()
    note = raw_note if raw_note else None

    return TradeRow(
        trade_date=trade_date,
        ticker=raw_ticker,
        side=raw_side,
        shares=shares,
        price=price,
        currency=currency,
        fee=fee,
        fx_rate=fx_rate,
        note=note,
    )


def _normalize_header(value: Any) -> str:
    text = str(value or "").strip().lower()
    text = text.lstrip("\ufeff")
    text = text.replace("-", "_")
    text = text.replace(" ", "_")
    return text


def _detect_delimiter(path: Path) -> str:
    sample = path.read_text(encoding="utf-8-sig")[:4096]
    if not sample.strip():
        return ","
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;")
        if dialect.delimiter in {",", ";"}:
            return dialect.delimiter
    except csv.Error:
        pass
    return ";" if sample.count(";") > sample.count(",") else ","


def _provider_instruments(instruments: list[PolicyInstrument], provider: str) -> list[PolicyInstrument]:
    provider_key = provider.strip().lower()
    non_provider = [row for row in instruments if row.provider != provider_key]
    if non_provider:
        sample = ", ".join(sorted(f"{row.instrument_id}:{row.provider}" for row in non_provider))
        raise ValueError(
            f"Portfolio operations currently support only provider='{provider_key}'. "
            f"Found non-{provider_key} instruments: {sample}."
        )
    return sorted(instruments, key=lambda row: row.instrument_id)


def _target_weight_by_ticker(
    *,
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
        bucket_weight_total = sum(float(item.weight_within_bucket) for item in bucket_instruments)
        if bucket_weight_total <= 0:
            raise ValueError(f"Bucket '{bucket}' has non-positive instrument weights.")

        bucket_fraction = bucket_pct / 100.0
        for instrument in bucket_instruments:
            ticker_fraction = bucket_fraction * (float(instrument.weight_within_bucket) / bucket_weight_total)
            weights[instrument.ticker] = weights.get(instrument.ticker, 0.0) + ticker_fraction

    return weights


def _detect_ticker_currencies(
    *,
    tickers: list[str],
    instruments: list[PolicyInstrument],
    warnings: list[str],
) -> dict[str, str]:
    policy_override: dict[str, str] = {}
    for instrument in instruments:
        currency = str(instrument.currency or "").strip().upper()
        if not currency:
            continue
        existing = policy_override.get(instrument.ticker)
        if existing and existing != currency:
            raise ValueError(
                f"Ticker '{instrument.ticker}' has conflicting policy currency overrides: {existing} vs {currency}."
            )
        policy_override[instrument.ticker] = currency

    by_ticker: dict[str, str] = {}
    for ticker in sorted(set(tickers)):
        overridden = policy_override.get(ticker)
        if overridden:
            by_ticker[ticker] = overridden
            continue
        warnings.append(
            f"Ticker '{ticker}': missing policy data.currency override; assuming EUR."
        )
        by_ticker[ticker] = "EUR"
    return by_ticker


def _load_latest_ticker_price(
    *,
    ticker: str,
    asof: date,
    prices_dir: Path,
    start_date: date | None,
    end_date: date | None,
) -> tuple[date, float] | None:
    cache_path = prices_dir / SUPPORTED_PROVIDER / f"{ticker}.csv"
    if not cache_path.exists():
        return None
    series = read_price_cache(cache_path)
    if not series:
        return None
    return _latest_price_on_or_before(series, asof=asof, start_date=start_date, end_date=end_date)


def _report_fx_rate(
    *,
    currency: str,
    asof: date,
    prices_dir: Path,
    warnings: list[str],
) -> float:
    normalized = _normalize_currency(currency)
    if normalized == "EUR":
        return 1.0
    try:
        resolved = _lookup_cached_fx_rate(currency=normalized, asof=asof, prices_dir=prices_dir)
        return float(resolved["rate"])
    except ValueError as exc:
        warnings.append(f"Currency '{normalized}': {exc} Using 1.0 fallback.")
        return 1.0


def _convert_amount_to_eur(
    *,
    amount: float,
    currency: str,
    asof: date,
    fx_rate: float | None,
    prices_dir: Path,
    require_cache: bool,
) -> float:
    value = float(amount)
    normalized = _normalize_currency(currency)
    if normalized == "EUR":
        return value
    if fx_rate is not None:
        return value * float(fx_rate)

    resolved = _lookup_cached_fx_rate(currency=normalized, asof=asof, prices_dir=prices_dir)
    rate = float(resolved["rate"])
    if rate <= 0:
        if require_cache:
            raise ValueError(f"Invalid FX rate for currency '{normalized}' on {asof.isoformat()}.")
        return value
    return value * rate


def _lookup_cached_fx_rate(
    *,
    currency: str,
    asof: date,
    prices_dir: Path,
) -> dict[str, Any]:
    normalized = _normalize_currency(currency)
    if normalized == "EUR":
        return {"rate": 1.0, "fx_ticker": "EUR", "invert": False}

    fx_cache_dir = prices_dir / FX_CACHE_DIRNAME
    if not fx_cache_dir.exists():
        raise ValueError(
            f"missing FX cache directory '{fx_cache_dir}'. Add fx_rate in CSV or fetch FX prices first."
        )

    def ticker_exists(ticker: str) -> bool:
        return (fx_cache_dir / f"{ticker}.csv").exists()

    direct = f"{normalized}EUR=X"
    inverse = f"EUR{normalized}=X"
    try:
        primary = resolve_fx_ticker(normalized, ticker_exists=ticker_exists)
    except ValueError:
        primary = (direct, False)

    candidates: list[tuple[str, bool]] = []
    for ticker, invert in (primary, (direct, False), (inverse, True)):
        key = (ticker, bool(invert))
        if key not in candidates:
            candidates.append(key)

    for fx_ticker, invert in candidates:
        cache_path = fx_cache_dir / f"{fx_ticker}.csv"
        if not cache_path.exists():
            continue
        series = read_price_cache(cache_path)
        if not series:
            continue
        row = _latest_price_on_or_before(series, asof=asof, start_date=None, end_date=None)
        if row is None:
            continue
        _, raw = row
        if raw <= 0:
            continue
        rate = (1.0 / raw) if invert else raw
        if rate <= 0:
            continue
        return {
            "rate": float(rate),
            "fx_ticker": fx_ticker,
            "invert": bool(invert),
        }

    raise ValueError(
        f"missing cached FX rate for currency '{normalized}' on or before {asof.isoformat()}."
    )


def _latest_price_on_or_before(
    series: dict[date, float],
    *,
    asof: date,
    start_date: date | None,
    end_date: date | None,
) -> tuple[date, float] | None:
    candidates: list[tuple[date, float]] = []
    for dt, px in series.items():
        if dt > asof:
            continue
        if start_date and dt < start_date:
            continue
        if end_date and dt > end_date:
            continue
        candidates.append((dt, float(px)))
    if not candidates:
        return None
    return max(candidates, key=lambda row: row[0])


def _ordered_holdings(raw: Any) -> dict[str, dict[str, float]]:
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, float]] = {}
    for ticker, row in sorted(raw.items(), key=lambda item: str(item[0])):
        normalized_ticker = str(ticker).strip()
        if not normalized_ticker:
            continue
        shares_value: Any
        if isinstance(row, dict):
            shares_value = row.get("shares")
        else:
            shares_value = row
        shares = _safe_float(shares_value, field_name=f"holdings.{normalized_ticker}.shares", default=0.0)
        out[normalized_ticker] = {"shares": _round_float(shares, 10)}
    return out


def _render_report(
    *,
    asof: str,
    cash_eur: float,
    invested_value_eur: float,
    total_value_eur: float,
    cash_pct_of_total: float | None,
    rows: list[dict[str, Any]],
    top_drifts: list[dict[str, Any]],
    warnings: list[str],
    missing_prices: list[str],
) -> str:
    lines: list[str] = []
    lines.append(f"# Portfolio Drift Report  {asof}")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total value (EUR): **{_fmt_money(total_value_eur)}**")
    lines.append(f"- Invested assets value (EUR): **{_fmt_money(invested_value_eur)}**")
    lines.append(f"- Cash (EUR): **{_fmt_money(cash_eur)}**")
    if cash_pct_of_total is None:
        lines.append("- Cash % of total value: **N/A**")
    else:
        lines.append(f"- Cash % of total value: **{_fmt_pct(cash_pct_of_total)}**")
    lines.append(f"- Tickers in table: **{len(rows)}**")
    lines.append("- Weights are computed on invested assets only (cash excluded).")
    lines.append("")
    lines.append("## Allocation")
    lines.append("")
    lines.append("| ticker | shares | price_eur | value_eur | target_wt | actual_wt | actual_wt_total | drift |")
    lines.append("| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |")
    for row in sorted(rows, key=lambda item: str(item["ticker"])):
        price_eur = row.get("price_eur")
        price_text = _fmt_money(float(price_eur)) if price_eur is not None else "N/A"
        lines.append(
            f"| {row['ticker']} | {_fmt_shares(float(row['shares']))} | {price_text} | "
            f"{_fmt_money(float(row['value_eur']))} | {_fmt_pct(float(row['target_weight']))} | "
            f"{_fmt_pct(float(row['actual_weight_invested']))} | {_fmt_pct(float(row['actual_weight_total']))} | "
            f"{_fmt_pct(float(row['drift_pct']))} |"
        )
    lines.append("")
    lines.append("## Top Drifts")
    lines.append("")
    if top_drifts:
        lines.append("| ticker | target_wt | actual_wt | drift |")
        lines.append("| --- | ---: | ---: | ---: |")
        for row in top_drifts:
            lines.append(
                f"| {row['ticker']} | {_fmt_pct(float(row['target_weight']))} | "
                f"{_fmt_pct(float(row['actual_weight']))} | {_fmt_pct(float(row['drift_pct']))} |"
            )
    else:
        lines.append("- No drift rows available.")
    lines.append("")
    lines.append("## Notes / Warnings")
    lines.append("")
    lines.append("- Prices are the latest cached adjusted-close on or before `asof` (no auto-fetch).")
    lines.append("- Trade CSV `fx_rate` is interpreted as: 1 unit of local currency = `fx_rate` EUR.")
    lines.append("- Drift uses invested-only actual weights (`value_eur / invested_value_eur`).")
    if missing_prices:
        lines.append(f"- Missing ticker prices: {', '.join(sorted(set(missing_prices)))}")
    if warnings:
        for warning in warnings:
            lines.append(f"- {warning}")
    else:
        lines.append("- None.")
    lines.append("")
    return "\n".join(lines)


def _safe_float(value: Any, *, field_name: str, default: float) -> float:
    if value is None:
        return float(default)
    if isinstance(value, str) and not value.strip():
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric.") from exc


def _safe_int(value: Any, *, field_name: str, default: int) -> int:
    if value is None:
        return int(default)
    if isinstance(value, str) and not value.strip():
        return int(default)
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be an integer.") from exc


def _parse_positive_float(raw: str, *, field_name: str) -> float:
    value = _parse_float(raw, field_name=field_name)
    if value <= 0:
        raise ValueError(f"{field_name} must be positive.")
    return value


def _parse_non_negative_float(raw: str, *, field_name: str) -> float:
    value = _parse_float(raw, field_name=field_name)
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative.")
    return value


def _parse_float(raw: str, *, field_name: str) -> float:
    text = str(raw or "").strip().replace(" ", "")
    if not text:
        raise ValueError(f"{field_name} is required.")
    if "," in text and "." not in text:
        text = text.replace(",", ".")
    elif "," in text and "." in text:
        text = text.replace(",", "")
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"{field_name} must be numeric.") from exc


def _normalize_currency(value: str) -> str:
    text = str(value or "").strip().upper()
    if len(text) != 3 or not text.isalpha():
        raise ValueError(f"Invalid currency code '{value}'.")
    return text


def _fmt_money(value: float) -> str:
    return f"{float(value):,.2f}"


def _fmt_pct(value: float) -> str:
    return f"{float(value) * 100.0:.2f}%"


def _fmt_shares(value: float) -> str:
    if abs(value) < 1e-12:
        return "0"
    rendered = f"{float(value):.8f}".rstrip("0").rstrip(".")
    return rendered or "0"


def _round_float(value: float, digits: int) -> float:
    return round(float(value), digits)
