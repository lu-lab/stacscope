"""The ImageStreaming role contract (F1-F6), run against the OrcaFlash device
over the fake DCAM library (fake_dcam.py).

The contract's tests are in tests/contract/image_streaming_contract.py; this
module supplies only the rig. DCAM's blocking wait runs in a thread, so this
runs on a real clock: ``elapse`` sleeps, and every step allows ``GRACE`` for
a thread to hand its result back to the event loop. The fake stands in for
the DAQ's trigger line with ``pulse``.

Windows only, like the adapter, and not part of the default run:

    uv run pytest tests/adapters/dcam
"""
# For end use and not for commercial purposes.

import asyncio
import pathlib
import sys
import uuid

import numpy as np
import pytest
from fake_dcam import FakeDcam

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))

from image_streaming_contract import *  # noqa: E402, F403 - the contract's tests, Rig, consumer
from image_streaming_contract import Listener, Rig  # noqa: E402

from stacscope.adapters.dcam import OrcaFlash  # noqa: E402
from stacscope.core.actors import Actor, Subscribe, ask, spawn  # noqa: E402

GRACE = 0.25
"""Seconds allowed for a DCAM wait's thread to return and its caller to run."""

FORMATS = {
    "plain": dict(),
    "padded": dict(row_bytes=96, top_offset=64),
}
"""The fake's options. Slots are packed at DCAM's frame bytes, as the
Flash's are."""

RESHAPED = 12  # rows, after reformat


@pytest.fixture(params=FORMATS.values(), ids=FORMATS.keys())
async def rig(request):
    fake = FakeDcam(**request.param)
    parent = Actor()
    spawn(parent, name="coordinator")
    name = "f" + uuid.uuid4().hex[:20]
    device = OrcaFlash(name, library=fake, n_slots=6, capacity_bytes=1 << 20, max_slots=16)
    ref = parent.spawn_child(device, name="camera")
    listener = Listener()
    ref.tell(Subscribe(spawn(listener)))
    await device.started.wait()

    async def trigger(n: int) -> None:
        fake.pulse(n)
        fake.settle()
        await asyncio.sleep(GRACE)

    async def elapse(seconds: float) -> None:
        await asyncio.sleep(seconds + GRACE)

    def burst(n: int) -> None:
        fake.pulse(n)
        fake.settle()

    def lose(n: int) -> None:
        fake.lose(n)

    def reformat() -> None:
        fake.reshape(RESHAPED)

    def image(i: int) -> np.ndarray:
        # The fake writes every byte of a frame's pixels as i % 200 + 1.
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
