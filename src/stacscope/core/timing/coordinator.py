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
    describe,
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
    """Whether losing this device ends the acquisition, and refuses every
    later one until the device is relaunched. Default true,
    so the safe answer is the one nobody has to remember. An optional device is
    a deliberate choice: an experiment worth finishing without it."""


class Coordinator(Actor):
    """Idle → Starting → Running → Stopping → Idle, plus Faulted.

    - ``StartAcquisition``: ``StartDevice`` to every participant and the
      source, concurrently, each with its own timeout. All ``DeviceReady`` →
      ``Go`` to the source, reply ``AcquisitionStarted``. Otherwise abort:
      reply ``Rejected`` naming each device not ready and why, publish a
      non-fatal ``Fault``, ``StopDevice`` to everyone (laggards included, so
      none is left armed), back to Idle.
    - ``StopAcquisition``: ``Finish`` to the source. On ``SourceFinished`` —
      requested or because the cycle count ran out — ``StopDevice`` to
      everyone, then reply ``AcquisitionStopped``.
    - While a ``required`` participant is lost, ``StartAcquisition`` is
      refused, naming it, until it is relaunched. A lost optional one is
      left out.
    - A participant's ``ChildExited`` drops it from the registry. The
      source's is fatal: everyone is stopped and the Coordinator is Faulted,
      refusing ``StartAcquisition`` until the source is relaunched.
    - ``Relaunch(name)``: spawn a fresh instance of a lost device from its
      spec, when Idle or Faulted. Relaunching the source leaves Faulted. A
      launch that fails is answered ``Rejected`` with why, and the device
      stays lost. Never automatic: when to relaunch is the user's call.
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
        # Nothing to subscribe to: the source reports its phases, and every
        # device its Faults, to its parent by address.
        self.source = await self._spawn(self._source_spec)

    async def _launch(self, spec: DeviceSpec) -> None:
        ref = self.participants[spec.name] = await self._spawn(spec)
        self.ref.tell(Subscribe(ref))  # so the source's phases reach the device

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
                if lost := self._lost_required():
                    # Refused, not latched: nothing holds the Coordinator, and
                    # each start is refused until the device is relaunched. A
                    # run without a required camera would still fire every
                    # other device (user, 2026-09-24).
                    self._reject(
                        f"required device lost: {', '.join(lost)}; Relaunch it to start"
                    )
                    return
                await self._start(StartDevice(root_filename, cycles))
            case StopAcquisition():
                if self._refuse(msg, CoordinatorState.RUNNING):
                    return
                self.state = CoordinatorState.STOPPING
                if self.sender is not None:
                    self._stop_waiters.append(self.sender)
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
                # Ignored, told or asked (user, 2026-09-30). The Coordinator
                # hears everything its source publishes -- MainApplied and
                # MainDiscarded are for whoever queued the waveforms.
                await super().handle(msg)

    async def _start(self, start: StartDevice) -> None:
        self.state = CoordinatorState.STARTING
        devices = self._devices()
        answers = await asyncio.gather(
            *(self._ready(ref, start, spec.ready_timeout) for spec, ref in devices)
        )
        # Each with its own reason: a refusal says what to fix, which a
        # timeout cannot, and a NotReady refusal publishes no Fault of its own.
        not_ready = [f"{spec.name} ({why})" for (spec, _), why in zip(devices, answers) if why]
        if not_ready:
            reason = f"not ready: {'; '.join(not_ready)}"
            self.report(Fault(self.ref, reason, fatal=False))
            self.reply(Rejected(reason))
            await self._stop_everyone()
            self.state = CoordinatorState.IDLE
            return
        self.source.tell(Go())
        self.state = CoordinatorState.RUNNING
        self.reply(AcquisitionStarted())

    async def _ready(self, ref: ActorRef, start: StartDevice, timeout: float) -> str | None:
        """``None`` if the device answered ready; otherwise why it is not."""
        try:
            answer = await ask(ref, start, timeout=timeout, clock=self.clock)
        except AskTimeout:
            return f"no reply within {timeout} s"
        if answer == DeviceReady():
            return None
        if isinstance(answer, Rejected):
            return answer.reason
        return f"answered {type(answer).__name__}"

    async def _stop_everyone(self) -> None:
        devices = self._devices()
        stopped = await asyncio.gather(*(self._stopped(spec, ref) for spec, ref in devices))
        unconfirmed = [spec.name for (spec, _), ok in zip(devices, stopped) if not ok]
        if unconfirmed:
            reason = f"stop not confirmed: {', '.join(unconfirmed)}"
            self.report(Fault(self.ref, reason, fatal=False))

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
            self.report(Fault(self.ref, reason, fatal=True))
            self._release_stop_waiters(Rejected(reason))
            await self._stop_everyone()
            return
        for name, ref in list(self.participants.items()):
            if ref == child:
                del self.participants[name]
                reason = f"participant {name} exited: {error or 'clean stop'}"
                self.report(Fault(self.ref, reason, fatal=False))
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
        self.report(Fault(self.ref, reason, fatal=False))
        await self._stop_everyone()
        self.state = CoordinatorState.IDLE
        self._release_stop_waiters(AcquisitionStopped())

    async def _relaunch(self, name: str) -> None:
        if self._refuse(Relaunch(name), CoordinatorState.IDLE, CoordinatorState.FAULTED):
            return
        is_source = name == self._source_spec.name
        if not is_source and name not in self._specs:
            self._reject(f"no device named {name!r} is configured")
            return
        running = self.source is not None if is_source else name in self.participants
        if running:
            self._reject(f"{name} is running; only a lost device can be relaunched")
            return
        # Caught here, not left to end the Coordinator: a device that has just
        # failed is the likeliest to fail its relaunch (a wedged driver), and
        # the Coordinator's death would stop every other device with it. Nothing
        # was registered before the launch raised, so the device is simply
        # still lost.
        try:
            if is_source:
                await self._launch_source()
            else:
                await self._launch(self._specs[name])
        except Exception as exc:  # noqa: BLE001 - any launch failure leaves it lost, never the Coordinator dead
            self._reject(f"{name} could not be relaunched: {describe(exc)}")
            return
        if is_source:
            self.state = CoordinatorState.IDLE
        self.reply(Accepted())

    def publish(self, msg: object, priority: Priority = Priority.NORMAL) -> None:
        """Clients hear everything; a device subscribed here hears only the
        timing phases."""
        # A device told a Fault would pass it back up, and the two would trade
        # it for ever; told of a launch, it would hold a sibling's ref. Its
        # only business here is the phases (the parent/child rule).
        devices = set(self.participants.values())
        for subscriber in list(self._subscribers):
            if subscriber in devices and not isinstance(msg, SourcePhase):
                continue
            subscriber.tell(msg, priority)

    def _lost_required(self) -> list[str]:
        return [
            name
            for name, spec in self._specs.items()
            if spec.required and name not in self.participants
        ]

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
        if self.sender is not None:
            self.reply(Rejected(reason))
        else:
            self.report(Fault(self.ref, reason, fatal=False))
