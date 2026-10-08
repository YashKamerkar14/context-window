"""Time-ordered UUIDs (RFC 9562 version 7)."""

import os
import time
import uuid


def uuid7(unix_ms: int | None = None) -> uuid.UUID:
    """A version 7 UUID: 48-bit Unix time in milliseconds followed by 74 random bits.

    IDs created in different milliseconds sort by creation time. Python 3.14 adds
    `uuid.uuid7`; this keeps the project working on 3.12 and 3.13.
    """
    ms = time.time_ns() // 1_000_000 if unix_ms is None else unix_ms
    if not 0 <= ms < 1 << 48:
        raise ValueError("timestamp out of range for UUIDv7")
    rand = int.from_bytes(os.urandom(10), "big")
    rand_a = rand >> 68  # 12 bits
    rand_b = rand & ((1 << 62) - 1)  # 62 bits
    value = (ms << 80) | (0x7 << 76) | (rand_a << 64) | (0b10 << 62) | rand_b
    return uuid.UUID(int=value)


def uuid7_unix_ms(value: uuid.UUID) -> int:
    return value.int >> 80
