"""5a-3 (Step 3, H1-H10) -- the NI adapter on a physical PCI-6733.

RUN BY THE USER ONLY. This suite starts real analog and digital output on the
board it is pointed at. Every sample it writes is 0 V on the analog channels
and low on the digital lines, but the board is still clocking out, so run it
only where that is safe -- on the development box, with nothing wired.

What it answers, from the architecture plan's Step 3:

  H1  That the adapter reports running dry as an Underflow, wherever the
      refill loop meets it.
  H2  That a drain ends when output does. (The legacy engine's
      wait_until_done was compared here too, and never returns on this
      board; see the plan.)
  H3  Refilling by polling for space (the adapter) against one blocking write
      per waveform (the legacy engine): what each costs in CPU.
  H4  What the saved tasks load with for data transfer, and whether setting
      DMA changes anything measurable.
  H5  How small the hardware buffer can be. The real WaveformSource drives
      the board at several buffer depths; for each, how thin the margin got,
      how much was queued at worst, and how long a MAIN update took to reach
      the output. A small buffer is what keeps updates prompt; running dry is
      its price. Fails if the refill holds the event loop.
  H6  How long a MAIN update takes to reach the outputs, sent at random
      points in the cycle, for a short and a long volume.
  H7  How long a stall of the event loop the output survives, by depth.
  H8  That a queued sequence of MAINs plays in order on the board.
  H9  That the instrument's buffer depth holds over a long run: how thin
      the queue got, and whether it ever ran dry.
  H10 That the last digital samples reach the pin when output ends. Needs
      P0.1 wired to a PFI line, named in STACSCOPE_FEEDBACK; skips without.

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

With P0.1 wired to PFI9, also set STACSCOPE_FEEDBACK=PFI9 to run H10.

Takes about six minutes, two of them H9's. For a longer soak, set STACSCOPE_SOAK_SECONDS (per volume):

    PowerShell:  $env:STACSCOPE_SOAK_SECONDS = "1800"
                 uv run --extra nidaqmx pytest tests/hardware/nidaqmx -k h9
"""

import asyncio
import contextlib
import inspect
import os
import random
import statistics
import time
from types import SimpleNamespace

import numpy as np
import pytest
from nidaqmx.constants import DataTransferActiveTransferMode, TaskMode
from nidaqmx.errors import DaqError
from saved_tasks import PREFIX, saved

from stacscope.adapters.nidaqmx import NiSignalOutput
from stacscope.adapters.nidaqmx.signal_output import BUFFER_SAMPLES, TAIL_PAD
from stacscope.core.actors import Accepted, Actor, Priority, Subscribe, ask, spawn
from stacscope.core.ports import Channel, MonotonicClock, OutputConfig, Underflow
from stacscope.core.protocol import (
    DeviceReady,
    Finish,
    Go,
    MainApplied,
    Phase,
    QueueMain,
    SetWaveform,
    StartDevice,
    Waveform,
)
from stacscope.core.timing import WaveformSource

DEVICE = os.environ.get("STACSCOPE_HARDWARE", "")
AO_TASK = f"{PREFIX}hw_ao"
DO_TASK = f"{PREFIX}hw_do"
RATE = 100_000.0

BUFFER = 100_000
"""1 s of output. The instrument's real depth is still an open item (the plan's
"confirm the actual DAQmx output buffer depth"), so H3 also runs at TIGHT."""

TIGHT = 10_000
"""100 ms: where a slow refill would show up first."""

REFILL = 2_000
"""20 ms per refill, the cadence of a short waveform."""

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
    # Every adapter is closed even if stopping it failed: a task left loaded
    # makes every later test fail to load it, hiding what they would measure.
    # Failures are re-raised once everything is released.
    failures = []
    for output in outputs:
        for release in (output.stop, output.close):
            try:
                await release()
            except Exception as exc:  # noqa: BLE001, PERF203 - release everything, then report
                failures.append(exc)
    if failures:
        raise ExceptionGroup("releasing the board failed", failures)


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

    underflow = await underflow_from(lambda: output.write(*zeros(REFILL)), report)

    assert underflow.at == pytest.approx(BUFFER / RATE, rel=TOLERANCE)


