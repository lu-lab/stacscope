"""Clock port: every timeout in the core goes through one, so tests run in virtual time."""

import asyncio
import time
from collections.abc import Awaitable
from typing import Protocol


class Clock(Protocol):
    def now(self) -> float:
        """Seconds on a monotonic scale; only differences are meaningful."""
        ...

    async def sleep(self, seconds: float) -> None: ...

    async def wait_for[T](self, aw: Awaitable[T], timeout: float) -> T:
        """Await ``aw``; cancel it and raise ``TimeoutError`` after ``timeout`` seconds."""
        ...


class MonotonicClock:
    """Wall-clock implementation over the event loop's own timers."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def wait_for[T](self, aw: Awaitable[T], timeout: float) -> T:
        return await asyncio.wait_for(aw, timeout)
