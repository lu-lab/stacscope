"""N5-N9 -- the write path, what a driver error means, and stopping safely.

Run with:  uv run --extra nidaqmx pytest tests/adapters/nidaqmx
"""

import asyncio

import numpy as np
import pytest
from nidaqmx.error_codes import DAQmxErrors
from nidaqmx.errors import DaqError
from test_configure import AO_TASK, BUFFER, CONFIG, DO_TASK, RATE, adapter, rig

from stacscope.adapters.nidaqmx.signal_output import TAIL_PAD
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


async def test_a_chunk_cut_from_a_longer_segment_reaches_the_driver(output, daq):
    """A refill writes a segment a piece at a time, as columns sliced out of
    it, and such a slice is not contiguous in memory. The port takes any
    array; the driver's ctypes layer refuses that one. Found on Dev1-sim,
    the first time the real ``WaveformSource`` drove the adapter."""
    ao, do = ramp(32)

    await output.write(ao[:, 8:24], do[:, 8:24])

    np.testing.assert_array_equal(daq.task("ao").written[0], ao[:, 8:24])
    np.testing.assert_array_equal(daq.task("do").written[0], do[:, 8:24])


async def test_the_segment_label_never_reaches_the_driver(output, daq):
    """N5, second half. The label is ours, for diagnostics; DAQmx has no
    notion of where one segment ends and the next begins."""
    await output.write(*ramp(8), label="COOLDOWN")

    assert "COOLDOWN" not in repr(daq.log)


async def test_an_underflow_code_becomes_an_underflow_at_the_time_it_happened(playing, daq):
    """N6. The driver reports a gap by error code; the caller wants the one
    exception the port defines, carrying when the output ran dry. Samples
    generated over the rate is not that time: on the physical board the
    analog task's count stops at the last value read before the error -- 0
    if nothing had read it -- which would date a gap by when someone last
    looked. With regeneration off the board plays exactly what was written
    and then stops, so written over the rate is.

    Here the driver notices between the adapter asking for space and writing,
    so it is the write that reports it."""
    daq.ran_dry(UNDERFLOW, space=BUFFER)

    with pytest.raises(Underflow) as caught:
        await playing.write(*ramp(8))

    assert caught.value.at == 64 / RATE


async def test_the_regeneration_guard_firing_is_an_underflow_like_any_other(playing, daq):
    """N6. The FIFO and DAC underflow codes describe a board that could not
    fetch samples fast enough. This one -- "generation stopped to prevent
    regeneration of old samples" -- is what a task with regeneration disabled
    reports when the refill loop simply did not get there in time, and on this
    instrument that is the ordinary way of running dry. Missing it would report
    a fatal driver fault at the end of every acquisition that ended by running
    the buffer down, which is how one normally ends."""
    daq.ran_dry(REGEN_STOPPED, space=BUFFER)

    with pytest.raises(Underflow) as caught:
        await playing.write(*ramp(8))

    assert caught.value.at == 64 / RATE


async def test_a_board_that_ran_dry_says_so_when_asked_for_space(playing, daq):
    """H1's finding. Once the physical board has run dry its free space reads
    0 -- no error -- so a refill loop that only asked for space would see a
    full buffer and wait for room that never comes. The adapter asks the
    driver why there is no room; only ``is_task_done`` says."""
    daq.ran_dry(REGEN_STOPPED)

    with pytest.raises(Underflow) as caught:
        playing.space_available()

    assert caught.value.at == 64 / RATE


async def test_a_late_write_to_a_board_that_ran_dry_is_an_underflow(playing, daq):
    """H1. The write that arrives too late must say the output gapped, not
    that its samples do not fit: the second is a caller's mistake, and would
    send whoever reads it looking for one."""
    daq.ran_dry(REGEN_STOPPED)

    with pytest.raises(Underflow):
        await playing.write(*ramp(8))


async def test_a_full_buffer_is_not_mistaken_for_one_that_ran_dry(playing, daq):
    """H1, the other side. No room is the ordinary state of a well-fed buffer,
    many times a second; only the driver's own report makes it a gap."""
    daq.space = 0

    assert playing.space_available() == 0


