"""Fixtures for the real-clock process tests.

Shared constants and helpers live in ``process_support.py``, not here -- see
the note at the top of that file for why a bare ``from conftest import ...`` is
not safe in a suite with more than one ``conftest.py``.
"""

import asyncio

import pytest

from stacscope.core.actors import Actor, ChildExited, spawn
from stacscope.core.transport import ProcessHost


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
