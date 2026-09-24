"""X10, X11 -- both flows across the boundary, and the boundary earning its keep.

The two flows cross differently and must not be confused. Control goes over
the link, as messages. Frames do not go over it at all: shared memory is
machine-local, and the consumer opens the same segment by name. That is why a
process boundary costs so little here -- only small control traffic crosses --
and why the network hop later will cost more, since data cannot follow.

X11 is the milestone's whole argument in one test. The camera's process is
killed mid-acquisition, the way a bad driver dies, and the engine neither dies
with it nor loses the frames it already has.
"""

import asyncio
import uuid
from functools import partial

import pytest
from process_support import FAST_HEARTBEAT, until

from stacscope.adapters.simulated import SimulatedFreeRunningCamera, SimulatedTimingSource
from stacscope.core.actors import Rejected, ask, spawn
from stacscope.core.buffer import ImageBuffer
from stacscope.core.protocol import (
    AcquisitionStarted,
    AcquisitionStopped,
    StartAcquisition,
    StopAcquisition,
)
from stacscope.core.timing import Coordinator, CoordinatorState, DeviceSpec, Launcher, Placement

pytestmark = pytest.mark.timeout(30)


@pytest.fixture
def buffer_name():
    return f"stacscope_x{uuid.uuid4().hex[:12]}"


@pytest.fixture
async def rig(host, buffer_name):
    """An engine whose camera is in a process of its own."""
    coordinator = Coordinator(
        source=DeviceSpec("source", partial(SimulatedTimingSource, 0.05), ready_timeout=10.0),
        participants=[
            DeviceSpec(
                "camera",
                partial(SimulatedFreeRunningCamera, buffer_name, interval=0.005),
                ready_timeout=10.0,
                heartbeat=FAST_HEARTBEAT,
                placement=Placement.PROCESS,
            )
        ],
        launcher=Launcher(host),
    )
    engine = spawn(coordinator, name="engine")
    await asyncio.sleep(0)
    return coordinator, engine


async def reader_for(name: str):
    """Open the camera's segment from this process, once the child has made it.

    A consumer is not told when a buffer appears -- it is not part of the
    control flow and has no link to the engine. It looks for the name it was
    configured with, which is exactly how a viewer will."""
    reader = None

    def appeared() -> bool:
        nonlocal reader
        try:
            reader = ImageBuffer.open(name)
        except FileNotFoundError:
            return False
        return True

    assert await until(appeared, deadline=10.0), f"no buffer named {name} appeared"
    return reader


def read(reader, count: int) -> list[int]:
    """The first ``count`` frame indices, oldest first.

    Views are dropped before returning: a numpy view into the segment keeps it
    mapped, and a reader that still holds one cannot close.
    """
    cursor = reader.cursor(reader.oldest())
    indices = []
    while len(indices) < count and (frame := cursor.next()) is not None:
        indices.append(frame.index)
    del frame, cursor
    return indices


async def test_frames_cross_by_shared_memory_while_control_crosses_the_link(rig, buffer_name):
    """X10. Neither flow is carried by the other."""
    coordinator, engine = rig
    reader = await reader_for(buffer_name)

    assert await ask(engine, StartAcquisition(-1, ""), timeout=15) == AcquisitionStarted()
    assert await until(lambda: reader.published() >= 5, deadline=10.0)
    frames = read(reader, 5)
    assert await ask(engine, StopAcquisition(), timeout=15) == AcquisitionStopped()

    assert frames == list(range(frames[0], frames[0] + 5))  # in order, none missing
    reader.close()


async def test_the_engine_survives_its_camera_process_being_killed(host, rig, buffer_name):
    """X11. Thesis 3.6, demonstrated: one process dies, the engine does not,
    and what it already recorded is still there to be read."""
    coordinator, engine = rig
    reader = await reader_for(buffer_name)
    await ask(engine, StartAcquisition(-1, ""), timeout=15)
    assert await until(lambda: reader.published() >= 3, deadline=10.0)
    taken = read(reader, 3)

    host.process("camera").kill()

    assert await until(lambda: coordinator.state is CoordinatorState.IDLE, deadline=10.0)
    assert "camera" not in coordinator.participants  # a required device, so the run ended
    # The segment outlives the process that wrote it: this consumer holds it,
    # and what it already recorded reads back the same after the death.
    assert read(reader, 3) == taken
    # Alive and answering, not latched: it refuses to start without its
    # required camera, and says which, until the user relaunches it (D5).
    refused = await ask(engine, StartAcquisition(-1, ""), timeout=15)
    assert isinstance(refused, Rejected) and "camera" in refused.reason
    assert coordinator.state is CoordinatorState.IDLE
    reader.close()
