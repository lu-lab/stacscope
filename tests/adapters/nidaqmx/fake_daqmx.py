"""A stand-in for ``nidaqmx.Task``, recording what the adapter did to it.

What this can and cannot prove, stated plainly because it is easy to forget:
it pins *our* call sequence against the API as it is documented. It cannot
prove the driver accepts that sequence -- that is 5a-2, against an NI MAX
simulated device -- and it cannot prove the hardware behaves, which only the
instrument can. Every attribute and method here was checked against the
installed ``nidaqmx`` 1.6.0 rather than recalled, since a double that invents
API names would pass happily and fail on the bench.
"""

from nidaqmx.constants import AcquisitionType, Edge, LineGrouping, RegenerationMode


class FakeDaq:
    """Hands out tasks and keeps them, with one log ordering events across all
    of them -- which is how start order between master and slave is checked."""

    def __init__(self, *, space=0):
        self.tasks: list[FakeTask] = []
        self.log: list[tuple[str, str]] = []
        self.space = space

    def __call__(self, new_task_name: str = "") -> "FakeTask":
        task = FakeTask(new_task_name, self)
        self.tasks.append(task)
        return task

    def task(self, name: str) -> "FakeTask":
        for task in self.tasks:
            if task.name.endswith(name):
                return task
        raise AssertionError(f"no task named {name!r} in {[t.name for t in self.tasks]}")

    def starts(self) -> list[str]:
        return [name for event, name in self.log if event == "start"]


class FakeTask:
    def __init__(self, name: str, daq: FakeDaq) -> None:
        self.name = name
        self._daq = daq
        self.ao_channels = _AoChannels()
        self.do_channels = _DoChannels()
        self.timing = _Timing()
        self.out_stream = _OutStream(self)
        self.started = False
        self.closed = False
        self.waited: float | None = None
        self.written: list = []
        self.auto_started: list = []
        self.fail: Exception | None = None
        """Set to a DaqError to make this task fail the way the driver would."""

    def write(self, data, auto_start=None, timeout=10.0):
        if self.fail is not None:
            raise self.fail
        self.written.append(data)
        self.auto_started.append(auto_start)
        self._daq.log.append(("write", self.name))
        return len(data[0])

    def start(self) -> None:
        self.started = True
        self._daq.log.append(("start", self.name))

    def stop(self) -> None:
        if self.fail is not None:
            raise self.fail  # the driver refused; this task's state is anyone's guess
        self.started = False
        self._daq.log.append(("stop", self.name))

    def close(self) -> None:
        self.closed = True
        self._daq.log.append(("close", self.name))

    def wait_until_done(self, timeout: float = 10.0) -> None:
        self.waited = timeout
        self._daq.log.append(("wait", self.name))
        if self.fail is not None:
            raise self.fail


class _AoChannels:
    def __init__(self) -> None:
        self.added: list[str] = []

    def add_ao_voltage_chan(self, physical_channel, name_to_assign_to_channel="",
                            min_val=-10.0, max_val=10.0):
        self.added.append(physical_channel)


class _DoChannels:
    def __init__(self) -> None:
        self.added: list[str] = []
        self.grouping: LineGrouping | None = None

    def add_do_chan(self, lines, name_to_assign_to_lines="",
                    line_grouping=LineGrouping.CHAN_FOR_ALL_LINES):
        self.added.append(lines)
        self.grouping = line_grouping


class _Timing:
    def __init__(self) -> None:
        self.rate: float | None = None
        self.source: str | None = None
        self.sample_mode: AcquisitionType | None = None
        self.samps_per_chan: int | None = None

    def cfg_samp_clk_timing(self, rate, source="", active_edge=Edge.RISING,
                            sample_mode=AcquisitionType.FINITE, samps_per_chan=1000):
        self.rate = rate
        self.source = source
        self.sample_mode = sample_mode
        self.samps_per_chan = samps_per_chan


class _OutStream:
    """Enough of ``task.out_stream`` for the adapter: the two settings the
    refill design rests on, plus the counters it reads back."""

    def __init__(self, task: "FakeTask") -> None:
        self._task = task
        self.regen_mode: RegenerationMode | None = None
        self.output_buf_size: int | None = None
        self.auto_start: bool | None = None
        self.total_samp_per_chan_generated = 0

    @property
    def space_avail(self) -> int:
        if self._task.fail is not None:
            raise self._task.fail
        return self._task._daq.space
