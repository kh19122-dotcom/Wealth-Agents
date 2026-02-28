import argparse
import json
import logging
from pathlib import Path

from .dashboard import build_dashboard
from .feed_health import format_health_table, load_feed_health
from .execution import execute_order_proposal
from .fetch_prices import fetch_prices_for_policy
from .ingest import ingest_manual_inputs
from .ibkr_preflight import run_ibkr_preflight
from .ips import (
    diversify_policy_instruments,
    draft_policy,
    finalize_policy,
    init_ips_files,
)
from .orders import propose_monthly_orders
from .orchestration import run_cycle
from .portfolio import (
    generate_portfolio_drift_report,
    import_portfolio_trades,
    init_live_portfolio,
)
from .policy_review import apply_review_proposal, review_policy
from .report import generate_weekly_report
from .rss import collect_from_feeds_with_stats
from .scheduler import run_scheduler_loop, run_scheduler_once
from .simulation import run_simulation
from .storage import append_unique_records, validate_jsonl


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="wealth_agents")
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="Collect RSS feed items")
    collect.add_argument("--config", default="config/feeds.yml")
    collect.add_argument("--output", default="data/raw/news.jsonl")
    collect.add_argument("--meta-output", default="data/meta/last_collect.json")
    collect.add_argument("--health-meta-output", default="data/meta/feed_health.json")

    report = sub.add_parser("report", help="Generate weekly report")
    report.add_argument("--week", required=True, help="ISO week format YYYY-WW, e.g., 2026-W06")
    report.add_argument("--data", default="data/raw/news.jsonl")
    report.add_argument("--rules", default="config/rules.yml")
    report.add_argument("--output-dir", default="reports")
    report.add_argument("--weekly-aggregates", default="data/meta/weekly_aggregates.jsonl")

    ingest = sub.add_parser("ingest", help="Ingest manual inputs from local files")
    ingest.add_argument("--path", default="inputs")
    ingest.add_argument("--source", default="manual")
    ingest.add_argument("--output", default="data/raw/news.jsonl")

    validate = sub.add_parser("validate", help="Validate JSONL data integrity")
    validate.add_argument("--data", default="data/raw/news.jsonl")

    feeds = sub.add_parser("feeds", help="Feed maintenance commands")
    feeds_sub = feeds.add_subparsers(dest="feeds_command", required=True)
    feeds_health = feeds_sub.add_parser("health", help="Show per-feed health/quarantine status")
    feeds_health.add_argument("--meta", default="data/meta/feed_health.json")

    ips = sub.add_parser("ips", help="Investment Policy Statement builder")
    ips_sub = ips.add_subparsers(dest="ips_command", required=True)

    ips_init = ips_sub.add_parser("init", help="Create IPS input template and checklist")
    ips_init.add_argument("--output", default="data/policy/ips_inputs.yml")
    ips_init.add_argument("--questions", default="reports/ips_questions.md")

    ips_draft = ips_sub.add_parser("draft", help="Generate IPS policy draft candidates")
    ips_draft.add_argument("--input", default="data/policy/ips_inputs.yml")
    ips_draft.add_argument("--draft-output", default="data/policy/policy_draft.yml")
    ips_draft.add_argument("--report-dir", default="reports")
    ips_draft.add_argument(
        "--weekly-aggregates",
        default="data/meta/weekly_aggregates.jsonl",
        help="Optional weekly aggregate store for signal-aware tilts (default: data/meta/weekly_aggregates.jsonl)",
    )
    ips_draft.add_argument(
        "--week",
        default=None,
        help="Optional ISO week (YYYY-Www) to anchor signal overlay; defaults to latest aggregate week.",
    )
    ips_draft.add_argument(
        "--max-signal-tilt",
        type=int,
        default=5,
        help="Maximum allocation tilt in percentage points from signal overlay (default: 5).",
    )
    ips_draft.add_argument(
        "--simulation-feedback",
        default=None,
        help="Optional simulation JSON payload path to calibrate signal tilt guardrails.",
    )

    ips_finalize = ips_sub.add_parser("finalize", help="Finalize one draft candidate")
    ips_finalize.add_argument("--choice", required=True, help="One of: conservative, balanced, aggressive")
    ips_finalize.add_argument("--draft", default="data/policy/policy_draft.yml")
    ips_finalize.add_argument("--policy-output", default="data/policy/policy.yml")
    ips_finalize.add_argument("--history", default="data/policy/policy_history.jsonl")

    ips_diversify = ips_sub.add_parser(
        "diversify",
        help="Create or apply diversified instrument template for current policy",
    )
    ips_diversify.add_argument("--policy", default="data/policy/policy.yml", help="Existing policy YAML path")
    ips_diversify.add_argument(
        "--output",
        default=None,
        help="Output policy YAML path (default: data/policy/policy_diversified.yml when not applying)",
    )
    ips_diversify.add_argument("--profile", default="core", choices=["core"])
    ips_diversify.add_argument("--history", default="data/policy/policy_history.jsonl")
    ips_diversify.add_argument(
        "--apply",
        action="store_true",
        help="Apply diversified instruments directly to --policy path",
    )
    ips_diversify.add_argument(
        "--yes",
        action="store_true",
        help="Confirm policy mutation for --apply",
    )

    propose_orders = sub.add_parser("propose-orders", help="Propose monthly BUY orders from policy")
    propose_orders.add_argument("--month", required=True, help="Month in YYYY-MM format, e.g., 2026-03")
    propose_orders.add_argument("--amount", type=float, default=None, help="Optional budget override in EUR")
    propose_orders.add_argument("--policy", default="data/policy/policy.yml")
    propose_orders.add_argument("--orders-dir", default="orders")
    propose_orders.add_argument("--reports-dir", default="reports")

    execute_orders = sub.add_parser("execute-orders", help="Execute proposed orders via broker adapter")
    execute_orders.add_argument(
        "--proposal",
        required=True,
        help="Order proposal JSON path, e.g., orders/proposed_2026-03.json",
    )
    execute_orders.add_argument(
        "--broker",
        default="mock",
        help="Broker adapter name (currently: mock)",
    )
    execute_orders.add_argument(
        "--mock-state",
        default="data/broker/mock_state.json",
        help="Mock broker state JSON path",
    )
    execute_orders.add_argument(
        "--dry-run",
        action="store_true",
        help="Validate and render execution payload without submitting to broker",
    )
    execute_orders.add_argument(
        "--output",
        default=None,
        help="Optional execution result JSON output path",
    )

    run_cycle_parser = sub.add_parser("run-cycle", help="Run Phase 1->4 orchestration cycle with checkpoints")
    run_cycle_parser.add_argument("--cycle-id", default=None, help="Optional cycle identifier")
    run_cycle_parser.add_argument("--cycle-dir", default="runs", help="Cycle root directory (default: runs)")
    run_cycle_parser.add_argument("--week", required=True, help="ISO week format YYYY-Www")
    run_cycle_parser.add_argument("--month", required=True, help="Order month format YYYY-MM")
    run_cycle_parser.add_argument("--collect-config", default="config/feeds.yml")
    run_cycle_parser.add_argument("--rules", default="config/rules.yml")
    run_cycle_parser.add_argument("--ips-input", default="data/policy/ips_inputs.yml")
    run_cycle_parser.add_argument("--policy-choice", default="balanced")
    run_cycle_parser.add_argument("--max-signal-tilt", type=int, default=5)
    run_cycle_parser.add_argument("--simulation-feedback", default=None)
    run_cycle_parser.add_argument("--simulate-start", required=True, help="Simulation start month YYYY-MM")
    run_cycle_parser.add_argument("--simulate-end", required=True, help="Simulation end month YYYY-MM")
    run_cycle_parser.add_argument("--simulate-monthly", required=True, type=float, help="Simulation monthly EUR")
    run_cycle_parser.add_argument("--simulate-initial", type=float, default=0.0, help="Simulation initial EUR")
    run_cycle_parser.add_argument("--prices-dir", default="data/prices")
    run_cycle_parser.add_argument("--allow-short-history", action="store_true")
    run_cycle_parser.add_argument(
        "--quality-gate-profile",
        choices=["off", "standard", "strict"],
        default="standard",
        help="Quality gate profile for report/order artifacts (default: standard)",
    )
    run_cycle_parser.add_argument("--execute-broker", default="mock")
    run_cycle_parser.add_argument(
        "--execute-submit",
        action="store_true",
        help="Submit broker orders (default is dry-run execution).",
    )
    run_cycle_parser.add_argument("--skip-execution", action="store_true")
    run_cycle_parser.add_argument("--resume", action="store_true")

    run_scheduler_parser = sub.add_parser(
        "run-scheduler",
        help="Run periodic scheduler that triggers run-cycle with de-dup state",
    )
    run_scheduler_parser.add_argument("--cadence", default="weekly", choices=["weekly", "monthly"])
    run_scheduler_parser.add_argument(
        "--state-path",
        default="runs/scheduler_state.json",
        help="Scheduler state path for de-duplication (default: runs/scheduler_state.json)",
    )
    run_scheduler_parser.add_argument("--cycle-dir", default="runs", help="Cycle root directory (default: runs)")
    run_scheduler_parser.add_argument(
        "--not-before",
        default=None,
        help="Do not run cycle before this date (YYYY-MM-DD).",
    )
    run_scheduler_parser.add_argument("--week", default=None, help="Optional fixed ISO week override YYYY-Www")
    run_scheduler_parser.add_argument("--month", default=None, help="Optional fixed order month override YYYY-MM")
    run_scheduler_parser.add_argument("--simulate-monthly", required=True, type=float, help="Simulation monthly EUR")
    run_scheduler_parser.add_argument("--simulate-initial", type=float, default=0.0, help="Simulation initial EUR")
    run_scheduler_parser.add_argument(
        "--simulate-lookback-months",
        type=int,
        default=14,
        help="Auto simulation window length in months (default: 14)",
    )
    run_scheduler_parser.add_argument(
        "--simulate-end-offset-months",
        type=int,
        default=0,
        help="Offset simulation end month from anchor month (default: 0)",
    )
    run_scheduler_parser.add_argument("--collect-config", default="config/feeds.yml")
    run_scheduler_parser.add_argument("--rules", default="config/rules.yml")
    run_scheduler_parser.add_argument("--ips-input", default="data/policy/ips_inputs.yml")
    run_scheduler_parser.add_argument("--policy-choice", default="balanced")
    run_scheduler_parser.add_argument("--max-signal-tilt", type=int, default=5)
    run_scheduler_parser.add_argument("--simulation-feedback", default=None)
    run_scheduler_parser.add_argument("--prices-dir", default="data/prices")
    run_scheduler_parser.add_argument("--allow-short-history", action="store_true")
    run_scheduler_parser.add_argument(
        "--quality-gate-profile",
        choices=["off", "standard", "strict"],
        default="standard",
        help="Quality gate profile for run-cycle execution (default: standard)",
    )
    run_scheduler_parser.add_argument("--execute-broker", default="mock")
    run_scheduler_parser.add_argument(
        "--execute-submit",
        action="store_true",
        help="Submit broker orders (default is dry-run execution).",
    )
    run_scheduler_parser.add_argument("--skip-execution", action="store_true")
    run_scheduler_parser.add_argument("--resume", action="store_true")
    run_scheduler_parser.add_argument("--force", action="store_true")
    run_scheduler_parser.add_argument("--loop", action="store_true", help="Run continuously until interrupted")
    run_scheduler_parser.add_argument(
        "--poll-seconds",
        type=int,
        default=300,
        help="Scheduler loop poll interval in seconds (default: 300)",
    )
    run_scheduler_parser.add_argument(
        "--max-runs",
        type=int,
        default=None,
        help="Optional maximum successful runs before exit in loop mode",
    )
    run_scheduler_parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Exit loop immediately when a cycle run fails",
    )

    ibkr_preflight = sub.add_parser(
        "ibkr-preflight",
        help="Validate IBKR connectivity, contract mappings, and historical market data access",
    )
    ibkr_preflight.add_argument("--policy", default="data/policy/policy.yml", help="Policy YAML path")
    ibkr_preflight.add_argument(
        "--ibkr-contracts",
        default="config/ibkr_contracts.yml",
        help="IBKR contract mapping YAML path",
    )
    ibkr_preflight.add_argument("--ibkr-host", default="127.0.0.1", help="IBKR TWS/Gateway host")
    ibkr_preflight.add_argument("--ibkr-port", type=int, default=7497, help="IBKR TWS/Gateway port")
    ibkr_preflight.add_argument("--ibkr-client-id", type=int, default=37, help="IBKR API client id")
    ibkr_preflight.add_argument(
        "--ibkr-timeout-sec",
        type=float,
        default=8.0,
        help="IBKR API connect timeout in seconds",
    )
    ibkr_preflight.add_argument(
        "--ibkr-lookback-days",
        type=int,
        default=14,
        help="Historical lookback window for market-data check (default: 14)",
    )
    ibkr_preflight.add_argument("--report-dir", default="reports", help="Output markdown report directory")
    ibkr_preflight.add_argument(
        "--allow-partial",
        action="store_true",
        help="Exit 0 even when some tickers fail preflight checks.",
    )

    dashboard_parser = sub.add_parser(
        "dashboard",
        help="Build static dashboard HTML from cycle checkpoints and scheduler state",
    )
    dashboard_parser.add_argument("--runs-dir", default="runs")
    dashboard_parser.add_argument("--state-path", default="runs/scheduler_state.json")
    dashboard_parser.add_argument("--output", default="runs/dashboard.html")
    dashboard_parser.add_argument("--limit", type=int, default=20)
    dashboard_parser.add_argument("--title", default="Wealth Agents Dashboard")

    fetch_prices = sub.add_parser(
        "fetch-prices",
        help="Fetch and cache adjusted-close prices from market data providers",
        description="Fetch and cache daily adjusted-close prices for policy instruments.",
        epilog=(
            "Examples:\n"
            "  python -m wealth_agents fetch-prices --start 2024-02-01 --end 2026-02-09\n"
            "  python -m wealth_agents fetch-prices --start 2025-01-01 --end 2025-12-31 --provider yahoo\n"
            "  python -m wealth_agents fetch-prices --start 2025-01-01 --end 2025-12-31 "
            "--prefer-source ibkr --ibkr-contracts config/ibkr_contracts.yml"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    fetch_prices.add_argument("--start", required=True, help="Start date (inclusive), format YYYY-MM-DD")
    fetch_prices.add_argument("--end", required=True, help="End date (inclusive), format YYYY-MM-DD")
    fetch_prices.add_argument("--provider", default="yahoo", help="Price provider name (default: yahoo)")
    fetch_prices.add_argument(
        "--prefer-source",
        default="yahoo",
        choices=("yahoo", "ibkr", "auto"),
        help="Preferred fetch source. auto/ibkr attempts IBKR first and can fall back to Yahoo.",
    )
    fetch_prices.add_argument(
        "--ibkr-contracts",
        default=None,
        help="Optional IBKR contract mapping YAML (ticker -> contract fields).",
    )
    fetch_prices.add_argument("--ibkr-host", default="127.0.0.1", help="IBKR TWS/Gateway host")
    fetch_prices.add_argument("--ibkr-port", type=int, default=7497, help="IBKR TWS/Gateway port")
    fetch_prices.add_argument("--ibkr-client-id", type=int, default=37, help="IBKR API client id")
    fetch_prices.add_argument(
        "--ibkr-timeout-sec",
        type=float,
        default=8.0,
        help="IBKR API connect timeout in seconds",
    )
    fetch_prices.add_argument(
        "--ibkr-max-retries",
        type=int,
        default=2,
        help="IBKR fetch retries per request",
    )
    fetch_prices.add_argument(
        "--no-source-fallback",
        action="store_true",
        help="Disable source fallback when prefer-source is ibkr/auto.",
    )
    fetch_prices.add_argument(
        "--price-quality-gate",
        choices=("off", "standard", "strict"),
        default="off",
        help="IBKR-vs-Yahoo divergence quality gate profile (default: off).",
    )
    fetch_prices.add_argument("--policy", default="data/policy/policy.yml", help="Policy YAML path")
    fetch_prices.add_argument("--prices-dir", default="data/prices", help="Local cache root directory")

    simulate = sub.add_parser(
        "simulate",
        help="Run monthly contribution simulation/backtest from cached prices",
        description="Run monthly portfolio simulation using cached month-end adjusted-close prices.",
        epilog=(
            "Examples:\n"
            "  python -m wealth_agents simulate --start 2024-02 --end 2026-02 --monthly 2500\n"
            "  python -m wealth_agents simulate --start 2025-01 --end 2025-12 --monthly 1500 --initial 5000"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    simulate.add_argument("--start", required=True, help="Start month (inclusive), format YYYY-MM")
    simulate.add_argument("--end", required=True, help="End month (inclusive), format YYYY-MM")
    simulate.add_argument("--monthly", required=True, type=float, help="Monthly contribution in EUR")
    simulate.add_argument("--initial", type=float, default=0.0, help="Initial cash in EUR (default: 0)")
    simulate.add_argument("--policy", default="data/policy/policy.yml", help="Policy YAML path")
    simulate.add_argument("--prices-dir", default="data/prices", help="Local price cache root directory")
    simulate.add_argument(
        "--allow-short-history",
        action="store_true",
        help="Allow late-starting tickers by shifting simulation start to the latest common first-available month.",
    )
    simulate.add_argument("--sim-dir", default="sim", help="Simulation JSON output directory")
    simulate.add_argument("--reports-dir", default="reports", help="Simulation report output directory")

    policy_review = sub.add_parser("policy-review", help="Generate quarterly-cadence policy adjustment proposal")
    policy_review.add_argument("--week", required=True, help="ISO week format YYYY-Www, e.g., 2026-W06")
    policy_review.add_argument("--policy", default="data/policy/policy.yml")
    policy_review.add_argument("--weekly-aggregates", default="data/meta/weekly_aggregates.jsonl")
    policy_review.add_argument("--report-dir", default="reports")
    policy_review.add_argument("--patch-dir", default="data/policy")
    policy_review.add_argument("--apply-history", default="data/policy/policy_apply_history.jsonl")
    policy_review.add_argument("--apply", action="store_true", help="Apply first generated proposal to policy.yml")
    policy_review.add_argument(
        "--yes",
        action="store_true",
        help="Confirm policy mutation for --apply (required in non-interactive mode)",
    )

    portfolio = sub.add_parser("portfolio", help="Live portfolio tracking and drift reporting")
    portfolio_sub = portfolio.add_subparsers(dest="portfolio_command", required=True)

    portfolio_init = portfolio_sub.add_parser("init", help="Initialize live portfolio state file")
    portfolio_init.add_argument("--asof", default=None, help="As-of date in YYYY-MM-DD format")
    portfolio_init.add_argument("--cash", type=float, default=0.0, help="Initial cash in EUR")
    portfolio_init.add_argument("--policy", default="data/policy/policy.yml", help="Policy YAML path")
    portfolio_init.add_argument("--live", default="data/portfolio/live.json", help="Live portfolio JSON path")
    portfolio_init.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing live portfolio state file if it already exists",
    )

    portfolio_import = portfolio_sub.add_parser("import-trades", help="Import executed trades CSV into live state")
    portfolio_import.add_argument("--csv", required=True, help="CSV path for executed trades")
    portfolio_import.add_argument("--broker", default="generic", help="Broker CSV dialect (default: generic)")
    portfolio_import.add_argument("--asof", default=None, help="Optional as-of date override YYYY-MM-DD")
    portfolio_import.add_argument("--live", default="data/portfolio/live.json", help="Live portfolio JSON path")
    portfolio_import.add_argument("--prices-dir", default="data/prices", help="Local price cache root directory")
    portfolio_import.add_argument(
        "--no-negative-cash",
        action="store_true",
        help="Reject import if resulting cash_eur would become negative",
    )

    portfolio_report = portfolio_sub.add_parser("report", help="Generate portfolio drift report vs policy")
    portfolio_report.add_argument("--asof", default=None, help="As-of date in YYYY-MM-DD format")
    portfolio_report.add_argument("--policy", default="data/policy/policy.yml", help="Policy YAML path")
    portfolio_report.add_argument("--live", default="data/portfolio/live.json", help="Live portfolio JSON path")
    portfolio_report.add_argument("--prices-dir", default="data/prices", help="Local price cache root directory")
    portfolio_report.add_argument("--reports-dir", default="reports", help="Report output directory")
    portfolio_report.add_argument("--prices-start", default=None, help="Optional price window start YYYY-MM-DD")
    portfolio_report.add_argument("--prices-end", default=None, help="Optional price window end YYYY-MM-DD")

    return parser


def main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s - %(message)s",
    )

    parser = build_parser()
    args = parser.parse_args()

    try:
        if args.command == "collect":
            records, stats = collect_from_feeds_with_stats(
                args.config,
                health_meta_path=args.health_meta_output,
            )
            appended = append_unique_records(path=args.output, records=records)
            stats["appended"] = appended
            _write_json(args.meta_output, stats)
            validated_lines = validate_jsonl(args.output)
            logging.getLogger(__name__).info("JSONL validated: path=%s lines=%s", args.output, validated_lines)
            logging.getLogger(__name__).info("Collect complete: fetched=%s appended=%s", len(records), appended)
            return 0

        if args.command == "report":
            validate_jsonl(args.data)
            output_path = generate_weekly_report(
                week=args.week,
                data_path=args.data,
                rules_path=args.rules,
                output_dir=args.output_dir,
                weekly_aggregates_path=args.weekly_aggregates,
            )
            logging.getLogger(__name__).info("Report generated: %s", output_path)
            return 0

        if args.command == "ingest":
            appended = ingest_manual_inputs(path=args.path, source=args.source, output_path=args.output)
            logging.getLogger(__name__).info("Ingest complete: appended=%s", appended)
            return 0

        if args.command == "validate":
            validated_lines = validate_jsonl(args.data)
            logging.getLogger(__name__).info("JSONL validation passed: path=%s lines=%s", args.data, validated_lines)
            return 0

        if args.command == "feeds":
            if args.feeds_command == "health":
                health = load_feed_health(args.meta)
                print(format_health_table(health))
                return 0

        if args.command == "ips":
            if args.ips_command == "init":
                input_path, questions_path = init_ips_files(args.output, args.questions)
                logging.getLogger(__name__).info("IPS init complete: input=%s questions=%s", input_path, questions_path)
                return 0
            if args.ips_command == "draft":
                draft_path, report_path = draft_policy(
                    input_path=args.input,
                    draft_path=args.draft_output,
                    report_dir=args.report_dir,
                    weekly_aggregates_path=args.weekly_aggregates,
                    week=args.week,
                    max_signal_tilt_pct=args.max_signal_tilt,
                    simulation_feedback_path=args.simulation_feedback,
                )
                logging.getLogger(__name__).info("IPS draft complete: draft=%s report=%s", draft_path, report_path)
                return 0
            if args.ips_command == "finalize":
                policy_path, policy_hash = finalize_policy(
                    choice=args.choice,
                    draft_path=args.draft,
                    policy_path=args.policy_output,
                    history_path=args.history,
                )
                logging.getLogger(__name__).info(
                    "IPS finalize complete: policy=%s policy_hash=%s",
                    policy_path,
                    policy_hash,
                )
                return 0
            if args.ips_command == "diversify":
                out_policy, policy_hash, summary = diversify_policy_instruments(
                    policy_path=args.policy,
                    output_path=args.output,
                    history_path=args.history,
                    profile=args.profile,
                    apply=args.apply,
                    require_yes=args.apply,
                    yes=args.yes,
                )
                logging.getLogger(__name__).info(
                    "IPS diversify complete: output=%s policy_hash=%s profile=%s apply=%s "
                    "instruments_before=%s instruments_after=%s",
                    out_policy,
                    policy_hash,
                    summary.get("profile"),
                    summary.get("apply"),
                    summary.get("previous_instrument_count"),
                    summary.get("new_instrument_count"),
                )
                return 0

        if args.command == "propose-orders":
            orders_path, report_path, _ = propose_monthly_orders(
                month=args.month,
                amount_eur=args.amount,
                policy_path=args.policy,
                orders_dir=args.orders_dir,
                reports_dir=args.reports_dir,
            )
            logging.getLogger(__name__).info(
                "Order proposal complete: orders=%s report=%s",
                orders_path,
                report_path,
            )
            return 0

        if args.command == "execute-orders":
            result = execute_order_proposal(
                proposal_path=args.proposal,
                broker=args.broker,
                mock_state_path=args.mock_state,
                dry_run=args.dry_run,
                output_path=args.output,
            )
            logging.getLogger(__name__).info(
                "Order execution complete: broker=%s dry_run=%s submitted=%s skipped=%s",
                result["broker"],
                result["dry_run"],
                result["submitted_count"],
                result["skipped_count"],
            )
            return 0

        if args.command == "run-cycle":
            result = run_cycle(
                week=args.week,
                month=args.month,
                simulate_start=args.simulate_start,
                simulate_end=args.simulate_end,
                simulate_monthly=args.simulate_monthly,
                simulate_initial=args.simulate_initial,
                cycle_id=args.cycle_id,
                cycle_dir=args.cycle_dir,
                collect_config=args.collect_config,
                rules_path=args.rules,
                ips_input_path=args.ips_input,
                policy_choice=args.policy_choice,
                max_signal_tilt_pct=args.max_signal_tilt,
                simulation_feedback_path=args.simulation_feedback,
                prices_dir=args.prices_dir,
                allow_short_history=args.allow_short_history,
                quality_gate_profile=args.quality_gate_profile,
                execute_broker=args.execute_broker,
                execute_dry_run=(not args.execute_submit),
                skip_execution=args.skip_execution,
                resume=args.resume,
            )
            logging.getLogger(__name__).info(
                "Run cycle complete: cycle_id=%s status=%s checkpoint=%s",
                result["cycle_id"],
                result["status"],
                result["checkpoint_path"],
            )
            return 0

        if args.command == "run-scheduler":
            common_kwargs = {
                "cadence": args.cadence,
                "state_path": args.state_path,
                "cycle_dir": args.cycle_dir,
                "not_before": args.not_before,
                "week": args.week,
                "month": args.month,
                "simulate_monthly": args.simulate_monthly,
                "simulate_initial": args.simulate_initial,
                "simulate_lookback_months": args.simulate_lookback_months,
                "simulate_end_offset_months": args.simulate_end_offset_months,
                "collect_config": args.collect_config,
                "rules_path": args.rules,
                "ips_input_path": args.ips_input,
                "policy_choice": args.policy_choice,
                "max_signal_tilt_pct": args.max_signal_tilt,
                "simulation_feedback_path": args.simulation_feedback,
                "prices_dir": args.prices_dir,
                "allow_short_history": args.allow_short_history,
                "quality_gate_profile": args.quality_gate_profile,
                "execute_broker": args.execute_broker,
                "execute_dry_run": (not args.execute_submit),
                "skip_execution": args.skip_execution,
                "resume": args.resume,
                "force": args.force,
            }
            if args.loop:
                summary = run_scheduler_loop(
                    **common_kwargs,
                    poll_seconds=args.poll_seconds,
                    max_runs=args.max_runs,
                    stop_on_error=args.stop_on_error,
                )
                logging.getLogger(__name__).info(
                    "Scheduler loop complete: ticks=%s successful_runs=%s last_reason=%s",
                    summary["ticks"],
                    summary["successful_runs"],
                    (summary.get("last_result") or {}).get("reason"),
                )
            else:
                result = run_scheduler_once(**common_kwargs)
                logging.getLogger(__name__).info(
                    "Scheduler tick: ran=%s cadence=%s period=%s reason=%s",
                    result["ran"],
                    result.get("cadence"),
                    result.get("period_key"),
                    result.get("reason"),
                )
            return 0

        if args.command == "dashboard":
            output_path = build_dashboard(
                runs_dir=args.runs_dir,
                state_path=args.state_path,
                output_path=args.output,
                limit=args.limit,
                title=args.title,
            )
            logging.getLogger(__name__).info("Dashboard generated: %s", output_path)
            return 0

        if args.command == "ibkr-preflight":
            summary = run_ibkr_preflight(
                policy_path=args.policy,
                ibkr_contracts_path=args.ibkr_contracts,
                host=args.ibkr_host,
                port=args.ibkr_port,
                client_id=args.ibkr_client_id,
                timeout_sec=args.ibkr_timeout_sec,
                lookback_days=args.ibkr_lookback_days,
                report_dir=args.report_dir,
            )
            logging.getLogger(__name__).info(
                "IBKR preflight: passed=%s tickers=%s failed=%s report=%s",
                summary["passed"],
                summary["tickers_count"],
                summary["failed_count"],
                summary["report_path"],
            )
            if not summary["passed"] and not args.allow_partial:
                raise RuntimeError(
                    "IBKR preflight failed. Review report: "
                    f"{summary['report_path']} (use --allow-partial to continue anyway)"
                )
            return 0

        if args.command == "fetch-prices":
            summary = fetch_prices_for_policy(
                start=args.start,
                end=args.end,
                provider=args.provider,
                policy_path=args.policy,
                prices_dir=args.prices_dir,
                prefer_source=args.prefer_source,
                ibkr_contracts_path=args.ibkr_contracts,
                ibkr_host=args.ibkr_host,
                ibkr_port=args.ibkr_port,
                ibkr_client_id=args.ibkr_client_id,
                ibkr_timeout_sec=args.ibkr_timeout_sec,
                ibkr_max_retries=args.ibkr_max_retries,
                allow_source_fallback=not args.no_source_fallback,
                price_quality_gate_profile=args.price_quality_gate,
            )
            logging.getLogger(__name__).info(
                "Price fetch summary: tickers_succeeded=%s tickers_skipped_empty=%s tickers_failed=%s total_rows_downloaded=%s",
                summary["tickers_succeeded"],
                summary["tickers_skipped_empty"],
                summary["tickers_failed"],
                summary["total_rows_downloaded"],
            )
            logging.getLogger(__name__).info(
                "Price fetch complete: provider=%s preferred_source=%s source_order=%s tickers=%s",
                summary["provider"],
                summary.get("preferred_source"),
                ",".join(summary.get("source_order", [])),
                summary["tickers_count"],
            )
            logging.getLogger(__name__).info(
                "Price quality gate: profile=%s evaluated=%s passed=%s failed=%s",
                summary.get("price_quality_gate_profile"),
                summary.get("price_quality_gate_evaluated"),
                summary.get("price_quality_gate_passed"),
                summary.get("price_quality_gate_failed"),
            )
            for row in summary["tickers"]:
                status = str(row.get("status") or "unknown")
                gate = row.get("price_quality_gate") or {}
                gate_profile = gate.get("profile")
                gate_passed = gate.get("passed")
                if status == "failed":
                    logging.getLogger(__name__).warning(
                        "Price cache: ticker=%s status=%s gate_profile=%s gate_passed=%s error=%s",
                        row["ticker"],
                        status,
                        gate_profile,
                        gate_passed,
                        row.get("error"),
                    )
                elif status == "skipped_empty":
                    logging.getLogger(__name__).warning(
                        "Price cache: ticker=%s status=%s gate_profile=%s gate_passed=%s attempted=%s had_existing_cache=%s rows_total=%s",
                        row["ticker"],
                        status,
                        gate_profile,
                        gate_passed,
                        ",".join(row.get("sources_attempted", [])),
                        row.get("had_existing_cache"),
                        row.get("rows_total"),
                    )
                else:
                    logging.getLogger(__name__).info(
                        "Price cache: ticker=%s status=%s source=%s gate_profile=%s gate_passed=%s rows_total=%s rows_appended=%s downloaded_rows=%s",
                        row["ticker"],
                        status,
                        row.get("source_used"),
                        gate_profile,
                        gate_passed,
                        row["rows_total"],
                        row["rows_appended"],
                        row["downloaded_rows"],
                    )
            return 0

        if args.command == "simulate":
            sim_path, report_path, payload = run_simulation(
                start=args.start,
                end=args.end,
                monthly=args.monthly,
                initial=args.initial,
                allow_short_history=args.allow_short_history,
                policy_path=args.policy,
                prices_dir=args.prices_dir,
                sim_dir=args.sim_dir,
                reports_dir=args.reports_dir,
            )
            history_window = payload.get("history_window") or {}
            if history_window.get("adjusted"):
                logging.getLogger(__name__).warning(
                    "Simulation window adjusted: requested=%s..%s effective=%s..%s",
                    history_window.get("requested_start"),
                    history_window.get("requested_end"),
                    history_window.get("effective_start"),
                    history_window.get("effective_end"),
                )
            for warning in payload.get("warnings", []):
                logging.getLogger(__name__).warning("Simulation warning: %s", warning)
            logging.getLogger(__name__).info(
                "Simulation complete: snapshots=%s sim=%s report=%s",
                len(payload.get("snapshots", [])),
                sim_path,
                report_path,
            )
            return 0

        if args.command == "policy-review":
            result = review_policy(
                week=args.week,
                policy_path=args.policy,
                weekly_aggregates_path=args.weekly_aggregates,
                report_dir=args.report_dir,
                patch_dir=args.patch_dir,
                apply_history_path=args.apply_history,
            )
            logging.getLogger(__name__).info(
                "Policy review complete: report=%s patch=%s proposal_generated=%s apply_allowed=%s",
                result.report_path,
                result.patch_path,
                result.proposal_generated,
                result.apply_guardrail.allowed,
            )

            if args.apply:
                applied, message = apply_review_proposal(
                    review_result=result,
                    policy_path=args.policy,
                    apply_history_path=args.apply_history,
                    require_yes=args.yes,
                )
                logging.getLogger(__name__).info("Policy apply: applied=%s detail=%s", applied, message)
            return 0

        if args.command == "portfolio":
            if args.portfolio_command == "init":
                live_path, _, initialized = init_live_portfolio(
                    asof=args.asof,
                    cash_eur=args.cash,
                    policy_path=args.policy,
                    live_path=args.live,
                    force=args.force,
                )
                if initialized:
                    logging.getLogger(__name__).info("Portfolio state initialized: %s", live_path)
                else:
                    logging.getLogger(__name__).info(
                        "Portfolio state already exists (unchanged, use --force to overwrite): %s",
                        live_path,
                    )
                return 0

            if args.portfolio_command == "import-trades":
                live_path, _, summary = import_portfolio_trades(
                    csv_path=args.csv,
                    broker=args.broker,
                    asof=args.asof,
                    live_path=args.live,
                    prices_dir=args.prices_dir,
                    no_negative_cash=args.no_negative_cash,
                )
                logging.getLogger(__name__).info(
                    "Portfolio trades imported: file=%s imported=%s cash_eur=%.2f asof=%s",
                    live_path,
                    summary["imported_trades"],
                    summary["cash_eur"],
                    summary["asof"],
                )
                return 0

            if args.portfolio_command == "report":
                report_path, summary = generate_portfolio_drift_report(
                    asof=args.asof,
                    policy_path=args.policy,
                    live_path=args.live,
                    prices_dir=args.prices_dir,
                    reports_dir=args.reports_dir,
                    prices_start=args.prices_start,
                    prices_end=args.prices_end,
                )
                ticker = summary.get("biggest_drift_ticker") or "n/a"
                drift = float(summary.get("biggest_drift_pct") or 0.0) * 100.0
                print(
                    "total_value_eur="
                    f"{float(summary['total_value_eur']):.2f} "
                    f"biggest_drift={ticker} ({drift:+.2f}%) "
                    f"report={report_path}"
                )
                logging.getLogger(__name__).info("Portfolio drift report generated: %s", report_path)
                return 0
    except (ValueError, RuntimeError) as exc:
        logging.getLogger(__name__).error(str(exc))
        return 1

    return 1


def _write_json(path: str, payload: dict) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
