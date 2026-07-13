"""Build the local SQLite analysis database from a plaintext export."""

from __future__ import annotations

import csv
import json
import os
import re
import sqlite3
import tempfile
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from .core import (
    DEFAULT_HMAC_SECRET,
    Message,
    PairCandidate,
    analyze_customer,
    content_digest,
    extract_mainland_phones,
    hmac_id,
    json_dumps,
    merge_turns,
    pair_turns,
    parse_timestamp,
    select_candidates,
    stable_split,
)
from .live_inbox import SourceSnapshot, load_live_inbox
from .source_snapshot import hmac_key_fingerprint
from .store import calibration_summary, get_health, initialize_schema, open_store, set_meta


def _load_conversation_index(export_root: Path) -> Dict[str, Dict[str, Any]]:
    path = export_root / "conversation_index.json"
    rows = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise ValueError("conversation_index.json must contain a list")
    return {
        str(row.get("conversation_id")): row
        for row in rows
        if row.get("conversation_type") == "friend" and row.get("conversation_id")
    }


def _source_file(index_row: Dict[str, Any]) -> str:
    value = str(index_row.get("file") or "")
    path = Path(value)
    if path.is_absolute():
        return "conversations/%s" % path.name
    return value or "messages.jsonl"


def _safe_display_name(index_row: Dict[str, Any], customer_key: str) -> str:
    """Keep a useful local label without persisting account identifiers."""

    for field in ("remark", "display_name", "nick_name"):
        value = str(index_row.get(field) or "")
        value = "".join(character for character in value if character >= " " and character != "\x7f")
        value = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[手机号]", value)
        value = re.sub(r"(?<!\d)\d{6,}(?!\d)", "[编号]", value)
        value = re.sub(
            r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "[邮箱]", value
        )
        value = re.sub(r"https?://\S+", "[链接]", value, flags=re.IGNORECASE)
        value = re.sub(r"\s+", " ", value).strip(" \t\r\n,，;；")
        if value and value not in ("[手机号]", "[邮箱]", "[链接]"):
            return value[:80]
    return "客户-%s" % customer_key[-8:]


def _known_name_tokens(index_row: Dict[str, Any]) -> List[str]:
    tokens = []
    for field in ("remark", "display_name", "nick_name"):
        value = "".join(
            character
            for character in str(index_row.get(field) or "").strip()
            if character >= " " and character != "\x7f"
        )
        if 2 <= len(value) <= 40 and not value.isdigit():
            tokens.append(value)
    return sorted(set(tokens), key=lambda value: (-len(value), value))


def _scrub_known_names(value: Any, tokens: Sequence[str]) -> Any:
    """Remove contact labels from derived/training text, never raw messages."""

    if isinstance(value, str):
        for token in tokens:
            value = value.replace(token, "[姓名]")
        return value
    if isinstance(value, list):
        return [_scrub_known_names(item, tokens) for item in value]
    if isinstance(value, dict):
        return {key: _scrub_known_names(item, tokens) for key, item in value.items()}
    return value


def _export_snapshot_window(
    export_root: Path,
    fallback_first: Optional[datetime],
    fallback_last: Optional[datetime],
) -> Tuple[Optional[datetime], Optional[datetime]]:
    """Read only safe aggregate timestamps from summary.json."""

    summary_path = export_root / "summary.json"
    if not summary_path.is_file():
        return fallback_first, fallback_last
    try:
        summary = json.loads(summary_path.read_text(encoding="utf-8"))
        first = parse_timestamp(str(summary.get("first_timestamp") or ""))
        last = parse_timestamp(str(summary.get("last_timestamp") or ""))
        return first, last
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return fallback_first, fallback_last


