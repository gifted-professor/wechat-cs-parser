from __future__ import annotations

import http.client
import json
import os
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from wechat_cs.api import ApiHandler, create_server
from wechat_cs.build import build_database


FIXTURE_EXPORT = Path(__file__).parent / "fixtures" / "export"
TOKEN = "synthetic-api-test-token"


def keys_in(value):
    if isinstance(value, dict):
        for key, item in value.items():
            yield str(key)
            yield from keys_in(item)
    elif isinstance(value, list):
        for item in value:
            yield from keys_in(item)


class ApiIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory()
        cls.db_path = Path(cls.temp_dir.name) / "api.sqlite3"
        build_database(
            str(FIXTURE_EXPORT),
            str(cls.db_path),
            account_id="synthetic-api-account",
            secret="synthetic-api-hmac-secret",
        )
        cls.server = create_server(host="127.0.0.1", port=0, db_path=cls.db_path, token=TOKEN)
        cls.thread = threading.Thread(target=cls.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        cls.thread.start()
        cls.port = cls.server.server_address[1]

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join(timeout=5)
        cls.temp_dir.cleanup()

    def request(self, method, path, payload=None, *, token=TOKEN, headers=None):
        body = None
        request_headers = {"Accept": "application/json"}
        if payload is not None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            request_headers["Content-Type"] = "application/json"
            request_headers["Content-Length"] = str(len(body))
        if token is not None:
            request_headers["Authorization"] = "Bearer " + token
        if headers:
            request_headers.update(headers)
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request(method, path, body=body, headers=request_headers)
            response = connection.getresponse()
            raw = response.read()
            parsed = json.loads(raw.decode("utf-8")) if raw else None
            return response.status, dict(response.getheaders()), parsed
        finally:
            connection.close()

    def request_raw(self, path):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        try:
            connection.request("GET", path)
            response = connection.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            connection.close()

    def test_review_workbench_static_assets_are_complete(self) -> None:
        for path, content_type, marker in (
            ("/", "text/html", b"view-opportunities"),
            ("/app.js", "application/javascript", b"/customer-insights"),
            ("/styles.css", "text/css", b".customer-row"),
            ("/favicon.svg", "image/svg+xml", b"<svg"),
        ):
            status, headers, body = self.request_raw(path)
            self.assertEqual(status, 200)
            self.assertIn(content_type, headers.get("Content-Type", ""))
            self.assertIn(marker, body)
        status, headers, body = self.request_raw("/")
        self.assertIn(b'href="styles.css"', body)
        self.assertIn(b'src="app.js"', body)
        self.assertIn(b'href="favicon.svg"', body)
        self.assertIn("default-src 'self'", headers.get("Content-Security-Policy", ""))

    def test_bearer_auth_is_required_when_token_is_configured(self) -> None:
        status, headers, payload = self.request("GET", "/v1/health", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")
        self.assertEqual(headers.get("Cache-Control"), "no-store")

    def test_health_reports_derived_counts_without_paths_or_identifiers(self) -> None:
        status, headers, payload = self.request("GET", "/v1/health")
        self.assertEqual(status, 200)
        self.assertEqual(payload["counts"]["customers"], 2)
        self.assertEqual(payload["counts"]["messages"], 7)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        forbidden = {"source_file", "conversation_username", "username", "phone", "wxid"}
        self.assertTrue(forbidden.isdisjoint(set(keys_in(payload))))

    def test_customer_endpoints_return_opaque_ids_and_no_raw_source_fields(self) -> None:
        status, _, payload = self.request("GET", "/v1/customer-insights?limit=20")
        self.assertEqual(status, 200)
        self.assertEqual(payload["total"], 2)
        customer_key = payload["items"][0]["customer_key"]
        self.assertTrue(customer_key.startswith("customer_"))
        forbidden = {"source_file", "conversation_username", "username", "avatar", "headimg"}
        self.assertTrue(forbidden.isdisjoint(set(keys_in(payload))))

        detail_status, _, detail = self.request("GET", "/v1/customers/" + customer_key)
        self.assertEqual(detail_status, 200)
        self.assertEqual(detail["customer"]["customer_key"], customer_key)
        self.assertTrue(forbidden.isdisjoint(set(keys_in(detail))))
        self.assertTrue(all(item["role"] in {"studio", "customer"} for item in detail["messages"]))

    def test_style_pair_review_is_persisted(self) -> None:
        status, _, payload = self.request("GET", "/v1/style-pairs?status=pending")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(payload["total"], 1)
        pair_id = payload["items"][0]["pair_id"]
        patch_status, _, patched = self.request(
            "PATCH",
            "/v1/style-pairs/" + pair_id,
            {"review_status": "approved", "review_reasons": ["虚构审核通过"], "reviewer": "unit-test"},
        )
        self.assertEqual(patch_status, 200)
        self.assertEqual(patched["item"]["review_status"], "approved")
        self.assertEqual(patched["item"]["review_reasons"], ["虚构审核通过"])

    def test_draft_without_kimi_key_fails_closed(self) -> None:
        _, _, insights = self.request("GET", "/v1/customer-insights")
        customer_key = insights["items"][0]["customer_key"]
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": ""}):
            status, _, payload = self.request(
                "POST", "/v1/drafts", {"customer_key": customer_key, "latest_message": "虚构客户新消息"}
            )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "kimi_unavailable")
        self.assertTrue(payload["grounding_missing"])

    def test_mocked_draft_for_dynamic_fact_is_flagged_and_feedback_round_trips(self) -> None:
        _, _, insights = self.request("GET", "/v1/customer-insights")
        customer_key = insights["items"][0]["customer_key"]
        model_result = {
            "draft_text": "我先为您核对虚构信息。",
            "intent": "presales",
            "needs_clarification": False,
            "needs_human": False,
            "risk_flags": [],
            "grounding_refs": [],
        }
        with mock.patch.dict(os.environ, {"KIMI_API_KEY": "synthetic-kimi-key"}), mock.patch.object(
            ApiHandler, "_call_kimi", return_value=model_result
        ):
            status, _, draft = self.request(
                "POST",
                "/v1/drafts",
                {"customer_key": customer_key, "latest_message": "请问虚构商品现在有库存和价格吗？"},
            )
        self.assertEqual(status, 201)
        self.assertTrue(draft["grounding_missing"])
        self.assertTrue(draft["needs_clarification"])
        self.assertTrue(draft["needs_human"])
        self.assertIn("grounding_missing", draft["risk_flags"])

        missing_status, _, missing = self.request(
            "POST", "/v1/drafts/" + draft["draft_id"] + "/feedback", {"outcome": "edited"}
        )
        self.assertEqual(missing_status, 400)
        self.assertEqual(missing["error"]["code"], "missing_final_text")

        feedback_status, _, feedback = self.request(
            "POST", "/v1/drafts/" + draft["draft_id"] + "/feedback", {"outcome": "adopted"}
        )
        self.assertEqual(feedback_status, 201)
        self.assertEqual(feedback["outcome"], "adopted")

        connection = sqlite3.connect(str(self.db_path))
        try:
            stored = connection.execute(
                "SELECT outcome,final_text FROM feedback WHERE draft_id=?", (draft["draft_id"],)
            ).fetchone()
        finally:
            connection.close()
        self.assertIsNotNone(stored)
        self.assertEqual(stored[0], "accepted")
        self.assertIn(stored[1], (None, ""))

    def test_invalid_filters_fail_with_stable_error_envelope(self) -> None:
        status, _, payload = self.request("GET", "/v1/customer-insights?level=impossible")
        self.assertEqual(status, 400)
        self.assertEqual(set(payload), {"error"})
        self.assertEqual(payload["error"]["code"], "invalid_level")


if __name__ == "__main__":
    unittest.main()
