"""WS6–WS17 -- the gateway: one WebSocket endpoint for every outside client.

A client is anything outside the engine -- the CLI, a script, a Pycro-Manager
hook, a browser page -- and the gateway is where its frames enter the actor
system. Everything here is about what a client must *not* be able to do with
a socket: speak as an engine actor, reach an actor it has no business with,
send the engine's internal protocol, leave refs behind in actors when it
goes, or slow the engine down by not reading.

These tests put the gateway in front of fake actors that record what reaches
them and from whom, so each rule is checked at the actor, where it matters.
``test_gateway_rig.py`` runs the same gateway in front of a simulated rig.
They run on the real clock, over real sockets, each wait with a deadline.
"""

import asyncio
import contextlib
import time

import msgpack
import numpy as np
import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed, InvalidStatus

from stacscope.core.actors import (
    Actor,
    ActorRef,
    Envelope,
    Heartbeat,
    Priority,
    Rejected,
    Subscribe,
    Unsubscribe,
    spawn,
)
from stacscope.core.gateway import Gateway
from stacscope.core.protocol import (
    MESSAGES,
    CoordinatorState,
    DeviceLaunched,
    DeviceStatus,
    FinishRecording,
    GetShortestTriggerPeriod,
    GetStatus,
    Go,
    MainApplied,
    SetSaveDirectory,
    SetWaveform,
    ShortestTriggerPeriod,
    StartDevice,
    Status,
    Waveform,
)
from stacscope.core.transport import Codec, Wire

pytestmark = pytest.mark.timeout(30)

DEADLINE = 5.0
QUIET = 0.2
"""Seconds a client listens to be sure nothing more is coming."""

ALLOWED_ORIGIN = "https://allowed.example"


# --- the engine the gateway fronts --------------------------------------------


class FakeCoordinator(Actor):
    """Answers ``GetStatus`` with ``status``; records everything else, with
    its sender's address."""

    def __init__(self) -> None:
        super().__init__()
        self.status = Status(CoordinatorState.IDLE, 0, ())
        self.heard: list[tuple[object, str | None]] = []

    async def handle(self, msg):
        match msg:
            case GetStatus():
                self.reply(self.status)
            case _:
                self.heard.append((msg, self.sender.address if self.sender else None))

    def change(self, status: Status) -> None:
        self.status = status
        self.publish(status)


