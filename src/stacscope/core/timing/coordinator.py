"""Coordinator: control flow only. First messaged, last to act.

It knows the participants and the one configured timing source, runs the
START/READY handshake, and never touches waveforms, frames or cycle timing.
"""

import asyncio
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from stacscope.core.actors import (
    Accepted,
    Actor,
    ActorRef,
    ChildExited,
    Fault,
    HeartbeatPolicy,
    Priority,
    Rejected,
    describe,
)
from stacscope.core.ports import Scheduled
from stacscope.core.protocol import (
    AcquisitionStarted,
    AcquisitionStopped,
    CoordinatorState,
    DeviceLaunched,
    DeviceReady,
    DeviceStatus,
    DeviceStopped,
    Finish,
    GetStatus,
    Go,
    Relaunch,
    SourceFinished,
    SourcePhase,
    StartAcquisition,
    StartDevice,
    Status,
    StopAcquisition,
    StopDevice,
)
from stacscope.core.timing.launcher import Launcher, Placement

__all__ = ["Coordinator", "CoordinatorState", "DeviceSpec"]


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


@dataclass(frozen=True, slots=True)
class _Due:
    """A device's answer was due by now. The Coordinator's to itself only."""

    round: int
    device: ActorRef


@dataclass(frozen=True, slots=True)
class _Launched:
    """A relaunch has ended, one way or the other. The Coordinator's to
    itself only."""

    name: str
    ref: ActorRef | None
    error: str | None
    asker: ActorRef | None


