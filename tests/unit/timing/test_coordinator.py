"""C1–C5 — the Coordinator.

Run against ``SimulatedTimingSource``, whose phases are no-ops: nothing here
may depend on the source being an AO board.
"""

from timing_support import PERIOD, READY_TIMEOUT, Dev, Factory, Rig

from stacscope.adapters.simulated import SimulatedDevice, SimulatedTimingSource
from stacscope.core.actors import Accepted, Rejected
from stacscope.core.protocol import (
    AcquisitionStarted,
    CountPulses,
    PulseCounts,
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
    # Optional, so an acquisition can run while it is lost.
    rig = Rig(camera=SimulatedDevice(), shutter=Dev(shutter, required=False))
    await rig.clock.advance(0)

    for name in ("nonexistent", "camera", "source"):  # unknown, or alive
        assert isinstance(await rig.call(Relaunch(name)), Rejected), name

    await rig.start()
    assert isinstance(await rig.call(Relaunch("shutter")), Rejected), "not mid-acquisition"
    assert len(shutter.made) == 1


# --- C6: each device's frame count ------------------------------------------------


def _counting(**pulses_per_cycle):
    return SimulatedTimingSource(cycle_period=PERIOD, pulses_per_cycle=pulses_per_cycle)


async def test_c6_a_triggered_device_is_started_with_its_lines_pulse_count():
    """A recorder sizes its file from the acquisition's length (user,
    2026-09-25). Only the timing source knows its pulse trains, and config
    names the line that triggers each device, so the Coordinator asks the
    one and tells the other. A device with no line is told -1."""
    rig = Rig(
        source=_counting(camera_trigger=4, ueye_trigger=1),
        camera=Dev(SimulatedDevice(), trigger="camera_trigger"),
        shutter=SimulatedDevice(),
    )

    assert await rig.start(cycles=10) == AcquisitionStarted()

    assert rig.devices["camera"].started_with[0].frames == 40
    assert rig.devices["shutter"].started_with[0].frames == -1


async def test_c6_an_acquisition_until_stopped_has_no_count():
    rig = Rig(source=_counting(camera_trigger=4), camera=Dev(SimulatedDevice(), trigger="camera_trigger"))

    assert await rig.start(cycles=-1) == AcquisitionStarted()

    assert rig.devices["camera"].started_with[0].frames == -1


async def test_c6_a_source_that_cannot_count_leaves_the_count_unknown_and_starts():
    """A count is a convenience to a recorder, not a condition of running:
    a camera as the source, say, has none to give."""
    rig = Rig(camera=Dev(SimulatedDevice(), trigger="camera_trigger"))

    assert await rig.start(cycles=10) == AcquisitionStarted()

    assert rig.devices["camera"].started_with[0].frames == -1
    assert rig.listener.faults() == []


async def test_c6_a_line_the_source_does_not_have_is_unknown():
    rig = Rig(source=_counting(camera_trigger=4), camera=Dev(SimulatedDevice(), trigger="camera_trigger_2"))

    assert await rig.start(cycles=10) == AcquisitionStarted()

    assert rig.devices["camera"].started_with[0].frames == -1


def test_c6_a_pulse_count_crosses_the_codec():
    from stacscope.core.protocol import MESSAGES
    from stacscope.core.transport import Codec

    counts = PulseCounts(("camera_trigger", "ueye_trigger"), (40, -1))
    codec = Codec(MESSAGES)

    assert codec.decode(codec.encode(counts)) == counts


class _Silent(SimulatedTimingSource):
    """A source that never answers ``CountPulses``, but starts."""

    async def handle(self, msg):
        if isinstance(msg, CountPulses):
            return
        await super().handle(msg)


async def test_c6_a_source_that_never_answers_the_count_delays_the_start_by_its_timeout():
    rig = Rig(source=_Silent(cycle_period=PERIOD), camera=Dev(SimulatedDevice(), trigger="camera_trigger"))

    starting = rig.request(StartAcquisition(10, "run-01"))
    await rig.clock.advance(READY_TIMEOUT)

    assert await starting == AcquisitionStarted()
    assert rig.devices["camera"].started_with[0].frames == -1
