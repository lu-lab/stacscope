"""The ``SignalOutput`` port over NI-DAQmx.

Everything NI-specific lives here: which task is master, where the digital
lines get their clock, and which driver error codes mean the output gapped.
The port itself knows none of it.

The board is the rig's sole timing authority, so the AO task runs on its own
onboard clock and the DO task is slaved to it. Regeneration is disabled,
which is what makes a sample play exactly once: fall behind and the driver
reports a gap rather than silently replaying the last block. That single
constraint is why the caller is a refill loop.
"""

import contextlib
from typing import TYPE_CHECKING

from nidaqmx.constants import AcquisitionType, LineGrouping, RegenerationMode, TaskMode
from nidaqmx.error_codes import DAQmxErrors
from nidaqmx.errors import DaqError

from stacscope.core.ports import Underflow

if TYPE_CHECKING:
    from stacscope.core.ports import Clock, OutputConfig

AO_TASK = "stacscope_ao"
DO_TASK = "stacscope_do"

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
    def __init__(self, clock: "Clock", *, tasks=None) -> None:
        """``tasks`` is the task factory, ``nidaqmx.Task`` unless a test
        supplies a stand-in. Imported lazily so this module can be read
        without a driver present."""
        if tasks is None:
            import nidaqmx

            tasks = nidaqmx.Task
        self._clock = clock
        self._tasks = tasks
        self._config: OutputConfig | None = None
        self._written = 0
        self._playing = False
        self._draining = False
        self._ao = None
        self._do = None
        self._closed = False

    async def configure(self, config: "OutputConfig") -> None:
        self._open()
        self._config = config
        device = config.ao[0].physical.split("/")[0]

        self._ao = self._tasks(new_task_name=AO_TASK)
        for channel in config.ao:
            self._ao.ao_channels.add_ao_voltage_chan(channel.physical)

        self._do = self._tasks(new_task_name=DO_TASK)
        for channel in config.do:
            self._do.do_channels.add_do_chan(
                channel.physical, line_grouping=LineGrouping.CHAN_PER_LINE
            )

        # The AO task owns the clock; the DO task follows it off the same
        # terminal, so an edge keeps its phase against the waveform.
        self._configure_timing(self._ao, config, source="")
        self._configure_timing(self._do, config, source=f"/{device}/ao/SampleClock")

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

    @staticmethod
    def _configure_timing(task, config: "OutputConfig", *, source: str) -> None:
        task.timing.cfg_samp_clk_timing(
            config.rate,
            source=source,
            sample_mode=AcquisitionType.CONTINUOUS,
            samps_per_chan=config.buffer_samples,
        )
        task.out_stream.regen_mode = RegenerationMode.DONT_ALLOW_REGENERATION
        task.out_stream.output_buf_size = config.buffer_samples


def _for_driver(samples):
    """Drop the channel axis for a one-channel task.

    DAQmx infers the channel count from the array it is handed and refuses a
    two-dimensional one for a task with a single channel. The port speaks
    channels x n whatever the count, so the difference is absorbed here.
    """
    return samples[0] if samples.shape[0] == 1 else samples
