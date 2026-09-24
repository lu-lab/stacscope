"""The FrameCapture port contract (frame_capture_contract.py), run against the
simulated adapter in virtual time.

Triggers travel the way they do on the rig: a ``SimulatedSignalOutput`` plays
DO pulses, and ``TriggerBus`` turns their rising edges into the capture's
trigger. A frame count here is therefore caused by pulses that played.

Two formats: a plain C-order frame, and one shaped like a DCAM buffer, with
row padding, a top offset and page-aligned slots, so that strides, offset and
alignment are each exercised rather than defaulted.
"""

import numpy as np
import pytest
from frame_capture_contract import *  # noqa: F403 - the contract's tests, Rig, buffers
from frame_capture_contract import SENTINEL, _capturing

from stacscope.adapters.simulated import (
    SimulatedFrameCapture,
    SimulatedSignalOutput,
    TriggerBus,
    VirtualClock,
)
from stacscope.core.ports import Channel, OutputConfig

RATE = 1024.0  # samples per second: binary-exact virtual times
CONFIG = OutputConfig(
    rate=RATE,
    ao=(Channel("Dev1/ao0", "etl"),),
    do=(Channel("Dev1/port0/line0", "camera_trigger"),),
    buffer_samples=1024,
)

FORMATS = {
    "plain": dict(shape=(24, 40), dtype="<u2"),
    "padded": dict(shape=(24, 40), dtype="<u2", row_bytes=96, frame_offset=64, slot_alignment=4096),
}


@pytest.fixture(params=FORMATS.values(), ids=FORMATS.keys())
async def rig(request):
    clock = VirtualClock()
    bus = TriggerBus(clock)
    output = SimulatedSignalOutput(clock, bus)
    await output.configure(CONFIG)
    capture = SimulatedFrameCapture(clock, bus, trigger="camera_trigger", **request.param)

    async def trigger(n: int) -> None:
        """``n`` one-sample pulses, a sample apart, played and done."""
        do = np.zeros((1, 2 * n), dtype=bool)
        do[0, ::2] = True
        await output.arm()
        await output.write(np.zeros((1, 2 * n)), do)
        await output.go()
        await clock.advance(2 * n / RATE)
        await output.stop()

    async def lose(n: int) -> None:
        capture.drop(n)
        await trigger(n)

    yield Rig(capture, trigger, clock.advance, lose=lose)
    await capture.stop()
    await capture.close()


# --- the simulated adapter's own behaviour, beyond the contract ------------------


async def test_the_image_is_a_bar_that_moves_one_column_per_frame(rig, buffers):
    """What a viewer shows: a bar four columns wide, one column further on
    each frame, on black. The contract cannot say what hardware images look
    like, so this is what shows the pixels were drawn at all."""
    writer = await _capturing(rig, buffers)
    await rig.trigger(2)
    bright = np.iinfo(writer.frames.dtype).max

    for index, frame in enumerate(writer.frames[:2]):
        lit = np.flatnonzero((frame == bright).all(axis=0))
        assert list(lit) == [index, index + 1, index + 2, index + 3]
        assert np.count_nonzero(frame) == frame.shape[0] * 4


async def test_pixels_are_written_where_the_format_places_them(rig, buffers):
    """Only the pixels move: the bytes before the first pixel and past the end
    of each row are as the slot was given. A write off by the frame offset or
    the row pitch would still change the slot, and so pass the contract,
    while every consumer reading through the header saw a skewed image."""
    writer = await _capturing(rig, buffers)
    fmt = rig.capture.frame_format()
    await rig.trigger(1)

    slot = writer.slots[0]
    touched = np.zeros(len(slot), bool)
    rows, cols = fmt.shape
    row_bytes, itemsize = fmt.strides
    for row in range(rows):
        start = fmt.frame_offset + row * row_bytes
        touched[start : start + cols * itemsize] = True

    assert (slot[~touched] == SENTINEL).all()
