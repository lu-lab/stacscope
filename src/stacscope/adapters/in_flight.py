"""Measure how far ahead a frame grabber writes.

A grabber reports the newest frame it has finished, but it may already be
writing the frames after it. How many is its in-flight depth, which every
capture adapter declares, and which a consumer relies on: a slot within that
depth of being reused must not be read. Most drivers do not document it, so
it is measured, once per camera and per readout setting.

measure_in_flight fills a set of slots with a sentinel byte, starts a
capture, and, as frames arrive, looks at the slots past the newest reported
frame for any that are no longer the sentinel. It then asks for the count
again, so that a frame that finished while it looked is not mistaken for one
in flight. The furthest written slot past that count is the observed depth.
Choose a sentinel the camera cannot produce at the exposure used, and
declare the observed depth plus whatever margin you want: a depth the
camera never showed cannot be ruled out by watching.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np

from stacscope.core.ports import FrameCapture


@dataclass(frozen=True)
class InFlight:
    """What a measurement saw. ``depth`` is the furthest past the newest
    reported frame that a written slot was found; ``partial`` counts the
    times a slot past the newest had its first row written and its last not,
    a frame caught mid-write; ``frames`` is how many the capture delivered and
    ``polls`` how many times the slots were looked at."""

    depth: int
    partial: int
    frames: int
    polls: int


async def measure_in_flight(
    capture: FrameCapture,
    slots: np.ndarray,
    *,
    frames: int,
    fire: Callable[[], Awaitable[None]],
    sentinel: int,
    scan: int = 8,
    max_polls: int = 1_000_000,
) -> InFlight:
    """Run one capture of ``frames`` frames into ``slots`` and measure its
    in-flight depth, looking up to ``scan`` slots past the newest frame.

    ``capture`` must be open with nothing attached; it is stopped and
    detached afterwards. ``slots`` is memory laid out for its
    ``frame_format()``, with at least ``frames + 2 * scan`` slots so that
    nothing wraps. ``fire`` is awaited before each look: have it send one
    trigger, or, if something else is triggering the camera, just yield.
    ``sentinel`` is the byte every slot is filled with once attached. Gives up after
    ``max_polls`` looks.
    """
    try:
        if len(slots) < frames + 2 * scan:
            raise ValueError(
                f"{len(slots)} slots are too few: {frames} frames and a scan of {scan} "
                f"need at least {frames + 2 * scan}, so that nothing wraps"
            )
        first_row, last_row = _rows(capture.frame_format())
        depth = partial = polls = delivered = 0
        await capture.attach(slots)
        # After the attach, which may overwrite the slots (the uEye's driver
        # zeroes memory it is given).
        slots[...] = sentinel
        try:
            await capture.start()
            while delivered < frames and polls < max_polls:
                await fire()
                before = await capture.wait_frames(delivered, 0)
                polls += 1
                # Slot ``before`` holds the frame after the newest reported;
                # nothing has wrapped, so any written byte there is a frame
                # the grabber had begun.
                written = [
                    (
                        slot,
                        bool(np.any(slots[slot, first_row] != sentinel)),
                        bool(np.any(slots[slot, last_row] != sentinel)),
                    )
                    for slot in range(before, min(before + scan, len(slots)))
                ]
                # Frames go on finishing while the slots are read, and at
                # hundreds a second a scan spans several. Measured against
                # the count before it, those would count as depth; against
                # the count after it, only frames begun and still not
                # reported do.
                delivered = await capture.wait_frames(before, 0)
                for slot, began, ended in written:
                    if slot < delivered:
                        continue
                    if began or ended:
                        depth = max(depth, slot - delivered + 1)
                    if began and not ended:
                        partial += 1
        finally:
            await capture.stop()
            await capture.detach()
        return InFlight(depth=depth, partial=partial, frames=delivered, polls=polls)
    except BaseException:
        # The error's traceback keeps this frame, and the slots, alive, and
        # the caller's segment cannot close while a view of it is alive: a
        # script closing it in a finally would get BufferError in place of
        # the camera's error.
        del slots
        raise


def _rows(fmt) -> tuple[slice, slice]:
    """The bytes of a frame's first and last rows, within its slot."""
    itemsize = np.dtype(fmt.dtype).itemsize
    row = fmt.shape[-1] * itemsize
    first = fmt.frame_offset
    last = fmt.frame_offset + (fmt.shape[0] - 1) * fmt.strides[0]
    return slice(first, first + row), slice(last, last + row)
