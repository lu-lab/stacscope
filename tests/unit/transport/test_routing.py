"""X2, X3 -- one end of a boundary: what is here, and how to reach the rest.

A ``Peer`` is the address book for one link. These tests use a loopback pair
-- two peers handing frames straight to each other -- so everything about
routing is proven in virtual time, deterministically, before a process or a
socket is involved. What is left for the real thing to add is only the bytes.
"""

import asyncio
import gc

import pytest

from stacscope.adapters.simulated import VirtualClock
from stacscope.core.actors import (
    REPLY_MARK,
    Actor,
    ActorRef,
    Heartbeat,
    HeartbeatReply,
    LocalTransport,
    Priority,
    Subscribe,
    ask,
    spawn,
)
from stacscope.core.protocol import (
    MESSAGES,
    DeviceLaunched,
    DeviceReady,
    DeviceStopped,
    Phase,
    SourcePhase,
    StartDevice,
    StopDevice,
)
from stacscope.core.transport import Codec, Connection, Peer, Undeliverable, Wire


class Loopback:
    """Two peers wired to each other, with no process in between."""

    def __init__(self) -> None:
        wire = Wire(Codec(MESSAGES))
        self.here = Peer(wire, send=lambda frame: self.there.receive(frame))
        self.there = Peer(wire, send=lambda frame: self.here.receive(frame))


class Recorder(Actor):
    """Keeps what reached ``handle``, in the order it got there."""

    def __init__(self) -> None:
        super().__init__()
        self.handled: list[object] = []

    async def handle(self, msg):
        self.handled.append(msg)
        if isinstance(msg, StartDevice):
            self.reply(DeviceReady())


class Busy(Actor):
    """Stuck in one long handler -- alive, just not draining its mailbox."""

    def __init__(self) -> None:
        super().__init__()
        self.handled: list[object] = []

    async def handle(self, msg):
        self.handled.append(msg)
        await asyncio.Event().wait()


@pytest.fixture
def clock():
    return VirtualClock()


@pytest.fixture
def link():
    return Loopback()


async def live(actor: Actor, peer: Peer, clock: VirtualClock, name: str) -> ActorRef:
    """Start ``actor`` and make it reachable from the far side as ``name``."""
    ref = spawn(actor, clock=clock, name=name)
    peer.register(ref)
    await clock.advance(0)
    return ref


# --- X2: priority is enforced at the actor, not on the wire ------------------


async def test_an_urgent_frame_is_handled_before_frames_that_arrived_first(link, clock):
    device = Recorder()
    await live(device, link.there, clock, "device")
    remote = link.here.ref_to("device")

    for _ in range(100):
        remote.tell(SourcePhase(Phase.RUN), Priority.LOW)
    remote.tell(StopDevice(), Priority.URGENT)
    await clock.advance(0)

    assert device.handled[0] == StopDevice()
    assert len(device.handled) == 101  # and nothing was dropped to get there


async def test_a_heartbeat_that_crosses_is_answered_while_a_handler_blocks(link, clock):
    """The point of heartbeats: they prove the far *runtime* is alive, so they
    are answered on receipt and never reach ``handle``. A wedged driver holding
    one handler open must still answer, or it would look dead when it is not."""
    device = Busy()
    await live(device, link.there, clock, "device")
    remote = link.here.ref_to("device")
    remote.tell(StartDevice("", 1))
    await clock.advance(0)

    replies: list[object] = []
    watch = ActorRef(LocalTransport(lambda envelope, _: replies.append(envelope.msg)), "watch#1")
    remote.tell(Heartbeat(), Priority.URGENT, sender=watch)
    await clock.advance(0)

    assert replies == [HeartbeatReply()]
    assert device.handled == [StartDevice("", 1)]  # the heartbeat never got in


# --- X3: refs are usable from the far side -----------------------------------


async def test_ask_is_answered_across_the_boundary(link, clock):
    device = Recorder()
    await live(device, link.there, clock, "device")

    reply = await ask(link.here.ref_to("device"), StartDevice("fish", 3), timeout=1, clock=clock)

    assert reply == DeviceReady()
    assert device.handled == [StartDevice("fish", 3)]


async def test_a_ref_inside_a_message_is_usable_from_the_far_side(link, clock):
    """``DeviceLaunched`` carries a ref. Whoever receives it must be able to
    address the device, whichever side of the boundary it turned out to be on."""
    device = Recorder()
    ref = await live(device, link.there, clock, "device")
    heard: list[object] = []
    watcher = ActorRef(LocalTransport(lambda envelope, _: heard.append(envelope.msg)), "watcher")
    link.here.register(watcher)

    link.there.ref_to("watcher").tell(DeviceLaunched("camera", ref))
    await clock.advance(0)
    [launched] = heard
    launched.device.tell(StopDevice())
    await clock.advance(0)

    assert launched.device.address == "device"
    assert device.handled == [StopDevice()]


