"""Coordinator: control flow only. First messaged, last to act.

It knows the participants and the one configured timing source, runs the
START/READY handshake, and never touches waveforms, frames or cycle timing.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from enum import Enum

from stacscope.core.actors import (
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
    DeviceReady,
    DeviceStopped,
    Finish,
    Go,
    SourceFinished,
    SourcePhase,
    StartAcquisition,
    StartDevice,
    StopAcquisition,
    StopDevice,
)


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
      refusing ``StartAcquisition`` from then on.
    """

    def __init__(self, source: DeviceSpec, participants: Sequence[DeviceSpec]) -> None:
        super().__init__()
        self._source_spec = source
        self._specs = {spec.name: spec for spec in participants}
        self.source: ActorRef | None = None
        self.participants: dict[str, ActorRef] = {}
        self.state = CoordinatorState.IDLE
        self._stop_waiters: list[ActorRef] = []

    async def setup(self) -> None:
        self.source = self._spawn(self._source_spec)
        self.source.tell(Subscribe(self.ref))
        for spec in self._specs.values():
            self.participants[spec.name] = self._spawn(spec)

    def _spawn(self, spec: DeviceSpec) -> ActorRef:
        return self.spawn_child(spec.factory(), name=spec.name, heartbeat=spec.heartbeat)

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
            case SourceFinished():
                if self.state in (CoordinatorState.RUNNING, CoordinatorState.STOPPING):
                    await self._stop_everyone()
                    self.state = CoordinatorState.IDLE
                    self._release_stop_waiters(AcquisitionStopped())
            case SourcePhase():
                pass
            case Fault():
                self.publish(msg)  # a device's fault is the Coordinator's clients' news
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
