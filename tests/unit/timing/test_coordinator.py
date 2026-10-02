"""C1–C5 — the Coordinator, and CR2–CR3 from the core review.

Run against ``SimulatedTimingSource``, whose phases are no-ops: nothing here
may depend on the source being an AO board.
"""

import asyncio

import pytest
from timing_support import PERIOD, READY_TIMEOUT, Dev, Factory, Rig

from stacscope.adapters.simulated import SimulatedDevice, SimulatedTimingSource
from stacscope.core.actors import Accepted, AskTimeout, Rejected, ask
from stacscope.core.devices import NotReady, TimedDevice
from stacscope.core.protocol import (
    AcquisitionStarted,
    AcquisitionStopped,
    DeviceLaunched,
    Phase,
    Relaunch,
    StartAcquisition,
    StopAcquisition,
)
from stacscope.core.timing import CoordinatorState


# --- C1 ------------------------------------------------------------------------


async def test_c1_a_participant_that_exits_is_dropped_from_the_registry():
    # Optional: a lost required device refuses the start instead (D5).
    rig = Rig(
        camera=SimulatedDevice(),
        shutter=Dev(SimulatedDevice(fail_in_setup=True), required=False),
    )
    await rig.clock.advance(0)

    assert set(rig.coordinator.participants) == {"camera"}
    [fault] = rig.listener.faults()
    assert not fault.fatal and "shutter" in fault.reason

    assert isinstance(await rig.start(), AcquisitionStarted)
    assert rig.devices["shutter"].calls == []


# --- C2 ------------------------------------------------------------------------


async def test_c2_ready_timeout_aborts_before_any_output():
    rig = Rig(camera=SimulatedDevice(), laggard=SimulatedDevice(never_ready=True))
    starting = rig.request(StartAcquisition(-1, "run-01"))

    await rig.clock.advance(READY_TIMEOUT)

    reply = await starting
    assert isinstance(reply, Rejected) and "laggard" in reply.reason
    assert "warmup" not in rig.source.calls, "the source must never receive Go"
    assert rig.source.phase is Phase.IDLE
    assert "stop" in rig.devices["camera"].names()
    assert "stop" in rig.devices["laggard"].names(), "a late READY must not leave it armed"
    assert "laggard" in rig.coordinator.participants, "a slow device is not a dead one"
    assert rig.coordinator.state is CoordinatorState.IDLE
    assert any("laggard" in f.reason and not f.fatal for f in rig.listener.faults())


async def test_c2_the_source_itself_can_be_the_laggard():
    rig = Rig(source=SimulatedTimingSource(cycle_period=PERIOD, arm_time=3.0), camera=SimulatedDevice())
    starting = rig.request(StartAcquisition(-1, "run-01"))

    await rig.clock.advance(READY_TIMEOUT)

    assert isinstance(await starting, Rejected)
    assert "warmup" not in rig.source.calls
    assert "stop" in rig.devices["camera"].names()


class Refuser(TimedDevice):
    """Refuses every arm, as a camera with no save directory set does."""

    async def arm(self, root_filename, cycles):
        raise NotReady("no save directory is set")


async def test_cr3_a_refusal_is_reported_with_its_reason_not_as_a_timeout():
    """CR3. Found by the core review (2026-10-02): the Coordinator reduced each
    device's answer to ready-or-not, so a device that refused at once was
    reported as "not ready within timeout", and its reason -- what the user
    must fix before the next start -- reached nobody, because a ``NotReady``
    refusal publishes no Fault of its own. Each device not ready is named with
    its own reason, beside any that really did time out."""
    rig = Rig(camera=Refuser(), laggard=SimulatedDevice(never_ready=True))
    starting = rig.request(StartAcquisition(-1, "run-01"))

    await rig.clock.advance(READY_TIMEOUT)

    reply = await starting
    assert isinstance(reply, Rejected)
    assert "camera (no save directory is set)" in reply.reason
    assert f"laggard (no reply within {READY_TIMEOUT} s)" in reply.reason
    assert any("no save directory is set" in f.reason for f in rig.listener.faults()), (
        "a client that did not ask hears why too"
    )
    assert rig.coordinator.state is CoordinatorState.IDLE


# --- C3 ------------------------------------------------------------------------


async def test_c3_go_waits_for_the_slowest_participant():
    rig = Rig(
        camera=SimulatedDevice(ready_delay=1.5),
        shutter=SimulatedDevice(ready_delay=0.5),
    )
    starting = rig.request(StartAcquisition(-1, "run-01"))

    await rig.clock.advance(1.0)
    assert rig.source.phase is Phase.ARMED, "armed, but the camera is not ready yet"

    await rig.clock.advance(1.0)
    assert isinstance(await starting, AcquisitionStarted)
    assert rig.source.phase is Phase.RUN
    assert rig.coordinator.state is CoordinatorState.RUNNING
    assert rig.listener.faults() == [], "a clean start draws no rejection from anyone"


