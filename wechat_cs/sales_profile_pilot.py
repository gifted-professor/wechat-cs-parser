"""Prepare and freeze the deterministic 50-person sales-profile pilot."""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .action_pipeline import (
    _feature_rows_at,
    _index_feature_inputs,
    _load_identity_rows,
    _load_messages,
    _load_order_source,
    _load_profile_sources,
    _persist_feature,
    _truncate_orders,
)
from .core import DEFAULT_HMAC_SECRET, hmac_id, json_dumps
from .customer_features import FEATURE_RULE_VERSION
from .sales_profile_sampling import (
    DEFAULT_STRATUM_QUOTAS,
    SAMPLING_VERSION,
    SamplingCandidate,
    select_sales_profile_subjects,
)
from .source_snapshot import hmac_key_fingerprint
from .store import initialize_schema, open_store


SHANGHAI = ZoneInfo("Asia/Shanghai")
DEFAULT_SOURCE_RUN_ID = "20260713T140730+0800-833c3257"
DEFAULT_AS_OF_AT = "2026-07-13T20:14:37+08:00"
DEFAULT_MODEL = "kimi-k2.7-code"
EXTRACTION_PROMPT_VERSION = "sales-events-v2"
PROFILE_PROMPT_VERSION = "sales-profile-v3"
PROFILE_SCHEMA_VERSION = "sales-profile-card-v1"

_FUTURE_WAIT_PATTERNS = (
    re.compile(r"(?:下次|过几天|改天|晚点|稍后|之后|以后).{0,12}(?:再来|再看|再买|再拍|再联系|再问)"),
    re.compile(r"(?:有了|到货|补货|发工资|发薪|活动|优惠).{0,12}(?:再来|再看|再买|再拍|再联系|再问)"),
    re.compile(r"(?:等|等到).{0,16}(?:活动|优惠|发工资|发薪|到货|补货).{0,12}(?:再|就)"),
)
_COMPLEX_PATTERNS = (
    re.compile(r"(?:别|不要|不用|无需|不必|请勿).{0,8}(?:联系|跟进|打扰|发消息|发微信)"),
    re.compile(r"(?:售后|退款|退货|换货|质量问题|破损|少件|补偿)"),
)


