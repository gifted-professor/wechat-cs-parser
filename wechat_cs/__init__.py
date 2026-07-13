"""Local-only WeChat customer-service analysis toolkit.

The package deliberately has no WeChat automation or network client.  It reads a
plaintext export, writes a local SQLite database, and exposes deterministic
analysis primitives for the HTTP layer.
"""

from .core import DEFAULT_HMAC_SECRET, extract_mainland_phones, hmac_id, redact_text
from .store import get_health, initialize_schema, open_store

__all__ = [
    "DEFAULT_HMAC_SECRET",
    "get_health",
    "extract_mainland_phones",
    "hmac_id",
    "initialize_schema",
    "open_store",
    "redact_text",
]

__version__ = "0.1.0"
