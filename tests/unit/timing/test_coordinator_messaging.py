"""CM15–CM31 -- the Coordinator under the parent/child rule.

Its devices are its children: it commands them, they answer and report to
it by its address, and it waits on them only as a state, never inside a
handler. So while it starts or stops an acquisition it still hears
everything else -- a query, a Fault, a death, a phase to relay. Clients
outside the tree -- the rig, the gateway, these tests -- subscribe to it and
hear everything; a device subscribed to it hears the timing phases and
nothing else, so no device ever learns of a sibling.

Run against ``SimulatedTimingSource``, whose phases are no-ops.
"""

import dataclasses

from timing_support import PERIOD, READY_TIMEOUT, Dev, Factory, Rig

from stacscope.adapters.simulated import Fail, FaultyDevice, SimulatedDevice, SimulatedTimingSource, VirtualClock
from stacscope.core.actors import Accepted, Actor, ActorRef, Fault, Rejected, Subscribe, spawn
from stacscope.core.devices import SoftwareTimed, TimedDevice
from stacscope.core.protocol import (
    MESSAGES,
    AcquisitionStarted,
    AcquisitionStopped,
    DeviceLaunched,
    DeviceReady,
    DeviceStatus,
    DeviceStopped,
    GetStatus,
    Go,
    Phase,
    Relaunch,
    SourceFinished,
    SourcePhase,
    StartAcquisition,
    StartDevice,
    Status,
    StopAcquisition,
    StopDevice,
)
from stacscope.core.timing import CoordinatorState, Launcher
from stacscope.core.transport import Codec


class Spy(SimulatedDevice):
    """A participant that keeps every envelope delivered to it, before its
    runtime sorts them -- a subscription included, which never reaches
    ``handle``."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.received = []

    def _receive(self, envelope, priority):
        self.received.append(envelope)
        super()._receive(envelope, priority)


class PhaseSpy(SoftwareTimed, TimedDevice):
    """A software-timed device, keeping every envelope delivered to it."""

    def __init__(self):
        super().__init__()
        self.received = []

    def _receive(self, envelope, priority):
        self.received.append(envelope)
        super()._receive(envelope, priority)

    def phases(self):
        return [e.msg.phase for e in self.received if isinstance(e.msg, SourcePhase)]


class Die:
    """Told to a device to end it the way a driver error would."""


class Mortal(SimulatedDevice):
    """Dies when told ``Die``; may also die partway through a start or a stop,
    or take its time stopping."""

    def __init__(self, *, die_on_start=None, die_on_stop=None, stop_time=0.0, **kwargs):
        super().__init__(**kwargs)
        self.die_on_start = die_on_start
        self.die_on_stop = die_on_stop
        self.stop_time = stop_time

    async def handle(self, msg):
        match msg:
            case Die():
                raise RuntimeError("the camera fell off the rig")
            case StartDevice() if self.die_on_start is not None:
                await self.clock.sleep(self.die_on_start)
                raise RuntimeError("the camera fell off the rig")
            case StopDevice() if self.die_on_stop is not None:
                await self.clock.sleep(self.die_on_stop)
                raise RuntimeError("the camera fell off the rig")
            case StopDevice() if self.stop_time:
                await self.clock.sleep(self.stop_time)
                await super().handle(msg)
            case _:
                await super().handle(msg)


class Refuser(SimulatedDevice):
    """Refuses to arm while ``refuse`` is set."""

    refuse = True

    async def handle(self, msg):
        if isinstance(msg, StartDevice) and self.refuse:
            self.reply(Rejected("not today"))
            return
        await super().handle(msg)


class SourceDiesArming(SimulatedTimingSource):
    async def arm(self, root_filename, cycles):
        await self.clock.sleep(0.5)
        raise RuntimeError("the AO board went away")


class SlowLauncher(Launcher):
    """Launches the named devices only after ``delay`` on the clock -- a
    process child whose camera takes its time to open."""

    def __init__(self, clock, delay, *names):
        super().__init__()
        self.clock = clock
        self.delay = delay
        self.names = names

    async def launch(self, parent, spec):
        if spec.name in self.names and parent.started.is_set():
            await self.clock.sleep(self.delay)
        return await super().launch(parent, spec)


def statuses(rig):
    return [m for m in rig.listener.heard if isinstance(m, Status)]


def refs_in(obj):
    """Every ref carried anywhere in a message."""
    if isinstance(obj, ActorRef):
        yield obj
    elif dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        for field in dataclasses.fields(obj):
            yield from refs_in(getattr(obj, field.name))
    elif isinstance(obj, tuple):
        for item in obj:
            yield from refs_in(item)


def foreign_refs(device):
    """Refs a device received that are neither its own, its parent's, nor
    an ask's one-shot address on itself."""
    own, parent = device.ref.address, device.parent.address
    seen = set()
    for envelope in device.received:
        for ref in [*refs_in(envelope.msg), *([envelope.sender] if envelope.sender else [])]:
            if ref.address not in (own, parent) and not ref.address.startswith(own + "#"):
                seen.add(ref.address)
    return seen


