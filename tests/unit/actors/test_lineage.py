"""CM1, CM3–CM6 -- the parent/child rule, as the runtime gives it.

Commands go down and reports go up. A child reaches its parent by address,
the address its own address already implies; whatever it has to say that
nobody asked for, it tells its parent. A ``Fault`` climbs the tree until
something acts on it, and a root publishes it. Every envelope says who sent
it, so an answer is an ordinary tell back -- there is no special kind of
message for replies.
"""

import asyncio
from dataclasses import dataclass

from stacscope.core.actors import Actor, ActorRef, Fault, Subscribe, ask, spawn


@dataclass(frozen=True)
class Ping:
    pass


@dataclass(frozen=True)
class Pong:
    pass


@dataclass(frozen=True)
class Note:
    text: str


class Keeper(Actor):
    """Keeps what reaches ``handle``, with who sent it, and passes the rest
    on to the base class -- so a ``Fault`` it receives goes where any
    actor's would."""

    def __init__(self, children=()):
        super().__init__()
        self.children = dict(children)
        self.refs: dict[str, ActorRef] = {}
        self.heard: list[tuple[object, ActorRef | None]] = []

    async def setup(self):
        for name, child in self.children.items():
            self.refs[name] = self.spawn_child(child, name=name)

    async def handle(self, msg):
        self.heard.append((msg, self.sender))
        match msg:
            case Ping():
                self.reply(Pong())
            case Note(text):
                self.report(Note(text.upper()))
            case _:
                await super().handle(msg)


class Absorber(Keeper):
    """Acts on a ``Fault`` in a case of its own, which is where it stops."""

    async def handle(self, msg):
        match msg:
            case Fault():
                self.heard.append((msg, self.sender))
            case _:
                await super().handle(msg)


class Sayer(Actor):
    """Tells someone something from inside its handler, and from a task it
    started -- a routine, say."""

    def __init__(self, to: ActorRef):
        super().__init__()
        self.to = to

    async def handle(self, msg):
        self.to.tell(Note("from the handler"))
        await asyncio.create_task(self._later())

    async def _later(self):
        self.to.tell(Note("from a task"))


async def settle():
    for _ in range(20):
        await asyncio.sleep(0)


def fault(text="it broke", fatal=False):
    return Fault(ActorRef(None, "somewhere"), text, fatal)


# --- CM1: a child's address is its lineage -----------------------------------


async def test_cm1_a_child_knows_its_parent_by_the_address_its_own_implies():
    """The runtime sets ``parent`` at spawn, and it is always the child's
    address less its last segment. If the two could disagree, a report could
    go somewhere the hierarchy says it should not."""
    grandchild = Keeper()
    child = Keeper({"camera": grandchild})
    root = Keeper({"coordinator": child})
    spawn(root, name="engine")
    await settle()

    assert root.parent is None
    assert child.parent == root.ref
    assert grandchild.parent == child.ref
    for actor in (child, grandchild):
        assert actor.parent.address == actor.ref.address.rsplit("/", 1)[0]


async def test_cm1_a_parent_in_another_place_is_given_by_its_ref():
    """A process child's parent is not in its process: the link hands the
    child a ref to it, and the child's address hangs from that ref's."""
    there = Keeper()
    parent = spawn(there, name="engine")
    child = Keeper()

    ref = spawn(child, parent=parent, name="camera")
    await settle()

    assert ref.address == "engine/camera"
    assert child.parent == parent


# --- CM3: unprompted reports go to the parent ---------------------------------


async def test_cm3_a_report_is_told_to_the_parent_and_nowhere_else():
    child = Keeper()
    root = Keeper({"child": child})
    spawn(root, name="root")
    listener = Keeper()
    await settle()
    child.ref.tell(Subscribe(spawn(listener)))  # a subscriber of the child's
    await settle()

    child.ref.tell(Note("hello"))
    await settle()

    assert (Note("HELLO"), child.ref) in root.heard
    assert listener.heard == []  # reports are not published by a child


