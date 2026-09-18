"""R4 — a child that raises posts ``ChildExited`` to its parent at URGENT.

The LAST-ACK equivalent: it is what keeps the Coordinator's registry honest.
"""

import asyncio
from dataclasses import dataclass

from stacscope.core.actors import Actor, ChildExited, Priority, spawn


class Boom(Exception):
    pass


@dataclass
class Tick:
    n: int


class Doomed(Actor):
    async def setup(self):
        raise Boom("device lost")


class Quits(Actor):
    async def handle(self, msg):
        self.stop()


class Parent(Actor):
    def __init__(self, child: Actor, ticks: int = 0):
        super().__init__()
        self.child = child
        self.ticks = ticks
        self.child_ref = None
        self.handled = []

    async def handle(self, msg):
        self.handled.append(msg)
        match msg:
            case "start":
                for n in range(self.ticks):
                    self.ref.tell(Tick(n), Priority.LOW)
                self.child_ref = self.spawn_child(self.child)
                self.child_ref.tell("go")
            case Tick():
                await asyncio.sleep(0)  # a slice of work; lets the child run
            case ChildExited():
                self.stop()


async def _run(parent):
    spawn(parent).tell("start")
    await asyncio.wait_for(parent.stopped.wait(), timeout=1)
    return [m for m in parent.handled if isinstance(m, ChildExited)]


async def test_child_that_raises_reports_its_error():
    parent = Parent(Doomed())
    [exited] = await _run(parent)

    assert exited.child is parent.child_ref
    assert "Boom" in exited.error and "device lost" in exited.error


async def test_clean_child_exit_is_reported_without_error():
    parent = Parent(Quits())
    [exited] = await _run(parent)

    assert exited.child is parent.child_ref
    assert exited.error is None


async def test_child_exited_jumps_the_parents_low_priority_queue():
    parent = Parent(Doomed(), ticks=10)
    await _run(parent)

    # The child dies while the parent is inside Tick(0); nine LOW ticks are
    # still queued, and ChildExited must be handled ahead of all of them.
    start, tick, exited = parent.handled
    assert (start, tick) == ("start", Tick(0))
    assert isinstance(exited, ChildExited)
