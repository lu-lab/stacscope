"""The gateway: where clients outside the engine reach it.

A client is anything that drives the engine from outside it: the stacscope
command, a script, a Pycro-Manager hook, a page in a browser. Every client
connects to the same WebSocket endpoint and speaks the same frames, so a
client in any language that has WebSocket and msgpack can be one.

    gateway = Gateway(coordinator, port=8765, origins=("https://lab.example",))
    spawn(gateway, name="gateway")

A frame is one binary WebSocket message holding a msgpack map:

    {"t": <the actor it is for>, "p": <priority: 0 urgent, 1 normal, 2 low>,
     "s": <your reply address, or nil>, "m": <the message>}

The message is a map of its fields plus "type", its name. Replies come back
in the same frames, with your reply address in "t". Every address you give
-- "s", and the subscriber in a Subscribe -- is your own: you choose it, and
nobody else sees or reaches it.

What a client may do:
- Send a request to the Coordinator ("coordinator"), the timing source, or
  any device the Coordinator lists as present, if the request is one that
  clients may send. Anything else is answered Rejected.
- Subscribe to the Coordinator, to hear its Status at once and then
  everything it publishes; to the timing source, to hear which waveforms
  play; or to any device the Coordinator lists as present, to hear its
  properties change. Unsubscribe the same way.
A frame that cannot be read is answered Rejected, at your reply address if
it can be found and at "" if not. The connection stays open.

The gateway listens on this computer only (127.0.0.1). A client that
disconnects leaves nothing behind, and changes nothing in the engine: an
acquisition it started keeps running. A client that does not read what it
is sent fast enough is disconnected, and can reconnect.
"""

import asyncio
import contextlib
import ipaddress
import itertools
from collections import OrderedDict
from dataclasses import dataclass

import msgpack
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from stacscope.core.actors import (
    Actor,
    ActorRef,
    Envelope,
    LocalTransport,
    Priority,
    Rejected,
    Subscribe,
    Unsubscribe,
)
from stacscope.core.protocol import MESSAGES, DeviceLaunched, GetStatus, MessageRegistry, Status
from stacscope.core.transport import Codec, CodecError, Wire
from stacscope.core.transport.stream import MAX_FRAME

QUEUE = 256
"""Frames that may wait to be sent to one client. A client further behind
than this is disconnected."""

ROOT = ""
"""A client's own address: where a reply goes when a frame names none."""

# Senders a client has used, held strongly so a reply can still find them.
# A reply from a device in a process of its own comes back through that
# link's address book, which holds senders weakly; without a strong ref
# here the reply would arrive to find nobody. Bounded, because a client
# names a fresh reply address for every request: an answer to one of the
# oldest is lost only after this many newer requests.
_SENDERS = 1024


@dataclass(frozen=True)
class _Arrived:
    client: "_Client"


@dataclass(frozen=True)
class _Received:
    client: "_Client"
    data: bytes | str


@dataclass(frozen=True)
class _Left:
    client: "_Client"


@dataclass(frozen=True)
class _Published:
    """Something an actor the gateway subscribes to published, or answered
    the gateway with."""

    target: str
    msg: object
    priority: Priority