async def test_c3_every_participant_is_started_with_the_root_filename():
    rig = Rig(camera=SimulatedDevice())
    await rig.start(cycles=-1)

    assert rig.devices["camera"].calls[0][:2] == ("start", "run-01")
    assert rig.source.calls[0] == ("arm", "run-01", -1)


# --- C4 ------------------------------------------------------------------------


async def test_c4_stop_finishes_the_source_before_stopping_participants():
    rig = Rig(
        source=SimulatedTimingSource(cycle_period=PERIOD, drain_time=2.0),
        camera=SimulatedDevice(),
    )
    await rig.start(cycles=-1)
    await rig.clock.advance(3.5)

    stopping = rig.request(StopAcquisition())
    await rig.clock.advance(PERIOD)  # current cycle ends; the source drains
    assert not stopping.done()
    assert "stop" not in rig.devices["camera"].names(), "camera must outlast the output"

    await rig.clock.advance(2.0)

    assert isinstance(await stopping, AcquisitionStopped)
    [(_, stopped_at)] = [c for c in rig.devices["camera"].calls if c[0] == "stop"]
    assert stopped_at == 6.0  # drain finished at 4.0 + 2.0
    assert rig.coordinator.state is CoordinatorState.IDLE


async def test_c4_a_finite_acquisition_stops_everything_on_its_own():
    rig = Rig(camera=SimulatedDevice())
    await rig.start(cycles=2)

    await rig.clock.advance(5.0)

    assert rig.source.calls.count("cycle") == 2
    assert "stop" in rig.devices["camera"].names()
    assert rig.coordinator.state is CoordinatorState.IDLE


async def test_c4_commands_in_the_wrong_state_are_rejected():
    rig = Rig(camera=SimulatedDevice())

    stop = rig.request(StopAcquisition())
    await rig.clock.advance(0)
    assert isinstance(await stop, Rejected)

    await rig.start()
    again = rig.request(StartAcquisition(1, "run-02"))
    await rig.clock.advance(0)
    assert isinstance(await again, Rejected)


class Stray:
    """A message the Coordinator has no use for, as a source's own events are."""


async def test_c4_a_message_the_coordinator_has_no_use_for_is_ignored():
    """Told or asked, it gets no answer and publishes nothing (user,
    2026-09-30). The Coordinator hears everything its source publishes, so
    until then every event it had no case for went out to every participant
    and client as a Fault."""
    rig = Rig(camera=SimulatedDevice())
    await rig.clock.advance(0)
    heard = len(rig.listener.heard)

    rig.ref.tell(Stray())
    asking = asyncio.create_task(ask(rig.ref, Stray(), timeout=1.0, clock=rig.clock))
    await rig.clock.advance(2.0)

    with pytest.raises(AskTimeout):
        await asking
    assert rig.listener.heard[heard:] == []
    assert rig.coordinator.state is CoordinatorState.IDLE


# --- C5 ------------------------------------------------------------------------


async def test_c5_losing_the_source_stops_everything_and_refuses_the_next_start():
    """The source is a device that is always required (CM22: FAULTED is
    folded into that rule). Losing it mid-run stops everyone; every later
    start is refused, naming it, until it is relaunched."""
    rig = Rig(
        source=SimulatedTimingSource(cycle_period=PERIOD, fail_on_cycle=2),
        camera=SimulatedDevice(),
    )
    await rig.start(cycles=-1)

    await rig.clock.advance(3.0)

    assert rig.coordinator.state is CoordinatorState.IDLE
    assert "stop" in rig.devices["camera"].names()
    assert any("timing source lost" in f.reason for f in rig.listener.faults())

    retry = rig.request(StartAcquisition(-1, "run-02"))
    await rig.clock.advance(0)
    reply = await retry
    assert isinstance(reply, Rejected) and "source" in reply.reason


# --- Relaunch: possible, never automatic -------------------------------------------
#
# When to relaunch is a decision for the user (a fault may be a hardware stop
# they must deal with first). The Coordinator only makes it possible.


class BrokenSource(SimulatedTimingSource):
    async def setup(self):
        raise RuntimeError("hardware still wedged")


def failing_then_healthy_source():
    return Factory(
        lambda: SimulatedTimingSource(cycle_period=PERIOD, fail_on_cycle=2),
        lambda: SimulatedTimingSource(cycle_period=PERIOD),
    )


async def test_a_lost_source_is_reported_with_its_cause_and_nothing_relaunches_on_its_own():
    source = failing_then_healthy_source()
    rig = Rig(source=source, camera=SimulatedDevice())
    await rig.start()
    await rig.clock.advance(100.0)

    assert rig.coordinator.source is None
    assert len(source.made) == 1
    [lost] = [f for f in rig.listener.faults() if "timing source lost" in f.reason]
    assert "simulated source failure on cycle 2" in lost.reason


