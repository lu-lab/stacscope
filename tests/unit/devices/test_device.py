"""D1–D3 — the device side of the protocol.

Milestone 1 built the Coordinator's half; this is the participant's. The base
class owns the states and the protocol replies, so an adapter supplies only
``arm`` and ``disarm`` — what preparing and releasing *this* hardware means.
"""

from stacscope.adapters.simulated import VirtualClock
from stacscope.core.actors import Actor, ChildExited, Fault, Rejected, Subscribe, ask, spawn
from stacscope.core.devices import DeviceState, NotReady, TimedDevice
from stacscope.core.protocol import DeviceReady, DeviceStopped, StartDevice, StopDevice


class Boom(RuntimeError):
    """An unexpected driver failure, as opposed to a refusal to arm."""


class Poison:
    """Handled by raising, to end the actor the way a driver error would."""


class FakeDevice(TimedDevice):
    """Records what the base class asked of it, in order."""

    def __init__(self, *, refuse=None, fail=None, disarm_fails=False):
        super().__init__()
        self.calls = []
        self.refuse = refuse
        self.fail = fail
        self.disarm_fails = disarm_fails

    async def arm(self, root_filename, cycles):
        self.calls.append(("arm", root_filename, cycles))
        if self.refuse is not None:
            raise NotReady(self.refuse)
        if self.fail is not None:
            raise Boom(self.fail)

    async def disarm(self):
        self.calls.append("disarm")
        if self.disarm_fails:
            raise Boom("the shutter jammed")

    async def close(self):
        self.calls.append("close")

    async def handle(self, msg):
        if isinstance(msg, Poison):
            raise Boom("driver died")
        await super().handle(msg)


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)

    def faults(self):
        return [m for m in self.heard if isinstance(m, Fault)]


class Rig:
    """One device, a subscriber, and a parent to hear it die."""

    def __init__(self, **kwargs):
        self.clock = VirtualClock()
        self.device = FakeDevice(**kwargs)
        self.parent = Listener()
        spawn(self.parent, clock=self.clock, name="parent")
        self.ref = self.parent.spawn_child(self.device, name="device")
        self.listener = Listener()
        self.ref.tell(Subscribe(spawn(self.listener, clock=self.clock, name="listener")))

    async def ask(self, msg):
        return await ask(self.ref, msg, timeout=1.0, clock=self.clock)

    async def start(self, root_filename="run-01", cycles=3):
        return await self.ask(StartDevice(root_filename, cycles))

    async def died(self):
        await self.device.stopped.wait()
        await self.clock.advance(0)  # let the parent hear it
        return [m for m in self.parent.heard if isinstance(m, ChildExited)]


# --- D1: the protocol ---------------------------------------------------------


async def test_start_arms_the_device_and_answers_ready():
    rig = Rig()

    assert await rig.start(root_filename="run-01", cycles=5) == DeviceReady()
    assert rig.device.calls == [("arm", "run-01", 5)]
    assert rig.device.state is DeviceState.ARMED


async def test_a_second_start_while_armed_is_rejected_and_does_not_rearm():
    rig = Rig()
    await rig.start()

    reply = await rig.start()
    assert isinstance(reply, Rejected)
    assert "ARMED" in reply.reason
    assert rig.device.calls.count(("arm", "run-01", 3)) == 1


async def test_stop_is_safe_in_every_state_and_always_confirms():
    rig = Rig()

    assert await rig.ask(StopDevice()) == DeviceStopped(), "idle: nothing to undo"
    assert "disarm" not in rig.device.calls

    await rig.start()
    assert await rig.ask(StopDevice()) == DeviceStopped()
    assert rig.device.calls[-1] == "disarm"
    assert rig.device.state is DeviceState.IDLE

    assert await rig.ask(StopDevice()) == DeviceStopped(), "stopping twice is not an error"
    assert rig.device.calls.count("disarm") == 1


async def test_a_device_that_was_stopped_can_be_started_again():
    rig = Rig()
    await rig.start()
    await rig.ask(StopDevice())

    assert await rig.start(root_filename="run-02") == DeviceReady()
    assert rig.device.calls == [
        ("arm", "run-01", 3), "disarm", ("arm", "run-02", 3),
    ]  # fmt: skip


# --- D2: a refusal to arm, and a failure to arm -------------------------------


async def test_a_refused_arm_is_rejected_and_the_device_stays_usable():
    rig = Rig(refuse="no MAIN waveform set")

    reply = await rig.start()
    assert reply == Rejected("no MAIN waveform set")
    assert rig.device.state is DeviceState.IDLE
    assert rig.listener.faults() == [], "a refusal is an answer, not a fault"

    rig.device.refuse = None
    assert await rig.start() == DeviceReady(), "the refusal left it armable"


async def test_an_unexpected_arm_failure_is_answered_and_published_as_fatal():
    """A driver error while arming is the hardware fault case: the acquisition
    must not proceed, but the device is not dead and needs no relaunch."""
    rig = Rig(fail="DCAMERR_BUSY")

    reply = await rig.start()
    assert isinstance(reply, Rejected)
    assert "DCAMERR_BUSY" in reply.reason
    assert rig.device.state is DeviceState.IDLE

    (fault,) = rig.listener.faults()
    assert fault.fatal, "the user decides what happens next"
    assert "DCAMERR_BUSY" in fault.reason
    assert not rig.device.stopped.is_set(), "a failed arm does not need a relaunch"


async def test_a_told_start_that_fails_is_published_rather_than_lost():
    rig = Rig(refuse="no MAIN waveform set")
    rig.ref.tell(StartDevice("run-01", 3))
    await rig.clock.advance(0)

    (fault,) = rig.listener.faults()
    assert "no MAIN waveform set" in fault.reason


# --- D3: release on death -----------------------------------------------------


async def test_a_device_that_dies_armed_is_still_released():
    """The actor dies mid-acquisition; the hardware must not be left running."""
    rig = Rig()
    await rig.start()

    rig.ref.tell(Poison())
    assert len(await rig.died()) == 1
    assert rig.device.calls[-2:] == ["disarm", "close"]


async def test_an_idle_device_is_closed_but_not_disarmed():
    rig = Rig()

    rig.ref.tell(Poison())
    await rig.died()
    assert rig.device.calls == ["close"]


async def test_close_runs_even_when_disarming_fails():
    rig = Rig(disarm_fails=True)
    await rig.start()

    rig.ref.tell(Poison())
    await rig.died()
    assert rig.device.calls[-1] == "close", "a jammed shutter still releases the driver"


async def test_a_device_that_cannot_reach_its_safe_state_dies_rather_than_confirming():
    """Unlike a failed arm, a failed disarm leaves the hardware in an unknown
    state: the honest answer is to die, so the supervisor sees it and the stop
    goes unconfirmed, rather than to claim ``DeviceStopped``."""
    rig = Rig(disarm_fails=True)
    await rig.start()

    rig.ref.tell(StopDevice())
    (exited,) = await rig.died()
    assert "jammed" in exited.error
    assert rig.device.calls == [
        ("arm", "run-01", 3), "disarm", "close",
    ], "teardown does not try the jammed shutter a second time"  # fmt: skip
