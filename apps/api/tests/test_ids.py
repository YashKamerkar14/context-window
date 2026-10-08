import time
import uuid

import pytest

from context_window.ids import uuid7, uuid7_unix_ms


def test_version_and_variant() -> None:
    value = uuid7()
    assert value.version == 7
    assert value.variant == uuid.RFC_4122


def test_embeds_the_current_time() -> None:
    before = time.time_ns() // 1_000_000
    value = uuid7()
    after = time.time_ns() // 1_000_000
    assert before <= uuid7_unix_ms(value) <= after


def test_sorts_by_time_across_milliseconds() -> None:
    ids = [uuid7(unix_ms=ms) for ms in (1_700_000_000_000, 1_700_000_000_001, 1_800_000_000_000)]
    assert sorted(ids) == ids


def test_unique() -> None:
    assert len({uuid7() for _ in range(10_000)}) == 10_000


@pytest.mark.parametrize("ms", [-1, 1 << 48])
def test_rejects_out_of_range_time(ms: int) -> None:
    with pytest.raises(ValueError):
        uuid7(unix_ms=ms)
