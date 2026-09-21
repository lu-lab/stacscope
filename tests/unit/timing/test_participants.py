"""D4–D6 — many participants, their own timeouts, and how each one fails.

Losing a device and a device reporting a fault are different failures, and
``required`` only decides the first. A device that dies is gone; a device that
reports a hardware fault is still there, and the fix is the user's.
"""

from timing_support import PERIOD, READY_TIMEOUT, Dev, Factory, Rig

from stacscope.adapters.simulated import SimulatedDevice, SimulatedTimingSource
from stacscope.core.actors import Accepted, Fault
from stacscope.core.protocol import (
    AcquisitionStarted,
    Phase,
    Relaunch,
    SourcePhase,
    StartAcquisition,
)
from stacscope.core.timing import CoordinatorState

SLOW_TIMEOUT = 6.0


class Die:
    """Told to a device to end it the way a driver error would."""


class Trip:
    """Told to a device to make it report a hardware fault it cannot fix."""


class Fragile(SimulatedDevice):
    async def handle(self, msg):
        match msg:
            case Die():
                raise RuntimeError("the camera fell off the rig")
            case Trip():
                self.publish(Fault(self.ref, "coolant loop lost", fatal=True))
            case _:
                await super().handle(msg)


class Grumbler(SimulatedDevice):
    async def handle(self, msg):
        match msg:
            case Trip():
                self.publish(Fault(self.ref, "filter wheel slow to seat", fatal=False))
            case _:
                await super().handle(msg)


def four_devices(**overrides):
    return {
        "camera": SimulatedDevice(ready_delay=1.5),
        "shutter": SimulatedDevice(ready_delay=0.5),
        "pressure": Dev(SimulatedDevice(ready_delay=4.0), ready_timeout=SLOW_TIMEOUT),
        "stage": SimulatedDevice(ready_delay=0.1),
        **overrides,
    }


# --- D4: every participant is ready, each on its own clock --------------------


async def test_d4_go_waits_for_the_slowest_of_many():
    rig = Rig(**four_devices())
    starting = rig.request(StartAcquisition(-1, "run-01"))

    await rig.clock.advance(3.9)
    assert not starting.done(), "three are ready; the pressure box is not"
    assert rig.source.phase is Phase.ARMED

    await rig.clock.advance(0.2)
    assert await starting == AcquisitionStarted()
    assert rig.source.phase is Phase.RUN
    assert rig.listener.faults() == []


async def test_d4_a_slow_device_is_judged_by_its_own_timeout_not_anothers():
    """The pressure box takes 4 s, well past the 2 s every other device is
    given. Only its own 6 s timeout may apply to it."""
    rig = Rig(**four_devices())
    starting = rig.request(StartAcquisition(-1, "run-01"))

    await rig.clock.advance(READY_TIMEOUT + 0.1)
    assert not starting.done(), "another device's timeout must not abort this one"

    await rig.clock.advance(2.0)
    assert await starting == AcquisitionStarted()


async def test_d4_a_device_past_its_own_timeout_aborts_at_that_timeout():
    rig = Rig(
        **four_devices(
            pressure=Dev(SimulatedDevice(never_ready=True), ready_timeout=SLOW_TIMEOUT)
        )
    )
    starting = rig.request(StartAcquisition(-1, "run-01"))

    await rig.clock.advance(READY_TIMEOUT + 0.1)
    assert not starting.done(), "aborting here would mean the default timeout was used"

    await rig.clock.advance(SLOW_TIMEOUT - rig.clock.now() - 0.1)
    assert not starting.done(), "not until its own timeout is actually reached"

    await rig.clock.advance(0.2)
    reply = await starting
    assert "pressure" in reply.reason
    assert "warmup" not in rig.source.calls
    assert rig.coordinator.state is CoordinatorState.IDLE


# --- D5: a participant is lost mid-run ----------------------------------------


async def test_d5_losing_a_required_participant_stops_the_acquisition():
    rig = Rig(camera=Fragile(), shutter=SimulatedDevice())
    await rig.start()
    await rig.clock.advance(2.0)

    rig.coordinator.participants["camera"].tell(Die())
    await rig.clock.advance(1.0)

    assert rig.coordinator.state is CoordinatorState.IDLE
    assert "camera" not in rig.coordinator.participants
    assert "stop" in rig.devices["shutter"].names(), "the others are stopped too"
    assert rig.source.phase is Phase.IDLE, "output has ceased"
    assert any("camera" in f.reason for f in rig.listener.faults())


async def test_d5_losing_an_optional_participant_lets_the_run_finish():
    """Some experiments are worth finishing without a non-essential device."""
    rig = Rig(camera=SimulatedDevice(), monitor=Dev(Fragile(), required=False))
    await rig.start()
    await rig.clock.advance(2.0)

    rig.coordinator.participants["monitor"].tell(Die())
    await rig.clock.advance(2.0)

    assert rig.coordinator.state is CoordinatorState.RUNNING
    assert "monitor" not in rig.coordinator.participants
    assert "stop" not in rig.devices["camera"].names()
    assert rig.source.phase is Phase.RUN, "output continues"
    assert any("monitor" in f.reason and not f.fatal for f in rig.listener.faults())


