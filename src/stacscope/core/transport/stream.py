"""Frames over a byte stream, in both directions, for one link.

A frame is its length as four big-endian bytes, then that many bytes. Small
and explicit, so a client in another language can speak it without a library.

Nothing here drains the writer or applies backpressure: a send goes straight
into the socket's own buffer.
"""

import asyncio
from collections.abc import Callable

from stacscope.core.transport.codec import CodecError
from stacscope.core.transport.routing import Peer, Undeliverable
from stacscope.core.transport.wire import Wire

RUNTIME_MARK = "#runtime"
"""Appended to an actor's address to address the runtime hosting it instead.

A peer's runtime needs somewhere to say things about its actor -- above all
why it ended -- and this keeps that traffic inside ordinary addressing rather
than inventing a second channel beside the frames."""


def runtime_address(address: str) -> str:
    return f"{address}{RUNTIME_MARK}"


_LENGTH_BYTES = 4
MAX_FRAME = 1 << 22
"""4 MiB. Control traffic only: frames are never bulk data, which goes through
shared memory instead. An oversize length means the stream is out of step."""


def send_frame(writer: asyncio.StreamWriter, frame: bytes) -> None:
    if writer.is_closing():
        return  # the far side is gone; its death is reported by whoever is watching
    writer.write(len(frame).to_bytes(_LENGTH_BYTES, "big") + frame)


async def read_frame(reader: asyncio.StreamReader) -> bytes | None:
    """The next frame, or ``None`` at end of stream or on a stream out of step."""
    try:
        size = int.from_bytes(await reader.readexactly(_LENGTH_BYTES), "big")
        if size > MAX_FRAME:
            return None
        return await reader.readexactly(size)
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        return None


# tell is synchronous by design, and control traffic is tiny -- the largest
# message in the system is a waveform retune at roughly 200 KB -- so the
# socket's own buffer is queue enough. This link deliberately has no
# backpressure, for the same reason the image buffer does not: a sender that
# must wait on a receiver has coupled the two.
class Connection:
    """One link: a stream at the bottom, a ``Peer`` at the top."""

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        wire: Wire,
        *,
        on_error: Callable[[Exception], None] | None = None,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._on_error = on_error
        self.peer = Peer(wire, send=lambda frame: send_frame(self._writer, frame))
        self.closed = asyncio.Event()

    async def serve(self) -> None:
        """Deliver arriving frames until the stream ends.

        A frame that cannot be delivered is reported and skipped. Only the
        stream ending closes the link: one malformed message from a device is
        not grounds for cutting it off, and the link is how we would find out
        anything else about it.
        """
        try:
            while (frame := await read_frame(self._reader)) is not None:
                try:
                    self.peer.receive(frame)
                except (CodecError, Undeliverable) as exc:
                    if self._on_error is not None:
                        self._on_error(exc)
        finally:
            await self.close()  # the stream is over; let go of the socket too

    def let_go(self) -> bool:
        """Whether this side has released the socket. Diagnostic."""
        return self._writer.is_closing()

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        self.closed.set()