# --- CM15: starting is a state -----------------------------------------------------


async def test_cm15_while_starting_the_coordinator_answers_and_passes_faults_on():
    """Before, the start was an ``await`` inside the handler: until the
    slowest device answered, nothing else got in -- not a status query, not
    a device's Fault, not a death."""
    rig = Rig(camera=SimulatedDevice(ready_delay=1.5))
    await rig.clock.advance(0)
    starting = rig.request(StartAcquisition(-1, "run-01"))
    await rig.clock.advance(0.5)

    status = await rig.call(GetStatus())
    rig.ref.tell(Fault(rig.coordinator.participants["camera"], "a warning", False))
    await rig.clock.advance(0)

    assert status.state is CoordinatorState.STARTING
    assert "a warning" in [f.reason for f in rig.listener.faults()]
    assert not starting.done()
    await rig.clock.advance(1.0)
    assert isinstance(await starting, AcquisitionStarted)


# --- CM16: a deadline is a message to itself ----------------------------------------


async def test_cm16_an_answer_after_the_deadline_is_ignored_and_the_device_stopped():
    late = SimulatedDevice(ready_delay=READY_TIMEOUT + 1.0)
    rig = Rig(camera=late)
    await rig.clock.advance(0)
    starting = rig.request(StartAcquisition(-1, "run-01"))

    await rig.clock.advance(READY_TIMEOUT)
    reply = await starting
    assert isinstance(reply, Rejected)
    assert f"camera (no reply within {READY_TIMEOUT} s)" in reply.reason

    await rig.clock.advance(2.0)  # its DeviceReady arrives, then its stop is answered
    assert late.names() == ["start", "stop"]
    assert rig.coordinator.state is CoordinatorState.IDLE
    assert "warmup" not in rig.source.calls, "no Go"


# --- CM17, CM18: a child lost while starting ----------------------------------------


async def test_cm17_a_required_device_lost_while_starting_fails_the_start_at_once():
    """A dead device will never answer, so waiting out its timeout only
    delays the news. The reason says which device, and why."""
    rig = Rig(camera=Mortal(die_on_start=0.5), shutter=SimulatedDevice(ready_delay=1.5))
    await rig.clock.advance(0)
    starting = rig.request(StartAcquisition(-1, "run-01"))

    await rig.clock.advance(0.5)
    assert starting.done(), "at once, not after the ready timeout"
    reply = await starting
    assert isinstance(reply, Rejected)
    assert "camera" in reply.reason and "fell off the rig" in reply.reason

    await rig.clock.advance(2.0)
    assert rig.devices["shutter"].names() == ["start", "stop"], "a laggard is stopped too"
    assert rig.coordinator.state is CoordinatorState.IDLE


async def test_cm17_the_source_lost_while_starting_fails_the_start_at_once():
    rig = Rig(source=SourceDiesArming(), camera=SimulatedDevice())
    await rig.clock.advance(0)
    starting = rig.request(StartAcquisition(-1, "run-01"))

    await rig.clock.advance(0.5)
    reply = await starting
    assert isinstance(reply, Rejected)
    assert "source" in reply.reason and "AO board went away" in reply.reason
    await rig.clock.advance(0)
    assert rig.devices["camera"].names() == ["start", "stop"]


async def test_cm18_an_optional_device_lost_while_starting_is_left_out():
    rig = Rig(camera=SimulatedDevice(), shutter=Dev(Mortal(die_on_start=0.5), required=False))
    await rig.clock.advance(0)
    starting = rig.request(StartAcquisition(-1, "run-01"))

    await rig.clock.advance(0.5)
    assert isinstance(await starting, AcquisitionStarted)
    assert "shutter" not in rig.coordinator.participants


