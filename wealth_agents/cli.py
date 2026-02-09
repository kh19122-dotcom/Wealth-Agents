import argparse
import json
import logging
from pathlib import Path

from .feed_health import format_health_table, load_feed_health
from .ingest import ingest_manual_inputs
from .ips import (
    draft_policy,
    finalize_policy,
    init_ips_files,
)
from .orders import propose_monthly_orders
from .policy_review import apply_review_proposal, review_policy
from .report import generate_weekly_report
from .rss import collect_from_feeds_with_stats
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

    ips_finalize = ips_sub.add_parser("finalize", help="Finalize one draft candidate")
    ips_finalize.add_argument("--choice", required=True, help="One of: conservative, balanced, aggressive")
    ips_finalize.add_argument("--draft", default="data/policy/policy_draft.yml")
    ips_finalize.add_argument("--policy-output", default="data/policy/policy.yml")
    ips_finalize.add_argument("--history", default="data/policy/policy_history.jsonl")

    propose_orders = sub.add_parser("propose-orders", help="Propose monthly BUY orders from policy")
    propose_orders.add_argument("--month", required=True, help="Month in YYYY-MM format, e.g., 2026-03")
    propose_orders.add_argument("--amount", type=float, default=None, help="Optional budget override in EUR")
    propose_orders.add_argument("--policy", default="data/policy/policy.yml")
    propose_orders.add_argument("--orders-dir", default="orders")
    propose_orders.add_argument("--reports-dir", default="reports")

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
    except (ValueError, RuntimeError) as exc:
        logging.getLogger(__name__).error(str(exc))
        return 1

    return 1


def _write_json(path: str, payload: dict) -> None:
    out_path = Path(path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
