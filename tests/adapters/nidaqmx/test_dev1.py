"""5a-2 -- the adapter against an NI MAX simulated PCI-6733 named Dev1-sim,
driving the instrument's own tasks, imported into NI MAX as *_sim copies.

What this proves that the fake cannot: that the *driver* accepts our call
sequence, and that the tasks saved in MAX are what the instrument needs. A fake
pins our calls against the API as documented; only a real DAQmx accepts or
rejects them. The simulated device runs a genuine sample clock, so it also
proves the board consumes at the configured rate and that the digital task
really does follow the analog one.

What it still cannot prove is underflow. A simulated device that runs dry
simply stalls -- no error code, and the next write is accepted -- so the
underflow path stays written but unverified until the instrument says
otherwise. Nothing here asserts anything about it.

Setup, once per machine:

1. Create the device in NI MAX (Devices and Interfaces > Create New >
   Simulated NI-DAQmx Device > PCI-6733) and rename it Dev1-sim, so it can sit
   beside a physical Dev1.
2. Import the tasks: in NI MAX, File > Import, and choose
   tests/adapters/nidaqmx/DAQmxConfiguration_sim.txt. That creates
   SPIM_Analog_Task_sim and SPIM_Digital_Task_sim on Dev1-sim -- the
   instrument's SPIM_*_Task pair, moved to the simulated board.

The module skips unless Dev1-sim exists *and* is simulated, and both tasks
exist with every channel on it, so it never drives a real board.

These run on a real clock, so durations are short and counts are compared
with tolerance rather than exactly. Run with:

    uv run --extra nidaqmx pytest tests/adapters/nidaqmx
"""

import asyncio
import time

import numpy as np
import pytest
from nidaqmx.constants import DataTransferActiveTransferMode, RegenerationMode

from stacscope.adapters.nidaqmx import NiSignalOutput, read_output_config
from stacscope.core.actors import spawn
from stacscope.core.ports import Channel, MonotonicClock, OutputConfig
from stacscope.core.timing import WaveformSource

DEVICE = "Dev1-sim"
AO_TASK = "SPIM_Analog_Task_sim"
DO_TASK = "SPIM_Digital_Task_sim"
RATE = 100_000.0
BUFFER = 40_000
"""0.4 s of output: long enough to measure a rate against, short enough that a
failing test does not hold the suite."""

TOLERANCE = 0.15
"""Fraction by which a sample count may differ from what elapsed time predicts.
Sleep granularity on Windows is tens of milliseconds, which at this rate is
thousands of samples; the assertion is that the board is clocking at the rate
asked for, not that Python woke up on time."""

INSTRUMENT = OutputConfig(
    rate=RATE,
    ao=(
        Channel(f"{DEVICE}/ao0", "ETL"),
        Channel(f"{DEVICE}/ao1", "Scan1"),
        Channel(f"{DEVICE}/ao3", "Scan2"),
        Channel(f"{DEVICE}/ao2", "Scan3"),
        Channel(f"{DEVICE}/ao4", "Scan4"),
    ),
    do=(
        Channel(f"{DEVICE}/port0/line0", "HamamatsuCamTrigger"),
        Channel(f"{DEVICE}/port0/line1", "LaserEnable"),
        Channel(f"{DEVICE}/port0/line2", "ThorCamTrigger"),
    ),
    buffer_samples=BUFFER,
)
"""What the instrument's tasks hold, written out rather than read from MAX.
Every test configures with this, so the adapter's own check refuses a *_sim
task edited to reach any other device before anything is committed."""


def _unsafe() -> str | None:
    """Why this suite must not run here, or None if it may."""
    # The device name alone is not enough: a board renamed in MAX, or a
    # physical device given this name, would have every test below assert real
    # analog and digital output. And the tasks are checked as well as the
    # device, since a *_sim task can be edited to point at the physical Dev1.
    # Runs against real hardware belong in tests/hardware/, run by the user --
    # never here.
    try:
        import nidaqmx.system

        system = nidaqmx.system.System.local()
        if not any(d.name == DEVICE and d.is_simulated for d in system.devices):
            return f"{DEVICE} is absent or is not simulated (this suite never drives a real board)"
        missing = [name for name in (AO_TASK, DO_TASK) if name not in system.tasks]
        if missing:
            return (
                f"NI MAX has no {' or '.join(missing)}: import "
                "tests/adapters/nidaqmx/DAQmxConfiguration_sim.txt with File > Import in NI MAX"
            )
        for name in (AO_TASK, DO_TASK):
            task = system.tasks[name].load()  # loading neither commits nor outputs
            try:
                devices = {d.name for d in task.devices}
            finally:
                task.close()
            if devices != {DEVICE}:
                return f"{name} uses {sorted(devices)}, not only {DEVICE}: re-import it"
    except Exception as exc:
        return f"no usable NI-DAQmx driver: {exc!r}"
    return None


UNSAFE = _unsafe()
pytestmark = pytest.mark.skipif(UNSAFE is not None, reason=UNSAFE or "")


def block(n: int, value: float = 1.0):
    return (
        np.full((len(INSTRUMENT.ao), n), value),
        np.zeros((len(INSTRUMENT.do), n), dtype=bool),
    )


def adapter() -> NiSignalOutput:
    return NiSignalOutput(MonotonicClock(), ao_task=AO_TASK, do_task=DO_TASK)