def load_messages(
    export_root: Path, secret: str, account_id: str
) -> Tuple[Dict[str, List[Message]], Dict[str, int], Optional[datetime], Optional[datetime]]:
    """Read only friend/plain-text/status-2-or-3 rows from messages.jsonl."""

    index = _load_conversation_index(export_root)
    messages_path = export_root / "messages.jsonl"
    by_customer: Dict[str, List[Message]] = defaultdict(list)
    status_by_key: Dict[str, int] = {}
    first_at: Optional[datetime] = None
    last_at: Optional[datetime] = None
    seen_keys = set()
    with messages_path.open("r", encoding="utf-8") as handle:
        for source_ordinal, line in enumerate(handle, 1):
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError("invalid JSONL at source ordinal %d" % source_ordinal) from exc
            conversation_id = str(row.get("conversation_id") or "")
            index_row = index.get(conversation_id)
            if not index_row or row.get("conversation_type") != "friend":
                continue
            # render_type=text includes links and quotes.  message_type=text is
            # the strict, ordinary plaintext subset required by V1.
            if row.get("message_type") != "text":
                continue
            status = (row.get("raw_payload") or {}).get("status")
            if status not in (2, 3):
                continue
            text = str(row.get("text") or "").replace("\x00", "").strip()
            if not text:
                continue
            timestamp = str(row.get("timestamp") or "")
            try:
                at = parse_timestamp(timestamp)
            except (TypeError, ValueError) as exc:
                raise ValueError("invalid timestamp at source ordinal %d" % source_ordinal) from exc
            customer_key = hmac_id(secret, "customer", account_id, conversation_id)
            role = "studio" if status == 2 else "customer"
            source_file = _source_file(index_row)
            message_key = hmac_id(
                secret,
                "message",
                account_id,
                customer_key,
                timestamp,
                source_ordinal,
                status,
                content_digest(text),
            )
            if message_key in seen_keys:
                continue
            seen_keys.add(message_key)
            message = Message(
                message_key=message_key,
                customer_key=customer_key,
                role=role,
                timestamp=timestamp,
                text=text,
                source_file=source_file,
                source_ordinal=source_ordinal,
            )
            by_customer[customer_key].append(message)
            status_by_key[message_key] = status
            first_at = at if first_at is None or at < first_at else first_at
            last_at = at if last_at is None or at > last_at else last_at
    return by_customer, status_by_key, first_at, last_at