class Coordinator(Actor):
    """Idle → Starting → Running → Stopping → Idle.

    - ``StartAcquisition``: numbers the acquisition, then ``StartDevice`` to
      every participant and the source, each with its own ``ready_timeout``.
      All ``DeviceReady`` → ``Go`` to the source, reply
      ``AcquisitionStarted(number)``. Otherwise reply ``Rejected`` naming each
      device not ready and why, report a non-fatal ``Fault``, and
      ``StopDevice`` to everyone, laggards included, back to Idle. A required
      device (or the source) lost while starting fails the start at once; an
      optional one is left out.
    - ``StopAcquisition``: ``Finish`` to the source. On ``SourceFinished`` --
      requested, or the cycle count running out -- ``StopDevice`` to
      everyone, each with its own ``stop_timeout``, then reply
      ``AcquisitionStopped(number)``. A device lost while stopping counts as
      stopped, and is reported as lost.
    - A fatal ``Fault`` from a device, or the loss of a required one, while
      running: ``StopDevice`` to everyone at once (the source still cools
      down), back to Idle. Nothing is latched.
    - While a required device or the source is lost, ``StartAcquisition`` is
      refused, naming it, until it is relaunched.
    - ``Relaunch(name)``: when Idle, launch a fresh instance of a lost device
      from its spec; ``Accepted`` once launched, or ``Rejected`` with why.
      Never automatic: when to relaunch is the user's call.
    - ``GetStatus`` → ``Status``.

    Through all of it, the Coordinator keeps handling messages: a start, a
    stop and a relaunch are states it passes through, not waits.

    Clients that subscribe hear everything: every launch as
    ``DeviceLaunched``, every change as ``Status``, every Fault and the
    source's phases. A device that subscribes hears the phases only.
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
        self.acquisition = 0
        """The number of the latest acquisition begun; 0 before the first."""
        # The handshake in progress, if any: who still owes an answer, and the
        # deadline for each. A round number tells a stale deadline from a
        # live one.
        self._round = 0
        self._awaited: dict[ActorRef, str] = {}
        self._due: dict[ActorRef, Scheduled] = {}
        self._not_ready: list[str] = []
        self._unconfirmed: list[str] = []
        self._starter: ActorRef | None = None
        self._stop_waiters: list[ActorRef] = []
        self._finishing = False
        """Stopping, and waiting for the source to finish before the others
        are told to stop."""
        self._relaunching: dict[str, asyncio.Task[None]] = {}

    async def setup(self) -> None:
        # Blocking here is fine: nothing is served until setup returns.
        self.source = await self._spawn(self._source_spec)
        for spec in self._specs.values():
            self.participants[spec.name] = await self._spawn(spec)

    async def teardown(self) -> None:
        for task in self._relaunching.values():
            task.cancel()

    async def _spawn(self, spec: DeviceSpec) -> ActorRef:
        """Wherever the launcher puts it, what comes back is a ref like any other."""
        ref = await self._launcher.launch(self, spec)
        self.publish(DeviceLaunched(spec.name, ref))
        return ref

    async def handle(self, msg: object) -> None:
        match msg:
            case StartAcquisition(cycles, root_filename):
                self._start(msg, StartDevice(root_filename, cycles))
            case StopAcquisition():
                self._stop_requested(msg)
            case GetStatus():
                self.reply(self._status())
            case Relaunch(name):
                self._relaunch(name)
            case _Launched():
                self._launched(msg)
            case DeviceReady() if self.state is CoordinatorState.STARTING:
                self._answered(self.sender, None)
            case Rejected(reason) if self.state is CoordinatorState.STARTING:
                self._answered(self.sender, reason)
            case DeviceStopped() if self._stopping_devices():
                self._answered(self.sender, None)
            case _Due(round, device):
                self._overdue(round, device)
            case SourceFinished():
                if self.state is CoordinatorState.RUNNING or self._finishing:
                    self._stop_all()
            case SourcePhase():
                self.publish(msg)  # a software-timed device synchronizes to these
            case Fault(_, reason, fatal):
                self.publish(msg)  # a device's fault is the Coordinator's clients' news
                if fatal:
                    self._abort(f"stopped by a fatal fault: {reason}")
            case ChildExited(child, error):
                self._child_exited(child, error)
            case _:
                # Ignored, told or asked (user, 2026-09-30): a late answer, an
                # answer to something no longer waited on. The source's
                # MainApplied and MainDiscarded are for whoever queued the
                # waveforms, who subscribed to the source.
                await super().handle(msg)

    # --- starting ------------------------------------------------------------

    def _start(self, msg: StartAcquisition, start: StartDevice) -> None:
        if self._refuse(msg, CoordinatorState.IDLE):
            return
        if lost := self._lost_required():
            # Refused, not latched: nothing holds the Coordinator, and each
            # start is refused until the device is relaunched. A run without a
            # required camera would still fire every other device (user,
            # 2026-09-24).
            self._reject(f"required device lost: {', '.join(lost)}; Relaunch it to start")
            return
        self.acquisition += 1
        self._starter = self.sender
        self._not_ready = []
        self._enter(CoordinatorState.STARTING)
        self._expect(start, lambda spec: spec.ready_timeout)

    def _started_if_answered(self) -> None:
        if self._awaited:
            return
        if self._not_ready:
            self._fail_start()
            return
        self.source.tell(Go())
        self._enter(CoordinatorState.RUNNING)
        self._answer_starter(AcquisitionStarted(self.acquisition))

    def _fail_start(self) -> None:
        # Each with its own reason: a refusal says what to fix, which a timeout
        # cannot, and a NotReady refusal reports no Fault of its own (CR3).
        reason = f"not ready: {'; '.join(self._not_ready)}"
        self.report(Fault(self.ref, reason, fatal=False))
        self._answer_starter(Rejected(reason))
        self._stop_all()  # laggards included, so none is left armed

    def _answer_starter(self, answer: object) -> None:
        starter, self._starter = self._starter, None
        if starter is not None:
            starter.tell(answer)

    # --- stopping ------------------------------------------------------------

    def _stop_requested(self, msg: StopAcquisition) -> None:
        if self._refuse(msg, CoordinatorState.RUNNING):
            return
        if self.sender is not None:
            self._stop_waiters.append(self.sender)
        self._enter(CoordinatorState.STOPPING)
        self._finishing = True
        self.source.tell(Finish(), Priority.URGENT)

    def _abort(self, reason: str) -> None:
        """End a running acquisition that cannot go on, and return to Idle.

        Not latched: the cause is outside STACScope, so if the user has not
        fixed it, the next acquisition simply fails the same way, which beats
        holding a lock they have to clear.

        Stopping goes straight through ``StopDevice`` rather than ``Finish``,
        because the thing that just failed may be the source itself: every
        device's own stop timeout then bounds the wait, and a healthy source
        still cools down before it confirms.
        """
        if self.state is not CoordinatorState.RUNNING and not self._finishing:
            return
        self.report(Fault(self.ref, reason, fatal=False))
        self._stop_all()

    def _stop_all(self) -> None:
        self._enter(CoordinatorState.STOPPING)
        self._finishing = False
        self._unconfirmed = []
        self._expect(StopDevice(), _stop_timeout)

    def _stopping_devices(self) -> bool:
        return self.state is CoordinatorState.STOPPING and not self._finishing

    def _stopped_if_answered(self) -> None:
        if self._awaited:
            return
        if self._unconfirmed:
            reason = f"stop not confirmed: {', '.join(self._unconfirmed)}"
            self.report(Fault(self.ref, reason, fatal=False))
        self._enter(CoordinatorState.IDLE)
        waiters, self._stop_waiters = self._stop_waiters, []
        for waiter in waiters:
            waiter.tell(AcquisitionStopped(self.acquisition))

    # --- the handshake both share --------------------------------------------

    def _expect(self, command: object, timeout: Callable[[DeviceSpec], float]) -> None:
        """Send ``command`` to every device present and await each answer,
        each with its own deadline."""
        self._round += 1
        self._awaited = {}
        for spec, ref in self._devices():
            self._awaited[ref] = spec.name
            ref.tell(command)
            self._due[ref] = self.tell_later(timeout(spec), _Due(self._round, ref))
        self._settle()

    def _answered(self, device: ActorRef | None, refusal: str | None) -> None:
        name = self._awaited.pop(device, None)
        if name is None:
            return  # not awaited: late, or from a device that was not asked
        self._due.pop(device).cancel()
        if refusal is not None:
            self._not_ready.append(f"{name} ({refusal})")
        self._settle()

    def _overdue(self, round: int, device: ActorRef) -> None:
        if round != self._round or device not in self._awaited:
            return
        name = self._awaited.pop(device)
        self._due.pop(device, None)
        spec = self._spec(name)
        if self.state is CoordinatorState.STARTING:
            self._not_ready.append(f"{name} (no reply within {spec.ready_timeout} s)")
        else:
            self._unconfirmed.append(name)
        self._settle()

    def _settle(self) -> None:
        """Go on, if every answer is in."""
        if self.state is CoordinatorState.STARTING:
            self._started_if_answered()
        elif self._stopping_devices():
            self._stopped_if_answered()

    # --- losing a device --------------------------------------------------------

    def _child_exited(self, child: ActorRef, error: str | None) -> None:
        if child == self.source:
            self.source = None
            name, required = self._source_spec.name, True
            self.report(Fault(self.ref, f"timing source lost: {error or 'exited'}", fatal=False))
        else:
            name = next((n for n, ref in self.participants.items() if ref == child), None)
            if name is None:
                return
            del self.participants[name]
            required = self._specs[name].required
            self.report(Fault(self.ref, f"participant {name} exited: {error or 'clean stop'}", fatal=False))
        self._subscribers.pop(child, None)  # its successor at this address has not asked
        self._publish_status()

        awaited = self._awaited.pop(child, None) is not None
        if awaited:
            self._due.pop(child).cancel()
        if self.state is CoordinatorState.STARTING:
            if required:
                # At once: a dead device will never answer, and waiting out its
                # timeout would only delay the news.
                self._not_ready.append(f"{name} (exited: {error or 'clean stop'})")
                self._fail_start()
            elif awaited:
                self._settle()  # left out
        elif required and (self.state is CoordinatorState.RUNNING or self._finishing):
            self._abort(f"stopped by the loss of {name}")
        elif awaited:
            self._settle()  # stopping: a device that is gone has stopped

    # --- relaunching --------------------------------------------------------------

    def _relaunch(self, name: str) -> None:
        if self._refuse(Relaunch(name), CoordinatorState.IDLE):
            return
        spec = self._source_spec if name == self._source_spec.name else self._specs.get(name)
        if spec is None:
            self._reject(f"no device named {name!r} is configured")
        elif name in self._relaunching:
            self._reject(f"{name} is already being relaunched")
        elif self._present(name):
            self._reject(f"{name} is running; only a lost device can be relaunched")
        else:
            self._relaunching[name] = asyncio.create_task(self._launch(spec, self.sender))

    async def _launch(self, spec: DeviceSpec, asker: ActorRef | None) -> None:
        # A task, not a wait in the handler: a process child's launch lasts as
        # long as its setup, seconds for a camera, and the Coordinator answers
        # meanwhile. Its result comes back as a message, URGENT so that it is
        # handled before a ChildExited from the device it launched.
        try:
            ref = await self._launcher.launch(self, spec)
        except Exception as exc:  # noqa: BLE001 - any launch failure leaves it lost, never the Coordinator dead
            # A device that has just failed is the likeliest to fail its
            # relaunch (a wedged driver), and it must not end the Coordinator,
            # whose exit would stop every other device (CR2).
            self.ref.tell(_Launched(spec.name, None, describe(exc), asker), Priority.URGENT)
            return
        self.ref.tell(_Launched(spec.name, ref, None, asker), Priority.URGENT)

    def _launched(self, msg: _Launched) -> None:
        name, ref, error, asker = msg.name, msg.ref, msg.error, msg.asker
        self._relaunching.pop(name, None)
        if ref is None:
            reason = f"{name} could not be relaunched: {error}"
            if asker is not None:
                asker.tell(Rejected(reason))
            else:
                self.report(Fault(self.ref, reason, fatal=False))
            return
        if name == self._source_spec.name:
            self.source = ref
        else:
            self.participants[name] = ref
        self.publish(DeviceLaunched(name, ref))
        self._publish_status()
        if asker is not None:
            asker.tell(Accepted())

    # --- status ---------------------------------------------------------------------

    def _enter(self, state: CoordinatorState) -> None:
        if state is not self.state:
            self.state = state
            self._publish_status()

    def _publish_status(self) -> None:
        self.publish(self._status())

    def _status(self) -> Status:
        source = self._source_spec.name
        devices = [DeviceStatus(source, True, True, self.source is not None, self.source)]
        for name, spec in self._specs.items():
            ref = self.participants.get(name)
            devices.append(DeviceStatus(name, False, spec.required, ref is not None, ref))
        return Status(self.state, self.acquisition, tuple(devices))

    def publish(self, msg: object, priority: Priority = Priority.NORMAL) -> None:
        """Clients hear everything; a device subscribed here hears only the
        timing phases."""
        # A device told a Fault would pass it back up, and the two would trade
        # it for ever; told of a launch or a status, it would hold a sibling's
        # ref. Its only business here is the phases (the parent/child rule).
        devices = set(self.participants.values())
        for subscriber in list(self._subscribers):
            if subscriber in devices and not isinstance(msg, SourcePhase):
                continue
            subscriber.tell(msg, priority)

    # --- helpers ----------------------------------------------------------------------

    def _lost_required(self) -> list[str]:
        """The source, if lost, and every required participant lost."""
        lost = [] if self.source is not None else [self._source_spec.name]
        return lost + [
            name for name, spec in self._specs.items() if spec.required and name not in self.participants
        ]

    def _present(self, name: str) -> bool:
        return self.source is not None if name == self._source_spec.name else name in self.participants

    def _spec(self, name: str) -> DeviceSpec:
        return self._source_spec if name == self._source_spec.name else self._specs[name]

    def _devices(self) -> list[tuple[DeviceSpec, ActorRef]]:
        devices = [(self._specs[name], ref) for name, ref in self.participants.items()]
        if self.source is not None:
            devices.append((self._source_spec, self.source))
        return devices

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


def _stop_timeout(spec: DeviceSpec) -> float:
    return spec.ready_timeout if spec.stop_timeout is None else spec.stop_timeout
