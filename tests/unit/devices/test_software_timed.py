"""D8–D9, and CR4 from the core review — a participant that acts during the acquisition on its own clock.

The pressure box is the case: serial commands, seconds at a time, against
sub-second volume rates. It can never be part of the acquisition loop, but it
is part of the imaging system, so it needs the acquisition's start and stop and
nothing finer.

It gets them from the timing source's published phases, relayed by the
Coordinator, which is control flow: good to milliseconds, never to be trusted
for timing. Which phase is the device's own choice — a shutter starts moving at
WARMUP, a driven experimental variable starts with the first image at RUN.
"""

from stacscope.adapters.simulated import VirtualClock
from stacscope.core.actors import Actor, Fault, Subscribe, ask, spawn
from stacscope.core.devices import SoftwareTimed, TimedDevice
from stacscope.core.protocol import (
    DeviceLaunched,
    DeviceReady,
    DeviceStopped,
    Phase,
    SourcePhase,
    StartDevice,
    StopDevice,
)

STEP = 10.0  # virtual seconds per routine step: far slower than any cycle


class Apparatus(TimedDevice):
    """The adapter's own half, with a safe state to return to."""

    def __init__(self):
        super().__init__()
        self.calls = []
        self.setting = None

    async def arm(self, root_filename, cycles):
        self.calls.append("arm")

    async def disarm(self):
        self.setting = 0.0  # vented
        self.calls.append("safe")


class Driven(SoftwareTimed, Apparatus):
    """Drives an experimental variable in slow steps, on the system clock."""

    start_phase = Phase.RUN

    async def routine(self):
        for pressure in (5.0, 10.0, 15.0):
            await self.clock.sleep(STEP)
            self.setting = pressure
            self.calls.append(("set", pressure, self.clock.now()))
        self.calls.append("routine done")


class Shutter(Driven):
    start_phase = Phase.WARMUP


class Unwinding(Driven):
    """A routine that has its own cleanup to do when it is cut short."""

    async def routine(self):
        try:
            await super().routine()
        finally:
            self.calls.append("routine cleanup")


class Failing(Driven):
    """A routine whose hardware goes away partway, as a serial port can."""

    async def routine(self):
        await self.clock.sleep(STEP)
        self.calls.append(("set", 5.0, self.clock.now()))
        raise OSError("serial port went away")


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)
        await super().handle(msg)  # as the rig's parent, a Fault goes on to the listener


class Rig:
    def __init__(self, cls=Driven):
        self.clock = VirtualClock()
        self.parent = Listener()
        spawn(self.parent, clock=self.clock, name="coordinator")
        self.device = cls()
        self.ref = self.parent.spawn_child(self.device, name="pressure")
        self.listener = Listener()
        # A child reports to its parent, and the root publishes: listen there.
        self.parent.ref.tell(Subscribe(spawn(self.listener, clock=self.clock, name="listener")))

    async def ask(self, msg):
        return await ask(self.ref, msg, timeout=1.0, clock=self.clock)

    async def arm(self):
        assert await self.ask(StartDevice("run-01", -1)) == DeviceReady()

    async def phase(self, phase):
        self.ref.tell(SourcePhase(phase))
        await self.clock.advance(0)

    def settings(self):
        return [c for c in self.device.calls if isinstance(c, tuple)]


# --- D8: the phase a device chooses to start on -------------------------------


async def test_the_routine_starts_on_the_phase_the_device_chose():
    rig = Rig()
    await rig.arm()

    await rig.phase(Phase.WARMUP)
    await rig.clock.advance(3 * STEP)
    assert rig.settings() == [], "a driven variable waits for the first image"

    await rig.phase(Phase.RUN)
    await rig.clock.advance(STEP)
    assert rig.settings() == [("set", 5.0, 40.0)]


async def test_a_shutter_starts_at_warmup_instead():
    rig = Rig(cls=Shutter)
    await rig.arm()

    await rig.phase(Phase.WARMUP)
    await rig.clock.advance(STEP)

    assert rig.settings() == [("set", 5.0, 10.0)], "moving before the first image"


async def test_a_phase_before_the_device_is_armed_starts_nothing():
    rig = Rig()

    await rig.phase(Phase.RUN)
    await rig.clock.advance(3 * STEP)

    assert rig.settings() == []


async def test_the_routine_is_started_once_however_many_phases_arrive():
    rig = Rig()
    await rig.arm()

    await rig.phase(Phase.RUN)
    await rig.phase(Phase.RUN)
    await rig.clock.advance(STEP)

    assert rig.settings() == [("set", 5.0, 10.0)], "not two routines at once"


async def test_a_message_the_device_does_not_care_about_is_ignored():
    """A device subscribed to its parent hears the phases, and only the one it
    chose starts anything. Anything else it has no use for is ignored: no
    answer, and nothing reported. (A ``Fault`` is the exception every actor
    shares: it goes up -- CM4.)"""
    rig = Rig()
    await rig.arm()

    rig.ref.tell(DeviceLaunched("camera", rig.ref))
    rig.ref.tell(SourcePhase(Phase.COOLDOWN))
    await rig.clock.advance(0)

    assert rig.listener.heard == [], "a broadcast is not a command"
    assert await rig.ask(StopDevice()) == DeviceStopped(), "and the device still works"


