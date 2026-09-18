"""SignalOutput port contract. Every adapter must pass this suite.

Modelled on NI-DAQmx continuous output with regeneration disabled: samples
written are played once, in order, at the configured rate; the software must
keep the buffer fed, and running dry mid-stream is an error (W3 at the port
level), not a silent gap. Only ``drain`` makes running dry the expected end.

The simulated adapter runs on a virtual clock. A hardware adapter supplies its
own ``rig`` fixture with a real clock and short durations.
"""

import asyncio

import numpy as np
import pytest

from stacscope.adapters.simulated import SimulatedSignalOutput, VirtualClock
from stacscope.core.ports import Channel, OutputConfig, Underflow

RATE = 8.0  # samples per second: binary-exact virtual times
CONFIG = OutputConfig(
    rate=RATE,
    ao=(Channel("Dev1/ao0", "etl"),),
    do=(Channel("Dev1/port0/line0", "camera_trigger"),),
    buffer_samples=16,
)


def block(n, value=1.0):
    return np.full((1, n), value), np.zeros((1, n), dtype=bool)


@pytest.fixture(params=[SimulatedSignalOutput], ids=lambda cls: cls.__name__)
def rig(request):
    clock = VirtualClock()
    return request.param(clock), clock


async def _configured(rig):
    output, clock = rig
    await output.configure(CONFIG)
    await output.arm()
    return output, clock


async def test_space_is_the_whole_buffer_once_configured(rig):
    output, _ = await _configured(rig)

    assert output.space_available() == 16


async def test_writes_consume_space_and_may_not_exceed_it(rig):
    output, _ = await _configured(rig)
    await output.write(*block(10))

    assert output.space_available() == 6
    with pytest.raises(ValueError):
        await output.write(*block(7))


async def test_output_consumes_samples_at_the_configured_rate(rig):
    output, clock = await _configured(rig)
    await output.write(*block(16))
    await output.go()

    await clock.advance(0.5)

    assert output.space_available() == 4


async def test_wait_for_space_returns_exactly_when_it_frees(rig):
    output, clock = await _configured(rig)
    await output.write(*block(16))
    await output.go()

    waiting = asyncio.create_task(output.wait_for_space(8))
    await clock.advance(0.75)
    assert not waiting.done()

    await clock.advance(0.25)
    assert waiting.done()
    assert output.space_available() == 8


async def test_starting_with_nothing_written_is_an_error(rig):
    output, _ = await _configured(rig)

    with pytest.raises(RuntimeError):
        await output.go()


async def test_waiting_on_a_full_buffer_that_was_never_started_is_an_error(rig):
    output, _ = await _configured(rig)
    await output.write(*block(16))

    with pytest.raises(RuntimeError):
        await output.wait_for_space(1)


async def test_running_dry_mid_stream_surfaces_as_underflow_on_the_next_call(rig):
    output, clock = await _configured(rig)
    await output.write(*block(8))
    await output.go()

    await clock.advance(1.5)  # ran dry at 1.0; nothing refilled it

    with pytest.raises(Underflow) as caught:
        await output.write(*block(1))
    assert caught.value.at == 1.0


async def test_drain_makes_running_dry_the_expected_end(rig):
    output, clock = await _configured(rig)
    await output.write(*block(12))
    await output.go()

    draining = asyncio.create_task(output.drain())
    await clock.advance(1.25)
    assert not draining.done()

    await clock.advance(0.5)  # well past the last sample at 1.5: no underflow
    await draining
    await output.stop()


async def test_stop_is_safe_whatever_the_state(rig):
    output, _ = await _configured(rig)
    await output.stop()
    await output.stop()