class Gateway(Actor):
    """Serves clients outside the engine over one WebSocket endpoint, in
    front of the Coordinator ``coordinator``; see the module's documentation.

    ``interface`` and ``port`` are where it listens; ``port=0`` picks a free
    port, and ``port`` holds the one taken once the actor has started.
    ``interface`` must be a loopback one, or construction raises
    ``ValueError``. ``origins`` lists the web pages, by origin
    (``"https://lab.example"``), that may connect from a browser; clients
    that are not browsers send no origin and are always accepted. ``queue``
    is how many frames may wait for one client before it is disconnected,
    and ``max_message`` the largest message, in bytes, a client may send.
    """

    def __init__(
        self,
        coordinator: ActorRef,
        *,
        interface: str = "127.0.0.1",
        port: int = 0,
        origins: tuple[str, ...] = (),
        queue: int = QUEUE,
        max_message: int = MAX_FRAME,
        registry: MessageRegistry = MESSAGES,
    ) -> None:
        # Until the endpoint has TLS and a token (decision 3, 2026-10-05),
        # whoever can reach the port can drive the instrument.
        if not loopback(interface):
            raise ValueError(
                f"the gateway listens only on a loopback interface (127.0.0.1, ::1, localhost), not {interface!r}"
            )
        super().__init__()
        self.coordinator = coordinator
        self.interface = interface
        self.port = port
        self.origins = tuple(origins)
        self.queue = queue
        self.max_message = max_message
        self._registry = registry
        self._wire = Wire(Codec(registry))
        self._server: Server | None = None
        self._numbers = itertools.count(1)
        self._clients: set[_Client] = set()
        # Where a client may send: the Coordinator, and whatever its latest
        # Status lists as present. Rebuilt from every Status.
        self._directory: dict[str, ActorRef] = {coordinator.address: coordinator}
        self._source: str | None = None
        self._status: Status | None = None
        # Actors the gateway subscribes to on its clients' behalf, by
        # address, with the ref it gave each as the subscriber, and each one's
        # subscribers among the clients. The engine holds only the gateway,
        # once per actor, never a client: clients come and go with sockets.
        self._watching: dict[str, ActorRef] = {}
        self._subscribers: dict[str, dict[tuple[_Client, ActorRef], None]] = {}

    @property
    def clients(self) -> int:
        """How many clients are connected. Diagnostic."""
        return len(self._clients)

    async def setup(self) -> None:
        coordinator = self.coordinator.address
        self._watch(coordinator)
        self.coordinator.tell(GetStatus(), sender=self._watching[coordinator])
        self._server = await serve(
            self._serve,
            self.interface,
            self.port,
            origins=[None, *self.origins],  # None: a client that is not a browser
            max_size=self.max_message,
            # Frames are small, and compressing them would cost the engine's
            # loop, which the timing source shares.
            compression=None,
        )
        self.port = self._server.sockets[0].getsockname()[1]

    async def teardown(self) -> None:
        for target, subscriber in self._watching.items():
            if (ref := self._directory.get(target)) is not None:
                ref.tell(Unsubscribe(subscriber), sender=self.ref)
        self._watching.clear()
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()

    async def handle(self, msg: object) -> None:
        match msg:
            case _Arrived(client):
                self._clients.add(client)
            case _Received(client, data):
                if not client.gone:
                    self._received(client, data)
            case _Left(client):
                self._left(client)
            case _Published(target, published, priority):
                self._published(target, published, priority)
            case _:
                await super().handle(msg)

    # --- one connection -------------------------------------------------------------

    async def _serve(self, ws: ServerConnection) -> None:
        # Runs as the server's task for each connection, not in this actor:
        # everything it learns goes to the mailbox, and the actor alone holds
        # the gateway's state.
        client = _Client(self.ref.address, next(self._numbers), ws, self._wire, self.queue)
        self.ref.tell(_Arrived(client), sender=None)
        writer = asyncio.create_task(client.write())
        try:
            async for data in ws:
                self.ref.tell(_Received(client, data), sender=None)
        except ConnectionClosed:
            pass
        finally:
            client.gone = True
            writer.cancel()
            self.ref.tell(_Left(client), sender=None)

    def _received(self, client: "_Client", data: bytes | str) -> None:
        if isinstance(data, str):
            self._refuse(client, None, "a frame is a binary message holding msgpack, not text")
            return
        try:
            # Every address in the frame, the sender's and any ref in the
            # message, is read as the client's own: whatever it writes, it
            # can only ever be answered itself.
            to, envelope, priority = self._wire.unpack(data, resolve=client.ref)
        except CodecError as exc:
            self._refuse(client, _reply_address(client, data), f"bad frame: {exc}")
            return
        msg, sender = envelope.msg, envelope.sender
        match msg:
            case Subscribe(subscriber) | Unsubscribe(subscriber):
                # An actor lost and watched stays subscribable, as the one it
                # will be relaunched as: the subscriptions carry over to it.
                if to not in self._directory and to != self._source and to not in self._watching:
                    self._refuse(client, sender, f"{to!r} publishes nothing to clients")
                elif isinstance(msg, Subscribe):
                    self._subscribe(to, client, subscriber)
                else:
                    self._unsubscribe(to, client, subscriber)
            case _ if not self._registry.client_sendable(type(msg)):
                self._refuse(client, sender, f"clients may not send {type(msg).__name__}")
            case _:
                target = self._directory.get(to)
                if target is None:
                    self._refuse(client, sender, f"no actor at {to!r} takes messages from clients")
                else:
                    target.tell(msg, priority, sender=sender)

    def _refuse(self, client: "_Client", reply: ActorRef | None, reason: str) -> None:
        if reply is None:
            client.send(ROOT, Envelope(Rejected(reason), self.ref), Priority.NORMAL)
        else:
            reply.tell(Rejected(reason), sender=self.ref)

    def _left(self, client: "_Client") -> None:
        client.gone = True
        self._clients.discard(client)
        for target, subscribers in list(self._subscribers.items()):
            for owner, subscriber in list(subscribers):
                if owner is client:
                    self._unsubscribe(target, client, subscriber)

    # --- subscriptions ------------------------------------------------------------

    def _subscribe(self, target: str, client: "_Client", subscriber: ActorRef) -> None:
        self._subscribers.setdefault(target, {})[client, subscriber] = None
        if target == self.coordinator.address and self._status is not None:
            # A page draws from the last Status it has, so it gets one now,
            # not at the next change. It is the one every later publication
            # follows, so nothing is missed or heard twice.
            subscriber.tell(self._status, sender=self.coordinator)
        if target not in self._watching:
            self._watch(target)

    def _unsubscribe(self, target: str, client: "_Client", subscriber: ActorRef) -> None:
        subscribers = self._subscribers.get(target, {})
        subscribers.pop((client, subscriber), None)
        if not subscribers and target != self.coordinator.address:
            # The gateway's own subscription to the Coordinator stays: it is
            # how the directory is kept.
            self._subscribers.pop(target, None)
            watched = self._watching.pop(target, None)
            if watched is not None and (ref := self._directory.get(target)) is not None:
                ref.tell(Unsubscribe(watched), sender=self.ref)

    def _watch(self, target: str) -> None:
        def receive(envelope: Envelope, priority: Priority) -> None:
            self._receive(Envelope(_Published(target, envelope.msg, priority), envelope.sender), priority)

        # One subscriber per actor watched, so a publication says whose it is
        # whoever published it -- from a handler, a routine or a test.
        subscriber = ActorRef(LocalTransport(receive), f"{self.ref.address}/~{target}")
        self._watching[target] = subscriber
        if (ref := self._directory.get(target)) is not None:
            ref.tell(Subscribe(subscriber), sender=self.ref)

    def _published(self, target: str, msg: object, priority: Priority) -> None:
        if target == self.coordinator.address:
            match msg:
                case Status():
                    self._status = msg
                    self._learn(msg)
                case DeviceLaunched(_, ref):
                    self._directory[ref.address] = ref
                    if (watched := self._watching.get(ref.address)) is not None:
                        # A relaunched actor knows nothing of its predecessor's
                        # subscribers: without this every client goes deaf to it.
                        ref.tell(Subscribe(watched), sender=self.ref)
        publisher = self._directory.get(target) or ActorRef(None, target)
        for _, subscriber in self._subscribers.get(target, {}):
            subscriber.tell(msg, priority, sender=publisher)

    def _learn(self, status: Status) -> None:
        directory = {self.coordinator.address: self.coordinator}
        for device in status.devices:
            if device.ref is None:
                continue
            if device.present:
                directory[device.ref.address] = device.ref
            if device.source:
                self._source = device.ref.address  # stable, lost or not
        self._directory = directory


