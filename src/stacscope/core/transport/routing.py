"""One end of a boundary: which actors are here, and how to reach the rest.

A ``Peer`` is the address book for a single link. It holds the refs that live
on this side, hands out refs for the ones that do not, and turns arriving
frames back into ordinary ``tell``s. Nothing above it can tell the difference:
an ``ActorRef`` from a peer behaves exactly like a local one, which is what
makes placement a composition-root decision rather than an architectural one.

Any address not registered here is assumed to be over there. That is true of a
child process, which has exactly one peer, and of an engine whose peers are its
children -- and it is what lets a ref cross inside a message and still work.
"""

import weakref
from collections.abc import Callable

from stacscope.core.actors import REPLY_MARK, ActorRef, Envelope, Priority
from stacscope.core.transport.wire import Wire


class Undeliverable(Exception):
    """A frame arrived for an address that is not on this side."""


class Peer:
    """The far end of one link, and the near end's directory."""

    def __init__(self, wire: Wire, send: Callable[[bytes], None]) -> None:
        self._wire = wire
        self._send = send
        self._local: dict[str, ActorRef] = {}
        self._replies: weakref.WeakValueDictionary[str, ActorRef] = weakref.WeakValueDictionary()
        """One-shot ``ask`` addresses that have crossed, held weakly: reachable
        while the asker is still waiting, released when it stops. Strong refs
        here would be a slow leak for the life of the connection."""

    # --- what is here ------------------------------------------------------

    def register(self, ref: ActorRef) -> None:
        """Make ``ref`` reachable from the far side, at its own address."""
        self._local[ref.address] = ref

    def unregister(self, address: str) -> None:
        self._local.pop(address, None)

    def pending_replies(self) -> int:
        """One-shot reply addresses still reachable here. Diagnostic."""
        return len(self._replies)

    # --- how to reach the rest ---------------------------------------------

    def ref_to(self, address: str) -> ActorRef:
        """A handle on an actor over there. Indistinguishable from a local ref."""
        return ActorRef(_Outbound(self, address), address)

    def resolve(self, address: str) -> ActorRef:
        """Turn an address off the wire back into something tellable."""
        ref = self._local.get(address) or self._replies.get(address)
        return ref if ref is not None else self.ref_to(address)

    # --- the link itself ---------------------------------------------------

    def send(self, to: str, envelope: Envelope, priority: Priority) -> None:
        reply_to = envelope.reply_to
        if reply_to is not None and reply_to.address not in self._local:
            self._replies[reply_to.address] = reply_to  # so the far side can answer it
        self._send(self._wire.pack(to, envelope, priority))

    def receive(self, frame: bytes) -> None:
        """Deliver one arriving frame, as though it had been told locally."""
        to, envelope, priority = self._wire.unpack(frame, resolve=self.resolve)
        target = self._local.get(to) or self._replies.get(to)
        if target is None:
            if REPLY_MARK in to:
                return  # a reply nobody is waiting for: discarded, as promised
            raise Undeliverable(f"no actor at {to!r} on this side")
        target.tell(envelope.msg, priority, reply_to=envelope.reply_to)


class _Outbound:
    """The transport behind every ref that points across a link."""

    def __init__(self, peer: Peer, address: str) -> None:
        self._peer = peer
        self._address = address

    def deliver(self, envelope: Envelope, priority: Priority) -> None:
        self._peer.send(self._address, envelope, priority)
