"""Strict, read-only adapter for the GPFS live-inbox JSONL format."""

from __future__ import annotations

import json
import unicodedata
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

from .core import Message, content_digest, hmac_id, redact_text
from .source_snapshot import StableBytes, read_stable_bytes


SHANGHAI = ZoneInfo("Asia/Shanghai")


@dataclass(frozen=True)
class AccountConfig:
    profile_id: str
    canonical_account_id: str
    self_sender: str
    binding_account_alias: str
    state: str


@dataclass(frozen=True)
class ConversationRef:
    customer_key: str
    profile_id: str
    canonical_account_id: str
    raw_wechat_id: str
    raw_wechat_id_hash: str


@dataclass(frozen=True)
class RoleEvidence:
    message_key: str
    source_kind: str
    profile_id: str
    evidence_type: str
    evidence_value: str


@dataclass
class SourceSnapshot:
    messages: List[Message]
    messages_by_customer: Dict[str, List[Message]]
    conversations: Dict[str, ConversationRef]
    role_evidence: Dict[str, RoleEvidence]
    accounts: Dict[str, AccountConfig]
    first_at: Optional[datetime]
    last_at: Optional[datetime]
    observed_until_by_profile: Dict[str, Optional[datetime]]
    event_source_hash: str
    state_source_hash: str
    account_config_hash: str
    quarantine_counts: Dict[str, int]
    consistency_state: str
    event_file: StableBytes
    state_file: StableBytes
    account_file: StableBytes
    event_record_count: int


def normalize_raw_wechat_id(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", str(value or "")).strip()
    if not normalized or any(ord(character) < 32 or ord(character) == 127 for character in normalized):
        raise ValueError("invalid raw WeChat ID")
    return normalized


def _parse_local_timestamp(value: object) -> datetime:
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=SHANGHAI)
    return parsed.astimezone(SHANGHAI)


