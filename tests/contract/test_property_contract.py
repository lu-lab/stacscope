"""The property contract (property_contract.py), run against the simulated
camera in virtual time.

Its table holds one of each thing the contract checks: a label changeable
only while idle (``pixel_format``), two numbers that bound each other
(``aoi_x`` and ``aoi_width``, which may not pass the sensor's edge between
them), a number changeable at any time (``exposure``), a flag, and a
read-only value.
"""

import uuid

import pytest
from property_contract import *  # noqa: F403 - the contract's tests and Rig
from property_contract import Rig

from stacscope.adapters.simulated import SimulatedCamera, VirtualClock
from stacscope.core.actors import Actor, ask, spawn


@pytest.fixture
async def rig():
    clock = VirtualClock()
    parent = Actor()
    spawn(parent, clock=clock, name="coordinator")
    device = SimulatedCamera("p" + uuid.uuid4().hex[:20], shape=(24, 40), n_slots=4)
    ref = parent.spawn_child(device, name="camera")
    await clock.advance(0)

    async def ask_device(msg):
        return await ask(ref, msg, timeout=1.0, clock=clock)

    yield Rig(
        ask=ask_device,
        moves=("aoi_width", 32.0, "aoi_x"),  # leaves room to move right
        idle=("pixel_format", "MONO8"),
        always=("exposure", 0.02),
    )
    if not device.stopped.is_set():
        device.stop()
        await device.stopped.wait()
    parent.stop()
