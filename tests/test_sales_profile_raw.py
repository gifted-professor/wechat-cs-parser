from __future__ import annotations

import hashlib
import tempfile
import unittest
from pathlib import Path

from wechat_cs.live_inbox import load_live_inbox
from wechat_cs.sales_profile_raw import (
    RawSalesMessage,
    chunk_raw_messages,
    load_raw_sales_conversations,
)


FIXTURE_ROOT = Path(__file__).parent / "fixtures" / "live_inbox"
EVENTS = FIXTURE_ROOT / "events.jsonl"
STATE = FIXTURE_ROOT / "state.json"
ACCOUNTS = FIXTURE_ROOT / "accounts.json"
SECRET = "live-inbox-fixture-secret-with-at-least-32-characters"


class RawSalesConversationTests(unittest.TestCase):
    def test_selected_raw_text_restores_same_message_keys_without_touching_source(self) -> None:
        redacted = load_live_inbox(EVENTS, ACCOUNTS, secret=SECRET, state_path=STATE)
        selected_customer = next(
            customer_key
            for customer_key, rows in redacted.messages_by_customer.items()
            if any("[客户标识]" in item.text or "[微信号]" in item.text for item in rows)
        )
        before = (EVENTS.stat().st_size, EVENTS.stat().st_mtime_ns, EVENTS.read_bytes())

        raw = load_raw_sales_conversations(
            EVENTS,
            ACCOUNTS,
            customer_keys={selected_customer},
            as_of_at="2026-07-13T23:59:59+08:00",
            secret=SECRET,
        )

        self.assertEqual(set(raw.messages_by_customer), {selected_customer})
        raw_rows = raw.messages_by_customer[selected_customer]
        redacted_rows = redacted.messages_by_customer[selected_customer]
        self.assertEqual(
            {item.message_key for item in raw_rows},
            {item.message_key for item in redacted_rows},
        )
        self.assertTrue(any("customer-raw" in item.text or "studio-sender" in item.text for item in raw_rows))
        self.assertEqual(
            (EVENTS.stat().st_size, EVENTS.stat().st_mtime_ns, EVENTS.read_bytes()),
            before,
        )

    def test_cutoff_and_customer_filter_are_strict(self) -> None:
        redacted = load_live_inbox(EVENTS, ACCOUNTS, secret=SECRET, state_path=STATE)
        selected_customer = min(
            redacted.messages_by_customer,
            key=lambda customer_key: redacted.messages_by_customer[customer_key][0].timestamp,
        )
        raw = load_raw_sales_conversations(
            EVENTS,
            ACCOUNTS,
            customer_keys={selected_customer},
            as_of_at="2026-07-13T10:02:00+08:00",
            secret=SECRET,
        )
        rows = raw.messages_by_customer[selected_customer]
        self.assertEqual(len(rows), 1)
        self.assertLessEqual(rows[0].timestamp, "2026-07-13T10:02:00+08:00")
        self.assertEqual(raw.missing_customer_keys, ())

    def test_missing_selected_customer_is_reported_without_fabrication(self) -> None:
        raw = load_raw_sales_conversations(
            EVENTS,
            ACCOUNTS,
            customer_keys={"customer_" + "f" * 24},
            as_of_at="2026-07-13T23:59:59+08:00",
            secret=SECRET,
        )
        self.assertEqual(raw.messages_by_customer, {})
        self.assertEqual(raw.missing_customer_keys, ("customer_" + "f" * 24,))

    def test_frozen_snapshot_accepts_append_only_growth_but_rejects_changed_prefix(self) -> None:
        redacted = load_live_inbox(EVENTS, ACCOUNTS, secret=SECRET, state_path=STATE)
        selected_customer = next(iter(redacted.messages_by_customer))
        frozen = EVENTS.read_bytes()
        with tempfile.TemporaryDirectory() as temp_dir:
            copied_events = Path(temp_dir) / "events.jsonl"
            copied_accounts = Path(temp_dir) / "accounts.json"
            copied_events.write_bytes(frozen + b'\n{"future":"append"}\n')
            copied_accounts.write_bytes(ACCOUNTS.read_bytes())
            raw = load_raw_sales_conversations(
                copied_events,
                copied_accounts,
                customer_keys={selected_customer},
                as_of_at="2026-07-13T23:59:59+08:00",
                secret=SECRET,
                snapshot_size=len(frozen),
                snapshot_sha256=hashlib.sha256(frozen).hexdigest(),
                account_config_sha256=hashlib.sha256(ACCOUNTS.read_bytes()).hexdigest(),
            )
            self.assertEqual(raw.scanned_record_count, len(frozen.splitlines()))

            changed = bytearray(copied_events.read_bytes())
            changed[0] = ord("[") if changed[0] != ord("[") else ord("{")
            copied_events.write_bytes(bytes(changed))
            with self.assertRaisesRegex(RuntimeError, "frozen snapshot"):
                load_raw_sales_conversations(
                    copied_events,
                    copied_accounts,
                    customer_keys={selected_customer},
                    as_of_at="2026-07-13T23:59:59+08:00",
                    secret=SECRET,
                    snapshot_size=len(frozen),
                    snapshot_sha256=hashlib.sha256(frozen).hexdigest(),
                )

    def test_frozen_snapshot_rejects_account_config_drift(self) -> None:
        redacted = load_live_inbox(EVENTS, ACCOUNTS, secret=SECRET, state_path=STATE)
        with self.assertRaisesRegex(RuntimeError, "account config"):
            load_raw_sales_conversations(
                EVENTS,
                ACCOUNTS,
                customer_keys={next(iter(redacted.messages_by_customer))},
                as_of_at="2026-07-13T23:59:59+08:00",
                secret=SECRET,
                account_config_sha256="f" * 64,
            )

    def test_chunking_keeps_turns_whole_and_every_message_exactly_once(self) -> None:
        rows = []
        for index in range(8):
            role = "studio" if index % 2 == 0 else "customer"
            rows.append(
                RawSalesMessage(
                    message_key=f"message-{index}",
                    customer_key="customer-1",
                    profile_id="aolai1",
                    role=role,
                    timestamp=f"2026-07-01T{index + 8:02d}:00:00+08:00",
                    text="甲" * 10,
                    event_id=f"event-{index}",
                    source_ordinal=index + 1,
                )
            )
        chunks = chunk_raw_messages(rows, max_chars=25, max_messages=3)
        flattened = [item for chunk in chunks for item in chunk]
        self.assertEqual(flattened, rows)
        self.assertEqual(len({item.message_key for item in flattened}), len(rows))
        self.assertTrue(all(len(chunk) <= 2 for chunk in chunks))

    def test_single_oversized_turn_is_not_split(self) -> None:
        rows = [
            RawSalesMessage(
                message_key=f"m-{index}",
                customer_key="customer-1",
                profile_id="aolai1",
                role="customer",
                timestamp=f"2026-07-01T10:{index:02d}:00+08:00",
                text="很长的一句话",
                event_id=f"e-{index}",
                source_ordinal=index + 1,
            )
            for index in range(4)
        ]
        chunks = chunk_raw_messages(rows, max_chars=5, max_messages=1)
        self.assertEqual(chunks, (tuple(rows),))


if __name__ == "__main__":
    unittest.main()
