"""Parent-side watch over one child: exit reporting and heartbeat liveness."""

import asyncio
from dataclasses import dataclass

from stacscope.core.actors.ask import AskTimeout, ask
from stacscope.core.actors.mailbox import Priority
from stacscope.core.actors.messages import ChildExited, Heartbeat
from stacscope.core.actors.ref import ActorRef
from stacscope.core.ports.clock import Clock


@dataclass(frozen=True, slots=True)
class HeartbeatPolicy:
    """Ping the child every ``interval`` s; declare it dead if a ping goes
    unanswered for ``timeout`` s. Chosen per child by its supervisor."""

    interval: float
    timeout: float


class ChildWatch:
    """Guarantees the parent receives exactly one ``ChildExited`` per child,
    whether the child's exit or a missed heartbeat is noticed first."""

    def __init__(self, parent: ActorRef, child: ActorRef) -> None:
        self._parent = parent
        self._child = child
        self._reported = False
        self._heartbeat: asyncio.Task[None] | None = None

    def start_heartbeat(self, policy: HeartbeatPolicy, clock: Clock) -> None:
        self._heartbeat = asyncio.create_task(self._beat(policy, clock))

    def report(self, error: str | None) -> None:
        if self._reported:
            return
        self._reported = True
        self._stop_heartbeat()
        self._parent.tell(ChildExited(self._child, error), Priority.URGENT)

    def close(self) -> None:
        """The parent is exiting; stop watching without reporting."""
        self._reported = True
        self._stop_heartbeat()

    def _stop_heartbeat(self) -> None:
        if self._heartbeat is not None and self._heartbeat is not asyncio.current_task():
            self._heartbeat.cancel()

    async def _beat(self, policy: HeartbeatPolicy, clock: Clock) -> None:
        while True:
            await clock.sleep(policy.interval)
            try:
                await ask(
                    self._child,
                    Heartbeat(),
                    timeout=policy.timeout,
                    clock=clock,
                    priority=Priority.URGENT,
                )
            except AskTimeout:
                self.report(f"heartbeat timeout: no reply within {policy.timeout} s")
                return
