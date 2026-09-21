"""X6-X9 -- the reason the boundary exists.

Thesis 3.6: a driver that segfaults should kill one process, not the engine.
These tests are the claim itself, so they use a device that really does fail
-- a real read of address zero, a real blocked event loop -- rather than one
that reports failing.

Every death must arrive as exactly one ``ChildExited``, the same message an
in-process child's death produces, because nothing above supervision should
need a second way to hear about it.
"""

import asyncio
from functools import partial

import pytest
from process_support import FAST_HEARTBEAT, PATIENT_HEARTBEAT, until

from stacscope.adapters.simulated import Fail, FaultyDevice, SimulatedDevice, SimulatedTimingSource
from stacscope.core.actors import Accepted, ask, spawn
from stacscope.core.protocol import (
    AcquisitionStopped,
    DeviceReady,
    Relaunch,
    StartAcquisition,
    StartDevice,
    StopAcquisition,
)
from stacscope.core.timing import Coordinator, CoordinatorState, DeviceSpec, Launcher, Placement
from stacscope.core.transport import ProcessHost

pytestmark = pytest.mark.timeout(30)


def failing(mode: Fail):
    return partial(FaultyDevice, mode)


async def test_a_clean_exit_is_reported_once_and_frees_the_name(host, parent):
    """X6. The ordinary ending: the device stopped itself and the process went."""
    device = await host.launch(parent, name="camera", factory=failing(Fail.QUIT))

    device.tell(StartDevice("", 1))

    assert await until(lambda: parent.exits)
    [exited] = parent.exits
    assert exited.child == device
    assert exited.error is None
    assert host.running() == 0
    await host.launch(parent, name="camera", factory=SimulatedDevice)  # the name is free


@pytest.mark.parametrize("mode", [Fail.CRASH, Fail.EXIT], ids=["segfault", "exit"])
async def test_a_process_that_dies_is_reported_at_once_naming_the_status(host, parent, mode):
    """X7. The heartbeat here is deliberately slow: if it were what noticed,
    this would take five seconds instead of milliseconds. A dying process needs
    nothing to be running in the child, which is what makes it the better death
    signal -- an aborted process runs no Python at all.

    What the reason says depends on which signal arrived: an exit status when
    the process has been reaped, the closed link when it has not. Either names
    the process, and either is a death."""
    device = await host.launch(
        parent, name="camera", factory=failing(mode), heartbeat=PATIENT_HEARTBEAT
    )
    process = host.process("camera")
    started = asyncio.get_running_loop().time()

    device.tell(StartDevice("", 1))

    assert await until(lambda: parent.exits, deadline=3.0), (
        f"no death reported; the child's returncode is {process.returncode!r}"
        " (None means it is still running, so it never died at all)"
    )
    assert asyncio.get_running_loop().time() - started < 2.0
    [exited] = parent.exits
    assert "process" in exited.error
    assert host.running() == 0


async def test_a_device_that_raises_says_why_across_the_boundary(host, parent):
    """An exception is what ``ChildExited`` carries in this process, so it must
    be what it carries from another one. Losing it to "exited with status 0"
    would throw the diagnosis away at the moment it is wanted."""
    device = await host.launch(
        parent, name="camera", factory=failing(Fail.RAISE), heartbeat=PATIENT_HEARTBEAT
    )

    device.tell(StartDevice("", 1))

    assert await until(lambda: parent.exits, deadline=3.0)
    [exited] = parent.exits
    assert "unrecoverable error" in exited.error


async def test_a_process_alive_but_no_longer_answering_is_found_by_heartbeat(host, parent):
    """X8. A blocking call into a driver that never returns: the process is up,
    its link is open, and nothing is running to notice. Only the heartbeat can
    tell, which is why a process child never goes without one."""
    device = await host.launch(
        parent, name="camera", factory=failing(Fail.BLOCK), heartbeat=FAST_HEARTBEAT
    )

    device.tell(StartDevice("", 1))

    assert await until(lambda: parent.exits, deadline=3.0)
    [exited] = parent.exits
    assert "heartbeat" in exited.error
    assert host.running() == 0


