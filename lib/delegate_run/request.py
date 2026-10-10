"""Parse and validate a delegate-run request (JSON on stdin)."""
from __future__ import annotations

from .contract import Request, RequestError  # noqa: F401


def parse_request(raw: bytes | str) -> Request:
    """Return a validated Request or raise RequestError. Never runs anything."""
    raise NotImplementedError