async def test_cm3_a_root_has_no_parent_so_it_publishes_its_reports():
    root = Keeper()
    listener = Keeper()
    ref = spawn(root, name="root")
    ref.tell(Subscribe(spawn(listener)))
    await settle()

    ref.tell(Note("hello"))
    await settle()

    assert [msg for msg, _ in listener.heard] == [Note("HELLO")]


# --- CM4, CM5: a Fault climbs until something acts on it ----------------------


async def test_cm4_a_fault_climbs_to_the_root_which_publishes_it():
    """Every actor's use for a ``Fault`` is to pass it to its parent. This is
    not an exception to ignoring what it has no use for: every actor has this
    use. Unchanged on the way, so whoever finally acts on it learns who
    faulted, not who passed it on."""
    grandchild = Keeper()
    child = Keeper({"grandchild": grandchild})
    root = Keeper({"child": child})
    listener = Keeper()
    ref = spawn(root, name="root")
    ref.tell(Subscribe(spawn(listener)))
    await settle()
    broken = fault()

    grandchild.ref.tell(broken)
    await settle()

    assert (broken, grandchild.ref) in child.heard
    assert (broken, child.ref) in root.heard
    assert [msg for msg, _ in listener.heard] == [broken]


async def test_cm4_an_actor_with_no_handler_of_its_own_still_passes_a_fault_up():
    """The base ``handle`` is where the default lives, so an actor that
    overrides nothing has it too."""
    child = Actor()
    root = Keeper({"child": child})
    spawn(root, name="root")
    await settle()

    child.ref.tell(fault())
    await settle()

    assert [msg for msg, _ in root.heard] == [fault()]


async def test_cm4_a_fault_from_the_parent_is_not_sent_back_to_it():
    """Reports go up; a ``Fault`` that came *down* -- published by a parent to
    a child subscribed to it -- is not a report from below. Passed back, the
    parent would publish it again, and the two would trade it for ever."""
    child = Keeper()
    root = Keeper({"child": child})
    ref = spawn(root, name="root")
    await settle()
    ref.tell(Subscribe(child.ref))
    await settle()

    ref.tell(fault())
    await settle()

    assert [msg for msg, _ in child.heard] == [fault()]
    assert [msg for msg, _ in root.heard] == [fault()], "once, from outside; never back from the child"


async def test_cm5_an_actor_that_acts_on_a_fault_stops_it_there():
    child = Keeper()
    middle = Absorber({"child": child})
    root = Keeper({"middle": middle})
    spawn(root, name="root")
    await settle()

    child.ref.tell(fault())
    await settle()

    assert [msg for msg, _ in middle.heard] == [fault()]
    assert root.heard == []


# --- CM6: every envelope carries its sender ------------------------------------


async def test_cm6_a_tell_from_inside_an_actor_carries_that_actor_as_its_sender():
    """From its handler or from a task it started: either is the actor
    speaking, and whoever hears it can answer."""
    hearer = Keeper()
    hearer_ref = spawn(hearer, name="hearer")
    sayer = spawn(Sayer(hearer_ref), name="sayer")

    sayer.tell(Ping())
    await settle()

    assert hearer.heard == [(Note("from the handler"), sayer), (Note("from a task"), sayer)]


async def test_cm6_a_tell_from_outside_any_actor_has_no_sender():
    hearer = Keeper()
    spawn(hearer, name="hearer").tell(Note("from a client"))
    await settle()

    assert hearer.heard == [(Note("from a client"), None)]


async def test_cm6_reply_answers_the_sender_of_a_tell_as_of_an_ask():
    """Told and asked are the same thing: a reply is an ordinary tell back."""
    asker = Keeper()
    asker_ref = spawn(asker, name="asker")
    answerer = spawn(Keeper(), name="answerer")

    answerer.tell(Ping(), sender=asker_ref)
    await settle()

    assert asker.heard == [(Pong(), answerer)]
    assert await ask(answerer, Ping(), timeout=1) == Pong()
