"""Coordinator: control flow only. First messaged, last to act.

It knows the participants and the one configured timing source, runs the
START/READY handshake, and never touches waveforms, frames or cycle timing.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from stacscope.core.actors import (
    Accepted,
    Actor,
    ActorRef,
    AskTimeout,
    ChildExited,
    Fault,
    HeartbeatPolicy,
    Priority,
    Rejected,
    Subscribe,
    ask,
)
from stacscope.core.protocol import (
    AcquisitionStarted,
    AcquisitionStopped,
    DeviceLaunched,
    DeviceReady,
    DeviceStopped,
    Finish,
    Go,
    Relaunch,
    SourceFinished,
    SourcePhase,
    StartAcquisition,
    StartDevice,
    StopAcquisition,
    StopDevice,
)
from stacscope.core.timing.launcher import Launcher, Placement


class CoordinatorState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"
    FAULTED = "faulted"


@dataclass(frozen=True, slots=True)
class DeviceSpec:
    """How the Coordinator spawns and supervises one device (from config)."""

    name: str
    factory: Callable[[], Actor]
    ready_timeout: float
    stop_timeout: float | None = None
    """Defaults to ``ready_timeout``."""
    heartbeat: HeartbeatPolicy | None = None
    placement: Placement = Placement.IN_PROCESS
    """Where the device runs. A process of its own costs a link and buys fault
    isolation: the right trade for a vendor driver, not for a shutter."""
    required: bool = True
    """Whether losing this device mid-run ends the acquisition. Default true,
    so the safe answer is the one nobody has to remember. An optional device is
    a deliberate choice: an experiment worth finishing without it."""


class Coordinator(Actor):
    """Idle → Starting → Running → Stopping → Idle, plus Faulted.

    - ``StartAcquisition``: ``StartDevice`` to every participant and the
      source, concurrently, each with its own timeout. All ``DeviceReady`` →
      ``Go`` to the source, reply ``AcquisitionStarted``. Otherwise abort:
      reply ``Rejected``, publish a non-fatal ``Fault``, ``StopDevice`` to
      everyone (laggards included, so none is left armed), back to Idle.
    - ``StopAcquisition``: ``Finish`` to the source. On ``SourceFinished`` —
      requested or because the cycle count ran out — ``StopDevice`` to
      everyone, then reply ``AcquisitionStopped``.
    - A participant's ``ChildExited`` drops it from the registry. The
      source's is fatal: everyone is stopped and the Coordinator is Faulted,
      refusing ``StartAcquisition`` until the source is relaunched.
    - ``Relaunch(name)``: spawn a fresh instance of a lost device from its
      spec, when Idle or Faulted. Relaunching the source leaves Faulted. Never
      automatic: when to relaunch is the user's call.
    - Every launch is published as ``DeviceLaunched(name, ref)``.
    """

    def __init__(
        self,
        source: DeviceSpec,
        participants: Sequence[DeviceSpec],
        launcher: Launcher | None = None,
    ) -> None:
        super().__init__()
        self._launcher = launcher or Launcher()
        self._source_spec = source
        self._specs = {spec.name: spec for spec in participants}
        self.source: ActorRef | None = None
        self.participants: dict[str, ActorRef] = {}
        self.state = CoordinatorState.IDLE
        self._stop_waiters: list[ActorRef] = []

    async def setup(self) -> None:
        await self._launch_source()
        for spec in self._specs.values():
            await self._launch(spec)

    async def _launch_source(self) -> None:
        self.source = await self._spawn(self._source_spec)
        self.source.tell(Subscribe(self.ref))

    async def _launch(self, spec: DeviceSpec) -> None:
        ref = self.participants[spec.name] = await self._spawn(spec)
        ref.tell(Subscribe(self.ref))  # so a device's faults reach here, not just its death
        self.ref.tell(Subscribe(ref))  # and so the source's phases reach the device

    async def _spawn(self, spec: DeviceSpec) -> ActorRef:
        """Wherever the launcher puts it, what comes back is a ref like any other."""
        ref = await self._launcher.launch(self, spec)
        self.publish(DeviceLaunched(spec.name, ref))
        return ref

    async def handle(self, msg: object) -> None:
        match msg:
            case StartAcquisition(cycles, root_filename):
                if self._refuse(msg, CoordinatorState.IDLE):
                    return
                await self._start(StartDevice(root_filename, cycles))
            case StopAcquisition():
                if self._refuse(msg, CoordinatorState.RUNNING):
                    return
                self.state = CoordinatorState.STOPPING
                if self.reply_to is not None:
                    self._stop_waiters.append(self.reply_to)
                self.source.tell(Finish(), Priority.URGENT)
            case Relaunch(name):
                await self._relaunch(name)
            case SourceFinished():
                if self.state in (CoordinatorState.RUNNING, CoordinatorState.STOPPING):
                    await self._stop_everyone()
                    self.state = CoordinatorState.IDLE
                    self._release_stop_waiters(AcquisitionStopped())
            case SourcePhase():
                self.publish(msg)  # a software-timed device synchronizes to these
            case Fault(_, reason, fatal):
                self.publish(msg)  # a device's fault is the Coordinator's clients' news
                if fatal:
                    await self._abort(f"stopped by a fatal fault: {reason}")
            case ChildExited(child, error):
                await self._child_exited(child, error)
            case _:
                self._reject(f"{type(msg).__name__} is not understood by the Coordinator")

    async def _start(self, start: StartDevice) -> None:
        self.state = CoordinatorState.STARTING
        devices = self._devices()
        ready = await asyncio.gather(
            *(self._ready(ref, start, spec.ready_timeout) for spec, ref in devices)
        )
        laggards = [spec.name for (spec, _), ok in zip(devices, ready) if not ok]
        if laggards:
            reason = f"not ready within timeout: {', '.join(laggards)}"
            self.publish(Fault(self.ref, reason, fatal=False))
            self.reply(Rejected(reason))
            await self._stop_everyone()
            self.state = CoordinatorState.IDLE
            return
        self.source.tell(Go())
        self.state = CoordinatorState.RUNNING
        self.reply(AcquisitionStarted())

    async def _ready(self, ref: ActorRef, start: StartDevice, timeout: float) -> bool:
        try:
            return await ask(ref, start, timeout=timeout, clock=self.clock) == DeviceReady()
        except AskTimeout:
            return False

    async def _stop_everyone(self) -> None:
        devices = self._devices()
        stopped = await asyncio.gather(*(self._stopped(spec, ref) for spec, ref in devices))
        unconfirmed = [spec.name for (spec, _), ok in zip(devices, stopped) if not ok]
        if unconfirmed:
            reason = f"stop not confirmed: {', '.join(unconfirmed)}"
            self.publish(Fault(self.ref, reason, fatal=False))

    async def _stopped(self, spec: DeviceSpec, ref: ActorRef) -> bool:
        timeout = spec.stop_timeout if spec.stop_timeout is not None else spec.ready_timeout
        try:
            return await ask(ref, StopDevice(), timeout=timeout, clock=self.clock) == DeviceStopped()
        except AskTimeout:
            return False

    async def _child_exited(self, child: ActorRef, error: str | None) -> None:
        if child == self.source:
            self.source = None
            self.state = CoordinatorState.FAULTED
            reason = f"timing source lost: {error or 'exited'}"
            self.publish(Fault(self.ref, reason, fatal=True))
            self._release_stop_waiters(Rejected(reason))
            await self._stop_everyone()
            return
        for name, ref in list(self.participants.items()):
            if ref == child:
                del self.participants[name]
                reason = f"participant {name} exited: {error or 'clean stop'}"
                self.publish(Fault(self.ref, reason, fatal=False))
                if self._specs[name].required:
                    await self._abort(f"stopped by the loss of {name}")

    async def _abort(self, reason: str) -> None:
        """End a running acquisition that cannot go on, and return to Idle.

        Deliberately not Faulted: that is for a lost source, where there is
        something to relaunch. Here the cause is outside STACScope, so nothing
        is latched — if the user has not fixed it, the next acquisition simply
        fails the same way, which beats holding a lock they have to clear.

        Stopping goes straight through ``StopDevice`` rather than ``Finish``,
        because the thing that just failed may be the source itself: every
        device's own stop timeout then bounds the wait, and a healthy source
        still cools down before it confirms.
        """
        if self.state not in (CoordinatorState.RUNNING, CoordinatorState.STOPPING):
            return
        self.state = CoordinatorState.STOPPING
        self.publish(Fault(self.ref, reason, fatal=False))
        await self._stop_everyone()
        self.state = CoordinatorState.IDLE
        self._release_stop_waiters(AcquisitionStopped())

    async def _relaunch(self, name: str) -> None:
        if self._refuse(Relaunch(name), CoordinatorState.IDLE, CoordinatorState.FAULTED):
            return
        if name == self._source_spec.name:
            if self.source is not None:
                self._reject(f"{name} is running; only a lost device can be relaunched")
                return
            await self._launch_source()
            self.state = CoordinatorState.IDLE
        elif name in self._specs:
            if name in self.participants:
                self._reject(f"{name} is running; only a lost device can be relaunched")
                return
            await self._launch(self._specs[name])
        else:
            self._reject(f"no device named {name!r} is configured")
            return
        self.reply(Accepted())

    def _devices(self) -> list[tuple[DeviceSpec, ActorRef]]:
        devices = [(self._specs[name], ref) for name, ref in self.participants.items()]
        if self.source is not None:
            devices.append((self._source_spec, self.source))
        return devices

    def _release_stop_waiters(self, reply: object) -> None:
        waiters, self._stop_waiters = self._stop_waiters, []
        for waiter in waiters:
            waiter.tell(reply)

    def _refuse(self, msg: object, *allowed: CoordinatorState) -> bool:
        if self.state in allowed:
            return False
        self._reject(f"{type(msg).__name__} is not valid while {self.state.name}")
        return True

    def _reject(self, reason: str) -> None:
        if self.reply_to is not None:
            self.reply(Rejected(reason))
        else:
            self.publish(Fault(self.ref, reason, fatal=False))
