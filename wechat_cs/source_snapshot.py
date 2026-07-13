"""Read-only source snapshots and derived-output safety guards."""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass(frozen=True)
class StableBytes:
    path: Path
    data: bytes
    device: int
    inode: int
    size: int
    mtime_ns: int
    sha256: str


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def assert_project_output(path: Path, project_root: Path) -> None:
    """Require every derived write to stay below ``<project>/.wechat-cs``."""

    root = Path(project_root).expanduser().resolve()
    candidate = Path(path).expanduser().resolve()
    derived_root = (root / ".wechat-cs").resolve()
    if candidate == derived_root or not _is_relative_to(candidate, derived_root):
        raise ValueError("output path must stay inside the project .wechat-cs directory")


def hmac_key_fingerprint(secret: str) -> str:
    """Return a one-way comparison token without persisting the HMAC secret."""

    if not isinstance(secret, str) or not secret:
        raise ValueError("HMAC secret must not be empty")
    digest = hmac.new(
        b"wechat-cs-hmac-key-fingerprint-v1",
        secret.encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    return "hmac-key-v1:%s" % digest


def _signature(stat_result: os.stat_result) -> tuple[int, int, int, int]:
    return (
        int(stat_result.st_dev),
        int(stat_result.st_ino),
        int(stat_result.st_size),
        int(stat_result.st_mtime_ns),
    )


def read_stable_bytes(path: Path, *, retries: int = 1) -> StableBytes:
    """Read bytes without modifying the source and reject concurrent changes."""

    source = Path(path).expanduser().resolve()
    attempts = max(0, int(retries)) + 1
    last_error: Optional[RuntimeError] = None
    for _ in range(attempts):
        before = source.stat()
        with source.open("rb") as handle:
            opened = os.fstat(handle.fileno())
            data = handle.read()
            finished = os.fstat(handle.fileno())
        after = source.stat()
        signatures = {
            _signature(before),
            _signature(opened),
            _signature(finished),
            _signature(after),
        }
        if len(signatures) == 1 and len(data) == before.st_size:
            return StableBytes(
                path=source,
                data=data,
                device=int(before.st_dev),
                inode=int(before.st_ino),
                size=int(before.st_size),
                mtime_ns=int(before.st_mtime_ns),
                sha256=hashlib.sha256(data).hexdigest(),
            )
        last_error = RuntimeError("source_changed_during_run")
    assert last_error is not None
    raise last_error
