"""N5-N9 -- the write path, what a driver error means, and stopping safely.

Run with:  uv run --extra nidaqmx pytest tests/adapters/nidaqmx
"""

import asyncio

import numpy as np
import pytest
from nidaqmx.error_codes import DAQmxErrors
from nidaqmx.errors import DaqError
from test_configure import AO_TASK, BUFFER, CONFIG, DO_TASK, RATE, adapter, rig

from stacscope.adapters.simulated import VirtualClock
from stacscope.core.ports import Underflow

UNDERFLOW = DaqError("output buffer underflow", DAQmxErrors.OUTPUT_FIFO_UNDERFLOW)
REGEN_STOPPED = DaqError(
    "generation stopped to prevent regeneration of old samples",
    DAQmxErrors.GEN_STOPPED_TO_PREVENT_REGEN_OF_OLD_SAMPLES,
)
OVERHEATED = DaqError("device too hot", DAQmxErrors.DEVICE_SHUT_DOWN_DUE_TO_HIGH_TEMP)


def ramp(n):
    """Distinguishable per channel and per sample, so a transposed or
    reordered array cannot pass unnoticed."""
    ao = np.arange(2 * n, dtype=float).reshape(2, n)
    do = np.zeros((2, n), dtype=bool)
    do[0, ::2] = True
    return ao, do


@pytest.fixture
def daq():
    return rig(space=BUFFER)


@pytest.fixture
def clock():
    return VirtualClock()


@pytest.fixture
async def output(daq, clock):
    output = adapter(clock, daq)
    await output.configure(CONFIG)
    await output.arm()
    return output


@pytest.fixture
async def playing(output, daq):
    await output.write(*ramp(64))
    await output.go()
    return output


async def test_the_samples_reach_the_driver_exactly_as_given(output, daq):
    """N5. Channels x samples, in the caller's order, untouched."""
    ao, do = ramp(32)
    await output.write(ao, do, label="WARMUP")

    np.testing.assert_array_equal(daq.task("ao").written[0], ao)
    np.testing.assert_array_equal(daq.task("do").written[0], do)


async def test_the_segment_label_never_reaches_the_driver(output, daq):
    """N5, second half. The label is ours, for diagnostics; DAQmx has no
    notion of where one segment ends and the next begins."""
    await output.write(*ramp(8), label="COOLDOWN")

    assert "COOLDOWN" not in repr(daq.log)


async def test_an_underflow_code_becomes_an_underflow_at_the_time_it_happened(playing, daq):
    """N6. The driver reports a gap by error code; the caller wants the one
    exception the port defines, carrying when the output ran dry. Samples
    generated over the rate is that time, and it is the only clock on offer."""
    daq.task("ao").out_stream.total_samp_per_chan_generated = 50_000
    daq.task("ao").fail = UNDERFLOW

    with pytest.raises(Underflow) as caught:
        await playing.write(*ramp(8))

    assert caught.value.at == 50_000 / RATE


async def test_the_regeneration_guard_firing_is_an_underflow_like_any_other(playing, daq):
    """N6. The FIFO and DAC underflow codes describe a board that could not
    fetch samples fast enough. This one -- "generation stopped to prevent
    regeneration of old samples" -- is what a task with regeneration disabled
    reports when the refill loop simply did not get there in time, and on this
    instrument that is the ordinary way of running dry. Missing it would report
    a fatal driver fault at the end of every acquisition that ended by running
    the buffer down, which is how one normally ends."""
    daq.task("ao").out_stream.total_samp_per_chan_generated = 50_000
    daq.task("ao").fail = REGEN_STOPPED

    with pytest.raises(Underflow) as caught:
        await playing.write(*ramp(8))

    assert caught.value.at == 50_000 / RATE


async def test_the_regeneration_guard_firing_during_a_drain_is_the_expected_end(playing, daq):
    """N8. Same code, other side of the acquisition: once nothing more is
    coming, the buffer emptying is what the drain was waiting for."""
    for name in ("ao", "do"):
        daq.task(name).out_stream.fail = REGEN_STOPPED

    await playing.drain()  # no Underflow, no DaqError


async def test_an_underflow_also_surfaces_when_asking_for_space(playing, daq):
    """N6. The port says either call may report it, and which one notices
    first depends only on where the refill loop happened to be."""
    daq.task("ao").fail = UNDERFLOW

    with pytest.raises(Underflow):
        playing.space_available()


async def test_another_driver_error_is_not_dressed_up_as_an_underflow(playing, daq):
    """N6, second half. An underflow means one specific thing -- the refill
    loop fell behind -- and a caller may respond by refilling harder. Reporting
    anything else as one sends it the wrong way and hides the real fault behind
    a plausible story. An overheating board is not a scheduling problem."""
    daq.task("ao").fail = OVERHEATED

    with pytest.raises(DaqError) as caught:
        await playing.write(*ramp(8))

    assert caught.value.error_code == DAQmxErrors.DEVICE_SHUT_DOWN_DUE_TO_HIGH_TEMP


