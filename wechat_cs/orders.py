"""Read-only normalization of dashboard order envelopes into M0 facts."""

from __future__ import annotations

import json
import os
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Dict, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

from .core import DEFAULT_HMAC_SECRET, hmac_id, json_dumps, redact_text
from .identity import global_phone_hmac, normalize_phone
from .source_snapshot import hmac_key_fingerprint, read_stable_bytes
from .store import initialize_schema, open_store


SHANGHAI = ZoneInfo("Asia/Shanghai")
ORDER_RULE_VERSION = "m0-order-v2"
SOURCE_NAMESPACE = "dashboard-orders-live"
_RETURN_TYPES = {"return", "return_taro"}
_OPEN_RETURN_STATES = {"处理中", "待处理", "未完成", "进行中", "open", "pending"}


@dataclass(frozen=True)
class CanonicalOrder:
    order_line_id: str
    source_namespace: str
    record_id: str
    phone_hmac: Optional[str]
    paid_on: Optional[str]
    revenue_minor: Optional[int]
    currency: str
    platform: Optional[str]
    refund_type: Optional[str]
    refund_reason: Optional[str]
    refund_amount_minor: Optional[int]
    refund_on: Optional[str]
    return_status: Optional[str]
    source_hash: str
    quality_flags: Tuple[str, ...]


def _text(value: object) -> Optional[str]:
    text = str(value or "").strip()
    return text or None


