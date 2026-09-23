"""N3, N4 -- starting the stream, and how much may be written.

Run with:  uv run --extra nidaqmx pytest tests/adapters/nidaqmx
"""

import numpy as np
import pytest
from test_configure import AO_TASK, BUFFER, CONFIG, DO_TASK, adapter, rig

from stacscope.adapters.simulated import VirtualClock


def block(n, value=1.0):
    return np.full((2, n), value), np.zeros((2, n), dtype=bool)


@pytest.fixture
def daq():
    return rig(space=BUFFER)


@pytest.fixture
async def output(daq):
    output = adapter(VirtualClock(), daq)
    await output.configure(CONFIG)
    await output.arm()
    return output


async def test_the_slave_starts_before_the_master(output, daq):
    """N3. The DO task takes its clock from the AO task, so it must already be
    waiting when the first edge arrives. Start them the other way round and the
    first samples clock out before the digital lines are listening -- the
    triggers would simply be late, by an amount nothing reports."""
    await output.write(*block(64))
    await output.go()

    assert daq.starts() == [DO_TASK, AO_TASK]
    # and nothing started itself on the way: an auto-starting write would have
    # started the master at the first block, before the slave existed.
    assert daq.task("ao").auto_started == [False]
    assert daq.task("do").auto_started == [False]


async def test_starting_with_nothing_written_starts_neither_task(output, daq):
    """N3, second half. With regeneration disabled there is nothing to play,
    and a started task with an empty buffer is an underflow waiting to be
    reported as though it were our refill loop's fault."""
    with pytest.raises(RuntimeError):
        await output.go()

    assert daq.starts() == []


async def test_space_is_what_the_driver_says_it_is(output, daq):
    """N4. The buffer belongs to the driver; we never model it ourselves."""
    assert output.space_available() == BUFFER

    daq.space = 129
    assert output.space_available() == 129


async def test_space_is_the_smaller_of_the_two_tasks(output, daq):
    """N4b. Each task has its own buffer, and on the physical board the
    digital task's is often fuller: its onboard FIFO holds 2047 samples to the
    analog task's 3276, so less of its buffer has drained into the board.
    Reporting only the analog task's space let the refill loop write into a
    full digital buffer, and the driver then waited inside the write for room
    -- 5-12 ms a time, with the event loop held, so every other actor starved
    for whole cycles and a MAIN update waited a cycle to be handled at all."""
    daq.do_space = 7

    assert output.space_available() == 7

    daq.space, daq.do_space = 7, BUFFER
    assert output.space_available() == 7


async def test_a_write_never_waits_in_the_driver(output, daq):
    """N4b, second half: what is written fits in *both* tasks, so the driver
    never has to wait for room. A write that would wait is refused before
    either task sees it, as N4 does for the analog task alone."""
    daq.do_space = 10

    with pytest.raises(ValueError):
        await output.write(*block(11))

    assert daq.task("ao").written == []
    assert daq.task("do").written == []


async def test_a_write_larger_than_the_space_never_reaches_the_driver(output, daq):
    """N4, second half. Refusing here keeps the failure ours and legible,
    rather than a driver error that has to be read back through error codes."""
    daq.space = 10

    with pytest.raises(ValueError):
        await output.write(*block(11))

    assert daq.task("ao").written == []
