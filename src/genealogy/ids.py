from __future__ import annotations

import secrets
import time
import uuid

from .model import PREFIXES


def uuid7(timestamp_ms: int | None = None) -> uuid.UUID:
    """Generate an RFC 9562 UUIDv7 using cryptographic randomness."""
    timestamp_ms = timestamp_ms if timestamp_ms is not None else time.time_ns() // 1_000_000
    if not 0 <= timestamp_ms < 2**48:
        raise ValueError("timestamp outside UUIDv7 range")
    value = timestamp_ms << 80
    value |= 0x7 << 76
    value |= secrets.randbits(12) << 64
    value |= 0b10 << 62
    value |= secrets.randbits(62)
    return uuid.UUID(int=value)


def allocate(entity_type: str, count: int = 1) -> list[str]:
    if entity_type not in PREFIXES:
        raise ValueError(f"unknown entity type: {entity_type}")
    if not 1 <= count <= 1000:
        raise ValueError("count must be between 1 and 1000")
    return [f"{PREFIXES[entity_type]}_{uuid7()}" for _ in range(count)]
