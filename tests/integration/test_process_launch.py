"""X4, X5 -- a device in a process of its own.

These are real-clock tests: a fresh interpreter has a real one. Everything
here is therefore bounded twice over -- by a deadline of its own and by the
fixture that kills whatever is left -- because the failure being tested is a
process that will not answer, and a suite that waits for one is no better.

The assertions are deliberately the same ones the in-process tests make. That
is the whole claim of this milestone: the Coordinator cannot tell.
"""

import asyncio
from functools import partial

import pytest

from stacscope.adapters.simulated import SimulatedDevice, SimulatedTimingSource
from stacscope.core.actors import Actor, ChildExited, HeartbeatPolicy, ask, spawn
from stacscope.core.protocol import (
    AcquisitionStarted,
    AcquisitionStopped,
    DeviceReady,
    DeviceStopped,
    StartAcquisition,
    StartDevice,
    StopAcquisition,
    StopDevice,
)
from stacscope.core.timing import Coordinator, CoordinatorState, DeviceSpec, Launcher, Placement
from stacscope.core.transport import LaunchFailed, ProcessHost

pytestmark = pytest.mark.timeout(30)

HEARTBEAT = HeartbeatPolicy(interval=0.05, timeout=0.5)


class Parent(Actor):
    """Stands in for whatever supervises a device. Keeps its children's deaths."""

    def __init__(self) -> None:
        super().__init__()
        self.exits: list[ChildExited] = []

    async def handle(self, msg):
        if isinstance(msg, ChildExited):
            self.exits.append(msg)


@pytest.fixture
async def host():
    host = ProcessHost(connect_timeout=15.0)
    await host.start()
    try:
        yield host
    finally:
        await host.close()  # kills anything a failed assertion left running


@pytest.fixture
async def parent():
    actor = Parent()
    spawn(actor, name="engine")
    await asyncio.sleep(0)
    return actor


async def test_a_device_in_its_own_process_speaks_the_same_protocol(host, parent):
    device = await host.launch(parent, name="camera", factory=SimulatedDevice)

    assert await ask(device, StartDevice("fish", 3), timeout=10) == DeviceReady()
    assert await ask(device, StopDevice(), timeout=10) == DeviceStopped()
    assert device.address == "engine/camera"


async def test_a_device_is_configured_by_what_crosses_not_by_what_is_shared(host, parent):
    """The factory is rebuilt in the child, so its configuration has to travel
    with it. Nothing of the parent's object graph does."""
    device = await host.launch(parent, name="camera", factory=partial(SimulatedDevice, 0.2))

    started = asyncio.get_running_loop().time()
    assert await ask(device, StartDevice("", 1), timeout=10) == DeviceReady()

    assert asyncio.get_running_loop().time() - started >= 0.2


async def test_a_factory_that_cannot_be_rebuilt_is_refused_before_anything_starts(host, parent):
    """X5. A closure cannot cross. Better to hear so at launch than to find out
    at the start of an experiment."""
    with pytest.raises(LaunchFailed, match="cannot be rebuilt"):
        await host.launch(parent, name="camera", factory=lambda: SimulatedDevice())

    assert host.running() == 0


async def test_a_device_that_fails_to_construct_reports_why_from_the_child(host, parent):
    """It is the child that discovers this, and the parent that must hear it."""
    with pytest.raises(LaunchFailed, match="failed to initialize"):
        await host.launch(parent, name="camera", factory=partial(SimulatedDevice, fail_in_setup=True))

    assert host.running() == 0


async def test_an_acquisition_runs_with_one_participant_in_another_process(host):
    """X4. Identical to the in-process rig, but for one line of config."""
    coordinator = Coordinator(
        source=DeviceSpec("source", SimulatedTimingSource, ready_timeout=10.0),
        participants=[
            DeviceSpec("shutter", SimulatedDevice, ready_timeout=10.0),
            DeviceSpec(
                "camera",
                SimulatedDevice,
                ready_timeout=10.0,
                heartbeat=HEARTBEAT,
                placement=Placement.PROCESS,
            ),
        ],
        launcher=Launcher(host),
    )
    ref = spawn(coordinator, name="engine")
    await asyncio.sleep(0)

    assert await ask(ref, StartAcquisition(2, "fish"), timeout=15) == AcquisitionStarted()
    assert await ask(ref, StopAcquisition(), timeout=15) == AcquisitionStopped()

    assert coordinator.state is CoordinatorState.IDLE
    assert set(coordinator.participants) == {"shutter", "camera"}
    assert host.running() == 1  # and the camera really was somewhere else