@pytest.fixture
async def make_output():
    """Build configured adapters, and close every one afterwards.

    Closing is not optional here: DAQmx refuses to load a task that is already
    loaded, so a leaked task fails the *next* test rather than this one.
    """
    outputs = []

    async def make() -> NiSignalOutput:
        output = adapter()
        outputs.append(output)
        await output.configure(INSTRUMENT)
        await output.arm()
        return output

    yield make
    for output in outputs:
        await output.stop()
        await output.close()


def generated(task) -> int:
    return task.out_stream.total_samp_per_chan_generated


async def until(predicate, timeout: float = 5.0) -> None:
    """Wait for a real-hardware condition, with a deadline of its own so a
    stalled board fails the test rather than the session's timeout."""
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "the board never reached the expected state"
        await asyncio.sleep(0.005)


def test_the_saved_tasks_are_the_instruments():
    """N18. The imported tasks hold the instrument's channel map, read back
    through the driver: five analog channels in MAX's order -- with Scan2 on
    ao3 and Scan3 on ao2, the transposition the rig is wired with -- three
    digital lines, and 100 kS/s. If ``read_output_config`` reordered,
    relabelled or misread anything, a rig config built from it would drive
    the wrong mirror."""
    assert read_output_config(AO_TASK, DO_TASK, buffer_samples=BUFFER) == INSTRUMENT


async def test_the_driver_accepts_the_configuration(make_output):
    """N18, and N15 against a real driver. The settings the refill design
    rests on arrive from MAX intact -- the digital task clocked off the analog
    task's sample clock, regeneration refused on both -- and the two settings
    ``configure`` makes are accepted for this board: DMA transfer and a buffer
    of the depth asked for. The analog task comes out of MAX polled, so the DMA
    assertion is not vacuous."""
    output = await make_output()
    ao, do = output._ao, output._do

    assert do.timing.samp_clk_src == f"/{DEVICE}/ao/SampleClock"
    for task in (ao, do):
        assert task.out_stream.regen_mode is RegenerationMode.DONT_ALLOW_REGENERATION
        assert task.out_stream.output_buf_size == BUFFER
    assert ao.ao_channels.all.ao_data_xfer_mech is DataTransferActiveTransferMode.DMA
    assert do.do_channels.all.do_data_xfer_mech is DataTransferActiveTransferMode.DMA


async def test_space_is_readable_before_anything_starts(make_output):
    """``configure`` reserves and commits, so the caller may ask how much room
    it has before the first write. An uncommitted task answers this with an
    error instead, which would break the refill loop at its first question."""
    output = await make_output()

    assert output.space_available() == BUFFER


async def test_a_write_is_accepted(make_output):
    """Five analog channels and three digital lines, as channels x n. The
    one-channel shape, which DAQmx wants flattened, is no longer reachable
    here -- the tasks come from MAX -- and is covered against the fake."""
    output = await make_output()

    await output.write(*block(100))

    assert output.space_available() == BUFFER - 100


async def test_the_board_consumes_at_the_configured_rate(make_output):
    """The point of hardware timing: samples leave at the board's rate, not at
    whatever rate software managed to write them."""
    output = await make_output()
    await output.write(*block(BUFFER))

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
    output = await make_output()
    await output.write(*block(BUFFER))
    await output.go()

    await output.drain()

    assert generated(output._ao) == BUFFER
    assert generated(output._do) == BUFFER


async def test_the_stream_continues_across_a_refill(make_output):
    """What the refill loop does for a living: with regeneration off, output
    lasts exactly as long as the samples handed to it, and more may be added
    while it plays."""
    output = await make_output()
    await output.write(*block(BUFFER))
    await output.go()

    await until(lambda: output.space_available() >= BUFFER // 2)
    await output.write(*block(BUFFER // 2))
    await output.drain()

    assert generated(output._ao) == BUFFER + BUFFER // 2


async def test_drain_returns_when_the_last_sample_has_played(make_output):
    """``drain`` ends when output does, and is not a fixed wait: a continuous
    task is never "done" in the driver's sense, so anything that waits for
    that will wait forever."""
    output = await make_output()
    await output.write(*block(BUFFER))

    started = time.monotonic()
    await output.go()
    await output.drain()
    elapsed = time.monotonic() - started

    assert generated(output._ao) == BUFFER
    assert elapsed == pytest.approx(BUFFER / RATE, rel=TOLERANCE)


async def test_stop_is_safe_and_repeatable(make_output):
    """The never-wedge-the-driver invariant, against a driver that can
    actually refuse."""
    output = await make_output()
    await output.write(*block(BUFFER))
    await output.go()

    await output.stop()
    await output.stop()


async def test_a_closed_adapter_gives_its_tasks_back(make_output):
    """Two acquisitions in one process. DAQmx refuses to load a task that is
    already loaded, so an adapter that does not release its tasks can only
    ever be built once -- and the board stays held for as long as the process
    lives."""
    first = await make_output()
    await first.close()

    second = await make_output()

    assert second.space_available() == BUFFER


async def test_the_timing_source_releases_the_board_when_it_ends(make_output):
    """N13. Nobody closes this adapter but the core: a ``WaveformSource`` is
    started and stopped, and its teardown alone must give both tasks back. If
    it did not, the board would stay reserved until the process exited, and
    the next adapter would be refused the tasks."""
    source = WaveformSource(adapter(), INSTRUMENT)
    spawn(source)
    await asyncio.wait_for(source.started.wait(), timeout=5.0)

    source.stop()
    await asyncio.wait_for(source.stopped.wait(), timeout=5.0)
    assert source.exit_error is None

    after = await make_output()
    assert after.space_available() == BUFFER