async def test_waiting_for_space_returns_once_the_driver_reports_it(playing, daq, clock):
    """N7. Polled through the injected clock, so the wait runs in virtual time
    for a caller that is testing, and real time for one that is not."""
    daq.space = 0
    waiting = asyncio.create_task(playing.wait_for_space(128))
    await clock.advance(0.05)
    assert not waiting.done()

    daq.space = 256
    await clock.advance(0.05)
    await waiting


async def test_waiting_on_a_full_buffer_that_never_started_is_an_error(output, daq):
    """N7, second half. Nothing is consuming, so the wait could never end.
    Saying so at once beats hanging on a promise that cannot be kept."""
    daq.space = 0

    with pytest.raises(RuntimeError):
        await output.wait_for_space(1)


async def test_draining_waits_for_what_was_written_to_play(playing, daq, clock):
    """N8. Draining is the wait for the samples we wrote to have been
    generated, and nothing shorter. What it must not be is a fixed pause or an
    immediate return: either would cut the tail off the last segment, and the
    last segment is the one that parks the scanners and drops the laser."""
    draining = asyncio.create_task(playing.drain())
    await clock.advance(0.01)
    assert not draining.done()

    for name in ("ao", "do"):
        daq.task(name).out_stream.played = 64
    await clock.advance(0.01)

    await draining


async def test_running_dry_while_draining_is_the_expected_end(playing, daq):
    """N8. Draining says no more samples are coming, so the buffer emptying is
    the point of the exercise rather than a fault to report."""
    for name in ("ao", "do"):
        daq.task(name).out_stream.fail = UNDERFLOW

    await playing.drain()  # no Underflow

    # and the digital task was drained too. Both share the AO sample clock, so
    # one of them running dry says nothing about the other having finished.
    assert ("generated", DO_TASK) in daq.log


async def test_draining_gives_up_rather_than_waiting_for_ever(playing, daq, clock):
    """N8. Nothing is playing and nothing ever will, so the wait can only end
    one way. A drain that hangs holds the whole shutdown path behind it."""
    draining = asyncio.create_task(playing.drain())

    await clock.advance(30.0)

    with pytest.raises(TimeoutError):
        await draining


async def test_draining_still_reports_a_fault_that_is_not_the_ending(playing, daq):
    """N8, second half. Only running dry is expected during a drain."""
    daq.task("ao").out_stream.fail = OVERHEATED

    with pytest.raises(DaqError):
        await playing.drain()


async def test_stop_is_safe_in_any_state_and_any_number_of_times(output, daq):
    """N9. Whatever happened, the output stops. This is called from the
    source's teardown, which runs however the acquisition ended."""
    await output.stop()  # never started
    await output.stop()

    await output.write(*ramp(8))
    await output.go()
    await output.stop()
    await output.stop()

    assert not daq.task("ao").started
    assert not daq.task("do").started


async def test_stopping_a_task_the_driver_is_unhappy_about_still_stops_the_other(output, daq):
    """N9, second half. A stop that gives up halfway leaves half the rig
    running, and on this instrument that means lines still asserted. So both
    tasks are attempted -- and the refusal is still reported afterwards,
    because a board that would not stop is exactly the thing not to discover
    later."""
    await output.write(*ramp(8))
    await output.go()
    daq.task("ao").fail = OVERHEATED

    with pytest.raises(ExceptionGroup) as caught:
        await output.stop()

    assert not daq.task("do").started  # attempted anyway, and it worked
    assert [type(e) for e in caught.value.exceptions] == [DaqError]


async def test_close_releases_both_tasks_and_may_be_repeated(output, daq, clock):
    """N13. ``close`` is the port's now, called by the source's teardown, so
    it meets the contract's terms: safe before anything was configured, safe
    twice, and both tasks released."""
    await adapter(clock, rig()).close()  # never configured

    await output.close()
    await output.close()

    assert daq.task("ao").closed
    assert daq.task("do").closed


@pytest.mark.parametrize(
    "use",
    [
        lambda output: output.configure(CONFIG),
        lambda output: output.arm(),
        lambda output: output.write(*ramp(1)),
        lambda output: output.go(),
        lambda output: output.wait_for_space(1),
        lambda output: output.drain(),
    ],
    ids=["configure", "arm", "write", "go", "wait_for_space", "drain"],
)
async def test_a_closed_adapter_refuses_to_be_used(output, daq, use):
    """N13, the contract's other half. Without it a closed adapter fails with
    whatever a missing task happens to raise -- or, for ``configure``, quietly
    reserves the board again after its owner released it."""
    await output.write(*ramp(4))
    await output.close()
    tasks = len(daq.loaded)

    with pytest.raises(RuntimeError):
        await use(output)
    with pytest.raises(RuntimeError):
        output.space_available()
    assert len(daq.loaded) == tasks, "a closed adapter loaded a task"


async def test_stop_after_close_is_still_safe(output):
    """N13. ``stop`` is safe in any state, and closed is one."""
    await output.close()
    await output.stop()
