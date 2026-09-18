"""Publish/subscribe: how state (SourcePhase, Fault, ...) reaches interested actors.

Subscriptions are handled by the runtime, so every actor can publish without
writing its own subscriber bookkeeping.
"""

import asyncio
from dataclasses import dataclass

from stacscope.core.actors import Actor, Subscribe, Unsubscribe, spawn


@dataclass(frozen=True)
class News:
    n: int


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)


class Publisher(Actor):
    def __init__(self):
        super().__init__()
        self.handled = []

    async def handle(self, msg):
        self.handled.append(msg)
        if isinstance(msg, News):
            self.publish(msg)


async def _settle():
    for _ in range(10):
        await asyncio.sleep(0)


async def test_subscribers_receive_what_is_published():
    listeners = [Listener(), Listener()]
    publisher = spawn(Publisher())
    for listener in listeners:
        publisher.tell(Subscribe(spawn(listener)))

    publisher.tell(News(1))
    await _settle()

    assert [listener.heard for listener in listeners] == [[News(1)], [News(1)]]


async def test_unsubscribed_actors_hear_nothing_further():
    listener = Listener()
    ref = spawn(listener)
    publisher = spawn(Publisher())
    publisher.tell(Subscribe(ref))
    publisher.tell(News(1))
    publisher.tell(Unsubscribe(ref))
    publisher.tell(News(2))
    await _settle()

    assert listener.heard == [News(1)]


async def test_subscription_messages_never_reach_handle():
    actor = Publisher()
    ref = spawn(actor)
    ref.tell(Subscribe(spawn(Listener())))
    await _settle()

    assert actor.handled == []


async def test_subscribing_twice_delivers_once():
    listener = Listener()
    ref = spawn(listener)
    publisher = spawn(Publisher())
    publisher.tell(Subscribe(ref))
    publisher.tell(Subscribe(ref))
    publisher.tell(News(1))
    await _settle()

    assert listener.heard == [News(1)]