# --- CM19, CM20: stopping is a state ---------------------------------------------


async def test_cm19_a_device_lost_while_stopping_counts_as_done_and_is_reported_lost():
    rig = Rig(camera=Mortal(die_on_stop=0.5))
    await rig.start()
    stopping = rig.request(StopAcquisition())

    await rig.clock.advance(PERIOD + 0.5)  # the cycle in hand, then the camera dies stopping
    assert stopping.done(), "not left waiting for its stop timeout"
    assert isinstance(await stopping, AcquisitionStopped)
    reasons = [f.reason for f in rig.listener.faults()]
    assert any("participant camera exited" in r for r in reasons)
    assert not any("not confirmed" in r for r in reasons)


async def test_cm20_a_phase_is_relayed_while_the_others_are_still_stopping():
    """An abort stops everyone at once. The source cools down and goes idle
    while a slow device is still stopping; a software-timed device must hear
    those phases then, not after the whole stop."""
    spy = PhaseSpy()
    rig = Rig(slow=Mortal(stop_time=5.0), pressure=spy, trip=FaultyDevice(Fail.NOTHING))
    await rig.start()
    await rig.clock.advance(0.5)

    rig.ref.tell(Fault(rig.coordinator.participants["trip"], "coolant loop lost", True))
    await rig.clock.advance(PERIOD + 0.1)  # the cycle in hand ends, then COOLDOWN, IDLE

    assert rig.coordinator.state is CoordinatorState.STOPPING, "the slow one has not answered"
    assert spy.phases()[-2:] == [Phase.COOLDOWN, Phase.IDLE]
    await rig.clock.advance(5.0)
    assert rig.coordinator.state is CoordinatorState.IDLE


# --- CM21: a relaunch is a state ------------------------------------------------------


async def test_cm21_while_a_device_is_relaunching_the_coordinator_still_answers():
    """A process child's launch waits for its setup -- seconds, for a camera.
    The Coordinator waits as a state: it answers meanwhile, refuses a second
    relaunch of the same device, and refuses to start without it."""
    camera = Factory(lambda: SimulatedDevice(fail_in_setup=True), SimulatedDevice)
    rig = Rig(camera=camera, launcher=None)
    rig.coordinator._launcher = SlowLauncher(rig.clock, 5.0, "camera")
    await rig.clock.advance(0)
    assert "camera" not in rig.coordinator.participants

    relaunching = rig.request(Relaunch("camera"))
    await rig.clock.advance(1.0)

    status = await rig.call(GetStatus())
    assert status.state is CoordinatorState.IDLE
    assert not relaunching.done()
    again = await rig.call(Relaunch("camera"))
    assert isinstance(again, Rejected) and "already" in again.reason
    refused = await rig.call(StartAcquisition(-1, "run-01"))
    assert isinstance(refused, Rejected) and "camera" in refused.reason

    await rig.clock.advance(4.0)
    assert await relaunching == Accepted()
    assert isinstance(await rig.start(), AcquisitionStarted)


# --- CM22: no FAULTED -----------------------------------------------------------------


async def test_cm22_a_lost_source_is_a_lost_required_device_and_nothing_is_latched():
    """FAULTED is folded into the rule for a lost required device: the
    source is one that is always required. Every start is refused, naming
    it, until it is relaunched."""
    source = Factory(
        lambda: SimulatedTimingSource(cycle_period=PERIOD, fail_on_cycle=2),
        lambda: SimulatedTimingSource(cycle_period=PERIOD),
    )
    rig = Rig(source=source, camera=SimulatedDevice())
    await rig.start()
    await rig.clock.advance(3.0)

    assert rig.coordinator.state is CoordinatorState.IDLE
    assert "stop" in rig.devices["camera"].names()
    refused = await rig.call(StartAcquisition(-1, "run-02"))
    assert isinstance(refused, Rejected)
    assert "source" in refused.reason and "Relaunch" in refused.reason

    assert await rig.call(Relaunch("source")) == Accepted()
    assert isinstance(await rig.start(), AcquisitionStarted)


# --- CM23: the Coordinator subscribes to nothing --------------------------------