async def test_dp17_a_ref_sent_inside_a_message_is_reachable_back_across_the_boundary(link, clock):
    """A subscriber is a ref handed over inside ``Subscribe``. The gateway
    subscribes to a camera in a process of its own with a ref that is no
    registered actor, and the camera must be able to publish to it. A peer
    that knew only senders refused every publication as undeliverable."""
    camera = Actor()
    await live(camera, link.there, clock, "camera")
    heard: list[object] = []
    subscriber = ActorRef(LocalTransport(lambda envelope, _: heard.append(envelope.msg)), "gateway/~camera")

    link.here.ref_to("camera").tell(Subscribe(subscriber))
    await clock.advance(0)
    camera.publish(SourcePhase(Phase.RUN))
    await clock.advance(0)

    assert heard == [SourcePhase(Phase.RUN)]


async def test_dp17_a_ref_sent_inside_a_message_is_held_only_while_its_owner_holds_it(link, clock):
    """Held weakly, as senders are: a long run hands over many refs, and the
    link must not keep each one alive for its own lifetime."""
    camera = Actor()
    await live(camera, link.there, clock, "camera")
    subscriber = ActorRef(LocalTransport(lambda envelope, _: None), "gateway/~camera")
    link.here.ref_to("camera").tell(Subscribe(subscriber))
    await clock.advance(0)
    camera.stop()
    await camera.stopped.wait()

    del subscriber
    gc.collect()

    with pytest.raises(Undeliverable):
        link.there.ref_to("gateway/~camera").tell(SourcePhase(Phase.RUN))


async def test_cm7_an_ordinary_tell_is_answered_across_the_boundary(link, clock):
    """Every envelope carries its sender, so an answer is a plain tell back,
    and a parent need not ``ask`` a child in another process to hear from it.
    The sender is an actor here, never registered with the peer: it has to be
    reachable from the far side all the same."""
    coordinator = Recorder()
    here = spawn(coordinator, clock=clock, name="coordinator")
    device = Recorder()
    await live(device, link.there, clock, "device")

    link.here.ref_to("device").tell(StartDevice("fish", 3), sender=here)
    await clock.advance(0)

    assert device.handled == [StartDevice("fish", 3)]
    assert coordinator.handled == [DeviceReady()]


async def test_a_frame_for_an_address_that_is_not_here_is_refused(link, clock):
    """Undeliverable, rather than handed to whoever happens to be listening.
    The loopback is synchronous, so the receiving peer's refusal surfaces at
    the sender; across a real link it surfaces in the read loop."""
    with pytest.raises(Undeliverable, match="ghost"):
        link.here.ref_to("ghost").tell(StopDevice())


async def test_a_one_shot_reply_address_is_reachable_and_then_released(link, clock):
    """Every ``ask`` mints an address the far side must be able to answer. They
    would accumulate forever in a long run, so the peer holds them weakly: alive
    while the asker is waiting, gone once it has stopped caring."""
    device = Recorder()
    await live(device, link.there, clock, "device")

    await ask(link.here.ref_to("device"), StartDevice("", 1), timeout=1, clock=clock)
    gc.collect()

    assert link.here.pending_replies() == 0


async def test_a_reply_nobody_is_waiting_for_is_dropped_rather_than_refused(link):
    """An ``ask`` that timed out leaves an address nobody is listening on. A
    late reply to it is not a routing failure -- it is exactly what ``ask``
    promises to discard -- and must not be reported as one during the slow
    device that caused it."""
    link.here.ref_to(f"device{REPLY_MARK}7").tell(DeviceStopped())


# --- the stream underneath ---------------------------------------------------


async def test_a_connection_lets_go_of_its_socket_when_the_stream_ends():
    """A socket nobody owns is a shutdown that waits on it. When the far side
    goes, this side is finished with it too -- and says so."""
    wire = Wire(Codec(MESSAGES))
    accepted: asyncio.Future = asyncio.get_running_loop().create_future()

    async def on_client(reader, writer):
        accepted.set_result(Connection(reader, writer, wire))

    server = await asyncio.start_server(on_client, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    client_reader, client_writer = await asyncio.open_connection(host, port)
    connection = await accepted

    client_writer.close()
    await connection.serve()  # returns when the far side is gone

    assert connection.closed.is_set()
    assert connection.let_go()
    server.close()
    await server.wait_closed()
