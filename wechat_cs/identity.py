"""Deterministic account-scoped WeChat-to-phone identity bridge."""

from __future__ import annotations

import csv
import io
import json
import os
import re
import hashlib
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Dict, Iterable, Iterator, Mapping, Optional, Sequence, Tuple

from .core import DEFAULT_HMAC_SECRET, extract_mainland_phones, hmac_id
from .live_inbox import normalize_raw_wechat_id
from .source_snapshot import hmac_key_fingerprint, read_stable_bytes
from .store import initialize_schema, open_store


APPROVAL_CONFIDENCE = Decimal("0.95")
IDENTITY_VERSION = "identity-v1"
FEISHU_IDENTITY_VERSION = "identity-feishu-v1"
ORDER_ELIGIBILITY_VERSION = "order-eligibility-v1"
_MAINLAND_PHONE = re.compile(r"1[3-9]\d{9}")
_NUMERIC_TRACKING = re.compile(r"(?<!\d)\d{10,24}(?!\d)")
_ALPHANUM_TRACKING = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,5}\d{7,24}(?![A-Za-z0-9])")


@dataclass(frozen=True)
class BindingCandidate:
    canonical_account_id: str
    raw_wechat_id: str
    phone_hmac: Optional[str]
    confidence: Decimal
    state: str
    match_method: str


@dataclass
class BindingLoad:
    bindings: Dict[Tuple[str, str], BindingCandidate]
    stats: Dict[str, object]
    source_hash: str

    def __getitem__(self, key: Tuple[str, str]) -> BindingCandidate:
        return self.bindings[key]

    def __iter__(self) -> Iterator[Tuple[str, str]]:
        return iter(self.bindings)

    def items(self):
        return self.bindings.items()


def normalize_phone(value: str) -> Optional[str]:
    text = str(value or "").strip()
    text = re.sub(r"[\s()（）-]+", "", text)
    if text.startswith("+86"):
        text = text[3:]
    elif text.startswith("0086"):
        text = text[4:]
    return text if _MAINLAND_PHONE.fullmatch(text) else None


def global_phone_hmac(secret: str, phone: str) -> str:
    normalized = normalize_phone(phone)
    if normalized is None:
        raise ValueError("invalid phone")
    return hmac_id(secret, "phone", normalized)


def classify_order_eligibility(conversation_name: object) -> str:
    """Return the non-PII order eligibility enum for a conversation label."""

    value = str(conversation_name or "")
    if "下单客户" in value:
        return "order_customer"
    if "相册客户" in value:
        return "album_customer"
    return "order_ineligible"


def normalize_tracking(value: object) -> Optional[str]:
    normalized = re.sub(r"[\s\-]+", "", str(value or "").strip()).upper()
    if not re.fullmatch(r"[A-Z0-9]{8,30}", normalized):
        return None
    if normalize_phone(normalized) is not None:
        return None
    if sum(character.isdigit() for character in normalized) < 7:
        return None
    return normalized


def extract_tracking_numbers(text: object) -> list[str]:
    value = str(text or "")
    output = set()
    for match in list(_NUMERIC_TRACKING.findall(value)) + list(_ALPHANUM_TRACKING.findall(value)):
        normalized = normalize_tracking(match)
        if normalized:
            output.add(normalized)
    return sorted(
        token
        for token in output
        if not any(token != other and token in other for other in output)
    )


def _combined_source_hash(parts: Iterable[str]) -> str:
    digest = hashlib.sha256()
    for part in parts:
        digest.update(str(part).encode("ascii"))
        digest.update(b"\0")
    return digest.hexdigest()