def build_live_inbox_database(
    events_path: Path,
    state_path: Path,
    accounts_path: Path,
    db_path: Path,
    *,
    secret: Optional[str] = None,
) -> Dict[str, Any]:
    """Load one stable live-inbox snapshot into an isolated M0 working DB."""

    actual_secret = secret or os.environ.get("WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET)
    snapshot: SourceSnapshot = load_live_inbox(
        Path(events_path),
        Path(accounts_path),
        secret=actual_secret,
        state_path=Path(state_path),
    )
    output = Path(db_path).expanduser().resolve()
    if not output.is_file():
        raise FileNotFoundError("live-inbox build requires an initialized M0 working database")
    connection = open_store(str(output))
    try:
        initialize_schema(connection)
        run = connection.execute(
            "SELECT * FROM pipeline_runs ORDER BY started_at DESC,run_id DESC LIMIT 1"
        ).fetchone()
        if run is None:
            raise RuntimeError("live-inbox build requires an initialized pipeline run")
        if run["hmac_key_fingerprint"] != hmac_key_fingerprint(actual_secret):
            raise RuntimeError("HMAC key fingerprint mismatch")
        if run["account_config_hash"] and run["account_config_hash"] != snapshot.account_config_hash:
            raise RuntimeError("account config hash mismatch")
        run_id = str(run["run_id"])
        captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
        event_snapshot_id = hmac_id(
            actual_secret,
            "snapshot",
            "live-inbox-events",
            snapshot.event_source_hash,
            snapshot.state_source_hash,
            snapshot.account_config_hash,
        )
        state_snapshot_id = hmac_id(
            actual_secret,
            "snapshot",
            "live-inbox-state",
            snapshot.state_source_hash,
            snapshot.account_config_hash,
        )
        observed_values = [
            value for value in snapshot.observed_until_by_profile.values() if value is not None
        ]
        conservative_observed_until = min(observed_values).isoformat(timespec="seconds") if observed_values else None

        prior_calibration = {
            row["message_key"]: dict(row)
            for row in connection.execute(
                "SELECT message_key,reviewer_role,reviewed_at FROM role_calibration "
                "WHERE reviewer_role IS NOT NULL"
            )
        }
        prior_links = [dict(row) for row in connection.execute("SELECT * FROM conversation_links")]
        with connection:
            connection.execute(
                "UPDATE pipeline_runs SET account_config_hash=?,state='running',completed_at=NULL WHERE run_id=?",
                (snapshot.account_config_hash, run_id),
            )
            for stable, snapshot_id, source_kind, record_count, first_at, last_at, observed in (
                (
                    snapshot.event_file,
                    event_snapshot_id,
                    "live-inbox-events",
                    snapshot.event_record_count,
                    snapshot.first_at,
                    snapshot.last_at,
                    conservative_observed_until,
                ),
                (
                    snapshot.state_file,
                    state_snapshot_id,
                    "live-inbox-state",
                    1,
                    None,
                    None,
                    conservative_observed_until,
                ),
            ):
                connection.execute(
                    """
                    INSERT INTO source_snapshots(
                        snapshot_id,run_id,source_kind,source_path_hash,device,inode,size,
                        mtime_ns,sha256,record_count,first_at,last_at,observed_until,
                        captured_at,consistency_state,quality_json
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(snapshot_id) DO UPDATE SET
                        device=excluded.device,inode=excluded.inode,size=excluded.size,
                        mtime_ns=excluded.mtime_ns,record_count=excluded.record_count,
                        first_at=excluded.first_at,last_at=excluded.last_at,
                        observed_until=excluded.observed_until,captured_at=excluded.captured_at,
                        consistency_state=excluded.consistency_state,quality_json=excluded.quality_json
                    """,
                    (
                        snapshot_id,
                        run_id,
                        source_kind,
                        hmac_id(actual_secret, "source-path", str(stable.path)),
                        stable.device,
                        stable.inode,
                        stable.size,
                        stable.mtime_ns,
                        stable.sha256,
                        record_count,
                        first_at.isoformat(timespec="seconds") if first_at else None,
                        last_at.isoformat(timespec="seconds") if last_at else None,
                        observed,
                        captured_at,
                        snapshot.consistency_state,
                        json_dumps({"quarantine_counts": snapshot.quarantine_counts}),
                    ),
                )

            for profile_id, account in snapshot.accounts.items():
                confidence = 1.0 if account.state == "approved" else 0.5
                connection.execute(
                    """
                    INSERT INTO account_registry(
                        profile_id,canonical_account_id,state,confidence,evidence_json,
                        config_hash,version
                    ) VALUES(?,?,?,?,?,?,?)
                    ON CONFLICT(profile_id) DO UPDATE SET
                        canonical_account_id=excluded.canonical_account_id,
                        state=excluded.state,confidence=excluded.confidence,
                        evidence_json=excluded.evidence_json,config_hash=excluded.config_hash,
                        version=excluded.version
                    """,
                    (
                        profile_id,
                        account.canonical_account_id,
                        account.state,
                        confidence,
                        json_dumps({"source_kind": "local_accounts_config"}),
                        snapshot.account_config_hash,
                        "accounts-v1",
                    ),
                )
                boundary = snapshot.observed_until_by_profile.get(profile_id)
                connection.execute(
                    """
                    INSERT INTO profile_observations(
                        snapshot_id,profile_id,observed_until,initialized,last_error_code,
                        consistency_state
                    ) VALUES(?,?,?,?,?,?)
                    ON CONFLICT(snapshot_id,profile_id) DO UPDATE SET
                        observed_until=excluded.observed_until,initialized=excluded.initialized,
                        last_error_code=excluded.last_error_code,
                        consistency_state=excluded.consistency_state
                    """,
                    (
                        event_snapshot_id,
                        profile_id,
                        boundary.isoformat(timespec="seconds") if boundary else None,
                        1 if boundary else 0,
                        None if boundary else "observation_unavailable",
                        "consistent" if boundary else "degraded",
                    ),
                )

            connection.execute("DELETE FROM card_outcomes")
            connection.execute("DELETE FROM decision_cards")
            connection.execute("DELETE FROM conversation_links")
            connection.execute("DELETE FROM conversation_refs")
            connection.execute("DELETE FROM role_calibration")
            connection.execute("DELETE FROM messages")
            current_customers = sorted(snapshot.messages_by_customer)
            if current_customers:
                placeholders = ",".join("?" for _ in current_customers)
                connection.execute(
                    "DELETE FROM customers WHERE customer_key NOT IN (%s)" % placeholders,
                    current_customers,
                )
            else:
                connection.execute("DELETE FROM customers")

            for customer_key in current_customers:
                customer_messages = snapshot.messages_by_customer[customer_key]
                last_active_at = max(item.timestamp for item in customer_messages)
                connection.execute(
                    """
                    INSERT INTO customers(
                        customer_key,display_name,last_active_at,opportunity_score,
                        opportunity_level,aftersales_priority,summary,reasons_json,
                        evidence_json,memory_json,source_file
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(customer_key) DO UPDATE SET
                        last_active_at=excluded.last_active_at,source_file=excluded.source_file
                    """,
                    (
                        customer_key,
                        "客户-%s" % customer_key[-8:],
                        last_active_at,
                        0,
                        "low",
                        None,
                        "live-inbox M0 customer",
                        "[]",
                        "[]",
                        "{}",
                        snapshot.event_file.path.name,
                    ),
                )
                connection.executemany(
                    """
                    INSERT INTO messages(
                        message_key,customer_key,role,timestamp,text,source_file,source_ordinal
                    ) VALUES(?,?,?,?,?,?,?)
                    """,
                    [
                        (
                            item.message_key,
                            item.customer_key,
                            item.role,
                            item.timestamp,
                            item.text,
                            item.source_file,
                            item.source_ordinal,
                        )
                        for item in customer_messages
                    ],
                )
                reference = snapshot.conversations[customer_key]
                connection.execute(
                    """
                    INSERT INTO conversation_refs(
                        customer_key,profile_id,canonical_account_id,raw_wechat_id_hash,
                        source_snapshot_id
                    ) VALUES(?,?,?,?,?)
                    """,
                    (
                        customer_key,
                        reference.profile_id,
                        reference.canonical_account_id,
                        reference.raw_wechat_id_hash,
                        event_snapshot_id,
                    ),
                )

            sample = _role_calibration_sample(snapshot.messages_by_customer, 200)
            for message in sample:
                evidence = snapshot.role_evidence[message.message_key]
                previous = prior_calibration.get(message.message_key, {})
                connection.execute(
                    """
                    INSERT INTO role_calibration(
                        calibration_id,customer_key,message_key,source_status,
                        source_role_evidence_json,expected_role,reviewer_role,reviewed_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    """,
                    (
                        hmac_id(actual_secret, "calibration", message.message_key),
                        message.customer_key,
                        message.message_key,
                        None,
                        json_dumps(
                            {
                                "source_kind": evidence.source_kind,
                                "profile_id": evidence.profile_id,
                                "evidence_type": evidence.evidence_type,
                                "evidence_value": evidence.evidence_value,
                            }
                        ),
                        message.role,
                        previous.get("reviewer_role"),
                        previous.get("reviewed_at"),
                    ),
                )

            valid_customers = set(current_customers)
            for row in prior_links:
                if row.get("customer_key") not in valid_customers:
                    continue
                columns = [
                    "link_id",
                    "customer_key",
                    "profile_id",
                    "raw_wechat_id_hash",
                    "phone_hmac",
                    "match_method",
                    "confidence",
                    "state",
                    "source_hash",
                    "version",
                    "reviewed_at",
                ]
                connection.execute(
                    "INSERT OR IGNORE INTO conversation_links(%s) VALUES(%s)"
                    % (",".join(columns), ",".join("?" for _ in columns)),
                    tuple(row.get(column) for column in columns),
                )
            set_meta(connection, "snapshot_first_at", snapshot.first_at.isoformat() if snapshot.first_at else "")
            set_meta(connection, "snapshot_last_at", snapshot.last_at.isoformat() if snapshot.last_at else "")
            set_meta(connection, "source_friend_text_messages", len(snapshot.messages))
            set_meta(connection, "source_friend_text_customers", len(snapshot.messages_by_customer))
            set_meta(connection, "role_mapping", {"empty_sender": "customer", "configured_self_sender": "studio"})
        return {
            "snapshot_id": event_snapshot_id,
            "state_snapshot_id": state_snapshot_id,
            "accepted_messages": len(snapshot.messages),
            "conversations": len(snapshot.conversations),
            "calibration_samples": min(200, len(snapshot.messages)),
            "quarantine_counts": snapshot.quarantine_counts,
            "consistency_state": snapshot.consistency_state,
        }
    finally:
        connection.close()


def _role_calibration_sample(
    by_customer: Dict[str, List[Message]], sample_size: int = 200
) -> List[Message]:
    """Sample across months, roles and customers without exposing text in logs."""

    buckets: Dict[Tuple[str, str], Dict[str, List[Message]]] = defaultdict(
        lambda: defaultdict(list)
    )
    for customer_key, messages in by_customer.items():
        for message in messages:
            month = message.timestamp[:7]
            buckets[(month, message.role)][customer_key].append(message)
    for customers in buckets.values():
        for items in customers.values():
            items.sort(key=lambda item: (item.timestamp, item.source_ordinal))
    # Flatten every month/role bucket by interleaving its customers first.
    bucket_sequences: Dict[Tuple[str, str], List[Message]] = {}
    for bucket_key, customers in buckets.items():
        sequence: List[Message] = []
        offset = 0
        while True:
            added = False
            for customer_key in sorted(customers):
                items = customers[customer_key]
                if offset < len(items):
                    sequence.append(items[offset])
                    added = True
            if not added:
                break
            offset += 1
        bucket_sequences[bucket_key] = sequence

    selected: List[Message] = []
    offset = 0
    bucket_keys = sorted(bucket_sequences)
    while len(selected) < sample_size:
        added = False
        for bucket_key in bucket_keys:
            sequence = bucket_sequences[bucket_key]
            if offset < len(sequence):
                selected.append(sequence[offset])
                added = True
                if len(selected) >= sample_size:
                    break
        if not added:
            break
        offset += 1
    return selected


def _snapshot_existing(db_path: Path) -> Dict[str, List[Dict[str, Any]]]:
    """Keep human decisions and draft feedback across deterministic rebuilds."""

    if not db_path.exists():
        return {}
    connection = None
    output: Dict[str, List[Dict[str, Any]]] = {}
    try:
        connection = open_store(str(db_path), read_only=True)
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        for table in (
            "style_pairs",
            "reviews",
            "role_calibration",
            "identity_bindings",
            "drafts",
            "feedback",
            "pipeline_runs",
            "source_snapshots",
            "profile_observations",
            "account_registry",
            "conversation_refs",
            "conversation_order_eligibility",
            "conversation_links",
            "order_snapshots",
            "orders",
            "decision_cards",
        ):
            if table in tables:
                output[table] = [dict(row) for row in connection.execute("SELECT * FROM %s" % table)]
    except sqlite3.Error:
        return {}
    finally:
        if connection is not None:
            connection.close()
    return output


def _restore_existing(connection: sqlite3.Connection, state: Dict[str, List[Dict[str, Any]]]) -> None:
    valid_pairs = {row["pair_id"] for row in connection.execute("SELECT pair_id FROM style_pairs")}
    valid_customers = {
        row["customer_key"] for row in connection.execute("SELECT customer_key FROM customers")
    }
    valid_messages = {
        row["message_key"] for row in connection.execute("SELECT message_key FROM messages")
    }
    for row in state.get("style_pairs", []):
        if row.get("pair_id") in valid_pairs:
            connection.execute(
                "UPDATE style_pairs SET review_status=?,review_reasons_json=? WHERE pair_id=?",
                (
                    row.get("review_status", "pending"),
                    row.get("review_reasons_json", "[]"),
                    row["pair_id"],
                ),
            )
    for row in state.get("reviews", []):
        if row.get("pair_id") in valid_pairs:
            connection.execute(
                "INSERT OR IGNORE INTO reviews(review_id,pair_id,verdict,reasons_json,reviewer,created_at) VALUES(?,?,?,?,?,?)",
                tuple(row.get(key) for key in ("review_id", "pair_id", "verdict", "reasons_json", "reviewer", "created_at")),
            )
    for row in state.get("role_calibration", []):
        if row.get("message_key") in valid_messages and row.get("reviewer_role"):
            connection.execute(
                "UPDATE role_calibration SET reviewer_role=?,reviewed_at=? WHERE message_key=?",
                (row.get("reviewer_role"), row.get("reviewed_at"), row["message_key"]),
            )
    for row in state.get("identity_bindings", []):
        # Cross-customer reuse always downgrades to ambiguous_shared, even when
        # an older build had approved the candidate.
        if row.get("state") not in ("approved", "rejected"):
            continue
        current = connection.execute(
            "SELECT state FROM identity_bindings WHERE binding_id=?", (row.get("binding_id"),)
        ).fetchone()
        if current is not None and current["state"] != "ambiguous_shared":
            connection.execute(
                "UPDATE identity_bindings SET state=?,reviewed_at=? WHERE binding_id=?",
                (row.get("state"), row.get("reviewed_at"), row.get("binding_id")),
            )
    for row in state.get("drafts", []):
        if row.get("customer_key") not in valid_customers:
            continue
        columns = (
            "draft_id",
            "customer_key",
            "request_text",
            "draft_text",
            "intent",
            "needs_clarification",
            "needs_human",
            "risk_json",
            "grounding_refs_json",
            "status",
            "created_at",
        )
        connection.execute(
            "INSERT OR IGNORE INTO drafts(%s) VALUES(%s)"
            % (",".join(columns), ",".join("?" for _ in columns)),
            tuple(row.get(column) for column in columns),
        )
    valid_drafts = {row["draft_id"] for row in connection.execute("SELECT draft_id FROM drafts")}
    for row in state.get("feedback", []):
        if row.get("draft_id") not in valid_drafts or row.get("customer_key") not in valid_customers:
            continue
        columns = ("feedback_id", "draft_id", "customer_key", "outcome", "final_text", "created_at")
        connection.execute(
            "INSERT OR IGNORE INTO feedback(%s) VALUES(%s)"
            % (",".join(columns), ",".join("?" for _ in columns)),
            tuple(row.get(column) for column in columns),
        )

    def restore_rows(table: str, rows: Iterable[Dict[str, Any]]) -> None:
        allowed = [row[1] for row in connection.execute("PRAGMA table_info(%s)" % table)]
        for item in rows:
            columns = [column for column in allowed if column in item]
            if not columns:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO %s(%s) VALUES(%s)"
                % (table, ",".join(columns), ",".join("?" for _ in columns)),
                tuple(item.get(column) for column in columns),
            )

    # Preserve durable M0 facts in dependency order.  Cards themselves are
    # regenerated by Task 5; only the review status of a still-stable card is
    # restored below, and outcomes are deliberately never copied.
    restore_rows("pipeline_runs", state.get("pipeline_runs", []))
    restore_rows("source_snapshots", state.get("source_snapshots", []))
    restore_rows("profile_observations", state.get("profile_observations", []))
    restore_rows("account_registry", state.get("account_registry", []))
    restore_rows(
        "conversation_refs",
        (
            row
            for row in state.get("conversation_refs", [])
            if row.get("customer_key") in valid_customers
        ),
    )
    valid_conversation_refs = {
        row["customer_key"] for row in connection.execute("SELECT customer_key FROM conversation_refs")
    }
    restore_rows(
        "conversation_order_eligibility",
        (
            row
            for row in state.get("conversation_order_eligibility", [])
            if row.get("customer_key") in valid_conversation_refs
        ),
    )
    restore_rows(
        "conversation_links",
        (
            row
            for row in state.get("conversation_links", [])
            if row.get("customer_key") in valid_conversation_refs
        ),
    )
    restore_rows("order_snapshots", state.get("order_snapshots", []))
    restore_rows("orders", state.get("orders", []))
    valid_cards = {row["card_id"] for row in connection.execute("SELECT card_id FROM decision_cards")}
    for row in state.get("decision_cards", []):
        if row.get("card_id") in valid_cards:
            connection.execute(
                "UPDATE decision_cards SET review_status=? WHERE card_id=?",
                (row.get("review_status", "pending"), row["card_id"]),
            )


