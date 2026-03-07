from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from datetime import date, datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Iterable
from urllib.error import URLError
from urllib.request import Request, urlopen

import yaml

from wealth_agents.policy import stable_policy_hash


STOOQ_DAILY_URL = "https://stooq.com/q/d/l/?s={symbol}&i=d"
USER_AGENT = "Mozilla/5.0 (compatible; WealthAgents/1.0; +https://example.invalid)"
PROXY_PRICES_NAMESPACE = "proxy_stooq"
SIMULATION_SCENARIOS: tuple[tuple[str, int], ...] = (
    ("2y", 24),
    ("5y", 60),
    ("10y", 120),
)


@dataclass(frozen=True)
class ProxyInstrument:
    bucket: str
    instrument_id: str
    isin: str
    name: str
    ticker: str
    stooq_symbol: str


@dataclass(frozen=True)
class SimulationExample:
    label: str
    start_month: str
    end_month: str
    sim_dir_name: str
    reports_dir_name: str
    clipped: bool


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
        help=(
            "Base output directory. Proxy caches are always written under "
            "<prices-dir>/proxy_stooq unless already namespaced "
            "(default: /tmp/wa_prices_proxy)"
        ),
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

    prices_root = _resolve_proxy_prices_root(Path(args.prices_dir))
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
    policy_doc = _build_proxy_policy_doc(created_at=_now_iso8601())
    policy_path.write_text(
        yaml.safe_dump(policy_doc, sort_keys=False, allow_unicode=False),
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
    examples = _build_simulation_examples(start=start, end=end)
    if not examples:
        print("  No full-month simulation window fits inside the requested start/end dates.")
        return 0
    for example in examples:
        label = example.label if not example.clipped else f"{example.label} clipped"
        print(
            f"  [{label}] uv run python -m wealth_agents simulate "
            f"--start {example.start_month} --end {example.end_month} "
            "--monthly 2500 --initial 0 "
            f"--policy {policy_path} --prices-dir {prices_root} "
            f"--sim-dir /tmp/wa_backtest_proxy/{example.sim_dir_name} "
            f"--reports-dir /tmp/wa_backtest_proxy/{example.reports_dir_name}"
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


def _resolve_proxy_prices_root(path: Path) -> Path:
    if path.name == PROXY_PRICES_NAMESPACE:
        return path
    return path / PROXY_PRICES_NAMESPACE


def _now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _calendar_month_end(dt: date) -> date:
    month_start = dt.replace(day=1)
    if month_start.month == 12:
        next_month = month_start.replace(year=month_start.year + 1, month=1, day=1)
    else:
        next_month = month_start.replace(month=month_start.month + 1, day=1)
    return next_month - date.resolution


def _add_months(month_start: date, delta_months: int) -> date:
    zero_based_month = (month_start.month - 1) + int(delta_months)
    year = month_start.year + (zero_based_month // 12)
    month = (zero_based_month % 12) + 1
    return month_start.replace(year=year, month=month, day=1)


def _full_month_window(start: date, end: date) -> tuple[str, str] | None:
    start_month = start.replace(day=1)
    if start != start_month:
        start_month = _add_months(start_month, 1)

    end_month = end.replace(day=1)
    if end != _calendar_month_end(end):
        end_month = _add_months(end_month, -1)

    if start_month > end_month:
        return None
    return start_month.strftime("%Y-%m"), end_month.strftime("%Y-%m")


def _build_simulation_examples(start: date, end: date) -> list[SimulationExample]:
    month_window = _full_month_window(start, end)
    if month_window is None:
        return []

    available_start, available_end = month_window
    available_start_month = datetime.strptime(available_start, "%Y-%m").date().replace(day=1)
    available_end_month = datetime.strptime(available_end, "%Y-%m").date().replace(day=1)

    out: list[SimulationExample] = []
    for label, span_months in SIMULATION_SCENARIOS:
        target_start = _add_months(available_end_month, -(span_months - 1))
        clipped = target_start < available_start_month
        scenario_start = max(target_start, available_start_month)
        out.append(
            SimulationExample(
                label=label,
                start_month=scenario_start.strftime("%Y-%m"),
                end_month=available_end_month.strftime("%Y-%m"),
                sim_dir_name=f"sim_{label}",
                reports_dir_name=f"reports_{label}",
                clipped=clipped,
            )
        )
    return out


def _write_price_cache_csv(path: Path, rows: Iterable[tuple[date, float]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(["date", "adj_close"])
        for dt, px in rows:
            writer.writerow([dt.isoformat(), f"{float(px):.10f}"])


def _build_proxy_policy_doc(created_at: str) -> dict:
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
                },
            }
        )

    policy = {
        "target_allocation": [
            {"bucket": "global_equity", "pct": 60},
            {"bucket": "bonds_cashlike", "pct": 35},
            {"bucket": "optional_gold", "pct": 5},
        ],
        "instruments": by_bucket,
        "rebalance_rules": {"frequency": "quarterly", "band_pct": 5.0, "buy_only": True},
        "guardrails": {"max_single_asset_pct": 80, "min_trade_eur": 50},
        "notes": {
            "instrument_source": "proxy_stooq_v1",
            "price_cache_namespace": PROXY_PRICES_NAMESPACE,
            "proxy_notice": (
                "This policy uses US-listed proxy ETFs from Stooq daily close data. "
                "Use for strategy stress/backtest only."
            ),
            "fx_notice": (
                "Underlying ETFs are USD-priced proxies. "
                "Simulation applies EUR FX conversion when Yahoo FX data is available."
            ),
        },
    }
    doc = {
        "policy_version": "proxy-backtest",
        "created_at": created_at,
        "selected_candidate": "balanced",
        "inputs_snapshot": {
            "base_currency": "EUR",
            "risk_tolerance": "medium",
            "horizon_years": 10,
        },
        "policy": policy,
    }
    doc["policy_hash"] = stable_policy_hash(
        {
            "selected_candidate": doc["selected_candidate"],
            "inputs_snapshot": doc["inputs_snapshot"],
            "policy": policy,
        }
    )
    return doc


if __name__ == "__main__":
    raise SystemExit(main())
