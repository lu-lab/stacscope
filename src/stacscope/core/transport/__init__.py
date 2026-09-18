"""Cross-boundary delivery: the message codec now; Process / Zmq transports later.

Depends on ``core.actors``, never the reverse.
"""

from stacscope.core.transport.codec import Codec, CodecError

__all__ = ["Codec", "CodecError"]
