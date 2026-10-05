"""CM52–CM57 -- the exit waits for the child's mail.

A ``ChildExited`` is sent at URGENT, so it can be handled ahead of a parent's
own queue. But a child that tells its parent something and then stops must
not have its exit handled first: the parent would act on "gone" before it had
heard the child's last word -- an answer it is waiting for, a report that
explains the exit. The rule (user, 2026-10-05): nothing a child sent before
its exit was noticed is handled after its ``ChildExited``. With the child's
mail queued, the exit is placed immediately behind the last of it to be
taken; with none, it is URGENT as before. No message's priority changes.

In each test the parent is kept busy while the child talks and dies, so that
everything the child sent is still queued when the exit arrives -- the only
case where the order is in question.
"""

import asyncio
from dataclasses import dataclass

from stacscope.adapters.simulated import SimulatedLink, VirtualClock
from stacscope.core.actors import Actor, ChildExited, HeartbeatPolicy, Priority, spawn


@dataclass(frozen=True)
class Say:
    """To a child: report ``Note(text)`` to your parent at ``priority``."""

    text: str
    priority: Priority = Priority.NORMAL


@dataclass(frozen=True)
class Note:
    text: str


@dataclass(frozen=True)
class Quit:
    """To a child: stop."""


@dataclass(frozen=True)
class Hold:
    """To the parent: stay in this handler, ungated, until ``release`` is set."""


@dataclass(frozen=True)
class Wait:
    """To the parent: wait, gated, until ``release`` is set."""


class Child(Actor):
    async def handle(self, msg):
        match msg:
            case Say(text, priority):
                self.report(Note(text), priority)
            case Quit():
                self.stop()
            case _:
                await super().handle(msg)


class Parent(Actor):
    """Records what it handles, in order, by short labels."""

    def __init__(self, *names, heartbeat=None):
        super().__init__()
        self.names = names
        self.heartbeat = heartbeat
        self.children = {}
        self.links = {}
        self.exits = []
        self.heard = []
        self.release = asyncio.Event()

    async def setup(self):
        for name in self.names:
            self.children[name] = self.spawn_child(
                Child(), name=name, heartbeat=self.heartbeat, link=self._link(name)
            )

    def _link(self, name):
        def make(inner):
            self.links[name] = SimulatedLink(inner)
            return self.links[name]

        return make

    async def handle(self, msg):
        match msg:
            case Hold():
                self.heard.append("hold")
                await self.release.wait()
            case Wait():
                self.heard.append("wait")
                await self.gated(self.release.wait())
                self.heard.append("waited")
            case Note(text):
                self.heard.append(text)
            case ChildExited(child):
                self.exits.append(msg)
                self.heard.append(f"{child.address.rsplit('/', 1)[-1]} exited")
            case _:
                await super().handle(msg)


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


async def start(*names, clock=None, heartbeat=None):
    parent = Parent(*names, heartbeat=heartbeat)
    ref = spawn(parent, name="parent", clock=clock)
    await settle()
    return parent, ref


async def test_cm52_a_childs_queued_message_is_handled_before_its_exit():
    """The race itself: the child reports at NORMAL and stops. Its exit,
    sent URGENT, used to overtake the report already queued, so the parent
    heard "gone" first -- and a parent waiting on that report would never
    see it as an answer."""
    parent, ref = await start("talker")
    ref.tell(Hold())
    await settle()

    parent.children["talker"].tell(Say("A"))
    parent.children["talker"].tell(Quit())
    await settle()
    parent.release.set()
    await settle()

    assert parent.heard == ["hold", "A", "talker exited"]


async def test_cm53_the_exit_goes_behind_the_last_of_the_childs_mail_to_be_taken():
    """Placement follows the order the parent will take the child's mail,
    not the order it was sent: URGENT B overtakes NORMAL A, as priority says
    it should, and the exit comes after both."""
    parent, ref = await start("talker")
    ref.tell(Hold())
    await settle()

    talker = parent.children["talker"]
    talker.tell(Say("A", Priority.NORMAL))
    talker.tell(Say("B", Priority.URGENT))
    talker.tell(Quit())
    await settle()
    parent.release.set()
    await settle()

    assert parent.heard == ["hold", "B", "A", "talker exited"]


async def test_cm54_with_nothing_of_the_childs_queued_the_exit_still_jumps_the_queue():
    """The rule is about the child's own mail only. A sibling's NORMAL and
    LOW messages are no reason to hold the exit back: a lost device must
    still be heard ahead of routine traffic (R4)."""
    parent, ref = await start("talker", "quitter")
    ref.tell(Hold())
    await settle()

    parent.children["talker"].tell(Say("X", Priority.NORMAL))
    parent.children["talker"].tell(Say("Y", Priority.LOW))
    await settle()
    parent.children["quitter"].tell(Quit())
    await settle()
    parent.release.set()
    await settle()

    assert parent.heard == ["hold", "quitter exited", "X", "Y"]


async def test_cm55_the_exit_sits_immediately_behind_the_childs_last_message():
    """Not at the end of that message's priority: another sender's message
    queued after the child's last one, but before the exit arrived, is still
    overtaken by the exit. Only the dying child's own mail holds it back."""
    parent, ref = await start("talker", "other")
    ref.tell(Hold())
    await settle()

    parent.children["talker"].tell(Say("A"))
    await settle()
    parent.children["other"].tell(Say("X"))
    await settle()
    parent.children["talker"].tell(Quit())
    await settle()
    parent.release.set()
    await settle()

    assert parent.heard == ["hold", "A", "talker exited", "X"]


async def test_cm56_an_exit_behind_normal_mail_waits_for_a_gated_wait_to_end():
    """No message's priority changes, so a gated wait under a NORMAL handler
    lets in neither the child's NORMAL report nor the exit queued behind it.
    Both are handled after the wait, in order. The cost of the rule, accepted:
    such an exit is not prompt."""
    parent, ref = await start("talker")
    ref.tell(Wait())
    await settle()

    parent.children["talker"].tell(Say("A"))
    parent.children["talker"].tell(Quit())
    await settle()
    assert parent.heard == ["wait"]

    parent.release.set()
    await settle()
    assert parent.heard == ["wait", "waited", "A", "talker exited"]


async def test_cm56_an_exit_with_nothing_queued_still_gets_into_a_gated_wait():
    parent, ref = await start("quitter")
    ref.tell(Wait())
    await settle()

    parent.children["quitter"].tell(Quit())
    await settle()
    assert parent.heard == ["wait", "quitter exited"]

    parent.release.set()
    await settle()
    assert parent.heard == ["wait", "quitter exited", "waited"]


async def test_cm57_an_exit_noticed_by_heartbeat_also_waits_for_the_childs_mail():
    """However the death is noticed, the exit goes through the same rule: a
    child that went silent after its last report has that report heard
    first."""
    clock = VirtualClock()
    policy = HeartbeatPolicy(interval=1.0, timeout=0.5)
    parent, ref = await start("talker", clock=clock, heartbeat=policy)
    ref.tell(Hold())
    await settle()

    parent.children["talker"].tell(Say("A"))
    await settle()
    parent.links["talker"].sever()
    await clock.advance(1.5)  # ping at 1.0, unanswered by 1.5
    await settle()
    assert len(parent.exits) == 0 and parent.heard == ["hold"]  # queued, not handled

    parent.release.set()
    await settle()

    assert parent.heard == ["hold", "A", "talker exited"]
    [exited] = parent.exits
    assert "heartbeat" in exited.error