async def test_d5_a_required_device_is_the_default():
    rig = Rig(camera=Fragile())
    assert rig.coordinator._specs["camera"].required


# --- D6: a participant reports a hardware fault it cannot fix -----------------


async def test_d6_a_fatal_device_fault_stops_the_acquisition_and_leaves_it_idle():
    rig = Rig(camera=Fragile(), shutter=SimulatedDevice())
    await rig.start()
    await rig.clock.advance(2.0)

    rig.coordinator.participants["camera"].tell(Trip())
    await rig.clock.advance(1.0)

    assert rig.coordinator.state is CoordinatorState.IDLE
    assert rig.source.phase is Phase.IDLE
    assert "stop" in rig.devices["camera"].names(), "the faulted device is stopped too"
    assert "stop" in rig.devices["shutter"].names()
    assert "camera" in rig.coordinator.participants, "a faulted device is not a dead one"
    assert any("coolant" in f.reason for f in rig.listener.faults()), "the client hears why"


async def test_d6_nothing_is_latched_so_the_next_acquisition_is_accepted():
    """We trust that if the user has not fixed the external cause, the system
    will simply fault again — better than holding a lock they must clear."""
    rig = Rig(camera=Fragile())
    await rig.start()
    await rig.clock.advance(2.0)
    rig.coordinator.participants["camera"].tell(Trip())
    await rig.clock.advance(1.0)

    assert await rig.start() == AcquisitionStarted()
    assert rig.coordinator.state is CoordinatorState.RUNNING


async def test_d6_a_non_fatal_device_fault_is_reported_but_does_not_stop_anything():
    rig = Rig(camera=Grumbler())
    await rig.start()
    await rig.clock.advance(2.0)

    rig.coordinator.participants["camera"].tell(Trip())
    await rig.clock.advance(1.0)

    assert rig.coordinator.state is CoordinatorState.RUNNING
    assert rig.source.phase is Phase.RUN
    assert any("filter wheel" in f.reason for f in rig.listener.faults())


async def test_d6_a_fatal_fault_while_idle_stops_nothing_and_is_still_reported():
    rig = Rig(camera=Fragile())
    await rig.clock.advance(0)

    rig.coordinator.participants["camera"].tell(Trip())
    await rig.clock.advance(1.0)

    assert rig.coordinator.state is CoordinatorState.IDLE
    assert any("coolant" in f.reason for f in rig.listener.faults())
    assert "stop" not in rig.devices["camera"].names(), "nothing was running to stop"


async def test_d6_a_fatal_source_fault_stops_and_idles_like_any_other():
    """A live source that says it cannot continue is not a lost source: there
    is nothing to relaunch, so it stops and idles rather than latching."""
    rig = Rig(source=SimulatedTimingSource(cycle_period=PERIOD), camera=SimulatedDevice())
    await rig.start()
    await rig.clock.advance(2.0)

    rig.source.publish(Fault(rig.source.ref, "AO board overheated", fatal=True))
    await rig.clock.advance(1.0)

    assert rig.coordinator.state is CoordinatorState.IDLE
    assert "stop" in rig.devices["camera"].names()


# --- D8: the Coordinator relays the source's phases ---------------------------


class PhaseWatcher(Fragile):
    """A participant that hears what the source is doing, and can be killed."""

    def __init__(self):
        super().__init__()
        self.phases = []

    async def handle(self, msg):
        match msg:
            case SourcePhase(phase):
                self.phases.append(phase)
            case _:
                await super().handle(msg)


async def test_d8_participants_hear_the_sources_phases():
    """A software-timed device synchronizes to the acquisition through these,
    so they must reach devices, not only clients."""
    rig = Rig(pressure=PhaseWatcher())
    await rig.start(cycles=2)
    await rig.clock.advance(5.0)

    assert rig.devices["pressure"].phases == [
        Phase.ARMED, Phase.WARMUP, Phase.RUN, Phase.COOLDOWN, Phase.IDLE,
    ]  # fmt: skip


async def test_d8_clients_hear_them_too():
    rig = Rig(camera=SimulatedDevice())
    await rig.start(cycles=1)
    await rig.clock.advance(5.0)

    heard = [m.phase for m in rig.listener.heard if isinstance(m, SourcePhase)]
    assert Phase.RUN in heard and Phase.COOLDOWN in heard


async def test_d8_a_relaunched_participant_hears_them_as_well():
    """A device respawned by Relaunch is a new actor: it has to be resubscribed
    or it would sit through the next acquisition hearing nothing."""
    rig = Rig(pressure=Dev(Factory(PhaseWatcher), required=False))
    await rig.clock.advance(0)
    rig.coordinator.participants["pressure"].tell(Die())
    await rig.clock.advance(1.0)

    assert await rig.call(Relaunch("pressure")) == Accepted()
    await rig.start(cycles=1)
    await rig.clock.advance(5.0)

    assert Phase.RUN in rig.coordinator._specs["pressure"].factory.made[-1].phases
