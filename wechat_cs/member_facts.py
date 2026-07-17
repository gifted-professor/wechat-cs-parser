"""Versioned, read-only import of member birthday and preference facts."""

from __future__ import annotations

import json
import os
import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .core import DEFAULT_HMAC_SECRET, hmac_id, json_dumps, redact_text
from .identity import global_phone_hmac, normalize_phone
from .source_snapshot import hmac_key_fingerprint, read_stable_bytes
from .store import initialize_schema, open_store


SHANGHAI = ZoneInfo("Asia/Shanghai")
SOURCE_NAMESPACE = "dashboard-birthday-members"
SOURCE_KIND = "birthday_members"
MEMBER_FACT_RULE_VERSION = "member-facts-v1"

_FIELD_LIMITS = {
    "member_birthday": 32,
    "preferred_style": 500,
    "expected_gift": 500,
    "member_shop": 200,
}
_ISO_DATE = re.compile(r"^(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})$")
_MONTH_DAY = re.compile(r"^(\d{1,2})(?:[-/.]|月)(\d{1,2})(?:日|号)?$")


@dataclass(frozen=True)
class _MemberRecord:
    source_record_id: str
    raw_record_id: str
    phone_hmac: str
    member_birthday: Optional[str]
    preferred_style: Optional[str]
    expected_gift: Optional[str]
    member_shop: Optional[str]
    quality_flags: Tuple[str, ...]


@dataclass(frozen=True)
class _ApprovedLink:
    customer_key: str
    profile_id: str


def _bounded_redacted_text(
    value: object,
    *,
    field: str,
) -> tuple[Optional[str], Tuple[str, ...]]:
    raw = str(value or "").replace("\x00", "").strip()
    if not raw:
        return None, ()
    redacted, redaction_flags = redact_text(raw)
    limit = _FIELD_LIMITS[field]
    flags = ["%s:redacted_%s" % (field, item) for item in redaction_flags]
    if len(redacted) > limit:
        flags.append("%s:truncated" % field)
    bounded = redacted[:limit].strip()
    return bounded or None, tuple(sorted(set(flags)))


def _structured_birthday(value: object) -> Optional[str]:
    """Normalize dashboard dates, including Excel serials, without guessing prose."""

    raw = str(value or "").strip()
    if not raw:
        return None

    # Feishu exports the birthday column as an Excel-compatible day serial.
    try:
        serial = Decimal(raw)
    except InvalidOperation:
        serial = None
    if serial is not None and serial.is_finite() and Decimal("20000") <= serial <= Decimal(
        "80000"
    ):
        whole_days = int(serial)
        return (date(1899, 12, 30) + timedelta(days=whole_days)).isoformat()

    match = _ISO_DATE.fullmatch(raw)
    if match:
        try:
            return date(*(int(part) for part in match.groups())).isoformat()
        except ValueError:
            return None

    match = _MONTH_DAY.fullmatch(raw)
    if match:
        month, day = (int(part) for part in match.groups())
        try:
            # A leap year validates 02-29 without inventing a customer's year.
            date(2000, month, day)
        except ValueError:
            return None
        return "%02d-%02d" % (month, day)

    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        return datetime.fromisoformat(text).date().isoformat()
    except ValueError:
        return None


def _birthday_fact(value: object) -> tuple[Optional[str], Tuple[str, ...]]:
    structured = _structured_birthday(value)
    if structured is not None:
        # Run the canonical value through the shared redactor as a guard.  A
        # validated structured date is intentionally retained as the fact the
        # sales profile needs; arbitrary date-like prose is never restored.
        _redacted, redaction_flags = redact_text(structured)
        unexpected = [
            "member_birthday:redacted_%s" % item
            for item in redaction_flags
            if item != "date"
        ]
        return structured[: _FIELD_LIMITS["member_birthday"]], tuple(sorted(unexpected))

    bounded, flags = _bounded_redacted_text(value, field="member_birthday")
    if bounded is not None:
        flags = tuple(sorted(set(flags + ("member_birthday:unstructured",))))
    return bounded, flags


def _safe_source_record_id(
    value: object,
    *,
    secret: str,
) -> tuple[Optional[str], Tuple[str, ...], str]:
    raw = str(value or "").replace("\x00", "").strip()
    if not raw:
        return None, (), raw
    redacted, flags = redact_text(raw)
    if flags or len(raw) > 200 or redacted != raw:
        return (
            hmac_id(secret, "member-source-record", raw),
            ("source_record_id:opaque",),
            raw,
        )
    return raw, (), raw