def _load_order_identity_index(
    order_paths: Sequence[Path],
) -> tuple[set[str], Dict[str, set[str]], list[str]]:
    phones: set[str] = set()
    tracking_to_phones: Dict[str, set[str]] = defaultdict(set)
    source_hashes: list[str] = []
    for path in order_paths:
        stable = read_stable_bytes(Path(path))
        source_hashes.append(stable.sha256)
        try:
            document = json.loads(stable.data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("order identity source must be valid UTF-8 JSON") from exc
        records = document.get("records") if isinstance(document, dict) else None
        if not isinstance(records, list):
            raise ValueError("order identity source must contain records")
        for row in records:
            if not isinstance(row, dict):
                continue
            phone = normalize_phone(row.get("phone"))
            if phone is None:
                continue
            phones.add(phone)
            tracking = normalize_tracking(row.get("tracking_no"))
            if tracking:
                tracking_to_phones[tracking].add(phone)
    return phones, tracking_to_phones, source_hashes


def import_feishu_order_bindings(
    db_path: Path,
    events_path: Path,
    accounts_path: Path,
    order_paths: Sequence[Path],
    *,
    target_profile_id: str = "aolai4",
    secret: Optional[str] = None,
) -> Dict[str, object]:
    """Evaluate order eligibility and add deterministic Feishu-backed links.

    Source names, phone numbers, tracking numbers and raw WeChat IDs are used
    only in memory.  SQLite receives enums and HMAC identifiers only.
    """

    actual_secret = secret or os.environ.get("WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET)
    event_file = read_stable_bytes(Path(events_path))
    account_file = read_stable_bytes(Path(accounts_path))
    try:
        account_document = json.loads(account_file.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("accounts config must be valid UTF-8 JSON") from exc
    account_rows = account_document.get("accounts") if isinstance(account_document, dict) else None
    if not isinstance(account_rows, dict) or target_profile_id not in account_rows:
        raise ValueError("target profile is not present in accounts config")
    accounts: Dict[str, tuple[str, str]] = {}
    for profile_id, row in account_rows.items():
        if not isinstance(row, dict):
            continue
        canonical = str(row.get("canonical_account_id") or "").strip()
        self_sender = str(row.get("self_sender") or "").strip()
        if canonical and self_sender:
            accounts[str(profile_id)] = (canonical, self_sender)
    if target_profile_id not in accounts:
        raise ValueError("target profile config is incomplete")

    order_phones, tracking_to_phones, order_hashes = _load_order_identity_index(order_paths)
    conversations: Dict[tuple[str, str], Dict[str, object]] = {}
    seen_events: set[str] = set()
    for ordinal, raw_line in enumerate(event_file.data.splitlines(), 1):
        if not raw_line.strip():
            continue
        try:
            row = json.loads(raw_line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(row, dict):
            continue
        event_id = str(row.get("event_id") or "").strip()
        if not event_id or event_id in seen_events:
            continue
        seen_events.add(event_id)
        profile_id = str(row.get("account_profile") or "").strip()
        account = accounts.get(profile_id)
        if account is None or row.get("chat_type") != "private" or row.get("message_type") != "文本":
            continue
        sender = str(row.get("sender") or "")
        if sender not in ("", account[1]):
            continue
        try:
            raw_wechat_id = normalize_raw_wechat_id(row.get("conversation_id"))
        except ValueError:
            continue
        key = (profile_id, raw_wechat_id)
        item = conversations.setdefault(
            key,
            {"canonical": account[0], "eligibility": "order_ineligible", "latest": -1, "phones": set(), "trackings": set()},
        )
        if ordinal >= int(item["latest"]):
            item["latest"] = ordinal
            item["eligibility"] = classify_order_eligibility(row.get("conversation_name"))
        if sender == "":
            text = str(row.get("text") or "")
            item["phones"].update(extract_mainland_phones(text))
            item["trackings"].update(extract_tracking_numbers(text))

    source_hash = _combined_source_hash([event_file.sha256, account_file.sha256, *order_hashes])
    connection = open_store(str(Path(db_path).expanduser().resolve()))
    try:
        initialize_schema(connection)
        run = connection.execute(
            "SELECT run_id,hmac_key_fingerprint,account_config_hash FROM pipeline_runs "
            "ORDER BY started_at DESC,run_id DESC LIMIT 1"
        ).fetchone()
        if run is None:
            raise RuntimeError("Feishu identity import requires an initialized pipeline run")
        if run["hmac_key_fingerprint"] != hmac_key_fingerprint(actual_secret):
            raise RuntimeError("HMAC key fingerprint mismatch")
        if run["account_config_hash"] != account_file.sha256:
            raise RuntimeError("account config hash mismatch")
        references = {
            (row["profile_id"], row["raw_wechat_id_hash"]): dict(row)
            for row in connection.execute(
                "SELECT customer_key,profile_id,raw_wechat_id_hash FROM conversation_refs"
            )
        }
        eligibility_counts: Counter[str] = Counter()
        link_counts: Counter[str] = Counter()
        match_methods: Counter[str] = Counter()
        evaluated_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with connection:
            connection.execute(
                "DELETE FROM conversation_links WHERE version=?", (FEISHU_IDENTITY_VERSION,)
            )
            connection.execute("DELETE FROM conversation_order_eligibility")
            for (profile_id, raw_wechat_id), item in conversations.items():
                canonical = str(item["canonical"])
                raw_hash = hmac_id(actual_secret, "raw-wechat-id", canonical, raw_wechat_id)
                reference = references.get((profile_id, raw_hash))
                if reference is None:
                    continue
                eligibility = str(item["eligibility"])
                eligibility_counts[eligibility] += 1
                connection.execute(
                    "INSERT INTO conversation_order_eligibility(customer_key,eligibility,source_hash,version,evaluated_at) "
                    "VALUES(?,?,?,?,?)",
                    (reference["customer_key"], eligibility, source_hash, ORDER_ELIGIBILITY_VERSION, evaluated_at),
                )
                if profile_id != target_profile_id or eligibility == "order_ineligible":
                    continue

                direct_phones = set(item["phones"])
                direct_order_phones = direct_phones & order_phones
                tracking_sets = [tracking_to_phones[value] for value in item["trackings"] if value in tracking_to_phones]
                ambiguous_tracking = any(len(values) != 1 for values in tracking_sets)
                tracking_phones = set().union(*tracking_sets) if tracking_sets else set()
                state: Optional[str] = None
                method: Optional[str] = None
                phone: Optional[str] = None
                confidence = Decimal("0")
                if len(direct_phones) > 1:
                    state, method = "conflict", "customer_phone_order_conflict"
                elif len(direct_order_phones) == 1:
                    phone = next(iter(direct_order_phones))
                    if ambiguous_tracking or (tracking_phones and tracking_phones != {phone}):
                        state, method, phone = "conflict", "customer_evidence_conflict", None
                    else:
                        state, method, confidence = "approved", "customer_phone_order_exact", Decimal("1")
                elif direct_phones and tracking_phones and direct_phones != tracking_phones:
                    state, method = "conflict", "customer_evidence_conflict"
                elif not direct_phones and not ambiguous_tracking and len(tracking_phones) == 1:
                    state, method, phone, confidence = (
                        "approved",
                        "customer_tracking_order_exact",
                        next(iter(tracking_phones)),
                        Decimal("0.98"),
                    )
                elif ambiguous_tracking or len(tracking_phones) > 1:
                    state, method = "conflict", "customer_tracking_order_conflict"
                if state is None or method is None:
                    continue
                phone_hmac = global_phone_hmac(actual_secret, phone) if phone else None
                link_id = hmac_id(actual_secret, "conversation-link", reference["customer_key"], FEISHU_IDENTITY_VERSION)
                connection.execute(
                    """
                    INSERT INTO conversation_links(
                        link_id,customer_key,profile_id,raw_wechat_id_hash,phone_hmac,
                        match_method,confidence,state,source_hash,version,reviewed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)
                    """,
                    (
                        link_id,
                        reference["customer_key"],
                        profile_id,
                        raw_hash,
                        phone_hmac,
                        method,
                        float(confidence),
                        state,
                        source_hash,
                        FEISHU_IDENTITY_VERSION,
                    ),
                )
                link_counts[state] += 1
                match_methods[method] += 1
        return {
            "source_hash": source_hash,
            "evaluated_conversations": sum(eligibility_counts.values()),
            "eligibility_counts": dict(sorted(eligibility_counts.items())),
            "target_profile": target_profile_id,
            "link_state_counts": dict(sorted(link_counts.items())),
            "match_method_counts": dict(sorted(match_methods.items())),
            "order_source_count": len(order_paths),
        }
    finally:
        connection.close()


def _parse_confidence(value: object) -> Optional[Decimal]:
    try:
        confidence = Decimal(str(value or "").strip())
    except InvalidOperation:
        return None
    return confidence if Decimal("0") <= confidence <= Decimal("1") else None


def load_binding_csv(
    path: Path,
    *,
    registry: Mapping[str, str],
    secret: str,
) -> BindingLoad:
    stable = read_stable_bytes(Path(path))
    try:
        text = stable.data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise ValueError("binding CSV must be UTF-8") from exc
    reader = csv.DictReader(io.StringIO(text, newline=""))
    required = {"账号", "客户手机号", "微信原始ID", "绑定置信度"}
    if not required.issubset(set(reader.fieldnames or [])):
        raise ValueError("binding CSV is missing required columns")

    stats: Dict[str, object] = {
        "total_rows": 0,
        "unknown_accounts": 0,
        "missing_raw_id": 0,
        "invalid_raw_id": 0,
        "invalid_phones": 0,
        "invalid_confidence": 0,
    }
    confidence_counts: Counter[str] = Counter()
    grouped: Dict[Tuple[str, str], list[Tuple[str, Decimal]]] = defaultdict(list)
    for row in reader:
        stats["total_rows"] = int(stats["total_rows"]) + 1
        confidence_text = str(row.get("绑定置信度") or "").strip()
        confidence_counts[confidence_text or "missing"] += 1
        account_alias = str(row.get("账号") or "").strip()
        canonical = registry.get(account_alias)
        if canonical is None:
            stats["unknown_accounts"] = int(stats["unknown_accounts"]) + 1
            continue
        raw_value = str(row.get("微信原始ID") or "")
        if not raw_value.strip():
            stats["missing_raw_id"] = int(stats["missing_raw_id"]) + 1
            continue
        try:
            raw_wechat_id = normalize_raw_wechat_id(raw_value)
        except ValueError:
            stats["invalid_raw_id"] = int(stats["invalid_raw_id"]) + 1
            continue
        phone = normalize_phone(str(row.get("客户手机号") or ""))
        if phone is None:
            stats["invalid_phones"] = int(stats["invalid_phones"]) + 1
            continue
        confidence = _parse_confidence(row.get("绑定置信度"))
        if confidence is None:
            stats["invalid_confidence"] = int(stats["invalid_confidence"]) + 1
            continue
        grouped[(canonical, raw_wechat_id)].append((phone, confidence))

    bindings: Dict[Tuple[str, str], BindingCandidate] = {}
    state_counts: Counter[str] = Counter()
    for key, rows in grouped.items():
        canonical, raw_wechat_id = key
        phones = {phone for phone, _ in rows}
        # Duplicate evidence must never raise confidence.  A lower-confidence
        # duplicate keeps the composite key in review until a human resolves it.
        confidence = min(value for _, value in rows)
        if len(phones) != 1:
            state = "conflict"
            phone_hmac = None
        else:
            phone = next(iter(phones))
            phone_hmac = global_phone_hmac(secret, phone)
            state = "approved" if confidence >= APPROVAL_CONFIDENCE else "review"
        state_counts[state] += 1
        bindings[key] = BindingCandidate(
            canonical_account_id=canonical,
            raw_wechat_id=raw_wechat_id,
            phone_hmac=phone_hmac,
            confidence=confidence,
            state=state,
            match_method="account_raw_exact",
        )
    stats["confidence_counts"] = dict(sorted(confidence_counts.items()))
    stats["state_counts"] = dict(sorted(state_counts.items()))
    stats["candidate_keys"] = len(bindings)
    return BindingLoad(bindings=bindings, stats=stats, source_hash=stable.sha256)


def _registry_from_accounts(path: Path) -> tuple[Dict[str, str], str]:
    stable = read_stable_bytes(Path(path))
    try:
        document = json.loads(stable.data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("accounts config must be valid UTF-8 JSON") from exc
    rows = document.get("accounts") if isinstance(document, dict) else None
    if not isinstance(rows, dict):
        raise ValueError("accounts config must contain accounts")
    registry: Dict[str, str] = {}
    for raw in rows.values():
        if not isinstance(raw, dict) or raw.get("state") != "approved":
            continue
        alias = str(raw.get("binding_account_alias") or "").strip()
        canonical = str(raw.get("canonical_account_id") or "").strip()
        if not alias:
            continue
        if not canonical or alias in registry:
            raise ValueError("binding account aliases must be unique and canonical")
        registry[alias] = canonical
    return registry, stable.sha256


def import_bindings(
    db_path: Path,
    bindings_path: Path,
    accounts_path: Path,
    *,
    secret: Optional[str] = None,
) -> Dict[str, object]:
    actual_secret = secret or os.environ.get("WECHAT_CS_HMAC_SECRET", DEFAULT_HMAC_SECRET)
    registry, account_config_hash = _registry_from_accounts(Path(accounts_path))
    if not registry:
        raise RuntimeError("no approved binding account aliases are configured")
    connection = open_store(str(Path(db_path).expanduser().resolve()))
    try:
        run = connection.execute(
            "SELECT run_id,hmac_key_fingerprint,account_config_hash FROM pipeline_runs "
            "ORDER BY started_at DESC,run_id DESC LIMIT 1"
        ).fetchone()
        if run is None:
            raise RuntimeError("binding import requires an initialized pipeline run")
        if run["hmac_key_fingerprint"] != hmac_key_fingerprint(actual_secret):
            raise RuntimeError("HMAC key fingerprint mismatch")
        if run["account_config_hash"] != account_config_hash:
            raise RuntimeError("account config hash mismatch")
        loaded = load_binding_csv(
            Path(bindings_path), registry=registry, secret=actual_secret
        )
        references = {
            (row["canonical_account_id"], row["raw_wechat_id_hash"]): dict(row)
            for row in connection.execute(
                "SELECT customer_key,profile_id,canonical_account_id,raw_wechat_id_hash "
                "FROM conversation_refs"
            )
        }
        reviewed_links = {
            row["link_id"]: dict(row)
            for row in connection.execute(
                "SELECT link_id,raw_wechat_id_hash,phone_hmac,state,source_hash,reviewed_at "
                "FROM conversation_links WHERE version=? AND reviewed_at IS NOT NULL",
                (IDENTITY_VERSION,),
            )
        }
        matched = 0
        unmatched = 0
        link_states: Counter[str] = Counter()
        with connection:
            connection.execute(
                "DELETE FROM conversation_links WHERE version=?", (IDENTITY_VERSION,)
            )
            for (_, raw_wechat_id), candidate in loaded.items():
                raw_hash = hmac_id(
                    actual_secret,
                    "raw-wechat-id",
                    candidate.canonical_account_id,
                    raw_wechat_id,
                )
                reference = references.get((candidate.canonical_account_id, raw_hash))
                if reference is None:
                    unmatched += 1
                    continue
                matched += 1
                link_id = hmac_id(
                    actual_secret,
                    "conversation-link",
                    reference["customer_key"],
                    IDENTITY_VERSION,
                )
                state = candidate.state
                reviewed_at = None
                previous = reviewed_links.get(link_id)
                if (
                    previous is not None
                    and previous["raw_wechat_id_hash"] == raw_hash
                    and previous["phone_hmac"] == candidate.phone_hmac
                    and previous["source_hash"] == loaded.source_hash
                ):
                    state = previous["state"]
                    reviewed_at = previous["reviewed_at"]
                link_states[state] += 1
                connection.execute(
                    """
                    INSERT INTO conversation_links(
                        link_id,customer_key,profile_id,raw_wechat_id_hash,phone_hmac,
                        match_method,confidence,state,source_hash,version,reviewed_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?)
                    """,
                    (
                        link_id,
                        reference["customer_key"],
                        reference["profile_id"],
                        raw_hash,
                        candidate.phone_hmac,
                        candidate.match_method,
                        float(candidate.confidence),
                        state,
                        loaded.source_hash,
                        IDENTITY_VERSION,
                        reviewed_at,
                    ),
                )
        return {
            "source_hash": loaded.source_hash,
            "total_rows": loaded.stats["total_rows"],
            "confidence_counts": loaded.stats["confidence_counts"],
            "candidate_state_counts": loaded.stats["state_counts"],
            "link_state_counts": dict(sorted(link_states.items())),
            "unknown_accounts": loaded.stats["unknown_accounts"],
            "missing_raw_id": loaded.stats["missing_raw_id"],
            "invalid_phones": loaded.stats["invalid_phones"],
            "conflicts": int(
                dict(loaded.stats["state_counts"]).get("conflict", 0)
            ),
            "matched_conversations": matched,
            "unmatched_candidates": unmatched,
        }
    finally:
        connection.close()
