"""``frames_written`` — the counter every producer publishes to and every
reader polls."""

# The producer writes a frame, then publishes the new count; a reader loads
# the count, then reads frames below it. That needs a release store here and
# an acquire load there. On x86-64 (TSO) plain aligned 8-byte stores and loads
# already give both, and each Python-level access is an opaque C call the
# compiler cannot reorder across, so these are plain accesses.
#
# Decision (2026-09-19): x86-64 only for now; the buffer refuses to start
# elsewhere. If ARM ever matters, a small C11 helper
# (atomic_store_explicit / atomic_load_explicit with release/acquire)
# replaces the two accesses below, and nothing else changes.

import ctypes
import platform
import sys

from stacscope.core.buffer.header import COUNTER_OFFSET

_ORDERED = {"amd64", "x86_64"}


class UnsupportedPlatform(RuntimeError):
    """The counter's plain accesses are not safe on this architecture."""


def require_ordered_memory() -> None:
    """Refuse anything but x86-64, where the plain accesses below are correct.
    32-bit x86 tears 8-byte accesses; ARM and others reorder them. A 32-bit
    interpreter on a 64-bit machine still reports AMD64, so check both."""
    machine = platform.machine()
    if machine.lower() not in _ORDERED or sys.maxsize <= 2**32:
        raise UnsupportedPlatform(
            f"the frame buffer needs x86-64 memory ordering, not {machine or 'unknown'!r}; "
            "see counter.py for the upgrade path"
        )


class Counter:
    def __init__(self, buf):
        self._cell = ctypes.c_uint64.from_buffer(buf, COUNTER_OFFSET)

    def publish(self, count: int) -> None:
        """Release store: every frame below ``count`` is written."""
        self._cell.value = count

    def published(self) -> int:
        """Acquire load: frames below the result may be read."""
        return self._cell.value

    def release(self) -> None:
        """Drop the pointer into the segment so it can close."""
        self._cell = None
