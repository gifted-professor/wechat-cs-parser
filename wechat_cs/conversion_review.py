"""Manual review service for historical conversion-attribution samples.

The audit artifacts and message database are read-only inputs. Human decisions
are written to a separate SQLite file beside the audit report, so opening-line
reviews for the frozen 50-person pilot remain untouched.
"""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3
import threading
from typing import Any, Callable, Dict, Mapping, Optional


IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
SAMPLE_STATES = frozenset({"converted_7d", "non_converted_7d"})
ORIGINS = frozenset({"customer_initiated", "studio_initiated"})
INTENTS = frozenset({"sales_inquiry", "general_or_unknown"})
PRICE_BARRIERS = frozenset(
    {"none", "explicit_price_objection", "promotion_wait", "discount_request"}
)
SUSPECTED_BARRIERS = frozenset({"none", "quote_then_silence_suspected"})
TALK_TRACKS = frozenset(
    {
        "price_quote",
        "promotion_offer",
        "product_recommendation",
        "trust_proof",
        "scarcity_or_urgency",
        "question_or_clarification",
        "other_observed_reply",
        "no_observed_reply",
    }
)
VERDICTS = frozenset({"approved", "corrected", "rejected"})
SIGNALS = frozenset(
    {"", "price_barrier", "quote_silence", "customer_initiated", "repeat_90d"}
)
REVIEW_STATUSES = frozenset({"", "pending", "reviewed"})


class ConversionReviewError(RuntimeError):
    """Base error for conversion review operations."""


class ConversionReviewValidationError(ConversionReviewError):
    """Raised when a request or audit artifact violates the review contract."""


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConversionReviewError("归因报告不可用") from exc
    if not isinstance(value, dict):
        raise ConversionReviewError("归因报告格式错误")
    return value


def _plain_text(value: Any, *, limit: int) -> str:
    text = "".join(
        character
        for character in str(value or "").strip()
        if character >= " " and character != "\x7f"
    )
    return re.sub(r"\s+", " ", text)[:limit]


def _public_review(row: Optional[Mapping[str, Any]]) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    fields = (
        "verdict",
        "corrected_origin",
        "corrected_intent",
        "corrected_explicit_price_barrier",
        "corrected_suspected_barrier",
        "corrected_talk_track_primary",
        "note",
        "updated_at",
    )
    return {field: row[field] for field in fields}


