"""A reader's own position in the ring, for reading every frame in order."""

from dataclasses import dataclass
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:
    from stacscope.core.buffer.frame_buffer import FrameReader


class Overrun(Exception):
    """The producer lapped this reader: ``skipped`` frames were lost. The
    cursor has already moved to the oldest safe frame."""

    def __init__(self, skipped: int):
        super().__init__(f"{skipped} frame(s) overwritten before they were read")
        self.skipped = skipped


@dataclass(frozen=True, eq=False)
class Frame:
    """One frame: its global index, its acquisition, and a view of its pixels.

    ``data`` is a view into shared memory, not a copy. Check
    ``reader.still_valid(index)`` after using it to know it was not overwritten
    meanwhile; ``data.copy()`` is the copy if one is wanted.
    """

    index: int
    acquisition_id: int
    data: np.ndarray


class Cursor:
    def __init__(self, reader: "FrameReader", position: int):
        self._reader = reader
        self.position = position

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
        return Frame(index, reader.acquisition_id, reader.frames[reader.slot_of(index)])
