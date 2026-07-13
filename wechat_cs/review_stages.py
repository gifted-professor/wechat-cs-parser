"""Deterministic, outcome-blind human review batches for Plan 7.

The review lane deliberately queries decision cards and rule annotations with
an explicit column allowlist.  It never joins ``card_outcomes`` or ``orders``.
Observed actions are available only in the local human-audit payload and are
removed by :func:`to_public_review_payload` before any model-facing use.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, Iterator, List, Mapping, Optional, Sequence, Tuple, Union

from .core import json_dumps, redact_text
from .store import open_store


REVIEW_POLICY_VERSION = "human-review-stages-v1"
STAGE_TARGETS: Dict[str, int] = {
    "protocol_20": 20,
    "acceptance_100": 100,
    "gold_500": 500,
}
STAGE_ORDER: Tuple[str, ...] = tuple(STAGE_TARGETS)
VERDICTS = frozenset({"approved", "edited", "rejected"})

_OPAQUE_CARD = re.compile(r"card_[0-9a-f]{16,64}\Z")
_OPAQUE_CUSTOMER = re.compile(r"customer_[0-9a-f]{16,64}\Z")
_REVIEWER = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_LABEL_KEY = re.compile(r"[a-z][a-z0-9_]{0,63}\Z")
_FORBIDDEN_LABEL_TOKENS = frozenset(
    {
        "address",
        "amount",
        "conversion",
        "email",
        "future",
        "identity",
        "matched",
        "name",
        "order",
        "outcome",
        "paid",
        "payment",
        "phone",
        "pii",
        "raw",
        "revenue",
        "wechat",
    }
)

Database = Union[str, Path, sqlite3.Connection]


class ReviewStageError(RuntimeError):
    """Base class for safe, aggregate-only review errors."""


class ReviewStageBlockedError(ReviewStageError):
    """Raised when a later review stage is requested too early."""


class InsufficientReviewCardsError(ReviewStageError):
    """Raised when an exact-size batch cannot be formed."""


@dataclass(frozen=True)
class _Card:
    card_id: str
    customer_key: str
    card_type: str
    as_of_at: str
    split: str
    blind_context_json: str
    observed_action_json: str
    customer_signal: str
    reply_strategy: str
    reuse_status: str
    required_facts_json: str
    prohibited_claims_json: str
    created_at: str

    @property
    def stratum(self) -> Tuple[str, str, str, str, str]:
        return (
            self.card_type,
            self.customer_signal,
            self.reply_strategy,
            self.reuse_status,
            self.split,
        )


def _require_stage(stage: str) -> str:
    normalized = str(stage or "").strip()
    if normalized not in STAGE_TARGETS:
        raise ValueError("unknown review stage")
    return normalized


@contextmanager
def _database(value: Database, *, read_only: bool) -> Iterator[sqlite3.Connection]:
    if isinstance(value, sqlite3.Connection):
        yield value
        return
    connection = open_store(str(value), read_only=read_only)
    try:
        yield connection
    finally:
        connection.close()


def _load_cards(connection: sqlite3.Connection) -> List[_Card]:
    """Load only pre-action and observed-action review columns.

    The explicit query is a privacy boundary.  Do not replace it with
    ``SELECT *`` and do not add an outcome/order join.
    """

    rows = connection.execute(
        """
        SELECT dc.card_id,dc.customer_key,dc.card_type,dc.as_of_at,dc.split,
               dc.blind_context_json,dc.observed_action_json,
               aa.customer_signal,aa.reply_strategy,aa.reuse_status,
               aa.required_facts_json,aa.prohibited_claims_json,dc.created_at
        FROM decision_cards dc
        JOIN action_annotations aa ON aa.card_id=dc.card_id
        ORDER BY dc.card_id
        """
    )
    output: List[_Card] = []
    for row in rows:
        card_id = str(row["card_id"])
        customer_key = str(row["customer_key"])
        if not _OPAQUE_CARD.fullmatch(card_id) or not _OPAQUE_CUSTOMER.fullmatch(
            customer_key
        ):
            raise ReviewStageError("review cards must use opaque identifiers")
        card_type = str(row["card_type"])
        if card_type not in {"inbound", "proactive_followup"}:
            raise ReviewStageError("review card type is invalid")
        output.append(
            _Card(
                card_id=card_id,
                customer_key=customer_key,
                card_type=card_type,
                as_of_at=str(row["as_of_at"]),
                split=str(row["split"]),
                blind_context_json=str(row["blind_context_json"]),
                observed_action_json=str(row["observed_action_json"]),
                customer_signal=str(row["customer_signal"]),
                reply_strategy=str(row["reply_strategy"]),
                reuse_status=str(row["reuse_status"]),
                required_facts_json=str(row["required_facts_json"]),
                prohibited_claims_json=str(row["prohibited_claims_json"]),
                created_at=str(row["created_at"]),
            )
        )
    return output


def _stable_card_key(card: _Card) -> Tuple[str, str]:
    material = "\x1f".join(
        (REVIEW_POLICY_VERSION, *card.stratum, card.customer_key, card.card_id)
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest(), card.card_id


def _round_robin(
    candidates: Sequence[_Card],
    *,
    limit: int,
    chosen_card_ids: set[str],
    allowed_customer,
    unique_customers: bool,
    chosen_customers: set[str],
) -> List[_Card]:
    grouped: Dict[Tuple[str, str, str, str, str], List[_Card]] = {}
    for card in candidates:
        if card.card_id in chosen_card_ids or not allowed_customer(card.customer_key):
            continue
        grouped.setdefault(card.stratum, []).append(card)
    for cards in grouped.values():
        cards.sort(key=_stable_card_key)

    selected: List[_Card] = []
    indexes = {key: 0 for key in grouped}
    strata = sorted(grouped)
    while len(selected) < limit:
        progressed = False
        for stratum in strata:
            cards = grouped[stratum]
            index = indexes[stratum]
            while index < len(cards):
                card = cards[index]
                index += 1
                if card.card_id in chosen_card_ids:
                    continue
                if unique_customers and card.customer_key in chosen_customers:
                    continue
                selected.append(card)
                chosen_card_ids.add(card.card_id)
                chosen_customers.add(card.customer_key)
                progressed = True
                break
            indexes[stratum] = index
            if len(selected) >= limit:
                break
        if not progressed:
            break
    return selected


def _select_stage(
    cards: Sequence[_Card],
    *,
    target: int,
    unavailable_card_ids: set[str],
    prior_customers: set[str],
) -> List[_Card]:
    """Select a stratified batch, preferring one never-used customer per card."""

    chosen_ids = set(unavailable_card_ids)
    chosen_customers: set[str] = set()
    selected: List[_Card] = []

    # Phase 1 gives each stage customers not present in earlier stages and uses
    # at most one card per customer.  With enough independent customers this is
    # a strict customer-isolated sample.
    selected.extend(
        _round_robin(
            cards,
            limit=target,
            chosen_card_ids=chosen_ids,
            allowed_customer=lambda value: value not in prior_customers,
            unique_customers=True,
            chosen_customers=chosen_customers,
        )
    )

    # If the corpus has too few fresh customers, retain within-stage isolation
    # before ever taking a second card from the same customer.
    if len(selected) < target:
        selected.extend(
            _round_robin(
                cards,
                limit=target - len(selected),
                chosen_card_ids=chosen_ids,
                allowed_customer=lambda value: True,
                unique_customers=True,
                chosen_customers=chosen_customers,
            )
        )

    # Exact stage sizes remain possible for small-customer/multi-card fixtures;
    # the payload reports the resulting isolation ratio instead of pretending
    # those additional cards are independent customers.
    if len(selected) < target:
        selected.extend(
            _round_robin(
                cards,
                limit=target - len(selected),
                chosen_card_ids=chosen_ids,
                allowed_customer=lambda value: True,
                unique_customers=False,
                chosen_customers=chosen_customers,
            )
        )
    return selected


def _moment(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReviewStageError("invalid stored %s" % field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ReviewStageError("invalid stored %s" % field)
    return parsed.astimezone(timezone.utc)


def _stage_cutoffs(connection: sqlite3.Connection) -> Dict[str, datetime]:
    output: Dict[str, datetime] = {}
    for row in connection.execute(
        """
        SELECT review_stage,MIN(created_at) AS started_at
        FROM card_annotations
        GROUP BY review_stage
        """
    ):
        stage = str(row["review_stage"])
        if stage in STAGE_TARGETS and row["started_at"]:
            output[stage] = _moment(
                str(row["started_at"]), field="review stage start time"
            )
    return output


def _all_batches(
    cards: Sequence[_Card], stage_cutoffs: Mapping[str, datetime]
) -> Dict[str, List[_Card]]:
    batches: Dict[str, List[_Card]] = {}
    used_card_ids: set[str] = set()
    used_customers: set[str] = set()
    for stage in STAGE_ORDER:
        cutoff = stage_cutoffs.get(stage)
        stage_cards = (
            [
                item
                for item in cards
                if _moment(item.created_at, field="decision card created_at") <= cutoff
            ]
            if cutoff is not None
            else list(cards)
        )
        selected = _select_stage(
            stage_cards,
            target=STAGE_TARGETS[stage],
            unavailable_card_ids=used_card_ids,
            prior_customers=used_customers,
        )
        batches[stage] = selected
        used_card_ids.update(item.card_id for item in selected)
        used_customers.update(item.customer_key for item in selected)
    return batches


def _parse_json(value: str, expected_type, *, field: str):
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise ReviewStageError("invalid stored %s" % field) from exc
    if not isinstance(parsed, expected_type):
        raise ReviewStageError("invalid stored %s" % field)
    return parsed


def _safe_time(value: object) -> Optional[str]:
    text = str(value or "").strip()
    if not text or len(text) > 64:
        return None
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return text


def _safe_context(value: str) -> List[Dict[str, object]]:
    rows = _parse_json(value, list, field="blind context")
    output: List[Dict[str, object]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        role = str(row.get("role") or "")
        if role not in {"customer", "studio"}:
            continue
        text, flags = redact_text(str(row.get("text") or ""))
        item: Dict[str, object] = {"role": role, "text": text}
        started_at = _safe_time(row.get("started_at"))
        ended_at = _safe_time(row.get("ended_at"))
        if started_at:
            item["started_at"] = started_at
        if ended_at:
            item["ended_at"] = ended_at
        if flags:
            item["redaction_flags"] = flags
        output.append(item)
    return output


def _safe_observed_action(value: str) -> Dict[str, object]:
    raw = _parse_json(value, dict, field="observed action")
    output: Dict[str, object] = {
        "human_audit_only": True,
        "state": str(raw.get("state") or "unobserved")[:64],
    }
    for key in ("reply_delay_seconds", "gap_seconds"):
        candidate = raw.get(key)
        if candidate is None or isinstance(candidate, bool):
            output[key] = None
        elif isinstance(candidate, (int, float)):
            output[key] = max(0, int(candidate))
        else:
            output[key] = None
    if raw.get("text") is None:
        output["text"] = None
    else:
        text, flags = redact_text(str(raw.get("text") or ""))
        output["text"] = text
        if flags:
            output["redaction_flags"] = flags
    return output


def _safe_string_list(value: str, *, field: str) -> List[str]:
    rows = _parse_json(value, list, field=field)
    output: List[str] = []
    for row in rows:
        if not isinstance(row, str):
            continue
        sanitized, _ = redact_text(row)
        if sanitized:
            output.append(sanitized[:128])
    return sorted(set(output))


def _card_payload(card: _Card) -> Dict[str, object]:
    return {
        "card_id": card.card_id,
        "customer_key": card.customer_key,
        "card_type": card.card_type,
        "as_of_at": card.as_of_at,
        "split": card.split,
        "blind_context": _safe_context(card.blind_context_json),
        "observed_action": _safe_observed_action(card.observed_action_json),
        "reply_audit": {
            "customer_signal": card.customer_signal,
            "reply_strategy": card.reply_strategy,
            "reuse_status": card.reuse_status,
            "required_facts": _safe_string_list(
                card.required_facts_json, field="required facts"
            ),
            "prohibited_claims": _safe_string_list(
                card.prohibited_claims_json, field="prohibited claims"
            ),
        },
    }


def to_public_review_payload(item: Mapping[str, object]) -> Dict[str, object]:
    """Return an outcome- and action-blind payload suitable for a model.

    Only the decision boundary context is copied.  Rule labels, human notes,
    observed replies, and all post-boundary outcomes are intentionally absent.
    """

    context = item.get("blind_context")
    if not isinstance(context, list):
        context = item.get("context")
    safe_context: List[Dict[str, object]] = []
    if isinstance(context, list):
        # Round-trip through the same narrow sanitizer used for database rows.
        safe_context = _safe_context(json_dumps(context))
    return {
        "card_id": str(item.get("card_id") or ""),
        "card_type": str(item.get("card_type") or ""),
        "as_of_at": str(item.get("as_of_at") or ""),
        "context": safe_context,
    }


def to_model_review_payload(item: Mapping[str, object]) -> Dict[str, object]:
    """Alias documenting the model-facing use of the public payload."""

    return to_public_review_payload(item)


def _annotation_state(
    connection: sqlite3.Connection,
    stage: str,
    selected: Sequence[_Card],
) -> Dict[str, object]:
    selected_ids = {item.card_id for item in selected}
    annotated_ids: set[str] = set()
    verdict_counts = {value: 0 for value in sorted(VERDICTS)}
    reviewers: set[str] = set()
    for row in connection.execute(
        """
        SELECT card_id,verdict,reviewer
        FROM card_annotations
        WHERE review_stage=?
        ORDER BY card_id,reviewer
        """,
        (stage,),
    ):
        card_id = str(row["card_id"])
        if card_id not in selected_ids:
            continue
        annotated_ids.add(card_id)
        verdict = str(row["verdict"])
        if verdict in verdict_counts:
            verdict_counts[verdict] += 1
        reviewers.add(str(row["reviewer"]))
    return {
        "annotated": len(annotated_ids),
        "remaining": max(0, STAGE_TARGETS[stage] - len(annotated_ids)),
        "annotation_rows": sum(verdict_counts.values()),
        "reviewer_count": len(reviewers),
        "verdict_counts": verdict_counts,
    }


def _status_with_connection(
    connection: sqlite3.Connection,
) -> Tuple[Dict[str, object], Dict[str, List[_Card]]]:
    cards = _load_cards(connection)
    batches = _all_batches(cards, _stage_cutoffs(connection))
    stages: Dict[str, Dict[str, object]] = {}
    prior_complete = True
    prior_stage: Optional[str] = None
    for stage in STAGE_ORDER:
        selected = batches[stage]
        annotation = _annotation_state(connection, stage, selected)
        target = STAGE_TARGETS[stage]
        if not prior_complete:
            status = "blocked"
            blocked_by = prior_stage
        elif len(selected) < target:
            status = "insufficient_cards"
            blocked_by = None
        elif int(annotation["annotated"]) == target:
            status = "complete"
            blocked_by = None
        elif int(annotation["annotated"]) > 0:
            status = "in_progress"
            blocked_by = None
        else:
            status = "not_started"
            blocked_by = None
        stage_payload: Dict[str, object] = {
            "target": target,
            "selected": len(selected),
            "status": status,
            **annotation,
        }
        if blocked_by:
            stage_payload["blocked_by"] = blocked_by
        stages[stage] = stage_payload
        prior_complete = status == "complete"
        prior_stage = stage
    return (
        {
            "review_policy_version": REVIEW_POLICY_VERSION,
            "eligible_cards": len(cards),
            "stages": stages,
            "automatic_approval": False,
            "m0_gate_changes": False,
        },
        batches,
    )


def get_review_status(database: Database) -> Dict[str, object]:
    """Return aggregate stage progress without exposing reviewer identities."""

    with _database(database, read_only=True) as connection:
        status, _ = _status_with_connection(connection)
        return status


def prepare_review_batch(database: Database, stage: str) -> Dict[str, object]:
    """Prepare one exact-size local human review batch.

    Later stages fail closed until the preceding stage has one human verdict
    for every selected card.  Preparing a batch never writes annotations,
    changes a decision-card review status, or changes an M0 acceptance gate.
    """

    normalized_stage = _require_stage(stage)
    with _database(database, read_only=True) as connection:
        status, batches = _status_with_connection(connection)
        stage_status = status["stages"][normalized_stage]
        if stage_status["status"] == "blocked":
            raise ReviewStageBlockedError(
                "review stage is blocked by %s" % stage_status["blocked_by"]
            )
        selected = batches[normalized_stage]
        target = STAGE_TARGETS[normalized_stage]
        if len(selected) != target:
            raise InsufficientReviewCardsError(
                "review stage requires %d cards; %d are available"
                % (target, len(selected))
            )
        items = [_card_payload(item) for item in selected]
        strata: Dict[str, int] = {}
        for item in selected:
            key = "|".join(item.stratum)
            strata[key] = strata.get(key, 0) + 1
        unique_customers = len({item.customer_key for item in selected})
        return {
            "review_policy_version": REVIEW_POLICY_VERSION,
            "stage": normalized_stage,
            "target": target,
            "count": len(items),
            "status": stage_status["status"],
            "customer_isolation": {
                "unique_customers": unique_customers,
                "card_count": len(items),
                "fully_isolated": unique_customers == len(items),
            },
            "strata": dict(sorted(strata.items())),
            "items": items,
            "automatic_approval": False,
            "m0_gate_changes": False,
        }


def _safe_label_value(value: object) -> object:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        sanitized, _ = redact_text(value)
        return sanitized[:128]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        if abs(float(value)) > 1_000_000:
            raise ValueError("annotation label number is out of range")
        return value
    if isinstance(value, list) and len(value) <= 20:
        return [_safe_label_value(item) for item in value]
    raise ValueError("annotation label values must be safe scalar values")


def _safe_labels(value: object) -> Dict[str, object]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("annotation labels must be an object")
    output: Dict[str, object] = {}
    for raw_key, raw_value in value.items():
        key = str(raw_key or "").strip().lower()
        if not _LABEL_KEY.fullmatch(key):
            raise ValueError("annotation label key is invalid")
        tokens = set(key.split("_"))
        if tokens & _FORBIDDEN_LABEL_TOKENS:
            raise ValueError("outcome, order, and PII labels are prohibited")
        output[key] = _safe_label_value(raw_value)
    return dict(sorted(output.items()))


def import_review_annotations(
    database: Database,
    *,
    stage: str,
    reviewer: str,
    annotations: Iterable[Mapping[str, object]],
) -> Dict[str, object]:
    """Insert or update human annotations idempotently for one stage/reviewer."""

    normalized_stage = _require_stage(stage)
    normalized_reviewer = str(reviewer or "").strip()
    if not _REVIEWER.fullmatch(normalized_reviewer):
        raise ValueError("reviewer must be an opaque local handle")
    provided = list(annotations)
    if not provided:
        raise ValueError("at least one annotation is required")

    with _database(database, read_only=False) as connection:
        batch = prepare_review_batch(connection, normalized_stage)
        allowed = {str(item["card_id"]) for item in batch["items"]}
        prepared: List[Tuple[str, str, str, Optional[str]]] = []
        seen: set[str] = set()
        for raw in provided:
            if not isinstance(raw, Mapping):
                raise ValueError("each annotation must be an object")
            card_id = str(raw.get("card_id") or "").strip()
            if card_id not in allowed:
                raise ValueError("annotation card is outside the selected stage batch")
            if card_id in seen:
                raise ValueError("duplicate card annotation in one import")
            seen.add(card_id)
            verdict = str(raw.get("verdict") or "").strip().lower()
            if verdict not in VERDICTS:
                raise ValueError("annotation verdict is invalid")
            labels = _safe_labels(raw.get("labels"))
            notes_value = raw.get("notes")
            notes: Optional[str]
            if notes_value is None or str(notes_value).strip() == "":
                notes = None
            else:
                notes, _ = redact_text(str(notes_value))
                notes = notes[:1000]
            prepared.append((card_id, verdict, json_dumps(labels), notes))

        existing = {
            str(row[0])
            for row in connection.execute(
                """
                SELECT card_id FROM card_annotations
                WHERE review_stage=? AND reviewer=?
                """,
                (normalized_stage, normalized_reviewer),
            )
        }
        created_at = datetime.now().astimezone().isoformat(timespec="seconds")
        with connection:
            for card_id, verdict, labels_json, notes in prepared:
                digest = hashlib.sha256(
                    "\x1f".join(
                        (
                            REVIEW_POLICY_VERSION,
                            normalized_stage,
                            normalized_reviewer,
                            card_id,
                        )
                    ).encode("utf-8")
                ).hexdigest()[:24]
                connection.execute(
                    """
                    INSERT INTO card_annotations(
                        annotation_id,card_id,review_stage,reviewer,verdict,
                        labels_json,notes,created_at
                    ) VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(card_id,review_stage,reviewer) DO UPDATE SET
                        verdict=excluded.verdict,
                        labels_json=excluded.labels_json,
                        notes=excluded.notes
                    """,
                    (
                        "annotation_" + digest,
                        card_id,
                        normalized_stage,
                        normalized_reviewer,
                        verdict,
                        labels_json,
                        notes,
                        created_at,
                    ),
                )

        placeholders = ",".join("?" for _ in allowed)
        rows_total = connection.execute(
            """
            SELECT COUNT(DISTINCT card_id)
            FROM card_annotations
            WHERE review_stage=? AND reviewer=? AND card_id IN (%s)
            """
            % placeholders,
            (normalized_stage, normalized_reviewer, *sorted(allowed)),
        ).fetchone()[0]
        status, _ = _status_with_connection(connection)
        return {
            "review_policy_version": REVIEW_POLICY_VERSION,
            "stage": normalized_stage,
            "received": len(prepared),
            "inserted": sum(card_id not in existing for card_id, *_ in prepared),
            "updated": sum(card_id in existing for card_id, *_ in prepared),
            "rows_total": int(rows_total),
            "stage_status": status["stages"][normalized_stage]["status"],
            "automatic_approval": False,
            "m0_gate_changes": False,
        }