def _parse_synced_at(value: object) -> Optional[str]:
    raw = str(value or "").strip()
    if not raw:
        return None
    text = raw[:-1] + "+00:00" if raw.endswith("Z") else raw
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("member source synced_at must be a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("member source synced_at must include a timezone")
    return parsed.astimezone(SHANGHAI).isoformat(timespec="seconds")


def _load_document(stable_data: bytes) -> tuple[List[object], object, Optional[str]]:
    try:
        document = json.loads(stable_data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("member source must be valid UTF-8 JSON") from exc
    if isinstance(document, list):
        return list(document), None, None
    if isinstance(document, dict) and isinstance(document.get("records"), list):
        return (
            list(document["records"]),
            document.get("total_records"),
            _parse_synced_at(document.get("synced_at")),
        )
    raise ValueError("member source must be a records envelope or top-level array")


def _normalize_records(
    records: Sequence[object],
    *,
    secret: str,
) -> tuple[List[_MemberRecord], Dict[str, int]]:
    normalized: List[_MemberRecord] = []
    stats: Counter[str] = Counter()
    seen_record_ids = set()
    for raw_row in records:
        if not isinstance(raw_row, dict):
            stats["non_object_records"] += 1
            continue
        safe_record_id, record_flags, raw_record_id = _safe_source_record_id(
            raw_row.get("record_id"), secret=secret
        )
        if safe_record_id is None:
            stats["missing_record_id_records"] += 1
            continue
        if raw_record_id in seen_record_ids:
            stats["duplicate_record_id_records"] += 1
            continue
        seen_record_ids.add(raw_record_id)

        phone = normalize_phone(str(raw_row.get("member_phone") or ""))
        if phone is None:
            stats["invalid_phone_records"] += 1
            continue

        birthday, birthday_flags = _birthday_fact(raw_row.get("member_birthday"))
        preferred_style, style_flags = _bounded_redacted_text(
            raw_row.get("preferred_style"), field="preferred_style"
        )
        expected_gift, gift_flags = _bounded_redacted_text(
            raw_row.get("expected_gift"), field="expected_gift"
        )
        member_shop, shop_flags = _bounded_redacted_text(
            raw_row.get("member_shop"), field="member_shop"
        )
        normalized.append(
            _MemberRecord(
                source_record_id=safe_record_id,
                raw_record_id=raw_record_id,
                phone_hmac=global_phone_hmac(secret, phone),
                member_birthday=birthday,
                preferred_style=preferred_style,
                expected_gift=expected_gift,
                member_shop=member_shop,
                quality_flags=tuple(
                    sorted(
                        set(
                            record_flags
                            + birthday_flags
                            + style_flags
                            + gift_flags
                            + shop_flags
                        )
                    )
                ),
            )
        )
    return normalized, dict(stats)


def _identity_index(
    rows: Iterable[Mapping[str, object]],
) -> tuple[Dict[str, Tuple[_ApprovedLink, ...]], set[str], set[str]]:
    materialized = list(rows)
    conflict_customers = {
        str(row["customer_key"])
        for row in materialized
        if row["state"] == "conflict"
    }
    conflict_phones = {
        str(row["phone_hmac"])
        for row in materialized
        if row["state"] == "conflict" and row["phone_hmac"]
    }
    approved: Dict[str, Dict[str, _ApprovedLink]] = defaultdict(dict)
    for row in materialized:
        phone_hmac = str(row["phone_hmac"] or "")
        customer_key = str(row["customer_key"])
        if (
            row["state"] != "approved"
            or not phone_hmac
            or customer_key in conflict_customers
            or phone_hmac in conflict_phones
        ):
            continue
        approved[phone_hmac][customer_key] = _ApprovedLink(
            customer_key=customer_key,
            profile_id=str(row["profile_id"]),
        )
    return (
        {
            phone: tuple(sorted(by_customer.values(), key=lambda item: item.customer_key))
            for phone, by_customer in approved.items()
        },
        conflict_customers,
        conflict_phones,
    )


def _quality_summary(
    *,
    source_records: int,
    normalization_stats: Mapping[str, int],
    matched_records: int,
    persisted_facts: int,
    conflict_filtered_records: int,
    unmatched_records: int,
    envelope_total: object,
    fact_flags: Counter[str],
) -> Dict[str, object]:
    quarantined = sum(int(value) for value in normalization_stats.values())
    return {
        "source_records": source_records,
        "normalized_records": source_records - quarantined,
        "matched_records": matched_records,
        "persisted_facts": persisted_facts,
        "conflict_filtered_records": conflict_filtered_records,
        "unmatched_records": unmatched_records,
        "invalid_phone_records": int(normalization_stats.get("invalid_phone_records", 0)),
        "missing_record_id_records": int(
            normalization_stats.get("missing_record_id_records", 0)
        ),
        "duplicate_record_id_records": int(
            normalization_stats.get("duplicate_record_id_records", 0)
        ),
        "non_object_records": int(normalization_stats.get("non_object_records", 0)),
        "quarantined_records": quarantined,
        "envelope_total": envelope_total,
        "envelope_total_matches": envelope_total is None or envelope_total == source_records,
        "quality_flag_counts": dict(sorted(fact_flags.items())),
        "rule_version": MEMBER_FACT_RULE_VERSION,
    }


def import_member_facts(
    db_path: Path,
    source_path: Path,
    *,
    secret: Optional[str] = None,
    source_namespace: str = SOURCE_NAMESPACE,
) -> Dict[str, object]:
    """Import a stable member snapshot without persisting names or raw identities."""

    actual_secret = secret or os.environ.get("WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET)
    stable = read_stable_bytes(Path(source_path))
    records, envelope_total, observed_until = _load_document(stable.data)
    normalized, normalization_stats = _normalize_records(records, secret=actual_secret)
    snapshot_id = hmac_id(
        actual_secret,
        "source-snapshot",
        SOURCE_KIND,
        source_namespace,
        stable.sha256,
        MEMBER_FACT_RULE_VERSION,
    )

    connection = open_store(str(Path(db_path).expanduser().resolve()))
    try:
        initialize_schema(connection)
        run = connection.execute(
            "SELECT run_id,hmac_key_fingerprint FROM pipeline_runs "
            "ORDER BY started_at DESC,run_id DESC LIMIT 1"
        ).fetchone()
        if run is None:
            raise RuntimeError("member fact import requires an initialized pipeline run")
        if run["hmac_key_fingerprint"] != hmac_key_fingerprint(actual_secret):
            raise RuntimeError("HMAC key fingerprint mismatch")

        prior = connection.execute(
            "SELECT quality_json FROM source_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if prior is not None:
            return {
                "source_snapshot_id": snapshot_id,
                "source_hash": stable.sha256,
                "state": "imported",
                "idempotent": True,
                "quality": json.loads(prior["quality_json"]),
            }

        identity_rows = connection.execute(
            """
            SELECT cl.customer_key,cl.profile_id,cl.phone_hmac,cl.state
            FROM conversation_links cl
            JOIN conversation_refs cr
              ON cr.customer_key=cl.customer_key AND cr.profile_id=cl.profile_id
            """
        ).fetchall()
        approved_by_phone, conflict_customers, conflict_phones = _identity_index(identity_rows)

        facts = []
        matched_records = 0
        conflict_filtered_records = 0
        unmatched_records = 0
        fact_flags: Counter[str] = Counter()
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        for record in normalized:
            approved = approved_by_phone.get(record.phone_hmac, ())
            if not approved:
                phone_rows = [
                    row
                    for row in identity_rows
                    if row["phone_hmac"] == record.phone_hmac
                ]
                has_conflict = record.phone_hmac in conflict_phones or any(
                    str(row["customer_key"]) in conflict_customers for row in phone_rows
                )
                if has_conflict:
                    conflict_filtered_records += 1
                else:
                    unmatched_records += 1
                continue
            matched_records += 1
            shared_flags = set(record.quality_flags)
            if len(approved) > 1:
                shared_flags.add("identity:multiple_approved_customers")
            for link in approved:
                flags = tuple(sorted(shared_flags))
                fact_flags.update(flags)
                facts.append(
                    (
                        hmac_id(
                            actual_secret,
                            "customer-aux-fact",
                            source_namespace,
                            record.raw_record_id,
                            link.customer_key,
                            stable.sha256,
                            MEMBER_FACT_RULE_VERSION,
                        ),
                        snapshot_id,
                        source_namespace,
                        record.source_record_id,
                        link.customer_key,
                        link.profile_id,
                        record.phone_hmac,
                        record.member_birthday,
                        record.preferred_style,
                        record.expected_gift,
                        record.member_shop,
                        stable.sha256,
                        json_dumps(list(flags)),
                        now,
                    )
                )

        quality = _quality_summary(
            source_records=len(records),
            normalization_stats=normalization_stats,
            matched_records=matched_records,
            persisted_facts=len(facts),
            conflict_filtered_records=conflict_filtered_records,
            unmatched_records=unmatched_records,
            envelope_total=envelope_total,
            fact_flags=fact_flags,
        )
        with connection:
            connection.execute(
                """
                INSERT INTO source_snapshots(
                    snapshot_id,run_id,source_kind,source_path_hash,device,inode,size,
                    mtime_ns,sha256,record_count,first_at,last_at,observed_until,
                    captured_at,consistency_state,quality_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    run["run_id"],
                    SOURCE_KIND,
                    hmac_id(actual_secret, "source-path", source_namespace),
                    stable.device,
                    stable.inode,
                    stable.size,
                    stable.mtime_ns,
                    stable.sha256,
                    len(records),
                    None,
                    None,
                    observed_until,
                    now,
                    "consistent",
                    json_dumps(quality),
                ),
            )
            connection.executemany(
                """
                INSERT INTO customer_aux_facts(
                    aux_fact_id,source_snapshot_id,source_namespace,source_record_id,
                    customer_key,profile_id,phone_hmac,member_birthday,preferred_style,
                    expected_gift,member_shop,source_hash,quality_flags_json,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                facts,
            )
        return {
            "source_snapshot_id": snapshot_id,
            "source_hash": stable.sha256,
            "state": "imported",
            "idempotent": False,
            "quality": quality,
        }
    finally:
        connection.close()
