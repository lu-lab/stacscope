"""5a-3 (Step 3, H1-H4) -- the NI adapter on a physical PCI-6733.

RUN BY THE USER ONLY. This suite starts real analog and digital output on the
board it is pointed at. Every sample it writes is 0 V on the analog channels
and low on the digital lines, but the board is still clocking out, so run it
only where that is safe -- on the development box, with nothing wired.

What it answers, from the architecture plan's Step 3:

  H1  Whether the board reports running dry the way the adapter expects, and
      whether the adapter can still read how far output got while handling it.
  H2  Draining by polling the sample count (the adapter) against the driver's
      wait_until_done (the legacy engine): where each thinks output ended.
  H3  Refilling by polling for space (the adapter) against one blocking write
      per segment (the legacy engine): margin to running dry at 100 kS/s, and
      what each costs in CPU.
  H4  What the saved tasks load with for data transfer, and whether setting
      DMA changes anything measurable.

Most of these are measurements rather than verdicts. Every test adds its
numbers to a "hardware findings" section at the end of the run; paste that
section back. A test fails only where the adapter's own contract is broken.

The suite saves its own tasks to NI MAX (stacscope_test_hw_ao and _do) and
deletes them afterwards; the instrument's SPIM_* tasks are never loaded.

It runs only against the device named in STACSCOPE_HARDWARE, and skips
without it. Pointed at a simulated device, it rehearses: the tests a simulated
board cannot answer (it stalls rather than running dry) skip themselves.

    PowerShell:  $env:STACSCOPE_HARDWARE = "Dev1"
                 uv run --extra nidaqmx pytest tests/hardware/nidaqmx
                 Remove-Item Env:STACSCOPE_HARDWARE

    Git Bash:    STACSCOPE_HARDWARE=Dev1 uv run --extra nidaqmx pytest tests/hardware/nidaqmx

Takes about a minute.
"""

import asyncio
import inspect
import os
import time

import numpy as np
import pytest
from nidaqmx.constants import DataTransferActiveTransferMode, TaskMode
from nidaqmx.errors import DaqError
from saved_tasks import PREFIX, saved

from stacscope.adapters.nidaqmx import NiSignalOutput
from stacscope.adapters.nidaqmx.signal_output import UNDERFLOW_CODES
from stacscope.core.ports import Channel, MonotonicClock, OutputConfig, Underflow

DEVICE = os.environ.get("STACSCOPE_HARDWARE", "")
AO_TASK = f"{PREFIX}hw_ao"
DO_TASK = f"{PREFIX}hw_do"
RATE = 100_000.0

BUFFER = 100_000
"""1 s of output. The instrument's real depth is still an open item (the plan's
"confirm the actual DAQmx output buffer depth"), so H3 also runs at TIGHT."""

TIGHT = 10_000
"""100 ms: where a slow refill would show up first."""

SEGMENT = 2_000
"""20 ms per refill, the cadence of a short segment."""

STREAM_SECONDS = 4.0
TOLERANCE = 0.15
"""As in the simulated suite: Windows sleep granularity, not board accuracy."""

pytestmark = pytest.mark.timeout(60)


def config(buffer: int = BUFFER) -> OutputConfig:
    """The instrument's shape -- five analog channels, three digital lines --
    on the board under test."""
    return OutputConfig(
        rate=RATE,
        ao=tuple(Channel(f"{DEVICE}/ao{i}", f"analog{i}") for i in range(5)),
        do=tuple(Channel(f"{DEVICE}/port0/line{i}", f"trigger{i}") for i in range(3)),
        buffer_samples=buffer,
    )


def _board():
    """(why this suite must not run, whether the board is simulated)."""
    if not DEVICE:
        return "set STACSCOPE_HARDWARE to the board's NI MAX name (e.g. Dev1) to run this suite", False
    try:
        import nidaqmx.system

        devices = {d.name: d for d in nidaqmx.system.System.local().devices}
    except Exception as exc:
        return f"no usable NI-DAQmx driver: {exc!r}", False
    if DEVICE not in devices:
        return f"NI MAX has no device named {DEVICE!r}", False
    return None, devices[DEVICE].is_simulated


