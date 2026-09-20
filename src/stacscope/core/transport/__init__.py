"""Cross-boundary delivery: the codec, the wire frame, routing, and processes.

Depends on ``core.actors``, never the reverse.
"""

from stacscope.core.transport.codec import Codec, CodecError
from stacscope.core.transport.process import LaunchFailed, ProcessHost
from stacscope.core.transport.routing import Peer, Undeliverable
from stacscope.core.transport.stream import Connection
from stacscope.core.transport.wire import Wire

__all__ = [
    "Codec",
    "CodecError",
    "Connection",
    "LaunchFailed",
    "Peer",
    "ProcessHost",
    "Undeliverable",
    "Wire",
]
