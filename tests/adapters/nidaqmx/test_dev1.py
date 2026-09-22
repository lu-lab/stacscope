"""5a-2 -- the adapter against an NI MAX simulated PCI-6733 named Dev1.

What this proves that the fake cannot: that the *driver* accepts our call
sequence. A fake pins our calls against the API as documented; only a real
DAQmx accepts or rejects them. The simulated device runs a genuine sample
clock, so it also proves the board consumes at the configured rate and that
the digital task really does follow the analog one.

What it still cannot prove is underflow. A simulated device that runs dry
simply stalls -- no error code, and the next write is accepted -- so the
underflow path stays written but unverified until the instrument says
otherwise. Nothing here asserts anything about it.

These run on a real clock, so durations are short and counts are compared
with tolerance rather than exactly. Run with:

    uv run --extra nidaqmx pytest tests/adapters/nidaqmx
"""

import asyncio
import time

import numpy as np
import pytest

from stacscope.adapters.nidaqmx import NiSignalOutput
from stacscope.core.ports import Channel, MonotonicClock, OutputConfig

DEVICE = "Dev1"
RATE = 10_000.0
BUFFER = 4000
"""0.4 s of output: long enough to measure a rate against, short enough that a
failing test does not hold the suite."""

TOLERANCE = 0.15
"""Fraction by which a sample count may differ from what elapsed time predicts.
Sleep granularity on Windows is tens of milliseconds, which at this rate is
hundreds of samples; the assertion is that the board is clocking at the rate
asked for, not that Python woke up on time."""


def _dev1() -> bool:
    try:
        import nidaqmx.system

        return any(d.name == DEVICE for d in nidaqmx.system.System.local().devices)
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _dev1(), reason=f"no NI-DAQmx driver, or no simulated device named {DEVICE}"
)


def config(n_ao: int = 1, n_do: int = 1) -> OutputConfig:
    return OutputConfig(
        rate=RATE,
        ao=tuple(Channel(f"{DEVICE}/ao{i}", f"ao{i}") for i in range(n_ao)),
        do=tuple(Channel(f"{DEVICE}/port0/line{i}", f"do{i}") for i in range(n_do)),
        buffer_samples=BUFFER,
    )


def block(cfg: OutputConfig, n: int, value: float = 1.0):
    return (
        np.full((len(cfg.ao), n), value),
        np.zeros((len(cfg.do), n), dtype=bool),
    )


@pytest.fixture
async def make_output():
    """Build configured adapters, and close every one afterwards.

    Closing is not optional here: DAQmx refuses a second task of the same name
    while the first is open, so a leaked task fails the *next* test rather than
    this one.
    """
    outputs = []

    async def make(cfg: OutputConfig) -> NiSignalOutput:
        output = NiSignalOutput(MonotonicClock())
        outputs.append(output)
        await output.configure(cfg)
        await output.arm()
        return output

    yield make
    for output in outputs:
        await output.stop()
        await output.close()


def generated(output: NiSignalOutput, task) -> int:
    return task.out_stream.total_samp_per_chan_generated


async def until(predicate, timeout: float = 5.0) -> None:
    """Wait for a real-hardware condition, with a deadline of its own so a
    stalled board fails the test rather than the session's timeout."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "the board never reached the expected state"
        await asyncio.sleep(0.005)


async def test_the_driver_accepts_the_configuration(make_output):
    """The settings the whole design rests on are not merely set, they are
    accepted: a DO task clocked off the AO task's sample clock, regeneration
    refused, and a buffer of the depth we asked for. Any one of them could
    have been rejected by the driver for this board."""
    output = await make_output(config(n_ao=2, n_do=2))

    assert output.space_available() == BUFFER


async def test_space_is_readable_before_anything_starts(make_output):
    """``configure`` reserves and commits, so the caller may ask how much room
    it has before the first write. An uncommitted task answers this with an
    error instead, which would break the refill loop at its first question."""
    output = await make_output(config())

    assert output.space_available() == BUFFER


async def test_a_single_channel_write_is_accepted(make_output):
    """The port passes channels x n whatever the channel count, but DAQmx
    refuses a two-dimensional array for a one-channel task. One AO channel and
    one DO line is the smallest real configuration, so this is the shape most
    likely to be used first on the instrument."""
    cfg = config(n_ao=1, n_do=1)
    output = await make_output(cfg)

    await output.write(*block(cfg, 100))

    assert output.space_available() == BUFFER - 100


async def test_a_multi_channel_write_is_accepted(make_output):
    """The other shape, in the same test run: a fix for the single-channel
    case that broke the multi-channel one would pass unnoticed otherwise."""
    cfg = config(n_ao=2, n_do=2)
    output = await make_output(cfg)

    await output.write(*block(cfg, 100))

    assert output.space_available() == BUFFER - 100


async def test_the_board_consumes_at_the_configured_rate(make_output):
    """The point of hardware timing: samples leave at the board's rate, not at
    whatever rate software managed to write them."""
    cfg = config()
    output = await make_output(cfg)
    await output.write(*block(cfg, BUFFER))

    started = time.monotonic()
    await output.go()
    await asyncio.sleep(0.2)
    elapsed = time.monotonic() - started

    played = BUFFER - output.space_available()
    assert played == pytest.approx(elapsed * RATE, rel=TOLERANCE)


async def test_the_digital_task_keeps_pace_with_the_analog_one(make_output):
    """The digital lines take their clock from the AO task, so the two must
    generate the same number of samples. A DO task left on its own oscillator
    would still produce plausible output, drifting against the waveform by an
    amount nothing reports -- so it is asserted, not assumed."""
    cfg = config()
    output = await make_output(cfg)
    await output.write(*block(cfg, BUFFER))
    await output.go()

    await output.drain()

    assert generated(output, output._ao) == BUFFER
    assert generated(output, output._do) == BUFFER


async def test_the_stream_continues_across_a_refill(make_output):
    """What the refill loop does for a living: with regeneration off, output
    lasts exactly as long as the samples handed to it, and more may be added
    while it plays."""
    cfg = config()
    output = await make_output(cfg)
    await output.write(*block(cfg, BUFFER))
    await output.go()

    await until(lambda: output.space_available() >= BUFFER // 2)
    await output.write(*block(cfg, BUFFER // 2))
    await output.drain()

    assert generated(output, output._ao) == BUFFER + BUFFER // 2


async def test_drain_returns_when_the_last_sample_has_played(make_output):
    """``drain`` ends when output does, and is not a fixed wait: a continuous
    task is never "done" in the driver's sense, so anything that waits for
    that will wait forever."""
    cfg = config()
    output = await make_output(cfg)
    await output.write(*block(cfg, BUFFER))

    started = time.monotonic()
    await output.go()
    await output.drain()
    elapsed = time.monotonic() - started

    assert generated(output, output._ao) == BUFFER
    assert elapsed == pytest.approx(BUFFER / RATE, rel=TOLERANCE)


async def test_stop_is_safe_and_repeatable(make_output):
    """The never-wedge-the-driver invariant, against a driver that can
    actually refuse."""
    cfg = config()
    output = await make_output(cfg)
    await output.write(*block(cfg, BUFFER))
    await output.go()

    await output.stop()
    await output.stop()


async def test_a_closed_adapter_frees_its_task_names(make_output):
    """Two acquisitions in one process. DAQmx refuses a task whose name is
    already taken, so an adapter that does not release its tasks can only ever
    be built once -- and the board stays held for as long as the process
    lives."""
    first = await make_output(config())
    await first.close()

    second = await make_output(config())

    assert second.space_available() == BUFFER
