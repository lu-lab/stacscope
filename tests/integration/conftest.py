"""Shared ground for the real-clock process tests.

Nothing here ever waits without a deadline. The failures under test are
processes that will not answer, and a suite that waits for one is no better
than the hang it is testing for.
"""

import asyncio

import pytest

from stacscope.core.actors import Actor, ChildExited, HeartbeatPolicy, spawn
from stacscope.core.transport import ProcessHost

FAST_HEARTBEAT = HeartbeatPolicy(interval=0.05, timeout=0.3)
"""Tens of milliseconds, so a death by silence is found in well under a second."""

PATIENT_HEARTBEAT = HeartbeatPolicy(interval=5.0, timeout=5.0)
"""Long enough that anything found quickly was not found by heartbeat."""


class Parent(Actor):
    """Stands in for whatever supervises a device. Keeps its children's deaths."""

    def __init__(self) -> None:
        super().__init__()
        self.exits: list[ChildExited] = []

    async def handle(self, msg):
        if isinstance(msg, ChildExited):
            self.exits.append(msg)


@pytest.fixture
async def host():
    host = ProcessHost(connect_timeout=15.0)
    await host.start()
    try:
        yield host
    finally:
        await host.close()  # kills whatever a failed assertion left running


@pytest.fixture
async def parent():
    actor = Parent()
    spawn(actor, name="engine")
    await asyncio.sleep(0)
    return actor


async def until(predicate, deadline: float = 5.0) -> bool:
    """Wait for ``predicate``, up to ``deadline``. Never longer."""
    loop = asyncio.get_running_loop()
    expires = loop.time() + deadline
    while loop.time() < expires:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()