UNSAFE, SIMULATED = _board()
pytestmark = [pytestmark, pytest.mark.skipif(UNSAFE is not None, reason=UNSAFE or "")]
physical_only = pytest.mark.skipif(
    SIMULATED, reason="a simulated board stalls when it runs dry instead of reporting it"
)


@pytest.fixture(scope="module", autouse=True)
def tasks():
    with saved(config(), ao=AO_TASK, do=DO_TASK):
        yield


@pytest.fixture
async def make_output():
    """Configured, armed adapters, each stopped and closed afterwards."""
    outputs = []

    async def make(buffer: int = BUFFER) -> NiSignalOutput:
        output = NiSignalOutput(MonotonicClock(), ao_task=AO_TASK, do_task=DO_TASK)
        outputs.append(output)
        await output.configure(config(buffer))
        await output.arm()
        return output

    yield make
    for output in outputs:
        await output.stop()
        await output.close()


def zeros(n: int):
    return np.zeros((5, n)), np.zeros((3, n), dtype=bool)


def generated(task) -> int:
    return task.out_stream.total_samp_per_chan_generated


def loaded():
    """Load the saved pair as they are, read what the adapter would change,
    and release them. Loading does not start or commit anything."""
    import nidaqmx.system

    tasks = nidaqmx.system.System.local().tasks
    ao, do = tasks[AO_TASK].load(), tasks[DO_TASK].load()
    try:
        return {
            "ao_mech": ao.ao_channels.all.ao_data_xfer_mech,
            "do_mech": do.do_channels.all.do_data_xfer_mech,
            "ao_buf": ao.out_stream.output_buf_size,
            "do_buf": do.out_stream.output_buf_size,
        }
    finally:
        ao.close()
        do.close()


def driver_end(task) -> float:
    """Seconds until ``wait_until_done`` returns on ``task`` -- by raising the
    ran-dry error, on a continuous task that has emptied its buffer."""
    started = time.monotonic()
    try:
        task.wait_until_done(timeout=BUFFER / RATE + 2.0)
    except DaqError as exc:
        if exc.error_code not in UNDERFLOW_CODES:
            raise
    return time.monotonic() - started


def ms(samples: float) -> str:
    return f"{samples / RATE * 1000:.1f} ms"


# --- the board -------------------------------------------------------------------


def test_what_the_board_is(report):
    """Read-only: the context every other number needs. And H4's first half:
    what data transfer and buffer depth the saved tasks load with, before the
    adapter changes them."""
    import nidaqmx.system

    system = nidaqmx.system.System.local()
    device = system.devices[DEVICE]
    version = system.driver_version
    found = loaded()

    report(f"{DEVICE} is a {device.product_type}, {'SIMULATED' if SIMULATED else 'physical'}, "
           f"driver {version.major_version}.{version.minor_version}.{version.update_version}")
    report(f"saved tasks load with AO transfer {found['ao_mech'].name}, DO transfer "
           f"{found['do_mech'].name}, buffers {found['ao_buf']} / {found['do_buf']} samples")


# --- H1: running dry ---------------------------------------------------------------


async def ran_dry(output) -> None:
    """Fill the buffer once, start, and wait until it has certainly emptied."""
    await output.write(*zeros(BUFFER))
    await output.go()
    await asyncio.sleep(BUFFER / RATE + 0.5)