# --- D9: the routine runs, and stops safely -----------------------------------


async def test_the_routine_runs_on_the_clock_through_the_acquisition():
    rig = Rig()
    await rig.arm()
    await rig.phase(Phase.RUN)

    await rig.clock.advance(3 * STEP)

    assert rig.settings() == [
        ("set", 5.0, 10.0), ("set", 10.0, 20.0), ("set", 15.0, 30.0),
    ]  # fmt: skip
    assert "routine done" in rig.device.calls


async def test_a_stop_mid_routine_reaches_the_safe_state_before_confirming():
    rig = Rig()
    await rig.arm()
    await rig.phase(Phase.RUN)
    await rig.clock.advance(1.5 * STEP)

    assert rig.device.setting == 5.0, "mid-sequence"
    assert await rig.ask(StopDevice()) == DeviceStopped()

    assert rig.device.setting == 0.0, "vented"
    assert rig.device.calls[-1] == "safe", "the last thing it did was become safe"

    await rig.clock.advance(5 * STEP)
    assert rig.settings() == [("set", 5.0, 10.0)], "the routine does not go on alone"


async def test_a_routine_that_finishes_leaves_the_device_stoppable():
    rig = Rig()
    await rig.arm()
    await rig.phase(Phase.RUN)
    await rig.clock.advance(4 * STEP)

    assert await rig.ask(StopDevice()) == DeviceStopped()
    assert rig.device.setting == 0.0


async def test_a_device_that_dies_mid_routine_still_becomes_safe():
    rig = Rig()
    await rig.arm()
    await rig.phase(Phase.RUN)
    await rig.clock.advance(1.5 * STEP)

    rig.device.stop()
    rig.ref.tell(SourcePhase(Phase.COOLDOWN))  # any message ends the stopped loop
    await rig.device.stopped.wait()

    assert rig.device.setting == 0.0, "vented on the way out"


async def test_the_next_acquisition_runs_the_routine_again():
    rig = Rig()
    await rig.arm()
    await rig.phase(Phase.RUN)
    await rig.clock.advance(4 * STEP)
    await rig.ask(StopDevice())

    await rig.arm()
    await rig.phase(Phase.RUN)
    await rig.clock.advance(STEP)

    assert rig.settings()[-1][:2] == ("set", 5.0), "back at the start of the sequence"


async def test_a_cut_short_routine_unwinds_before_the_safe_state():
    """The adapter's safe state is the last word: a routine's own cleanup runs
    first, so the two cannot fight over the hardware."""
    rig = Rig(cls=Unwinding)
    await rig.arm()
    await rig.phase(Phase.RUN)
    await rig.clock.advance(1.5 * STEP)

    assert await rig.ask(StopDevice()) == DeviceStopped()
    assert rig.device.calls[-2:] == ["routine cleanup", "safe"]


# --- CR4: a routine that fails is reported -------------------------------------


def faults(rig):
    return [m for m in rig.listener.heard if isinstance(m, Fault)]


async def test_cr4_a_routine_that_fails_is_a_fatal_fault_when_it_happens():
    """CR4. Found by the core review (2026-10-02): the routine's exception sat
    in its task until disarm gathered it with ``return_exceptions=True`` and
    dropped it. A pressure box whose serial port died mid-run went unreported,
    and the acquisition ran on without the experiment it was meant to drive.
    Fatal, because the device can no longer do its part; published when it
    happens, so the Coordinator stops the acquisition instead of the user
    finding out from the data. The device stays armed until that stop, which
    still reaches the safe state."""
    rig = Rig(cls=Failing)
    await rig.arm()
    await rig.phase(Phase.RUN)

    await rig.clock.advance(STEP)
    await rig.clock.advance(0)

    [fault] = faults(rig)
    assert fault.fatal and "serial port went away" in fault.reason
    assert fault.source == rig.ref
    assert await rig.ask(StopDevice()) == DeviceStopped()
    assert rig.device.setting == 0.0, "vented"
    assert len(faults(rig)) == 1, "reported once, not again at the stop"


async def test_cr4_a_routine_that_ends_or_is_cut_short_is_not_a_fault():
    """The other half of CR4: a cancelled routine is how every stop mid-run
    ends, and a finished one is the normal case. Neither may read as a
    failure, or every acquisition would end in a fatal fault."""
    rig = Rig()
    await rig.arm()
    await rig.phase(Phase.RUN)
    await rig.clock.advance(1.5 * STEP)
    assert await rig.ask(StopDevice()) == DeviceStopped()

    await rig.arm()
    await rig.phase(Phase.RUN)
    await rig.clock.advance(4 * STEP)
    assert "routine done" in rig.device.calls
    assert await rig.ask(StopDevice()) == DeviceStopped()
    await rig.clock.advance(0)

    assert faults(rig) == []
