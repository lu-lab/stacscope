"""Cross-boundary delivery: the codec, the wire frame, and the process transport.

Depends on ``core.actors``, never the reverse.
"""

from stacscope.core.transport.codec import Codec, CodecError
from stacscope.core.transport.wire import Wire

__all__ = ["Codec", "CodecError", "Wire"]
