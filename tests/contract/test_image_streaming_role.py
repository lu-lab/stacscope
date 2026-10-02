"""The ImageStreaming role contract (image_streaming_contract.py), run against
the simulated camera in virtual time.

``SimulatedCamera`` is the role over ``SimulatedFrameCapture``. Triggers
travel as they do on the rig: a ``SimulatedSignalOutput`` plays DO pulses,
and ``TriggerBus`` turns their rising edges into frames. A burst, or a loss,
calls the capture's trigger input directly, so that the frames arrive before
the device can run.

Two formats, as in the capture's own suite: a plain C-order frame, and one
shaped like a DCAM buffer, with row padding, a top offset, page-aligned slots
and two frames in flight.
"""

import uuid

import numpy as np
import pytest
from image_streaming_contract import *  # noqa: F403 - the contract's tests, Rig, consumer
from image_streaming_contract import Listener

from stacscope.adapters.simulated import (
    SimulatedCamera,
    SimulatedSignalOutput,
    TriggerBus,
    VirtualClock,
)
from stacscope.core.actors import Actor, Subscribe, ask, spawn
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
    "padded": dict(
        shape=(24, 40),
        dtype="<u2",
        row_bytes=96,
        frame_offset=64,
        slot_alignment=4096,
        in_flight=2,
    ),
}
RESHAPED = (12, 20)
BAR = 4  # the simulated capture's bar: this many columns, one further per frame


@pytest.fixture(params=FORMATS.values(), ids=FORMATS.keys())
async def rig(request):
    clock = VirtualClock()
    bus = TriggerBus(clock)
    output = SimulatedSignalOutput(clock, bus)
    await output.configure(CONFIG)
    parent = Actor()
    spawn(parent, clock=clock, name="coordinator")
    name = "f" + uuid.uuid4().hex[:20]
    device = SimulatedCamera(name, bus=bus, trigger="camera_trigger", n_slots=6, **request.param)
    ref = parent.spawn_child(device, name="camera")
    listener = Listener()
    # A child reports to its parent, and the root publishes: listen there.
    parent.ref.tell(Subscribe(spawn(listener, clock=clock)))
    await clock.advance(0)

    async def trigger(n: int) -> None:
        """``n`` one-sample pulses, a sample apart, played and done."""
        do = np.zeros((1, 2 * n), dtype=bool)
        do[0, ::2] = True
        await output.arm()
        await output.write(np.zeros((1, 2 * n)), do)
        await output.go()
        await clock.advance(2 * n / RATE)
        await output.stop()
        await clock.advance(0)

    def burst(n: int) -> None:
        for _ in range(n):
            device.capture._triggered()

    def lose(n: int) -> None:
        device.capture.drop(n)
        burst(n)

    def reformat() -> None:
        device.capture.reformat(shape=RESHAPED)

    def image(i: int) -> np.ndarray:
        fmt = device.capture.frame_format()
        frame = np.zeros(fmt.shape, fmt.dtype)
        frame[:, (np.arange(BAR) + i) % fmt.shape[1]] = np.iinfo(frame.dtype).max
        return frame

    async def ask_device(msg):
        return await ask(ref, msg, timeout=1.0, clock=clock)

    yield Rig(
        device=device,
        ref=ref,
        buffer_name=name,
        trigger=trigger,
        elapse=clock.advance,
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