class _Client:
    """One connection: its namespace, and the frames waiting for it."""

    def __init__(self, gateway: str, number: int, ws: ServerConnection, wire: Wire, queue: int) -> None:
        self.prefix = f"{gateway}/c{number}"
        self.ws = ws
        self.gone = False
        self._wire = wire
        self._frames: asyncio.Queue[bytes] = asyncio.Queue(queue)
        self._refs: OrderedDict[str, ActorRef] = OrderedDict()

    def ref(self, local: str) -> ActorRef:
        """The ref an actor holds for this client's address ``local``."""
        ref = self._refs.pop(local, None)
        if ref is None:
            ref = ActorRef(_ToClient(self, local), f"{self.prefix}/{local}")
        self._refs[local] = ref
        if len(self._refs) > _SENDERS:
            self._refs.popitem(last=False)
        return ref

    def send(self, to: str, envelope: Envelope, priority: Priority) -> None:
        # Called by whichever actor tells this client something, so it must
        # never raise into that actor: a camera answering must not die of a
        # tab being closed.
        if self.gone:
            return
        try:
            frame = self._wire.pack(to, envelope, priority)
        except CodecError:
            return  # a message that cannot cross; until logging exists, it is dropped
        try:
            self._frames.put_nowait(frame)
        except asyncio.QueueFull:
            self.drop()

    def drop(self) -> None:
        """Cut the connection off: the client is too far behind."""
        if not self.gone:
            self.gone = True
            # Aborted, not closed: a client that is not reading would not
            # read a close frame either, and a close waits for its answer.
            self.ws.transport.abort()

    async def write(self) -> None:
        with contextlib.suppress(ConnectionClosed):
            while True:
                await self.ws.send(await self._frames.get())


class _ToClient:
    """The transport behind a ref to a client's address."""

    def __init__(self, client: _Client, local: str) -> None:
        self._client = client
        self._local = local

    def deliver(self, envelope: Envelope, priority: Priority) -> None:
        self._client.send(self._local, envelope, priority)


def _reply_address(client: _Client, data: bytes) -> ActorRef | None:
    """The sender a frame names, if that much of it can be read."""
    try:
        raw = msgpack.unpackb(data, raw=False)
    except Exception:  # anything at all: this is the frame that failed
        return None
    sender = raw.get("s") if isinstance(raw, dict) else None
    return client.ref(sender) if isinstance(sender, str) else None


def loopback(interface: str) -> bool:
    """Whether ``interface`` is one only this computer can reach."""
    if interface == "localhost":
        return True
    try:
        return ipaddress.ip_address(interface).is_loopback
    except ValueError:
        return False