class ConversionReviewService:
    """Load audit samples, expose safe views, and persist manual decisions."""

    def __init__(
        self,
        audit_dir: Path,
        message_db_path: Path,
        *,
        cleaner: Callable[[Any], str],
    ) -> None:
        self.audit_dir = Path(audit_dir).expanduser().resolve()
        self.message_db_path = Path(message_db_path).expanduser().resolve()
        self.report_path = self.audit_dir / "report.json"
        self.samples_path = self.audit_dir / "episode_samples.jsonl"
        self.review_db_path = self.audit_dir / "manual_reviews.sqlite3"
        self.cleaner = cleaner
        self.lock = threading.Lock()
        if not self.audit_dir.is_dir() or not self.message_db_path.is_file():
            raise ConversionReviewError("归因审核数据尚未准备好")
        self.report = _read_json(self.report_path)
        self.audit_version = _plain_text(self.report.get("audit_version"), limit=128)
        if not self.audit_version:
            raise ConversionReviewError("归因报告缺少版本")
        self.samples = self._load_samples()
        self.samples_by_id = {str(item["episode_id"]): item for item in self.samples}
        self._ensure_review_schema()

    def _load_samples(self) -> list[Dict[str, Any]]:
        samples: list[Dict[str, Any]] = []
        seen = set()
        try:
            handle = self.samples_path.open("r", encoding="utf-8")
        except OSError as exc:
            raise ConversionReviewError("归因样本不可用") from exc
        with handle:
            for line_number, raw in enumerate(handle, 1):
                if not raw.strip():
                    continue
                try:
                    item = json.loads(raw)
                except json.JSONDecodeError as exc:
                    raise ConversionReviewError("归因样本第 %d 行格式错误" % line_number) from exc
                if not isinstance(item, dict):
                    raise ConversionReviewError("归因样本第 %d 行格式错误" % line_number)
                episode_id = str(item.get("episode_id") or "")
                customer_key = str(item.get("customer_key") or "")
                if (
                    not IDENTIFIER.fullmatch(episode_id)
                    or not IDENTIFIER.fullmatch(customer_key)
                    or episode_id in seen
                ):
                    raise ConversionReviewError("归因样本编号无效")
                seen.add(episode_id)
                if item.get("eligible_for_sales_method") is not True:
                    continue
                if (
                    item.get("sample_state") not in SAMPLE_STATES
                    or item.get("origin") not in ORIGINS
                    or item.get("intent") not in INTENTS
                    or item.get("explicit_price_barrier") not in PRICE_BARRIERS
                    or item.get("suspected_barrier") not in SUSPECTED_BARRIERS
                    or item.get("talk_track_primary") not in TALK_TRACKS
                ):
                    raise ConversionReviewError("归因样本标签无效")
                try:
                    date.fromisoformat(str(item.get("ended_on") or ""))
                except ValueError as exc:
                    raise ConversionReviewError("归因样本日期无效") from exc
                samples.append(item)
        return samples

    def _ensure_review_schema(self) -> None:
        connection = sqlite3.connect(str(self.review_db_path), timeout=10.0)
        try:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS conversion_sample_reviews (
                    episode_id TEXT NOT NULL,
                    reviewer_key TEXT NOT NULL,
                    audit_version TEXT NOT NULL,
                    verdict TEXT NOT NULL CHECK(verdict IN ('approved','corrected','rejected')),
                    corrected_origin TEXT NOT NULL DEFAULT '',
                    corrected_intent TEXT NOT NULL DEFAULT '',
                    corrected_explicit_price_barrier TEXT NOT NULL DEFAULT '',
                    corrected_suspected_barrier TEXT NOT NULL DEFAULT '',
                    corrected_talk_track_primary TEXT NOT NULL DEFAULT '',
                    note TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY(episode_id, reviewer_key)
                );
                CREATE INDEX IF NOT EXISTS conversion_sample_reviews_updated
                    ON conversion_sample_reviews(updated_at DESC, episode_id);
                """
            )
            connection.commit()
        finally:
            connection.close()

    def _review_rows(self) -> Dict[str, sqlite3.Row]:
        connection = sqlite3.connect(str(self.review_db_path), timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT episode_id,verdict,corrected_origin,corrected_intent,"
                "corrected_explicit_price_barrier,corrected_suspected_barrier,"
                "corrected_talk_track_primary,note,updated_at "
                "FROM conversion_sample_reviews WHERE reviewer_key='operator-shared-workbench'"
            ).fetchall()
        finally:
            connection.close()
        return {str(row["episode_id"]): row for row in rows}

    @staticmethod
    def _priority(item: Mapping[str, Any]) -> int:
        score = 0
        if item.get("explicit_price_barrier") != "none":
            score += 50
        if item.get("suspected_barrier") == "quote_then_silence_suspected":
            score += 45
        if item.get("intent") == "sales_inquiry":
            score += 20
        if item.get("origin") == "customer_initiated":
            score += 10
        if item.get("repeat_90d") is True:
            score += 10
        score += 8 if item.get("sample_state") == "converted_7d" else 6
        return score

    def _public_sample(
        self,
        item: Mapping[str, Any],
        review: Optional[Mapping[str, Any]],
    ) -> Dict[str, Any]:
        signals = []
        if item.get("origin") == "customer_initiated":
            signals.append("customer_initiated")
        if item.get("explicit_price_barrier") != "none":
            signals.append("price_barrier")
        if item.get("suspected_barrier") == "quote_then_silence_suspected":
            signals.append("quote_silence")
        if item.get("repeat_90d") is True:
            signals.append("repeat_90d")
        episode_id = str(item["episode_id"])
        return {
            "episode_id": episode_id,
            "sample_label": "方法样本 %s" % episode_id.rsplit("_", 1)[-1][-8:],
            "ended_on": item["ended_on"],
            "origin": item["origin"],
            "intent": item["intent"],
            "explicit_price_barrier": item["explicit_price_barrier"],
            "suspected_barrier": item["suspected_barrier"],
            "talk_track_primary": item["talk_track_primary"],
            "sample_state": item["sample_state"],
            "repeat_90d": item.get("repeat_90d"),
            "signals": signals,
            "review_priority": self._priority(item),
            "reviewed": review is not None,
            "latest_verdict": review["verdict"] if review is not None else None,
        }

    def summary(self) -> Dict[str, Any]:
        reviews = self._review_rows()
        eligible_ids = set(self.samples_by_id)
        active_reviews = [row for episode_id, row in reviews.items() if episode_id in eligible_ids]
        states = Counter(str(item["sample_state"]) for item in self.samples)
        verdicts = Counter(str(row["verdict"]) for row in active_reviews)
        population = self.report.get("population")
        gate = self.report.get("training_gate")
        return {
            "audit_version": self.audit_version,
            "requested_as_of": self.report.get("requested_as_of"),
            "method_sample_total": len(self.samples),
            "converted_7d": states.get("converted_7d", 0),
            "non_converted_7d": states.get("non_converted_7d", 0),
            "reviewed": len(active_reviews),
            "pending": max(len(self.samples) - len(active_reviews), 0),
            "verdicts": dict(verdicts),
            "population": population if isinstance(population, dict) else {},
            "training_gate": gate if isinstance(gate, dict) else {},
            "weights_trained": False,
            "send_allowed": False,
            "claim_mode": "historical_association_only_no_causal_claim",
        }

    def list_samples(
        self,
        *,
        status: str = "",
        sample_state: str = "",
        signal: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> Dict[str, Any]:
        if status not in REVIEW_STATUSES:
            raise ConversionReviewValidationError("审核状态无效")
        if sample_state and sample_state not in SAMPLE_STATES:
            raise ConversionReviewValidationError("样本结果无效")
        if signal not in SIGNALS:
            raise ConversionReviewValidationError("样本信号无效")
        if limit < 1 or limit > 500 or offset < 0:
            raise ConversionReviewValidationError("分页参数无效")
        reviews = self._review_rows()
        values = []
        for item in self.samples:
            review = reviews.get(str(item["episode_id"]))
            if status == "pending" and review is not None:
                continue
            if status == "reviewed" and review is None:
                continue
            if sample_state and item.get("sample_state") != sample_state:
                continue
            if signal == "price_barrier" and item.get("explicit_price_barrier") == "none":
                continue
            if signal == "quote_silence" and item.get("suspected_barrier") != "quote_then_silence_suspected":
                continue
            if signal == "customer_initiated" and item.get("origin") != "customer_initiated":
                continue
            if signal == "repeat_90d" and item.get("repeat_90d") is not True:
                continue
            values.append(self._public_sample(item, review))
        values.sort(
            key=lambda item: (
                bool(item["reviewed"]),
                -int(item["review_priority"]),
                -int(str(item["ended_on"]).replace("-", "")),
                str(item["episode_id"]),
            )
        )
        return {
            "items": values[offset : offset + limit],
            "total": len(values),
            "limit": limit,
            "offset": offset,
            "weights_trained": False,
            "send_allowed": False,
        }

    def _messages(self, item: Mapping[str, Any]) -> list[Dict[str, Any]]:
        ended_on = date.fromisoformat(str(item["ended_on"]))
        started_on = ended_on - timedelta(days=7)
        ending_exclusive = ended_on + timedelta(days=1)
        uri = "file:%s?mode=ro" % self.message_db_path.as_posix()
        connection = sqlite3.connect(uri, uri=True, timeout=10.0)
        connection.row_factory = sqlite3.Row
        try:
            rows = connection.execute(
                "SELECT role,timestamp,text FROM messages WHERE customer_key=? "
                "AND timestamp>=? AND timestamp<? "
                "ORDER BY timestamp,source_ordinal,message_key LIMIT 80",
                (
                    item["customer_key"],
                    started_on.isoformat(),
                    ending_exclusive.isoformat(),
                ),
            ).fetchall()
        except sqlite3.Error as exc:
            raise ConversionReviewError("归因聊天上下文不可用") from exc
        finally:
            connection.close()
        return [
            {
                "role": row["role"],
                "timestamp": row["timestamp"],
                "text": self.cleaner(row["text"]),
            }
            for row in rows
        ]

    def detail(self, episode_id: str) -> Dict[str, Any]:
        if not IDENTIFIER.fullmatch(str(episode_id or "")):
            raise ConversionReviewValidationError("归因样本编号无效")
        item = self.samples_by_id.get(episode_id)
        if item is None:
            raise ConversionReviewValidationError("归因样本不存在")
        review = self._review_rows().get(episode_id)
        return {
            **self._public_sample(item, review),
            "messages": self._messages(item),
            "message_window": {
                "kind": "ended_on_minus_7_days",
                "note": "展示该回合结束日前 7 天的近似上下文，用于人工核对，不代表某句话导致成交。",
            },
            "review": _public_review(review),
            "audit_version": self.audit_version,
            "weights_trained": False,
            "send_allowed": False,
        }

    def save_review(self, episode_id: str, body: Any) -> Dict[str, Any]:
        item = self.samples_by_id.get(str(episode_id or ""))
        if item is None:
            raise ConversionReviewValidationError("归因样本不存在")
        if not isinstance(body, dict):
            raise ConversionReviewValidationError("审核内容格式错误")
        version = _plain_text(body.get("audit_version"), limit=128)
        if version != self.audit_version:
            raise ConversionReviewValidationError("归因样本已更新，请刷新后重新审核")
        verdict = _plain_text(body.get("verdict"), limit=32)
        if verdict not in VERDICTS:
            raise ConversionReviewValidationError("请选择审核结论")
        corrected_fields = {
            "corrected_origin": (ORIGINS, "origin"),
            "corrected_intent": (INTENTS, "intent"),
            "corrected_explicit_price_barrier": (PRICE_BARRIERS, "explicit_price_barrier"),
            "corrected_suspected_barrier": (SUSPECTED_BARRIERS, "suspected_barrier"),
            "corrected_talk_track_primary": (TALK_TRACKS, "talk_track_primary"),
        }
        corrected: Dict[str, str] = {}
        changed = False
        for field, (allowed, source_field) in corrected_fields.items():
            value = _plain_text(body.get(field), limit=80)
            if value and value not in allowed:
                raise ConversionReviewValidationError("修正标签无效")
            corrected[field] = value if verdict == "corrected" else ""
            if verdict == "corrected" and value and value != str(item.get(source_field) or ""):
                changed = True
        note = _plain_text(body.get("note"), limit=1000)
        if verdict == "corrected" and not (changed or note):
            raise ConversionReviewValidationError("修正后纳入时，请修改标签或填写说明")
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        with self.lock:
            connection = sqlite3.connect(str(self.review_db_path), timeout=10.0)
            connection.row_factory = sqlite3.Row
            try:
                connection.execute(
                    "INSERT INTO conversion_sample_reviews(episode_id,reviewer_key,audit_version,verdict,"
                    "corrected_origin,corrected_intent,corrected_explicit_price_barrier,"
                    "corrected_suspected_barrier,corrected_talk_track_primary,note,created_at,updated_at) "
                    "VALUES(?, 'operator-shared-workbench', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(episode_id,reviewer_key) DO UPDATE SET "
                    "audit_version=excluded.audit_version,verdict=excluded.verdict,"
                    "corrected_origin=excluded.corrected_origin,corrected_intent=excluded.corrected_intent,"
                    "corrected_explicit_price_barrier=excluded.corrected_explicit_price_barrier,"
                    "corrected_suspected_barrier=excluded.corrected_suspected_barrier,"
                    "corrected_talk_track_primary=excluded.corrected_talk_track_primary,"
                    "note=excluded.note,updated_at=excluded.updated_at",
                    (
                        episode_id,
                        self.audit_version,
                        verdict,
                        corrected["corrected_origin"],
                        corrected["corrected_intent"],
                        corrected["corrected_explicit_price_barrier"],
                        corrected["corrected_suspected_barrier"],
                        corrected["corrected_talk_track_primary"],
                        note,
                        now,
                        now,
                    ),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT verdict,corrected_origin,corrected_intent,"
                    "corrected_explicit_price_barrier,corrected_suspected_barrier,"
                    "corrected_talk_track_primary,note,updated_at "
                    "FROM conversion_sample_reviews WHERE episode_id=? "
                    "AND reviewer_key='operator-shared-workbench'",
                    (episode_id,),
                ).fetchone()
            finally:
                connection.close()
        result = _public_review(row)
        assert result is not None
        return result
