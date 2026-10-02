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
class ActorDied:
    """A runtime's last word about the actor it was hosting, sent to its peer.

    Never reaches a handler. It exists so that a death across a boundary says
    as much as the same death in this process would: without it, an actor that
    raised would be reported only as a process that exited, and the exception
    that explains it would be lost at exactly the moment it is wanted.
    """

    reason: str | None


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
    """Reported to an actor's parent when it hits an error that is not an
    answer to anyone. Every actor passes one it receives on to its parent,
    unless it acts on it, and a root publishes it, so a Fault rises until
    something handles it. ``source`` is the actor that faulted; ``fatal``
    means it cannot continue in its role."""

    source: ActorRef
    reason: str
    fatal: bool
