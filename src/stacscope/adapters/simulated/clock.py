"""Virtual clock: time moves only when a test advances it."""

import asyncio
import heapq
import itertools
from collections.abc import Awaitable

# Loop iterations granted after each timer fires, so woken tasks can run to
# their next await (and possibly register new timers) before time moves on.
_SETTLE_YIELDS = 50


class VirtualClock:
    """Implements the ``Clock`` port. Deadlines fire only inside ``advance``."""

    def __init__(self, start: float = 0.0) -> None:
        self._now = start
        self._timers: list[tuple[float, int, asyncio.Future[None]]] = []
        self._seq = itertools.count()

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        await self._timer(seconds)

    async def wait_for[T](self, aw: Awaitable[T], timeout: float) -> T:
        task = asyncio.ensure_future(aw)
        timer = self._timer(timeout)
        await asyncio.wait({task, timer}, return_when=asyncio.FIRST_COMPLETED)
        if task.done():
            timer.cancel()
            return task.result()
        task.cancel()
        raise TimeoutError

    async def advance(self, seconds: float) -> None:
        """Move time forward, firing every deadline reached, in order."""
        await _settle()
        target = self._now + seconds
        while self._timers and self._timers[0][0] <= target:
            deadline, _, fut = heapq.heappop(self._timers)
            self._now = deadline
            if not fut.done():
                fut.set_result(None)
            await _settle()
        self._now = target
        await _settle()

    def _timer(self, seconds: float) -> asyncio.Future[None]:
        fut = asyncio.get_running_loop().create_future()
        heapq.heappush(self._timers, (self._now + seconds, next(self._seq), fut))
        return fut


async def _settle() -> None:
    for _ in range(_SETTLE_YIELDS):
        await asyncio.sleep(0)
