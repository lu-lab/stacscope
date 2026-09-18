"""Messages the runtime itself sends. Plain data, like every other message."""

from dataclasses import dataclass

from stacscope.core.actors.ref import ActorRef
from stacscope.core.protocol.registry import message


@message
@dataclass(frozen=True, slots=True)
class ChildExited:
    """A child actor has ended. Sent to its parent at URGENT priority.

    ``error`` describes what ended it, or is ``None`` for a clean stop.
    A string, not the exception, because death may be detected across a
    process boundary (or by heartbeat) where no exception object exists.
    """

    child: ActorRef
    error: str | None


@message
@dataclass(frozen=True, slots=True)
class Heartbeat:
    """Liveness ping. Answered by the runtime on receipt, never by ``handle``."""


@message
@dataclass(frozen=True, slots=True)
class HeartbeatReply:
    pass


@message
@dataclass(frozen=True, slots=True)
class Subscribe:
    """Ask an actor to send ``subscriber`` everything it publishes.
    Handled by the runtime, in mailbox order; never reaches ``handle``."""

    subscriber: ActorRef


@message
@dataclass(frozen=True, slots=True)
class Unsubscribe:
    subscriber: ActorRef


@message
@dataclass(frozen=True, slots=True)
class Accepted:
    """Reply to a request that was carried out and has nothing else to say."""


@message
@dataclass(frozen=True, slots=True)
class Rejected:
    """Reply to a request the actor cannot honour in its current state."""

    reason: str


@message
@dataclass(frozen=True, slots=True)
class Fault:
    """Published when an actor hits an error that is not a reply to anyone.
    ``fatal`` means the publisher cannot continue in its role."""

    source: ActorRef
    reason: str
    fatal: bool
