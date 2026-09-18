"""R1 — priority mailbox.

A STOP enqueued behind 100 LOW self-messages is handled next; FIFO within a
priority; unorderable payloads are never compared.
"""

import asyncio
from dataclasses import dataclass

from stacscope.core.actors import Actor, Mailbox, Priority, spawn


@dataclass
class Tick:
    n: int


@dataclass
class Stop:
    pass


@dataclass
class Kick:
    pass


async def test_urgent_is_received_before_queued_low():
    mailbox = Mailbox()
    for n in range(100):
        mailbox.put(Tick(n), Priority.LOW)
    mailbox.put(Stop(), Priority.URGENT)

    assert await mailbox.get() == Stop()


async def test_fifo_within_each_priority():
    mailbox = Mailbox()
    for n in range(10):
        mailbox.put(Tick(n), Priority.LOW)
        mailbox.put(Tick(100 + n), Priority.NORMAL)

    received = [await mailbox.get() for _ in range(20)]

    assert received == [Tick(100 + n) for n in range(10)] + [Tick(n) for n in range(10)]


async def test_unorderable_payloads_are_never_compared():
    mailbox = Mailbox()
    # dicts raise TypeError on `<`; a tie on priority must not fall through to them
    mailbox.put({"a": 1}, Priority.NORMAL)
    mailbox.put({"a": 2}, Priority.NORMAL)

    assert await mailbox.get() == {"a": 1}
    assert await mailbox.get() == {"a": 2}


async def test_actor_handles_stop_behind_its_own_low_self_messages():
    class Looper(Actor):
        def __init__(self):
            super().__init__()
            self.handled = []

        async def handle(self, msg):
            self.handled.append(msg)
            match msg:
                case Kick():
                    for n in range(100):
                        self.ref.tell(Tick(n), Priority.LOW)
                    self.ref.tell(Stop(), Priority.URGENT)
                case Stop():
                    self.stop()

    actor = Looper()
    ref = spawn(actor)
    ref.tell(Kick())

    await asyncio.wait_for(actor.stopped.wait(), timeout=1)

    assert actor.handled == [Kick(), Stop()]
