"""Read-only knowledge registry used by the drafting service.

The first release deliberately separates source synchronization from retrieval.
Feishu Doc/Wiki and Base connectors may refresh the configured cache files, while
this module only reads those files and never writes back to Feishu.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


_WORD_RE = re.compile(r"[A-Za-z0-9_\-]+|[\u4e00-\u9fff]")


def _timestamp(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip().replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _tokens(text: str) -> set:
    return {part.lower() for part in _WORD_RE.findall(text or "")}


def _bigrams(text: str) -> set:
    compact = re.sub(r"\s+", "", (text or "").lower())
    if len(compact) < 2:
        return {compact} if compact else set()
    return {compact[index : index + 2] for index in range(len(compact) - 1)}


def _similarity(query: str, candidate: str) -> float:
    q_tokens, c_tokens = _tokens(query), _tokens(candidate)
    q_bigrams, c_bigrams = _bigrams(query), _bigrams(candidate)
    token_score = len(q_tokens & c_tokens) / max(len(q_tokens), 1)
    bigram_score = len(q_bigrams & c_bigrams) / max(len(q_bigrams), 1)
    return token_score * 0.45 + bigram_score * 0.55


@dataclass(frozen=True)
class KnowledgeHit:
    source_id: str
    source_type: str
    title: str
    content: str
    source_ref: str
    score: float
    structured: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "title": self.title,
            "content": self.content,
            "source_ref": self.source_ref,
            "score": round(self.score, 4),
            "structured": self.structured,
        }


class KnowledgeRegistry:
    """Load source metadata and search already-synchronized local caches."""

    def __init__(self, config_path: Path, workspace_root: Optional[Path] = None):
        self.config_path = Path(config_path)
        self.workspace_root = Path(workspace_root or self.config_path.parent.parent).resolve()
        self.config = self._load_config()

    def _load_config(self) -> Dict[str, Any]:
        if not self.config_path.exists():
            return {"version": 1, "sources": []}
        data = json.loads(self.config_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("sources", []), list):
            raise ValueError("knowledge source registry must contain a sources list")
        return data

    def _cache_path(self, source: Dict[str, Any]) -> Optional[Path]:
        value = str(source.get("cache_file") or "").strip()
        if not value:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = self.workspace_root / path
        return path.resolve()

    def source_status(self, now: Optional[float] = None) -> List[Dict[str, Any]]:
        now = float(now or time.time())
        statuses: List[Dict[str, Any]] = []
        for source in self.config.get("sources", []):
            cache_path = self._cache_path(source)
            configured = bool(str(source.get("url") or "").strip())
            enabled = bool(source.get("enabled"))
            exists = bool(cache_path and cache_path.is_file())
            updated_at = cache_path.stat().st_mtime if exists and cache_path else None
            max_age = int(source.get("max_age_seconds") or 0)
            age_seconds = now - updated_at if updated_at is not None else None
            fresh = bool(exists and (max_age <= 0 or (age_seconds is not None and age_seconds <= max_age)))
            if not enabled:
                state = "disabled"
            elif not configured:
                state = "unconfigured"
            elif not exists:
                state = "missing_cache"
            elif not fresh:
                state = "stale"
            else:
                state = "ready"
            statuses.append(
                {
                    "id": source.get("id"),
                    "type": source.get("type"),
                    "title": source.get("title"),
                    "enabled": enabled,
                    "configured": configured,
                    "state": state,
                    "fresh": fresh,
                    "updated_at": (
                        datetime.fromtimestamp(updated_at, tz=timezone.utc).isoformat()
                        if updated_at is not None
                        else None
                    ),
                    "age_seconds": round(age_seconds, 1) if age_seconds is not None else None,
                }
            )
        return statuses

    @staticmethod
    def _items(payload: Any) -> Iterable[Dict[str, Any]]:
        if isinstance(payload, list):
            for item in payload:
                if isinstance(item, dict):
                    yield item
                elif isinstance(item, str):
                    yield {"content": item}
            return
        if isinstance(payload, dict):
            for key in ("records", "chunks", "items", "data"):
                value = payload.get(key)
                if isinstance(value, list):
                    for item in KnowledgeRegistry._items(value):
                        yield item
                    return
            yield payload

    def search(self, query: str, limit: int = 8, now: Optional[float] = None) -> Dict[str, Any]:
        query = (query or "").strip()
        statuses = self.source_status(now=now)
        ready_ids = {status["id"] for status in statuses if status["state"] == "ready"}
        hits: List[KnowledgeHit] = []
        current_time = float(now or time.time())
        for source in self.config.get("sources", []):
            source_id = str(source.get("id") or "")
            if source_id not in ready_ids:
                continue
            cache_path = self._cache_path(source)
            if not cache_path:
                continue
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            for item in self._items(payload):
                expires_at = _timestamp(item.get("expires_at") or item.get("effective_until"))
                if expires_at is not None and expires_at < current_time:
                    continue
                title = str(item.get("title") or item.get("name") or source.get("title") or source_id)
                content = str(
                    item.get("content")
                    or item.get("text")
                    or item.get("policy")
                    or item.get("answer")
                    or ""
                ).strip()
                structured = item.get("fields") if isinstance(item.get("fields"), dict) else {}
                if not content and structured:
                    content = " ".join(f"{key}: {value}" for key, value in structured.items())
                if not content:
                    continue
                score = _similarity(query, f"{title} {content}") if query else 0.0
                if query and score <= 0:
                    continue
                hits.append(
                    KnowledgeHit(
                        source_id=source_id,
                        source_type=str(source.get("type") or "unknown"),
                        title=title,
                        content=content[:1200],
                        source_ref=str(item.get("source_ref") or item.get("url") or source.get("url") or ""),
                        score=score,
                        structured=structured,
                    )
                )
        hits.sort(key=lambda hit: (-hit.score, hit.source_id, hit.title))
        selected = hits[: max(1, min(int(limit), 20))]
        enabled = [status for status in statuses if status["enabled"]]
        return {
            "hits": [hit.as_dict() for hit in selected],
            "sources": statuses,
            "grounding_missing": not selected or any(status["state"] != "ready" for status in enabled),
        }
