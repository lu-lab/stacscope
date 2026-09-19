"""X1 -- the wire frame: everything the codec deliberately does not carry.

The codec encodes a message's fields. What surrounds a message on its way to
a mailbox -- who it is for, who to answer, and how urgently -- belongs to the
transport, and has to survive the trip just as exactly. A lost ``reply_to`` is
an ``ask`` that hangs; a lost priority is a ``StopDevice`` queued behind a
refill loop.
"""

import msgpack
import pytest

from stacscope.core.actors import ActorRef, Envelope, LocalTransport, Priority
from stacscope.core.protocol import MESSAGES, Phase, SourcePhase, StartDevice, StopDevice
from stacscope.core.transport import Codec, CodecError, Wire


@pytest.fixture
def wire():
    return Wire(Codec(MESSAGES))


def ref(address: str) -> ActorRef:
    """Stand-in for a router's lookup: any address resolves to a ref."""
    return ActorRef(LocalTransport(lambda envelope, priority: None), address)


def frame(**fields) -> bytes:
    return msgpack.packb({"t": "coordinator", "p": 1, "r": None, "m": b"", **fields})


class Unregistered:
    """Not a message. Nothing stops an actor from telling one locally."""


def test_a_frame_round_trips_with_its_destination_priority_and_reply_address(wire):
    sent = Envelope(StartDevice("fish", 7), reply_to=ref("coordinator#reply-3"))

    packed = wire.pack("coordinator/camera", sent, Priority.URGENT)
    to, received, priority = wire.unpack(packed, resolve=ref)

    assert to == "coordinator/camera"
    assert received.msg == StartDevice("fish", 7)
    assert received.reply_to.address == "coordinator#reply-3"
    assert priority is Priority.URGENT


def test_a_told_message_arrives_told_and_not_answerable(wire):
    packed = wire.pack("viewer", Envelope(SourcePhase(Phase.COOLDOWN)), Priority.LOW)

    to, received, priority = wire.unpack(packed, resolve=ref)

    assert (to, received, priority) == ("viewer", Envelope(SourcePhase(Phase.COOLDOWN), None), Priority.LOW)


def test_an_unregistered_message_is_refused_where_it_is_sent(wire):
    with pytest.raises(CodecError, match="Unregistered"):
        wire.pack("coordinator", Envelope(Unregistered()), Priority.NORMAL)


def test_an_unregistered_reply_address_is_refused_where_it_is_sent(wire):
    """``reply_to`` is not part of the message, so the codec never sees it."""
    with pytest.raises(CodecError, match="reply address"):
        wire.pack("coordinator", Envelope(StopDevice(), reply_to="coordinator"), Priority.NORMAL)


def test_a_reply_address_that_resolves_nowhere_is_refused_on_arrival(wire):
    """Better a loud frame than a silent ``ask`` that can never be answered."""
    packed = wire.pack("coordinator", Envelope(StopDevice(), reply_to=ref("nobody")), Priority.NORMAL)

    with pytest.raises(CodecError, match="nobody"):
        wire.unpack(packed, resolve=lambda address: None)


@pytest.mark.parametrize(
    "bad",
    [
        b"",
        b"\xc1 not msgpack at all",
        msgpack.packb([1, 2, 3]),
        msgpack.packb({"t": "coordinator", "p": 1, "r": None}),
        frame(p=99),
        frame(t=None),
        frame(m=msgpack.packb({"type": "NoSuchMessage"})),
    ],
    ids=["empty", "garbage", "not-a-map", "no-message", "impossible-priority",
         "no-destination", "unknown-message"],
)
def test_a_frame_that_is_not_one_is_refused_rather_than_crashing(wire, bad):
    """Bytes arrive from another process. They are input, not a promise."""
    with pytest.raises(CodecError):
        wire.unpack(bad, resolve=ref)