@physical_only
async def test_h1_what_the_driver_says_after_running_dry(report):
    """H1, as a survey rather than a verdict. The first physical run showed
    ``space_avail`` returning 0 after running dry, rather than raising, and
    ``stop`` raising -200290. Before the adapter is changed, this records
    what every call it might use instead does in that state, in order, on
    the raw tasks: which report the gap, whether any clears it, whether a
    stop takes, and whether the pair can play again without reloading --
    which is what a second acquisition would need. Never fails on the
    driver's answers; every one is a finding.

    It holds its own adapter rather than using the fixture, whose release
    step would report the survey's deliberate stops as failures."""
    output = NiSignalOutput(MonotonicClock(), ao_task=AO_TASK, do_task=DO_TASK)
    try:
        await output.configure(config())
        await output.arm()
        await ran_dry(output)
        ao, do = output._ao, output._do

        def probe(what, call):
            try:
                result = call()
            except DaqError as exc:
                report(f"{what}: raised {exc.error_code} ({exc.error_type.name})")
            else:
                report(f"{what}: returned {result!r}")

        probe("AO space_avail", lambda: ao.out_stream.space_avail)
        probe("AO total generated", lambda: ao.out_stream.total_samp_per_chan_generated)
        probe("DO total generated", lambda: do.out_stream.total_samp_per_chan_generated)
        probe("AO is_task_done", ao.is_task_done)
        probe("DO is_task_done", do.is_task_done)
        probe("AO space_avail again", lambda: ao.out_stream.space_avail)
        probe("AO write while stopped by the driver",
              lambda: ao.write(np.zeros((5, REFILL)), auto_start=False))
        probe("AO stop", ao.stop)
        probe("AO stop again", ao.stop)
        probe("DO stop", do.stop)
        probe("DO stop again", do.stop)
        probe("AO space_avail after stop", lambda: ao.out_stream.space_avail)
        probe("DO write after stop",
              lambda: do.write(np.zeros((3, REFILL), dtype=bool), auto_start=False))
        probe("AO write after stop",
              lambda: ao.write(np.zeros((5, REFILL)), auto_start=False))
        probe("DO start again", do.start)
        probe("AO start again", ao.start)
        await asyncio.sleep(0.1)
        probe("AO total generated after restart",
              lambda: ao.out_stream.total_samp_per_chan_generated)
        probe("AO stop after restart", ao.stop)
        probe("DO stop after restart", do.stop)
    finally:
        with contextlib.suppress(Exception):
            await output.stop()
        await output.close()


def read_state(ao, do) -> tuple:
    """(AO space, DO space, AO generated, DO generated), each an error code if
    reading it raised. Only the cheap reads: none of them is ``is_task_done``,
    which is the call suspected of changing what the others say."""

    def read(call):
        try:
            return call()
        except DaqError as exc:
            return f"E{exc.error_code}"

    return (read(lambda: ao.out_stream.space_avail), read(lambda: do.out_stream.space_avail),
            read(lambda: generated(ao)), read(lambda: generated(do)))


def spans(samples: list[tuple[float, tuple]]) -> list[str]:
    """A timed series of states, compressed to one line per stretch in which
    each reading kept doing the same thing -- rising, falling, holding, at 0
    or raising -- judged against the reading 2 ms earlier, so that a count
    moving in blocks is not a new stretch at every block. Each line gives
    the readings at the stretch's start and end."""

    def trend(now, before):
        if isinstance(now, str):
            return "error"
        if now == 0:
            return "zero"
        if isinstance(before, str) or now == before:
            return "held"
        return "rising" if now > before else "falling"

    keys = []
    for t, state in samples:
        before = next((s for u, s in reversed(samples) if u <= t - 0.002), samples[0][1])
        keys.append(tuple(trend(a, b) for a, b in zip(state, before)))

    lines = []
    i = 0
    while i < len(samples):
        j = i
        while j + 1 < len(samples) and keys[j + 1] == keys[i]:
            j += 1
        (t0, first), (t1, last) = samples[i], samples[j]
        span = " ".join(f"{name} {a}->{b} ({k})" for name, a, b, k in
                        zip(("AO space", "DO space", "AO generated", "DO generated"), first, last, keys[i]))
        lines.append(f"{t0 * 1000:+.1f} to {t1 * 1000:+.1f} ms: {span}")
        i = j + 1
    return lines


WATCH_AFTER = 1.0
"""How long past the end of output ``space_avail`` is watched: twice as long
as H1 waited before it read 0."""


