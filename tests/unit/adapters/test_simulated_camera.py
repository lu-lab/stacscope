"""The simulated camera answers triggers only while it is armed.

A trigger line keeps pulsing whether or not any particular device wants the
frames — the uEye's does, with no actor behind it at all — so a camera that is
not armed must count the pulse and write nothing.
"""

import uuid

import pytest

from stacscope.adapters.simulated import SimulatedCamera, TriggerBus, VirtualClock
from stacscope.core.actors import ask, spawn
from stacscope.core.protocol import DeviceReady, StartDevice, StopDevice


@pytest.fixture
async def camera():
    clock = VirtualClock()
    bus = TriggerBus(clock)
    device = SimulatedCamera(
        "t" + uuid.uuid4().hex[:20], bus=bus, trigger="camera_trigger", shape=(4, 8), n_slots=4
    )
    ref = spawn(device, clock=clock, name="camera")
    await clock.advance(0)
    yield device, ref, clock
    device.stop()
    ref.tell(StopDevice())
    await device.stopped.wait()


async def test_a_trigger_before_arming_is_counted_and_written_nowhere(camera):
    device, _, _ = camera

    device.capture._triggered()
    device.capture._triggered()

    assert device.capture.missed == 2
    assert device.writer.published() == 0


async def test_a_trigger_after_stopping_is_counted_too(camera):
    device, ref, clock = camera
    assert await ask(ref, StartDevice("run-01", 4), timeout=1.0, clock=clock) == DeviceReady()
    device.capture._triggered()

    await ask(ref, StopDevice(), timeout=1.0, clock=clock)
    device.capture._triggered()

    assert device.writer.published() == 1, "only the one that arrived while armed"
    assert device.capture.missed == 1
