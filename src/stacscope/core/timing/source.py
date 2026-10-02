"""TimingSource: the one actor that owns hardware time.

The base class drives the phase progression; subclasses supply only what each
phase *does*, through the hooks. That split is what lets an AO board and a
camera both be sources: the Coordinator sees the same phases from either.
"""

import asyncio
from collections import deque
from dataclasses import dataclass

from stacscope.core.actors import Actor, ActorRef, Fault, Priority, Rejected
from stacscope.core.ports import Scheduled
from stacscope.core.devices import NotReady
from stacscope.core.protocol import (
    DeviceReady,
    DeviceStopped,
    Finish,
    Go,
    Phase,
    SourceFinished,
    SourcePhase,
    StartDevice,
    StopDevice,
)

__all__ = ["NotReady", "TimingSource"]  # NotReady is a device's refusal; a source is a device too

_LEGAL = {
    (Phase.IDLE, Phase.ARMED),
    (Phase.ARMED, Phase.IDLE),
    (Phase.ARMED, Phase.WARMUP),
    (Phase.WARMUP, Phase.RUN),
    (Phase.RUN, Phase.COOLDOWN),
    (Phase.COOLDOWN, Phase.IDLE),
}


@dataclass(frozen=True, slots=True)
class _Announce:
    """URGENT self-message: check whether the next phase to announce has
    started playing. URGENT, so it gets in while a refill waits."""


@dataclass(frozen=True, slots=True)
class _NextCycle:
    """LOW-priority self-message sustaining Run. Never leaves this actor, so
    anything URGENT (``Finish``, ``StopDevice``) is handled between cycles."""


