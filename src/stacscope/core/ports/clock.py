"""Clock port: every timeout in the core goes through one, so tests run in virtual time."""

import asyncio
import time
from collections.abc import Awaitable, Callable
from typing import Protocol


class Scheduled(Protocol):
    """A call ``call_later`` has scheduled."""

    def cancel(self) -> None:
        """Make sure it never runs. Harmless once it has."""
        ...

    def cancelled(self) -> bool: ...


class Clock(Protocol):
    def now(self) -> float:
        """Seconds on a monotonic scale; only differences are meaningful."""
        ...

    async def sleep(self, seconds: float) -> None: ...

    async def wait_for[T](self, aw: Awaitable[T], timeout: float) -> T:
        """Await ``aw``; cancel it and raise ``TimeoutError`` after ``timeout`` seconds."""
        ...

    def call_later(self, seconds: float, callback: Callable[[], object]) -> Scheduled:
        """Run ``callback`` on the event loop after ``seconds``, unless it is
        cancelled first. Nothing waits for it."""
        ...


class MonotonicClock:
    """Wall-clock implementation over the event loop's own timers."""

    def now(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)

    async def wait_for[T](self, aw: Awaitable[T], timeout: float) -> T:
        return await asyncio.wait_for(aw, timeout)

    def call_later(self, seconds: float, callback: Callable[[], object]) -> Scheduled:
        return asyncio.get_running_loop().call_later(seconds, callback)