def build_database(
    export_root: str,
    db_path: str,
    limit_pairs: int = 500,
    account_id: Optional[str] = None,
    secret: Optional[str] = None,
) -> Dict[str, Any]:
    root = Path(export_root).expanduser().resolve()
    output = Path(db_path).expanduser().resolve()
    if not (root / "messages.jsonl").is_file() or not (root / "conversation_index.json").is_file():
        raise FileNotFoundError("export root must contain messages.jsonl and conversation_index.json")
    actual_secret = secret or os.environ.get("WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET)
    actual_account_id = account_id or os.environ.get("WECHAT_CS_ACCOUNT_ID", "current-export")
    prior_state = _snapshot_existing(output)
    prior_fingerprints = {
        str(row.get("hmac_key_fingerprint"))
        for row in prior_state.get("pipeline_runs", [])
        if row.get("hmac_key_fingerprint")
    }
    if prior_fingerprints and prior_fingerprints != {hmac_key_fingerprint(actual_secret)}:
        raise RuntimeError("HMAC key fingerprint mismatch")
    conversation_index = _load_conversation_index(root)
    customer_index = {
        hmac_id(actual_secret, "customer", actual_account_id, conversation_id): row
        for conversation_id, row in conversation_index.items()
    }
    by_customer, statuses, first_at, last_at = load_messages(root, actual_secret, actual_account_id)
    if not by_customer or last_at is None:
        raise ValueError("no eligible friend/plain-text/status-2-or-3 messages found")
    snapshot_first, snapshot_last = _export_snapshot_window(root, first_at, last_at)
    assert snapshot_last is not None

    candidates: List[PairCandidate] = []
    analyses: Dict[str, Dict[str, Any]] = {}
    for customer_key, messages in by_customer.items():
        turns = merge_turns(messages, window_minutes=15)
        customer_candidates = pair_turns(turns, reply_window_minutes=30)
        known_names = _known_name_tokens(customer_index.get(customer_key, {}))
        for candidate in customer_candidates:
            candidate.trigger_text = _scrub_known_names(candidate.trigger_text, known_names)
            candidate.reply_text = _scrub_known_names(candidate.reply_text, known_names)
            candidate.context = _scrub_known_names(candidate.context, known_names)
        candidates.extend(customer_candidates)
        analysis = analyze_customer(messages, snapshot_last)
        analysis["memory"] = _scrub_known_names(analysis["memory"], known_names)
        analyses[customer_key] = analysis
    selected = select_candidates(candidates, max(0, limit_pairs), secret=actual_secret)

    phones_by_customer: Dict[str, Dict[str, List[str]]] = {}
    phone_customers: Dict[str, set] = defaultdict(set)
    for customer_key, messages in by_customer.items():
        phone_evidence: Dict[str, List[str]] = defaultdict(list)
        for message in messages:
            for phone in extract_mainland_phones(message.text):
                phone_evidence[phone].append(message.message_key)
                phone_customers[phone].add(customer_key)
        phones_by_customer[customer_key] = phone_evidence

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=str(output.parent))
    os.close(fd)
    temp_path = Path(temp_name)
    try:
        connection = open_store(str(temp_path))
        initialize_schema(connection)
        now = datetime.now().isoformat(timespec="seconds")
        for customer_key in sorted(by_customer):
            messages = sorted(
                by_customer[customer_key], key=lambda item: (item.timestamp, item.source_ordinal)
            )
            analysis = analyses[customer_key]
            connection.execute(
                """INSERT INTO customers(
                    customer_key,display_name,last_active_at,opportunity_score,
                    opportunity_level,aftersales_priority,summary,reasons_json,
                    evidence_json,memory_json,source_file
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    customer_key,
                    _safe_display_name(customer_index.get(customer_key, {}), customer_key),
                    analysis["last_active_at"],
                    analysis["opportunity_score"],
                    analysis["opportunity_level"],
                    analysis["aftersales_priority"],
                    analysis["summary"],
                    json_dumps(analysis["reasons"]),
                    json_dumps(analysis["evidence_message_keys"]),
                    json_dumps(analysis["memory"]),
                    messages[0].source_file,
                ),
            )
            connection.executemany(
                """INSERT INTO messages(
                    message_key,customer_key,role,timestamp,text,source_file,source_ordinal
                ) VALUES(?,?,?,?,?,?,?)""",
                [
                    (
                        message.message_key,
                        message.customer_key,
                        message.role,
                        message.timestamp,
                        message.text,
                        message.source_file,
                        message.source_ordinal,
                    )
                    for message in messages
                ],
            )
            phone_evidence = phones_by_customer[customer_key]
            if not phone_evidence:
                connection.execute(
                    """INSERT INTO identity_bindings(
                        binding_id,customer_key,phone_hmac,masked_hint,state,
                        evidence_message_keys_json,reviewed_at
                    ) VALUES(?,?,?,?,?,?,NULL)""",
                    (
                        hmac_id(actual_secret, "binding", actual_account_id, customer_key, "missing"),
                        customer_key,
                        None,
                        "",
                        "missing",
                        "[]",
                    ),
                )
            else:
                customer_phone_count = len(phone_evidence)
                for phone in sorted(phone_evidence):
                    phone_hmac = hmac_id(actual_secret, "phone", actual_account_id, phone)
                    if len(phone_customers[phone]) > 1:
                        state = "ambiguous_shared"
                    elif customer_phone_count == 1:
                        state = "candidate_unique"
                    else:
                        state = "review"
                    connection.execute(
                        """INSERT INTO identity_bindings(
                            binding_id,customer_key,phone_hmac,masked_hint,state,
                            evidence_message_keys_json,reviewed_at
                        ) VALUES(?,?,?,?,?,?,NULL)""",
                        (
                            hmac_id(
                                actual_secret,
                                "binding",
                                actual_account_id,
                                customer_key,
                                phone_hmac,
                            ),
                            customer_key,
                            phone_hmac,
                            "%s******%s" % (phone[0], phone[-4:]),
                            state,
                            json_dumps(sorted(set(phone_evidence[phone]))),
                        ),
                    )
            for reason in analysis["reasons"]:
                message_keys = reason.get("evidence", [])
                if not message_keys:
                    continue
                evidence_key = hmac_id(
                    actual_secret,
                    "evidence",
                    customer_key,
                    reason["code"],
                    ",".join(message_keys),
                )
                connection.execute(
                    "INSERT INTO evidence(evidence_key,customer_key,kind,message_keys_json,summary,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        evidence_key,
                        customer_key,
                        reason["code"],
                        json_dumps(message_keys),
                        reason["code"],
                        now,
                    ),
                )

        for candidate in selected:
            pair_id = hmac_id(
                actual_secret,
                "pair",
                actual_account_id,
                candidate.customer_key,
                candidate.timestamp,
                content_digest(candidate.trigger_text),
                content_digest(candidate.reply_text),
            )
            risk = {"flags": candidate.risk_flags, "level": candidate.risk_level}
            connection.execute(
                """INSERT INTO style_pairs(
                    pair_id,customer_key,trigger_text,reply_text,context_json,
                    intent_stage,risk_json,review_status,review_reasons_json,split,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    pair_id,
                    candidate.customer_key,
                    candidate.trigger_text,
                    candidate.reply_text,
                    json_dumps(candidate.context),
                    candidate.intent_stage,
                    json_dumps(risk),
                    "pending",
                    "[]",
                    stable_split(actual_secret, candidate.customer_key),
                    candidate.timestamp,
                ),
            )

        for message in _role_calibration_sample(by_customer, 200):
            expected = "studio" if statuses[message.message_key] == 2 else "customer"
            calibration_id = hmac_id(actual_secret, "calibration", message.message_key)
            connection.execute(
                """INSERT INTO role_calibration(
                    calibration_id,customer_key,message_key,source_status,
                    source_role_evidence_json,expected_role
                ) VALUES(?,?,?,?,?,?)""",
                (
                    calibration_id,
                    message.customer_key,
                    message.message_key,
                    statuses[message.message_key],
                    json_dumps(
                        {
                            "source_kind": "export",
                            "evidence_type": "raw_payload.status",
                            "evidence_value": str(statuses[message.message_key]),
                        }
                    ),
                    expected,
                ),
            )

        _restore_existing(connection, prior_state)
        set_meta(connection, "built_at", now)
        set_meta(connection, "snapshot_first_at", snapshot_first.isoformat() if snapshot_first else "")
        set_meta(connection, "snapshot_last_at", snapshot_last.isoformat())
        set_meta(connection, "eligible_message_first_at", first_at.isoformat() if first_at else "")
        set_meta(connection, "eligible_message_last_at", last_at.isoformat())
        set_meta(connection, "source_friend_text_messages", sum(len(items) for items in by_customer.values()))
        set_meta(connection, "source_friend_text_customers", len(by_customer))
        set_meta(connection, "source_friend_sessions", len(conversation_index))
        set_meta(
            connection,
            "source_friend_sessions_with_any_messages",
            sum(bool(row.get("message_count")) for row in conversation_index.values()),
        )
        set_meta(connection, "candidate_pairs_before_limit", len(candidates))
        set_meta(connection, "selected_style_pairs", len(selected))
        set_meta(
            connection,
            "identity_binding_customers_with_candidates",
            sum(bool(items) for items in phones_by_customer.values()),
        )
        set_meta(connection, "uses_default_hmac_secret", "true" if actual_secret == DEFAULT_HMAC_SECRET else "false")
        set_meta(connection, "weak_hmac_secret", "true" if len(actual_secret) < 32 else "false")
        set_meta(connection, "role_mapping", {"2": "studio", "3": "customer"})
        connection.commit()
        # Fold WAL changes into the main file and switch the file header back to
        # DELETE mode.  A database left in persistent WAL mode cannot reliably
        # be opened read-only after its sidecars are removed/moved.
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("PRAGMA journal_mode = DELETE")
        health = get_health(connection)
        connection.close()
        for suffix in ("-wal", "-shm"):
            old_sidecar = Path(str(output) + suffix)
            if old_sidecar.exists():
                old_sidecar.unlink()
        os.replace(str(temp_path), str(output))
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(temp_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        return health
    except Exception:
        try:
            connection.close()  # type: ignore[name-defined]
        except Exception:
            pass
        if temp_path.exists():
            temp_path.unlink()
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(temp_path) + suffix)
            if sidecar.exists():
                sidecar.unlink()
        raise


