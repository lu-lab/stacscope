"""The ``SignalOutput`` port over NI-DAQmx.

Everything NI-specific lives here: which task is master, where the digital
lines get their clock, and which driver error codes mean the output gapped.
The port itself knows none of it.

The output tasks are the instrument's, saved in NI MAX: one analog output task
and one digital output task, named when the adapter is built. MAX owns their
channels, rate and clock routing. The adapter loads them, checks them against
the configuration it is given, and refuses to run if they disagree -- so a
change to the rig is made in one place, NI MAX, and a stale configuration is
caught at setup rather than mid-acquisition. read_output_config reads the
configuration straight from the saved tasks, so it need not be written twice.

The saved tasks must be set up the way this adapter needs, and it checks that
too. The board is the rig's sole timing authority, so the analog task runs on
the board's own clock and the digital task takes its clock from
ao/SampleClock. Both run continuously with regeneration disabled, which is
what makes a sample play exactly once: fall behind and the driver reports a
gap rather than silently replaying the last block.
"""

import contextlib
from typing import TYPE_CHECKING

import numpy as np
from nidaqmx.constants import (
    AcquisitionType,
    DataTransferActiveTransferMode,
    RegenerationMode,
    TaskMode,
)
from nidaqmx.error_codes import DAQmxErrors
from nidaqmx.errors import DaqError

from stacscope.core.ports import Channel, OutputConfig, Underflow

if TYPE_CHECKING:
    from stacscope.core.ports import Clock

POLL = 0.001
"""How often to ask the driver for space. Through the injected clock, so a test
pays nothing for it and the instrument pays one millisecond."""

DRAIN_MARGIN = 1.0
"""Seconds added to the buffer's own playing time before a drain gives up. A
drain that hangs would hold the whole shutdown path."""

BUFFER_SAMPLES = 5000
"""The instrument's host buffer depth per channel, used by
``read_output_config`` when the caller names none: 50 ms at 100 kS/s. A
deeper buffer rides out longer stalls and delays every update by as much."""
# Chosen by the user, 2026-09-23, from the Step 3 measurements on Dev1 through
# WaveformSource. An update reaches the pins one lead after it is accepted (the
# host buffer plus ~20 ms in the board's FIFO), and a stall of the event loop
# survives if it is shorter than what is queued at the thinnest (about 3/4 of
# the host buffer, since refills top up a quarter at a time, plus that FIFO):
#   3276 (the floor)  lead ~39 ms   thinnest 38 ms   30 ms survives, 40 runs dry
#   5000              lead ~61 ms   thinnest 58 ms   50 ms survives, 60 runs dry
#   10000             lead ~119 ms  not measured, ~90 ms expected
# The floor leaves 30 ms against a 16 ms Windows timer step, and a 60 s soak
# there dipped to ~16 ms queued. 5000 costs ~17 ms of latency for 20 ms more
# tolerance. Go higher (10000) if any of these turns up:
#   - WaveformSource shares its event loop with other actors, and H5 or H7 run
#     in that process reports the loop held for more than ~40 ms (a long
#     handler, a file write, a GC pause);
#   - a long soak at 5000 has its thinnest queue below ~20 ms, or runs dry;
#   - is_task_done moves back onto the loop at a fixed rate (it holds it for
#     up to 37 ms), or AO goes back to interrupt transfer (10-20 ms less
#     tolerance on the raw probe);
#   - the rig drops to three AO channels or fewer: the onboard FIFO is 16384
#     samples shared by the channels, so the floor rises to 5461 and configure
#     refuses 5000;
#   - the rate changes: this is a sample count, so rescale it to keep 50 ms.

