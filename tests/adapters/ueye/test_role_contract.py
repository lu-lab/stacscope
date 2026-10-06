"""The ImageStreaming role contract (F1-F6), run against the UEyeCamera device
over the fake pyueye (fake_ueye.py).

The contract's tests are in tests/contract/image_streaming_contract.py; this
module supplies only the rig. The capture takes frames in a thread of its
own, so this runs on a real clock: ``elapse`` sleeps, and every step allows
``GRACE`` for the thread's news to reach the event loop. The fake stands in
for the DAQ's trigger line with ``pulse``.

Not part of the default run:

    uv run pytest tests/adapters/ueye
"""

import asyncio
import pathlib
import sys
import time
import uuid

import numpy as np
import pytest
from fake_ueye import FakeUEye

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))

from image_streaming_contract import *  # noqa: E402, F403 - the contract's tests, Rig, consumer
from image_streaming_contract import Listener, Rig  # noqa: E402

from stacscope.adapters.ueye import UEyeCamera  # noqa: E402
from stacscope.core.actors import Actor, Subscribe, ask, spawn  # noqa: E402

GRACE = 0.25
"""Seconds allowed for the capture's thread to take a frame and say so."""

FORMATS = {
    "raw8": dict(),
    "raw10": dict(pixel_format="RAW10"),
}
"""The device's options."""


@pytest.fixture(params=FORMATS.values(), ids=FORMATS.keys())
async def rig(request):
    fake = FakeUEye()
    parent = Actor()
    spawn(parent, name="coordinator")
    name = "e" + uuid.uuid4().hex[:20]
    device = UEyeCamera(
        name, ueye=fake, n_slots=6, capacity_bytes=1 << 20, max_slots=16,
        settings=[("aoi_width", 64.0), ("aoi_height", 32.0)], **request.param
    )
    ref = parent.spawn_child(device, name="camera")
    listener = Listener()
    # A child reports to its parent, and the root publishes: listen there.
    parent.ref.tell(Subscribe(spawn(listener)))
    await device.started.wait()

    async def trigger(n: int) -> None:
        fake.pulse(n)
        fake.settle()
        await asyncio.sleep(GRACE)

    async def elapse(seconds: float) -> None:
        await asyncio.sleep(seconds + GRACE)

    def burst(n: int) -> None:
        # The event loop is held here, as a stalled publisher's would be;
        # the capture's own thread goes on taking frames meanwhile, and a
        # stall lasts until it has taken them all.
        with device.capture._state:
            target = device.capture._delivered + n
        fake.pulse(n)
        fake.settle()
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline:
            with device.capture._state:
                if device.capture._delivered >= target:
                    return
            time.sleep(0.001)

    def lose(n: int) -> None:
        fake.lose(n)

    def reformat() -> None:
        device.capture.set_aoi(0, 0, 32, 16)

    def image(i: int) -> np.ndarray:
        # The fake writes every byte of frame i's pixels as i % 200 + 1.
        fmt = device.capture.frame_format()
        pixel = bytes([i % 200 + 1]) * np.dtype(fmt.dtype).itemsize
        return np.full(fmt.shape, int.from_bytes(pixel, "little"), fmt.dtype)

    async def ask_device(msg):
        return await ask(ref, msg, timeout=2.0)

    yield Rig(
        device=device,
        ref=ref,
        buffer_name=name,
        trigger=trigger,
        elapse=elapse,
        ask=ask_device,
        listener=listener,
        burst=burst,
        lose=lose,
        reformat=reformat,
        image=image,
    )
    if not device.stopped.is_set():
        device.stop()
        await device.stopped.wait()
    listener.stop()
    parent.stop()
    assert fake.misuse == []
