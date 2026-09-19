"""TimingSource: the one actor that owns hardware time.

The base class drives the phase progression; subclasses supply only what each
phase *does*, through the hooks. That split is what lets an AO board and a
camera both be sources: the Coordinator sees the same phases from either.
"""

import asyncio
from dataclasses import dataclass

from stacscope.core.actors import Actor, ActorRef, Fault, Priority, Rejected
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
class _NextCycle:
    """LOW-priority self-message sustaining Run. Never leaves this actor, so
    anything URGENT (``Finish``, ``StopDevice``) is handled between cycles."""


class TimingSource(Actor):
    """IDLE → ARMED → WARMUP → RUN → COOLDOWN → IDLE, published as ``SourcePhase``.

    - ``StartDevice``: ``arm()``, reply ``DeviceReady``.
    - ``Go``: ``warmup()``, then ``run_cycle()`` once per cycle.
    - ``Finish``, or the cycle count running out: after the current cycle,
      ``cooldown()`` then ``drain()``; publishes ``SourceFinished``.
    - ``StopDevice``: safe in any phase. Idle: no-op. Armed: ``disarm()``.
      Running: finishes as above first. Replies ``DeviceStopped`` when idle.

    A command in the wrong phase is answered ``Rejected``, or published as a
    non-fatal ``Fault`` if it was told rather than asked. Phases are announced
    as they are *entered*, which for a buffered source is when they are
    committed, ahead of when they play.
    """

    def __init__(self) -> None:
        super().__init__()
        self.phase = Phase.IDLE
        self._remaining = 0
        self._finishing = False
        self._stop_waiters: list[ActorRef] = []

    # --- hooks for implementations -----------------------------------------

    async def arm(self, root_filename: str, cycles: int) -> None:
        """Prepare hardware so that ``warmup`` can start output immediately.
        Raise ``NotReady`` to refuse."""

    async def disarm(self) -> None:
        """Undo ``arm`` without ever having produced output."""

    async def warmup(self) -> None:
        """Start output. The AO source commits its WARMUP segment here."""

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
            case Finish():
                if self._refuse(msg, Phase.WARMUP, Phase.RUN):
                    return
                self._finishing = True
            case StopDevice():
                await self._stop()
            case _:
                self._reject(f"{type(msg).__name__} is not understood by {type(self).__name__}")

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
        self.publish(SourceFinished())
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
                if self.reply_to is not None:
                    self._stop_waiters.append(self.reply_to)

    def _enter(self, phase: Phase) -> None:
        if (self.phase, phase) not in _LEGAL:
            raise RuntimeError(f"illegal phase transition {self.phase.name} → {phase.name}")
        self.phase = phase
        self.publish(SourcePhase(phase))

    def _refuse(self, msg: object, *allowed: Phase) -> bool:
        if self.phase in allowed:
            return False
        self._reject(f"{type(msg).__name__} is not valid in phase {self.phase.name}")
        return True

    def _reject(self, reason: str) -> None:
        if self.reply_to is not None:
            self.reply(Rejected(reason))
        else:
            self.publish(Fault(self.ref, reason, fatal=False))
