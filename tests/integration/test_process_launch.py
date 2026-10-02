"""X4, X5 -- a device in a process of its own -- and CR1 from the core review.

These are real-clock tests: a fresh interpreter has a real one. Everything
here is therefore bounded twice over -- by a deadline of its own and by the
fixture that kills whatever is left -- because the failure being tested is a
process that will not answer, and a suite that waits for one is no better.

The assertions are deliberately the same ones the in-process tests make. That
is the whole claim of this milestone: the Coordinator cannot tell.
"""

import asyncio
import pickle
from functools import partial

import msgpack
import pytest

from process_support import until

from stacscope.adapters.simulated import Fail, FaultyDevice, SimulatedDevice, SimulatedTimingSource
from stacscope.core.actors import Actor, ActorRef, ChildExited, Fault, HeartbeatPolicy, Subscribe, ask, spawn
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
from stacscope.core.transport import LaunchFailed, ProcessHost, child
from stacscope.core.transport.stream import read_frame, send_frame

pytestmark = pytest.mark.timeout(30)

HEARTBEAT = HeartbeatPolicy(interval=0.05, timeout=0.5)


class Parent(Actor):
    """Stands in for whatever supervises a device. Keeps its children's deaths."""

    def __init__(self) -> None:
        super().__init__()
        self.exits: list[ChildExited] = []
        self.faults: list[Fault] = []

    async def handle(self, msg):
        if isinstance(msg, ChildExited):
            self.exits.append(msg)
        if isinstance(msg, Fault):
            self.faults.append(msg)


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


async def test_cm2_a_process_child_reports_to_its_parent_by_address(host):
    """The child's parent is not in its process. Its address names the
    parent, the link reaches it, and a Fault the device reports arrives at
    the Coordinator -- which publishes it, as a root -- without anything
    subscribing to anything."""
    coordinator = Coordinator(
        source=DeviceSpec("source", SimulatedTimingSource, ready_timeout=10.0),
        participants=[
            DeviceSpec(
                "camera",
                partial(FaultyDevice, Fail.REPORT, on="StartDevice"),
                ready_timeout=10.0,
                heartbeat=HEARTBEAT,
                placement=Placement.PROCESS,
            ),
        ],
        launcher=Launcher(host),
    )
    ref = spawn(coordinator, name="engine")
    clients = Parent()
    ref.tell(Subscribe(spawn(clients, name="client")))
    await asyncio.sleep(0)

    assert await ask(ref, StartAcquisition(2, "fish"), timeout=15) == AcquisitionStarted()
    assert await until(lambda: clients.faults)

    assert clients.faults == [Fault(ActorRef(None, "engine/camera"), "the driver reported a warning", False)]
    await ask(ref, StopAcquisition(), timeout=15)


# --- CR1: a child lives as long as its link -----------------------------------


async def test_cr1_a_child_runs_until_its_link_closes_however_long_that_is():
    """CR1. Found by the core review (2026-10-02): once its device was ready,
    the child waited for "link closed or device stopped" through a helper
    still carrying the bootstrap's 30 s bound. When it ran out, the child
    stopped a healthy device and exited cleanly, so every process-placed
    device -- both cameras on the instrument -- ended 30 s after launch, and
    the Coordinator heard a clean stop. Every G13 run ended inside 30 s, so
    nothing on the hardware showed it.

    Run in this process, against the host's half of the handshake, so the
    bound can be shortened: five bounds pass, and the child is still serving."""
    links: asyncio.Queue = asyncio.Queue()

    async def accept(reader, writer):
        await links.put((reader, writer))

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    serving = asyncio.create_task(
        child.serve(f"{host}:{port}", "token", "engine/camera", bootstrap_timeout=0.2)
    )
    try:
        reader, writer = await asyncio.wait_for(links.get(), 5)
        assert msgpack.unpackb(await read_frame(reader)) == {"token": "token"}
        send_frame(writer, msgpack.packb({"factory": pickle.dumps(SimulatedDevice)}))
        assert msgpack.unpackb(await asyncio.wait_for(read_frame(reader), 5)) == {"ready": "engine/camera"}

        await asyncio.sleep(1.0)
        assert not serving.done(), "the child gave up on a healthy link"

        writer.close()
        assert await asyncio.wait_for(serving, 5) == 0, "and ends when the link does"
    finally:
        serving.cancel()
        server.close()


async def test_cr1_a_device_slower_to_start_than_the_host_waits_is_a_launch_failure():
    """The host bounds the whole bootstrap, the device's setup included. A
    setup that outlasts it raised a bare ``TimeoutError`` from ``launch``, so
    the rig reported "TimeoutError()" and nothing named the device or said
    what it had been waiting for."""
    host = ProcessHost(connect_timeout=4.0)
    await host.start()
    actor = Parent()
    spawn(actor, name="engine")
    await asyncio.sleep(0)
    try:
        with pytest.raises(LaunchFailed, match=r"camera: did not finish starting within 4\.0 s"):
            await host.launch(actor, name="camera", factory=partial(SimulatedDevice, setup_time=60.0))
        assert host.running() == 0
    finally:
        await host.close()