async def test_a_full_buffer_that_is_still_playing_costs_no_is_task_done(playing, daq):
    """Found on Dev1: ``is_task_done`` takes 16 ms at the median and up to 37,
    where the generated count takes 0.03 -- and a well-fed buffer reads no
    room on most polls. At the smallest buffer the board allows there is about
    47 ms queued, so asking on every full poll spent most of the stall
    tolerance on the question. A count still moving is proof enough that the
    output has not run dry."""
    daq.space = 0
    ao = daq.task("ao").out_stream
    for played in (100, 200, 300):
        ao.played = played
        assert playing.space_available() == 0

    assert ("done?", daq.task("ao").name) not in daq.log


async def test_a_count_that_stops_moving_is_asked_why(playing, daq):
    """The other side: a board whose count stands still with no room may have
    run dry, and only ``is_task_done`` says so. Not every driver resets the
    count to 0 on running dry, so a count that merely stops must be enough."""
    daq.space = 0
    ao = daq.task("ao").out_stream
    ao.played = 100
    playing.space_available()
    daq.task("ao").dry = REGEN_STOPPED  # count left where it was

    with pytest.raises(Underflow):
        playing.space_available()


async def test_a_full_buffer_before_go_costs_no_is_task_done(output, daq):
    """Filling the buffer before ``go`` is the normal start, and leaves no
    room. Nothing is playing, so nothing can have run dry."""
    await output.write(*ramp(64))
    daq.space = 0

    assert output.space_available() == 0
    assert ("done?", daq.task("ao").name) not in daq.log


async def test_a_wait_for_space_ends_when_the_board_runs_dry(playing, daq, clock):
    """H1. On the physical board the space never comes back, so a wait that
    did not notice would hang the refill loop -- and with it the actor, which
    could then not even be told to stop between cycles."""
    daq.space = 0
    waiting = asyncio.create_task(playing.wait_for_space(128))
    await clock.advance(0.01)

    daq.ran_dry(REGEN_STOPPED)
    await clock.advance(0.01)

    with pytest.raises(Underflow):
        await waiting


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


async def test_draining_ends_when_the_board_reports_running_dry(playing, daq, clock):
    """H2's finding. On the physical board the analog task's sample count
    stops short of what was written -- 99 985 of 100 000, the last value
    read before the error -- while the digital
    task, on the same clock, counts every sample. So the analog task is done
    when the driver says it ran dry, which it says only once its buffer has
    emptied, and the digital task when its count is complete."""
    draining = asyncio.create_task(playing.drain())
    await clock.advance(0.01)
    assert not draining.done()

    daq.ran_dry(REGEN_STOPPED)
    daq.task("ao").out_stream.played = 49  # short, and not what ends it
    await clock.advance(0.01)

    await draining


async def test_draining_waits_for_the_digital_task_too(playing, daq, clock):
    """The analog task reporting that it ran dry says nothing of the digital
    task's last samples; a drain that stopped there would leave a line
    asserted."""
    draining = asyncio.create_task(playing.drain())
    daq.ran_dry(REGEN_STOPPED)
    daq.task("do").out_stream.played = 63
    await clock.advance(0.01)
    assert not draining.done()

    daq.task("do").out_stream.played = 64
    await clock.advance(0.01)

    await draining


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


def padding(daq, name):
    """What a drain wrote after the samples: every write past the first."""
    return daq.task(name).written[1:]


async def test_a_drain_holds_the_last_values_past_the_end(playing, daq, clock):
    """H10's finding. On the physical PCI-6733, output that has wrapped its
    host buffer and ends part-way through it never plays its last 64 digital
    samples, plus the end's offset from a multiple of 4 -- so a line is left
    wherever it was ~0.64 ms before the end, which on this instrument can be
    a laser still enabled. Padding of at least that many samples after them
    brought every one back. So a drain first writes TAIL_PAD more samples,
    each channel holding its last value, on both tasks: whatever the board
    drops is padding, and the lines end where the caller's last sample put
    them. (Chosen by the user, 2026-09-23, over requiring every COOLDOWN to
    end in a parked stretch.)"""
    ao, do = ramp(64)

    draining = asyncio.create_task(playing.drain())
    await clock.advance(0.01)

    [ao_pad], [do_pad] = padding(daq, "ao"), padding(daq, "do")
    assert ao_pad.shape == (2, TAIL_PAD) and do_pad.shape == (2, TAIL_PAD)
    assert (ao_pad == ao[:, -1:]).all()
    assert (do_pad == do[:, -1:]).all()
    assert TAIL_PAD >= 64 + 3
    draining.cancel()


