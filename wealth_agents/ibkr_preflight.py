from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .fetch_prices import load_ibkr_contract_specs
from .market_prices import build_ibkr_contract
from .policy_instruments import load_policy_and_instruments


DEFAULT_POLICY_PATH = "data/policy/policy.yml"
DEFAULT_CONTRACTS_PATH = "config/ibkr_contracts.yml"
DEFAULT_REPORTS_DIR = "reports"


def run_ibkr_preflight(
    *,
    policy_path: str = DEFAULT_POLICY_PATH,
    ibkr_contracts_path: str = DEFAULT_CONTRACTS_PATH,
    host: str = "127.0.0.1",
    port: int = 7497,
    client_id: int = 37,
    timeout_sec: float = 8.0,
    lookback_days: int = 14,
    report_dir: str = DEFAULT_REPORTS_DIR,
) -> dict[str, Any]:
    if lookback_days <= 0:
        raise ValueError("lookback_days must be > 0.")

    _, _, instruments = load_policy_and_instruments(policy_path)
    tickers = sorted({row.ticker for row in instruments if row.provider == "yahoo"})
    if not tickers:
        raise ValueError("No yahoo instruments found in policy.")

    contracts = load_ibkr_contract_specs(ibkr_contracts_path)
    rows: list[dict[str, Any]] = []
    started_at = _now_iso8601()

    connect_error: str | None = None
    ib = None
    try:
        try:
            from ib_insync import IB
        except ImportError as exc:
            connect_error = (
                "ib_insync is not installed. Run: uv sync --extra dev --extra ibkr"
            )
            raise RuntimeError(connect_error) from exc

        ib = IB()
        ib.connect(
            host=str(host),
            port=int(port),
            clientId=int(client_id),
            timeout=float(timeout_sec),
            readonly=True,
        )
    except Exception as exc:  # pragma: no cover - depends on local runtime
        if connect_error is None:
            connect_error = str(exc)
    finally:
        if connect_error:
            for ticker in tickers:
                rows.append(
                    {
                        "ticker": ticker,
                        "status": "connection_failed",
                        "message": connect_error,
                    }
                )
            report_path = _write_preflight_report(
                report_dir=Path(report_dir),
                started_at=started_at,
                finished_at=_now_iso8601(),
                host=host,
                port=port,
                client_id=client_id,
                lookback_days=lookback_days,
                rows=rows,
            )
            return _build_summary(
                started_at=started_at,
                finished_at=_now_iso8601(),
                lookback_days=lookback_days,
                report_path=report_path,
                rows=rows,
            )

    assert ib is not None
    try:
        for ticker in tickers:
            contract_spec = contracts.get(ticker)
            if not contract_spec:
                rows.append(
                    {
                        "ticker": ticker,
                        "status": "missing_contract",
                        "message": f"Ticker '{ticker}' not found in {ibkr_contracts_path}",
                    }
                )
                continue

            try:
                contract = build_ibkr_contract(contract_spec)
            except Exception as exc:
                rows.append(
                    {
                        "ticker": ticker,
                        "status": "invalid_contract",
                        "message": str(exc),
                    }
                )
                continue

            try:
                qualified = ib.qualifyContracts(contract)
            except Exception as exc:
                rows.append(
                    {
                        "ticker": ticker,
                        "status": "qualify_failed",
                        "message": str(exc),
                    }
                )
                continue

            if not qualified:
                rows.append(
                    {
                        "ticker": ticker,
                        "status": "qualify_failed",
                        "message": "No matching contract found in IBKR.",
                    }
                )
                continue

            resolved = qualified[0]
            end_date = date.today()
            end_datetime = (end_date + timedelta(days=1)).strftime("%Y%m%d 00:00:00 UTC")
            try:
                bars = ib.reqHistoricalData(
                    resolved,
                    endDateTime=end_datetime,
                    durationStr=f"{int(lookback_days)} D",
                    barSizeSetting="1 day",
                    whatToShow="TRADES",
                    useRTH=True,
                    formatDate=1,
                )
            except Exception as exc:
                rows.append(
                    {
                        "ticker": ticker,
                        "status": "market_data_failed",
                        "message": str(exc),
                    }
                )
                continue

            last_date: str | None = None
            last_close: float | None = None
            for bar in reversed(list(bars or [])):
                close_value = getattr(bar, "close", None)
                if close_value is None:
                    continue
                parsed = _coerce_bar_date(getattr(bar, "date", None))
                if parsed is None:
                    continue
                last_date = parsed.isoformat()
                last_close = float(close_value)
                break

            if last_close is None:
                rows.append(
                    {
                        "ticker": ticker,
                        "status": "market_data_empty",
                        "message": "Historical data returned no usable close values.",
                    }
                )
                continue

            rows.append(
                {
                    "ticker": ticker,
                    "status": "ok",
                    "message": "contract + historical data check passed",
                    "contract": {
                        "conid": _as_int_or_none(getattr(resolved, "conId", None)),
                        "symbol": str(getattr(resolved, "symbol", "") or ""),
                        "exchange": str(getattr(resolved, "exchange", "") or ""),
                        "primary_exchange": str(getattr(resolved, "primaryExchange", "") or ""),
                        "currency": str(getattr(resolved, "currency", "") or ""),
                    },
                    "last_bar_date": last_date,
                    "last_bar_close": round(last_close, 8),
                }
            )
    finally:
        try:
            ib.disconnect()
        except Exception:
            pass

    finished_at = _now_iso8601()
    report_path = _write_preflight_report(
        report_dir=Path(report_dir),
        started_at=started_at,
        finished_at=finished_at,
        host=host,
        port=port,
        client_id=client_id,
        lookback_days=lookback_days,
        rows=rows,
    )
    return _build_summary(
        started_at=started_at,
        finished_at=finished_at,
        lookback_days=lookback_days,
        report_path=report_path,
        rows=rows,
    )