def apply_role_calibration_csv(db_path: str, csv_path: str) -> Dict[str, Any]:
    """Apply human labels from calibration_id,reviewer_role CSV columns."""

    connection = open_store(db_path)
    updated = 0
    with Path(csv_path).expanduser().open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not {"calibration_id", "reviewer_role"}.issubset(set(reader.fieldnames or [])):
            raise ValueError("CSV requires calibration_id and reviewer_role columns")
        for row in reader:
            role = (row.get("reviewer_role") or "").strip()
            if role not in ("studio", "customer"):
                raise ValueError("reviewer_role must be studio or customer")
            cursor = connection.execute(
                "UPDATE role_calibration SET reviewer_role=?,reviewed_at=? WHERE calibration_id=?",
                (role, datetime.now().isoformat(timespec="seconds"), row["calibration_id"]),
            )
            updated += cursor.rowcount
    connection.commit()
    summary = calibration_summary(connection)
    connection.close()
    summary["updated"] = updated
    return summary


def export_chatml(
    db_path: str,
    output_path: str,
    split: str = "all",
    include_pending: bool = False,
    allow_unverified_roles: bool = False,
    include_risky: bool = False,
) -> Dict[str, Any]:
    connection = open_store(db_path, read_only=True)
    calibration = calibration_summary(connection)
    if not calibration["passed"] and not allow_unverified_roles:
        connection.close()
        raise RuntimeError("role calibration must review all 200 samples at >=99% before export")
    conditions = []
    params: List[Any] = []
    if not include_pending:
        conditions.append("review_status='approved'")
    if split != "all":
        if split not in ("train", "validation", "test"):
            connection.close()
            raise ValueError("split must be train, validation, test, or all")
        conditions.append("split=?")
        params.append(split)
    query = "SELECT * FROM style_pairs"
    if conditions:
        query += " WHERE " + " AND ".join(conditions)
    query += " ORDER BY created_at,pair_id"
    rows = list(connection.execute(query, params))
    if not include_risky:
        rows = [row for row in rows if json.loads(row["risk_json"]).get("level") == "low"]
    output = Path(output_path).expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name + ".", suffix=".tmp", dir=str(output.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            for row in rows:
                context = json.loads(row["context_json"])
                context_text = "\n".join(
                    "%s：%s" % ("客户" if item["role"] == "customer" else "工作室", item["text"])
                    for item in context
                )
                user_text = row["trigger_text"]
                if context_text:
                    user_text = "历史上下文：\n%s\n\n客户最新消息：\n%s" % (context_text, user_text)
                record = {
                    "messages": [
                        {
                            "role": "system",
                            "content": "模仿工作室的语气、节奏和长度；不要复制历史事实、姓名、数字、价格、库存、物流或承诺。",
                        },
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": row["reply_text"]},
                    ],
                    "metadata": {
                        "sample_id": row["pair_id"],
                        "customer_id": row["customer_key"],
                        "intent_stage": row["intent_stage"],
                        "risk": json.loads(row["risk_json"]),
                        "split": row["split"],
                        "schema": "wechat_style_pair.v1",
                    },
                }
                handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, str(output))
    except Exception:
        try:
            os.close(fd)
        except OSError:
            pass
        if Path(temp_name).exists():
            Path(temp_name).unlink()
        connection.close()
        raise
    connection.close()
    return {
        "exported": len(rows),
        "output": str(output),
        "split": split,
        "included_risky": include_risky,
    }
