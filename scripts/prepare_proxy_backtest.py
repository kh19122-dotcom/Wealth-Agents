from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime
from io import StringIO
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml


STOOQ_DAILY_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
USER_AGENT = "Mozilla/5.0 (compatible; WealthAgents/1.0; +https://example.invalid)"


@dataclass(frozen=True)
class ProxyInstrument:
    bucket: str
    instrument_id: str
    isin: str
    name: str
    ticker: str
    stooq_symbol: str


PROXY_INSTRUMENTS: tuple[ProxyInstrument, ...] = (
    ProxyInstrument(
        bucket="global_equity",
        instrument_id="proxy_spy",
        isin="US78462F1030",
        name="SPDR S&P 500 ETF Trust (Proxy)",
        ticker="SPY",
        stooq_symbol="spy.us",
    ),
    ProxyInstrument(
        bucket="bonds_cashlike",
        instrument_id="proxy_shy",
        isin="US4642874576",
        name="iShares 1-3 Year Treasury Bond ETF (Proxy)",
        ticker="SHY",
        stooq_symbol="shy.us",
    ),
    ProxyInstrument(
        bucket="optional_gold",
        instrument_id="proxy_gld",
        isin="US78463V1070",
        name="SPDR Gold Shares (Proxy)",
        ticker="GLD",
        stooq_symbol="gld.us",
    ),
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Prepare long-history proxy backtest inputs from Stooq and write "
            "a standalone policy YAML + local CSV caches."
        )
    )
    parser.add_argument("--start", default="2016-03-01", help="Start date in YYYY-MM-DD format")
    parser.add_argument("--end", default="2026-02-28", help="End date in YYYY-MM-DD format")
    parser.add_argument(
        "--prices-dir",
        default="/tmp/wa_prices_proxy",
        help="Output prices root directory (default: /tmp/wa_prices_proxy)",
    )
    parser.add_argument(
        "--policy-output",
        default="/tmp/wa_policy_backtest_proxy.yml",
        help="Proxy policy YAML output path (default: /tmp/wa_policy_backtest_proxy.yml)",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    start = _parse_date(args.start, field_name="start")
    end = _parse_date(args.end, field_name="end")
    if start > end:
        raise ValueError("start must be less than or equal to end.")

    prices_root = Path(args.prices_dir)
    prices_yahoo_dir = prices_root / "yahoo"
    prices_yahoo_dir.mkdir(parents=True, exist_ok=True)

    summary_rows: list[dict[str, str]] = []
    for instrument in PROXY_INSTRUMENTS:
        series = _fetch_stooq_daily_close(instrument.stooq_symbol)
        filtered = [(dt, px) for dt, px in series if start <= dt <= end]
        if not filtered:
            raise RuntimeError(
                f"No rows in requested range for {instrument.stooq_symbol} "
                f"({start.isoformat()}..{end.isoformat()})."
            )
        out_path = prices_yahoo_dir / f"{instrument.ticker}.csv"
        _write_price_cache_csv(out_path, filtered)
        summary_rows.append(
            {
                "ticker": instrument.ticker,
                "rows": str(len(filtered)),
                "first": filtered[0][0].isoformat(),
                "last": filtered[-1][0].isoformat(),
                "path": str(out_path),
            }
        )

    policy_path = Path(args.policy_output)
    policy_path.parent.mkdir(parents=True, exist_ok=True)
    policy_path.write_text(
        yaml.safe_dump(_build_proxy_policy_doc(), sort_keys=False, allow_unicode=False),
        encoding="utf-8",
    )

    print(f"Proxy policy written: {policy_path}")
    print(f"Prices root written: {prices_root}")
    print("Downloaded series summary:")
    for row in summary_rows:
        print(
            f"  - {row['ticker']}: rows={row['rows']} "
            f"first={row['first']} last={row['last']} path={row['path']}"
        )

    print("")
    print("Next commands:")
    print(
        "  uv run python -m wealth_agents simulate "
        "--start 2024-03 --end 2026-02 --monthly 2500 --initial 0 "
        "--allow-short-history "
        f"--policy {policy_path} --prices-dir {prices_root} "
        "--sim-dir /tmp/wa_backtest_proxy/sim_2y --reports-dir /tmp/wa_backtest_proxy/reports_2y"
    )
    print(
        "  uv run python -m wealth_agents simulate "
        "--start 2021-03 --end 2026-02 --monthly 2500 --initial 0 "
        "--allow-short-history "
        f"--policy {policy_path} --prices-dir {prices_root} "
        "--sim-dir /tmp/wa_backtest_proxy/sim_5y --reports-dir /tmp/wa_backtest_proxy/reports_5y"
    )
    print(
        "  uv run python -m wealth_agents simulate "
        "--start 2016-03 --end 2026-02 --monthly 2500 --initial 0 "
        "--allow-short-history "
        f"--policy {policy_path} --prices-dir {prices_root} "
        "--sim-dir /tmp/wa_backtest_proxy/sim_10y --reports-dir /tmp/wa_backtest_proxy/reports_10y"
    )
    return 0


def _parse_date(value: str, *, field_name: str) -> date:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{field_name} must be in YYYY-MM-DD format.") from exc


def _fetch_stooq_daily_close(symbol: str) -> list[tuple[date, float]]:
    url = STOOQ_DAILY_URL.format(symbol=symbol)
    req = Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urlopen(req, timeout=20) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except URLError as exc:
        raise RuntimeError(f"Failed to download Stooq data for symbol '{symbol}': {exc}") from exc

    reader = csv.DictReader(StringIO(raw))
    if not reader.fieldnames:
        raise RuntimeError(f"Stooq response has no CSV header for symbol '{symbol}'.")
    columns = {col.strip().lower(): col for col in reader.fieldnames if col}
    date_col = columns.get("date")
    close_col = columns.get("close")
    if not date_col or not close_col:
        raise RuntimeError(
            f"Unexpected Stooq CSV format for symbol '{symbol}'. "
            f"Columns={reader.fieldnames}"
        )

    out: list[tuple[date, float]] = []
    for row in reader:
        raw_date = str(row.get(date_col) or "").strip()
        raw_close = str(row.get(close_col) or "").strip()
        if not raw_date or not raw_close or raw_close.upper() == "N/A":
            continue
        try:
            dt = datetime.strptime(raw_date, "%Y-%m-%d").date()
            px = float(raw_close)
        except ValueError:
            continue
        out.append((dt, px))

    out.sort(key=lambda pair: pair[0])
    if not out:
        raise RuntimeError(f"No valid rows parsed from Stooq symbol '{symbol}'.")
    return out


def _write_price_cache_csv(path: Path, rows: Iterable[tuple[date, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "adj_close"])
        for dt, px in rows:
            writer.writerow([dt.isoformat(), f"{float(px):.10f}"])


def _build_proxy_policy_doc() -> dict:
    by_bucket: dict[str, list[dict]] = {
        "global_equity": [],
        "bonds_cashlike": [],
        "optional_gold": [],
    }
    for instrument in PROXY_INSTRUMENTS:
        by_bucket[instrument.bucket].append(
            {
                "id": instrument.instrument_id,
                "isin": instrument.isin,
                "name": instrument.name,
                "weight_within_bucket": 1.0,
                "data": {
                    "provider": "yahoo",
                    "ticker": instrument.ticker,
                    # Keep simulation FX-neutral to avoid Yahoo FX dependency.
                    "currency": "EUR",
                },
            }
        )

    return {
        "policy_version": "proxy-backtest",
        "created_at": "2026-02-28T00:00:00Z",
        "policy_hash": "proxy_backtest_stooq_v1",
        "selected_candidate": "balanced",
        "inputs_snapshot": {
            "base_currency": "EUR",
            "risk_tolerance": "medium",
            "horizon_years": 10,
        },
        "policy": {
            "target_allocation": [
                {"bucket": "global_equity", "pct": 60},
                {"bucket": "bonds_cashlike", "pct": 35},
                {"bucket": "optional_gold", "pct": 5},
            ],
            "instruments": by_bucket,
            "rebalance_rules": {"frequency": "quarterly", "band_pct": 5.0, "buy_only": False},
            "guardrails": {"max_single_asset_pct": 80, "min_trade_eur": 50},
            "notes": {
                "instrument_source": "proxy_stooq_v1",
                "proxy_notice": (
                    "This policy uses US-listed proxy ETFs from Stooq daily close data. "
                    "Use for strategy stress/backtest only."
                ),
                "fx_notice": "Prices are treated as EUR (FX-neutral approximation).",
            },
        },
    }


if __name__ == "__main__":
    raise SystemExit(main())