def _load_accounts(stable: StableBytes) -> Dict[str, AccountConfig]:
    try:
        document = json.loads(stable.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("accounts config must be valid UTF-8 JSON") from exc
    rows = document.get("accounts") if isinstance(document, dict) else None
    if not isinstance(rows, dict) or not rows:
        raise ValueError("accounts config must contain a non-empty accounts object")
    output: Dict[str, AccountConfig] = {}
    canonical_ids = set()
    for profile_id, raw in rows.items():
        if not isinstance(raw, dict):
            raise ValueError("each account config entry must be an object")
        profile = str(profile_id or "").strip()
        canonical = str(raw.get("canonical_account_id") or "").strip()
        self_sender = normalize_raw_wechat_id(raw.get("self_sender"))
        state = str(raw.get("state") or "review").strip()
        if not profile or not canonical:
            raise ValueError("account profile and canonical account ID are required")
        if canonical in canonical_ids:
            raise ValueError("canonical account IDs must be unique")
        if state not in ("approved", "review", "rejected"):
            raise ValueError("account state must be approved, review, or rejected")
        canonical_ids.add(canonical)
        output[profile] = AccountConfig(
            profile_id=profile,
            canonical_account_id=canonical,
            self_sender=self_sender,
            binding_account_alias=str(raw.get("binding_account_alias") or "").strip(),
            state=state,
        )
    return output


def _event_signature(row: dict) -> str:
    return json.dumps(row, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _scrub_source_identifiers(
    text: str,
    *,
    raw_wechat_id: str,
    accounts: Dict[str, AccountConfig],
) -> str:
    """Remove collector identifiers even when chat text repeats them verbatim."""

    result = text.replace(raw_wechat_id, "[客户标识]")
    for token in sorted(
        {account.self_sender for account in accounts.values()},
        key=lambda value: (-len(value), value),
    ):
        result = result.replace(token, "[微信号]")
    return result


def load_live_inbox(
    events_path: Path,
    accounts_path: Path,
    *,
    secret: str,
    state_path: Optional[Path] = None,
) -> SourceSnapshot:
    events = Path(events_path).expanduser().resolve()
    state = Path(state_path or (events.parent / "state.json")).expanduser().resolve()
    accounts_file = Path(accounts_path).expanduser().resolve()
    event_file = read_stable_bytes(events)
    state_file = read_stable_bytes(state)
    account_file = read_stable_bytes(accounts_file)
    account_config = _load_accounts(account_file)
    try:
        state_document = json.loads(state_file.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("state file must be valid UTF-8 JSON") from exc
    if not isinstance(state_document, dict):
        raise ValueError("state file must contain an object")

    quarantine: Counter[str] = Counter()
    seen_events: Dict[str, str] = {}
    messages: List[Message] = []
    by_customer: Dict[str, List[Message]] = defaultdict(list)
    conversations: Dict[str, ConversationRef] = {}
    role_evidence: Dict[str, RoleEvidence] = {}
    last_message_by_profile: Dict[str, datetime] = {}
    first_at: Optional[datetime] = None
    last_at: Optional[datetime] = None
    lines = event_file.data.splitlines()
    for source_ordinal, raw_line in enumerate(lines, 1):
        if not raw_line.strip():
            quarantine["blank_line"] += 1
            continue
        try:
            row = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            quarantine["invalid_json"] += 1
            continue
        if not isinstance(row, dict):
            quarantine["invalid_record"] += 1
            continue
        event_id = str(row.get("event_id") or "").strip()
        if not event_id:
            quarantine["missing_event_id"] += 1
            continue
        signature = _event_signature(row)
        if event_id in seen_events:
            quarantine[
                "duplicate_exact" if seen_events[event_id] == signature else "duplicate_conflict"
            ] += 1
            continue
        seen_events[event_id] = signature

        profile_id = str(row.get("account_profile") or "").strip()
        account = account_config.get(profile_id)
        if account is None:
            quarantine["unknown_profile"] += 1
            continue
        if row.get("chat_type") != "private":
            quarantine["non_private"] += 1
            continue
        if row.get("message_type") != "文本":
            quarantine["non_text"] += 1
            continue
        text = str(row.get("text") or "").replace("\x00", "").strip()
        if not text:
            quarantine["empty_text"] += 1
            continue
        sender = str(row.get("sender") or "")
        if not sender:
            role = "customer"
            evidence_value = "empty_sender"
        elif sender == account.self_sender:
            role = "studio"
            evidence_value = "configured_self_sender"
        else:
            quarantine["unknown_sender"] += 1
            continue
        try:
            epoch = float(row.get("message_timestamp"))
            at = datetime.fromtimestamp(epoch, tz=SHANGHAI)
            display_at = _parse_local_timestamp(row.get("message_time"))
        except (TypeError, ValueError, OSError, OverflowError):
            quarantine["invalid_timestamp"] += 1
            continue
        if abs((at - display_at).total_seconds()) > 60:
            quarantine["timestamp_mismatch"] += 1
            continue
        try:
            raw_wechat_id = normalize_raw_wechat_id(row.get("conversation_id"))
        except ValueError:
            quarantine["invalid_conversation_id"] += 1
            continue
        customer_key = hmac_id(
            secret, "customer", account.canonical_account_id, raw_wechat_id
        )
        raw_wechat_id_hash = hmac_id(
            secret,
            "raw-wechat-id",
            account.canonical_account_id,
            raw_wechat_id,
        )
        redacted, _ = redact_text(
            _scrub_source_identifiers(
                text,
                raw_wechat_id=raw_wechat_id,
                accounts=account_config,
            )
        )
        timestamp = at.isoformat(timespec="seconds")
        message_key = hmac_id(
            secret,
            "message",
            event_id,
            timestamp,
            content_digest(text),
        )
        message = Message(
            message_key=message_key,
            customer_key=customer_key,
            role=role,
            timestamp=timestamp,
            text=redacted,
            source_file=events.name,
            source_ordinal=source_ordinal,
        )
        messages.append(message)
        by_customer[customer_key].append(message)
        conversations[customer_key] = ConversationRef(
            customer_key=customer_key,
            profile_id=profile_id,
            canonical_account_id=account.canonical_account_id,
            raw_wechat_id=raw_wechat_id,
            raw_wechat_id_hash=raw_wechat_id_hash,
        )
        role_evidence[message_key] = RoleEvidence(
            message_key=message_key,
            source_kind="live-inbox",
            profile_id=profile_id,
            evidence_type="profile_sender_match",
            evidence_value=evidence_value,
        )
        last_message_by_profile[profile_id] = max(
            at, last_message_by_profile.get(profile_id, at)
        )
        first_at = at if first_at is None or at < first_at else first_at
        last_at = at if last_at is None or at > last_at else last_at

    observed_until: Dict[str, Optional[datetime]] = {}
    consistency_state = "consistent"
    state_accounts = state_document.get("accounts")
    if not isinstance(state_accounts, dict):
        state_accounts = {}
    try:
        global_success = _parse_local_timestamp(state_document.get("last_success_at"))
    except (TypeError, ValueError):
        global_success = None
        quarantine["invalid_global_observation"] += 1
        consistency_state = "degraded"
    for profile_id in sorted(account_config):
        row = state_accounts.get(profile_id)
        boundary: Optional[datetime] = None
        if not isinstance(row, dict):
            quarantine["missing_profile_observation"] += 1
        elif row.get("initialized") is not True:
            quarantine["profile_not_initialized"] += 1
        elif str(row.get("last_error") or "").strip():
            quarantine["profile_observation_error"] += 1
        else:
            try:
                boundary = _parse_local_timestamp(row.get("last_poll_at"))
            except (TypeError, ValueError):
                quarantine["invalid_profile_observation"] += 1
        profile_last = last_message_by_profile.get(profile_id)
        if boundary is not None and profile_last is not None and boundary < profile_last:
            quarantine["profile_observation_before_message"] += 1
            boundary = None
        if boundary is not None and global_success is not None:
            if abs((boundary - global_success).total_seconds()) > 86400:
                quarantine["profile_global_observation_skew"] += 1
                consistency_state = "degraded"
        if boundary is None:
            consistency_state = "degraded"
        observed_until[profile_id] = boundary

    for rows in by_customer.values():
        rows.sort(key=lambda item: (item.timestamp, item.source_ordinal, item.message_key))
    messages.sort(key=lambda item: (item.timestamp, item.source_ordinal, item.message_key))
    return SourceSnapshot(
        messages=messages,
        messages_by_customer=dict(by_customer),
        conversations=conversations,
        role_evidence=role_evidence,
        accounts=account_config,
        first_at=first_at,
        last_at=last_at,
        observed_until_by_profile=observed_until,
        event_source_hash=event_file.sha256,
        state_source_hash=state_file.sha256,
        account_config_hash=account_file.sha256,
        quarantine_counts=dict(sorted(quarantine.items())),
        consistency_state=consistency_state,
        event_file=event_file,
        state_file=state_file,
        account_file=account_file,
        event_record_count=len(lines),
    )
