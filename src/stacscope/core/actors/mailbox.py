"""Priority mailbox: the receive side of every actor, whatever the transport."""

import asyncio
import itertools
from enum import IntEnum


class Priority(IntEnum):
    """Delivery priority. Lower value is received first."""

    URGENT = 0
    NORMAL = 1
    LOW = 2


class Mailbox:
    """Priority queue of messages, FIFO within a priority.

    Entries are ``(priority, seq, msg)``: the monotonic ``seq`` breaks ties so
    that messages themselves are never compared. Priority is enforced here, at
    the receiving actor, so it survives any transport.

    Not thread-safe: ``put`` must be called from the event loop's thread.
    Transports delivering from elsewhere hop onto the loop first.
    """

    def __init__(self) -> None:
        self._queue: asyncio.PriorityQueue[tuple[Priority, int, object]] = asyncio.PriorityQueue()
        self._seq = itertools.count()

    def put(self, msg: object, priority: Priority = Priority.NORMAL) -> None:
        self._queue.put_nowait((priority, next(self._seq), msg))

    async def get(self) -> object:
        _, _, msg = await self._queue.get()
        return msg