async def test_cm23_the_coordinator_subscribes_to_no_device():
    """Devices report to their parent by its address, so there is nothing to
    subscribe to. A subscription would also put the Coordinator on a list a
    device publishes to, which the rule keeps for clients."""
    spy = Spy()
    rig = Rig(camera=spy)
    await rig.clock.advance(0)
    await rig.start()

    assert not [e for e in spy.received if isinstance(e.msg, Subscribe)]


async def test_cm23_a_devices_fault_reaches_the_coordinators_clients_once():
    """Reported up by address and published by the root, once. Had a device
    subscribed to the Coordinator heard its Faults, it would pass each back
    up, and the two would trade it forever."""
    rig = Rig(camera=FaultyDevice(Fail.REPORT, on="StartDevice"))
    await rig.clock.advance(0)

    assert isinstance(await rig.start(), AcquisitionStarted)
    await rig.clock.advance(1)

    [fault] = rig.listener.faults()
    assert fault == Fault(rig.coordinator.participants["camera"], "the driver reported a warning", False)


async def test_cm23_a_fatal_fault_reported_by_a_device_ends_the_acquisition():
    rig = Rig(camera=SimulatedDevice())
    await rig.start()

    rig.ref.tell(Fault(rig.coordinator.participants["camera"], "coolant loop lost", True))
    await rig.clock.advance(PERIOD + 0.1)

    assert rig.coordinator.state is CoordinatorState.IDLE
    assert rig.devices["camera"].names() == ["start", "stop"]


# --- CM24–CM26: who hears what --------------------------------------------------------


async def test_cm24_a_software_timed_device_subscribes_to_its_parent_for_the_phases():
    spy = PhaseSpy()
    plain = Spy()
    rig = Rig(pressure=spy, camera=plain)
    await rig.start(cycles=1)
    await rig.clock.advance(2 * PERIOD)

    assert spy.phases() == [Phase.ARMED, Phase.WARMUP, Phase.RUN, Phase.COOLDOWN, Phase.IDLE]
    assert not [e for e in plain.received if isinstance(e.msg, SourcePhase)], "it did not ask"


async def test_cm24_no_device_ever_receives_another_actors_ref():
    """Siblings never answer each other, so none needs the other's address.
    Through an acquisition, a fault, a death and a relaunch, every device
    hears only from its parent, about itself."""
    spy = PhaseSpy()
    plain = Spy()
    shutter = Factory(lambda: Mortal(), SimulatedDevice)
    rig = Rig(pressure=spy, camera=plain, shutter=Dev(shutter, required=False))
    await rig.start()
    rig.ref.tell(Fault(rig.coordinator.participants["camera"], "a warning", False))
    rig.coordinator.participants["shutter"].tell(Die())
    await rig.clock.advance(1.0)
    stopping = rig.request(StopAcquisition())
    await rig.clock.advance(2 * PERIOD)
    assert isinstance(await stopping, AcquisitionStopped)
    assert await rig.call(Relaunch("shutter")) == Accepted()
    await rig.start(cycles=1)
    await rig.clock.advance(3 * PERIOD)

    assert foreign_refs(spy) == set()
    assert foreign_refs(plain) == set()


async def test_cm25_a_client_hears_everything():
    shutter = Factory(lambda: Mortal(), SimulatedDevice)
    rig = Rig(camera=SimulatedDevice(), shutter=Dev(shutter, required=False))
    await rig.start(cycles=1)
    rig.coordinator.participants["shutter"].tell(Die())
    await rig.clock.advance(3 * PERIOD)
    await rig.call(Relaunch("shutter"))

    kinds = {type(m) for m in rig.listener.heard}
    assert {SourcePhase, Fault, Status, DeviceLaunched} <= kinds


async def test_cm26_a_device_that_exits_is_dropped_from_the_coordinators_subscribers():
    """Nothing more is sent to its address: in this process that would reach
    the dead instance, across a link its successor, which did not ask."""
    pressure = Factory(PhaseSpy, Spy)
    rig = Rig(pressure=Dev(pressure, required=False))
    await rig.clock.advance(0)
    dead = pressure.made[0]
    dead.stop()
    await rig.clock.advance(0)
    heard_alive = len(dead.received)
    assert await rig.call(Relaunch("pressure")) == Accepted()

    await rig.start(cycles=1)
    await rig.clock.advance(3 * PERIOD)

    assert len(dead.received) == heard_alive
    assert not [e for e in pressure.made[-1].received if isinstance(e.msg, SourcePhase)]


