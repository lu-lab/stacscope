"""R4 — a child that raises posts ``ChildExited`` to its parent at URGENT.

The LAST-ACK equivalent: it is what keeps the Coordinator's registry honest.
"""

import asyncio
from dataclasses import dataclass

import pytest

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


async def test_a_failed_teardown_reaches_the_parent_beside_what_caused_it():
    """Invariant 6: a failure to release hardware is reported, never raised
    over the fault that started the shutdown -- and never dropped either.

    The runtime keeps the first error and notes the teardown failure on it,
    but the parent only ever sees a string. If that string is built by
    ``repr``, which leaves notes out, the failed release is lost on the way:
    the operator learns the device died, and not that it may still hold the
    board."""

    class LeavesHardwareHeld(Doomed):
        async def teardown(self):
            raise OSError("handle would not close")

    parent = Parent(LeavesHardwareHeld())
    [exited] = await _run(parent)

    assert "device lost" in exited.error
    assert "handle would not close" in exited.error


async def test_child_exited_jumps_the_parents_low_priority_queue():
    parent = Parent(Doomed(), ticks=10)
    await _run(parent)

    # The child dies while the parent is inside Tick(0); nine LOW ticks are
    # still queued, and ChildExited must be handled ahead of all of them.
    start, tick, exited = parent.handled
    assert (start, tick) == ("start", Tick(0))
    assert isinstance(exited, ChildExited)


class Relauncher(Actor):
    """Spawns a named child; when it exits, spawns a fresh one under the same
    name — the relaunch path the Coordinator will use."""

    def __init__(self, first: Actor, second: Actor):
        super().__init__()
        self.children = [first, second]
        self.refs = []
        self.error = None

    async def setup(self):
        self.refs.append(self.spawn_child(self.children[0], name="camera"))

    async def handle(self, msg):
        if isinstance(msg, ChildExited) and len(self.refs) == 1:
            try:
                self.refs.append(self.spawn_child(self.children[1], name="camera"))
            except ValueError as exc:
                self.error = exc
            self.stop()


async def test_a_dead_childs_name_is_free_for_a_relaunch():
    parent = Relauncher(Doomed(), Quits())
    spawn(parent, name="coordinator")
    await asyncio.wait_for(parent.stopped.wait(), timeout=1)

    assert parent.error is None
    first, second = parent.refs
    assert first.address == second.address == "coordinator/camera"


async def test_a_live_childs_name_is_still_taken():
    parent = Relauncher(Quits(), Quits())
    spawn(parent, name="coordinator")
    await asyncio.sleep(0)

    with pytest.raises(ValueError):
        parent.spawn_child(Quits(), name="camera")