def _build_summary(
    *,
    started_at: str,
    finished_at: str,
    lookback_days: int,
    report_path: Path,
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    failed_count = sum(
        status_counts.get(key, 0)
        for key in (
            "connection_failed",
            "missing_contract",
            "invalid_contract",
            "qualify_failed",
            "market_data_failed",
            "market_data_empty",
            "unknown",
        )
    )
    return {
        "started_at": started_at,
        "finished_at": finished_at,
        "lookback_days": int(lookback_days),
        "tickers_count": len(rows),
        "failed_count": failed_count,
        "passed": failed_count == 0,
        "status_counts": status_counts,
        "report_path": str(report_path),
        "rows": rows,
    }


def _write_preflight_report(
    *,
    report_dir: Path,
    started_at: str,
    finished_at: str,
    host: str,
    port: int,
    client_id: int,
    lookback_days: int,
    rows: list[dict[str, Any]],
) -> Path:
    report_dir.mkdir(parents=True, exist_ok=True)
    out = report_dir / f"ibkr_preflight_{date.today().isoformat()}.md"

    status_counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status") or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    failed_count = sum(
        status_counts.get(key, 0)
        for key in (
            "connection_failed",
            "missing_contract",
            "invalid_contract",
            "qualify_failed",
            "market_data_failed",
            "market_data_empty",
            "unknown",
        )
    )
    passed = failed_count == 0

    lines: list[str] = []
    lines.append("# IBKR Preflight Report")
    lines.append("")
    lines.append("## Summary")
    lines.append(f"- started_at: {started_at}")
    lines.append(f"- finished_at: {finished_at}")
    lines.append(f"- host: {host}")
    lines.append(f"- port: {port}")
    lines.append(f"- client_id: {client_id}")
    lines.append(f"- lookback_days: {int(lookback_days)}")
    lines.append(f"- passed: {passed}")
    lines.append(f"- tickers_checked: {len(rows)}")
    lines.append(f"- failed_count: {failed_count}")
    for key in sorted(status_counts):
        lines.append(f"- status_{key}: {status_counts[key]}")
    lines.append("")
    lines.append("## Ticker Checks")
    lines.append("| ticker | status | last_bar_date | last_bar_close | message |")
    lines.append("|---|---|---|---:|---|")
    for row in rows:
        ticker = str(row.get("ticker") or "")
        status = str(row.get("status") or "")
        last_bar_date = str(row.get("last_bar_date") or "")
        last_bar_close = row.get("last_bar_close")
        close_text = "" if last_bar_close is None else str(last_bar_close)
        message = str(row.get("message") or "")
        lines.append(f"| {ticker} | {status} | {last_bar_date} | {close_text} | {message} |")
    lines.append("")
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def _coerce_bar_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    text = str(value).strip()
    if len(text) >= 8 and text[:8].isdigit():
        try:
            return datetime.strptime(text[:8], "%Y%m%d").date()
        except ValueError:
            pass
    if len(text) >= 10:
        try:
            return datetime.strptime(text[:10], "%Y-%m-%d").date()
        except ValueError:
            pass
    return None


def _now_iso8601() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _as_int_or_none(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