# --- CM27–CM29: status, and acquisitions by number ----------------------------------


async def test_cm27_get_status_says_the_state_and_every_device():
    rig = Rig(camera=SimulatedDevice(), shutter=Dev(SimulatedDevice(fail_in_setup=True), required=False))
    await rig.clock.advance(0)

    status = await rig.call(GetStatus())

    coordinator = rig.coordinator
    assert status == Status(
        CoordinatorState.IDLE,
        0,
        (
            DeviceStatus("source", source=True, required=True, present=True, ref=coordinator.source),
            DeviceStatus("camera", source=False, required=True, present=True, ref=coordinator.participants["camera"]),
            DeviceStatus("shutter", source=False, required=False, present=False, ref=None),
        ),
    )


async def test_cm28_status_is_published_on_every_change():
    shutter = Factory(lambda: Mortal(), SimulatedDevice)
    rig = Rig(camera=SimulatedDevice(), shutter=Dev(shutter, required=False))
    await rig.clock.advance(0)
    rig.listener.heard.clear()

    await rig.start(cycles=1)
    await rig.clock.advance(3 * PERIOD)
    assert [s.state for s in statuses(rig)] == [
        CoordinatorState.STARTING,
        CoordinatorState.RUNNING,
        CoordinatorState.STOPPING,
        CoordinatorState.IDLE,
    ]

    rig.listener.heard.clear()
    rig.coordinator.participants["shutter"].tell(Die())
    await rig.clock.advance(0)
    await rig.call(Relaunch("shutter"))
    present = [{d.name: d.present for d in s.devices}["shutter"] for s in statuses(rig)]
    assert present == [False, True]


async def test_cm29_acquisitions_are_numbered_from_the_moment_a_start_begins():
    """A failed start has a number too: its Fault and its stop belong to it."""
    refuser = Refuser()
    rig = Rig(camera=refuser)
    await rig.clock.advance(0)

    assert isinstance(await rig.start(), Rejected)
    assert (await rig.call(GetStatus())).acquisition == 1
    refuser.refuse = False
    await rig.clock.advance(0)

    assert await rig.start() == AcquisitionStarted(2)
    assert (await rig.call(GetStatus())).acquisition == 2
    stopping = rig.request(StopAcquisition())
    await rig.clock.advance(2 * PERIOD)
    assert await stopping == AcquisitionStopped(2)


# --- CM30: the source's own order ---------------------------------------------------


async def test_cm30_the_sources_idle_arrives_before_its_stop_and_finish():
    """Faults and phases carry no acquisition number: their order is what
    attributes them. The source says IDLE before it confirms the stop or
    says it finished, all on the one channel to its parent."""
    class Parent(Actor):
        def __init__(self):
            super().__init__()
            self.heard = []

        async def handle(self, msg):
            self.heard.append(msg)

    clock = VirtualClock()
    parent = Parent()
    spawn(parent, clock=clock, name="coordinator")
    source = SimulatedTimingSource(cycle_period=PERIOD)
    ref = parent.spawn_child(source, name="source")
    await clock.advance(0)
    ref.tell(StartDevice("", -1), sender=parent.ref)
    ref.tell(Go(), sender=parent.ref)
    await clock.advance(0.5)
    ref.tell(StopDevice(), sender=parent.ref)
    await clock.advance(2 * PERIOD)

    tail = [m for m in parent.heard if m in (SourcePhase(Phase.IDLE), DeviceStopped(), SourceFinished())]
    assert tail == [SourcePhase(Phase.IDLE), SourceFinished(), DeviceStopped()]
    assert DeviceReady() in parent.heard


# --- CM31: the new messages cross ----------------------------------------------------


def test_cm31_status_and_its_query_cross_the_codec():
    codec = Codec(MESSAGES)
    ref = ActorRef(None, "coordinator/camera")
    status = Status(
        CoordinatorState.RUNNING,
        7,
        (DeviceStatus("camera", source=False, required=True, present=True, ref=ref),),
    )
    resolve = lambda address: ActorRef(None, address)  # noqa: E731

    assert codec.decode(codec.encode(GetStatus())) == GetStatus()
    assert codec.decode(codec.encode(status), resolve=resolve) == status
    assert codec.decode(codec.encode(AcquisitionStarted(7))) == AcquisitionStarted(7)
