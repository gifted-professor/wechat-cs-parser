from __future__ import annotations

import json
import os
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from wechat_cs.__main__ import main
from wechat_cs.sales_profile_pilot import (
    DEFAULT_AS_OF_AT,
    DEFAULT_MODEL,
    DEFAULT_SOURCE_RUN_ID,
)


SECRET = "sales-profile-cli-test-secret-000000000000"


class SalesProfileCliTests(unittest.TestCase):
    def _run(self, argv: list[str]) -> tuple[int, dict]:
        output = StringIO()
        with redirect_stdout(output):
            code = main(argv)
        return code, json.loads(output.getvalue())

    @patch("wechat_cs.__main__.import_member_facts")
    def test_import_member_facts_passes_paths_and_hmac_secret(self, importer) -> None:
        importer.return_value = {"state": "imported", "persisted_facts": 5}
        with patch.dict(os.environ, {"WECHAT_CS_HMAC_SECRET": SECRET}):
            code, payload = self._run(
                [
                    "import-member-facts",
                    "--db",
                    "/tmp/pilot.sqlite3",
                    "--members",
                    "/tmp/birthday_members.json",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["state"], "imported")
        importer.assert_called_once_with(
            db_path=Path("/tmp/pilot.sqlite3"),
            source_path=Path("/tmp/birthday_members.json"),
            secret=SECRET,
        )

    @patch("wechat_cs.__main__.run_sales_profile_pilot")
    @patch("wechat_cs.__main__.prepare_sales_profile_pilot")
    def test_prepare_uses_frozen_defaults_and_never_runs_model(
        self, prepare, run
    ) -> None:
        prepare.return_value = {
            "sales_profile_run_id": "pilot-run",
            "subject_count": 50,
            "model_called": False,
        }
        with patch.dict(os.environ, {"WECHAT_CS_HMAC_SECRET": SECRET}):
            code, payload = self._run(
                ["prepare-sales-profile-pilot", "--db", "/tmp/pilot.sqlite3"]
            )

        self.assertEqual(code, 0)
        self.assertEqual(payload["subject_count"], 50)
        self.assertFalse(payload["model_called"])
        prepare.assert_called_once_with(
            Path("/tmp/pilot.sqlite3"),
            as_of_at=DEFAULT_AS_OF_AT,
            source_run_id=DEFAULT_SOURCE_RUN_ID,
            model=DEFAULT_MODEL,
            secret=SECRET,
        )
        run.assert_not_called()

    @patch("wechat_cs.__main__.run_sales_profile_pilot")
    def test_run_forwards_resume_run_id_and_concurrency(self, run) -> None:
        run.return_value = {
            "sales_profile_run_id": "pilot-run",
            "processed": 2,
            "status": "partial",
            "send_allowed": False,
        }
        with patch.dict(os.environ, {"WECHAT_CS_HMAC_SECRET": SECRET}):
            code, payload = self._run(
                [
                    "run-sales-profile-pilot",
                    "--db",
                    "/tmp/pilot.sqlite3",
                    "--events",
                    "/tmp/events.jsonl",
                    "--accounts-config",
                    "/tmp/accounts.local.json",
                    "--run-id",
                    "pilot-run",
                    "--resume",
                    "--concurrency",
                    "3",
                ]
            )

        self.assertEqual(code, 0)
        self.assertFalse(payload["send_allowed"])
        run.assert_called_once_with(
            Path("/tmp/pilot.sqlite3"),
            events_path=Path("/tmp/events.jsonl"),
            accounts_path=Path("/tmp/accounts.local.json"),
            sales_profile_run_id="pilot-run",
            resume=True,
            concurrency=3,
            secret=SECRET,
        )

    @patch("wechat_cs.__main__.run_sales_profile_pilot")
    def test_run_defaults_to_latest_with_concurrency_two(self, run) -> None:
        run.return_value = {
            "sales_profile_run_id": "latest-run",
            "processed": 50,
            "status": "complete",
        }
        with patch.dict(os.environ, {"WECHAT_CS_HMAC_SECRET": SECRET}):
            code, _payload = self._run(
                [
                    "run-sales-profile-pilot",
                    "--db",
                    "/tmp/pilot.sqlite3",
                    "--events",
                    "/tmp/events.jsonl",
                    "--accounts-config",
                    "/tmp/accounts.local.json",
                ]
            )

        self.assertEqual(code, 0)
        self.assertEqual(run.call_args.kwargs["sales_profile_run_id"], "latest")
        self.assertEqual(run.call_args.kwargs["concurrency"], 2)
        self.assertFalse(run.call_args.kwargs["resume"])


if __name__ == "__main__":
    unittest.main()