@physical_only
@pytest.mark.parametrize("watch", ["polled", "left-alone"])
@pytest.mark.parametrize("fill", ["written-once", "kept-fed"])
async def test_h1_how_free_space_reads_as_output_runs_dry(report, fill, watch):
    """H1, a survey, to reconcile two findings. Written once and read 0.5 s
    after running dry, the AO task's ``space_avail`` read 0 (the first H1
    survey). Kept fed at a small buffer and read just after a stall ran it
    dry, it read the whole buffer free (the stall probe). The adapter reads
    both ways correctly -- a full buffer free sends the refill to a write
    that raises, and 0 with a stopped count asks ``is_task_done`` -- but
    which one the board gives, and when, should be known rather than
    guessed.

    So this watches the transition itself: from 100 ms before output ends to
    a second after, with only the cheap reads, then asks ``is_task_done``
    and reads again. ``fill`` is the two histories -- one long write, or a
    buffer at the instrument's depth kept fed and then left. ``watch`` is
    whether anything reads the task while it runs dry, in case reading is
    what moves it; left alone, the first read comes a second after the end.
    Times are from the predicted end of output -- the last sample written
    reaching the outputs, from the analog count at the last write -- so
    running dry, which the board declares when the host buffer empties,
    should show about 13 ms before 0.
    Never fails on the driver's answers; every one is a finding."""
    buffer = BUFFER if fill == "written-once" else BUFFER_SAMPLES
    output = NiSignalOutput(MonotonicClock(), ao_task=AO_TASK, do_task=DO_TASK)
    lines = []
    written = 0
    try:
        await output.configure(config(buffer))
        await output.arm()
        ao, do = output._ao, output._do
        await output.write(*zeros(buffer))
        await output.go()
        if fill == "kept-fed":
            until_left = time.monotonic() + 0.5
            while time.monotonic() < until_left:
                await output.wait_for_space(REFILL)
                await output.write(*zeros(REFILL))
        # From here on nothing is written, and the loop blocks: nothing else
        # runs in this test, and a sleep would blur the times by the Windows
        # timer's 16 ms.
        written = output._written
        end = time.perf_counter() + (written - generated(ao)) / RATE

        samples = []
        if watch == "polled":
            start = end - 0.1
            while time.perf_counter() < start:
                pass
            while (now := time.perf_counter()) < end + WATCH_AFTER:
                samples.append((now - end, read_state(ao, do)))
                while time.perf_counter() < now + 0.0005:
                    pass
        else:
            while time.perf_counter() < end + WATCH_AFTER:
                pass
        samples.append((time.perf_counter() - end, read_state(ao, do)))
        lines += spans(samples)

        for name, task in (("AO", ao), ("DO", do)):
            try:
                lines.append(f"then {name} is_task_done: returned {task.is_task_done()!r}")
            except DaqError as exc:
                lines.append(f"then {name} is_task_done: raised {exc.error_code}")
        lines.append(f"then the reads: {read_state(ao, do)}")
        time.sleep(0.5)
        lines.append(f"0.5 s later: {read_state(ao, do)}")
    finally:
        with contextlib.suppress(Exception):
            await output.stop()
        await output.close()

    report(f"{ms(buffer)} host buffer, {written} samples written; times from the last one "
           f"reaching the outputs:")
    for line in lines:
        report(f"  {line}")


# --- H2: where output ends -------------------------------------------------------


async def test_h2_drain_ends_when_output_does(make_output, report):
    """H2. ``drain`` returns once the board has played everything written,
    and no sooner. The first physical run found the analog task's count stops
    15 short and never arrives, so the drain now also ends on the driver's
    ran-dry report. The digital task, on the same clock, must still have
    counted every sample -- that is the evidence the clock ran to the end --
    and the stop after it must be clean. Its count may go past them into the
    drain's padding (TAIL_PAD), of which the board drops the last 64 (H10),
    but no further."""
    output = await make_output()
    await output.write(*zeros(BUFFER))
    started = time.monotonic()
    await output.go()

    await output.drain()
    took = time.monotonic() - started
    counts = generated(output._ao), generated(output._do)
    await output.stop()

    report(f"drain returned after {took:.4f} s (expected {BUFFER / RATE:.4f}), "
           f"generated AO {counts[0]}, DO {counts[1]}")
    assert BUFFER <= counts[1] <= BUFFER + TAIL_PAD
    assert took == pytest.approx(BUFFER / RATE, rel=TOLERANCE)


# --- H3 and H4: keeping it fed ---------------------------------------------------


async def stream(output, refill: str) -> tuple[float, int]:
    """Pre-fill, start, and feed ``STREAM_SECONDS`` of zeros a waveform at a
    time. Returns the process's CPU time as a fraction of wall time while
    streaming, and the number of refills. Running dry raises."""
    # (The first run also reported the fill just after each refill. That is
    # full by construction, so it measured nothing; H5 measures the margin,
    # just before each refill.)
    while output.space_available() >= REFILL:
        await output.write(*zeros(REFILL))
    await output.go()

    refills = int(STREAM_SECONDS * RATE / REFILL)
    wall, cpu = time.monotonic(), time.process_time()
    for _ in range(refills):
        if refill == "polled":
            await output.wait_for_space(REFILL)
            await output.write(*zeros(REFILL))
        else:
            # The legacy engine's refill: one write per waveform, left to block
            # inside the driver until there is room for all of it.
            ao, do = zeros(REFILL)
            output._ao.write(ao, auto_start=False, timeout=10.0)
            output._do.write(do, auto_start=False, timeout=10.0)
    output.space_available()  # raises Underflow if it ran dry on the way
    load = (time.process_time() - cpu) / (time.monotonic() - wall)
    return load, refills


