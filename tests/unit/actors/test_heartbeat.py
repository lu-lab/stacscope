"""R5 — heartbeat timeout produces ``ChildExited`` for an actor that stops
responding without raising.

Heartbeats prove the child's *runtime* is alive (process, loop, link), so the
runtime answers them outside ``handle``: a handler may run long without being
declared dead. A hung handler is caught by per-request ``ask`` timeouts instead.
"""

import asyncio

from stacscope.adapters.simulated import SimulatedLink, VirtualClock
from stacscope.core.actors import Actor, ChildExited, HeartbeatPolicy, spawn

POLICY = HeartbeatPolicy(interval=1.0, timeout=0.5)


class Idle(Actor):
    pass


class Busy(Actor):
    """Stuck in one long handler — alive, just not draining its mailbox."""

    async def handle(self, msg):
        await asyncio.Event().wait()


class Quits(Actor):
    async def handle(self, msg):
        self.stop()


class Parent(Actor):
    def __init__(self, child: Actor):
        super().__init__()
        self.child = child
        self.link: SimulatedLink | None = None
        self.child_ref = None
        self.exits: list[ChildExited] = []

    async def setup(self):
        def make_link(inner):
            self.link = SimulatedLink(inner)
            return self.link

        self.child_ref = self.spawn_child(self.child, heartbeat=POLICY, link=make_link)
        self.child_ref.tell("go")

    async def handle(self, msg):
        if isinstance(msg, ChildExited):
            self.exits.append(msg)


async def _start(child):
    clock = VirtualClock()
    parent = Parent(child)
    spawn(parent, clock=clock)
    await clock.advance(0)
    return parent, clock


async def test_silent_child_is_declared_dead_one_timeout_after_the_missed_ping():
    parent, clock = await _start(Idle())
    parent.link.sever()

    await clock.advance(1.25)  # ping sent at 1.0, still unanswered
    assert parent.exits == []

    await clock.advance(0.25)  # its timeout expires at 1.5
    [exited] = parent.exits
    assert exited.child is parent.child_ref
    assert "heartbeat" in exited.error


async def test_child_busy_in_a_long_handler_is_not_declared_dead():
    parent, clock = await _start(Busy())

    await clock.advance(30.0)

    assert parent.exits == []


async def test_clean_exit_is_reported_once_and_heartbeat_stops():
    parent, clock = await _start(Quits())

    await clock.advance(30.0)

    [exited] = parent.exits
    assert exited.error is None


async def test_child_that_dies_after_heartbeat_timeout_is_not_reported_twice():
    parent, clock = await _start(Busy())
    parent.link.sever()
    await clock.advance(1.5)
    assert len(parent.exits) == 1

    parent.child._task.cancel()  # the "dead" child finally goes away
    await clock.advance(5.0)

    assert len(parent.exits) == 1
