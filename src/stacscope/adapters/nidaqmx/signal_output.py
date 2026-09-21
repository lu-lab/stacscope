"""The ``SignalOutput`` port over NI-DAQmx.

Everything NI-specific lives here: which task is master, where the digital
lines get their clock, and which driver error codes mean the output gapped.
The port itself knows none of it.

The board is the rig's sole timing authority (thesis 3.4.1), so the AO task
runs on its own onboard clock and the DO task is slaved to it. Regeneration is
disabled, which is what makes a sample play exactly once: fall behind and the
driver reports a gap rather than silently replaying the last block. That single
constraint is why the caller is a refill loop.
"""

from typing import TYPE_CHECKING

from nidaqmx.constants import AcquisitionType, LineGrouping, RegenerationMode

if TYPE_CHECKING:
    from stacscope.core.ports import Clock, OutputConfig

AO_TASK = "stacscope_ao"
DO_TASK = "stacscope_do"


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
        self._ao = None
        self._do = None

    async def configure(self, config: "OutputConfig") -> None:
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

    async def arm(self) -> None:
        self._written = 0

    def space_available(self) -> int:
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
        self._ao.write(ao, auto_start=False)
        self._do.write(do, auto_start=False)
        self._written += samples

    async def go(self) -> None:
        if not self._written:
            raise RuntimeError("nothing has been written: there is nothing to play")
        # The slave first. It takes its clock from the master's output, so it
        # has to be waiting before the master produces an edge.
        self._do.start()
        self._ao.start()

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
