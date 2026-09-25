"""FrameCapture port: a frame grabber writing frames into memory it is given.

Modelled on a camera driver that DMAs each frame into a buffer the program
allocated: the program hands over a set of slots, starts capture, and each
trigger lands a frame in the next slot, from the first slot and wrapping. The
adapter reports how many frames arrived and what the hardware said about each.
Losing a frame is an error, never a silent gap.
"""

from dataclasses import dataclass
from typing import Protocol

import numpy as np

from stacscope.core.buffer.metadata import Metadata

STAMP_DTYPE = np.dtype([("hw_frame", "<u8"), ("device_ticks", "<i8"), ("host_time_ns", "<i8")])
"""One frame's stamps, as ``FrameCapture.stamps`` returns them. The fields mean
what the buffer's per-frame metadata record of the same names means."""


@dataclass(frozen=True, slots=True)
class FrameFormat:
    """Where the hardware puts one frame within its slot.

    Offsets and strides are in bytes from the start of the slot. ``frame_bytes``
    is everything the hardware writes per frame, ``frame_offset`` and any
    padding included, so a slot must hold at least that much. Slots must start
    on a multiple of ``slot_alignment`` (a power of two). ``in_flight`` is how
    many frames the hardware may be writing ahead of the newest it reported.
    """

    shape: tuple[int, ...]
    dtype: str
    strides: tuple[int, ...]
    frame_bytes: int
    frame_offset: int = 0
    slot_alignment: int = 1
    in_flight: int = 1


class FramesLost(RuntimeError):
    """Frames were exposed but never delivered. ``after`` is how many frames of
    this acquisition arrived before the gap, and ``lost`` how many are missing,
    or ``None`` if the hardware reported a loss without saying how many."""

    def __init__(self, after: int, lost: int | None) -> None:
        how_many = "an unknown number of frames" if lost is None else f"{lost} frame(s)"
        super().__init__(f"{how_many} lost after frame {after}")
        self.after = after
        self.lost = lost


# Deliberately dumb: the counter, the header, the metadata table, and deciding
# what may be published all belong to the ImageStreaming role above it, so each
# hardware adapter stays thin.
class FrameCapture(Protocol):
    def frame_format(self) -> FrameFormat:
        """How the next capture will place frames, given the device's current
        settings. Ask again after changing any setting."""
        ...

    def metadata(self) -> Metadata:
        """Which of ``hw_frame``, ``device_ticks`` and ``host_time_ns`` the
        stamps carry, and where device time comes from."""
        ...

    async def attach(self, slots: np.ndarray) -> None:
        """Hand over the frame memory: a writable ``(n_slots, slot_bytes)``
        ``uint8`` array, one row per slot, laid out for ``frame_format()``.
        Raises ``ValueError`` if the slots are not bytes, are smaller than
        ``frame_bytes``, or do not start on ``slot_alignment``."""
        ...

    async def start(self) -> None:
        """Begin capture into slot 0. Ready for the first trigger on return."""
        ...

    async def wait_frames(self, seen: int, timeout: float) -> int:
        """Return how many frames this capture has delivered, once that is
        more than ``seen``, or after ``timeout`` seconds, or promptly once
        ``stop`` is called. Raises ``FramesLost`` if frames were lost."""
        ...

    def stamps(self, first: int, n: int) -> np.ndarray:
        """Stamps of frames ``first`` to ``first + n - 1`` of this capture, as
        an array of ``STAMP_DTYPE``."""
        ...

    async def stop(self) -> None:
        """Stop capture. Safe in any state, any number of times."""
        ...

    async def detach(self) -> None:
        """Give the frame memory back: afterwards the adapter holds no
        reference to any slot. Stops capture first if it is running."""
        ...

    async def close(self) -> None:
        """Release the device, detaching first. Safe in any state, any number
        of times. Afterwards ``stop`` and ``detach`` do nothing, and every
        other call raises ``RuntimeError``."""
        ...