async def test_relaunching_the_source_makes_acquisitions_possible_again():
    source = failing_then_healthy_source()
    camera = SimulatedDevice()
    rig = Rig(source=source, camera=camera)
    await rig.start()
    await rig.clock.advance(3.0)
    assert rig.coordinator.source is None

    assert await rig.call(Relaunch("source")) == Accepted()
    assert rig.coordinator.state is CoordinatorState.IDLE
    assert DeviceLaunched("source", rig.coordinator.source) in rig.listener.heard

    assert isinstance(await rig.start(cycles=2), AcquisitionStarted)
    await rig.clock.advance(5.0)

    fresh = source.made[-1]
    assert fresh is not source.made[0], "a relaunch uses a new instance"
    assert fresh.calls.count("cycle") == 2
    assert rig.coordinator.state is CoordinatorState.IDLE, "heard the new source finish"
    assert camera.names() == ["start", "stop", "start", "stop"]


async def test_a_relaunched_source_that_fails_to_start_is_lost_again_and_says_why():
    source = Factory(
        lambda: SimulatedTimingSource(cycle_period=PERIOD, fail_on_cycle=1),
        BrokenSource,
    )
    rig = Rig(source=source, camera=SimulatedDevice())
    await rig.start()
    await rig.clock.advance(2.0)

    assert await rig.call(Relaunch("source")) == Accepted()
    await rig.clock.advance(0)

    assert rig.coordinator.state is CoordinatorState.IDLE
    assert rig.coordinator.source is None
    assert any("timing source lost" in f.reason and "hardware still wedged" in f.reason for f in rig.listener.faults())


async def test_relaunching_a_lost_participant_brings_it_back_into_acquisitions():
    shutter = Factory(lambda: SimulatedDevice(fail_in_setup=True), SimulatedDevice)
    rig = Rig(camera=SimulatedDevice(), shutter=shutter)
    await rig.clock.advance(0)
    assert "shutter" not in rig.coordinator.participants

    assert await rig.call(Relaunch("shutter")) == Accepted()
    assert DeviceLaunched("shutter", rig.coordinator.participants["shutter"]) in rig.listener.heard

    assert isinstance(await rig.start(), AcquisitionStarted)
    assert shutter.made[-1].names() == ["start"]


async def test_relaunch_is_refused_unless_the_device_is_gone_and_nothing_is_running():
    shutter = Factory(lambda: SimulatedDevice(fail_in_setup=True), SimulatedDevice)
    # Optional, so an acquisition can run while it is lost.
    rig = Rig(camera=SimulatedDevice(), shutter=Dev(shutter, required=False))
    await rig.clock.advance(0)

    for name in ("nonexistent", "camera", "source"):  # unknown, or alive
        assert isinstance(await rig.call(Relaunch(name)), Rejected), name

    await rig.start()
    assert isinstance(await rig.call(Relaunch("shutter")), Rejected), "not mid-acquisition"
    assert len(shutter.made) == 1


# --- CR2: a relaunch that cannot launch ----------------------------------------


def unbuildable():
    raise RuntimeError("camera driver not found")


async def test_cr2_a_relaunch_that_cannot_launch_is_refused_and_the_coordinator_survives():
    """CR2. Found by the core review (2026-10-02): the launch ran inside the
    Coordinator's handler with nothing to catch it, so a factory that raised
    -- or a process launch raising ``LaunchFailed``, as a camera whose driver
    is still wedged would -- killed the Coordinator. The asker heard nothing,
    and the source and every device ran on unsupervised (a parent's exit did
    not then stop its children; since CM10 it would stop them all). Relaunching a device that has just
    failed is exactly when its launch is likeliest to fail again."""
    camera = Factory(lambda: SimulatedDevice(fail_in_setup=True), unbuildable, SimulatedDevice)
    rig = Rig(camera=camera)
    await rig.clock.advance(0)
    assert "camera" not in rig.coordinator.participants

    reply = await rig.call(Relaunch("camera"))

    assert isinstance(reply, Rejected)
    assert "camera" in reply.reason and "camera driver not found" in reply.reason
    assert not rig.coordinator.stopped.is_set(), "the Coordinator survives"
    assert "camera" not in rig.coordinator.participants, "and the device is still lost"
    assert await rig.call(Relaunch("camera")) == Accepted(), "so it can be tried again"
    assert isinstance(await rig.start(), AcquisitionStarted)


async def test_cr2_a_source_relaunch_that_cannot_launch_leaves_it_lost():
    source = Factory(lambda: SimulatedTimingSource(cycle_period=PERIOD, fail_on_cycle=1), unbuildable)
    rig = Rig(source=source, camera=SimulatedDevice())
    await rig.start()
    await rig.clock.advance(2.0)
    assert rig.coordinator.source is None

    reply = await rig.call(Relaunch("source"))

    assert isinstance(reply, Rejected) and "camera driver not found" in reply.reason
    assert not rig.coordinator.stopped.is_set()
    assert rig.coordinator.state is CoordinatorState.IDLE
    assert rig.coordinator.source is None