def parse_synced_at(value: object) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("orders synced_at must be a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("orders synced_at must include a timezone")
    return parsed.astimezone(SHANGHAI)


def _source_date(value: object) -> tuple[Optional[date], Optional[str]]:
    if value is None or str(value).strip() == "":
        return None, None
    text = str(value).strip().replace("/", "-")
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        try:
            parsed_date = date.fromisoformat(text[:10])
        except ValueError:
            return None, "invalid"
    else:
        parsed_date = parsed.astimezone(SHANGHAI).date() if parsed.tzinfo else parsed.date()
    if parsed_date.year <= 1970:
        return None, "epoch_placeholder"
    return parsed_date, None


def _money_minor(value: object) -> tuple[Optional[int], Optional[str]]:
    if value is None or str(value).strip() == "":
        return None, None
    try:
        amount = Decimal(str(value).replace(",", "").strip())
    except InvalidOperation:
        return None, "invalid"
    if not amount.is_finite():
        return None, "invalid"
    minor = int((amount * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    return minor, None


def normalize_refund_type(value: object) -> Optional[str]:
    text = str(value or "").strip()
    if not text or text in {"0", "正常", "无"}:
        return None
    if text == "取消":
        return "cancel"
    if text == "退":
        return "return"
    if text == "退芋圆":
        return "return_taro"
    if text == "换":
        return "exchange"
    if text == "补":
        return "compensation"
    return "other"


def _scrub_refund_reason(value: object, row: Dict[str, object]) -> Optional[str]:
    reason = _text(value)
    if reason is None:
        return None
    replacements = (
        ("customer_name", "[客户姓名]"),
        ("address", "[地址]"),
        ("phone", "[手机号]"),
        ("tracking_no", "[单号]"),
        ("customer_info", "[客户信息]"),
    )
    for field, placeholder in replacements:
        token = _text(row.get(field))
        if token and len(token) >= 2:
            reason = reason.replace(token, placeholder)
    return redact_text(reason)[0]


def normalize_order(
    row: Dict[str, object],
    *,
    synced_at: datetime,
    secret: str,
    source_hash: str,
    source_namespace: str = SOURCE_NAMESPACE,
) -> CanonicalOrder:
    record_id = str(row.get("record_id") or "").strip()
    if not record_id:
        raise ValueError("order record_id is required")
    flags = set()
    paid_date, paid_date_error = _source_date(row.get("pay_date"))
    revenue_minor, revenue_error = _money_minor(row.get("revenue"))
    if paid_date_error:
        flags.add("invalid_paid_on")
    if revenue_error:
        flags.add("invalid_revenue")
    if paid_date and paid_date > synced_at.date():
        flags.add("future_paid_on")
        paid_date = None
    valid_payment = paid_date is not None and revenue_minor is not None and revenue_minor > 0
    if not valid_payment:
        if paid_date is not None or (revenue_minor is not None and revenue_minor > 0):
            flags.add("incomplete_customer_payment")
        paid_on = None
        normalized_revenue = None
    else:
        paid_on = paid_date.isoformat()
        normalized_revenue = revenue_minor

    refund_type = normalize_refund_type(row.get("refund_type"))
    refund_date, refund_date_error = _source_date(row.get("refund_date"))
    refund_minor, refund_error = _money_minor(row.get("refund_amount"))
    if refund_date_error:
        flags.add("invalid_refund_on")
    if refund_error or (refund_minor is not None and refund_minor < 0):
        flags.add("invalid_refund_amount")
        refund_minor = None
    if refund_date and refund_date > synced_at.date():
        flags.add("future_refund_on")
    if refund_type in _RETURN_TYPES:
        if refund_date is None:
            flags.add("missing_refund_on")
        if refund_minor is None:
            flags.add("missing_refund_amount")
    if refund_minor is not None and normalized_revenue is not None and refund_minor > normalized_revenue:
        flags.add("refund_exceeds_revenue")
    return_status = _text(row.get("return_status"))
    if return_status and return_status.lower() in _OPEN_RETURN_STATES:
        flags.add("aftersale_open")

    phone = normalize_phone(row.get("phone"))
    phone_hmac = global_phone_hmac(secret, phone) if phone else None
    refund_reason = _scrub_refund_reason(row.get("refund_reason"), row)
    return CanonicalOrder(
        order_line_id=hmac_id(secret, "order-line", source_namespace, record_id),
        source_namespace=source_namespace,
        record_id=record_id,
        phone_hmac=phone_hmac,
        paid_on=paid_on,
        revenue_minor=normalized_revenue,
        currency="CNY",
        platform=_text(row.get("platform")),
        refund_type=refund_type,
        refund_reason=refund_reason,
        refund_amount_minor=refund_minor,
        refund_on=refund_date.isoformat() if refund_date else None,
        return_status=return_status,
        source_hash=source_hash,
        quality_flags=tuple(sorted(flags)),
    )


def _quality_summary(
    orders: Sequence[CanonicalOrder],
    *,
    source_records: int,
    quarantined: int,
    envelope_total: object,
) -> Dict[str, object]:
    flags: Counter[str] = Counter()
    refund_types: Counter[str] = Counter()
    for order in orders:
        flags.update(order.quality_flags)
        refund_types[order.refund_type or "none"] += 1
    return {
        "source_records": source_records,
        "accepted_records": len(orders),
        "quarantined_records": quarantined,
        "envelope_total": envelope_total,
        "envelope_total_matches": envelope_total == source_records,
        "customer_paid_records": sum(order.paid_on is not None for order in orders),
        "phone_hmac_records": sum(order.phone_hmac is not None for order in orders),
        "refund_type_counts": dict(sorted(refund_types.items())),
        "quality_flag_counts": dict(sorted(flags.items())),
    }


def import_orders(
    db_path: Path,
    orders_path: Path,
    *,
    secret: Optional[str] = None,
    source_namespace: str = SOURCE_NAMESPACE,
) -> Dict[str, object]:
    actual_secret = secret or os.environ.get("WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET)
    stable = read_stable_bytes(Path(orders_path))
    try:
        document = json.loads(stable.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("orders source must be valid UTF-8 JSON") from exc
    if not isinstance(document, dict) or not isinstance(document.get("records"), list):
        raise ValueError("orders source must contain a records envelope")
    synced_at = parse_synced_at(document.get("synced_at"))
    records = document["records"]
    seen_record_ids = set()
    normalized = []
    quarantined = 0
    for row in records:
        if not isinstance(row, dict):
            quarantined += 1
            continue
        record_id = str(row.get("record_id") or "").strip()
        if not record_id or record_id in seen_record_ids:
            quarantined += 1
            continue
        seen_record_ids.add(record_id)
        normalized.append(
            normalize_order(
                row,
                synced_at=synced_at,
                secret=actual_secret,
                source_hash=stable.sha256,
                source_namespace=source_namespace,
            )
        )
    quality = _quality_summary(
        normalized,
        source_records=len(records),
        quarantined=quarantined,
        envelope_total=document.get("total_records"),
    )
    if len(normalized) + quarantined != len(records):
        raise RuntimeError("order normalization accounting mismatch")

    connection = open_store(str(Path(db_path).expanduser().resolve()))
    try:
        initialize_schema(connection)
        run = connection.execute(
            "SELECT run_id,hmac_key_fingerprint FROM pipeline_runs "
            "ORDER BY started_at DESC,run_id DESC LIMIT 1"
        ).fetchone()
        if run is None:
            raise RuntimeError("order import requires an initialized pipeline run")
        if run["hmac_key_fingerprint"] != hmac_key_fingerprint(actual_secret):
            raise RuntimeError("HMAC key fingerprint mismatch")
        snapshot_id = hmac_id(actual_secret, "source-snapshot", "orders", stable.sha256)
        order_snapshot_id = hmac_id(
            actual_secret,
            "order-snapshot",
            source_namespace,
            stable.sha256,
            synced_at.isoformat(timespec="seconds"),
            ORDER_RULE_VERSION,
        )
        active = connection.execute(
            "SELECT order_snapshot_id,quality_json FROM order_snapshots WHERE state='active'"
        ).fetchone()
        if active is not None and active["order_snapshot_id"] == order_snapshot_id:
            return {
                "order_snapshot_id": order_snapshot_id,
                "source_hash": stable.sha256,
                "synced_at": synced_at.isoformat(timespec="seconds"),
                "state": "active",
                "idempotent": True,
                "quality": json.loads(active["quality_json"]),
            }
        captured_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with connection:
            connection.execute(
                "UPDATE pipeline_runs SET order_rule_version=? WHERE run_id=?",
                (ORDER_RULE_VERSION, run["run_id"]),
            )
            connection.execute("DELETE FROM card_outcomes")
            connection.execute("DELETE FROM order_snapshots")
            connection.execute(
                """
                INSERT OR IGNORE INTO source_snapshots(
                    snapshot_id,run_id,source_kind,source_path_hash,device,inode,size,
                    mtime_ns,sha256,record_count,first_at,last_at,observed_until,
                    captured_at,consistency_state,quality_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    snapshot_id,
                    run["run_id"],
                    "orders_live",
                    hmac_id(actual_secret, "source-path", "orders_live"),
                    stable.device,
                    stable.inode,
                    stable.size,
                    stable.mtime_ns,
                    stable.sha256,
                    len(records),
                    None,
                    None,
                    synced_at.isoformat(timespec="seconds"),
                    captured_at,
                    "consistent",
                    json_dumps({"envelope_total_matches": quality["envelope_total_matches"]}),
                ),
            )
            connection.execute(
                "INSERT INTO order_snapshots(order_snapshot_id,source_snapshot_id,synced_at,record_count,state,quality_json) "
                "VALUES(?,?,?,?,?,?)",
                (
                    order_snapshot_id,
                    snapshot_id,
                    synced_at.isoformat(timespec="seconds"),
                    len(normalized),
                    "staging",
                    json_dumps(quality),
                ),
            )
            connection.executemany(
                """
                INSERT INTO orders(
                    order_line_id,order_snapshot_id,source_namespace,record_id,phone_hmac,
                    paid_on,revenue_minor,currency,platform,refund_type,refund_reason,
                    refund_amount_minor,refund_on,return_status,source_hash,quality_flags_json
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                [
                    (
                        order.order_line_id,
                        order_snapshot_id,
                        order.source_namespace,
                        order.record_id,
                        order.phone_hmac,
                        order.paid_on,
                        order.revenue_minor,
                        order.currency,
                        order.platform,
                        order.refund_type,
                        order.refund_reason,
                        order.refund_amount_minor,
                        order.refund_on,
                        order.return_status,
                        order.source_hash,
                        json_dumps(list(order.quality_flags)),
                    )
                    for order in normalized
                ],
            )
            connection.execute(
                "UPDATE order_snapshots SET state='active' WHERE order_snapshot_id=?",
                (order_snapshot_id,),
            )
        return {
            "order_snapshot_id": order_snapshot_id,
            "source_hash": stable.sha256,
            "synced_at": synced_at.isoformat(timespec="seconds"),
            "state": "active",
            "idempotent": False,
            "quality": quality,
        }
    finally:
        connection.close()