class TimingSource(Actor):
    """IDLE → ARMED → WARMUP → RUN → COOLDOWN → IDLE, reported as ``SourcePhase``.

    - ``StartDevice``: ``arm()``, reply ``DeviceReady``.
    - ``Go``: ``warmup()``, then ``run_cycle()`` once per cycle.
    - ``Finish``, or the cycle count running out: after the current cycle,
      ``cooldown()`` then ``drain()``; reports ``SourceFinished``.
    - ``StopDevice``: safe in any phase. Idle: no-op. Armed: ``disarm()``.
      Running: finishes as above first. Replies ``DeviceStopped`` when idle.

    Phases and ``SourceFinished`` are reported to the parent, or published by
    a source that is a root. A command in the wrong phase is answered
    ``Rejected``, or reported as a non-fatal ``Fault`` if it came with no
    sender.

    Every phase is announced as it starts *playing*: WARMUP when output
    starts, RUN at the first MAIN sample, COOLDOWN at its first sample,
    IDLE once output has ceased. A buffered source enters each phase when it
    commits it, ahead of when it plays, and says where it starts with
    ``phase_start`` and ``until_played``; a source with no buffer plays each
    phase as it enters it, which is the default. An acquisition stopped
    before any MAIN plays never announces RUN.
    """

    def __init__(self) -> None:
        super().__init__()
        self.phase = Phase.IDLE
        self._remaining = 0
        self._finishing = False
        self._stop_waiters: list[ActorRef] = []
        # Phases entered and not yet announced, each with the sample it
        # starts playing at, in order; and the check for the first of them.
        self._unannounced: deque[tuple[Phase, int | None]] = deque()
        self._check: Scheduled | None = None

    # --- hooks for implementations -----------------------------------------

    def phase_start(self, phase: Phase) -> int | None:
        """The sample, counted from this acquisition's first, at which
        ``phase`` starts playing; asked as the phase is entered. ``None``, the
        default, when it plays as it is entered -- a source with no buffer.
        A buffered source answers WARMUP, RUN and COOLDOWN."""
        return None

    def until_played(self, sample: int) -> float | None:
        """Seconds until ``sample`` plays: 0 once it has, ``None`` while
        output has not started. Asked only of a source whose ``phase_start``
        gives samples; it calls ``announce_due`` when its output starts."""
        return 0.0

    async def arm(self, root_filename: str, cycles: int) -> None:
        """Prepare hardware so that ``warmup`` can start output immediately.
        Raise ``NotReady`` to refuse."""

    async def disarm(self) -> None:
        """Undo ``arm`` without ever having produced output."""

    async def warmup(self) -> None:
        """Start output. The AO source commits its WARMUP waveform here."""

    async def run_cycle(self) -> None:
        """Produce one cycle; return when the next may be committed."""

    async def cooldown(self) -> None:
        """Wind down. Called directly after the last cycle, so a buffered
        source can commit it with no gap."""

    async def drain(self) -> None:
        """Return once hardware output has actually ceased."""

    # --- the progression ---------------------------------------------------

    async def handle(self, msg: object) -> None:
        match msg:
            case StartDevice(root_filename, cycles):
                if self._refuse(msg, Phase.IDLE):
                    return
                try:
                    await self.arm(root_filename, cycles)
                except NotReady as exc:
                    self._reject(str(exc))
                    return
                self._remaining = cycles
                self._forget_announcements()
                self._enter(Phase.ARMED)
                self.reply(DeviceReady())
            case Go():
                if self._refuse(msg, Phase.ARMED):
                    return
                self._enter(Phase.WARMUP)
                await self.warmup()
                self._enter(Phase.RUN)
                self.ref.tell(_NextCycle(), Priority.LOW)
            case _NextCycle():
                await self._next_cycle()
            case _Announce():
                self._check = None
                self.announce_due()
            case Finish():
                if self._refuse(msg, Phase.WARMUP, Phase.RUN):
                    return
                self._finishing = True
            case StopDevice():
                await self._stop()
            case _:
                await super().handle(msg)  # ignored, told or asked, but a Fault goes up

    async def _next_cycle(self) -> None:
        if self.phase is not Phase.RUN:
            return
        if self._finishing or self._remaining == 0:
            await self._finish()
            return
        await self.run_cycle()
        if self._remaining > 0:
            self._remaining -= 1
        await asyncio.sleep(0)  # a cycle always yields, even if run_cycle did not
        self.ref.tell(_NextCycle(), Priority.LOW)

    async def _finish(self) -> None:
        self._enter(Phase.COOLDOWN)
        await self.cooldown()
        await self.drain()
        self._finishing = False
        self._enter(Phase.IDLE)
        self.report(SourceFinished())
        waiters, self._stop_waiters = self._stop_waiters, []
        for waiter in waiters:
            waiter.tell(DeviceStopped())

    async def _stop(self) -> None:
        match self.phase:
            case Phase.IDLE:
                self.reply(DeviceStopped())
            case Phase.ARMED:
                await self.disarm()
                self._enter(Phase.IDLE)
                self.reply(DeviceStopped())
            case _:
                self._finishing = True
                if self.sender is not None:
                    self._stop_waiters.append(self.sender)

    def _enter(self, phase: Phase) -> None:
        if (self.phase, phase) not in _LEGAL:
            raise RuntimeError(f"illegal phase transition {self.phase.name} → {phase.name}")
        self.phase = phase
        start = self.phase_start(phase)
        if phase is Phase.COOLDOWN and self._unannounced and self._unannounced[-1] == (Phase.RUN, start):
            # No MAIN was queued between them, so RUN never plays: an
            # acquisition stopped before its first MAIN never announces RUN
            # (user, 2026-10-02).
            self._unannounced.pop()
        if phase is Phase.IDLE:
            # Output has ceased: whatever was entered has played, if it ever
            # will -- a source that never started its clock had empty phases.
            self._unannounced = deque((p, None) for p, _ in self._unannounced)
        self._unannounced.append((phase, start))
        self.announce_due()

    def announce_due(self) -> None:
        """Announce every phase that has started playing, in order, and set
        a check for the next. A buffered source calls it when its output
        starts."""
        while self._unannounced:
            phase, start = self._unannounced[0]
            if start is not None:
                wait = self.until_played(start)
                if wait is None or wait > 0:
                    self._check_in(wait)
                    return
            self._unannounced.popleft()
            self.report(SourcePhase(phase))

    def _check_in(self, wait: float | None) -> None:
        # One check at a time, for the phase at the head of the line. None:
        # output has not started, and the source calls announce_due when it
        # does.
        if self._check is not None:
            self._check.cancel()
            self._check = None
        if wait is not None:
            self._check = self.tell_later(wait, _Announce(), Priority.URGENT)

    def _forget_announcements(self) -> None:
        self._unannounced.clear()
        self._check_in(None)

    def _refuse(self, msg: object, *allowed: Phase) -> bool:
        if self.phase in allowed:
            return False
        self._reject(f"{type(msg).__name__} is not valid in phase {self.phase.name}")
        return True

    def _reject(self, reason: str) -> None:
        if self.sender is not None:
            self.reply(Rejected(reason))
        else:
            self.report(Fault(self.ref, reason, fatal=False))
