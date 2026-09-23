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
        # digital one comes back DMA), and a refill loop at 100 kS/s is what
        # DMA exists for. Whether it changes anything measurable on the board
        # is H4.
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

    def space_available(self) -> int:
        self._open()
        with self._translated():
            return self._ao.out_stream.space_avail

    async def write(self, ao, do, *, label: str | None = None) -> None:
        """``label`` is for diagnostics on our side and never reaches the
        driver, which has no notion of where one segment ends.

        Sample values are passed through untouched. What an analog line drives
        and what it may safely be driven to are the instrument's business, set
        up in NI MAX, and opaque from here."""
        samples = ao.shape[1]
        if samples > self.space_available():
            raise ValueError(
                f"{samples} samples do not fit in {self.space_available()} free"
            )
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
        # Ending means our samples have been generated -- or that the buffer
        # emptied, which the driver reports as an underflow and which is
        # exactly what a drain is waiting for.
        #
        # `wait_until_done` would also work, and is what the instrument has
        # always run: on a continuous task it returns by raising the
        # regeneration-stopped error once the buffer empties. It times out
        # against a simulated device, which stalls instead of running dry --
        # that is a property of the simulation, not of the board. Which of the
        # two this should be is still open.
        while True:
            try:
                played = task.out_stream.total_samp_per_chan_generated
            except DaqError as exc:
                if exc.error_code not in UNDERFLOW_CODES:
                    raise
                return
            if played >= self._written:
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
                failures.append(exc)
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
            raise Underflow(self._ao.out_stream.total_samp_per_chan_generated
                            / self._config.rate) from exc


def read_output_config(
    ao_task: str, do_task: str, *, buffer_samples: int, system=None
) -> OutputConfig:
    """The ``OutputConfig`` that the tasks saved in NI MAX describe: their
    channels in MAX's order, labelled with MAX's channel names, at the analog
    task's rate. ``buffer_samples`` is the caller's, since MAX does not hold
    it. Both tasks are released again before this returns. Raises
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
    """Drop the channel axis for a one-channel task.

    DAQmx infers the channel count from the array it is handed and refuses a
    two-dimensional one for a task with a single channel. The port speaks
    channels x n whatever the count, so the difference is absorbed here.
    """
    return samples[0] if samples.shape[0] == 1 else samples