@pytest.mark.parametrize("buffer", [BUFFER, TIGHT], ids=["1s-buffer", "100ms-buffer"])
@pytest.mark.parametrize("refill", ["polled", "blocking"])
async def test_h3_refill(make_output, report, refill, buffer):
    """H3. The adapter polls ``space_avail`` every millisecond; the legacy
    engine blocked in one write per waveform. Both must keep the board fed at
    100 kS/s without running dry. CPU time is what the refill cost, driver
    included."""
    output = await make_output(buffer)

    load, refills = await stream(output, refill)

    report(f"{refills} refills of {REFILL}, no gap, CPU {load:.1%} of one core")


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
        load, refills = await stream(output, "polled")
    except (DaqError, Underflow) as exc:
        if mechanism == "DMA":
            raise
        why = (f"DAQmx {exc.error_code}: {str(exc).splitlines()[0]}"
               if isinstance(exc, DaqError) else repr(exc))
        report(f"AO {names[0]} / DO {names[1]}: refused -- {why}")
        return

    report(f"AO {names[0]} / DO {names[1]}: {refills} refills, no gap, CPU {load:.1%} of one core")
    if mechanism == "DMA":
        assert (output._ao.ao_channels.all.ao_data_xfer_mech,
                output._do.do_channels.all.do_data_xfer_mech) == (DataTransferActiveTransferMode.DMA,) * 2


# --- H5-H8: the real source on the board -------------------------------------------

MAIN = 5_000
"""50 ms, a frame at 20 fps. Longer than the smaller buffers in the sweep, so
the chunked refill of a long waveform is exercised too."""

LONG_MAIN = 30_000
"""300 ms, the longest volume the instrument has used."""

FLOOR = 3_276
"""The smallest host buffer the adapter accepts with five analog channels: the
analog task's onboard FIFO. Below it the board failed with nothing on our side
late (the refill experiments, in the plan)."""

SWEEP = [10_000, 5_000, FLOOR]

LOOP_LAG_LIMIT = 0.1
"""How long a 10 ms sleep elsewhere on the event loop may take while the
source runs. The Windows timer alone makes it up to ~16 ms, and a busy refill
a little more; before the adapter asked both tasks for space, a write that
blocked inside the driver made it take whole cycles -- 150 ms at a 50 ms MAIN,
900 ms at 300 ms -- and every other actor starved with it."""


SLOW_CALL = 0.005
"""Seconds: a call into the adapter longer than this is noted as a possible
cause of a loop hold (CM51)."""


class Watched:
    """The adapter, observed. At each write once output is playing: when it
    came, how many samples had been written before it, how much was still
    queued ahead of the outputs, and the first analog value it carries.
    Everything else passes straight through.

    Queued is written less the analog task's generated count, which the refill
    experiments checked against a counter on ao/SampleClock: it follows the
    clock to within a few samples, so it is what is really still to play --
    the host buffer plus the board's FIFO. The digital task's count is not
    used: it runs up to its FIFO's depth ahead of the clock."""

    def __init__(self, output: NiSignalOutput) -> None:
        self.output = output
        self.playing = None
        self.written = 0
        self.writes: list[tuple[float, int, int, float]] = []
        self.slow: list[tuple[float, str, float]] = []
        """(when it began, which call, seconds) for every call into the
        adapter that held the event loop longer than SLOW_CALL: the
        candidates for a loop hold (CM51)."""

    def __getattr__(self, name):
        return getattr(self.output, name)

    def _timed(self, name: str, call):
        began = time.monotonic()
        try:
            return call()
        finally:
            took = time.monotonic() - began
            if took > SLOW_CALL:
                self.slow.append((began, name, took))

    def space_available(self) -> int:
        # The call that may ask is_task_done, 16 ms at the median on Dev1.
        return self._timed("space_available", self.output.space_available)

    def samples_played(self) -> int:
        return self._timed("samples_played", self.output.samples_played)

    async def go(self) -> None:
        await self.output.go()
        self.playing = time.monotonic()

    async def write(self, ao, do, *, label=None) -> None:
        if self.playing is not None:
            queued = self.written - generated(self.output._ao)
            self.writes.append((time.monotonic(), self.written, queued, float(ao[0, 0])))
        began = time.monotonic()
        await self.output.write(ao, do, label=label)
        took = time.monotonic() - began
        if took > SLOW_CALL:
            self.slow.append((began, "write", took))
        self.written += ao.shape[1]

    def plays_at(self, sample: int) -> float:
        """When ``sample`` reached the outputs, from the write that queued it."""
        for t, start, queued, _ in reversed(self.writes):
            if start <= sample:
                return t + (queued + sample - start) / RATE
        raise AssertionError(f"sample {sample} was written before output started")


class Heard(Actor):
    def __init__(self) -> None:
        super().__init__()
        self.heard: list = []

    async def handle(self, msg) -> None:
        self.heard.append(msg)