class Spy(Actor):
    """A device or source. Records what reaches it, with its sender's address.
    Answers ``SetSaveDirectory`` with a ``Rejected`` naming the sender, and
    ``GetShortestTriggerPeriod`` once ``release`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.heard: list[tuple[object, str | None]] = []
        self.release = asyncio.Event()

    async def handle(self, msg):
        self.heard.append((msg, self.sender.address if self.sender else None))
        match msg:
            case SetSaveDirectory():
                self.reply(Rejected(f"from {self.sender.address}"))
            case GetShortestTriggerPeriod():
                asker = self.sender
                await self.release.wait()
                asker.tell(ShortestTriggerPeriod(0.01))


class Engine:
    def __init__(self) -> None:
        self.coordinator = FakeCoordinator()
        self.ref = spawn(self.coordinator, name="coordinator")
        self.source = Spy()
        self.cam = Spy()
        self.source_ref = spawn(self.source, parent=self.coordinator, name="source")
        self.cam_ref = spawn(self.cam, parent=self.coordinator, name="cam")
        self.coordinator.status = self.status()

    def status(self, state=CoordinatorState.IDLE, acquisition=0, *, extra=()) -> Status:
        return Status(
            state,
            acquisition,
            (
                DeviceStatus("source", True, True, True, self.source_ref),
                DeviceStatus("cam", False, True, True, self.cam_ref),
                *extra,
            ),
        )


@pytest.fixture
async def engine():
    engine = Engine()
    await asyncio.wait_for(engine.coordinator.started.wait(), DEADLINE)
    yield engine
    engine.coordinator.stop()
    await asyncio.wait_for(engine.coordinator.stopped.wait(), DEADLINE)


@pytest.fixture
async def gateway(engine):
    gateway = Gateway(engine.ref, port=0, origins=(ALLOWED_ORIGIN,), queue=8)
    spawn(gateway, name="gateway")
    await asyncio.wait_for(gateway.started.wait(), DEADLINE)
    yield gateway
    gateway.stop()
    await asyncio.wait_for(gateway.stopped.wait(), DEADLINE)
    assert gateway.exit_error is None


# --- a client, by hand --------------------------------------------------------

WIRE = Wire(Codec(MESSAGES))


def address(local: str) -> ActorRef:
    """A ref as a client writes one: only its address travels."""
    return ActorRef(None, local)


class Client:
    """A client speaking frames by hand, so a test can send anything."""

    def __init__(self, ws) -> None:
        self.ws = ws

    @classmethod
    async def connect(cls, gateway, **options) -> "Client":
        ws = await connect(f"ws://127.0.0.1:{gateway.port}", open_timeout=DEADLINE, **options)
        return cls(ws)

    async def send(self, to: str, msg, sender: str | None = "ask-1", priority=Priority.NORMAL) -> None:
        envelope = Envelope(msg, None if sender is None else address(sender))
        await self.ws.send(WIRE.pack(to, envelope, priority))

    async def recv(self) -> tuple[str, str | None, object]:
        """The next frame: who it is for here, who sent it, and the message."""
        data = await asyncio.wait_for(self.ws.recv(), DEADLINE)
        to, envelope, _ = WIRE.unpack(data, resolve=address)
        return to, (envelope.sender.address if envelope.sender else None), envelope.msg

    async def nothing(self) -> None:
        with pytest.raises(TimeoutError):
            await asyncio.wait_for(self.ws.recv(), QUIET)

    async def close(self) -> None:
        await self.ws.close()


@pytest.fixture
async def clients(gateway):
    opened: list[Client] = []

    async def open_client(**options) -> Client:
        client = await Client.connect(gateway, **options)
        opened.append(client)
        return client

    yield open_client
    for client in opened:
        await client.close()


async def until(condition, what: str) -> None:
    deadline = time.monotonic() + DEADLINE
    while not condition():
        assert time.monotonic() < deadline, f"never: {what}"
        await asyncio.sleep(0.01)


def subscribers(actor: Actor) -> list[str]:
    return [ref.address for ref in actor._subscribers]


# --- WS6: a client speaks only as itself --------------------------------------


async def test_ws6_the_actor_sees_the_clients_sender_inside_the_clients_own_namespace(engine, clients):
    """An actor replies to whoever its message names as the sender. Whatever
    a client writes there, the actor must see an address under the gateway,
    one per client, so a reply can only ever go back to that client."""
    one, two = await clients(), await clients()

    await one.send("coordinator/cam", SetSaveDirectory("D:/x"), sender="ask-1")
    await two.send("coordinator/cam", SetSaveDirectory("D:/x"), sender="ask-1")
    await until(lambda: len(engine.cam.heard) == 2, "both requests reach the camera")

    [(_, first), (_, second)] = engine.cam.heard
    assert first.startswith("gateway/") and first.endswith("/ask-1")
    assert second.startswith("gateway/") and second.endswith("/ask-1")
    assert first != second


async def test_ws6_a_client_claiming_to_be_an_engine_actor_gets_the_reply_itself(engine, clients):
    """A client that writes "coordinator" as its sender must not make the
    camera answer the Coordinator: the Coordinator would take a reply it never
    asked for as the answer to one it did."""
    client = await clients()

    await client.send("coordinator/cam", SetSaveDirectory("D:/x"), sender="coordinator")

    to, sender, msg = await client.recv()
    assert (to, sender) == ("coordinator", "coordinator/cam")
    assert isinstance(msg, Rejected)
    [(_, seen)] = engine.cam.heard
    assert seen != "coordinator" and seen.startswith("gateway/")
    assert engine.coordinator.heard == []


async def test_ws6_two_clients_with_the_same_reply_address_each_get_their_own_answer(engine, clients):
    one, two = await clients(), await clients()

    await one.send("coordinator/cam", SetSaveDirectory("D:/x"), sender="ask-1")
    _, _, first = await one.recv()
    await two.send("coordinator/cam", SetSaveDirectory("D:/x"), sender="ask-1")
    _, _, second = await two.recv()

    [(_, one_address), (_, two_address)] = engine.cam.heard
    assert first == Rejected(f"from {one_address}")
    assert second == Rejected(f"from {two_address}")
    await one.nothing()
    await two.nothing()


# --- WS7: refs in a client's message are the client's own ---------------------


async def test_ws7_a_ref_in_a_clients_message_is_the_clients_own(engine, clients):
    """``Subscribe`` is the only client message that carries a ref. A client
    naming a device as the subscriber must not make the Coordinator publish
    to that device: a device that hears another's Fault passes it up, and the
    two trade it for ever. The ref is read as the client's own address, so
    the publications come back to the client, and the device hears nothing."""
    client = await clients()

    await client.send("coordinator", Subscribe(address("coordinator/cam")))
    to, sender, snapshot = await client.recv()
    engine.coordinator.change(engine.status(CoordinatorState.STARTING, 1))
    later = await client.recv()

    assert (to, sender, type(snapshot)) == ("coordinator/cam", "coordinator", Status)
    assert later[0] == "coordinator/cam" and later[2].state is CoordinatorState.STARTING
    assert engine.cam.heard == []
    assert all(not a.startswith("coordinator") for a in subscribers(engine.coordinator))


# --- WS8: only client-sendable messages pass ----------------------------------


@pytest.mark.parametrize(
    ("to", "msg"),
    [
        ("coordinator/cam", StartDevice("run", 1)),
        ("coordinator/source", Go()),
        ("coordinator/cam", FinishRecording(3)),
        ("coordinator", Heartbeat()),  # answered by the runtime, never by handle
    ],
    ids=["StartDevice", "Go", "FinishRecording", "Heartbeat"],
)
async def test_ws8_the_engines_own_protocol_is_refused_and_never_reaches_the_actor(engine, clients, to, msg):
    client = await clients()

    await client.send(to, msg, sender="ask-1")

    reply_to, sender, reply = await client.recv()
    assert (reply_to, sender) == ("ask-1", "gateway")
    assert isinstance(reply, Rejected) and type(msg).__name__ in reply.reason
    await client.nothing()
    assert engine.coordinator.heard == engine.cam.heard == engine.source.heard == []


async def test_ws8_a_client_sendable_message_is_delivered_with_its_priority(engine, clients):
    client = await clients()

    await client.send("coordinator/cam", GetShortestTriggerPeriod(), priority=Priority.URGENT)
    await until(lambda: engine.cam.heard, "the request reaches the camera")
    engine.cam.release.set()

    assert await client.recv() == ("ask-1", "coordinator/cam", ShortestTriggerPeriod(0.01))


# --- WS9: only the actors the Coordinator lists -------------------------------


async def test_ws9_a_destination_the_coordinator_does_not_list_is_refused_naming_it(engine, clients):
    client = await clients()

    await client.send("coordinator/stranger", SetSaveDirectory("D:/x"))
    to, _, reply = await client.recv()

    assert to == "ask-1" and isinstance(reply, Rejected) and "coordinator/stranger" in reply.reason


async def test_ws9_a_device_launched_later_is_reachable_once_the_coordinator_says_so(engine, clients):
    """A device that is lost is unlisted (present, no ref) and refused; a
    relaunched one is announced by ``DeviceLaunched`` and must be reachable
    at once, or a client would have to reconnect after every relaunch."""
    client = await clients()
    absent = DeviceStatus("cam2", False, False, False, None)
    engine.coordinator.change(engine.status(extra=(absent,)))

    await client.send("coordinator/cam2", SetSaveDirectory("D:/x"))
    _, _, refused = await client.recv()

    cam2 = Spy()
    cam2_ref = spawn(cam2, parent=engine.coordinator, name="cam2")
    engine.coordinator.publish(DeviceLaunched("cam2", cam2_ref))
    await client.send("coordinator/cam2", SetSaveDirectory("D:/x"), sender="ask-2")
    to, sender, answered = await client.recv()

    assert isinstance(refused, Rejected) and "coordinator/cam2" in refused.reason
    assert (to, sender) == ("ask-2", "coordinator/cam2")
    assert answered.reason.startswith("from gateway/")


# --- WS10: a bad frame is answered, not fatal ---------------------------------


def frame(**fields) -> bytes:
    return msgpack.packb({"t": "coordinator", "p": 1, "s": "ask-9", "m": b"", **fields}, use_bin_type=True)


@pytest.mark.parametrize(
    ("data", "reply_to"),
    [
        (b"\xc1", ""),  # not msgpack
        (msgpack.packb([1, 2]), ""),  # msgpack, not a frame
        (frame(m=msgpack.packb({"type": "Nope"})), "ask-9"),  # an unknown message
        (frame(m=msgpack.packb({"type": "StartAcquisition", "cycles": 1})), "ask-9"),  # a field missing
        (frame(p=7), "ask-9"),  # an unknown priority
        ("GetStatus please", ""),  # a text message
    ],
    ids=["not-msgpack", "not-a-frame", "unknown-type", "missing-field", "bad-priority", "text"],
)
async def test_ws10_a_bad_frame_is_answered_rejected_and_the_connection_stays_open(engine, clients, data, reply_to):
    """A client in another language will get a frame wrong. It must hear why,
    at the address it gave if that much can be read, and at its own root
    address ("") if not -- and the connection must survive to send the next
    one."""
    client = await clients()

    await client.ws.send(data)
    to, sender, reply = await client.recv()
    await client.send("coordinator", GetStatus(), sender="ask-2")
    after = await client.recv()

    assert (to, sender, type(reply)) == (reply_to, "gateway", Rejected)
    assert after[0] == "ask-2" and isinstance(after[2], Status)
    assert engine.coordinator.heard == []


# --- WS11: a snapshot, then every change --------------------------------------


async def test_ws11_a_subscriber_gets_the_current_status_then_every_publication_in_order(engine, clients):
    """A page draws from the last Status it has. It needs one at once, not at
    the next change, and then every change after it with nothing missed and
    nothing twice -- even while the Coordinator is changing state."""
    client = await clients()
    states = [CoordinatorState.STARTING, CoordinatorState.RUNNING, CoordinatorState.STOPPING, CoordinatorState.IDLE]

    await client.send("coordinator", Subscribe(address("events")))
    for n, state in enumerate(states, start=1):
        engine.coordinator.change(engine.status(state, n))
        await asyncio.sleep(0)

    heard = []
    while len(heard) < 5:
        to, sender, msg = await client.recv()
        assert (to, sender) == ("events", "coordinator")
        heard.append(msg.acquisition)
        if msg.acquisition == len(states):
            break
    await client.nothing()

    assert heard == list(range(heard[0], len(states) + 1))


async def test_ws11_an_unsubscribed_client_hears_no_more(engine, clients):
    client = await clients()
    await client.send("coordinator", Subscribe(address("events")))
    await client.recv()

    await client.send("coordinator", Unsubscribe(address("events")))
    await client.send("coordinator", GetStatus(), sender="ask-2")
    await client.recv()  # the Unsubscribe is handled before this is answered
    engine.coordinator.change(engine.status(CoordinatorState.STARTING, 1))

    await client.nothing()


# --- WS12: one subscription per actor, whatever the number of clients ---------


async def test_ws12_clients_subscribed_to_the_source_share_one_subscription_and_survive_its_relaunch(
    engine, clients
):
    """A client queueing waveforms needs the source's ``MainApplied``. The
    source must hold the gateway once, not each client -- the engine never
    holds a client's ref -- and a relaunched source must be subscribed again,
    or every client goes deaf to it without a word."""
    one, two = await clients(), await clients()
    await one.send("coordinator/source", Subscribe(address("mains")))
    await two.send("coordinator/source", Subscribe(address("mains")))
    await until(lambda: len(subscribers(engine.source)) == 1, "the gateway subscribes to the source")
    await one.send("coordinator", GetStatus(), sender="sync")
    await one.recv()
    await two.send("coordinator", GetStatus(), sender="sync")
    await two.recv()

    engine.source.publish(MainApplied("a", 0, 0))
    assert await one.recv() == ("mains", "coordinator/source", MainApplied("a", 0, 0))
    assert await two.recv() == ("mains", "coordinator/source", MainApplied("a", 0, 0))
    assert subscribers(engine.source) == ["gateway/~coordinator/source"]

    engine.source.stop()
    await asyncio.wait_for(engine.source.stopped.wait(), DEADLINE)
    successor = Spy()
    successor_ref = spawn(successor, parent=engine.coordinator, name="source")
    engine.coordinator.publish(DeviceLaunched("source", successor_ref))
    await until(lambda: subscribers(successor) == ["gateway/~coordinator/source"], "the gateway subscribes again")

    successor.publish(MainApplied("b", 0, 0))
    assert await one.recv() == ("mains", "coordinator/source", MainApplied("b", 0, 0))
    assert await two.recv() == ("mains", "coordinator/source", MainApplied("b", 0, 0))


async def test_dp17_a_device_present_in_status_can_be_subscribed_to(engine, clients):
    """A device publishes its property changes, so a page showing it sees a
    change a script made (decision 2)."""
    one, two = await clients(), await clients()
    await one.send("coordinator/cam", Subscribe(address("props")))
    await two.send("coordinator/cam", Subscribe(address("props")))
    await until(lambda: subscribers(engine.cam) == ["gateway/~coordinator/cam"], "one subscription")

    engine.cam.publish(MainApplied("x", 0, 0))

    assert await one.recv() == ("props", "coordinator/cam", MainApplied("x", 0, 0))
    assert await two.recv() == ("props", "coordinator/cam", MainApplied("x", 0, 0))


@pytest.mark.parametrize("target", ["coordinator/ghost", "coordinator/lost", "gateway"])
async def test_dp17_an_actor_not_present_in_status_cannot_be_subscribed_to(engine, clients, target):
    """Not a device the Coordinator lists, or one it lists as lost: there is
    nothing to subscribe to, and the gateway is not a publisher at all."""
    lost = DeviceStatus("lost", False, False, False, ActorRef(None, "coordinator/lost"))
    engine.coordinator.change(engine.status(extra=(lost,)))
    client = await clients()

    await client.send(target, Subscribe(address("events")), sender="ask-1")
    to, _, reply = await client.recv()

    assert to == "ask-1" and isinstance(reply, Rejected)


async def test_dp17_a_client_may_unsubscribe_from_a_device_while_it_is_lost(engine, gateway, clients):
    """Lost, the device is no longer one a client may send to, but the
    client's subscription is still the gateway's to end, so that a relaunch
    does not subscribe the gateway again for nobody."""
    client = await clients()
    await client.send("coordinator/cam", Subscribe(address("props")))
    await until(lambda: subscribers(engine.cam), "the gateway subscribes")
    lost = DeviceStatus("cam", False, True, False, engine.cam_ref)
    engine.coordinator.change(Status(CoordinatorState.IDLE, 0, (engine.status().devices[0], lost)))
    await until(lambda: "coordinator/cam" not in gateway._directory, "the gateway learns it is lost")

    await client.send("coordinator/cam", Unsubscribe(address("props")), sender="ask-1")

    await client.nothing()  # no Rejected
    await until(lambda: "coordinator/cam" not in gateway._watching, "the gateway stops watching it")


async def test_dp17_a_client_that_leaves_leaves_no_subscription_to_a_device(engine, gateway, clients):
    client = await clients()
    await client.send("coordinator/cam", Subscribe(address("props")))
    await until(lambda: subscribers(engine.cam), "the gateway subscribes")

    await client.close()

    await until(lambda: subscribers(engine.cam) == [], "the gateway unsubscribes")


# --- WS13: a client leaves nothing behind -------------------------------------


async def test_ws13_no_actor_holds_a_client_and_a_reply_to_a_client_gone_is_dropped(engine, gateway, clients):
    """A ref to a client held by an actor would outlive the client, and a
    tell to a dead socket must never raise in the actor that sends it -- the
    camera answering would die of a browser tab being closed."""
    client = await clients()
    await client.send("coordinator", Subscribe(address("events")))
    await client.send("coordinator/source", Subscribe(address("mains")))
    await client.send("coordinator/cam", GetShortestTriggerPeriod(), sender="ask-1")
    await until(lambda: engine.cam.heard, "the camera is asked")

    await client.close()
    await until(lambda: gateway.clients == 0, "the gateway lets the client go")
    engine.cam.release.set()
    await asyncio.sleep(QUIET)

    for actor in (engine.coordinator, engine.source, engine.cam):
        assert all(a.startswith("gateway/~") for a in subscribers(actor)), subscribers(actor)
    assert subscribers(engine.source) == []  # its last subscriber left
    assert not engine.cam.stopped.is_set() and engine.cam.exit_error is None
    assert not gateway.stopped.is_set()


# --- WS14: a client that does not read ----------------------------------------


async def test_ws14_a_client_that_stops_reading_is_disconnected_and_nobody_else_waits(engine, gateway, clients):
    """A browser tab in the background stops reading. Its frames pile up in
    the gateway until its queue is full; then it is cut off, as a client
    that cannot keep up is (decision 6), rather than holding memory or
    dropping what it would have heard in silence. Other clients, and the
    Coordinator publishing, must not notice."""
    reader = await clients()
    stalled = await clients(max_queue=1)  # takes one frame, then stops reading the socket
    await reader.send("coordinator", Subscribe(address("events")))
    await stalled.send("coordinator", Subscribe(address("events")))
    await reader.recv()
    heard: list[int] = []

    async def read():
        while True:
            _, _, msg = await reader.recv()
            heard.append(msg.acquisition)

    reading = asyncio.create_task(read())
    big = DeviceStatus("x" * 100_000, False, False, False, None)
    sent, lag = 0, 0.0
    try:
        while gateway.clients == 2:
            assert sent < 4000, "the stalled client was never cut off"
            for _ in range(4):  # in bursts, as an engine publishes
                sent += 1
                engine.coordinator.change(engine.status(CoordinatorState.IDLE, sent, extra=(big,)))
            before = time.monotonic()
            await asyncio.sleep(0.005)
            lag = max(lag, time.monotonic() - before - 0.005)
        await until(lambda: heard and heard[-1] == sent, "the reader hears every publication")
    finally:
        reading.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await reading

    assert heard == list(range(1, sent + 1))
    assert lag < 1.0, f"the engine's loop was held for {lag:.3f} s"
    await reader.send("coordinator", GetStatus(), sender="ask-2")
    assert (await reader.recv())[0] == "ask-2"


# --- WS15: origins ----------------------------------------------------------------


async def test_ws15_a_client_with_no_origin_or_a_listed_one_is_accepted(clients):
    """Python clients send no Origin; a page from a listed origin sends its own."""
    for options in ({}, {"origin": ALLOWED_ORIGIN}):
        client = await clients(**options)
        await client.send("coordinator", GetStatus())
        assert isinstance((await client.recv())[2], Status)


async def test_ws15_a_page_from_any_other_origin_is_refused_at_the_handshake(gateway):
    """Any web page the user has open can try to open a socket to localhost.
    Only the pages the rig's config lists may drive the instrument."""
    with pytest.raises(InvalidStatus) as refused:
        await connect(f"ws://127.0.0.1:{gateway.port}", origin="https://elsewhere.example", open_timeout=DEADLINE)

    assert refused.value.response.status_code == 403


