"""Priority mailbox: the receive side of every actor, whatever the transport."""

import asyncio
import heapq
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
        self._heap: list[tuple[Priority, int, object]] = []
        self._seq = itertools.count()
        self._waiters: list[asyncio.Future[None]] = []

    def put(self, msg: object, priority: Priority = Priority.NORMAL) -> None:
        heapq.heappush(self._heap, (priority, next(self._seq), msg))
        waiters, self._waiters = self._waiters, []
        for waiter in waiters:
            if not waiter.done():
                waiter.set_result(None)

    async def get(self) -> object:
        msg, _ = await self.take()
        return msg

    async def take(self, above: Priority | None = None) -> tuple[object, Priority]:
        """The next message and its priority, waiting for one if need be.
        With ``above``, only a message more urgent than that priority is
        taken; the rest stay queued, in order."""
        while True:
            # Nothing is awaited between the check and the pop, so a getter
            # cancelled while waiting never takes a message with it.
            if self._heap and (above is None or self._heap[0][0] < above):
                priority, _, msg = heapq.heappop(self._heap)
                return msg, priority
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            await waiter
