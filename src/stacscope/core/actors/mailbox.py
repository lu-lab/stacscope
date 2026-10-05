"""Priority mailbox: the receive side of every actor, whatever the transport."""

import asyncio
import heapq
import itertools
from collections.abc import Callable
from enum import IntEnum


class Priority(IntEnum):
    """Delivery priority. Lower value is received first."""

    URGENT = 0
    NORMAL = 1
    LOW = 2


class Mailbox:
    """Priority queue of messages, FIFO within a priority.

    A message may instead be put ``behind`` the queued messages that match a
    test: it is then taken immediately after the last of them to be taken,
    whatever its own priority. Priority is enforced here, at the receiving
    actor, so it survives any transport.

    Not thread-safe: ``put`` must be called from the event loop's thread.
    Transports delivering from elsewhere hop onto the loop first.
    """

    # Entries are ``(priority, seq, behind, msg)``. The monotonic ``seq``
    # breaks ties so that messages themselves are never compared. ``behind``
    # is 0 for an ordinary entry; one placed behind another copies that
    # entry's priority and seq and takes a fresh, larger ``behind``, so it
    # sorts after its anchor (and after anything placed behind it earlier)
    # and before everything that followed the anchor. No entry already
    # queued moves.

    def __init__(self) -> None:
        self._heap: list[tuple[Priority, int, int, object]] = []
        self._seq = itertools.count()
        self._behind = itertools.count(1)
        self._waiters: list[asyncio.Future[None]] = []

    def put(
        self,
        msg: object,
        priority: Priority = Priority.NORMAL,
        *,
        behind: Callable[[object], bool] | None = None,
    ) -> None:
        """Queue ``msg`` at ``priority``. With ``behind``, if any queued
        message passes that test, ``msg`` is placed immediately after the last
        of them to be taken, and is taken at that message's priority; if none
        does, ``priority`` applies as usual."""
        anchor = None
        if behind is not None:
            anchor = max((e for e in self._heap if behind(e[3])), key=lambda e: e[:3], default=None)
        if anchor is None:
            entry = (priority, next(self._seq), 0, msg)
        else:
            entry = (anchor[0], anchor[1], next(self._behind), msg)
        heapq.heappush(self._heap, entry)
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
                priority, _, _, msg = heapq.heappop(self._heap)
                return msg, priority
            waiter = asyncio.get_running_loop().create_future()
            self._waiters.append(waiter)
            await waiter