UNDERFLOW_CODES = frozenset({
    DAQmxErrors.GEN_STOPPED_TO_PREVENT_REGEN_OF_OLD_SAMPLES,
    DAQmxErrors.OUTPUT_FIFO_UNDERFLOW,
    DAQmxErrors.OUTPUT_FIFO_UNDERFLOW_2,
    DAQmxErrors.DAC_UNDERFLOW,
})
"""The driver's several ways of saying the output ran dry. Nothing else is an
underflow: reporting another fault as one would send a caller off refilling
harder at a board that has, say, overheated."""
# The regeneration one is first because it is the one this rig actually meets.
# With regeneration disabled the driver stops the generation rather than replay
# old samples, and reports that -- so it is the ordinary consequence of a late
# refill, and equally the ordinary way a drained buffer announces it is empty.
# The other three are the board failing to fetch in time, which is rarer and
# further down.


class NiSignalOutput:
    def __init__(self, clock: "Clock", *, ao_task: str, do_task: str, system=None) -> None:
        """``ao_task`` and ``do_task`` name the analog and digital output tasks
        saved in NI MAX. Nothing is loaded, and the board is not reserved,
        until ``configure``. ``system`` is ``nidaqmx.system.System.local()``
        unless a test supplies a stand-in."""
        self._clock = clock
        self._ao_task = ao_task
        self._do_task = do_task
        self._system = system
        self._config: OutputConfig | None = None
        self._written = 0
        self._playing = False
        self._draining = False
        self._reported = False
        self._last_generated = 0
        self._ao = None
        self._do = None
        self._closed = False

    async def configure(self, config: OutputConfig) -> None:
        """Load both tasks from NI MAX, check them against ``config``, and
        reserve the board.

        Raises ``LookupError`` naming a task NI MAX does not have, before
        anything is loaded, and ``ValueError`` listing every way the saved
        tasks differ from ``config``. After a ``ValueError`` the tasks stay
        loaded until ``close``, which is safe to call whatever happened here."""
        self._open()
        self._config = config
        system = self._system or _local_system()
        _require(system, self._ao_task, self._do_task)
        self._ao = system.tasks[self._ao_task].load()
        self._do = system.tasks[self._do_task].load()

        # Checked, never imposed: overriding MAX here would have the rig's
        # owner edit one thing and get another, and quietly "fixing" a
        # transposed pair of scanners is worse than refusing to run.
        problems = _differences(self._ao, self._do, config)
        if problems:
            raise ValueError(
                "the tasks saved in NI MAX do not match the configuration:\n  "
                + "\n  ".join(problems)
            )

        # The two things MAX does not own. DMA because a saved analog output
        # task comes back POLLED (read off the instrument's own task, where the
        # digital one comes back DMA), and the PCI-6733 refuses programmed I/O
        # for buffered analog output at commit, -200767 (Step 3, H4). So this
        # is required, not tuning.
        self._ao.ao_channels.all.ao_data_xfer_mech = DataTransferActiveTransferMode.DMA
        self._do.do_channels.all.do_data_xfer_mech = DataTransferActiveTransferMode.DMA
        for task in (self._ao, self._do):
            task.out_stream.output_buf_size = config.buffer_samples

        # Committing reserves and programs the board once, here, rather than
        # on every start/stop cycle -- which is what the LabVIEW engine does
        # for this board, and what keeps an arm from paying for it. It is also
        # what makes the buffer's free space readable at all: an uncommitted
        # task answers that question with an error, and the refill loop's
        # first act is to ask it.
        for task in (self._ao, self._do):
            task.control(TaskMode.TASK_COMMIT)

    async def arm(self) -> None:
        self._open()
        self._written = 0
        self._playing = False
        self._draining = False
        self._reported = False

    def space_available(self) -> int:
        self._open()
        with self._translated():
            # Both buffers, not only the master's: a write that does not fit
            # the digital task's buffer waits inside the driver for room, and
            # holds the event loop while it does. On the PCI-6733 the digital
            # buffer is usually the fuller one -- its onboard FIFO is 2047
            # samples to the analog task's 3276, so less has drained out of it.
            # Asking only the analog task, writes blocked 5-12 ms each and
            # starved every other coroutine for whole cycles (Step 3, refill
            # experiments).
            space = min(self._ao.out_stream.space_avail, self._do.out_stream.space_avail)
            # A PCI-6733 that has run dry reads 0 here, with no error, for as
            # long as it is left (Step 3, H1) -- which is also what a full
            # buffer reads. Only `is_task_done` raises the ran-dry error, but
            # on Dev1 it costs 16 ms at the median and 37 at worst, holding the
            # event loop, against 0.03 ms for the generated count. Asked on
            # every full poll, it spent most of the ~47 ms a minimum-size
            # buffer holds. So it is asked only when the count has stopped
            # moving since the last full poll: a count still advancing cannot
            # have run dry. After running dry the analog count reads 0 (H1),
            # which differs from the last reading once and then stands still,
            # so the report comes one poll late -- a millisecond.
            #
            # If this ever misses a gap, the alternatives weighed were: asking
            # `is_task_done` from a worker thread, off the event loop (costs
            # nothing on the loop, but the answer arrives late and the thread
            # needs a lifetime); or at a fixed low rate, say every 50 ms (one
            # 16 ms stall per interval, and a gap reported up to that late).
            if space == 0 and self._playing:
                generated = self._ao.out_stream.total_samp_per_chan_generated
                if generated == self._last_generated:
                    self._ao.is_task_done()
                self._last_generated = generated
            return space

    async def write(self, ao, do, *, label: str | None = None) -> None:
        """``label`` is for diagnostics on our side and never reaches the
        driver, which has no notion of where one segment ends.

        Sample values are passed through untouched. What an analog line drives
        and what it may safely be driven to are the instrument's business, set
        up in NI MAX, and opaque from here."""
        samples = ao.shape[1]
        space = self.space_available()  # raises Underflow first, if that is why there is no room
        if samples > space:
            raise ValueError(f"{samples} samples do not fit in {space} free")
        # Never auto-start: the first write must not be what starts the AO
        # task, or it would clock out before the digital lines are waiting.
        with self._translated():
            self._ao.write(_for_driver(ao), auto_start=False)
            self._do.write(_for_driver(do), auto_start=False)
        self._written += samples

    async def go(self) -> None:
        self._open()
        if not self._written:
            raise RuntimeError("nothing has been written: there is nothing to play")
        # The slave first. It takes its clock from the master's output, so it
        # has to be waiting before the master produces an edge.
        self._do.start()
        self._ao.start()
        self._playing = True
        self._last_generated = 0

    async def wait_for_space(self, n: int) -> None:
        while (space := self.space_available()) < n:
            if not self._playing:
                raise RuntimeError(
                    f"{space} samples free and nothing is playing: no more will free"
                )
            await self._clock.sleep(POLL)

    async def drain(self) -> None:
        """Everything written plays out. From here running dry is the ending
        we asked for, so an underflow reported during it is not a fault."""
        self._open()
        self._draining = True
        timeout = self._config.buffer_samples / self._config.rate + DRAIN_MARGIN
        deadline = self._clock.now() + timeout
        # Each task is waited on separately. They share the AO sample clock,
        # but one of them running dry says nothing about the other having
        # finished, and a drain that skips the digital task leaves lines
        # asserted -- the half-stopped rig that `stop` goes to lengths to
        # avoid.
        for task in (self._ao, self._do):
            await self._play_out(task, deadline, timeout)

    async def _play_out(self, task, deadline: float, timeout: float) -> None:
        # Ending means our samples have been generated, or that the driver
        # reports the buffer emptied -- which it does as an underflow, and
        # which is exactly what a drain is waiting for. Either alone falls
        # short (Step 3, H2, on the physical PCI-6733):
        # - its analog task's count stops short of what was written, at
        #   99 985 of 100 000, and never gets there; only `is_task_done`
        #   raising the ran-dry error ends that task. The digital task, on
        #   the same sample clock, counts every sample, so the clock did
        #   tick for all of them.
        # - its digital task never reports running dry, and a simulated
        #   device stalls rather than run dry; the count ends those.
        # `wait_until_done`, which the legacy engine used and which ended
        # there by raising the ran-dry error, timed out instead on NI-DAQmx
        # 20.0 against this board.
        while True:
            try:
                played = task.out_stream.total_samp_per_chan_generated
                if played >= self._written or task.is_task_done():
                    return
            except DaqError as exc:
                if exc.error_code not in UNDERFLOW_CODES:
                    raise
                return
            if self._clock.now() >= deadline:
                raise TimeoutError(
                    f"{task.name} played {played} of {self._written} samples in {timeout} s"
                )
            await self._clock.sleep(POLL)

    async def stop(self) -> None:
        """Safe in any state, any number of times. Both tasks are stopped even
        if the first refuses: giving up halfway leaves half the rig running,
        which on this instrument means lines still asserted."""
        self._playing = False
        failures = []
        for task in (self._ao, self._do):
            if task is None:
                continue
            try:
                task.stop()
            except DaqError as exc:  # noqa: PERF203 - each task gets its own attempt
                if exc.error_code not in UNDERFLOW_CODES:
                    failures.append(exc)
                    continue
                # A board that ran dry says so once more here, and the stop
                # still takes (Step 3, H1). The gap is reported only if nobody
                # has heard of it: after a drain it is the ending asked for,
                # and after an Underflow it has been said. The legacy engine
                # drew the same line -- "Unexpected stops still propagate the
                # error."
                if not (self._draining or self._reported):
                    failures.append(self._underflow(exc))
        if failures:
            raise ExceptionGroup("the driver refused to stop a task", failures)

    async def close(self) -> None:
        """Release both tasks and the board. Safe in any state, any number of
        times, and the adapter is unusable afterwards. Raises an
        ``ExceptionGroup`` of the driver's refusals if either task would not
        close; both are attempted regardless."""
        # Until this runs the board is reserved against anything else wanting
        # it, and DAQmx refuses a second task under a name still taken, so an
        # adapter never closed can be built only once per process. The legacy
        # Stop Core.vi cleared both tasks on every exit path; the core's
        # teardown does the same by calling this.
        self._closed = True
        failures = []
        for task in (self._ao, self._do):
            if task is None:
                continue
            try:
                task.close()
            except DaqError as exc:  # noqa: PERF203 - each task gets its own attempt
                failures.append(exc)
        self._ao = self._do = None
        if failures:
            raise ExceptionGroup("the driver refused to close a task", failures)

    def _open(self) -> None:
        if self._closed:
            raise RuntimeError("the output has been closed")

    @contextlib.contextmanager
    def _translated(self):
        """Turn the driver's underflow codes into the port's one exception,
        and leave every other error exactly as the driver raised it."""
        try:
            yield
        except DaqError as exc:
            if exc.error_code not in UNDERFLOW_CODES:
                raise
            if self._draining:
                return  # running dry is what a drain is waiting for
            raise self._underflow(exc) from exc

    def _underflow(self, exc: DaqError) -> Underflow:
        # When it ran dry is what was written over the rate: with
        # regeneration off the board plays exactly that and stops. The sample
        # count is no use here -- the analog task's reads 0 once it has run
        # dry (Step 3, H1).
        self._reported = True
        underflow = Underflow(self._written / self._config.rate)
        underflow.__cause__ = exc
        return underflow