def _moment(value: object, *, field: str) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("%s must be a valid ISO timestamp" % field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("%s must include a timezone" % field)
    return parsed.astimezone(SHANGHAI)


def refresh_sales_profile_features(
    connection,
    *,
    as_of_at: datetime,
    source_run_id: str,
    secret: str,
) -> Dict[str, object]:
    """Persist v2 point-in-time features without rebuilding the action queue."""

    sources = _load_profile_sources(connection, None)
    if {item.run_id for item in sources} != {source_run_id}:
        raise RuntimeError("sales profile source run does not match normalized conversations")
    order_source = _load_order_source(connection)
    if order_source is None or order_source.run_id != source_run_id:
        raise RuntimeError("sales profile pilot requires an active order snapshot")
    point_in_time_orders = _truncate_orders(order_source.rows, as_of_at)
    feature_rows: Dict[str, Mapping[str, object]] = {}
    for source in sources:
        messages = _load_messages(connection, source, as_of_at=as_of_at)
        identity_rows = _load_identity_rows(connection, source.profile_id)
        index = _index_feature_inputs(identity_rows, point_in_time_orders, messages)
        current, _freshness = _feature_rows_at(
            source,
            cutoff=as_of_at,
            target_customers=source.customer_keys,
            identity_rows=identity_rows,
            orders=point_in_time_orders,
            messages=messages,
            order_source=order_source,
            effective_collector_status="running",
            secret=secret,
            input_index=index,
        )
        feature_rows.update(
            {str(row["feature_snapshot_id"]): row for row in current.values()}
        )
    created_at = as_of_at.isoformat(timespec="seconds")
    with connection:
        for row in feature_rows.values():
            _persist_feature(connection, row, created_at)
    return {
        "feature_snapshots": len(feature_rows),
        "feature_rule_version": FEATURE_RULE_VERSION,
    }


def _message_signals(connection, cutoff: str) -> Tuple[Counter[str], set[str]]:
    future_counts: Counter[str] = Counter()
    complex_customers = set()
    for row in connection.execute(
        "SELECT customer_key,text FROM messages WHERE role='customer' AND timestamp<=?",
        (cutoff,),
    ):
        text = str(row["text"] or "")
        customer_key = str(row["customer_key"])
        if any(pattern.search(text) for pattern in _FUTURE_WAIT_PATTERNS):
            future_counts[customer_key] += 1
        if any(pattern.search(text) for pattern in _COMPLEX_PATTERNS):
            complex_customers.add(customer_key)
    return future_counts, complex_customers


def _valid_payment_stats(connection, cutoff: datetime) -> Dict[str, Tuple[int, int]]:
    result: Dict[str, Tuple[int, int]] = {}
    rows = connection.execute(
        """
        SELECT o.phone_hmac,COUNT(*) AS frequency,SUM(o.revenue_minor) AS monetary
        FROM orders o
        JOIN order_snapshots os ON os.order_snapshot_id=o.order_snapshot_id
        WHERE os.state='active' AND o.phone_hmac IS NOT NULL
          AND o.revenue_minor>0
          AND (
            (o.paid_at IS NOT NULL AND o.paid_at<=?) OR
            (o.paid_at IS NULL AND o.paid_on IS NOT NULL AND o.paid_on<?)
          )
        GROUP BY o.phone_hmac
        """,
        (cutoff.isoformat(timespec="seconds"), cutoff.date().isoformat()),
    )
    for row in rows:
        result[str(row["phone_hmac"])] = (int(row["frequency"]), int(row["monetary"]))
    return result


def _order_is_complex_at(row: Mapping[str, object], as_of_at: datetime) -> bool:
    try:
        flags = json.loads(str(row["quality_flags_json"] or "[]"))
    except json.JSONDecodeError:
        flags = []
    if not isinstance(flags, list):
        flags = []
    refund_on = str(row["refund_on"] or "")
    historical_refund = bool(
        row["refund_type"]
        and refund_on
        and refund_on[:10] < as_of_at.date().isoformat()
    )
    current_open = bool(
        "aftersale_open" in flags and "future_refund_on" not in flags
    )
    return historical_refund or current_open


def load_sampling_candidates(
    connection,
    *,
    as_of_at: datetime,
    source_run_id: str,
    aux_snapshot_id: Optional[str] = None,
) -> Tuple[SamplingCandidate, ...]:
    """Load only approved, conflict-free customers with a valid payment."""

    cutoff = as_of_at.isoformat(timespec="seconds")
    feature_rows = {
        str(row["customer_key"]): row
        for row in connection.execute(
            """
            SELECT * FROM customer_value_snapshots
            WHERE run_id=? AND as_of_at=? AND feature_rule_version=?
            ORDER BY customer_key
            """,
            (source_run_id, cutoff, FEATURE_RULE_VERSION),
        )
    }
    if not feature_rows:
        raise RuntimeError("sales profile pilot requires customer-features-v2 snapshots")

    links_by_customer: Dict[str, list] = defaultdict(list)
    for row in connection.execute(
        """
        SELECT cl.customer_key,cl.profile_id,cl.phone_hmac,cl.state
        FROM conversation_links cl
        JOIN account_registry ar ON ar.profile_id=cl.profile_id
        WHERE ar.state='approved'
        ORDER BY cl.customer_key,cl.link_id
        """
    ):
        links_by_customer[str(row["customer_key"])].append(row)
    payment_stats = _valid_payment_stats(connection, as_of_at)
    future_counts, message_complex = _message_signals(connection, cutoff)
    birthday_customers = set()
    if aux_snapshot_id:
        birthday_customers = {
            str(row[0])
            for row in connection.execute(
                "SELECT DISTINCT customer_key FROM customer_aux_facts "
                "WHERE source_snapshot_id=?",
                (aux_snapshot_id,),
            )
        }
    order_complex_phones = set()
    for row in connection.execute(
        """
        SELECT o.phone_hmac,o.refund_type,o.refund_on,o.quality_flags_json
        FROM orders o JOIN order_snapshots os ON os.order_snapshot_id=o.order_snapshot_id
        WHERE os.state='active' AND o.phone_hmac IS NOT NULL
        """
    ):
        if _order_is_complex_at(row, as_of_at):
            order_complex_phones.add(str(row["phone_hmac"]))

    candidates = []
    for customer_key, feature_row in feature_rows.items():
        links = links_by_customer.get(customer_key, [])
        approved_phones = {
            str(row["phone_hmac"])
            for row in links
            if row["state"] == "approved" and row["phone_hmac"]
        }
        if any(row["state"] == "conflict" for row in links) or len(approved_phones) != 1:
            continue
        phone_hmac = next(iter(approved_phones))
        if phone_hmac not in payment_stats:
            continue
        try:
            profile = json.loads(str(feature_row["profile_json"]))
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(profile, dict) or profile.get("identity_state") != "approved":
            continue
        frequency, monetary = payment_stats[phone_hmac]
        recency_days = int(profile.get("rfm_recency_days") or 0)
        aftersales_rate = profile.get("aftersales_rate")
        try:
            normalized_aftersales = (
                float(aftersales_rate) if aftersales_rate is not None else None
            )
        except (TypeError, ValueError):
            normalized_aftersales = None
        risk = str(profile.get("aftersales_risk") or "") in {"elevated", "high"}
        candidates.append(
            SamplingCandidate(
                customer_key=customer_key,
                profile_id=str(feature_row["profile_id"]),
                phone_hmac=phone_hmac,
                feature_snapshot_id=str(feature_row["feature_snapshot_id"]),
                complex_risk=(
                    risk
                    or customer_key in message_complex
                    or phone_hmac in order_complex_phones
                ),
                future_return_wait=future_counts[customer_key] > 0,
                frequency=frequency,
                monetary_minor=monetary,
                average_order_minor=monetary // frequency,
                recency_days=recency_days,
                aftersales_rate=normalized_aftersales,
                future_signal_count=future_counts[customer_key],
                birthday_match=customer_key in birthday_customers,
            )
        )
    return tuple(candidates)


def _latest_snapshot(connection, source_run_id: str, source_kind: str) -> Optional[str]:
    row = connection.execute(
        """
        SELECT snapshot_id FROM source_snapshots
        WHERE run_id=? AND source_kind=?
        ORDER BY captured_at DESC,snapshot_id DESC LIMIT 1
        """,
        (source_run_id, source_kind),
    ).fetchone()
    return str(row["snapshot_id"]) if row else None


def _latest_observed_snapshot(
    connection,
    source_run_id: str,
    source_kind: str,
    *,
    as_of_at: datetime,
) -> Optional[str]:
    row = connection.execute(
        """
        SELECT snapshot_id FROM source_snapshots
        WHERE run_id=? AND source_kind=? AND observed_until IS NOT NULL
          AND observed_until<=?
        ORDER BY observed_until DESC,captured_at DESC,snapshot_id DESC LIMIT 1
        """,
        (source_run_id, source_kind, as_of_at.isoformat(timespec="seconds")),
    ).fetchone()
    return str(row["snapshot_id"]) if row else None


def prepare_sales_profile_pilot(
    db_path: Path,
    *,
    as_of_at: object = DEFAULT_AS_OF_AT,
    source_run_id: str = DEFAULT_SOURCE_RUN_ID,
    secret: Optional[str] = None,
    model: str = DEFAULT_MODEL,
) -> Dict[str, object]:
    """Refresh deterministic facts and freeze exactly 50 subjects; never call Kimi."""

    actual_secret = secret or os.environ.get("WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET)
    cutoff = _moment(as_of_at, field="as_of_at")
    connection = open_store(str(Path(db_path).expanduser().resolve()))
    try:
        initialize_schema(connection)
        run = connection.execute(
            "SELECT run_id,hmac_key_fingerprint,quality_json FROM pipeline_runs WHERE run_id=?",
            (source_run_id,),
        ).fetchone()
        if run is None:
            raise RuntimeError("sales profile source run was not found")
        if run["hmac_key_fingerprint"] != hmac_key_fingerprint(actual_secret):
            raise RuntimeError("HMAC key fingerprint mismatch")
        quality_before = str(run["quality_json"])
        message_snapshot_id = _latest_snapshot(
            connection, source_run_id, "live-inbox-events"
        )
        if message_snapshot_id is None:
            raise RuntimeError("sales profile pilot requires a message source snapshot")
        order_row = connection.execute(
            """
            SELECT os.order_snapshot_id,os.synced_at,ss.run_id
            FROM order_snapshots os
            JOIN source_snapshots ss ON ss.snapshot_id=os.source_snapshot_id
            WHERE os.state='active'
            """
        ).fetchone()
        if (
            order_row is None
            or str(order_row["run_id"]) != source_run_id
            or _moment(order_row["synced_at"], field="order synced_at") > cutoff
        ):
            raise RuntimeError(
                "sales profile pilot requires an active point-in-time order snapshot"
            )
        aux_snapshot_id = _latest_observed_snapshot(
            connection,
            source_run_id,
            "birthday_members",
            as_of_at=cutoff,
        )
        refresh_sales_profile_features(
            connection,
            as_of_at=cutoff,
            source_run_id=source_run_id,
            secret=actual_secret,
        )
        candidates = load_sampling_candidates(
            connection,
            as_of_at=cutoff,
            source_run_id=source_run_id,
            aux_snapshot_id=aux_snapshot_id,
        )
        selected = select_sales_profile_subjects(candidates, secret=actual_secret)
        if len(selected) != 50:
            raise RuntimeError("sales profile pilot must freeze exactly 50 subjects")

        cohort_payload = [
            (item.customer_key, item.stratum, item.stratum_rank) for item in selected
        ]
        # Bind additive run uniqueness to the complete generation contract so
        # the same frozen people can be evaluated by a newer model without
        # overwriting or colliding with an earlier pilot.
        cohort_hash = hashlib.sha256(
            json_dumps(
                {
                    "subjects": cohort_payload,
                    "model": model,
                    "extraction_prompt_version": EXTRACTION_PROMPT_VERSION,
                    "profile_prompt_version": PROFILE_PROMPT_VERSION,
                    "profile_schema_version": PROFILE_SCHEMA_VERSION,
                    "sampling_version": SAMPLING_VERSION,
                }
            ).encode("utf-8")
        ).hexdigest()
        run_id = hmac_id(
            actual_secret,
            "sales-profile-run",
            source_run_id,
            cutoff.isoformat(timespec="seconds"),
            cohort_hash,
            SAMPLING_VERSION,
            model,
            EXTRACTION_PROMPT_VERSION,
            PROFILE_PROMPT_VERSION,
            PROFILE_SCHEMA_VERSION,
        )
        existing = connection.execute(
            "SELECT sales_profile_run_id FROM sales_profile_runs WHERE sales_profile_run_id=?",
            (run_id,),
        ).fetchone()
        stratum_counts = Counter(item.stratum for item in selected)
        profile_counts = Counter(item.profile_id for item in selected)
        birthday_count = sum(item.birthday_match for item in selected)
        result = {
            "sales_profile_run_id": run_id,
            "source_run_id": source_run_id,
            "as_of_at": cutoff.isoformat(timespec="seconds"),
            "subject_count": len(selected),
            "stratum_counts": dict(sorted(stratum_counts.items())),
            "profile_counts": dict(sorted(profile_counts.items())),
            "birthday_match_count": birthday_count,
            "model": model,
            "model_called": False,
            "send_allowed": False,
        }
        if existing is not None:
            result["idempotent"] = True
            return result

        now = datetime.now().astimezone().isoformat(timespec="seconds")
        counts = {
            "subjects": len(selected),
            "strata": dict(sorted(stratum_counts.items())),
            "profiles": dict(sorted(profile_counts.items())),
            "birthday_matches": birthday_count,
        }
        feature_payloads = {
            str(row["feature_snapshot_id"]): (
                str(row["profile_json"]),
                str(row["freshness_json"]),
            )
            for row in connection.execute(
                "SELECT feature_snapshot_id,profile_json,freshness_json "
                "FROM customer_value_snapshots WHERE run_id=? AND as_of_at=? "
                "AND feature_rule_version=?",
                (
                    source_run_id,
                    cutoff.isoformat(timespec="seconds"),
                    FEATURE_RULE_VERSION,
                ),
            )
        }
        with connection:
            connection.execute(
                """
                INSERT INTO sales_profile_runs(
                    sales_profile_run_id,source_run_id,as_of_at,status,model,
                    prompt_version,profile_schema_version,sampling_version,
                    message_snapshot_id,order_snapshot_id,aux_snapshot_id,cohort_hash,
                    config_json,counts_json,quality_json,created_at
                ) VALUES(?,?,?,'prepared',?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    run_id,
                    source_run_id,
                    cutoff.isoformat(timespec="seconds"),
                    model,
                    "%s+%s" % (EXTRACTION_PROMPT_VERSION, PROFILE_PROMPT_VERSION),
                    PROFILE_SCHEMA_VERSION,
                    SAMPLING_VERSION,
                    message_snapshot_id,
                    str(order_row["order_snapshot_id"]),
                    aux_snapshot_id,
                    cohort_hash,
                    json_dumps(
                        {
                            "quotas": dict(DEFAULT_STRATUM_QUOTAS),
                            "automatic_send": False,
                            "contact_warning": "联系前核对最新状态",
                        }
                    ),
                    json_dumps(counts),
                    "{}",
                    now,
                ),
            )
            for item in selected:
                subject_id = hmac_id(actual_secret, "sales-profile-subject", run_id, item.customer_key)
                sales_profile_id = hmac_id(actual_secret, "sales-profile", run_id, item.customer_key)
                connection.execute(
                    """
                    INSERT INTO sales_profile_subjects(
                        subject_id,sales_profile_run_id,customer_key,profile_id,phone_hmac,
                        stratum,stratum_rank,feature_snapshot_id,feature_payload_json,
                        feature_freshness_json,selection_reason_json,status,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,'prepared',?,?)
                    """,
                    (
                        subject_id,
                        run_id,
                        item.customer_key,
                        item.profile_id,
                        item.phone_hmac,
                        item.stratum,
                        item.stratum_rank,
                        item.feature_snapshot_id,
                        feature_payloads.get(item.feature_snapshot_id or "", ("{}", "{}"))[0],
                        feature_payloads.get(item.feature_snapshot_id or "", ("{}", "{}"))[1],
                        json_dumps(dict(item.selection_reason)),
                        now,
                        now,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO sales_profiles(
                        sales_profile_id,subject_id,status,model,prompt_version,
                        profile_schema_version,created_at,updated_at
                    ) VALUES(?,?,'pending',?,?,?,?,?)
                    """,
                    (
                        sales_profile_id,
                        subject_id,
                        model,
                        PROFILE_PROMPT_VERSION,
                        PROFILE_SCHEMA_VERSION,
                        now,
                        now,
                    ),
                )
        quality_after = connection.execute(
            "SELECT quality_json FROM pipeline_runs WHERE run_id=?", (source_run_id,)
        ).fetchone()[0]
        if str(quality_after) != quality_before:
            raise RuntimeError("sales profile preparation changed M0 acceptance metadata")
        result["idempotent"] = False
        return result
    finally:
        connection.close()


__all__ = [
    "DEFAULT_AS_OF_AT",
    "DEFAULT_MODEL",
    "DEFAULT_SOURCE_RUN_ID",
    "EXTRACTION_PROMPT_VERSION",
    "PROFILE_PROMPT_VERSION",
    "PROFILE_SCHEMA_VERSION",
    "load_sampling_candidates",
    "prepare_sales_profile_pilot",
    "refresh_sales_profile_features",
]
