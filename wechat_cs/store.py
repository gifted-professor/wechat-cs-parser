"""SQLite persistence for the local WeChat CS service."""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Optional
from zoneinfo import ZoneInfo

from .core import DEFAULT_HMAC_SECRET, hmac_id, json_dumps, parse_timestamp
from .source_snapshot import assert_project_output, hmac_key_fingerprint


SCHEMA_VERSION = 4
SHANGHAI = ZoneInfo("Asia/Shanghai")
M0_ACCEPTANCE_GATES = ("m0_a", "m0_b", "m0_c", "m0_d", "integration")


def open_store(path: str, read_only: bool = False) -> sqlite3.Connection:
    db_path = Path(path).expanduser().resolve()
    if read_only:
        connection = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
    else:
        connection = sqlite3.connect(str(db_path))
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 5000")
    if not read_only:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
    return connection


def initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS customers (
            customer_key TEXT PRIMARY KEY,
            display_name TEXT NOT NULL,
            last_active_at TEXT NOT NULL,
            opportunity_score INTEGER NOT NULL CHECK(opportunity_score BETWEEN 0 AND 100),
            opportunity_level TEXT NOT NULL CHECK(opportunity_level IN ('high','medium','low')),
            aftersales_priority TEXT CHECK(aftersales_priority IN ('P0','P1','P2') OR aftersales_priority IS NULL),
            summary TEXT NOT NULL,
            reasons_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            memory_json TEXT NOT NULL,
            source_file TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS messages (
            message_key TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
            role TEXT NOT NULL CHECK(role IN ('studio','customer')),
            timestamp TEXT NOT NULL,
            text TEXT NOT NULL,
            source_file TEXT NOT NULL,
            source_ordinal INTEGER NOT NULL,
            UNIQUE(customer_key, source_file, source_ordinal)
        );
        CREATE INDEX IF NOT EXISTS messages_customer_time
            ON messages(customer_key, timestamp, source_ordinal);

        CREATE TABLE IF NOT EXISTS identity_bindings (
            binding_id TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
            phone_hmac TEXT,
            masked_hint TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN (
                'missing','candidate_unique','review','ambiguous_shared','approved','rejected'
            )),
            evidence_message_keys_json TEXT NOT NULL,
            reviewed_at TEXT,
            UNIQUE(customer_key, phone_hmac)
        );
        CREATE INDEX IF NOT EXISTS identity_bindings_customer_state
            ON identity_bindings(customer_key, state);
        CREATE INDEX IF NOT EXISTS identity_bindings_phone
            ON identity_bindings(phone_hmac) WHERE phone_hmac IS NOT NULL;

        CREATE TABLE IF NOT EXISTS evidence (
            evidence_key TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
            kind TEXT NOT NULL,
            message_keys_json TEXT NOT NULL,
            summary TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS evidence_customer_kind
            ON evidence(customer_key, kind);

        CREATE TABLE IF NOT EXISTS style_pairs (
            pair_id TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
            trigger_text TEXT NOT NULL,
            reply_text TEXT NOT NULL,
            context_json TEXT NOT NULL,
            intent_stage TEXT NOT NULL CHECK(intent_stage IN ('presales','aftersales','general')),
            risk_json TEXT NOT NULL,
            review_status TEXT NOT NULL DEFAULT 'pending',
            review_reasons_json TEXT NOT NULL DEFAULT '[]',
            split TEXT NOT NULL CHECK(split IN ('train','validation','test')),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS style_pairs_review_split
            ON style_pairs(review_status, split);
        CREATE INDEX IF NOT EXISTS style_pairs_customer
            ON style_pairs(customer_key);

        CREATE TABLE IF NOT EXISTS reviews (
            review_id TEXT PRIMARY KEY,
            pair_id TEXT NOT NULL REFERENCES style_pairs(pair_id) ON DELETE CASCADE,
            verdict TEXT NOT NULL CHECK(verdict IN ('approved','rejected','pending')),
            reasons_json TEXT NOT NULL,
            reviewer TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS reviews_pair_created
            ON reviews(pair_id, created_at);

        CREATE TABLE IF NOT EXISTS role_calibration (
            calibration_id TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
            message_key TEXT NOT NULL REFERENCES messages(message_key) ON DELETE CASCADE,
            source_status INTEGER,
            source_role_evidence_json TEXT NOT NULL DEFAULT '{}',
            expected_role TEXT NOT NULL CHECK(expected_role IN ('studio','customer')),
            reviewer_role TEXT CHECK(reviewer_role IN ('studio','customer') OR reviewer_role IS NULL),
            reviewed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS drafts (
            draft_id TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
            request_text TEXT NOT NULL,
            draft_text TEXT NOT NULL,
            intent TEXT NOT NULL,
            needs_clarification INTEGER NOT NULL DEFAULT 0,
            needs_human INTEGER NOT NULL DEFAULT 1,
            risk_json TEXT NOT NULL DEFAULT '[]',
            grounding_refs_json TEXT NOT NULL DEFAULT '[]',
            status TEXT NOT NULL DEFAULT 'generated',
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS drafts_customer_created
            ON drafts(customer_key, created_at);

        CREATE TABLE IF NOT EXISTS feedback (
            feedback_id TEXT PRIMARY KEY,
            draft_id TEXT NOT NULL REFERENCES drafts(draft_id) ON DELETE CASCADE,
            customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
            outcome TEXT NOT NULL CHECK(outcome IN ('accepted','edited','rejected')),
            final_text TEXT,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS feedback_draft_created
            ON feedback(draft_id, created_at);

        CREATE TABLE IF NOT EXISTS build_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE VIEW IF NOT EXISTS customer_insights AS SELECT * FROM customers;
        CREATE VIEW IF NOT EXISTS draft_feedback AS SELECT * FROM feedback;
        CREATE VIEW IF NOT EXISTS system_meta AS SELECT * FROM build_meta;
        """
    )
    _migrate_role_calibration(connection)
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS schema_migrations (
            version INTEGER PRIMARY KEY,
            applied_at TEXT NOT NULL,
            checksum TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS pipeline_runs (
            run_id TEXT PRIMARY KEY,
            state TEXT NOT NULL CHECK(state IN ('running','complete','failed')),
            parser_version TEXT NOT NULL,
            hmac_key_fingerprint TEXT NOT NULL,
            account_config_hash TEXT NOT NULL DEFAULT '',
            order_rule_version TEXT NOT NULL DEFAULT 'm0-order-v1',
            card_rule_version TEXT NOT NULL DEFAULT 'm0-card-v1',
            started_at TEXT NOT NULL,
            completed_at TEXT,
            quality_json TEXT NOT NULL DEFAULT '{}'
        );

        CREATE TABLE IF NOT EXISTS source_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
            source_kind TEXT NOT NULL,
            source_path_hash TEXT NOT NULL,
            device INTEGER NOT NULL,
            inode INTEGER NOT NULL,
            size INTEGER NOT NULL CHECK(size >= 0),
            mtime_ns INTEGER NOT NULL,
            sha256 TEXT NOT NULL,
            record_count INTEGER CHECK(record_count >= 0 OR record_count IS NULL),
            first_at TEXT,
            last_at TEXT,
            observed_until TEXT,
            captured_at TEXT NOT NULL,
            consistency_state TEXT NOT NULL,
            quality_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS source_snapshots_run_kind
            ON source_snapshots(run_id, source_kind);

        CREATE TABLE IF NOT EXISTS profile_observations (
            snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id) ON DELETE CASCADE,
            profile_id TEXT NOT NULL,
            observed_until TEXT,
            initialized INTEGER NOT NULL CHECK(initialized IN (0,1)),
            last_error_code TEXT,
            consistency_state TEXT NOT NULL,
            PRIMARY KEY(snapshot_id, profile_id)
        );

        CREATE TABLE IF NOT EXISTS account_registry (
            profile_id TEXT PRIMARY KEY,
            canonical_account_id TEXT NOT NULL UNIQUE,
            state TEXT NOT NULL CHECK(state IN ('approved','review','rejected')),
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            evidence_json TEXT NOT NULL,
            config_hash TEXT NOT NULL,
            version TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS conversation_refs (
            customer_key TEXT PRIMARY KEY REFERENCES customers(customer_key) ON DELETE CASCADE,
            profile_id TEXT NOT NULL REFERENCES account_registry(profile_id),
            canonical_account_id TEXT NOT NULL,
            raw_wechat_id_hash TEXT NOT NULL,
            source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
            UNIQUE(profile_id, raw_wechat_id_hash)
        );

        CREATE TABLE IF NOT EXISTS conversation_links (
            link_id TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL REFERENCES conversation_refs(customer_key) ON DELETE CASCADE,
            profile_id TEXT NOT NULL,
            raw_wechat_id_hash TEXT NOT NULL,
            phone_hmac TEXT,
            match_method TEXT NOT NULL,
            confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
            state TEXT NOT NULL CHECK(state IN ('approved','review','conflict','rejected')),
            source_hash TEXT NOT NULL,
            version TEXT NOT NULL,
            reviewed_at TEXT
        );
        CREATE INDEX IF NOT EXISTS conversation_links_phone_state
            ON conversation_links(phone_hmac, state) WHERE phone_hmac IS NOT NULL;

        CREATE TABLE IF NOT EXISTS conversation_order_eligibility (
            customer_key TEXT PRIMARY KEY REFERENCES conversation_refs(customer_key) ON DELETE CASCADE,
            eligibility TEXT NOT NULL CHECK(eligibility IN (
                'order_customer','album_customer','order_ineligible'
            )),
            source_hash TEXT NOT NULL,
            version TEXT NOT NULL,
            evaluated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS conversation_order_eligibility_state
            ON conversation_order_eligibility(eligibility);

        CREATE TABLE IF NOT EXISTS order_snapshots (
            order_snapshot_id TEXT PRIMARY KEY,
            source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
            synced_at TEXT NOT NULL,
            record_count INTEGER NOT NULL CHECK(record_count >= 0),
            state TEXT NOT NULL CHECK(state IN ('staging','active','failed','superseded')),
            quality_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE UNIQUE INDEX IF NOT EXISTS one_active_order_snapshot
            ON order_snapshots(state) WHERE state='active';

        CREATE TABLE IF NOT EXISTS orders (
            order_line_id TEXT PRIMARY KEY,
            order_snapshot_id TEXT NOT NULL REFERENCES order_snapshots(order_snapshot_id) ON DELETE CASCADE,
            source_namespace TEXT NOT NULL,
            record_id TEXT NOT NULL,
            phone_hmac TEXT,
            ordered_at TEXT,
            paid_at TEXT,
            paid_on TEXT,
            revenue_minor INTEGER,
            currency TEXT NOT NULL DEFAULT 'CNY',
            platform TEXT,
            sku_name TEXT,
            factory TEXT,
            category TEXT,
            color TEXT,
            size TEXT,
            order_note TEXT,
            refund_type TEXT,
            refund_reason TEXT,
            refund_amount_minor INTEGER,
            refund_on TEXT,
            return_status TEXT,
            source_hash TEXT NOT NULL,
            quality_flags_json TEXT NOT NULL DEFAULT '[]',
            UNIQUE(order_snapshot_id, source_namespace, record_id)
        );
        CREATE INDEX IF NOT EXISTS orders_phone_paid
            ON orders(phone_hmac, paid_on) WHERE phone_hmac IS NOT NULL;

        CREATE TABLE IF NOT EXISTS decision_cards (
            card_id TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
            episode_id TEXT NOT NULL,
            card_type TEXT NOT NULL,
            as_of_at TEXT NOT NULL,
            boundary_ordinal INTEGER NOT NULL,
            boundary_message_key TEXT,
            source_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
            action_window_end TEXT NOT NULL,
            observation_until TEXT,
            blind_context_json TEXT NOT NULL,
            observed_action_json TEXT NOT NULL,
            context_message_keys_json TEXT NOT NULL,
            action_message_keys_json TEXT NOT NULL,
            split TEXT NOT NULL CHECK(split IN ('train','validation','test')),
            review_status TEXT NOT NULL DEFAULT 'pending',
            rule_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS decision_cards_customer_time
            ON decision_cards(customer_key, as_of_at, boundary_ordinal);

        CREATE TABLE IF NOT EXISTS card_outcomes (
            card_id TEXT PRIMARY KEY REFERENCES decision_cards(card_id) ON DELETE CASCADE,
            paid_1d INTEGER CHECK(paid_1d IN (0,1) OR paid_1d IS NULL),
            paid_3d INTEGER CHECK(paid_3d IN (0,1) OR paid_3d IS NULL),
            paid_7d INTEGER CHECK(paid_7d IN (0,1) OR paid_7d IS NULL),
            retained_30d INTEGER CHECK(retained_30d IN (0,1) OR retained_30d IS NULL),
            aftersale_30d INTEGER CHECK(aftersale_30d IN (0,1) OR aftersale_30d IS NULL),
            exchange_30d INTEGER CHECK(exchange_30d IN (0,1) OR exchange_30d IS NULL),
            compensation_30d INTEGER CHECK(compensation_30d IN (0,1) OR compensation_30d IS NULL),
            refund_loss_ratio REAL,
            attribution_state TEXT NOT NULL CHECK(attribution_state IN (
                'none','associated','ambiguous','identity_unverified','quality_unknown'
            )),
            attribution_flags_json TEXT NOT NULL,
            matched_orders_json TEXT NOT NULL,
            computed_at TEXT NOT NULL
        );
        """
    )
    # SQLite's CREATE TABLE IF NOT EXISTS does not add columns to a v2 database.
    # Keep these alterations additive so a reviewed M0 run can be upgraded without
    # recreating, replacing, or silently rewriting its source-derived rows.
    _ensure_column(connection, "orders", "sku_name", "TEXT")
    _ensure_column(connection, "orders", "factory", "TEXT")
    _ensure_column(connection, "orders", "category", "TEXT")
    _ensure_column(connection, "orders", "color", "TEXT")
    _ensure_column(connection, "orders", "size", "TEXT")
    _ensure_column(connection, "orders", "ordered_at", "TEXT")
    _ensure_column(connection, "orders", "paid_at", "TEXT")
    _ensure_column(connection, "orders", "order_note", "TEXT")
    _ensure_column(connection, "decision_cards", "boundary_message_key", "TEXT")
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS customer_value_snapshots (
            feature_snapshot_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
            customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
            profile_id TEXT NOT NULL,
            as_of_at TEXT NOT NULL,
            message_snapshot_id TEXT NOT NULL REFERENCES source_snapshots(snapshot_id),
            order_snapshot_id TEXT REFERENCES order_snapshots(order_snapshot_id)
                ON DELETE SET NULL,
            feature_rule_version TEXT NOT NULL,
            profile_json TEXT NOT NULL,
            freshness_json TEXT NOT NULL,
            messages_fresh INTEGER NOT NULL CHECK(messages_fresh IN (0,1)),
            orders_fresh INTEGER NOT NULL CHECK(orders_fresh IN (0,1)),
            queue_ready INTEGER NOT NULL CHECK(queue_ready IN (0,1)),
            created_at TEXT NOT NULL,
            UNIQUE(run_id, customer_key, as_of_at, feature_rule_version)
        );
        CREATE INDEX IF NOT EXISTS customer_value_snapshots_customer_time
            ON customer_value_snapshots(customer_key, as_of_at);

        CREATE TABLE IF NOT EXISTS card_feature_snapshots (
            card_id TEXT PRIMARY KEY REFERENCES decision_cards(card_id) ON DELETE CASCADE,
            feature_snapshot_id TEXT NOT NULL
                REFERENCES customer_value_snapshots(feature_snapshot_id),
            feature_payload_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS action_annotations (
            card_id TEXT PRIMARY KEY REFERENCES decision_cards(card_id) ON DELETE CASCADE,
            customer_signal TEXT NOT NULL CHECK(customer_signal IN (
                'positive','negative','mixed','unknown'
            )),
            reply_strategy TEXT NOT NULL CHECK(reply_strategy IN (
                'answer_fact','clarify','recommend','quote','trust_proof',
                'light_followup','aftersales_repair','handoff_human','other'
            )),
            reuse_status TEXT NOT NULL CHECK(reuse_status IN (
                'direct','fill_slots','case_only','prohibited'
            )),
            required_facts_json TEXT NOT NULL DEFAULT '[]',
            prohibited_claims_json TEXT NOT NULL DEFAULT '[]',
            annotation_json TEXT NOT NULL DEFAULT '{}',
            rule_version TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS card_annotations (
            annotation_id TEXT PRIMARY KEY,
            card_id TEXT NOT NULL REFERENCES decision_cards(card_id) ON DELETE CASCADE,
            review_stage TEXT NOT NULL CHECK(review_stage IN (
                'protocol_20','acceptance_100','gold_500'
            )),
            reviewer TEXT NOT NULL,
            verdict TEXT NOT NULL CHECK(verdict IN ('approved','edited','rejected')),
            labels_json TEXT NOT NULL DEFAULT '{}',
            notes TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(card_id, review_stage, reviewer)
        );
        CREATE INDEX IF NOT EXISTS card_annotations_stage
            ON card_annotations(review_stage, verdict);

        CREATE TABLE IF NOT EXISTS strategy_catalog (
            strategy_key TEXT PRIMARY KEY,
            strategy_level TEXT NOT NULL CHECK(strategy_level IN ('exact','coarse')),
            parent_strategy_key TEXT REFERENCES strategy_catalog(strategy_key),
            independent_customer_count INTEGER NOT NULL DEFAULT 0
                CHECK(independent_customer_count >= 0),
            statistics_visible INTEGER NOT NULL DEFAULT 0
                CHECK(statistics_visible IN (0,1)),
            metrics_json TEXT NOT NULL DEFAULT '{}',
            version TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            CHECK(statistics_visible = 0 OR independent_customer_count >= 30)
        );

        CREATE TABLE IF NOT EXISTS action_queue_items (
            action_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
            feature_snapshot_id TEXT
                REFERENCES customer_value_snapshots(feature_snapshot_id),
            customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
            profile_id TEXT NOT NULL,
            queue_date TEXT NOT NULL,
            lane TEXT NOT NULL CHECK(lane IN ('reply_now','proactive_today','suppressed')),
            priority_score INTEGER NOT NULL,
            priority_version TEXT NOT NULL,
            phone_hmac TEXT,
            reason_codes_json TEXT NOT NULL,
            contact_window_json TEXT NOT NULL,
            recommended_action TEXT NOT NULL,
            strategy_version TEXT NOT NULL,
            signals_json TEXT NOT NULL DEFAULT '{}',
            required_facts_json TEXT NOT NULL DEFAULT '[]',
            missing_facts_json TEXT NOT NULL DEFAULT '[]',
            prohibited_claims_json TEXT NOT NULL DEFAULT '[]',
            draft_json TEXT NOT NULL DEFAULT '{}',
            confidence REAL CHECK(confidence IS NULL OR (confidence >= 0 AND confidence <= 1)),
            freshness_json TEXT NOT NULL,
            human_confirmation_state TEXT NOT NULL DEFAULT 'pending'
                CHECK(human_confirmation_state IN ('pending','adopted','edited','rejected')),
            send_allowed INTEGER NOT NULL DEFAULT 0 CHECK(send_allowed = 0),
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(profile_id, queue_date, customer_key)
        );
        CREATE INDEX IF NOT EXISTS action_queue_items_profile_date_lane
            ON action_queue_items(profile_id, queue_date, lane, priority_score DESC);
        CREATE UNIQUE INDEX IF NOT EXISTS one_proactive_phone_per_day
            ON action_queue_items(profile_id, queue_date, phone_hmac)
            WHERE lane='proactive_today' AND phone_hmac IS NOT NULL;

        CREATE TABLE IF NOT EXISTS action_queue_runs (
            queue_run_id TEXT PRIMARY KEY,
            run_id TEXT NOT NULL REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
            profile_id TEXT NOT NULL,
            queue_date TEXT NOT NULL,
            as_of_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN ('ready','degraded_order_data','blocked')),
            policy_version TEXT NOT NULL,
            block_reasons_json TEXT NOT NULL DEFAULT '[]',
            freshness_json TEXT NOT NULL,
            counts_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(profile_id, queue_date)
        );
        CREATE INDEX IF NOT EXISTS action_queue_runs_profile_date
            ON action_queue_runs(profile_id, queue_date);

        CREATE TABLE IF NOT EXISTS action_queue_feedback (
            feedback_id TEXT PRIMARY KEY,
            action_id TEXT NOT NULL REFERENCES action_queue_items(action_id) ON DELETE CASCADE,
            outcome TEXT NOT NULL CHECK(outcome IN ('adopted','edited','rejected')),
            final_text TEXT,
            reason_codes_json TEXT NOT NULL DEFAULT '[]',
            reviewer TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS action_queue_feedback_action_time
            ON action_queue_feedback(action_id, created_at);

        CREATE TABLE IF NOT EXISTS contact_suppressions (
            suppression_id TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
            profile_id TEXT NOT NULL,
            phone_hmac TEXT,
            reason_code TEXT NOT NULL,
            starts_at TEXT NOT NULL,
            ends_at TEXT,
            source_action_id TEXT REFERENCES action_queue_items(action_id),
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS contact_suppressions_customer_time
            ON contact_suppressions(customer_key, starts_at, ends_at);
        CREATE INDEX IF NOT EXISTS contact_suppressions_phone_time
            ON contact_suppressions(phone_hmac, starts_at, ends_at)
            WHERE phone_hmac IS NOT NULL;
        """
    )
    _ensure_column(connection, "action_queue_items", "signals_json", "TEXT NOT NULL DEFAULT '{}'")
    _ensure_column(
        connection,
        "action_queue_items",
        "missing_facts_json",
        "TEXT NOT NULL DEFAULT '[]'",
    )
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS orders_phone_paid_at
            ON orders(phone_hmac, paid_at) WHERE phone_hmac IS NOT NULL;

        CREATE TABLE IF NOT EXISTS customer_aux_facts (
            aux_fact_id TEXT PRIMARY KEY,
            source_snapshot_id TEXT NOT NULL
                REFERENCES source_snapshots(snapshot_id) ON DELETE CASCADE,
            source_namespace TEXT NOT NULL,
            source_record_id TEXT NOT NULL,
            customer_key TEXT NOT NULL
                REFERENCES customers(customer_key) ON DELETE CASCADE,
            profile_id TEXT NOT NULL,
            phone_hmac TEXT NOT NULL,
            member_birthday TEXT,
            preferred_style TEXT,
            expected_gift TEXT,
            member_shop TEXT,
            source_hash TEXT NOT NULL,
            quality_flags_json TEXT NOT NULL DEFAULT '[]',
            created_at TEXT NOT NULL,
            UNIQUE(
                source_snapshot_id, source_namespace, source_record_id, customer_key
            )
        );
        CREATE INDEX IF NOT EXISTS customer_aux_facts_customer_snapshot
            ON customer_aux_facts(customer_key, source_snapshot_id);
        CREATE INDEX IF NOT EXISTS customer_aux_facts_phone_snapshot
            ON customer_aux_facts(phone_hmac, source_snapshot_id);

        CREATE TABLE IF NOT EXISTS sales_profile_runs (
            sales_profile_run_id TEXT PRIMARY KEY,
            source_run_id TEXT NOT NULL
                REFERENCES pipeline_runs(run_id) ON DELETE CASCADE,
            as_of_at TEXT NOT NULL,
            status TEXT NOT NULL CHECK(status IN (
                'prepared','running','partial','complete','failed'
            )),
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            profile_schema_version TEXT NOT NULL,
            sampling_version TEXT NOT NULL,
            message_snapshot_id TEXT NOT NULL
                REFERENCES source_snapshots(snapshot_id),
            order_snapshot_id TEXT
                REFERENCES order_snapshots(order_snapshot_id) ON DELETE SET NULL,
            aux_snapshot_id TEXT
                REFERENCES source_snapshots(snapshot_id) ON DELETE SET NULL,
            cohort_hash TEXT NOT NULL,
            config_json TEXT NOT NULL DEFAULT '{}',
            counts_json TEXT NOT NULL DEFAULT '{}',
            quality_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT,
            UNIQUE(source_run_id, as_of_at, sampling_version, cohort_hash)
        );
        CREATE INDEX IF NOT EXISTS sales_profile_runs_status_time
            ON sales_profile_runs(status, created_at DESC);

        CREATE TABLE IF NOT EXISTS sales_profile_subjects (
            subject_id TEXT PRIMARY KEY,
            sales_profile_run_id TEXT NOT NULL
                REFERENCES sales_profile_runs(sales_profile_run_id) ON DELETE CASCADE,
            customer_key TEXT NOT NULL
                REFERENCES customers(customer_key) ON DELETE CASCADE,
            profile_id TEXT NOT NULL,
            phone_hmac TEXT NOT NULL,
            stratum TEXT NOT NULL CHECK(stratum IN (
                'complex_risk','future_return_wait','high_frequency',
                'high_value','dormant_repeat','control'
            )),
            stratum_rank INTEGER NOT NULL CHECK(stratum_rank > 0),
            feature_snapshot_id TEXT
                REFERENCES customer_value_snapshots(feature_snapshot_id),
            feature_payload_json TEXT NOT NULL DEFAULT '{}',
            feature_freshness_json TEXT NOT NULL DEFAULT '{}',
            selection_reason_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'prepared' CHECK(status IN (
                'prepared','running','succeeded','failed'
            )),
            input_hash TEXT,
            idempotency_key TEXT,
            error_code TEXT,
            error_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(sales_profile_run_id, customer_key),
            UNIQUE(sales_profile_run_id, stratum, stratum_rank)
        );
        CREATE INDEX IF NOT EXISTS sales_profile_subjects_run_status
            ON sales_profile_subjects(sales_profile_run_id, status, stratum, stratum_rank);

        CREATE TABLE IF NOT EXISTS sales_profile_events (
            sales_profile_event_id TEXT PRIMARY KEY,
            subject_id TEXT NOT NULL
                REFERENCES sales_profile_subjects(subject_id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL DEFAULT 0 CHECK(chunk_index >= 0),
            event_type TEXT NOT NULL,
            event_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            confidence REAL CHECK(confidence IS NULL OR (
                confidence >= 0 AND confidence <= 1
            )),
            validation_state TEXT NOT NULL CHECK(validation_state IN (
                'accepted','rejected'
            )),
            rejection_reason TEXT,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            input_hash TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sales_profile_events_subject_state
            ON sales_profile_events(subject_id, validation_state, chunk_index);

        CREATE TABLE IF NOT EXISTS sales_profiles (
            sales_profile_id TEXT PRIMARY KEY,
            subject_id TEXT NOT NULL UNIQUE
                REFERENCES sales_profile_subjects(subject_id) ON DELETE CASCADE,
            status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN (
                'pending','running','succeeded','failed'
            )),
            input_hash TEXT,
            idempotency_key TEXT UNIQUE,
            model TEXT NOT NULL,
            prompt_version TEXT NOT NULL,
            profile_schema_version TEXT NOT NULL,
            card_version TEXT,
            deterministic_facts_json TEXT NOT NULL DEFAULT '{}',
            profile_json TEXT NOT NULL DEFAULT '{}',
            evidence_json TEXT NOT NULL DEFAULT '[]',
            error_code TEXT,
            error_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS sales_profiles_status_updated
            ON sales_profiles(status, updated_at DESC);

        CREATE TABLE IF NOT EXISTS sales_profile_reviews (
            review_id TEXT PRIMARY KEY,
            sales_profile_id TEXT NOT NULL
                REFERENCES sales_profiles(sales_profile_id) ON DELETE CASCADE,
            card_version TEXT NOT NULL,
            verdict TEXT NOT NULL CHECK(verdict IN (
                'approved','edited','rejected'
            )),
            fact_accuracy INTEGER NOT NULL CHECK(fact_accuracy BETWEEN 1 AND 5),
            insight_usefulness INTEGER NOT NULL
                CHECK(insight_usefulness BETWEEN 1 AND 5),
            sales_realism INTEGER NOT NULL CHECK(sales_realism BETWEEN 1 AND 5),
            timing_quality INTEGER NOT NULL CHECK(timing_quality BETWEEN 1 AND 5),
            evidence_quality INTEGER NOT NULL CHECK(evidence_quality BETWEEN 1 AND 5),
            corrections_json TEXT NOT NULL DEFAULT '{}',
            notes TEXT NOT NULL DEFAULT '',
            reviewer TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            UNIQUE(sales_profile_id, reviewer)
        );
        CREATE INDEX IF NOT EXISTS sales_profile_reviews_profile_time
            ON sales_profile_reviews(sales_profile_id, updated_at DESC);
        """
    )
    _ensure_column(
        connection,
        "sales_profile_subjects",
        "feature_payload_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    _ensure_column(
        connection,
        "sales_profile_subjects",
        "feature_freshness_json",
        "TEXT NOT NULL DEFAULT '{}'",
    )
    applied_at = datetime.now().astimezone().isoformat(timespec="seconds")
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,applied_at,checksum) VALUES(?,?,?)",
        (2, applied_at, "wechat-cs-m0-schema-v2"),
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,applied_at,checksum) VALUES(?,?,?)",
        (3, applied_at, "wechat-cs-action-queue-schema-v3"),
    )
    connection.execute(
        "INSERT OR IGNORE INTO schema_migrations(version,applied_at,checksum) VALUES(?,?,?)",
        (4, applied_at, "wechat-cs-sales-profile-schema-v4"),
    )
    set_meta(connection, "schema_version", str(SCHEMA_VERSION))
    connection.execute("PRAGMA user_version = %d" % SCHEMA_VERSION)
    connection.commit()


def _ensure_column(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    declaration: str,
) -> None:
    """Add one known schema column without rebuilding or copying user data."""

    columns = {row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)}
    if column not in columns:
        connection.execute(
            "ALTER TABLE %s ADD COLUMN %s %s" % (table, column, declaration)
        )


def _migrate_role_calibration(connection: sqlite3.Connection) -> None:
    columns = {
        row[1]: row for row in connection.execute("PRAGMA table_info(role_calibration)")
    }
    if "source_role_evidence_json" in columns and columns.get("source_status", (None,) * 4)[3] == 0:
        return
    connection.execute("ALTER TABLE role_calibration RENAME TO role_calibration_v1_migration")
    connection.execute(
        """
        CREATE TABLE role_calibration (
            calibration_id TEXT PRIMARY KEY,
            customer_key TEXT NOT NULL REFERENCES customers(customer_key) ON DELETE CASCADE,
            message_key TEXT NOT NULL REFERENCES messages(message_key) ON DELETE CASCADE,
            source_status INTEGER,
            source_role_evidence_json TEXT NOT NULL DEFAULT '{}',
            expected_role TEXT NOT NULL CHECK(expected_role IN ('studio','customer')),
            reviewer_role TEXT CHECK(reviewer_role IN ('studio','customer') OR reviewer_role IS NULL),
            reviewed_at TEXT
        )
        """
    )
    connection.execute(
        """
        INSERT INTO role_calibration(
            calibration_id,customer_key,message_key,source_status,
            source_role_evidence_json,expected_role,reviewer_role,reviewed_at
        )
        SELECT calibration_id,customer_key,message_key,source_status,
               '{"source_kind":"export","evidence_type":"raw_payload.status"}',
               expected_role,reviewer_role,reviewed_at
        FROM role_calibration_v1_migration
        """
    )
    connection.execute("DROP TABLE role_calibration_v1_migration")


def initialize_run(
    connection: sqlite3.Connection,
    *,
    run_id: str,
    secret: str,
    parser_version: str = "0.1.0",
    account_config_hash: str = "",
    order_rule_version: str = "m0-order-v1",
    card_rule_version: str = "m0-card-v1",
) -> Dict[str, Any]:
    """Create or resume a run while enforcing one HMAC key per database."""

    fingerprint = hmac_key_fingerprint(secret)
    existing = {
        row[0]
        for row in connection.execute(
            "SELECT DISTINCT hmac_key_fingerprint FROM pipeline_runs"
        )
    }
    if existing and existing != {fingerprint}:
        raise RuntimeError("HMAC key fingerprint mismatch")
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    connection.execute(
        """
        INSERT INTO pipeline_runs(
            run_id,state,parser_version,hmac_key_fingerprint,account_config_hash,
            order_rule_version,card_rule_version,started_at,completed_at,quality_json
        ) VALUES(?,?,?,?,?,?,?,?,NULL,'{}')
        ON CONFLICT(run_id) DO NOTHING
        """,
        (
            run_id,
            "running",
            parser_version,
            fingerprint,
            account_config_hash,
            order_rule_version,
            card_rule_version,
            now,
        ),
    )
    row = connection.execute(
        "SELECT run_id,state,hmac_key_fingerprint,started_at FROM pipeline_runs WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None or row["hmac_key_fingerprint"] != fingerprint:
        raise RuntimeError("HMAC key fingerprint mismatch")
    connection.commit()
    return dict(row)


def initialize_m0_run(
    *,
    runs_dir: Path,
    secret: str,
    project_root: Path,
    run_id: Optional[str] = None,
) -> Dict[str, Any]:
    if secret == DEFAULT_HMAC_SECRET or len(secret) < 32:
        raise RuntimeError("a non-default HMAC secret of at least 32 characters is required")
    root = Path(project_root).expanduser().resolve()
    runs = Path(runs_dir).expanduser().resolve()
    assert_project_output(runs, root)
    actual_run_id = run_id or "%s-%s" % (
        datetime.now().astimezone().strftime("%Y%m%dT%H%M%S%z"),
        uuid.uuid4().hex[:8],
    )
    run_dir = runs / actual_run_id
    assert_project_output(run_dir, root)
    run_dir.mkdir(parents=True, exist_ok=False)
    db_path = run_dir / "wechat_cs_m0.sqlite3"
    connection = open_store(str(db_path))
    try:
        initialize_schema(connection)
        initialize_run(connection, run_id=actual_run_id, secret=secret)
    finally:
        connection.close()
    return {"run_id": actual_run_id, "db": str(db_path), "state": "running"}


def validate_m0_database(db_path: Path) -> Dict[str, Any]:
    path = Path(db_path).expanduser().resolve()
    connection = open_store(str(path))
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        if integrity != "ok" or foreign_keys:
            raise RuntimeError("M0 database integrity validation failed")
        run = connection.execute(
            "SELECT run_id,quality_json FROM pipeline_runs "
            "ORDER BY started_at DESC,run_id DESC LIMIT 1"
        ).fetchone()
        if run is None:
            raise RuntimeError("M0 database has no pipeline run")
        try:
            quality = json.loads(run["quality_json"] or "{}")
        except json.JSONDecodeError as exc:
            raise RuntimeError("M0 pipeline quality metadata is invalid") from exc
        gates = quality.get("acceptance_gates") if isinstance(quality, dict) else None
        missing = [
            gate
            for gate in M0_ACCEPTANCE_GATES
            if not isinstance(gates, dict) or gates.get(gate) is not True
        ]
        if missing:
            raise RuntimeError(
                "M0 acceptance gates are incomplete: %s" % ",".join(missing)
            )
        completed_at = datetime.now().astimezone().isoformat(timespec="seconds")
        connection.execute(
            "UPDATE pipeline_runs SET state='complete',completed_at=? WHERE run_id=?",
            (completed_at, run["run_id"]),
        )
        connection.commit()
        return {
            "run_id": run["run_id"],
            "state": "complete",
            "integrity_check": integrity,
            "foreign_key_errors": len(foreign_keys),
        }
    finally:
        connection.close()


def publish_m0_database(
    db_path: Path,
    output_path: Path,
    *,
    project_root: Path,
) -> Dict[str, Any]:
    source = Path(db_path).expanduser().resolve()
    output = Path(output_path).expanduser().resolve()
    assert_project_output(source, project_root)
    assert_project_output(output, project_root)
    if source == output:
        raise ValueError("working database and published output must be different")
    connection = open_store(str(source))
    try:
        integrity = str(connection.execute("PRAGMA integrity_check").fetchone()[0])
        foreign_keys = list(connection.execute("PRAGMA foreign_key_check"))
        complete = connection.execute(
            "SELECT COUNT(*) FROM pipeline_runs WHERE state='complete'"
        ).fetchone()[0]
        if integrity != "ok" or foreign_keys or not complete:
            raise RuntimeError("M0 database is not complete and publishable")
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode = DELETE")
    finally:
        connection.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(
        prefix=output.name + ".", suffix=".tmp", dir=str(output.parent)
    )
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        shutil.copyfile(source, temp_path)
        with temp_path.open("rb") as handle:
            os.fsync(handle.fileno())
        os.replace(temp_path, output)
    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise
    return {"output": str(output), "source": str(source), "state": "published"}


def set_meta(connection: sqlite3.Connection, key: str, value: Any) -> None:
    now = datetime.now().isoformat(timespec="seconds")
    serialized = value if isinstance(value, str) else json_dumps(value)
    connection.execute(
        """
        INSERT INTO build_meta(key, value, updated_at) VALUES(?,?,?)
        ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
        """,
        (key, serialized, now),
    )


def get_meta(connection: sqlite3.Connection, key: str, default: Optional[str] = None) -> Optional[str]:
    row = connection.execute("SELECT value FROM build_meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def _count(connection: sqlite3.Connection, table: str, where: str = "", params: Iterable = ()) -> int:
    # Table and where clauses are internal constants, never user input.
    query = "SELECT COUNT(*) AS count FROM %s" % table
    if where:
        query += " WHERE " + where
    return int(connection.execute(query, tuple(params)).fetchone()["count"])


def calibration_summary(connection: sqlite3.Connection) -> Dict[str, Any]:
    total = _count(connection, "role_calibration")
    reviewed = _count(connection, "role_calibration", "reviewer_role IS NOT NULL")
    correct = _count(
        connection,
        "role_calibration",
        "reviewer_role IS NOT NULL AND reviewer_role=expected_role",
    )
    accuracy = (correct / reviewed) if reviewed else None
    passed = bool(total >= 200 and reviewed == total and accuracy is not None and accuracy >= 0.99)
    return {
        "total": total,
        "reviewed": reviewed,
        "correct": correct,
        "accuracy": accuracy,
        "passed": passed,
    }


def get_health(connection: sqlite3.Connection) -> Dict[str, Any]:
    calibration = calibration_summary(connection)
    last_snapshot = get_meta(connection, "snapshot_last_at")
    stale = True
    age_days = None
    if last_snapshot:
        try:
            snapshot_at = parse_timestamp(last_snapshot)
            if snapshot_at.tzinfo is None:
                snapshot_at = snapshot_at.replace(tzinfo=SHANGHAI)
            else:
                snapshot_at = snapshot_at.astimezone(SHANGHAI)
            age_days = max(
                0,
                int((datetime.now(SHANGHAI) - snapshot_at).total_seconds() // 86400),
            )
            stale = age_days > 14
        except (TypeError, ValueError):
            stale = True
    default_secret = get_meta(connection, "uses_default_hmac_secret", "true") == "true"
    weak_secret = get_meta(connection, "weak_hmac_secret", "true") == "true"
    warnings = []
    if default_secret:
        warnings.append("default_hmac_secret")
    if weak_secret:
        warnings.append("weak_hmac_secret")
    if stale:
        warnings.append("snapshot_stale")
    if not calibration["passed"]:
        warnings.append("role_calibration_required")
    binding_states = {
        row["state"]: int(row["count"])
        for row in connection.execute(
            "SELECT state,COUNT(*) AS count FROM identity_bindings GROUP BY state"
        )
    }
    return {
        "status": "degraded" if warnings else "ok",
        "schema_version": int(get_meta(connection, "schema_version", "0") or 0),
        "built_at": get_meta(connection, "built_at"),
        "snapshot_last_at": last_snapshot,
        "snapshot_age_days": age_days,
        "snapshot_stale": stale,
        "uses_default_hmac_secret": default_secret,
        "weak_hmac_secret": weak_secret,
        "role_calibration": calibration,
        "counts": {
            "customers": _count(connection, "customers"),
            "messages": _count(connection, "messages"),
            "style_pairs": _count(connection, "style_pairs"),
            "approved_style_pairs": _count(
                connection, "style_pairs", "review_status='approved'"
            ),
            "open_aftersales": _count(
                connection, "customers", "aftersales_priority IS NOT NULL"
            ),
            "drafts": _count(connection, "drafts"),
            "feedback": _count(connection, "feedback"),
            "identity_binding_customers_with_candidates": int(
                connection.execute(
                    "SELECT COUNT(DISTINCT customer_key) AS count FROM identity_bindings WHERE state!='missing'"
                ).fetchone()["count"]
            ),
        },
        "identity_binding_states": binding_states,
        "warnings": warnings,
    }


def record_feedback(
    connection: sqlite3.Connection,
    draft_id: str,
    customer_key: str,
    outcome: str,
    final_text: Optional[str] = None,
    secret: Optional[str] = None,
) -> str:
    if outcome not in ("accepted", "edited", "rejected"):
        raise ValueError("outcome must be accepted, edited, or rejected")
    draft = connection.execute(
        "SELECT customer_key FROM drafts WHERE draft_id=?", (draft_id,)
    ).fetchone()
    if draft is None:
        raise ValueError("draft does not exist")
    if draft["customer_key"] != customer_key:
        raise ValueError("draft does not belong to customer")
    actual_secret = secret or os.environ.get("WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET)
    created_at = datetime.now().isoformat(timespec="seconds")
    feedback_id = hmac_id(
        actual_secret, "feedback", draft_id, outcome, created_at, final_text or ""
    )
    connection.execute(
        """INSERT INTO feedback(
            feedback_id,draft_id,customer_key,outcome,final_text,created_at
        ) VALUES(?,?,?,?,?,?)""",
        (feedback_id, draft_id, customer_key, outcome, final_text, created_at),
    )
    connection.commit()
    return feedback_id
