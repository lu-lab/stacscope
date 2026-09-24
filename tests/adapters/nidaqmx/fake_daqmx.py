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

import ctypes
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
        self.do_space: int | None = None
        """The digital task's free space, when it differs from the analog
        task's. On the physical board it often does, and is the smaller (Step 3,
        refill experiments): its onboard FIFO is shallower."""
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

    def ran_dry(self, error: DaqError, *, space: int = 0) -> None:
        """Put the pair in the state the physical PCI-6733 reported once its
        output had run dry (Step 3, H1's survey): the analog task reports it
        from ``is_task_done``, from a write, and from the first ``stop`` only;
        its free space reads 0 and its sample count 0 (the last value read
        before the error, and here nothing had read it), where the
        digital task's count is still right. Nothing reads as an error on the
        digital task.

        ``space`` is for the other order of events -- the driver noticing
        between the adapter asking for space and writing."""
        ao, do = self.task("ao"), self.task("do")
        ao.dry = error
        self.space = space
        do.out_stream.played = sum(data.shape[-1] for data in do.written)
        ao.out_stream.played = 0


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
        self.dry: DaqError | None = None
        """The driver's report that this task stopped generating because it
        ran out of samples. See ``FakeDaq.ran_dry``."""

    def write(self, data, auto_start=None, timeout=10.0):
        if self.fail is not None:
            raise self.fail
        if self.dry is not None:
            raise self.dry
        self._check_channels(data)
        if data.shape[-1] > self.out_stream.space_avail:
            # The real driver does not refuse this: it waits inside the call,
            # holding the event loop, until the board has played enough to make
            # room (Step 3, refill experiments). A fake cannot block, so it
            # fails the test instead.
            raise AssertionError(
                f"{self.name}: wrote {data.shape[-1]} samples into {self.out_stream.space_avail} "
                "free -- the driver would block the event loop here"
            )
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
        # And, found on Dev1-sim, the ctypes layer refuses an array that is not
        # C-contiguous -- such as a slice of a waveform's columns, which is
        # what a chunked write hands over -- before the driver sees it.
        if not data.flags.c_contiguous:
            raise ctypes.ArgumentError("argument 6: TypeError: array must have flags ['C_CONTIGUOUS']")
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
        if self.dry is not None:
            # As on the board: the stop takes, and reports the gap on its way
            # out. A second stop returns quietly.
            dry, self.dry = self.dry, None
            raise dry

    def is_task_done(self) -> bool:
        self._daq.log.append(("done?", self.name))
        if self.fail is not None:
            raise self.fail
        if self.dry is not None:
            raise self.dry
        return not self.started

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
        # The onboard FIFOs are the physical PCI-6733's, read off Dev1 with its
        # five analog channels and three lines (Step 3, refill experiments).
        onboard = 3276 if saved.kind == "ao" else 2047
        super().__init__(task, regen_mode=saved.regen, output_buf_size=0, played=0, fail=None,
                         output_onbrd_buf_size=onboard)
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
        daq = self._task._daq
        if daq.do_space is not None and self._task.do_channels:
            return daq.do_space
        return daq.space

    @property
    def total_samp_per_chan_generated(self) -> int:
        self._task._daq.log.append(("generated", self._task.name))  # logged as attempted
        if self.fail is not None:
            raise self.fail
        return self.played

    @total_samp_per_chan_generated.setter
    def total_samp_per_chan_generated(self, value: int) -> None:
        object.__setattr__(self, "played", value)
