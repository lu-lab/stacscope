"""The wire frame: an envelope, ready for a byte stream.

The codec carries a message. This carries what surrounds one on its way to a
mailbox -- the sender and the priority -- neither of which is part of any
message: message types stay plain data.

The frame is a msgpack map of four keys, so it stays readable from another
language, like the messages inside it:

    {"t": <destination address>, "p": <priority>,
     "s": <sender address or nil>, "m": <encoded message>}

The destination travels in the frame, not the connection: one link carries
traffic for many addresses, a child's device answering the Coordinator and
every ``ask``'s one-shot reply address among them.

Priority travels in the frame as a number but is *enforced* at the receiving
actor's mailbox, not here.
"""

import msgpack

from stacscope.core.actors import ActorRef, Envelope, Priority
from stacscope.core.transport.codec import Codec, CodecError, Resolve

_TO = "t"
_PRIORITY = "p"
_SENDER = "s"
_MESSAGE = "m"
_KEYS = {_TO, _PRIORITY, _SENDER, _MESSAGE}


# The destination rides the frame rather than the connection because one
# link carries traffic for many addresses. Priority is enforced at the
# mailbox rather than here so a transport with no priorities of its own --
# ZeroMQ will not -- loses nothing by it.
class Wire:
    """Packs and unpacks one frame. Framing on a stream is the connection's job."""

    def __init__(self, codec: Codec) -> None:
        self._codec = codec

    def pack(self, to: str, envelope: Envelope, priority: Priority) -> bytes:
        sender = envelope.sender
        if sender is not None and not isinstance(sender, ActorRef):
            raise CodecError(f"sender {sender!r} is not an ActorRef")
        return msgpack.packb(
            {
                _TO: to,
                _PRIORITY: int(priority),
                _SENDER: None if sender is None else sender.address,
                _MESSAGE: self._codec.encode(envelope.msg),
            },
            use_bin_type=True,
        )

    def unpack(self, frame: bytes, *, resolve: Resolve) -> tuple[str, Envelope, Priority]:
        """``resolve`` turns an address back into a ref that reaches wherever it
        lives. Frames come from another process, so nothing here is trusted."""
        try:
            raw = msgpack.unpackb(frame, raw=False)
        except Exception as exc:
            raise CodecError(f"not a frame: {exc!r}") from None
        if not isinstance(raw, dict) or raw.keys() != _KEYS:
            raise CodecError(f"not a frame: {raw!r:.60}")
        if not isinstance(raw[_TO], str):
            raise CodecError(f"frame has no destination: {raw[_TO]!r}")
        try:
            priority = Priority(raw[_PRIORITY])
        except ValueError:
            raise CodecError(f"unknown priority {raw[_PRIORITY]!r}") from None

        address = raw[_SENDER]
        sender = None
        if address is not None:
            sender = resolve(address)
            if sender is None:
                raise CodecError(f"cannot resolve sender {address!r}")
        envelope = Envelope(self._codec.decode(raw[_MESSAGE], resolve=resolve), sender)
        return raw[_TO], envelope, priority
