"""A stand-in for ``nidaqmx.system.System``: tasks saved in NI MAX, loaded by
name, recording what the adapter did to them.

What this can and cannot prove, stated plainly because it is easy to forget:
it pins *our* call sequence against the API as it is documented. It cannot
prove the driver accepts that sequence -- that is 5a-2, against an NI MAX
simulated device -- and it cannot prove the hardware behaves, which only the
instrument can. Every attribute and method here was checked against the
installed ``nidaqmx`` 1.6.0 rather than recalled, since a double that invents
API names would pass happily and fail on the bench. What a *loaded* task
reports back (channel names without the task prefix, physical names without a
leading slash, a fully qualified clock source) was read off real persisted
tasks on Dev1-sim, not the documentation.

A loaded task deliberately has no way to add channels or configure timing:
since step 2 those belong to NI MAX, and an adapter that tried would fail here
with an AttributeError rather than pass.
"""

from dataclasses import dataclass
from types import SimpleNamespace

from nidaqmx.constants import AcquisitionType, RegenerationMode, TaskMode
from nidaqmx.error_codes import DAQmxErrors
from nidaqmx.errors import DaqError


@dataclass
class Saved:
    """A task as saved in NI MAX. Tests edit one before loading it to make the
    rig disagree with the configuration."""

    kind: str  # "ao" or "do"
    channels: list[tuple[str, str]]  # (MAX channel name, physical channel), in MAX's order
    rate: float
    source: str
    mode: AcquisitionType = AcquisitionType.CONTINUOUS
    regen: RegenerationMode = RegenerationMode.DONT_ALLOW_REGENERATION


class FakeDaq:
    """The system: what is saved, what has been loaded, and one log ordering
    events across every task -- which is how start order between master and
    slave is checked."""

    def __init__(self, *, space=0):
        self.saved: dict[str, Saved] = {}
        self.loaded: list[FakeTask] = []
        self.log: list[tuple[str, str]] = []
        self.space = space
        self.tasks = _PersistedTasks(self)

    def saving(self, config, *, ao: str, do: str) -> "FakeDaq":
        """Save an AO and a DO task in MAX exactly as ``config`` describes
        them, set up the way the instrument's are."""
        device = config.ao[0].physical.split("/")[0]
        self.saved[ao] = Saved("ao", [(c.label, c.physical) for c in config.ao],
                               config.rate, f"/{device}/ao/SampleClockTimebase")
        self.saved[do] = Saved("do", [(c.label, c.physical) for c in config.do],
                               config.rate, f"/{device}/ao/SampleClock")
        return self

    def task(self, name: str) -> "FakeTask":
        for task in self.loaded:
            if task.name.endswith(name):
                return task
        raise AssertionError(f"no task named {name!r} in {[t.name for t in self.loaded]}")

    def starts(self) -> list[str]:
        return [name for event, name in self.log if event == "start"]


class _PersistedTasks:
    """``System.tasks``. As in the real collection, indexing by a name that
    was never saved does not fail -- only loading it does."""

    def __init__(self, daq: FakeDaq) -> None:
        self._daq = daq

    @property
    def task_names(self) -> list[str]:
        return list(self._daq.saved)

    def __contains__(self, name: str) -> bool:
        return name in self._daq.saved

    def __getitem__(self, name: str) -> "_PersistedTask":
        return _PersistedTask(name, self._daq)


class _PersistedTask:
    def __init__(self, name: str, daq: FakeDaq) -> None:
        self.name = name
        self._daq = daq

    def load(self) -> "FakeTask":
        saved = self._daq.saved.get(self.name)
        if saved is None:
            raise DaqError("Task specified is invalid or does not exist.",
                           DAQmxErrors.INVALID_TASK)
        if any(t.name == self.name and not t.closed for t in self._daq.loaded):
            raise DaqError("Specified task name is already in use.",
                           DAQmxErrors.DUPLICATE_TASK)
        task = FakeTask(self.name, saved, self._daq)
        self._daq.loaded.append(task)
        self._daq.log.append(("load", self.name))
        return task


