"""CM8–CM11, CM13–CM14 -- deadlines, a parent's exit, and the gated wait.

A handler never awaits another actor: a wait on one is a state, and its
deadline is a message the actor sends itself, which it ignores if the answer
came first. A parent's exit takes its children with it, last launched first,
so nothing is left holding hardware with nobody to supervise it. And a wait
on the actor's own hardware is gated: while it lasts, only what is more
urgent than the message being handled gets in.
"""

import asyncio
from dataclasses import dataclass

import pytest

from stacscope.adapters.simulated import VirtualClock
from stacscope.core.actors import Actor, ChildExited, Priority, spawn
from stacscope.core.ports import MonotonicClock


@dataclass(frozen=True)
class Note:
    text: str


@dataclass(frozen=True)
class Wait:
    """Handle this by waiting, gated, until ``release`` is set."""


class Keeper(Actor):
    def __init__(self, log=None, name=""):
        super().__init__()
        self.heard = []
        self.log = log if log is not None else []
        self.name = name
        self.release = asyncio.Event()
        self.pending = None

    async def handle(self, msg):
        self.heard.append(msg)
        match msg:
            case Wait():
                await self.gated(self.release.wait())
                self.heard.append("waited")
            case Note("fail"):
                raise RuntimeError("a handler failed during the wait")
            case Note("later"):
                self.pending = self.tell_later(2.0, Note("deadline"))
            case Note("cancel"):
                self.pending.cancel()
            case _:
                await super().handle(msg)

    async def teardown(self):
        self.log.append(f"{self.name} teardown")


class Parent(Keeper):
    def __init__(self, children, log, name="parent"):
        super().__init__(log, name)
        self.children = children

    async def setup(self):
        for child in self.children:
            self.spawn_child(child, name=child.name)


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


# --- CM8: Clock.call_later --------------------------------------------------------


async def test_cm8_a_virtual_call_later_fires_at_its_deadline_and_not_before():
    clock = VirtualClock()
    fired = []
    clock.call_later(1.5, lambda: fired.append(clock.now()))

    await clock.advance(1.499)
    assert fired == []
    await clock.advance(0.001)
    assert fired == [1.5]


async def test_cm8_a_cancelled_call_never_fires():
    clock = VirtualClock()
    fired = []
    clock.call_later(1.0, lambda: fired.append("late")).cancel()

    await clock.advance(5.0)
    assert fired == []


async def test_cm8_the_real_clock_calls_later_on_the_event_loop():
    fired = asyncio.Event()
    MonotonicClock().call_later(0.01, fired.set)
    await asyncio.wait_for(fired.wait(), 1.0)
    cancelled = []
    MonotonicClock().call_later(0.01, lambda: cancelled.append(1)).cancel()
    await asyncio.sleep(0.05)
    assert cancelled == []


# --- CM9: tell_later --------------------------------------------------------------


async def test_cm9_tell_later_delivers_to_the_actors_own_mailbox_on_its_clock():
    clock = VirtualClock()
    actor = Keeper()
    spawn(actor, clock=clock).tell(Note("later"))

    await clock.advance(1.999)
    assert Note("deadline") not in actor.heard
    await clock.advance(0.001)
    assert actor.heard[-1] == Note("deadline")


async def test_cm9_a_cancelled_deadline_is_never_delivered():
    """The answer came first: the deadline it guarded is ignored, by never
    arriving."""
    clock = VirtualClock()
    actor = Keeper()
    ref = spawn(actor, clock=clock)
    ref.tell(Note("later"))
    await clock.advance(1.0)
    ref.tell(Note("cancel"))

    await clock.advance(5.0)
    assert Note("deadline") not in actor.heard


async def test_cm9_an_actors_exit_cancels_its_deadlines():
    clock = VirtualClock()
    actor = Keeper()
    ref = spawn(actor, clock=clock)
    ref.tell(Note("later"))
    await clock.advance(0)
    actor.stop()
    await actor.stopped.wait()

    await clock.advance(5.0)
    assert actor.pending.cancelled()


# --- CM10, CM11: a parent's exit stops its children ------------------------------


async def test_cm10_a_parents_exit_stops_its_children_last_launched_first_then_itself():
    """Children first, so nothing they hold outlives the parent supervising
    it; in reverse launch order, so what was started on top of something is
    stopped before it; and the parent's own teardown last."""
    log = []
    grandchild = Keeper(log, "grandchild")
    first = Parent([grandchild], log, "first")
    second = Keeper(log, "second")
    parent = Parent([first, second], log)
    spawn(parent, name="parent")
    await settle()

    parent.stop()
    await asyncio.wait_for(parent.stopped.wait(), 1.0)

    assert log == ["second teardown", "grandchild teardown", "first teardown", "parent teardown"]
    assert all(actor.stopped.is_set() for actor in (grandchild, first, second))


