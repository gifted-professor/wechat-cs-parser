"""Read-only recovery of raw conversations for frozen sales-profile subjects."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterable, Mapping, Sequence, Tuple

from .core import content_digest, hmac_id
from .live_inbox import (
    SHANGHAI,
    _event_signature,
    _load_accounts,
    _parse_local_timestamp,
    normalize_raw_wechat_id,
)
from .source_snapshot import read_stable_bytes


TURN_MERGE_SECONDS = 15 * 60


@dataclass(frozen=True)
class RawSalesMessage:
    message_key: str
    customer_key: str
    profile_id: str
    role: str
    timestamp: str
    text: str
    event_id: str
    source_ordinal: int

    @property
    def at(self) -> datetime:
        return datetime.fromisoformat(self.timestamp)


@dataclass(frozen=True)
class RawConversationSnapshot:
    source_hash: str
    messages_by_customer: Mapping[str, Tuple[RawSalesMessage, ...]]
    missing_customer_keys: Tuple[str, ...]
    scanned_record_count: int


def _as_of(value: object) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError("sales profile as_of_at must be a valid ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError("sales profile as_of_at must include a timezone")
    return parsed.astimezone(SHANGHAI)


def load_raw_sales_conversations(
    events_path: Path,
    accounts_path: Path,
    *,
    customer_keys: Iterable[str],
    as_of_at: object,
    secret: str,
    snapshot_size: int | None = None,
    snapshot_sha256: str | None = None,
    account_config_sha256: str | None = None,
) -> RawConversationSnapshot:
    """Rescan live-inbox and retain raw text only for explicitly selected customers."""

    selected = frozenset(str(item) for item in customer_keys)
    if not selected:
        raise ValueError("at least one sales profile customer is required")
    cutoff = _as_of(as_of_at)
    event_file = read_stable_bytes(Path(events_path))
    account_file = read_stable_bytes(Path(accounts_path))
    if account_config_sha256 and account_file.sha256 != account_config_sha256:
        raise RuntimeError("sales profile account config does not match the frozen run")
    event_data = event_file.data
    source_hash = event_file.sha256
    if snapshot_size is not None or snapshot_sha256 is not None:
        if snapshot_size is None or snapshot_sha256 is None:
            raise ValueError("snapshot_size and snapshot_sha256 must be provided together")
        if snapshot_size < 0 or len(event_data) < snapshot_size:
            raise RuntimeError("sales profile raw source is shorter than the frozen snapshot")
        event_data = event_data[:snapshot_size]
        source_hash = hashlib.sha256(event_data).hexdigest()
        if source_hash != snapshot_sha256:
            raise RuntimeError("sales profile raw source does not match the frozen snapshot")
    accounts = _load_accounts(account_file)
    seen_events: Dict[str, str] = {}
    output: Dict[str, list[RawSalesMessage]] = {key: [] for key in selected}
    lines = event_data.splitlines()

    for source_ordinal, raw_line in enumerate(lines, start=1):
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        event_id = str(row.get("event_id") or "").strip()
        if not event_id:
            continue
        signature = _event_signature(row)
        if event_id in seen_events:
            continue
        seen_events[event_id] = signature
        profile_id = str(row.get("account_profile") or "").strip()
        account = accounts.get(profile_id)
        if account is None or row.get("chat_type") != "private" or row.get("message_type") != "文本":
            continue
        text = str(row.get("text") or "").replace("\x00", "").strip()
        if not text:
            continue
        sender = str(row.get("sender") or "")
        if not sender:
            role = "customer"
        elif sender == account.self_sender:
            role = "studio"
        else:
            continue
        try:
            epoch = float(row.get("message_timestamp"))
            at = datetime.fromtimestamp(epoch, tz=SHANGHAI)
            display_at = _parse_local_timestamp(row.get("message_time"))
            raw_wechat_id = normalize_raw_wechat_id(row.get("conversation_id"))
        except (TypeError, ValueError, OSError, OverflowError):
            continue
        if abs((at - display_at).total_seconds()) > 60 or at > cutoff:
            continue
        customer_key = hmac_id(
            secret, "customer", account.canonical_account_id, raw_wechat_id
        )
        if customer_key not in selected:
            continue
        timestamp = at.isoformat(timespec="seconds")
        message_key = hmac_id(
            secret,
            "message",
            event_id,
            timestamp,
            content_digest(text),
        )
        output[customer_key].append(
            RawSalesMessage(
                message_key=message_key,
                customer_key=customer_key,
                profile_id=profile_id,
                role=role,
                timestamp=timestamp,
                text=text,
                event_id=event_id,
                source_ordinal=source_ordinal,
            )
        )

    materialized = {}
    for customer_key, rows in output.items():
        if not rows:
            continue
        materialized[customer_key] = tuple(
            sorted(rows, key=lambda item: (item.timestamp, item.source_ordinal, item.message_key))
        )
    missing = tuple(sorted(selected - set(materialized)))
    return RawConversationSnapshot(
        source_hash=source_hash,
        messages_by_customer=materialized,
        missing_customer_keys=missing,
        scanned_record_count=len(lines),
    )


def _turns(messages: Sequence[RawSalesMessage]) -> Tuple[Tuple[RawSalesMessage, ...], ...]:
    turns: list[list[RawSalesMessage]] = []
    for item in sorted(messages, key=lambda row: (row.timestamp, row.source_ordinal, row.message_key)):
        if (
            turns
            and turns[-1][-1].role == item.role
            and 0 <= (item.at - turns[-1][-1].at).total_seconds() <= TURN_MERGE_SECONDS
        ):
            turns[-1].append(item)
        else:
            turns.append([item])
    return tuple(tuple(turn) for turn in turns)


def chunk_raw_messages(
    messages: Sequence[RawSalesMessage],
    *,
    max_chars: int = 24_000,
    max_messages: int = 300,
) -> Tuple[Tuple[RawSalesMessage, ...], ...]:
    """Time-slice messages without splitting one conversation turn."""

    if max_chars <= 0 or max_messages <= 0:
        raise ValueError("sales profile chunk limits must be positive")
    chunks: list[Tuple[RawSalesMessage, ...]] = []
    current: list[RawSalesMessage] = []
    current_chars = 0
    for turn in _turns(messages):
        turn_chars = sum(len(item.text) for item in turn)
        would_exceed = bool(
            current
            and (
                len(current) + len(turn) > max_messages
                or current_chars + turn_chars > max_chars
            )
        )
        if would_exceed:
            chunks.append(tuple(current))
            current = []
            current_chars = 0
        current.extend(turn)
        current_chars += turn_chars
    if current:
        chunks.append(tuple(current))
    return tuple(chunks)


__all__ = [
    "RawConversationSnapshot",
    "RawSalesMessage",
    "chunk_raw_messages",
    "load_raw_sales_conversations",
]
