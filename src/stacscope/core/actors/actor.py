"""Actor base class and lifecycle."""

import asyncio
import itertools
import re
from collections.abc import Callable

from stacscope.core.actors.mailbox import Mailbox, Priority
from stacscope.core.actors.messages import Heartbeat, HeartbeatReply, Subscribe, Unsubscribe
from stacscope.core.actors.ref import ActorRef, Envelope, LocalTransport, Transport
from stacscope.core.actors.supervision import ChildWatch, HeartbeatPolicy
from stacscope.core.ports.clock import Clock, MonotonicClock

type Link = Callable[[Transport], Transport]
"""Wraps the transport a new actor is reached through (composition-root hook)."""

_NAME = re.compile(r"[A-Za-z0-9_.-]+")
_default_ids = itertools.count()


class _WakeUp:
    """Put in the mailbox by ``stop`` so a loop waiting on an empty one notices.

    Without it, an actor told to stop from outside would sit in ``get()`` until
    something else happened to arrive -- and its ``teardown``, which is what
    releases the hardware, would never run.
    """


class Actor:
    """Base actor: ``setup()`` → ``run()`` → ``teardown()``.

    ``teardown`` runs in a ``finally`` covering both ``setup`` and ``run``, so a
    device opened partway through a failed ``setup`` is still released.
    Subclasses normally override ``handle`` and leave ``run`` as the message loop.
    """

    def __init__(self) -> None:
        self._mailbox = Mailbox()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._reply_to: ActorRef | None = None
        self._children: list[ChildWatch] = []
        self._child_names: set[str] = set()
        self._subscribers: dict[ActorRef, None] = {}  # ordered set
        self.ref: ActorRef | None = None
        self.clock: Clock = MonotonicClock()
        self.started = asyncio.Event()
        """Set once ``setup`` has returned and the mailbox is being served."""
        self.stopped = asyncio.Event()
        self.exit_error: Exception | None = None
        """Why the actor ended, or ``None`` for a clean ``stop()``."""

    async def setup(self) -> None:
        pass

    async def run(self) -> None:
        while not self._stopping:
            envelope: Envelope = await self._mailbox.get()
            match envelope.msg:
                case Subscribe(subscriber):
                    # Refs are equal by address, so a relaunched actor's ref is
                    # equal to its dead predecessor's and a plain assignment
                    # would keep the old key -- and publish into the void.
                    self._subscribers.pop(subscriber, None)
                    self._subscribers[subscriber] = None
                case Unsubscribe(subscriber):
                    self._subscribers.pop(subscriber, None)
                case _WakeUp():
                    pass  # only here to end the wait above
                case msg:
                    self._reply_to = envelope.reply_to
                    try:
                        await self.handle(msg)
                    finally:
                        self._reply_to = None

    async def handle(self, msg: object) -> None:
        pass

    async def teardown(self) -> None:
        pass

    @property
    def reply_to(self) -> ActorRef | None:
        """Reply address of the message being handled; ``None`` if it was told.

        Capture it to reply after ``handle`` returns.
        """
        return self._reply_to

    def reply(self, msg: object) -> None:
        """Answer the message being handled. A no-op if it was told, not asked,
        so a handler works the same either way."""
        if self._reply_to is not None:
            self._reply_to.tell(msg)

    def publish(self, msg: object, priority: Priority = Priority.NORMAL) -> None:
        """Send ``msg`` to every current subscriber."""
        for subscriber in self._subscribers:
            subscriber.tell(msg, priority)

    def stop(self) -> None:
        """End the message loop once the current message has been handled.

        Safe to call from outside the actor -- which is how a child process
        shuts its device down when the engine goes away.
        """
        self._stopping = True
        self._mailbox.put(Envelope(_WakeUp()), Priority.URGENT)

    def spawn_child(
        self,
        child: "Actor",
        *,
        heartbeat: HeartbeatPolicy | None = None,
        link: Link | None = None,
        name: str | None = None,
    ) -> ActorRef:
        """Start ``child`` on this actor's clock, addressed ``<own address>/<name>``.

        Its exit — or, with a ``heartbeat``, its silence — arrives here as an
        URGENT ``ChildExited``. Sibling names must be unique.
        """
        return spawn(
            child, parent=self, clock=self.clock, heartbeat=heartbeat, link=link, name=name
        )

    def _receive(self, envelope: Envelope, priority: Priority) -> None:
        """Receive side of every transport. Heartbeats are answered here, not
        in ``handle``, so they prove the runtime is alive however long a
        handler runs."""
        if isinstance(envelope.msg, Heartbeat):
            if envelope.reply_to is not None and not self.stopped.is_set():
                envelope.reply_to.tell(HeartbeatReply())
            return
        self._mailbox.put(envelope, priority)

    async def _lifecycle(self) -> None:
        try:
            await self.setup()
            self.started.set()
            await self.run()
        except Exception as exc:
            self.exit_error = exc
        finally:
            try:
                await self.teardown()
            except Exception as exc:
                if self.exit_error is None:
                    self.exit_error = exc
                else:
                    self.exit_error.add_note(f"teardown also failed: {exc!r}")
            finally:
                for watch in list(self._children):
                    watch.close()
                self.stopped.set()


