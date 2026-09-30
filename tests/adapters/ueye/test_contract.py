"""U1: the FrameCapture port contract, run against UEyeFrameCapture over the
fake pyueye (fake_ueye.py).

The contract's tests are in tests/contract/frame_capture_contract.py; this
module supplies only the rig. The adapter runs the image queue's blocking
wait in a thread, so this runs on a real clock: ``elapse`` sleeps, and every
step allows ``GRACE`` for a thread to hand its result back to the event
loop. The camera runs the same contract in 5f-3.

The fake stands in for the DAQ's trigger line with ``pulse``, and a trigger
here returns once the driver has finished every frame it began. The AOI is
made small, so that each test's slots are a few kilobytes, not megabytes.

Not part of the default run:

    uv run pytest tests/adapters/ueye
"""

import asyncio
import pathlib
import sys

import pytest
from fake_ueye import FakeUEye

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))

from frame_capture_contract import *  # noqa: E402, F403 - the contract's tests, Rig, buffers
from frame_capture_contract import Rig  # noqa: E402

from stacscope.adapters.ueye import UEyeFrameCapture  # noqa: E402

GRACE = 0.25
"""Seconds allowed for a queue wait's thread to return and its caller to run."""

FORMATS = {
    "raw8": (dict(), dict()),
    "raw10": (dict(), dict(pixel_format="RAW10")),
    "aligned": (dict(), dict(slot_alignment=4096)),
}
"""The fake's options, then the adapter's. By default the adapter asks for no
alignment, so one format asks for some, or the contract's refusal of
unaligned slots would never run against the uEye."""


@pytest.fixture(params=FORMATS.values(), ids=FORMATS.keys())
async def rig(request):
    fake_options, capture_options = request.param
    fake = FakeUEye(**fake_options)
    capture = UEyeFrameCapture(1, ueye=fake, in_flight=1, **capture_options)
    capture.set_aoi(0, 0, 64, 32)

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
