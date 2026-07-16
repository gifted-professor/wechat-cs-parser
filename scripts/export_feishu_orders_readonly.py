#!/usr/bin/env python3
"""Export a minimal read-only Feishu Base order envelope via lark-cli.

The output is private staging data for the existing normalization commands.  It
contains customer phone and tracking evidence, is created mode 0600 below the
project's .wechat-cs directory, and should be removed after successful import.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence


FIELDS: Sequence[tuple[str, str]] = (
    ("flduNkgmcN", "phone"),
    ("fldhhZmgbt", "pay_date"),
    ("fldmqTEiNK", "revenue"),
    ("fld5IDQFx6", "tracking_no"),
    ("fld1sKUhRY", "platform"),
    ("fldtD4YJ09", "sku_name"),
    ("fldWradhXw", "factory"),
    ("fldEyKjfO5", "category"),
    ("fldMFC3gqa", "color"),
    ("fldh2VGIFN", "size"),
    ("fldK5I7wfQ", "refund_type"),
    ("fldZrYCPHz", "refund_reason"),
    ("fldNSp737h", "refund_amount"),
    ("fldDcVqKJL", "refund_date"),
    ("fldBmeoWgh", "return_status"),
)
_SINGLE_VALUE_FIELDS = frozenset(
    {"platform", "factory", "refund_type", "return_status"}
)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _validate_output(path: Path, project_root: Path) -> Path:
    output = path.expanduser().resolve()
    private_root = (project_root.expanduser().resolve() / ".wechat-cs" / "private-staging").resolve()
    if not _is_relative_to(output, private_root):
        raise ValueError("output must stay inside .wechat-cs/private-staging")
    return output


def _page_command(
    *, base_token: str, table_id: str, offset: int, page_size: int
) -> List[str]:
    command = [
        "lark-cli",
        "base",
        "+record-list",
        "--base-token",
        base_token,
        "--table-id",
        table_id,
        "--as",
        "user",
        "--format",
        "json",
        "--offset",
        str(offset),
        "--limit",
        str(page_size),
    ]
    for field_id, _name in FIELDS:
        command.extend(("--field-id", field_id))
    return command


def _read_page(
    *, base_token: str, table_id: str, offset: int, page_size: int
) -> Mapping[str, object]:
    completed = subprocess.run(
        _page_command(
            base_token=base_token,
            table_id=table_id,
            offset=offset,
            page_size=page_size,
        ),
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if completed.returncode != 0:
        raise RuntimeError("Feishu read failed at offset %d" % offset)
    try:
        envelope = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Feishu read returned invalid JSON at offset %d" % offset) from exc
    if not isinstance(envelope, dict) or envelope.get("ok") is not True:
        raise RuntimeError("Feishu read returned an error at offset %d" % offset)
    data = envelope.get("data")
    if not isinstance(data, dict):
        raise RuntimeError("Feishu read response is missing data at offset %d" % offset)
    return data


def normalize_cell(name: str, value: object) -> object:
    """Flatten Feishu single-select cells while preserving other cell types."""

    if name not in _SINGLE_VALUE_FIELDS or not isinstance(value, list):
        return value
    if not value:
        return None
    if len(value) != 1:
        raise RuntimeError("single-select field %s returned multiple values" % name)
    return value[0]


def export_orders(
    *,
    base_token: str,
    table_id: str,
    output: Path,
    synced_at: datetime,
    page_size: int = 200,
) -> Dict[str, object]:
    if synced_at.tzinfo is None or synced_at.utcoffset() is None:
        raise ValueError("synced_at must include a timezone")
    if not 1 <= page_size <= 200:
        raise ValueError("page_size must be between 1 and 200")

    records: List[Dict[str, object]] = []
    offset = 0
    expected_fields = [item[0] for item in FIELDS]
    while True:
        page = _read_page(
            base_token=base_token,
            table_id=table_id,
            offset=offset,
            page_size=page_size,
        )
        field_ids = page.get("field_id_list")
        rows = page.get("data")
        record_ids = page.get("record_id_list")
        if field_ids != expected_fields:
            raise RuntimeError("Feishu field projection changed at offset %d" % offset)
        if not isinstance(rows, list) or not isinstance(record_ids, list) or len(rows) != len(record_ids):
            raise RuntimeError("Feishu page shape is invalid at offset %d" % offset)
        for record_id, cells in zip(record_ids, rows):
            if not isinstance(cells, list) or len(cells) != len(FIELDS):
                raise RuntimeError("Feishu row shape is invalid at offset %d" % offset)
            row = {
                name: normalize_cell(name, value)
                for (_field_id, name), value in zip(FIELDS, cells)
            }
            row["record_id"] = str(record_id)
            records.append(row)
        offset += len(rows)
        if offset and offset % 2000 == 0:
            print("read_records=%d" % offset, file=sys.stderr, flush=True)
        if page.get("has_more") is not True:
            break
        if not rows:
            raise RuntimeError("Feishu pagination stalled at offset %d" % offset)

    document = {
        "synced_at": synced_at.isoformat(timespec="seconds"),
        "total_records": len(records),
        "primary_records": len(records),
        "source": {
            "kind": "feishu_base_readonly",
            "table_id": table_id,
            "field_ids": expected_fields,
            "page_size": page_size,
        },
        "records": records,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(str(output), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
    except Exception:
        try:
            output.unlink()
        except OSError:
            pass
        raise
    os.chmod(output, 0o600)
    return {
        "output": str(output),
        "record_count": len(records),
        "synced_at": document["synced_at"],
        "mode": oct(output.stat().st_mode & 0o777),
        "read_only_source": True,
    }


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-token", required=True)
    parser.add_argument("--table-id", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--synced-at", required=True)
    parser.add_argument("--page-size", type=int, default=200)
    args = parser.parse_args(argv)
    try:
        synced_at = datetime.fromisoformat(args.synced_at.replace("Z", "+00:00"))
        output = _validate_output(Path(args.output), Path.cwd())
        result = export_orders(
            base_token=args.base_token,
            table_id=args.table_id,
            output=output,
            synced_at=synced_at,
            page_size=args.page_size,
        )
        print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
        return 0
    except Exception as exc:
        print("error: %s" % str(exc), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