def describe(error: BaseException | None) -> str | None:
    """How an actor's ``exit_error`` is reported to whoever supervises it:
    its ``repr`` followed by any notes, or ``None`` for a clean stop."""
    # Not plain repr: notes are how a failed teardown rides along on the error
    # that caused the shutdown (see `_lifecycle`), and repr leaves them out.
    # Everything past this point is a string, so a note not copied here is gone.
    if error is None:
        return None
    return "; ".join([repr(error), *getattr(error, "__notes__", ())])


def adopt(
    parent: Actor,
    child: ActorRef,
    *,
    heartbeat: HeartbeatPolicy | None = None,
    clock: Clock | None = None,
    on_release: Callable[[], None] | None = None,
) -> ChildWatch:
    """Supervise a child this actor did not start -- one in another process.

    Supervision needs only an address: exits are reported by whoever notices
    them, and a heartbeat travels like any other message. ``on_release`` runs
    with the name, just before ``ChildExited``, for whatever else the far side
    owns -- a process to be reaped, a connection to be closed.
    """
    name = child.address.rsplit("/", 1)[-1]
    _reserve(parent, name)
    return _watch(parent, child, name, heartbeat, clock or parent.clock, on_release)


def spawn(
    actor: Actor,
    *,
    parent: Actor | None = None,
    clock: Clock | None = None,
    heartbeat: HeartbeatPolicy | None = None,
    link: Link | None = None,
    name: str | None = None,
    address: str | None = None,
) -> ActorRef:
    """Start ``actor`` on the running event loop and return its address.

    ``name`` must match ``[A-Za-z0-9_.-]+``; by default it is derived from the
    class name. With a ``parent``, the address is the parent's path plus the
    name, and the actor's exit is reported to the parent as ``ChildExited``.

    ``address`` places the actor at an explicit path instead. It is for an
    actor whose parent is in another process: the hierarchy it belongs to is
    real, but no part of it is here to hang the name from.
    """
    if address is not None and parent is not None:
        raise ValueError("an actor with a parent takes its address from it")
    if name is None:
        name = f"{type(actor).__name__}-{next(_default_ids)}"
    elif not _NAME.fullmatch(name):
        raise ValueError(f"actor name {name!r} must match {_NAME.pattern}")
    if parent is not None:
        _reserve(parent, name)
    if address is None:
        address = name if parent is None else f"{parent.ref.address}/{name}"

    transport: Transport = LocalTransport(actor._receive)
    ref = actor.ref = ActorRef(link(transport) if link else transport, address)
    if clock is not None:
        actor.clock = clock

    watch = None
    if parent is not None:
        watch = _watch(parent, ref, name, heartbeat, actor.clock, None)

    async def lifecycle() -> None:
        cancelled = False
        try:
            await actor._lifecycle()
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            if watch is not None:
                if cancelled:
                    watch.report("cancelled")
                else:
                    watch.report(describe(actor.exit_error))

    actor._task = asyncio.create_task(lifecycle())
    return ref


def _reserve(parent: Actor, name: str) -> None:
    if name in parent._child_names:
        raise ValueError(f"{parent.ref.address} already has a child named {name!r}")
    parent._child_names.add(name)


def _watch(
    parent: Actor,
    child: ActorRef,
    name: str,
    heartbeat: HeartbeatPolicy | None,
    clock: Clock,
    on_release: Callable[[], None] | None,
) -> ChildWatch:
    watch: ChildWatch

    def release() -> None:
        parent._child_names.discard(name)
        parent._children.remove(watch)
        if on_release is not None:
            on_release()

    watch = ChildWatch(parent.ref, child, release)
    parent._children.append(watch)
    if heartbeat is not None:
        watch.start_heartbeat(heartbeat, clock)
    return watch