def read_output_config(
    ao_task: str, do_task: str, *, buffer_samples: int = BUFFER_SAMPLES, system=None
) -> OutputConfig:
    """The ``OutputConfig`` that the tasks saved in NI MAX describe: their
    channels in MAX's order, labelled with MAX's channel names, at the analog
    task's rate. ``buffer_samples`` is the caller's, since MAX does not hold
    it, and defaults to ``BUFFER_SAMPLES``. Both tasks are released again
    before this returns. Raises
    ``LookupError`` naming a task NI MAX does not have."""
    system = system or _local_system()
    _require(system, ao_task, do_task)
    with contextlib.ExitStack() as held:
        ao = system.tasks[ao_task].load()
        held.callback(ao.close)
        do = system.tasks[do_task].load()
        held.callback(do.close)
        return _read(ao, do, buffer_samples)


def _local_system():
    # Imported here so this module can be read without a driver present.
    import nidaqmx.system

    return nidaqmx.system.System.local()


def _require(system, *names: str) -> None:
    # Asked before loading anything. The driver's own answer comes only from
    # `load`, as a bare code -- indexing the saved tasks by an unknown name
    # succeeds -- and by then the other task may already be holding the board.
    for name in names:
        if name not in system.tasks:
            raise LookupError(
                f"NI MAX has no task named {name!r}. Create or import it in NI MAX, "
                "under Data Neighborhood > NI-DAQmx Tasks."
            )