async def test_a_process_child_is_given_a_heartbeat_even_when_none_was_asked_for(parent):
    """Decision, deliberately not left to config: the link catches a process
    that dies, and only a heartbeat catches one that is alive and no longer
    answering. The safe answer must not be one a spec can forget."""
    host = ProcessHost(heartbeat=FAST_HEARTBEAT)
    await host.start()
    try:
        device = await host.launch(parent, name="camera", factory=failing(Fail.BLOCK))

        device.tell(StartDevice("", 1))

        assert await until(lambda: parent.exits, deadline=3.0)
        assert "heartbeat" in parent.exits[0].error
    finally:
        await host.close()


async def test_closing_the_link_lets_the_device_release_its_hardware(host, parent, tmp_path):
    """The orderly shutdown. A child is asked to go by closing its link, and it
    stops its device first -- because ``teardown`` is where a camera is handed
    back, and a process killed outright never runs one."""
    note = tmp_path / "released.txt"
    await host.launch(parent, name="camera", factory=partial(FaultyDevice, note=str(note)))

    started = asyncio.get_running_loop().time()
    await host.close()

    assert note.read_text(encoding="utf-8") == "released"
    # Promptly, too: letting the interpreter tear the process down would also
    # leave a note, but only after the grace period and only for a teardown
    # that never awaits anything. A device asked to stop stops.
    assert asyncio.get_running_loop().time() - started < 2.0


async def test_shutting_down_after_a_death_waits_for_nothing(parent):
    """A dead child's socket is nobody's to hold. Left open on this side,
    shutdown waits on a connection that will never close -- which is how this
    suite first hung, and the reason that wait is bounded at all."""
    host = ProcessHost(heartbeat=FAST_HEARTBEAT)
    await host.start()
    device = await host.launch(parent, name="camera", factory=failing(Fail.EXIT))
    device.tell(StartDevice("", 1))
    assert await until(lambda: parent.exits, deadline=3.0)

    started = asyncio.get_running_loop().time()
    await host.close()

    assert asyncio.get_running_loop().time() - started < 1.0


async def test_a_wedged_process_is_killed_rather_than_left_holding_its_device(host, parent):
    """Declaring it dead is not enough. It still has the camera open."""
    device = await host.launch(
        parent, name="camera", factory=failing(Fail.BLOCK), heartbeat=FAST_HEARTBEAT
    )
    process = host.process("camera")

    device.tell(StartDevice("", 1))

    assert await until(lambda: parent.exits, deadline=3.0)
    assert await until(lambda: process.returncode is not None, deadline=3.0)


async def test_the_engine_survives_and_relaunches_a_device_that_crashed(host):
    """X9. The claim in full: the process that segfaulted is gone, the engine
    is not, and a fresh one takes its place at the same address."""
    coordinator = Coordinator(
        source=DeviceSpec("source", partial(SimulatedTimingSource, 0.05), ready_timeout=10.0),
        participants=[
            DeviceSpec(
                "camera",
                partial(FaultyDevice, Fail.CRASH, on="SourcePhase"),
                ready_timeout=10.0,
                heartbeat=FAST_HEARTBEAT,
                placement=Placement.PROCESS,
                required=False,  # so the crash drops it rather than ending the run
            )
        ],
        launcher=Launcher(host),
    )
    engine = spawn(coordinator, name="engine")
    await asyncio.sleep(0)

    # -1 cycles: the run lasts until it is stopped, so the crash lands mid-run
    await ask(engine, StartAcquisition(-1, "fish"), timeout=15)
    assert await until(lambda: "camera" not in coordinator.participants)

    assert coordinator.state is CoordinatorState.RUNNING  # the engine did not go with it
    assert host.running() == 0
    assert await ask(engine, StopAcquisition(), timeout=15) == AcquisitionStopped()

    assert await ask(engine, Relaunch("camera"), timeout=15) == Accepted()
    camera = coordinator.participants["camera"]
    assert camera.address == "engine/camera"
    assert await ask(camera, StartDevice("", 1), timeout=10) == DeviceReady()
