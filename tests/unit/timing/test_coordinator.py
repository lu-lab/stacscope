"""C1–C5 — the Coordinator.

Run against ``SimulatedTimingSource``, whose phases are no-ops: nothing here
may depend on the source being an AO board.
"""

from timing_support import PERIOD, READY_TIMEOUT, Factory, Rig

from stacscope.adapters.simulated import SimulatedDevice, SimulatedTimingSource
from stacscope.core.actors import Accepted, Rejected
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
    rig = Rig(camera=SimulatedDevice(), shutter=SimulatedDevice(fail_in_setup=True))
    await rig.clock.advance(0)

    assert set(rig.coordinator.participants) == {"camera"}
    [fault] = rig.listener.faults()
    assert not fault.fatal and "shutter" in fault.reason

    assert await rig.start() == AcquisitionStarted()
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
    assert await starting == AcquisitionStarted()
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

    assert await stopping == AcquisitionStopped()
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


# --- C5 ------------------------------------------------------------------------


async def test_c5_losing_the_source_faults_the_coordinator():
    rig = Rig(
        source=SimulatedTimingSource(cycle_period=PERIOD, fail_on_cycle=2),
        camera=SimulatedDevice(),
    )
    await rig.start(cycles=-1)

    await rig.clock.advance(3.0)

    assert rig.coordinator.state is CoordinatorState.FAULTED
    assert "stop" in rig.devices["camera"].names()
    assert any(f.fatal for f in rig.listener.faults())

    retry = rig.request(StartAcquisition(-1, "run-02"))
    await rig.clock.advance(0)
    assert isinstance(await retry, Rejected)


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


async def test_faulted_is_reported_with_its_cause_and_nothing_relaunches_on_its_own():
    source = failing_then_healthy_source()
    rig = Rig(source=source, camera=SimulatedDevice())
    await rig.start()
    await rig.clock.advance(100.0)

    assert rig.coordinator.state is CoordinatorState.FAULTED
    assert len(source.made) == 1
    [fatal] = [f for f in rig.listener.faults() if f.fatal]
    assert "simulated source failure on cycle 2" in fatal.reason


async def test_relaunching_the_source_recovers_from_faulted():
    source = failing_then_healthy_source()
    camera = SimulatedDevice()
    rig = Rig(source=source, camera=camera)
    await rig.start()
    await rig.clock.advance(3.0)
    assert rig.coordinator.state is CoordinatorState.FAULTED

    assert await rig.call(Relaunch("source")) == Accepted()
    assert rig.coordinator.state is CoordinatorState.IDLE
    assert DeviceLaunched("source", rig.coordinator.source) in rig.listener.heard

    assert await rig.start(cycles=2) == AcquisitionStarted()
    await rig.clock.advance(5.0)

    fresh = source.made[-1]
    assert fresh is not source.made[0], "a relaunch uses a new instance"
    assert fresh.calls.count("cycle") == 2
    assert rig.coordinator.state is CoordinatorState.IDLE, "heard the new source finish"
    assert camera.names() == ["start", "stop", "start", "stop"]


async def test_a_relaunch_that_fails_returns_to_faulted_and_says_why():
    source = Factory(
        lambda: SimulatedTimingSource(cycle_period=PERIOD, fail_on_cycle=1),
        BrokenSource,
    )
    rig = Rig(source=source, camera=SimulatedDevice())
    await rig.start()
    await rig.clock.advance(2.0)

    assert await rig.call(Relaunch("source")) == Accepted()
    await rig.clock.advance(0)

    assert rig.coordinator.state is CoordinatorState.FAULTED
    assert any(f.fatal and "hardware still wedged" in f.reason for f in rig.listener.faults())


async def test_relaunching_a_lost_participant_brings_it_back_into_acquisitions():
    shutter = Factory(lambda: SimulatedDevice(fail_in_setup=True), SimulatedDevice)
    rig = Rig(camera=SimulatedDevice(), shutter=shutter)
    await rig.clock.advance(0)
    assert "shutter" not in rig.coordinator.participants

    assert await rig.call(Relaunch("shutter")) == Accepted()
    assert DeviceLaunched("shutter", rig.coordinator.participants["shutter"]) in rig.listener.heard

    assert await rig.start() == AcquisitionStarted()
    assert shutter.made[-1].names() == ["start"]


async def test_relaunch_is_refused_unless_the_device_is_gone_and_nothing_is_running():
    shutter = Factory(lambda: SimulatedDevice(fail_in_setup=True), SimulatedDevice)
    rig = Rig(camera=SimulatedDevice(), shutter=shutter)
    await rig.clock.advance(0)

    for name in ("nonexistent", "camera", "source"):  # unknown, or alive
        assert isinstance(await rig.call(Relaunch(name)), Rejected), name

    await rig.start()
    assert isinstance(await rig.call(Relaunch("shutter")), Rejected), "not mid-acquisition"
    assert len(shutter.made) == 1
