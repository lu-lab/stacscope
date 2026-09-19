"""Cross-boundary delivery: the codec, the wire frame, and routing.

Depends on ``core.actors``, never the reverse.
"""

from stacscope.core.transport.codec import Codec, CodecError
from stacscope.core.transport.routing import Peer, Undeliverable
from stacscope.core.transport.wire import Wire

__all__ = ["Codec", "CodecError", "Peer", "Undeliverable", "Wire"]
