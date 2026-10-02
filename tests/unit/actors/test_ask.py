"""R2 — ``ask`` returns the reply; with no reply it times out in virtual time."""

import asyncio
from dataclasses import dataclass

import pytest

from stacscope.adapters.simulated import VirtualClock
from stacscope.core.actors import Actor, AskTimeout, ask, spawn


@dataclass
class Ping:
    n: int


@dataclass
class Pong:
    n: int


class Echo(Actor):
    async def handle(self, msg):
        match msg:
            case Ping(n):
                self.reply(Pong(n))


class Silent(Actor):
    async def handle(self, msg):
        pass


async def test_ask_returns_the_reply():
    ref = spawn(Echo())

    assert await ask(ref, Ping(7), timeout=1.0, clock=VirtualClock()) == Pong(7)


async def test_ask_times_out_exactly_at_the_deadline_in_virtual_time():
    clock = VirtualClock()
    ref = spawn(Silent())
    asking = asyncio.create_task(ask(ref, Ping(1), timeout=2.0, clock=clock))

    await clock.advance(1.999)
    assert not asking.done()

    await clock.advance(0.001)
    assert asking.done()
    with pytest.raises(AskTimeout):
        await asking


async def test_a_tell_from_outside_any_actor_carries_no_sender():
    class Replier(Actor):
        def __init__(self):
            super().__init__()
            self.sender_seen = "unset"

        async def handle(self, msg):
            self.sender_seen = self.sender
            self.stop()

    actor = Replier()
    spawn(actor).tell(Ping(0))
    await asyncio.wait_for(actor.stopped.wait(), timeout=1)

    assert actor.sender_seen is None
