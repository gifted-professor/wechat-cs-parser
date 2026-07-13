"""Command-line interface for building and inspecting the local service."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from .action_pipeline import build_action_artifacts
from .build import (
    apply_role_calibration_csv,
    build_database,
    build_live_inbox_database,
    export_chatml,
)
from .core import DEFAULT_HMAC_SECRET
from .identity import import_bindings, import_feishu_order_bindings
from .member_facts import import_member_facts
from .orders import import_orders
from .review_stages import (
    STAGE_TARGETS,
    get_review_status,
    import_review_annotations,
    prepare_review_batch,
)
from .sales_profile_generation import run_sales_profile_pilot
from .sales_profile_pilot import (
    DEFAULT_AS_OF_AT as DEFAULT_SALES_PROFILE_AS_OF_AT,
    DEFAULT_MODEL as DEFAULT_SALES_PROFILE_MODEL,
    DEFAULT_SOURCE_RUN_ID as DEFAULT_SALES_PROFILE_SOURCE_RUN_ID,
    prepare_sales_profile_pilot,
)
from .store import (
    get_health,
    initialize_m0_run,
    open_store,
    publish_m0_database,
    validate_m0_database,
)


def _print(value: object) -> None:
    # Results contain counts and opaque IDs only; chat content is never logged.
    print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def make_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m wechat_cs")
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build", help="build a local SQLite analysis database")
    build.add_argument("--input", default=".", help="plaintext export root")
    build.add_argument(
        "--db", default=".wechat-cs/data/wechat_cs.sqlite3", help="output SQLite path"
    )
    build.add_argument("--limit-pairs", type=int, default=500)
    build.add_argument("--account-id", help="non-PII account namespace")
    build.add_argument(
        "--input-format",
        choices=("auto", "export", "live-inbox"),
        default="auto",
    )
    build.add_argument(
        "--accounts-config",
        help="local live-inbox account mapping under .wechat-cs/config",
    )
    build.add_argument(
        "--state",
        help="live-inbox state.json (defaults to the events.jsonl directory)",
    )

    status = subparsers.add_parser("status", help="show safe health and aggregate counts")
    status.add_argument("--db", default=".wechat-cs/data/wechat_cs.sqlite3")

    export = subparsers.add_parser("export-chatml", help="export reviewed redacted ChatML JSONL")
    export.add_argument("--db", default=".wechat-cs/data/wechat_cs.sqlite3")
    export.add_argument("--output", required=True)
    export.add_argument("--split", choices=("train", "validation", "test", "all"), default="all")
    export.add_argument("--include-pending", action="store_true", help="testing only")
    export.add_argument("--allow-unverified-roles", action="store_true", help="testing only")
    export.add_argument(
        "--include-risky",
        action="store_true",
        help="include medium/high/critical examples for explicit research only",
    )

    calibrate = subparsers.add_parser(
        "apply-role-calibration", help="apply human reviewer roles from a CSV"
    )
    calibrate.add_argument("--db", default=".wechat-cs/data/wechat_cs.sqlite3")
    calibrate.add_argument("--csv", required=True)

    init_m0 = subparsers.add_parser(
        "init-m0-run", help="create an isolated M0 working database"
    )
    init_m0.add_argument("--runs-dir", default=".wechat-cs/runs")
    init_m0.add_argument("--run-id", help="explicit run ID for deterministic tests")

    validate_m0 = subparsers.add_parser(
        "validate-m0", help="validate and complete an M0 working database"
    )
    validate_m0.add_argument("--db", required=True)

    publish_m0 = subparsers.add_parser(
        "publish-m0", help="atomically publish a completed M0 database"
    )
    publish_m0.add_argument("--db", required=True)
    publish_m0.add_argument(
        "--output", default=".wechat-cs/data/wechat_cs_m0.sqlite3"
    )

    bindings = subparsers.add_parser(
        "import-bindings", help="import deterministic account-scoped phone bindings"
    )
    bindings.add_argument("--db", required=True)
    bindings.add_argument("--bindings", required=True)
    bindings.add_argument("--accounts-config", required=True)

    feishu_bindings = subparsers.add_parser(
        "import-feishu-bindings",
        help="evaluate order eligibility and import deterministic Feishu-backed links",
    )
    feishu_bindings.add_argument("--db", required=True)
    feishu_bindings.add_argument("--events", required=True)
    feishu_bindings.add_argument("--accounts-config", required=True)
    feishu_bindings.add_argument(
        "--orders", action="append", required=True, help="read-only dashboard order cache; repeatable"
    )
    feishu_bindings.add_argument("--target-profile", default="aolai4")

    orders = subparsers.add_parser(
        "import-orders", help="import canonical customer payment and refund facts"
    )
    orders.add_argument("--db", required=True)
    orders.add_argument("--orders", required=True)

    member_facts = subparsers.add_parser(
        "import-member-facts",
        help="import versioned birthday and preference facts from a read-only member cache",
    )
    member_facts.add_argument("--db", required=True)
    member_facts.add_argument("--members", required=True)

    prepare_sales_profile = subparsers.add_parser(
        "prepare-sales-profile-pilot",
        help="freeze the deterministic 50-person review cohort without calling Kimi",
    )
    prepare_sales_profile.add_argument("--db", required=True)
    prepare_sales_profile.add_argument(
        "--as-of",
        default=DEFAULT_SALES_PROFILE_AS_OF_AT,
        help="timezone-aware historical cutoff",
    )
    prepare_sales_profile.add_argument(
        "--source-run",
        default=DEFAULT_SALES_PROFILE_SOURCE_RUN_ID,
        help="fixed normalized source run",
    )
    prepare_sales_profile.add_argument(
        "--model", default=DEFAULT_SALES_PROFILE_MODEL
    )

    run_sales_profile = subparsers.add_parser(
        "run-sales-profile-pilot",
        help="generate Kimi profiles for a previously frozen review cohort",
    )
    run_sales_profile.add_argument("--db", required=True)
    run_sales_profile.add_argument("--events", required=True)
    run_sales_profile.add_argument("--accounts-config", required=True)
    run_sales_profile.add_argument("--run-id", default="latest")
    run_sales_profile.add_argument("--resume", action="store_true")
    run_sales_profile.add_argument("--concurrency", type=int, default=2)

    action_queue = subparsers.add_parser(
        "build-action-queue",
        help="build review-only point-in-time cards, outcomes, and action queue",
    )
    action_queue.add_argument("--db", required=True)
    action_queue.add_argument(
        "--as-of",
        required=True,
        help="timezone-aware decision cutoff, for example 2026-07-13T12:00:00+08:00",
    )
    action_queue.add_argument(
        "--collector-status",
        required=True,
        help="current message collector status; only 'running' can open the queue",
    )
    action_queue.add_argument("--profile", help="optional local profile such as aolai1")

    review_batch = subparsers.add_parser(
        "review-batch",
        help="prepare one deterministic outcome-blind local human review batch",
    )
    review_batch.add_argument("--db", required=True)
    review_batch.add_argument("--stage", required=True, choices=tuple(STAGE_TARGETS))

    review_status = subparsers.add_parser(
        "review-status", help="show aggregate Plan 7 human review stage progress"
    )
    review_status.add_argument("--db", required=True)

    review_annotate = subparsers.add_parser(
        "review-annotate", help="import redacted local human review annotations"
    )
    review_annotate.add_argument("--db", required=True)
    review_annotate.add_argument("--stage", required=True, choices=tuple(STAGE_TARGETS))
    review_annotate.add_argument("--reviewer", required=True)
    review_annotate.add_argument("--input", required=True, help="local JSON annotation file")
    return parser


def main(argv=None) -> int:
    parser = make_parser()
    args = parser.parse_args(argv)
    try:
        if args.command == "build":
            input_path = Path(args.input).expanduser()
            input_format = args.input_format
            if input_format == "auto":
                if input_path.is_file() and input_path.name == "events.jsonl":
                    input_format = "live-inbox"
                elif (
                    input_path.is_dir()
                    and (input_path / "conversation_index.json").is_file()
                    and (input_path / "messages.jsonl").is_file()
                ):
                    input_format = "export"
                else:
                    raise ValueError("unable to detect input format")
            if input_format == "live-inbox":
                if not args.accounts_config:
                    raise ValueError("--accounts-config is required for live-inbox")
                state_path = Path(args.state) if args.state else input_path.parent / "state.json"
                _print(
                    build_live_inbox_database(
                        events_path=input_path,
                        state_path=state_path,
                        accounts_path=Path(args.accounts_config),
                        db_path=Path(args.db),
                    )
                )
            else:
                _print(
                    build_database(
                        export_root=args.input,
                        db_path=args.db,
                        limit_pairs=args.limit_pairs,
                        account_id=args.account_id,
                    )
                )
        elif args.command == "status":
            connection = open_store(args.db, read_only=True)
            try:
                _print(get_health(connection))
            finally:
                connection.close()
        elif args.command == "export-chatml":
            _print(
                export_chatml(
                    db_path=args.db,
                    output_path=args.output,
                    split=args.split,
                    include_pending=args.include_pending,
                    allow_unverified_roles=args.allow_unverified_roles,
                    include_risky=args.include_risky,
                )
            )
        elif args.command == "apply-role-calibration":
            _print(apply_role_calibration_csv(args.db, args.csv))
        elif args.command == "init-m0-run":
            _print(
                initialize_m0_run(
                    runs_dir=Path(args.runs_dir),
                    secret=os.environ.get("WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET),
                    project_root=Path.cwd(),
                    run_id=args.run_id,
                )
            )
        elif args.command == "validate-m0":
            _print(validate_m0_database(Path(args.db)))
        elif args.command == "publish-m0":
            _print(
                publish_m0_database(
                    Path(args.db), Path(args.output), project_root=Path.cwd()
                )
            )
        elif args.command == "import-bindings":
            _print(
                import_bindings(
                    db_path=Path(args.db),
                    bindings_path=Path(args.bindings),
                    accounts_path=Path(args.accounts_config),
                )
            )
        elif args.command == "import-feishu-bindings":
            _print(
                import_feishu_order_bindings(
                    db_path=Path(args.db),
                    events_path=Path(args.events),
                    accounts_path=Path(args.accounts_config),
                    order_paths=[Path(value) for value in args.orders],
                    target_profile_id=args.target_profile,
                )
            )
        elif args.command == "import-orders":
            _print(import_orders(db_path=Path(args.db), orders_path=Path(args.orders)))
        elif args.command == "import-member-facts":
            _print(
                import_member_facts(
                    db_path=Path(args.db),
                    source_path=Path(args.members),
                    secret=os.environ.get(
                        "WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET
                    ),
                )
            )
        elif args.command == "prepare-sales-profile-pilot":
            _print(
                prepare_sales_profile_pilot(
                    Path(args.db),
                    as_of_at=args.as_of,
                    source_run_id=args.source_run,
                    model=args.model,
                    secret=os.environ.get(
                        "WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET
                    ),
                )
            )
        elif args.command == "run-sales-profile-pilot":
            _print(
                run_sales_profile_pilot(
                    Path(args.db),
                    events_path=Path(args.events),
                    accounts_path=Path(args.accounts_config),
                    sales_profile_run_id=args.run_id,
                    resume=args.resume,
                    concurrency=args.concurrency,
                    secret=os.environ.get(
                        "WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET
                    ),
                )
            )
        elif args.command == "build-action-queue":
            _print(
                build_action_artifacts(
                    Path(args.db),
                    as_of_at=args.as_of,
                    collector_status=args.collector_status,
                    profile_id=args.profile,
                    secret=os.environ.get("WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET),
                )
            )
        elif args.command == "review-batch":
            _print(prepare_review_batch(Path(args.db), args.stage))
        elif args.command == "review-status":
            _print(get_review_status(Path(args.db)))
        elif args.command == "review-annotate":
            try:
                annotation_payload = json.loads(
                    Path(args.input).expanduser().read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise ValueError("unable to read annotation JSON") from exc
            if isinstance(annotation_payload, dict):
                annotation_payload = annotation_payload.get("items")
            if not isinstance(annotation_payload, list):
                raise ValueError("annotation JSON must be a list or contain an items list")
            _print(
                import_review_annotations(
                    Path(args.db),
                    stage=args.stage,
                    reviewer=args.reviewer,
                    annotations=annotation_payload,
                )
            )
        else:
            parser.error("unknown command")
        return 0
    except Exception as exc:
        # Never include row content or secrets in errors.  Our own exceptions use
        # only paths, ordinals, and aggregate gate explanations.
        print("error: %s" % str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