async def test_a_drain_waits_for_room_for_the_padding(playing, daq, clock):
    """A drain usually follows the last refill, which filled the buffer. The
    padding waits for room, as any write does, rather than being refused or
    skipped: a full buffer frees TAIL_PAD samples in about a millisecond."""
    daq.space = 0
    daq.task("ao").out_stream.played = 1  # still playing: the count moves
    draining = asyncio.create_task(playing.drain())
    await clock.advance(0.01)
    assert padding(daq, "ao") == []

    daq.space = TAIL_PAD
    daq.task("ao").out_stream.played = 2
    await clock.advance(0.01)

    assert len(padding(daq, "ao")) == len(padding(daq, "do")) == 1
    draining.cancel()


async def test_a_drain_that_never_finds_room_still_gives_up(playing, daq, clock):
    """The wait for room is bounded by the drain's own deadline: a board that
    stopped consuming without saying so must end in the drain's TimeoutError,
    not a wait that holds the shutdown path for ever."""
    daq.space = 0
    draining = asyncio.create_task(playing.drain())

    await clock.advance(30.0)

    with pytest.raises(TimeoutError):
        await draining


async def test_a_drain_after_running_dry_pads_nothing(playing, daq):
    """Output that has already run dry has nothing left to protect, and the
    driver refuses writes. The drain ends as it did before padding."""
    daq.ran_dry(REGEN_STOPPED)

    await playing.drain()

    assert padding(daq, "ao") == padding(daq, "do") == []


async def test_a_drain_with_nothing_written_pads_nothing(output, daq):
    """There is no last value to hold, and nothing to protect."""
    await output.drain()

    assert daq.task("ao").written == daq.task("do").written == []


async def test_the_padding_is_not_counted_as_written(playing, daq, clock):
    """The drain still ends when the caller's samples have played -- the board
    may never play the padding, which is the point of it -- and a gap is
    still dated by the caller's samples."""
    draining = asyncio.create_task(playing.drain())
    await clock.advance(0.01)

    for name in ("ao", "do"):
        daq.task(name).out_stream.played = 64
    await clock.advance(0.01)

    await draining
    assert playing._written == 64


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


async def test_stopping_after_a_drain_accepts_the_ran_dry_report(playing, daq):
    """H1's finding: the physical board reports running dry once more, from
    the first ``stop``. After a drain that is the ending asked for, and a
    teardown that reported it would call every clean acquisition a failed
    release."""
    daq.ran_dry(REGEN_STOPPED)
    await playing.drain()

    await playing.stop()

    assert not daq.task("ao").started
    assert not daq.task("do").started


async def test_a_gap_nobody_was_told_about_is_reported_by_stop(playing, daq):
    """The legacy engine suppressed this code only on its STOP path:
    "Unexpected stops still propagate the error." If output ran dry and
    nothing has said so -- a stop from outside, mid-stream -- then stop is
    the last chance to, and silence would hide a gap in the acquisition.
    Both tasks are still stopped, and it is said once."""
    daq.ran_dry(REGEN_STOPPED)

    with pytest.raises(ExceptionGroup) as caught:
        await playing.stop()

    assert [type(e) for e in caught.value.exceptions] == [Underflow]
    assert caught.value.exceptions[0].at == 64 / RATE
    assert not daq.task("ao").started
    assert not daq.task("do").started
    await playing.stop()


async def test_a_gap_already_reported_is_not_reported_again_by_stop(playing, daq):
    """An ``Underflow`` kills the source, whose teardown then stops the
    output. Reporting the same gap again there would add "the output was not
    released cleanly" to the cause -- when the release was fine."""
    daq.ran_dry(REGEN_STOPPED)
    with pytest.raises(Underflow):
        playing.space_available()

    await playing.stop()


async def test_a_gap_reported_in_one_acquisition_does_not_silence_the_next(playing, daq):
    """The tasks play again after a stop without being reloaded (Step 3, H1),
    so one adapter serves many acquisitions, and what was said about the last
    one's gap says nothing about this one's."""
    daq.ran_dry(REGEN_STOPPED)
    with pytest.raises(Underflow):
        playing.space_available()
    await playing.stop()

    await playing.arm()
    daq.space = BUFFER
    await playing.write(*ramp(64))
    await playing.go()
    daq.ran_dry(REGEN_STOPPED)

    with pytest.raises(ExceptionGroup):
        await playing.stop()


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
