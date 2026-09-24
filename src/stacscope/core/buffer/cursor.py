"""A reader's own position in the ring, for reading every frame in order."""

import asyncio
import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from stacscope.core.buffer.image_buffer import FrameReader


class Overrun(Exception):
    """The producer lapped this reader: ``skipped`` frames were lost. The
    cursor has already moved to the oldest safe frame."""

    def __init__(self, skipped: int):
        super().__init__(f"{skipped} frame(s) overwritten before they were read")
        self.skipped = skipped


@dataclass(frozen=True, eq=False)
class Frame:
    """One frame: its global index, its acquisition, and a view of its pixels.

    ``data`` is a view into shared memory, not a copy, and the reader cannot
    close while it is alive. Check ``reader.still_valid(index)`` after using
    it to know it was not overwritten meanwhile; ``data.copy()`` is the copy
    if one is wanted. ``meta`` is a copy
    of the frame's metadata record (40 bytes).
    """

    index: int
    acquisition_id: int
    data: np.ndarray
    meta: np.void


@dataclass(frozen=True, eq=False)
class Group:
    """A complete group of frames (a stack, say), ``first`` to ``first + len - 1``.

    ``data`` is one contiguous view when the group does not wrap the ring (set
    ``n_slots`` to a multiple of the group size to guarantee it), otherwise a
    sequence of per-frame views. Either way, no copies.
    """

    first: int
    acquisition_id: int
    data: np.ndarray | Sequence[np.ndarray]
    meta: np.ndarray


class Cursor:
    def __init__(self, reader: "FrameReader", position: int):
        self._reader = reader
        self.position = position

    def wait(self, timeout: float | None = None, poll: float = 0.001) -> bool:
        """Block until a frame past this cursor is published. False on timeout.
        Polls: the producer never signals, so it never knows readers exist."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while self._reader.published() <= self.position:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(poll)
        return True

    async def wait_async(self, timeout: float | None = None, poll: float = 0.001) -> bool:
        """``wait`` for an event loop: polls without blocking it."""
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        while self._reader.published() <= self.position:
            if deadline is not None and loop.time() >= deadline:
                return False
            await asyncio.sleep(poll)
        return True

    def next(self) -> Frame | None:
        """The next frame, or None if the producer has not published it yet.
        Raises ``Overrun`` once for each gap, then carries on after it."""
        reader = self._reader
        oldest, published = reader._window()
        if self.position < oldest:
            skipped = oldest - self.position
            self.position = oldest
            raise Overrun(skipped)
        if self.position >= published:
            return None
        index = self.position
        self.position += 1
        return _frame(reader, index)

    def latest(self) -> Frame | None:
        """The newest frame, if it is newer than this cursor's position.
        Frames skipped on the way are not an error."""
        reader = self._reader
        oldest, published = reader._window()
        newest = published - 1
        if newest < max(self.position, oldest):
            return None
        self.position = published
        return _frame(reader, newest)

    def latest_group(self, size: int) -> Group | None:
        """The newest complete group of ``size`` frames, counted from the
        acquisition's first frame, if it is newer than this cursor's position
        and none of it has been overwritten."""
        reader = self._reader
        oldest, published = reader._window()
        layout = reader._layout
        if layout is None:
            return None
        if size > layout.n_slots - layout.in_flight:
            raise ValueError(f"a group of {size} can never fit in the readable slots")
        start = reader.acquisition_start
        complete = (published - start) // size
        if complete == 0:
            return None
        first = start + (complete - 1) * size
        if first < max(self.position, oldest):
            return None
        self.position = first + size
        slots = [reader.slot_of(i) for i in range(first, first + size)]
        if slots[0] + size <= layout.n_slots:
            data = reader.frames[slots[0] : slots[0] + size]
        else:
            data = tuple(reader.frames[s] for s in slots)
        return Group(first, reader.acquisition_id, data, reader.meta[slots])


def _frame(reader, index):
    slot = reader.slot_of(index)
    return Frame(index, reader.acquisition_id, reader.frames[slot], reader.meta[slot : slot + 1].copy()[0])
