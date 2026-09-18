"""Request/reply over one-way messages."""

import asyncio
import itertools

from stacscope.core.actors.mailbox import Priority
from stacscope.core.actors.ref import ActorRef, Envelope
from stacscope.core.ports.clock import Clock, MonotonicClock


_reply_ids = itertools.count()


class AskTimeout(TimeoutError):
    """No reply arrived before the deadline. A late reply is discarded."""


class _ReplyTransport:
    """One-shot address that resolves a future with the first message delivered."""

    def __init__(self, future: asyncio.Future[object]) -> None:
        self._future = future

    def deliver(self, envelope: Envelope, priority: Priority) -> None:
        if not self._future.done():
            self._future.set_result(envelope.msg)


async def ask(
    ref: ActorRef,
    msg: object,
    *,
    timeout: float,
    clock: Clock | None = None,
    priority: Priority = Priority.NORMAL,
) -> object:
    """Send ``msg`` and await the reply, raising ``AskTimeout`` after ``timeout``."""
    reply = asyncio.get_running_loop().create_future()
    reply_to = ActorRef(_ReplyTransport(reply), f"{ref.address}#reply-{next(_reply_ids)}")
    ref.tell(msg, priority, reply_to=reply_to)
    try:
        return await (clock or MonotonicClock()).wait_for(reply, timeout)
    except TimeoutError as exc:
        raise AskTimeout(f"no reply to {type(msg).__name__} within {timeout} s") from exc
