"""The rig: the instrument's engine, built from its config file.

    config = load_config("config/rig.toml")
    rig = await Rig.start(config)
    ...                       # tell rig.ref, the Coordinator, what to do
    await rig.close()

Rig.start launches the timing source and every device the file names, each
where the file places it, and returns once all of them are running. If any
cannot start, it raises naming the device and why, and shuts down what had
started. Rig.close stops an acquisition still running, then shuts everything
down, devices first.

The running rig says where things are: rig.ref is the Coordinator, to which
StartAcquisition and StopAcquisition go; await rig.status() is its state and
devices; await rig.device(name) is a device, for its own settings; await
rig.source() takes the waveforms; rig.buffers gives each streaming device's
image buffer name, which a viewer opens; and rig.line(rig.triggers[name]) is
the row of a device's trigger line in every digital waveform.

The NI source needs the "nidaqmx" extra, and reads the channels and rate
from the tasks saved in NI MAX when the rig starts.
"""

import asyncio
import contextlib
from collections.abc import Callable

from stacscope.app.config import ConfigError, DeviceConfig, RigConfig, SourceConfig, check_triggers
from stacscope.core.actors import Actor, ActorRef, AskTimeout, ask, describe, spawn
from stacscope.core.ports import Clock, MonotonicClock, OutputConfig
from stacscope.core.protocol import GetStatus, Status, StopAcquisition
from stacscope.core.timing import Coordinator, CoordinatorState, DeviceSpec, Launcher, Placement, WaveformSource
from stacscope.core.transport import LaunchFailed, ProcessHost

CLOSE_TIMEOUT = 30.0
"""Seconds ``close`` gives a running acquisition to stop, and the
Coordinator, with every device under it, to end, before going on without
it."""

STATUS_TIMEOUT = 5.0
"""Seconds the rig waits for the Coordinator to answer ``GetStatus``."""


class Rig:
    """A running rig. Made by ``Rig.start``; see the module's documentation."""

    def __init__(
        self,
        config: RigConfig,
        output: OutputConfig,
        coordinator: Coordinator,
        ref: ActorRef,
        host: ProcessHost | None,
        made: dict[str, Actor],
        clock: Clock,
    ) -> None:
        self.config = config
        self.output = output
        """The source's channels and rate. Waveform rows follow its order."""
        self.coordinator = coordinator
        self.ref = ref
        """The Coordinator."""
        self.host = host
        """The host of the devices in processes of their own, if there are any."""
        self.triggers = {d.name: d.trigger for d in config.devices if d.trigger is not None}
        """Each triggered device's trigger line, by label."""
        self.buffers = {d.name: d.options["buffer_name"] for d in config.devices if "buffer_name" in d.options}
        """Each streaming device's image buffer name."""
        self._made = made
        self._clock = clock

    @classmethod
    async def start(
        cls, config: RigConfig, *, clock: Clock | None = None, launch_timeout: float = 60.0
    ) -> "Rig":
        """Launch the rig ``config`` describes, on ``clock`` (the real one by
        default). Returns once the source and every device have started.

        Raises ``ConfigError`` if a device's trigger is not a line the source
        plays, and ``LaunchFailed`` naming any part that could not start or
        did not within ``launch_timeout`` seconds; what had started is shut
        down first."""
        clock = clock or MonotonicClock()
        output = _output(config.source)
        check_triggers(config.devices, output)
        bus = _bus(config.source, clock)
        made: dict[str, Actor] = {}
        host = None
        if any(device.placement is Placement.PROCESS for device in config.devices):
            host = ProcessHost()
            await host.start()
        coordinator = Coordinator(
            source=DeviceSpec(
                "source",
                _kept(made, "source", _source_factory(config.source, output, clock, bus)),
                ready_timeout=config.source.ready_timeout,
                stop_timeout=config.source.stop_timeout,
            ),
            participants=[_spec(device, made, bus) for device in config.devices],
            launcher=Launcher(host),
        )
        ref = spawn(coordinator, clock=clock, name="coordinator")
        rig = cls(config, output, coordinator, ref, host, made, clock)
        try:
            await asyncio.wait_for(rig._launched(), launch_timeout)
        except TimeoutError:
            await rig.close()
            raise LaunchFailed(f"the rig did not start within {launch_timeout} s") from None
        except BaseException:
            await rig.close()
            raise
        return rig

    async def status(self) -> Status:
        """The Coordinator's ``Status``: its state, the acquisition's number,
        and each device, present or lost."""
        return await ask(self.ref, GetStatus(), timeout=STATUS_TIMEOUT, clock=self._clock)

    async def device(self, name: str) -> ActorRef:
        """The device called ``name`` in the rig config, where it is now.
        Raises ``KeyError`` if there is none, or it is lost."""
        return _present(await self.status(), name, source=False)

    async def source(self) -> ActorRef:
        """The timing source, where it is now; it takes the waveforms.
        Raises ``KeyError`` if it is lost."""
        return _present(await self.status(), None, source=True)

    def line(self, label: str) -> int:
        """The row of the digital line ``label`` in a waveform's ``do``."""
        for row, channel in enumerate(self.output.do):
            if channel.label == label:
                return row
        raise KeyError(f"the source plays no line {label!r}")

    async def close(self) -> None:
        """Stop an acquisition still running, through the Coordinator so
        every device disarms and closes its file, then stop the Coordinator,
        which stops every device first, here and in processes of their own.
        Safe to call more than once."""
        if not self.coordinator.stopped.is_set():
            with contextlib.suppress(AskTimeout):
                if (await self.status()).state is CoordinatorState.RUNNING:
                    await ask(self.ref, StopAcquisition(), timeout=CLOSE_TIMEOUT, clock=self._clock)
        # Its exit stops its devices first, last launched first, each waited
        # for, and reports none of them lost: a deliberate shutdown raises no
        # Faults (G8).
        await _stopped(self.coordinator)
        if self.host is not None:
            await self.host.close()  # stops listening; its children are already gone

    async def _launched(self) -> None:
        """Wait for the Coordinator to have launched everything, and for each
        device here to have finished starting; raise ``LaunchFailed`` for the
        first that did not."""
        coordinator = self.coordinator
        await _either(coordinator.started, coordinator.stopped)
        if not coordinator.started.is_set():
            # The Coordinator's own launch of a device failed: in a process
            # of its own, a launch waits for the device's setup.
            raise LaunchFailed(describe(coordinator.exit_error) or "the Coordinator stopped")
        # A device here is spawned without waiting for its setup.
        for name, actor in list(self._made.items()):
            await _either(actor.started, actor.stopped)
            if not actor.started.is_set():
                raise LaunchFailed(f"{name}: {describe(actor.exit_error)}")


