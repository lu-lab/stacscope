"""Shared values and helpers for the real-clock process tests.

Why this is not in ``conftest.py``: a test module importing ``from conftest
import ...`` resolves that bare name through ``sys.path``, and this suite has
several ``conftest.py`` files. Only one can hold the name at a time, so such an
import binds to whichever was imported first -- which depends on how pytest was
invoked. Shared values therefore live in a uniquely named module, and
``conftest.py`` is left to do only what pytest asks of it: define fixtures.

Nothing here ever waits without a deadline. The failures under test are
processes that will not answer, and a suite that waits for one is no better
than the hang it is testing for.
"""

import asyncio

from stacscope.core.actors import HeartbeatPolicy

FAST_HEARTBEAT = HeartbeatPolicy(interval=0.05, timeout=0.3)
"""Tens of milliseconds, so a death by silence is found in well under a second."""

PATIENT_HEARTBEAT = HeartbeatPolicy(interval=5.0, timeout=5.0)
"""Long enough that anything found quickly was not found by heartbeat."""


async def until(predicate, deadline: float = 5.0) -> bool:
    """Wait for ``predicate``, up to ``deadline``. Never longer."""
    loop = asyncio.get_running_loop()
    expires = loop.time() + deadline
    while loop.time() < expires:
        if predicate():
            return True
        await asyncio.sleep(0.005)
    return predicate()