def _read(ao, do, buffer_samples: int) -> OutputConfig:
    # MAX's order is kept exactly: it is the order of the caller's array rows,
    # and it is not the order of the physical lines (on the instrument Scan2
    # is ao3 and Scan3 is ao2), so sorting anywhere swaps two scanners.
    return OutputConfig(
        rate=ao.timing.samp_clk_rate,
        ao=tuple(Channel(c.physical_channel.name, c.name) for c in ao.ao_channels),
        do=tuple(Channel(c.physical_channel.name, c.name) for c in do.do_channels),
        buffer_samples=buffer_samples,
    )


def _differences(ao, do, config: OutputConfig) -> list[str]:
    """Every way the loaded tasks disagree with ``config``, or with what the
    refill design needs of them, as lines naming the task."""
    found = _read(ao, do, config.buffer_samples)
    problems = [
        f"{task.name} has channels {_listing(have)}; configured {_listing(wanted)}"
        for task, have, wanted in ((ao, found.ao, config.ao), (do, found.do, config.do))
        if have != wanted
    ]
    for task in (ao, do):
        if task.timing.samp_clk_rate != config.rate:
            problems.append(
                f"{task.name} runs at {task.timing.samp_clk_rate} S/s; configured {config.rate}"
            )
        if task.timing.samp_quant_samp_mode is not AcquisitionType.CONTINUOUS:
            problems.append(f"{task.name} is not set to continuous samples")
        if task.out_stream.regen_mode is not RegenerationMode.DONT_ALLOW_REGENERATION:
            problems.append(f"{task.name} allows regeneration")
        # A host buffer smaller than the board's own is not safe on the
        # PCI-6733, however promptly it is refilled: the driver refills the
        # board in blocks of about 2050 samples, and below its FIFO's size it
        # raised -50410 ("no more space in the buffer") with 15 ms still queued,
        # or ran dry (Step 3, refill experiments). The FIFO's size depends on
        # the channel count, so it is read, not assumed.
        onboard = task.out_stream.output_onbrd_buf_size
        if config.buffer_samples < onboard:
            problems.append(
                f"buffer_samples is {config.buffer_samples}, smaller than {task.name}'s onboard "
                f"buffer of {onboard} samples; it must be at least that"
            )
    # The source reads back fully qualified whatever form it was saved in
    # ("ao/SampleClock" in the export file). The device is the one the analog
    # task is actually on, so a rig moved to another board is reported once,
    # as channels, rather than again here.
    if found.ao:
        clock = f"/{found.ao[0].physical.split('/')[0]}/ao/SampleClock"
        if do.timing.samp_clk_src != clock:
            problems.append(f"{do.name} is clocked from {do.timing.samp_clk_src!r}, not {clock!r}")
    return problems


def _listing(channels: tuple[Channel, ...]) -> str:
    return "[" + ", ".join(f"{c.label}={c.physical}" for c in channels) + "]"


def _for_driver(samples):
    """Drop the channel axis for a one-channel task, and make the array
    contiguous.

    DAQmx infers the channel count from the array it is handed and refuses a
    two-dimensional one for a task with a single channel. The port speaks
    channels x n whatever the count, so the difference is absorbed here.
    nidaqmx's ctypes layer also refuses an array that is not C-contiguous,
    and a chunk of a segment -- columns sliced out of it -- is not. The copy
    that makes it so is what a contiguous array skips.
    """
    return np.ascontiguousarray(samples[0] if samples.shape[0] == 1 else samples)