async def underflow_from(call, report) -> Underflow:
    """What ``call`` raises, which must be ``Underflow``. ``call`` may be
    synchronous or return an awaitable."""
    try:
        result = call()
        if inspect.isawaitable(result):
            result = await result
    except Underflow as exc:
        cause = exc.__cause__
        report(f"Underflow, from DAQmx error {cause.error_code} ({cause.error_type.name}), "
               f"at t = {exc.at:.4f} s")
        return exc
    except DaqError as exc:
        pytest.fail(
            f"DAQmx error {exc.error_code} ({exc.error_type.name}) reached the caller where "
            f"the port promises Underflow -- either the board ran dry with a code the adapter "
            f"does not know, or reading the sample count failed while handling it:\n{exc}"
        )
    pytest.fail(f"the board ran dry and the call returned {result!r} instead of raising Underflow")


@physical_only
async def test_h1_space_available_reports_running_dry(make_output, report):
    """H1. The port promises ``space_available`` raises ``Underflow`` once
    output has run dry, and the core's refill loop asks it first -- so if the
    board reports the gap only on the next write, the loop sees free space
    instead. ``Underflow.at`` is read from the sample count *while handling
    the driver's error*; if that read fails, a raw DaqError surfaces here."""
    output = await make_output()
    await ran_dry(output)

    underflow = await underflow_from(output.space_available, report)

    assert underflow.at == pytest.approx(BUFFER / RATE, rel=TOLERANCE)


@physical_only
async def test_h1_a_late_write_reports_running_dry(make_output, report):
    """H1. The same, for the write that arrives too late: the refill loop's
    other point of contact."""
    output = await make_output()
    await ran_dry(output)

    underflow = await underflow_from(lambda: output.write(*zeros(SEGMENT)), report)

    assert underflow.at == pytest.approx(BUFFER / RATE, rel=TOLERANCE)


# --- H2: where output ends -------------------------------------------------------


async def test_h2_drain_by_polling(make_output, report):
    """H2, the adapter's way. ``drain`` polls the sample count, so it can
    only be as late as one poll -- but it may return before the board is
    truly finished. On a physical board, how much later ``wait_until_done``
    then returns on each task is the gap between the two notions of "done"."""
    output = await make_output()
    await output.write(*zeros(BUFFER))
    started = time.monotonic()
    await output.go()

    await output.drain()
    polled = time.monotonic() - started
    counts = generated(output._ao), generated(output._do)

    report(f"drain returned after {polled:.4f} s (expected {BUFFER / RATE:.4f}), "
           f"generated AO {counts[0]}, DO {counts[1]}")
    if not SIMULATED:
        report(f"wait_until_done then took a further AO {driver_end(output._ao) * 1000:.1f} ms, "
               f"DO {driver_end(output._do) * 1000:.1f} ms")
    assert counts == (BUFFER, BUFFER)
    assert polled == pytest.approx(BUFFER / RATE, rel=TOLERANCE)


@physical_only
async def test_h2_drain_by_waiting(make_output, report):
    """H2, the legacy engine's way: ``wait_until_done`` on each task, which
    on a continuous task ends by raising the ran-dry error. Compare the
    elapsed time with the polling drain's. The sample counts are read after
    that error, which is also H1's question."""
    output = await make_output()
    await output.write(*zeros(BUFFER))
    started = time.monotonic()
    await output.go()

    ends = driver_end(output._ao), driver_end(output._do)
    waited = time.monotonic() - started
    counts = generated(output._ao), generated(output._do)

    report(f"wait_until_done returned after {waited:.4f} s (expected {BUFFER / RATE:.4f}; "
           f"AO {ends[0]:.4f} s, then DO {ends[1] * 1000:.1f} ms more), "
           f"generated AO {counts[0]}, DO {counts[1]}")
    assert counts == (BUFFER, BUFFER)
    assert waited == pytest.approx(BUFFER / RATE, rel=TOLERANCE)


# --- H3 and H4: keeping it fed ---------------------------------------------------


