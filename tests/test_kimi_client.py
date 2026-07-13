from __future__ import annotations

import json
import os
import unittest
import urllib.error
from unittest import mock

from wechat_cs.kimi_client import (
    KimiCredentialError,
    KimiHttpError,
    KimiInvalidJsonError,
    KimiJsonClient,
    KimiNetworkError,
    KimiSchemaError,
)


def kimi_response(content):
    return json.dumps(
        {"choices": [{"message": {"content": content}}]},
        ensure_ascii=False,
    ).encode("utf-8")


class FakeResponse:
    def __init__(self, body: bytes, *, status: int = 200) -> None:
        self.body = body
        self.status = status
        self.read_limit = None
        self.closed = False

    def read(self, limit=-1):
        self.read_limit = limit
        return self.body if limit < 0 else self.body[:limit]

    def getcode(self):
        return self.status

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.close()
        return False


class SequenceOpener:
    def __init__(self, *outcomes) -> None:
        self.outcomes = list(outcomes)
        self.calls = []

    def __call__(self, request, *, timeout):
        self.calls.append((request, timeout))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def http_error(status: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError(
        "https://api.moonshot.cn/v1/chat/completions",
        status,
        "synthetic error",
        hdrs=None,
        fp=None,
    )


class KimiJsonClientTests(unittest.TestCase):
    def test_complete_json_uses_openai_compatible_request_and_requires_object(self) -> None:
        response = FakeResponse(kimi_response("```json\n{\"answer\": 42}\n```"))
        opener = SequenceOpener(response)
        client = KimiJsonClient(
            api_key="synthetic-key",
            base_url="https://kimi.invalid/v1/",
            opener=opener,
            sleeper=lambda _: None,
        )

        result = client.complete_json(
            [{"role": "user", "content": "return JSON"}],
            model="kimi-k2.6",
            temperature=0,
            timeout_seconds=12,
        )

        self.assertEqual(result, {"answer": 42})
        self.assertEqual(len(opener.calls), 1)
        request, timeout = opener.calls[0]
        self.assertEqual(request.full_url, "https://kimi.invalid/v1/chat/completions")
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer synthetic-key")
        self.assertEqual(request.get_header("Content-type"), "application/json")
        self.assertEqual(timeout, 12.0)
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(payload["model"], "kimi-k2.6")
        self.assertEqual(payload["temperature"], 0.0)
        self.assertEqual(payload["response_format"], {"type": "json_object"})
        self.assertEqual(payload["messages"], [{"role": "user", "content": "return JSON"}])
        self.assertTrue(response.closed)

    def test_constructor_reads_key_and_base_url_from_environment(self) -> None:
        opener = SequenceOpener(FakeResponse(kimi_response('{"ok": true}')))
        with mock.patch.dict(
            os.environ,
            {"KIMI_API_KEY": "environment-key", "KIMI_BASE_URL": "https://env.invalid/v1/"},
        ):
            client = KimiJsonClient(opener=opener, sleeper=lambda _: None)
            result = client.complete_json([], "kimi-k2.6", 0.2, 45)

        self.assertEqual(result, {"ok": True})
        request, _ = opener.calls[0]
        self.assertEqual(request.full_url, "https://env.invalid/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer environment-key")

    def test_missing_api_key_is_credential_error_before_any_request(self) -> None:
        opener = SequenceOpener(FakeResponse(kimi_response('{"unused": true}')))
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": ""}):
            client = KimiJsonClient(opener=opener, sleeper=lambda _: None)
            with self.assertRaises(KimiCredentialError) as raised:
                client.complete_json([], "kimi-k2.6", 0, 45)

        self.assertEqual(raised.exception.category, "credential")
        self.assertEqual(opener.calls, [])

    def test_auth_http_errors_are_credential_errors_without_retry(self) -> None:
        for status in (401, 403):
            with self.subTest(status=status):
                sleeps = []
                opener = SequenceOpener(http_error(status))
                client = KimiJsonClient(
                    api_key="synthetic-key",
                    opener=opener,
                    sleeper=sleeps.append,
                )
                with self.assertRaises(KimiCredentialError) as raised:
                    client.complete_json([], "kimi-k2.6", 0, 45)
                self.assertEqual(raised.exception.status_code, status)
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(sleeps, [])

    def test_non_retryable_http_error_is_classified_and_not_retried(self) -> None:
        sleeps = []
        opener = SequenceOpener(http_error(400))
        client = KimiJsonClient(api_key="key", opener=opener, sleeper=sleeps.append)

        with self.assertRaises(KimiHttpError) as raised:
            client.complete_json([], "kimi-k2.6", 0, 45)

        self.assertEqual(raised.exception.category, "http")
        self.assertEqual(raised.exception.status_code, 400)
        self.assertFalse(raised.exception.retryable)
        self.assertEqual(len(opener.calls), 1)
        self.assertEqual(sleeps, [])

    def test_only_retryable_http_statuses_use_three_total_attempts(self) -> None:
        for status in (408, 429, 500, 503):
            with self.subTest(status=status):
                sleeps = []
                opener = SequenceOpener(http_error(status), http_error(status), http_error(status))
                client = KimiJsonClient(
                    api_key="key",
                    opener=opener,
                    sleeper=sleeps.append,
                    retry_backoff_seconds=0.1,
                )

                with self.assertRaises(KimiHttpError) as raised:
                    client.complete_json([], "kimi-k2.6", 0, 45)

                self.assertEqual(raised.exception.status_code, status)
                self.assertTrue(raised.exception.retryable)
                self.assertEqual(len(opener.calls), 3)
                self.assertEqual(sleeps, [0.1, 0.2])

    def test_url_error_and_timeout_retry_then_succeed(self) -> None:
        sleeps = []
        opener = SequenceOpener(
            urllib.error.URLError("synthetic network failure"),
            TimeoutError("synthetic timeout"),
            FakeResponse(kimi_response('{"recovered": true}')),
        )
        client = KimiJsonClient(
            api_key="key",
            opener=opener,
            sleeper=sleeps.append,
            retry_backoff_seconds=0.05,
        )

        result = client.complete_json([], "kimi-k2.6", 0, 45)

        self.assertEqual(result, {"recovered": True})
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(sleeps, [0.05, 0.1])

    def test_exhausted_network_retry_is_network_error(self) -> None:
        sleeps = []
        opener = SequenceOpener(
            TimeoutError("one"),
            TimeoutError("two"),
            TimeoutError("three"),
        )
        client = KimiJsonClient(api_key="key", opener=opener, sleeper=sleeps.append)

        with self.assertRaises(KimiNetworkError) as raised:
            client.complete_json([], "kimi-k2.6", 0, 45)

        self.assertEqual(raised.exception.category, "network")
        self.assertEqual(len(opener.calls), 3)
        self.assertEqual(len(sleeps), 2)

    def test_invalid_json_and_non_object_content_do_not_retry(self) -> None:
        for content in ("not-json", "[1, 2, 3]"):
            with self.subTest(content=content):
                sleeps = []
                opener = SequenceOpener(FakeResponse(kimi_response(content)))
                client = KimiJsonClient(api_key="key", opener=opener, sleeper=sleeps.append)

                with self.assertRaises(KimiInvalidJsonError) as raised:
                    client.complete_json([], "kimi-k2.6", 0, 45)

                self.assertEqual(raised.exception.category, "invalid_json")
                self.assertEqual(len(opener.calls), 1)
                self.assertEqual(sleeps, [])

    def test_validator_can_transform_result_and_schema_failure_does_not_retry(self) -> None:
        success_opener = SequenceOpener(FakeResponse(kimi_response('{"answer": "42"}')))
        client = KimiJsonClient(api_key="key", opener=success_opener, sleeper=lambda _: None)
        result = client.complete_json(
            [],
            "kimi-k2.6",
            0,
            45,
            validator=lambda value: {"answer": int(value["answer"])},
        )
        self.assertEqual(result, {"answer": 42})

        sleeps = []
        failure_opener = SequenceOpener(FakeResponse(kimi_response('{"answer": 42}')))
        client = KimiJsonClient(api_key="key", opener=failure_opener, sleeper=sleeps.append)
        with self.assertRaises(KimiSchemaError) as raised:
            client.complete_json([], "kimi-k2.6", 0, 45, validator=lambda _: False)
        self.assertEqual(raised.exception.category, "schema")
        self.assertEqual(len(failure_opener.calls), 1)
        self.assertEqual(sleeps, [])


if __name__ == "__main__":
    unittest.main()
