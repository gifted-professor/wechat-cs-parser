from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, Mapping, Optional, Sequence, Tuple


DEFAULT_KIMI_BASE_URL = "https://api.moonshot.cn/v1"
MAX_RESPONSE_BYTES = 4 * 1024 * 1024
MAX_ATTEMPTS = 3

JsonValidator = Callable[[Dict[str, Any]], Any]


class KimiClientError(RuntimeError):
    """Base error for a failed Kimi JSON request."""

    category = "unknown"


class KimiCredentialError(KimiClientError):
    category = "credential"

    def __init__(self, message: str, *, status_code: Optional[int] = None) -> None:
        super().__init__(message)
        self.status_code = status_code


class KimiHttpError(KimiClientError):
    category = "http"

    def __init__(self, status_code: int, *, retryable: bool) -> None:
        super().__init__("Kimi HTTP request failed with status %d" % status_code)
        self.status_code = status_code
        self.retryable = retryable


class KimiNetworkError(KimiClientError):
    category = "network"


class KimiInvalidJsonError(KimiClientError):
    category = "invalid_json"


class KimiSchemaError(KimiClientError):
    category = "schema"


class KimiJsonClient:
    """Small OpenAI-compatible client that returns a validated JSON object."""

    def __init__(
        self,
        *,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        opener: Optional[Any] = None,
        sleeper: Optional[Callable[[float], None]] = None,
        validator: Optional[JsonValidator] = None,
        retry_backoff_seconds: float = 0.25,
    ) -> None:
        configured_key = os.environ.get("KIMI_API_KEY", "") if api_key is None else api_key
        configured_base = os.environ.get("KIMI_BASE_URL", DEFAULT_KIMI_BASE_URL) if base_url is None else base_url
        self.api_key = str(configured_key or "").strip()
        self.base_url = str(configured_base or DEFAULT_KIMI_BASE_URL).strip().rstrip("/")
        self.opener = opener or urllib.request.urlopen
        self.sleeper = sleeper or time.sleep
        self.validator = validator
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))

    def complete_json(
        self,
        messages: Sequence[Mapping[str, str]],
        model: str,
        temperature: float,
        timeout_seconds: float,
        *,
        validator: Optional[JsonValidator] = None,
    ) -> Dict[str, Any]:
        if not self.api_key:
            raise KimiCredentialError("KIMI_API_KEY is required")

        try:
            timeout = float(timeout_seconds)
            request_temperature = float(temperature)
        except (TypeError, ValueError) as exc:
            raise KimiSchemaError("Kimi request settings are invalid") from exc
        if timeout <= 0:
            raise KimiSchemaError("Kimi timeout must be positive")
        timeout = min(timeout, 120.0)

        request_payload = {
            "model": str(model),
            "messages": list(messages),
            "temperature": request_temperature,
            "response_format": {"type": "json_object"},
        }
        try:
            encoded_payload = json.dumps(request_payload, ensure_ascii=False).encode("utf-8")
        except (TypeError, ValueError) as exc:
            raise KimiSchemaError("Kimi request payload is not JSON serializable") from exc

        request = urllib.request.Request(
            self.base_url + "/chat/completions",
            data=encoded_payload,
            headers={
                "Authorization": "Bearer " + self.api_key,
                "Content-Type": "application/json",
                "User-Agent": "wechat-cs-local/1.0",
            },
            method="POST",
        )

        raw = self._request_with_retries(request, timeout)
        result = self._parse_json_object(raw)
        return self._validate(result, validator or self.validator)

    def _request_with_retries(self, request: urllib.request.Request, timeout: float) -> bytes:
        for attempt in range(MAX_ATTEMPTS):
            try:
                raw, status = self._open(request, timeout)
            except urllib.error.HTTPError as exc:
                status = int(exc.code)
                exc.close()
                if status in (401, 403):
                    raise KimiCredentialError(
                        "Kimi rejected the configured credential",
                        status_code=status,
                    ) from exc
                retryable = self._retryable_status(status)
                if retryable and attempt + 1 < MAX_ATTEMPTS:
                    self._backoff(attempt)
                    continue
                raise KimiHttpError(status, retryable=retryable) from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt + 1 < MAX_ATTEMPTS:
                    self._backoff(attempt)
                    continue
                raise KimiNetworkError("Kimi network request failed") from exc
            except OSError as exc:
                # urllib wraps retryable transport failures in URLError. A raw
                # OSError is still classified as network, but is not retried.
                raise KimiNetworkError("Kimi network request failed") from exc

            if status in (401, 403):
                raise KimiCredentialError(
                    "Kimi rejected the configured credential",
                    status_code=status,
                )
            if status >= 400:
                retryable = self._retryable_status(status)
                if retryable and attempt + 1 < MAX_ATTEMPTS:
                    self._backoff(attempt)
                    continue
                raise KimiHttpError(status, retryable=retryable)
            return raw

        raise KimiNetworkError("Kimi network request failed")

    def _open(self, request: urllib.request.Request, timeout: float) -> Tuple[bytes, int]:
        if callable(self.opener):
            response = self.opener(request, timeout=timeout)
        else:
            response = self.opener.open(request, timeout=timeout)

        if hasattr(response, "__enter__") and hasattr(response, "__exit__"):
            with response as opened:
                return self._read_response(opened)
        try:
            return self._read_response(response)
        finally:
            close = getattr(response, "close", None)
            if callable(close):
                close()

    @staticmethod
    def _read_response(response: Any) -> Tuple[bytes, int]:
        status = getattr(response, "status", None)
        if status is None:
            getcode = getattr(response, "getcode", None)
            status = getcode() if callable(getcode) else 200
        raw = response.read(MAX_RESPONSE_BYTES)
        return raw, int(status or 200)

    def _backoff(self, attempt: int) -> None:
        self.sleeper(self.retry_backoff_seconds * (2**attempt))

    @staticmethod
    def _retryable_status(status: int) -> bool:
        return status in (408, 429) or 500 <= status <= 599

    @staticmethod
    def _parse_json_object(raw: bytes) -> Dict[str, Any]:
        try:
            payload = json.loads(raw.decode("utf-8"))
            content = payload["choices"][0]["message"]["content"]
            if isinstance(content, list):
                content = "".join(
                    str(item.get("text", "")) if isinstance(item, Mapping) else str(item)
                    for item in content
                )
            text = str(content).strip()
            if text.startswith("```"):
                text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.IGNORECASE)
            result = json.loads(text)
        except (KeyError, IndexError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise KimiInvalidJsonError("Kimi returned an invalid JSON response") from exc
        if not isinstance(result, dict):
            raise KimiInvalidJsonError("Kimi response must be a JSON object")
        return result

    @staticmethod
    def _validate(result: Dict[str, Any], validator: Optional[JsonValidator]) -> Dict[str, Any]:
        if validator is None:
            return result
        if not callable(validator):
            raise KimiSchemaError("Kimi response validator is not callable")
        try:
            validated = validator(result)
        except KimiSchemaError:
            raise
        except Exception as exc:
            raise KimiSchemaError("Kimi response failed schema validation") from exc
        if validated is False:
            raise KimiSchemaError("Kimi response failed schema validation")
        if validated is None or validated is True:
            return result
        if isinstance(validated, Mapping):
            return dict(validated)
        raise KimiSchemaError("Kimi response validator returned an unsupported value")


__all__ = [
    "KimiClientError",
    "KimiCredentialError",
    "KimiHttpError",
    "KimiInvalidJsonError",
    "KimiJsonClient",
    "KimiNetworkError",
    "KimiSchemaError",
]
