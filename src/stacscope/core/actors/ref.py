"""Location-transparent actor addresses."""

from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Protocol

from stacscope.core.actors.mailbox import Priority


@dataclass(frozen=True, slots=True)
class Envelope:
    """A message plus who sent it, so whoever handles it can answer.

    The sender rides here rather than in the message so that message types
    stay plain data. ``None`` when the message came from outside any actor
    and nobody is waiting to hear back.
    """

    msg: object
    sender: "ActorRef | None" = None


class Transport(Protocol):
    """Carries an envelope to an actor's mailbox, wherever that actor lives."""

    def deliver(self, envelope: Envelope, priority: Priority) -> None: ...


class LocalTransport:
    """Same-process delivery: straight to the actor's receive side."""

    def __init__(self, receive: Callable[[Envelope, Priority], None]) -> None:
        self._receive = receive

    def deliver(self, envelope: Envelope, priority: Priority) -> None:
        self._receive(envelope, priority)


# Set in each actor's own task by the runtime, and inherited by every task
# the actor starts, so a tell from a handler or a routine names its sender
# without the call site having to. A callback scheduled from the actor's task
# copies it too, which is right for a message an actor sends itself later.
current_sender: ContextVar["ActorRef | None"] = ContextVar("current_sender", default=None)


class _Unset:
    pass


_FROM_CONTEXT = _Unset()


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
        sender: "ActorRef | None | _Unset" = _FROM_CONTEXT,
    ) -> None:
        """Send ``msg``. The sender is the actor this is called from, or
        ``None`` from outside any actor; pass ``sender`` to name another."""
        if isinstance(sender, _Unset):
            sender = current_sender.get()
        self._transport.deliver(Envelope(msg, sender), priority)
