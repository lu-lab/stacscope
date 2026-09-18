"""Location-transparent actor addresses."""

from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from stacscope.core.actors.mailbox import Priority


@dataclass(frozen=True, slots=True)
class Envelope:
    """A message plus where to send the reply, if anyone is waiting for one.

    The reply address rides here rather than in the message so that message
    types stay plain data.
    """

    msg: object
    reply_to: "ActorRef | None" = None


class Transport(Protocol):
    """Carries an envelope to an actor's mailbox, wherever that actor lives."""

    def deliver(self, envelope: Envelope, priority: Priority) -> None: ...


class LocalTransport:
    """Same-process delivery: straight to the actor's receive side."""

    def __init__(self, receive: Callable[[Envelope, Priority], None]) -> None:
        self._receive = receive

    def deliver(self, envelope: Envelope, priority: Priority) -> None:
        self._receive(envelope, priority)


class ActorRef:
    """The only handle one actor holds on another. Never exposes the actor.

    Identity is the ``address`` — a hierarchical path such as
    ``coordinator/camera0`` — which is what crosses process and network
    boundaries. Two refs to the same address are equal.
    """

    def __init__(self, transport: Transport, address: str) -> None:
        self._transport = transport
        self.address = address

    def __eq__(self, other: object) -> bool:
        return isinstance(other, ActorRef) and other.address == self.address

    def __hash__(self) -> int:
        return hash(self.address)

    def __repr__(self) -> str:
        return f"ActorRef({self.address!r})"

    def tell(
        self,
        msg: object,
        priority: Priority = Priority.NORMAL,
        *,
        reply_to: "ActorRef | None" = None,
    ) -> None:
        self._transport.deliver(Envelope(msg, reply_to), priority)
