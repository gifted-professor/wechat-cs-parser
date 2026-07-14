"""Public, review-only portal for the frozen 50-person sales-profile pilot.

The portal deliberately exposes a much smaller surface than the operator API:
anonymous profile cards, evidence excerpts, aggregate progress, and review
submission.  It has no customer lookup, drafting, model trigger, or send route.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sqlite3
import threading
import traceback
from datetime import datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple
from urllib.parse import parse_qs, unquote, urlsplit


STATIC_DIR = Path(__file__).with_name("review_portal_static")
DEFAULT_DB_PATH = Path(
    "/Volumes/GPFS/Users/a1234/Desktop/Coding/wechat-cs-parser/.wechat-cs/"
    "runs/20260713T140730+0800-833c3257/wechat_cs_m0.sqlite3"
)
MAX_BODY_BYTES = 64 * 1024
IDENTIFIER = re.compile(r"[A-Za-z0-9_.:-]{1,160}")
PHONE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
IDENTITY = re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")
EVENT_ID = re.compile(r"sales-profile-event-[a-f0-9]{12,}")
TECHNICAL_REF = re.compile(
    r"(?:message[_-][A-Za-z0-9]+|order-line[_-][A-Za-z0-9]+|sales-profile-event-[A-Za-z0-9]+)"
)

EVIDENCE_FIELD_LABELS = {
    "sku_name": "商品",
    "brand": "品牌",
    "category": "品类",
    "color": "颜色",
    "size": "尺码",
    "order_note": "订单备注",
    "paid_on": "付款日期",
    "refund_type": "售后类型",
    "refund_on": "售后日期",
    "refund_fact_at_cutoff": "截止点售后事实",
}

STRATUM_LABELS = {
    "complex_risk": "售后关怀",
    "future_return_wait": "回访等待",
    "high_frequency": "高频客户",
    "high_value": "高价值客户",
    "dormant_repeat": "沉睡复购",
    "control": "普通对照",
}
STRATUM_ORDER = {
    "complex_risk": 1,
    "future_return_wait": 2,
    "high_frequency": 3,
    "high_value": 4,
    "dormant_repeat": 5,
    "control": 6,
}


class PortalError(RuntimeError):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = int(status)
        self.code = code
        self.message = message


def _json(value: Any, default: Any) -> Any:
    if value is None:
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default
    return parsed


def _clean_text(value: Any) -> str:
    text = str(value or "")
    text = PHONE.sub("[手机号已隐藏]", text)
    text = IDENTITY.sub("[身份信息已隐藏]", text)
    text = EVENT_ID.sub("[已验证证据]", text)
    text = TECHNICAL_REF.sub("[记录编号已隐藏]", text)
    return text


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return _clean_text(value)
    if isinstance(value, list):
        return [_clean_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _clean_value(item) for key, item in value.items()}
    return value


def _label(stratum: str, rank: int) -> str:
    return "%s · 样本 %02d" % (STRATUM_LABELS.get(stratum, "客户画像"), rank)


def _public_facts(raw: Any) -> Dict[str, Any]:
    facts = _json(raw, {})
    features = facts.get("customer_features") if isinstance(facts, dict) else {}
    if not isinstance(features, dict):
        features = {}
    monetary = features.get("rfm_monetary_minor")
    try:
        monetary_yuan = round(int(monetary) / 100, 2) if monetary is not None else None
    except (TypeError, ValueError):
        monetary_yuan = None
    member_facts = facts.get("member_facts") if isinstance(facts, dict) else []
    return _clean_value(
        {
            "value_level": features.get("value_bucket"),
            "historical_orders": features.get("rfm_frequency"),
            "historical_spend_yuan": monetary_yuan,
            "days_since_last_order": features.get("rfm_recency_days"),
            "recommended_contact_window": features.get("recommended_contact_window"),
            "contact_evidence_count": features.get("contact_window_evidence_count"),
            "median_reply_seconds": features.get("median_reply_delay_seconds"),
            "preferred_products": features.get("preferred_skus") or [],
            "preferred_colors": features.get("preferred_colors") or [],
            "preferred_sizes": features.get("preferred_sizes") or [],
            "member_profile_matched": bool(member_facts),
            "inventory_assumption": "默认满库存，可按历史偏好推荐商品",
        }
    )


def _public_event(row: Mapping[str, Any]) -> Dict[str, Any]:
    event = _json(row.get("event_json"), {})
    evidence = _json(row.get("evidence_json"), [])
    public_evidence = []
    for index, item in enumerate(evidence if isinstance(evidence, list) else [], 1):
        if not isinstance(item, dict):
            continue
        quote = item.get("quote") or item.get("excerpt") or item.get("text")
        if not quote and item.get("kind") == "order" and item.get("field"):
            field = str(item.get("field"))
            value = item.get("value")
            if isinstance(value, bool):
                value = "是" if value else "否"
            quote = "%s：%s" % (EVIDENCE_FIELD_LABELS.get(field, field), value)
        if not quote:
            continue
        public_evidence.append(
            {
                "label": "聊天证据 %02d" % index if item.get("kind") == "message" else "订单证据 %02d" % index,
                "quote": _clean_text(quote),
            }
        )
    return {
        "event_type": row.get("event_type"),
        "summary": _clean_text(event.get("summary") if isinstance(event, dict) else ""),
        "confidence": row.get("confidence"),
        "evidence": public_evidence,
    }


def _scores(row: Mapping[str, Any]) -> Dict[str, int]:
    return {
        "fact_accuracy": int(row.get("fact_accuracy") or 0),
        "insight_usefulness": int(row.get("insight_usefulness") or 0),
        "sales_realism": int(row.get("sales_realism") or 0),
        "timing_quality": int(row.get("timing_quality") or 0),
        "evidence_quality": int(row.get("evidence_quality") or 0),
    }


def _public_review(row: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "review_id": row.get("review_id"),
        "card_version": row.get("card_version"),
        "verdict": row.get("verdict"),
        "scores": _scores(row),
        "corrections": _clean_value(_json(row.get("corrections_json"), {})),
        "notes": _clean_text(row.get("notes")),
        "reviewer": _clean_text(row.get("reviewer")),
        "updated_at": row.get("updated_at"),
    }


def _validate_review(body: Any) -> Dict[str, Any]:
    if not isinstance(body, dict):
        raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_review", "审核内容格式错误")
    expected = {"card_version", "verdict", "scores", "corrections", "notes", "reviewer"}
    if set(body) != expected:
        raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_review", "审核字段不完整")
    card_version = str(body.get("card_version") or "").strip()
    verdict = str(body.get("verdict") or "").strip()
    reviewer = str(body.get("reviewer") or "").strip()
    notes = str(body.get("notes") or "").strip()
    scores = body.get("scores")
    corrections = body.get("corrections")
    if not card_version or len(card_version) > 256:
        raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_card_version", "卡片版本无效")
    if verdict not in {"approved", "edited", "rejected"}:
        raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_verdict", "请选择审核结论")
    if not reviewer or len(reviewer) > 120 or any(ord(char) < 32 for char in reviewer):
        raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_reviewer", "请填写有效的审核人姓名")
    if len(notes) > 4000:
        raise PortalError(HTTPStatus.BAD_REQUEST, "notes_too_long", "评语过长")
    if not isinstance(corrections, dict) or len(json.dumps(corrections, ensure_ascii=False)) > 12000:
        raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_corrections", "修改建议格式错误")
    if verdict == "edited" and not corrections:
        raise PortalError(HTTPStatus.BAD_REQUEST, "missing_corrections", "修改后通过时请填写修改建议")
    score_names = {
        "fact_accuracy",
        "insight_usefulness",
        "sales_realism",
        "timing_quality",
        "evidence_quality",
    }
    if not isinstance(scores, dict) or set(scores) != score_names:
        raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_scores", "请完成全部五项评分")
    normalized_scores = {}
    for name in score_names:
        value = scores.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 5:
            raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_scores", "每项评分必须为 1–5 分")
        normalized_scores[name] = value
    return {
        "card_version": card_version,
        "verdict": verdict,
        "scores": normalized_scores,
        "corrections": corrections,
        "notes": notes,
        "reviewer": reviewer,
    }


class ReviewPortalServer(ThreadingHTTPServer):
    daemon_threads = True

    def __init__(
        self,
        address: Tuple[str, int],
        *,
        db_path: Path,
        access_code: str,
        run_id: str,
        allowed_hosts: Sequence[str],
    ) -> None:
        super().__init__(address, ReviewPortalHandler)
        self.db_path = db_path
        self.access_code = access_code
        self.run_id = run_id
        self.allowed_hosts = {item.strip().lower() for item in allowed_hosts if item.strip()}
        self.write_lock = threading.Lock()


class ReviewPortalHandler(BaseHTTPRequestHandler):
    server: ReviewPortalServer
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args: Any) -> None:
        path = urlsplit(getattr(self, "path", "/")).path
        print("review-portal method=%s path=%s status=%s" % (self.command, path, getattr(self, "_status", "-")))

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch("GET")

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch("POST")

    def _dispatch(self, method: str) -> None:
        try:
            split = urlsplit(self.path)
            path = unquote(split.path)
            self._check_host()
            if not path.startswith("/api/"):
                if method != "GET":
                    raise PortalError(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "不支持该操作")
                self._static(path)
                return
            self._authenticate()
            if method == "GET" and path == "/api/summary":
                self._summary()
            elif method == "GET" and path == "/api/profiles":
                self._profiles(parse_qs(split.query))
            elif method == "GET" and path.startswith("/api/profiles/"):
                self._profile(path[len("/api/profiles/") :].strip("/"))
            elif method == "POST" and path.startswith("/api/profiles/") and path.endswith("/review"):
                profile_id = path[len("/api/profiles/") : -len("/review")].strip("/")
                self._save_review(profile_id)
            else:
                raise PortalError(HTTPStatus.NOT_FOUND, "not_found", "页面接口不存在")
        except PortalError as error:
            self._error(error)
        except (BrokenPipeError, ConnectionResetError):
            return
        except Exception:
            traceback.print_exc()
            self._error(PortalError(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "服务暂时不可用"))

    def _check_host(self) -> None:
        host = self.headers.get("Host", "").strip().lower()
        if host.startswith("["):
            hostname = host.split("]", 1)[0].lstrip("[")
        else:
            hostname = host.rsplit(":", 1)[0] if host.count(":") == 1 else host
        if not hostname or hostname not in self.server.allowed_hosts:
            raise PortalError(HTTPStatus.FORBIDDEN, "host_denied", "访问地址未获允许")
        origin = self.headers.get("Origin", "").rstrip("/")
        if origin and origin not in {"http://" + host, "https://" + host}:
            raise PortalError(HTTPStatus.FORBIDDEN, "origin_denied", "跨站请求已拒绝")

    def _authenticate(self) -> None:
        supplied = self.headers.get("X-Review-Access-Code", "").strip()
        if not supplied or not hmac.compare_digest(supplied, self.server.access_code):
            raise PortalError(HTTPStatus.UNAUTHORIZED, "unauthorized", "访问码不正确")

    def _db(self, *, write: bool = False) -> sqlite3.Connection:
        if not self.server.db_path.is_file():
            raise PortalError(HTTPStatus.SERVICE_UNAVAILABLE, "data_unavailable", "画像数据尚未准备好")
        conn = sqlite3.connect(str(self.server.db_path), timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        if not write:
            conn.execute("PRAGMA query_only = ON")
        return conn

    def _run(self, conn: sqlite3.Connection) -> sqlite3.Row:
        fields = "sales_profile_run_id,as_of_at,status,model,created_at,completed_at"
        if self.server.run_id == "latest":
            row = conn.execute(
                "SELECT %s FROM sales_profile_runs ORDER BY created_at DESC,sales_profile_run_id DESC LIMIT 1" % fields
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT %s FROM sales_profile_runs WHERE sales_profile_run_id=?" % fields,
                (self.server.run_id,),
            ).fetchone()
        if row is None:
            raise PortalError(HTTPStatus.NOT_FOUND, "run_not_found", "画像批次不存在")
        return row

    @staticmethod
    def _profile_id(value: str) -> str:
        if not IDENTIFIER.fullmatch(value):
            raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_profile", "画像编号无效")
        return value

    def _summary(self) -> None:
        with self._db() as conn:
            run = self._run(conn)
            run_id = run["sales_profile_run_id"]
            counts = conn.execute(
                "SELECT COUNT(*) total,SUM(p.status='succeeded') generated,"
                "SUM(EXISTS(SELECT 1 FROM sales_profile_reviews rv WHERE rv.sales_profile_id=p.sales_profile_id)) reviewed "
                "FROM sales_profile_subjects s JOIN sales_profiles p ON p.subject_id=s.subject_id "
                "WHERE s.sales_profile_run_id=?",
                (run_id,),
            ).fetchone()
            verdicts = {
                row["verdict"]: int(row["n"])
                for row in conn.execute(
                    "SELECT verdict,COUNT(DISTINCT sales_profile_id) n FROM sales_profile_reviews rv "
                    "WHERE EXISTS(SELECT 1 FROM sales_profile_subjects s JOIN sales_profiles p ON p.subject_id=s.subject_id "
                    "WHERE p.sales_profile_id=rv.sales_profile_id AND s.sales_profile_run_id=?) GROUP BY verdict",
                    (run_id,),
                )
            }
        self._send_json(
            HTTPStatus.OK,
            {
                "run": dict(run),
                "total": int(counts["total"] or 0),
                "generated": int(counts["generated"] or 0),
                "reviewed": int(counts["reviewed"] or 0),
                "verdicts": verdicts,
                "inventory_assumption": "默认满库存",
                "send_allowed": False,
            },
        )

    def _profiles(self, query: Mapping[str, Sequence[str]]) -> None:
        stratum = (query.get("stratum") or [""])[0]
        status = (query.get("status") or [""])[0]
        if stratum and stratum not in STRATUM_LABELS:
            raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_stratum", "客户分层无效")
        if status not in {"", "reviewed", "unreviewed"}:
            raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_status", "审核状态无效")
        with self._db() as conn:
            run = self._run(conn)
            clauses = ["s.sales_profile_run_id=?", "p.status='succeeded'"]
            params: list[Any] = [run["sales_profile_run_id"]]
            if stratum:
                clauses.append("s.stratum=?")
                params.append(stratum)
            review_exists = "EXISTS(SELECT 1 FROM sales_profile_reviews rv WHERE rv.sales_profile_id=p.sales_profile_id)"
            if status == "reviewed":
                clauses.append(review_exists)
            elif status == "unreviewed":
                clauses.append("NOT " + review_exists)
            rows = conn.execute(
                "SELECT p.sales_profile_id,p.card_version,s.stratum,s.stratum_rank,p.status,p.updated_at,"
                "(SELECT COUNT(*) FROM sales_profile_reviews rv WHERE rv.sales_profile_id=p.sales_profile_id) review_count,"
                "(SELECT verdict FROM sales_profile_reviews rv WHERE rv.sales_profile_id=p.sales_profile_id "
                "ORDER BY updated_at DESC,review_id DESC LIMIT 1) latest_verdict "
                "FROM sales_profile_subjects s JOIN sales_profiles p ON p.subject_id=s.subject_id WHERE "
                + " AND ".join(clauses)
                + " ORDER BY CASE s.stratum WHEN 'complex_risk' THEN 1 WHEN 'future_return_wait' THEN 2 "
                "WHEN 'high_frequency' THEN 3 WHEN 'high_value' THEN 4 WHEN 'dormant_repeat' THEN 5 "
                "WHEN 'control' THEN 6 ELSE 7 END,s.stratum_rank",
                params,
            ).fetchall()
        self._send_json(
            HTTPStatus.OK,
            {
                "items": [
                    {
                        "sales_profile_id": row["sales_profile_id"],
                        "label": _label(row["stratum"], int(row["stratum_rank"])),
                        "stratum": row["stratum"],
                        "rank": int(row["stratum_rank"]),
                        "status": row["status"],
                        "review_count": int(row["review_count"] or 0),
                        "latest_verdict": row["latest_verdict"],
                        "card_version": row["card_version"],
                    }
                    for row in rows
                ],
                "total": len(rows),
                "run_id": run["sales_profile_run_id"],
                "send_allowed": False,
            },
        )

    def _profile(self, profile_id: str) -> None:
        profile_id = self._profile_id(profile_id)
        with self._db() as conn:
            run = self._run(conn)
            row = conn.execute(
                "SELECT p.sales_profile_id,p.card_version,p.profile_json,p.deterministic_facts_json,"
                "p.model,p.updated_at,s.stratum,s.stratum_rank,r.as_of_at "
                "FROM sales_profiles p JOIN sales_profile_subjects s ON s.subject_id=p.subject_id "
                "JOIN sales_profile_runs r ON r.sales_profile_run_id=s.sales_profile_run_id "
                "WHERE p.sales_profile_id=? AND s.sales_profile_run_id=? AND p.status='succeeded'",
                (profile_id, run["sales_profile_run_id"]),
            ).fetchone()
            if row is None:
                raise PortalError(HTTPStatus.NOT_FOUND, "profile_not_found", "画像不存在")
            event_rows = conn.execute(
                "SELECT e.event_type,e.event_json,e.evidence_json,e.confidence FROM sales_profile_events e "
                "JOIN sales_profile_subjects s ON s.subject_id=e.subject_id "
                "JOIN sales_profiles p ON p.subject_id=s.subject_id "
                "WHERE p.sales_profile_id=? AND e.validation_state='accepted' "
                "ORDER BY e.chunk_index,e.created_at,e.sales_profile_event_id",
                (profile_id,),
            ).fetchall()
            review_rows = conn.execute(
                "SELECT review_id,card_version,verdict,fact_accuracy,insight_usefulness,sales_realism,"
                "timing_quality,evidence_quality,corrections_json,notes,reviewer,updated_at "
                "FROM sales_profile_reviews WHERE sales_profile_id=? ORDER BY updated_at DESC,review_id DESC",
                (profile_id,),
            ).fetchall()
        self._send_json(
            HTTPStatus.OK,
            {
                "sales_profile_id": row["sales_profile_id"],
                "label": _label(row["stratum"], int(row["stratum_rank"])),
                "stratum": row["stratum"],
                "rank": int(row["stratum_rank"]),
                "card_version": row["card_version"],
                "model": row["model"],
                "as_of_at": row["as_of_at"],
                "card": _clean_value(_json(row["profile_json"], {})),
                "facts": _public_facts(row["deterministic_facts_json"]),
                "events": [_public_event(dict(item)) for item in event_rows],
                "reviews": [_public_review(dict(item)) for item in review_rows],
                "inventory_assumption": "默认满库存",
                "send_allowed": False,
            },
        )

    def _save_review(self, profile_id: str) -> None:
        profile_id = self._profile_id(profile_id)
        body = _validate_review(self._read_json())
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        review_id = "sales_profile_review_" + hashlib.sha256(
            (profile_id + "\0" + body["reviewer"]).encode("utf-8")
        ).hexdigest()[:24]
        score = body["scores"]
        with self.server.write_lock:
            with self._db(write=True) as conn:
                run = self._run(conn)
                row = conn.execute(
                    "SELECT p.card_version FROM sales_profiles p JOIN sales_profile_subjects s ON s.subject_id=p.subject_id "
                    "WHERE p.sales_profile_id=? AND s.sales_profile_run_id=? AND p.status='succeeded'",
                    (profile_id, run["sales_profile_run_id"]),
                ).fetchone()
                if row is None:
                    raise PortalError(HTTPStatus.NOT_FOUND, "profile_not_found", "画像不存在")
                current_version = str(row["card_version"] or "")
                if not hmac.compare_digest(current_version, body["card_version"]):
                    raise PortalError(HTTPStatus.CONFLICT, "card_version_conflict", "卡片已更新，请刷新后重新审核")
                conn.execute(
                    "INSERT INTO sales_profile_reviews(review_id,sales_profile_id,card_version,verdict,"
                    "fact_accuracy,insight_usefulness,sales_realism,timing_quality,evidence_quality,"
                    "corrections_json,notes,reviewer,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                    "ON CONFLICT(sales_profile_id,reviewer) DO UPDATE SET card_version=excluded.card_version,"
                    "verdict=excluded.verdict,fact_accuracy=excluded.fact_accuracy,"
                    "insight_usefulness=excluded.insight_usefulness,sales_realism=excluded.sales_realism,"
                    "timing_quality=excluded.timing_quality,evidence_quality=excluded.evidence_quality,"
                    "corrections_json=excluded.corrections_json,notes=excluded.notes,updated_at=excluded.updated_at",
                    (
                        review_id,
                        profile_id,
                        current_version,
                        body["verdict"],
                        score["fact_accuracy"],
                        score["insight_usefulness"],
                        score["sales_realism"],
                        score["timing_quality"],
                        score["evidence_quality"],
                        json.dumps(body["corrections"], ensure_ascii=False, sort_keys=True),
                        body["notes"],
                        body["reviewer"],
                        now,
                        now,
                    ),
                )
                conn.commit()
                stored = conn.execute(
                    "SELECT review_id,card_version,verdict,fact_accuracy,insight_usefulness,sales_realism,"
                    "timing_quality,evidence_quality,corrections_json,notes,reviewer,updated_at "
                    "FROM sales_profile_reviews WHERE sales_profile_id=? AND reviewer=?",
                    (profile_id, body["reviewer"]),
                ).fetchone()
        self._send_json(HTTPStatus.OK, {"review": _public_review(dict(stored)), "send_allowed": False})

    def _read_json(self) -> Any:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_body", "请求内容格式错误") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise PortalError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "body_too_large", "请求内容过大")
        if "application/json" not in self.headers.get("Content-Type", "").lower():
            raise PortalError(HTTPStatus.UNSUPPORTED_MEDIA_TYPE, "content_type", "仅接受 JSON")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise PortalError(HTTPStatus.BAD_REQUEST, "invalid_json", "请求内容不是有效 JSON") from exc

    def _headers(self, *, api: bool) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'",
        )
        self.send_header("Cache-Control", "no-store" if api else "no-cache")

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        self._status = status
        self.send_response(status)
        self._headers(api=True)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _error(self, error: PortalError) -> None:
        self._send_json(error.status, {"error": {"code": error.code, "message": error.message}})

    def _static(self, path: str) -> None:
        files = {
            "/": ("index.html", "text/html; charset=utf-8"),
            "/index.html": ("index.html", "text/html; charset=utf-8"),
            "/app.js": ("app.js", "application/javascript; charset=utf-8"),
            "/styles.css": ("styles.css", "text/css; charset=utf-8"),
        }
        item = files.get(path)
        if not item:
            raise PortalError(HTTPStatus.NOT_FOUND, "not_found", "页面不存在")
        body = (STATIC_DIR / item[0]).read_bytes()
        self._status = HTTPStatus.OK
        self.send_response(HTTPStatus.OK)
        self._headers(api=False)
        self.send_header("Content-Type", item[1])
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def create_server(
    host: str,
    port: int,
    *,
    db_path: Path,
    access_code: str,
    run_id: str = "latest",
    allowed_hosts: Sequence[str] = (),
) -> ReviewPortalServer:
    if len(access_code) < 20:
        raise RuntimeError("review portal access code must contain at least 20 characters")
    hosts = {"127.0.0.1", "localhost", "localhost.localdomain", "::1", *allowed_hosts}
    return ReviewPortalServer(
        (host, int(port)),
        db_path=Path(db_path).expanduser().resolve(),
        access_code=access_code,
        run_id=run_id,
        allowed_hosts=tuple(hosts),
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Run the anonymous sales-profile review portal")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8898)
    parser.add_argument("--db", default=str(DEFAULT_DB_PATH))
    parser.add_argument("--run-id", default="latest")
    parser.add_argument("--access-code", default=os.environ.get("WECHAT_CS_REVIEW_ACCESS_CODE", ""))
    parser.add_argument("--allowed-host", action="append", default=[])
    args = parser.parse_args(argv)
    server = create_server(
        args.host,
        args.port,
        db_path=Path(args.db),
        access_code=args.access_code,
        run_id=args.run_id,
        allowed_hosts=args.allowed_host,
    )
    print("review portal listening on http://%s:%d" % server.server_address, flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