async def stream(output, refill: str) -> tuple[int, float, int]:
    """Pre-fill, start, and feed ``STREAM_SECONDS`` of zeros a segment at a
    time. Returns the lowest buffer fill seen just after a refill (samples),
    the process's CPU time as a fraction of wall time while streaming, and
    the number of refills."""
    written = 0
    while output.space_available() >= SEGMENT:
        await output.write(*zeros(SEGMENT))
        written += SEGMENT
    await output.go()

    lowest = written
    refills = int(STREAM_SECONDS * RATE / SEGMENT)
    wall, cpu = time.monotonic(), time.process_time()
    for _ in range(refills):
        if refill == "polled":
            await output.wait_for_space(SEGMENT)
            await output.write(*zeros(SEGMENT))
        else:
            # The legacy engine's refill: one write per segment, left to block
            # inside the driver until there is room for all of it.
            ao, do = zeros(SEGMENT)
            output._ao.write(ao, auto_start=False, timeout=10.0)
            output._do.write(do, auto_start=False, timeout=10.0)
        written += SEGMENT
        lowest = min(lowest, written - generated(output._ao))
    load = (time.process_time() - cpu) / (time.monotonic() - wall)
    return lowest, load, refills


@pytest.mark.parametrize("buffer", [BUFFER, TIGHT], ids=["1s-buffer", "100ms-buffer"])
@pytest.mark.parametrize("refill", ["polled", "blocking"])
async def test_h3_refill(make_output, report, refill, buffer):
    """H3. The adapter polls ``space_avail`` every millisecond; the legacy
    engine blocked in one write per segment. Both must keep the board fed at
    100 kS/s without running dry. The lowest fill after a refill is the
    margin that was left; CPU time is what the refill cost, driver included."""
    output = await make_output(buffer)

    lowest, load, refills = await stream(output, refill)

    report(f"{refills} refills of {SEGMENT}: lowest fill {lowest} samples "
           f"({ms(lowest)} from running dry), CPU {load:.1%} of one core")
    assert lowest > 0


@pytest.mark.parametrize("mechanism", ["as-loaded", "DMA"])
async def test_h4_data_transfer(make_output, report, mechanism):
    """H4. The polled refill at the tight buffer, once with the transfer
    mechanism the saved tasks load with and once with the DMA the adapter
    sets. A refusal or a run dry under the loaded mechanism is itself the
    answer, so it is reported rather than failed. (The simulated board
    refuses the analog task's loaded POLLED -- programmed I/O -- for buffered
    output at commit, -200767; whether the physical one agrees is the
    question.)"""
    found = loaded()
    output = await make_output(TIGHT)
    names = (found["ao_mech"].name, found["do_mech"].name) if mechanism == "as-loaded" else ("DMA",) * 2

    try:
        if mechanism == "as-loaded":
            # A committed task refuses a new transfer mechanism (-201288).
            for task in (output._ao, output._do):
                task.control(TaskMode.TASK_UNRESERVE)
            output._ao.ao_channels.all.ao_data_xfer_mech = found["ao_mech"]
            output._do.do_channels.all.do_data_xfer_mech = found["do_mech"]
            for task in (output._ao, output._do):
                task.control(TaskMode.TASK_COMMIT)
        lowest, load, refills = await stream(output, "polled")
    except (DaqError, Underflow) as exc:
        if mechanism == "DMA":
            raise
        why = (f"DAQmx {exc.error_code}: {str(exc).splitlines()[0]}"
               if isinstance(exc, DaqError) else repr(exc))
        report(f"AO {names[0]} / DO {names[1]}: refused -- {why}")
        return

    report(f"AO {names[0]} / DO {names[1]}: lowest fill {lowest} samples ({ms(lowest)}), "
           f"CPU {load:.1%} of one core")
    assert lowest > 0
    if mechanism == "DMA":
        assert (output._ao.ao_channels.all.ao_data_xfer_mech,
                output._do.do_channels.all.do_data_xfer_mech) == (DataTransferActiveTransferMode.DMA,) * 2