async def until(predicate, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "the source never got there"
        await asyncio.sleep(0.01)


async def drive(buffer: int, main: int, seconds: float, during=None, *, queue=()):
    """Run the real ``WaveformSource`` on the board for ``seconds``, with
    ``during(ref, output)`` alongside and ``queue`` -- (tag, samples) pairs --
    queued before it starts. Returns what it saw: the observed adapter, how
    long a 10 ms sleep elsewhere on the loop took at worst, the process's CPU
    share, everything the source published, and the Underflow it died of, if
    it did. Any other error is raised."""
    output = Watched(NiSignalOutput(MonotonicClock(), ao_task=AO_TASK, do_task=DO_TASK))
    source = WaveformSource(output, config(buffer))
    ref = spawn(source)
    heard = Heard()
    ref.tell(Subscribe(spawn(heard)))
    lags = [0.0]

    async def ticker():
        while True:
            t = time.monotonic()
            await asyncio.sleep(0.01)
            lags.append(time.monotonic() - t)

    beside = []
    try:
        await asyncio.wait_for(source.started.wait(), timeout=5.0)
        for waveform, n in ((Waveform.WARMUP, main), (Waveform.MAIN, main), (Waveform.COOLDOWN, REFILL)):
            assert await ask(ref, SetWaveform(waveform, *zeros(n)), timeout=5.0) == Accepted()
        for tag, n in queue:
            assert await ask(ref, QueueMain(tag, *zeros(n)), timeout=5.0) == Accepted()
        assert await ask(ref, StartDevice("board", -1), timeout=5.0) == DeviceReady()
        wall, cpu = time.monotonic(), time.process_time()
        ref.tell(Go())
        await until(lambda: output.playing is not None or source.stopped.is_set(), timeout=5.0)
        beside.append(asyncio.ensure_future(ticker()))
        if during is not None:
            beside.append(asyncio.ensure_future(during(ref, output)))
        end = time.monotonic() + seconds
        while time.monotonic() < end and not source.stopped.is_set():
            await asyncio.sleep(0.02)
        for task in beside:
            task.cancel()
        if beside[1:] and beside[1].done() and not beside[1].cancelled() and beside[1].exception():
            raise beside[1].exception()
        if not source.stopped.is_set():
            ref.tell(Finish(), Priority.URGENT)
            await until(lambda: source.phase is Phase.IDLE or source.stopped.is_set(), timeout=5.0)
        load = (time.process_time() - cpu) / (time.monotonic() - wall)
    finally:
        for task in beside:
            task.cancel()
        source.stop()
        await asyncio.wait_for(source.stopped.wait(), timeout=5.0)

    error = source.exit_error
    if error is not None and not isinstance(error, Underflow):
        raise error
    return SimpleNamespace(output=output, lag=max(lags), load=load, heard=heard.heard, underflow=error)


@pytest.mark.parametrize("buffer", SWEEP, ids=[f"{b // 100}ms-buffer" for b in SWEEP])
async def test_h5_how_small_the_buffer_can_be(report, buffer):
    """H5. The hardware buffer is small on purpose: waiting for room in it
    paces the refill loop, and its depth is all that can queue ahead of a
    MAIN update, so it sets how stale the output can be. Too small and the
    loop, woken late by the operating system, lets it run dry.

    This runs the real ``WaveformSource`` -- the actor, its refill loop and
    the adapter -- for a few seconds at each depth down to the floor the
    adapter accepts, sends a MAIN update halfway, and reports, for the choice
    of ``buffer_samples``:
    - whether it ran dry, which is a finding, not a failure;
    - the thinnest margin at a refill, and the most ever queued -- which,
      above the buffer's own depth, is what the board holds on board;
    - the longest gap between refills: how late the loop really woke;
    - how long the update took from being sent to reaching the outputs.
    The update is MAIN again, as -0.0: the same 0 V, told apart by its sign
    bit.

    It fails if the refill held the event loop: that starves every other
    actor in the process, and is the adapter's fault, not the depth's."""
    sent = []

    async def update(ref, output):
        await asyncio.sleep(STREAM_SECONDS / 2)
        ao, do = zeros(MAIN)
        ao[:] = -0.0
        sent.append(time.monotonic())
        assert await ask(ref, SetWaveform(Waveform.MAIN, ao, do), timeout=5.0) == Accepted()

    run = await drive(buffer, MAIN, STREAM_SECONDS, update)

    writes = run.output.writes
    times = [t for t, _, _, _ in writes]
    queued = [q for _, _, q, _ in writes]
    gaps = [b - a for a, b in zip(times, times[1:])]
    update = next((start for _, start, _, v in writes if np.signbit(v)), None)
    reached = (f"the update reached the outputs {(run.output.plays_at(update) - sent[0]) * 1000:.0f} ms "
               f"after it was sent" if update is not None and sent else "no update was played")
    dry = f"RAN DRY at t = {run.underflow.at:.3f} s" if run.underflow else "no gap"
    report(f"{ms(buffer)} buffer: {dry}; {len(times)} refills, margin at a refill "
           f"{ms(min(queued, default=0))} at least, queued {ms(max(queued, default=0))} at most; "
           f"refills up to {max(gaps, default=0) * 1000:.1f} ms apart; {reached}; "
           f"event loop held up to {run.lag * 1000:.0f} ms; CPU {run.load:.1%} of one core")
    # Not on a simulated device: its driver paces writes its own way, and
    # held the loop 140-160 ms in rehearsal whatever the adapter did.
    if not SIMULATED:
        assert run.lag < LOOP_LAG_LIMIT, f"the refill held the event loop for {run.lag * 1000:.0f} ms"


@physical_only
@pytest.mark.parametrize("main", [MAIN, LONG_MAIN], ids=["50ms-volume", "300ms-volume"])
async def test_h6_update_latency_across_the_cycle(report, main):
    """H6. The first-class behaviour, on the board. A MAIN update lands at the
    first cycle boundary not yet committed, and the writer commits a boundary
    one lead ahead of the outputs -- the host buffer plus the board's FIFO. So
    an update sent at a random moment reaches the outputs somewhere between
    one lead and one volume plus one lead later, and never later than that:
    nothing else queues ahead of it. Updates are sent at random phases and
    told apart by the index of the first sample of the MAIN they start, which
    ``MainApplied`` reports; the output stays at 0 V throughout."""
    updates = 12
    sent: list[float] = []

    async def send(ref, output):
        for _ in range(updates):
            await asyncio.sleep(main / RATE * random.uniform(1.6, 2.6))
            sent.append(time.monotonic())
            assert await ask(ref, SetWaveform(Waveform.MAIN, *zeros(main)), timeout=5.0) == Accepted()

    seconds = updates * (main / RATE * 2.6 + 0.05) + 0.5
    run = await drive(FLOOR, main, seconds, send)

    assert run.underflow is None
    applied = [m for m in run.heard if isinstance(m, MainApplied)][1:]  # [0] is cycle 0
    assert len(applied) == len(sent) == updates, "an update never reached the outputs"
    latency = [run.output.plays_at(m.sample) - t for m, t in zip(applied, sent)]
    lead = (FLOOR + 2 * 2048) / RATE  # host buffer, the digital FIFO, and a refill block of slack
    report(f"{main / RATE * 1000:.0f} ms volume at {ms(FLOOR)} buffer: {updates} updates reached the "
           f"outputs {min(latency) * 1000:.0f}-{max(latency) * 1000:.0f} ms after being sent, "
           f"mean {statistics.mean(latency) * 1000:.0f} ms")
    assert max(latency) <= main / RATE + lead


STALLS = [0.010, 0.020, 0.030, 0.040, 0.050, 0.060, 0.080]


@physical_only
@pytest.mark.parametrize("buffer", [FLOOR, 5_000], ids=["floor-buffer", "50ms-buffer"])
async def test_h7_how_long_a_stall_the_output_survives(report, buffer):
    """H7. What the depth buys: a stall of the whole event loop -- a GC pause,
    a slow disk, another actor's long handler -- that the output rides out.
    Each run blocks the loop eight times at random phases, for a longer time
    each run, until the output runs dry. The refill experiments found a stall
    survived exactly when it is shorter than what was queued as it began, so
    this reports that too. A measurement: it never fails on the depth."""
    survived = None
    for stall in STALLS:
        margins = []

        async def stalls(ref, output):
            await asyncio.sleep(0.5)
            for _ in range(8):
                await asyncio.sleep(random.uniform(0.15, 0.35))
                margins.append(output.written - generated(output.output._ao))
                time.sleep(stall)  # blocks the event loop, and the refill with it

        run = await drive(buffer, MAIN, 4.0, stalls)
        if run.underflow:
            report(f"{ms(buffer)} buffer: survived {survived * 1000:.0f} ms stalls; ran dry on a "
                   f"{stall * 1000:.0f} ms one, with {ms(min(margins))} queued at the thinnest"
                   if survived else f"{ms(buffer)} buffer: ran dry on a {stall * 1000:.0f} ms stall")
            return
        survived = stall
    report(f"{ms(buffer)} buffer: survived every stall up to {survived * 1000:.0f} ms")


async def test_h8_a_queued_sequence_plays_in_order(report):
    """H8. The second-class behaviour, on the board: queued MAINs play once
    each, in order, back to back, and the last one stays. Every step is 0 V,
    so the steps are told apart by length -- which a queued MAIN may choose --
    and ``MainApplied`` must report each one at the sample where the previous
    one ended."""
    steps = [(f"step{i}", MAIN + 100 * i) for i in range(1, 7)]

    run = await drive(FLOOR, MAIN, 2.0, queue=steps)

    assert run.underflow is None
    applied = [m for m in run.heard if isinstance(m, MainApplied)]
    assert [m.tag for m in applied] == [tag for tag, _ in steps]
    assert [m.cycle for m in applied] == list(range(len(steps)))
    starts = [m.sample for m in applied]
    assert starts[0] == MAIN  # right after warmup
    assert [b - a for a, b in zip(starts, starts[1:])] == [n for _, n in steps[:-1]]
    report(f"{len(steps)} queued steps played in order from sample {starts[0]}, no gap; "
           f"event loop held up to {run.lag * 1000:.0f} ms")


SOAK_SECONDS = float(os.environ.get("STACSCOPE_SOAK_SECONDS", "60"))
"""How long H9 runs each volume. A minute by default, so every run of the
suite gives a number; set STACSCOPE_SOAK_SECONDS for a long soak."""

THIN = 0.020
"""The thinnest queue the instrument's depth should keep, from the plan's
conditions for going higher: below it, one more late wake of the loop than a
short run sees is enough to run dry."""


@pytest.mark.timeout(2 * SOAK_SECONDS + 60)
@pytest.mark.parametrize("main", [MAIN, LONG_MAIN], ids=["50ms-volume", "300ms-volume"])
async def test_h9_a_long_run_at_the_instruments_depth(report, main):
    """H9. The depth the instrument uses, ``BUFFER_SAMPLES``, run for long
    enough to meet the operating system's rare late wakes, which short runs
    miss. A 60 s soak at the floor had no gap, but its queue dipped to about
    16 ms where short runs never went below 30. The depth was chosen on the
    short runs; this is the check that it holds.

    It fails if the output runs dry: at the instrument's own depth, with
    nothing else in the process, that is a gap in a real acquisition. The
    thinnest queue is a finding, flagged when it is below ``THIN`` -- one of
    the plan's conditions for going higher. With it comes how late the loop
    woke just before that refill, which says whether the dip was the
    operating system or the refill itself; and how many refills were thin,
    which says whether it was one event or a habit.

    CM51, the loop-lag probe: the ticker is a 10 ms timer on the loop, and
    how late each fires is how long the loop was held. The worst holds are
    reported with when they came and any call into the adapter that was
    running across them -- a write, or a space check that asked
    ``is_task_done`` -- so a hold like the 57 ms one seen in the core review
    can be put down to the driver or to something else in the process."""
    ticks: list[tuple[float, float]] = []

    async def ticker(ref, output):
        while True:
            t = time.monotonic()
            await asyncio.sleep(0.01)
            ticks.append((time.monotonic(), time.monotonic() - t))

    run = await drive(BUFFER_SAMPLES, main, SOAK_SECONDS, ticker)

    writes = run.output.writes
    queued = np.array([q for _, _, q, _ in writes]) / RATE
    dry = f"RAN DRY at t = {run.underflow.at:.3f} s" if run.underflow else "no gap"
    if len(queued):
        worst = int(np.argmin(queued))
        at = writes[worst][0]
        held = max((lag for t, lag in ticks if at - 0.1 <= t <= at + 0.02), default=0.0)
        thin = int(np.sum(queued < THIN))
        flag = f" -- BELOW {THIN * 1000:.0f} ms" if queued.min() < THIN else ""
        report(f"{ms(BUFFER_SAMPLES)} buffer, {main / RATE * 1000:.0f} ms volume: loop holds "
               + holds(ticks, run.output))
        report(f"{ms(BUFFER_SAMPLES)} buffer, {main / RATE * 1000:.0f} ms volume, {SOAK_SECONDS:.0f} s: "
               f"{dry}; {len(queued)} refills, queued at a refill {queued.min() * 1000:.1f} ms at "
               f"least{flag}, "
               f"1st percentile {np.percentile(queued, 1) * 1000:.1f} ms, median "
               f"{np.median(queued) * 1000:.1f} ms; {thin} refills below {THIN * 1000:.0f} ms; "
               f"the thinnest came {at - run.output.playing:.1f} s in, after the loop was held "
               f"{held * 1000:.0f} ms; loop held up to {run.lag * 1000:.0f} ms overall; "
               f"CPU {run.load:.1%} of one core")
    else:
        report(f"{ms(BUFFER_SAMPLES)} buffer, {main / RATE * 1000:.0f} ms volume: {dry}, no refills")
    assert run.underflow is None, f"the instrument's depth ran dry: {dry}"


def holds(ticks: list[tuple[float, float]], output: Watched, worst: int = 5) -> str:
    """The loop-lag probe's findings (CM51): how the holds are spread, and
    the worst few, each with when it came and the adapter calls that ran
    across it."""
    if not ticks:
        return "none measured"
    late = np.array([lag for _, lag in ticks]) - 0.01
    lines = [
        f"{len(ticks)} ticks; late by {np.median(late) * 1000:.1f} ms median, "
        f"{np.percentile(late, 99) * 1000:.1f} ms at the 99th percentile; "
        f"{int(np.sum(late > THIN))} over {THIN * 1000:.0f} ms"
    ]
    for end, lag in sorted(ticks, key=lambda tick: tick[1], reverse=True)[:worst]:
        start = end - lag
        across = [
            f"{name} {took * 1000:.0f} ms"
            for began, name, took in output.slow
            if began < end and began + took > start
        ]
        lines.append(
            f"{(lag - 0.01) * 1000:.0f} ms at {start - output.playing:.1f} s"
            f" ({', '.join(across) or 'no adapter call'})"
        )
    return "; ".join(lines)


# --- H10: the last digital samples, seen at a pin ------------------------------------

FEEDBACK = os.environ.get("STACSCOPE_FEEDBACK", "")
"""The PFI line P0.1 is wired to, for H10 (on this rig, PFI9). Unset, H10
skips: without the wire it would count nothing and report that as lost."""

PULSE = 4
"""High for PULSE samples, low for PULSE: one rising edge every 2 * PULSE."""

TAIL = 256
"""The pulses fill only the last TAIL samples, so a count short by k means
the last k * 2 * PULSE samples never reached the pin."""


def tail_pulses(n: int):
    """``n`` samples, all 0 V and low, except P0.1 (DO index 1), which pulses
    through the last ``TAIL`` and ends low."""
    ao, do = zeros(n)
    phase = np.arange(TAIL) % (2 * PULSE)
    do[1, n - TAIL:] = phase < PULSE
    return ao, do


def edge_counter(counter: str, edge):
    import nidaqmx
    from nidaqmx.constants import Edge

    task = nidaqmx.Task()
    channel = task.ci_channels.add_ci_count_edges_chan(
        f"{DEVICE}/{counter}", edge=Edge.RISING if edge == "rising" else Edge.FALLING)
    channel.ci_count_edges_term = f"/{DEVICE}/{FEEDBACK}"
    return task


def park_p01_low() -> None:
    """Drive P0.1 low on demand, whatever the test left it at."""
    import nidaqmx

    with nidaqmx.Task() as task:
        task.do_channels.add_do_chan(f"{DEVICE}/port0/line1")
        task.write(False)


@physical_only
@pytest.mark.skipif(not FEEDBACK, reason="set STACSCOPE_FEEDBACK to the PFI line P0.1 is wired to")
@pytest.mark.parametrize(
    ("buffer", "total"),
    [(BUFFER, BUFFER), (BUFFER, 57_000), (5_000, 57_000), (5_000, 57_037), (5_000, 60_000),
     (BUFFER, 150_000)],
    ids=["never-wraps", "never-wraps-partial", "wraps", "wraps-odd-length",
         "wraps-ends-on-a-wrap", "wraps-long-buffer"],
)
async def test_h10_the_last_digital_samples_reach_the_pin(report, buffer, total):
    """H10. Whether the digital task plays its last samples when output ends,
    which H1's survey left open: kept fed and then left, the DO task's free
    space stopped 64 short of its whole buffer and its count 64 short of what
    was written, and it raised before the analog task. If the tail of
    COOLDOWN does not play, a trigger or laser line is left at whatever it
    held before -- a safety question, not only a timing one.

    P0.1 pulses through the last TAIL samples and ends low. Two counters on
    the wired PFI line count its rising and falling edges. All of them
    counted means every sample played; a shortfall says how many did not; a
    rising count above the falling one means the line was left high. The
    output ends the way an acquisition does, by ``drain``.

    The cases are an output that fits the host buffer, and one that
    wraps it -- ending part-way through the buffer, at an odd length, and
    exactly on a wrap. First run on Dev1 (2026-09-23): an output that
    wrapped and ended part-way lost its last 64 DO samples, plus the end's
    offset from a multiple of 4, at 5000 and at 100 000 samples of buffer.
    One that never wrapped, or ended exactly on a wrap, lost none. Written
    padding of at least that many samples brought every edge back, so
    ``drain`` now pads (TAIL_PAD); with it, every case passed.

    Fails if an edge is missing: the port promises everything written is
    played once."""
    expected = TAIL // (2 * PULSE)
    output = NiSignalOutput(MonotonicClock(), ao_task=AO_TASK, do_task=DO_TASK)
    rising, falling = edge_counter("ctr0", "rising"), edge_counter("ctr1", "falling")
    ao, do = tail_pulses(total)
    try:
        rising.start()
        falling.start()
        await output.configure(config(buffer))
        await output.arm()
        at = min(buffer, total)
        await output.write(ao[:, :at], do[:, :at])
        await output.go()
        while at < total:
            n = min(REFILL, total - at)
            await output.wait_for_space(n)
            await output.write(ao[:, at:at + n], do[:, at:at + n])
            at += n
        await output.drain()
        await asyncio.sleep(0.05)
        counts = int(rising.read()), int(falling.read())
        do_generated = generated(output._do)
    finally:
        with contextlib.suppress(Exception):
            await output.stop()
        await output.close()
        rising.close()
        falling.close()
        park_p01_low()

    report(f"{buffer} buffer, {total} samples: {counts[0]} rising and {counts[1]} falling edges of "
           f"{expected}; DO generated {do_generated}")
    assert counts == (expected, expected), (
        f"the last samples did not all reach the pin: {counts[0]} rising, {counts[1]} falling "
        f"of {expected} -- about {(expected - counts[1]) * 2 * PULSE} samples short"
        + ("; P0.1 was left high" if counts[0] > counts[1] else ""))