# --- WS16: message size ---------------------------------------------------------


async def test_ws16_a_message_over_the_limit_closes_that_client_only(engine, gateway, clients):
    """Control traffic is small; the largest message is a waveform of about
    200 KB, which must get through. A message far larger is a client out of
    step, and it is cut off -- nobody else is."""
    other, sender = await clients(), await clients()
    ao = np.zeros((5, 5000))
    do = np.zeros((3, 5000), dtype=bool)

    await sender.send("coordinator/source", SetWaveform(Waveform.MAIN, ao, do))
    await until(lambda: engine.source.heard, "the waveform reaches the source")
    await sender.ws.send(bytes(gateway.max_message + 1))
    with pytest.raises(ConnectionClosed) as closed:
        await asyncio.wait_for(sender.ws.recv(), DEADLINE)

    assert closed.value.rcvd.code == 1009  # message too big
    [(waveform, _)] = engine.source.heard
    assert waveform.ao.shape == (5, 5000) and waveform.ao.nbytes == 200_000
    await other.send("coordinator", GetStatus())
    assert isinstance((await other.recv())[2], Status)


# --- WS17: localhost only ---------------------------------------------------------


@pytest.mark.parametrize("interface", ["0.0.0.0", "::", "192.168.1.5", "lab-pc.example"])
def test_ws17_an_interface_other_than_loopback_is_refused(interface):
    """Until the endpoint has TLS and a token, anyone who can reach the port
    can drive the instrument, so it listens only where nobody else can."""
    with pytest.raises(ValueError, match="loopback"):
        Gateway(ActorRef(None, "coordinator"), interface=interface)


@pytest.mark.parametrize("interface", ["127.0.0.1", "::1", "localhost"])
def test_ws17_loopback_interfaces_are_accepted(interface):
    Gateway(ActorRef(None, "coordinator"), interface=interface)
