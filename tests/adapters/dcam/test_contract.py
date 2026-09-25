"""Q1: the FrameCapture port contract, run against DcamFrameCapture over the
fake DCAM library (fake_dcam.py).

The contract's tests are in tests/contract/frame_capture_contract.py; this
module supplies only the rig. The adapter runs DCAM's blocking wait in a
thread, so this runs on a real clock: ``elapse`` sleeps, and every step
allows ``GRACE`` for a thread to hand its result back to the event loop.
The camera itself runs the same contract in 5d.

The fake stands in for the DAQ's trigger line with ``pulse``, and a
trigger here returns once the grabber has finished every frame it began.

Windows only, like the adapter, and not part of the default run:

    uv run pytest tests/adapters/dcam
"""
# For end use and not for commercial purposes.

import asyncio
import pathlib
import sys

import pytest
from fake_dcam import FakeDcam

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))

from frame_capture_contract import *  # noqa: E402, F403 - the contract's tests, Rig, buffers
from frame_capture_contract import Rig  # noqa: E402

from stacscope.adapters.dcam import DcamFrameCapture  # noqa: E402

GRACE = 0.25
"""Seconds allowed for a DCAM wait's thread to return and its caller to run."""

FORMATS = {
    "plain": (dict(), dict()),
    "padded": (dict(row_bytes=96, top_offset=64), dict(slot_alignment=4096)),
    "mono8": (dict(pixel_type="MONO8"), dict()),
}
"""The fake's options, then the adapter's. By default the adapter asks for
no alignment, so one format asks for some, or the contract's refusal of
unaligned slots would never run against DCAM."""


@pytest.fixture(params=FORMATS.values(), ids=FORMATS.keys())
async def rig(request):
    fake_options, capture_options = request.param
    fake = FakeDcam(**fake_options)
    capture = DcamFrameCapture(library=fake, in_flight=1, **capture_options)

    async def trigger(n: int) -> None:
        fake.pulse(n)
        fake.settle()
        await asyncio.sleep(GRACE)

    async def elapse(seconds: float) -> None:
        await asyncio.sleep(seconds + GRACE)

    async def lose(n: int) -> None:
        fake.lose(n)

    yield Rig(capture, trigger, elapse, lose=lose)
    await capture.close()
    assert fake.misuse == []
