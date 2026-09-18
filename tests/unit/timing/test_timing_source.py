"""T1 — the timing-source phase progression is enforced and published;
out-of-order commands are rejected.

Every source must go IDLE → ARMED → WARMUP → RUN → COOLDOWN → IDLE. What each
phase *does* belongs to the implementation; here the phases are the no-op
hooks of ``SimulatedTimingSource``, cycling on a virtual clock.
"""

import asyncio

from stacscope.adapters.simulated import SimulatedTimingSource, VirtualClock
from stacscope.core.actors import Actor, Fault, Priority, Rejected, Subscribe, ask, spawn
from stacscope.core.protocol import (
    DeviceReady,
    DeviceStopped,
    Finish,
    Go,
    Phase,
    SourceFinished,
    SourcePhase,
    StartDevice,
    StopDevice,
)

PERIOD = 1.0  # virtual seconds per cycle


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)

    def phases(self):
        return [m.phase for m in self.heard if isinstance(m, SourcePhase)]


class Rig:
    def __init__(self, drain_time=0.0):
        self.clock = VirtualClock()
        self.source = SimulatedTimingSource(cycle_period=PERIOD, drain_time=drain_time)
        self.ref = spawn(self.source, clock=self.clock, name="source")
        self.listener = Listener()
        self.ref.tell(Subscribe(spawn(self.listener, name="listener")))

    async def ask(self, msg):
        return await ask(self.ref, msg, timeout=1.0, clock=self.clock)

    async def arm_and_go(self, cycles):
        assert await self.ask(StartDevice("run-01", cycles)) == DeviceReady()
        self.ref.tell(Go())
        await self.clock.advance(0)


async def test_full_progression_publishes_every_phase_in_order():
    rig = Rig()
    await rig.arm_and_go(cycles=3)
    await rig.clock.advance(10 * PERIOD)

    assert rig.listener.phases() == [
        Phase.ARMED, Phase.WARMUP, Phase.RUN, Phase.COOLDOWN, Phase.IDLE,
    ]  # fmt: skip
    assert rig.listener.heard[-1] == SourceFinished()
    assert rig.source.calls == [
        ("arm", "run-01", 3), "warmup", "cycle", "cycle", "cycle", "cooldown", "drain",
    ]  # fmt: skip


async def test_cycles_complete_on_the_clock():
    rig = Rig()
    await rig.arm_and_go(cycles=3)

    await rig.clock.advance(2.5 * PERIOD)
    assert rig.source.calls.count("cycle") == 2
    assert rig.source.phase is Phase.RUN


async def test_zero_cycles_goes_from_warmup_straight_to_cooldown():
    rig = Rig()
    await rig.arm_and_go(cycles=0)

    assert "cycle" not in rig.source.calls
    assert rig.source.phase is Phase.IDLE


async def test_negative_cycles_run_until_finish_which_never_tears_a_cycle():
    rig = Rig()
    await rig.arm_and_go(cycles=-1)
    await rig.clock.advance(10.5 * PERIOD)
    assert rig.source.calls.count("cycle") == 10
    assert rig.source.phase is Phase.RUN

    rig.ref.tell(Finish(), Priority.URGENT)
    await rig.clock.advance(PERIOD)  # the in-flight cycle completes first

    assert rig.source.calls[-3:] == ["cycle", "cooldown", "drain"]
    assert rig.source.cycles_started == rig.source.cycles_completed == 11
    assert rig.listener.heard[-1] == SourceFinished()


async def test_out_of_order_commands_are_rejected_and_change_nothing():
    rig = Rig()
    assert isinstance(await rig.ask(Go()), Rejected)
    assert isinstance(await rig.ask(Finish()), Rejected)
    assert rig.source.phase is Phase.IDLE

    await rig.ask(StartDevice("run-01", 1))
    assert isinstance(await rig.ask(StartDevice("run-02", 1)), Rejected)
    assert isinstance(await rig.ask(Finish()), Rejected)
    assert rig.source.phase is Phase.ARMED
    assert rig.source.calls == [("arm", "run-01", 1)]


async def test_a_rejected_command_that_was_told_is_published_as_a_fault():
    rig = Rig()
    rig.ref.tell(Go())
    await rig.clock.advance(0)

    [fault] = [m for m in rig.listener.heard if isinstance(m, Fault)]
    assert fault.source == rig.ref
    assert not fault.fatal
    assert "Go" in fault.reason


async def test_stop_device_when_idle_is_harmless():
    rig = Rig()

    assert await rig.ask(StopDevice()) == DeviceStopped()
    assert rig.source.calls == []


async def test_stop_device_when_armed_disarms():
    rig = Rig()
    await rig.ask(StartDevice("run-01", 5))

    assert await rig.ask(StopDevice()) == DeviceStopped()
    assert rig.source.calls == [("arm", "run-01", 5), "disarm"]
    assert rig.listener.phases() == [Phase.ARMED, Phase.IDLE]


async def test_stop_device_while_running_replies_only_once_output_has_ceased():
    rig = Rig(drain_time=2 * PERIOD)
    await rig.arm_and_go(cycles=-1)
    await rig.clock.advance(2.5 * PERIOD)

    stopping = asyncio.create_task(
        ask(rig.ref, StopDevice(), timeout=100.0, clock=rig.clock, priority=Priority.URGENT)
    )
    await rig.clock.advance(PERIOD)  # in-flight cycle completes; draining begins
    assert rig.source.phase is Phase.COOLDOWN
    assert not stopping.done(), "must not reply while committed output still plays"

    await rig.clock.advance(2 * PERIOD)

    assert await stopping == DeviceStopped()
    assert rig.source.calls[-2:] == ["cooldown", "drain"]
    assert rig.source.phase is Phase.IDLE
