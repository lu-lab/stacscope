"""Actor base class and lifecycle."""

import asyncio
import itertools
import re
from collections.abc import Callable

from stacscope.core.actors.mailbox import Mailbox, Priority
from stacscope.core.actors.messages import Fault, Heartbeat, HeartbeatReply, Subscribe, Unsubscribe
from stacscope.core.actors.ref import ActorRef, Envelope, LocalTransport, Transport, current_sender
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

    An actor knows its ``parent`` by address, and tells it what nobody asked
    for with ``report``. Every message arrives with its ``sender``, and
    ``reply`` answers it.
    """

    def __init__(self) -> None:
        self._mailbox = Mailbox()
        self._stopping = False
        self._task: asyncio.Task[None] | None = None
        self._sender: ActorRef | None = None
        self._children: list[ChildWatch] = []
        self._child_names: set[str] = set()
        self._subscribers: dict[ActorRef, None] = {}  # ordered set
        self.ref: ActorRef | None = None
        self.parent: ActorRef | None = None
        """Who this actor reports to; ``None`` for a root. Set at spawn, and
        always the actor's own address less its last segment."""
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
                    self._sender = envelope.sender
                    try:
                        await self.handle(msg)
                    finally:
                        self._sender = None

    async def handle(self, msg: object) -> None:
        """Act on one message. The default passes a ``Fault`` to the parent
        (a root publishes it) and ignores everything else, so a subclass
        ends its own match with ``case _: await super().handle(msg)``, and
        acts on a ``Fault`` only in a case of its own."""
        # Every actor's use for a Fault is to pass it up (user, 2026-10-02):
        # it climbs until something acts on it, and the root tells the
        # clients. Anything else an actor has no use for is ignored, told or
        # asked (user, 2026-09-30).
        if isinstance(msg, Fault):
            self.report(msg)

    async def teardown(self) -> None:
        pass

    @property
    def sender(self) -> ActorRef | None:
        """Who sent the message being handled: an actor, an ``ask``'s
        one-shot address, or ``None`` from outside any actor.

        Capture it to answer after ``handle`` returns.
        """
        return self._sender

    def reply(self, msg: object) -> None:
        """Answer the message being handled, by telling its sender. A no-op
        when it has none."""
        if self._sender is not None:
            self._sender.tell(msg)

    def report(self, msg: object, priority: Priority = Priority.NORMAL) -> None:
        """Tell the parent something nobody asked for: a ``Fault``, a phase.
        A root has no parent, and publishes it instead."""
        if self.parent is not None:
            self.parent.tell(msg, priority)
        else:
            self.publish(msg, priority)

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
            if envelope.sender is not None and not self.stopped.is_set():
                envelope.sender.tell(HeartbeatReply(), sender=self.ref)
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
    parent: "Actor | ActorRef | None" = None,
    clock: Clock | None = None,
    heartbeat: HeartbeatPolicy | None = None,
    link: Link | None = None,
    name: str | None = None,
) -> ActorRef:
    """Start ``actor`` on the running event loop and return its address.

    ``name`` must match ``[A-Za-z0-9_.-]+``; by default it is derived from the
    class name. With a ``parent``, the address is the parent's path plus the
    name and the actor reports to it; a parent ``Actor`` also hears the
    actor's exit, as ``ChildExited``.

    A parent given as an ``ActorRef`` is in another process: the actor hangs
    from its address and reports to it, and whoever holds the link watches
    it from the far side.
    """
    if name is None:
        name = f"{type(actor).__name__}-{next(_default_ids)}"
    elif not _NAME.fullmatch(name):
        raise ValueError(f"actor name {name!r} must match {_NAME.pattern}")
    local_parent = parent if isinstance(parent, Actor) else None
    if local_parent is not None:
        _reserve(local_parent, name)
        parent = local_parent.ref
    address = name if parent is None else f"{parent.address}/{name}"

    transport: Transport = LocalTransport(actor._receive)
    ref = actor.ref = ActorRef(link(transport) if link else transport, address)
    actor.parent = parent
    if clock is not None:
        actor.clock = clock

    watch = None
    if local_parent is not None:
        watch = _watch(local_parent, ref, name, heartbeat, actor.clock, None)

    async def lifecycle() -> None:
        current_sender.set(ref)  # this task is the actor, so its tells are from it
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