class _Recorded:
    """Part of a task whose attribute writes are recorded on the task, so a
    test can see every setting the adapter changed and nothing else."""

    def __init__(self, task: "FakeTask", **values) -> None:
        object.__setattr__(self, "_task", task)
        for name, value in values.items():
            object.__setattr__(self, name, value)

    def __setattr__(self, name, value):
        self._task.changed[name] = value
        object.__setattr__(self, name, value)


class FakeTask:
    def __init__(self, name: str, saved: Saved, daq: FakeDaq) -> None:
        self.name = name
        self._daq = daq
        self.changed: dict[str, object] = {}
        """Every driver setting written since loading, by attribute name."""
        channels = [
            SimpleNamespace(name=label, physical_channel=SimpleNamespace(name=physical))
            for label, physical in saved.channels
        ]
        self.ao_channels = _Channels(self, channels if saved.kind == "ao" else [], "ao")
        self.do_channels = _Channels(self, channels if saved.kind == "do" else [], "do")
        self.channel_names = [c.name for c in channels]
        self.timing = _Recorded(self, samp_clk_rate=saved.rate, samp_clk_src=saved.source,
                                samp_quant_samp_mode=saved.mode)
        self.out_stream = _OutStream(self, saved)
        self.started = False
        self.closed = False
        self.committed = False
        self.waited: float | None = None
        self.written: list = []
        self.auto_started: list = []
        self.fail: Exception | None = None
        """Set to a DaqError to make this task fail the way the driver would."""

    def write(self, data, auto_start=None, timeout=10.0):
        if self.fail is not None:
            raise self.fail
        self._check_channels(data)
        self.written.append(data)
        self.auto_started.append(auto_start)
        self._daq.log.append(("write", self.name))
        return data.shape[-1]

    def _check_channels(self, data) -> None:
        """DAQmx works out the channel count from the array's shape, and
        refuses a two-dimensional array for a task holding one channel.

        Modelled because it is invisible without a driver: a double that takes
        any shape passes a single-channel configuration the instrument then
        rejects, and one channel of analog out with one digital line is the
        smallest real configuration there is.
        """
        channels = len(self.channel_names)
        given = data.shape[0] if data.ndim == 2 else 1
        if (data.ndim == 2) != (channels > 1) or given != channels:
            raise DaqError(
                f"data has {given} channels and the task has {channels}",
                DAQmxErrors.WRITE_NUM_CHANS_MISMATCH,
            )

    def control(self, mode: TaskMode) -> None:
        self.committed = mode is TaskMode.TASK_COMMIT
        self._daq.log.append(("control", self.name))

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


class _Channels:
    """``task.ao_channels`` / ``task.do_channels``: iterable in MAX's order,
    and ``all`` for a property set on every channel at once."""

    def __init__(self, task: FakeTask, channels: list, kind: str) -> None:
        self._channels = channels
        self.all = _Recorded(task, **{f"{kind}_data_xfer_mech": None})

    def __iter__(self):
        return iter(self._channels)

    def __len__(self) -> int:
        return len(self._channels)


class _OutStream(_Recorded):
    """Enough of ``task.out_stream`` for the adapter: the settings the refill
    design rests on, plus the counters it reads back."""

    def __init__(self, task: FakeTask, saved: Saved) -> None:
        super().__init__(task, regen_mode=saved.regen, output_buf_size=0, played=0, fail=None)
        # `played`: samples the task has generated. Tests advance it by hand:
        # the fake has no clock, and a drain is exactly the wait for this to
        # catch up.
        # `fail`: set to a DaqError to fail reads of the generated counter.
        # Separate from the task's own switch because the adapter reads this
        # counter *while handling* a failed write, to say when the output ran
        # dry.

    @property
    def space_avail(self) -> int:
        if self._task.fail is not None:
            raise self._task.fail
        return self._task._daq.space

    @property
    def total_samp_per_chan_generated(self) -> int:
        self._task._daq.log.append(("generated", self._task.name))  # logged as attempted
        if self.fail is not None:
            raise self.fail
        return self.played

    @total_samp_per_chan_generated.setter
    def total_samp_per_chan_generated(self, value: int) -> None:
        object.__setattr__(self, "played", value)