def _output(source: SourceConfig) -> OutputConfig:
    if source.output is not None:
        return source.output
    try:
        from stacscope.adapters.nidaqmx import read_output_config
    except ImportError as exc:
        raise ConfigError(f"source: the nidaqmx source needs the nidaqmx extra ({exc})") from None
    if source.buffer_samples is None:
        return read_output_config(source.ao_task, source.do_task)
    return read_output_config(source.ao_task, source.do_task, buffer_samples=source.buffer_samples)


def _bus(source: SourceConfig, clock: Clock):
    if source.kind != "simulated":
        return None
    from stacscope.adapters.simulated import TriggerBus

    return TriggerBus(clock)


def _source_factory(source: SourceConfig, output: OutputConfig, clock: Clock, bus) -> Callable[[], Actor]:
    if source.kind == "simulated":
        from stacscope.adapters.simulated import SimulatedSignalOutput

        return lambda: WaveformSource(SimulatedSignalOutput(clock, bus=bus), output)
    from stacscope.adapters.nidaqmx import NiSignalOutput

    return lambda: WaveformSource(NiSignalOutput(clock, ao_task=source.ao_task, do_task=source.do_task), output)


def _spec(device: DeviceConfig, made: dict[str, Actor], bus) -> DeviceSpec:
    if device.placement is Placement.PROCESS:
        # Sent to the process as it is, so nothing live may be in it.
        factory = device.factory()
    else:
        injected = {"bus": bus, "trigger": device.trigger} if device.simulated_trigger else {}
        factory = _kept(made, device.name, device.factory(**injected))
    return DeviceSpec(
        device.name,
        factory,
        ready_timeout=device.ready_timeout,
        stop_timeout=device.stop_timeout,
        heartbeat=device.heartbeat,
        placement=device.placement,
        required=device.required,
    )


def _kept(made: dict[str, Actor], name: str, factory: Callable[[], Actor]) -> Callable[[], Actor]:
    """``factory``, keeping what it makes, so that the rig can wait for each
    device in this process to finish starting. Startup only: after that the
    rig reaches them by message."""

    def make() -> Actor:
        actor = made[name] = factory()
        return actor

    return make


def _present(status: Status, name: str | None, *, source: bool) -> ActorRef:
    for device in status.devices:
        if device.source == source and (source or device.name == name) and device.ref is not None:
            return device.ref
    what = "the timing source" if source else f"device {name!r}"
    raise KeyError(f"{what} is not running")


async def _stopped(actor: Actor) -> None:
    if not actor.stopped.is_set():
        actor.stop()
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(actor.stopped.wait(), CLOSE_TIMEOUT)


async def _either(*events: asyncio.Event) -> None:
    waits = [asyncio.ensure_future(event.wait()) for event in events]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()