async def test_cm10_a_parent_that_dies_still_stops_its_children():
    log = []
    child = Keeper(log, "child")
    parent = Parent([child], log)
    ref = spawn(parent, name="parent")
    await settle()

    ref.tell(Note("fail"))
    await asyncio.wait_for(parent.stopped.wait(), 1.0)

    assert isinstance(parent.exit_error, RuntimeError)
    assert log == ["child teardown", "parent teardown"]


async def test_cm11_a_child_mid_message_finishes_before_the_parent_goes_on():
    """A recorder told to finish writes every frame first: stopping lets the
    message in hand complete, and the parent waits for it."""
    log = []
    child = Keeper(log, "child")
    parent = Parent([child], log)
    spawn(parent, name="parent")
    await settle()
    child.ref.tell(Wait())
    await settle()

    parent.stop()
    await settle()
    assert not parent.stopped.is_set(), "the parent waits for the child's message"
    child.release.set()
    await asyncio.wait_for(parent.stopped.wait(), 1.0)

    assert child.heard[-1] == "waited"
    assert log == ["child teardown", "parent teardown"]


async def test_cm11_a_child_stopped_by_its_parents_exit_is_not_reported_lost():
    """Checked at the mailbox: a parent that has stopped handling would never
    act on a ``ChildExited``, but one sitting there would still be a report
    of a loss that did not happen."""
    log = []
    child = Keeper(log, "child")
    parent = Parent([child], log)
    delivered = []
    receive = parent._receive
    parent._receive = lambda envelope, priority: (delivered.append(envelope.msg), receive(envelope, priority))
    spawn(parent, name="parent")
    await settle()

    parent.stop()
    await asyncio.wait_for(parent.stopped.wait(), 1.0)
    await settle()

    assert child.stopped.is_set()
    assert not [m for m in delivered if isinstance(m, ChildExited)]


# --- CM13, CM14: the priority-gated wait ------------------------------------------


async def test_cm13_during_a_gated_wait_only_more_urgent_messages_are_handled():
    actor = Keeper()
    ref = spawn(actor)
    ref.tell(Wait(), Priority.NORMAL)
    await settle()

    ref.tell(Note("same"), Priority.NORMAL)
    ref.tell(Note("less"), Priority.LOW)
    ref.tell(Note("urgent 1"), Priority.URGENT)
    ref.tell(Note("urgent 2"), Priority.URGENT)
    await settle()
    assert actor.heard == [Wait(), Note("urgent 1"), Note("urgent 2")]

    actor.release.set()
    await settle()
    assert actor.heard == [Wait(), Note("urgent 1"), Note("urgent 2"), "waited", Note("same"), Note("less")]


async def test_cm13_a_gated_wait_in_a_low_handler_takes_normal_and_urgent():
    actor = Keeper()
    ref = spawn(actor)
    ref.tell(Wait(), Priority.LOW)
    await settle()

    ref.tell(Note("low"), Priority.LOW)
    ref.tell(Note("normal"), Priority.NORMAL)
    await settle()

    assert actor.heard == [Wait(), Note("normal")]
    actor.release.set()
    await settle()
    assert actor.heard[-2:] == ["waited", Note("low")]


async def test_cm13_a_gated_wait_returns_what_it_waited_for():
    class Answer(Actor):
        async def handle(self, msg):
            self.answer = await self.gated(asyncio.sleep(0, result=42))

    actor = Answer()
    spawn(actor).tell(Note("go"))
    await settle()
    assert actor.answer == 42


async def test_cm14_an_error_in_a_message_handled_during_the_wait_ends_the_actor():
    actor = Keeper()
    ref = spawn(actor)
    ref.tell(Wait(), Priority.NORMAL)
    await settle()

    ref.tell(Note("fail"), Priority.URGENT)
    await asyncio.wait_for(actor.stopped.wait(), 1.0)

    assert isinstance(actor.exit_error, RuntimeError)
    assert "waited" not in actor.heard


@pytest.mark.parametrize("priority", [Priority.URGENT])
async def test_cm13_an_urgent_handler_takes_nothing_during_its_wait(priority):
    """Nothing is more urgent than URGENT, so its waits hold everything."""
    actor = Keeper()
    ref = spawn(actor)
    ref.tell(Wait(), priority)
    await settle()
    ref.tell(Note("urgent"), Priority.URGENT)
    await settle()
    assert actor.heard == [Wait()]
    actor.release.set()
    await settle()
    assert actor.heard == [Wait(), "waited", Note("urgent")]
