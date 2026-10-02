"""The far side of a process boundary: one actor, one link home.

Run as ``python -m stacscope.core.transport.child <endpoint> <token> <address>``
by ``ProcessHost``. A fresh interpreter, holding nothing of the parent's but
the factory it is sent.

Every wait while starting is bounded, and every ending is an exit. A child
that cannot reach its endpoint, or is never told what to be, exits rather than
lingering. Once its device is running, the child serves it for as long as the
link home stays open; when the link closes, it stops its device -- running the
``teardown`` that releases the hardware -- and exits too. An orphan holding a
camera open is worse than no child at all.
"""

import asyncio
import pickle
import sys

import msgpack

from stacscope.core.actors import Actor, ActorDied, Priority, describe, spawn
from stacscope.core.protocol import MESSAGES
from stacscope.core.transport.codec import Codec
from stacscope.core.transport.stream import (
    Connection,
    read_frame,
    runtime_address,
    send_frame,
)
from stacscope.core.transport.wire import Wire

CONNECT_TIMEOUT = 30.0
STOP_GRACE = 5.0

UNREACHABLE = 10
"""Could not reach the endpoint it was given."""
UNINSTRUCTED = 11
"""Reached it, but was never told what to be."""
UNBUILT = 12
"""Told what to be, and could not be it. The parent is given the reason."""


async def serve(
    endpoint: str, token: str, address: str, *, bootstrap_timeout: float = CONNECT_TIMEOUT
) -> int:
    """Connect to the host at ``endpoint``, build the device it sends, and
    serve it as ``address`` until the link closes or the device stops.
    Returns the exit code. ``bootstrap_timeout`` bounds each step of
    starting: connecting, being told what to be, and the device's setup."""
    connected = await _connect(endpoint, bootstrap_timeout)
    if connected is None:
        return UNREACHABLE
    reader, writer = connected

    send_frame(writer, msgpack.packb({"token": token}))
    factory = _field(await _bounded(read_frame(reader), bootstrap_timeout), "factory")
    if not isinstance(factory, bytes):
        return UNINSTRUCTED

    # Held: the device's setup may already report to its parent, and nothing
    # may reach the host's end before the handshake's last frame.
    connection = Connection(reader, writer, Wire(Codec(MESSAGES)), held=True)
    actor = await _build(factory, address, connection, bootstrap_timeout)
    if isinstance(actor, str):
        send_frame(writer, msgpack.packb({"error": actor}))
        await connection.close()
        return UNBUILT

    connection.peer.register(actor.ref)
    serving = asyncio.create_task(connection.serve())
    send_frame(writer, msgpack.packb({"ready": address}))
    connection.release()

    # Unbounded, unlike everything above: the device runs for as long as the
    # acquisitions do. A bound here once stopped every healthy device 30 s
    # after launch (CR1). It cannot strand an orphan: the operating system
    # closes the link when the engine's process ends, however it ends.
    await _either(connection.closed.wait(), actor.stopped.wait(), timeout=None)
    actor.stop()  # whether the link went or the device did, this process is done
    await _bounded(actor.stopped.wait(), STOP_GRACE)
    # The last word: why the device ended, so that a death here reads the same
    # as a death at home. Pointless if the link is what went, and impossible if
    # the process is what went -- which is why an exit status is still a death.
    if not connection.closed.is_set():
        connection.peer.ref_to(runtime_address(address)).tell(
            ActorDied(describe(actor.exit_error)), Priority.URGENT
        )
    serving.cancel()
    await connection.close()
    return 0


async def _connect(endpoint: str, timeout: float):
    host, _, port = endpoint.rpartition(":")
    try:
        return await asyncio.wait_for(asyncio.open_connection(host, int(port)), timeout)
    except (OSError, TimeoutError, ValueError) as exc:
        print(f"no engine at {endpoint}: {exc!r}", file=sys.stderr)
        return None


async def _build(factory: bytes, address: str, connection: Connection, timeout: float) -> Actor | str:
    """The actor, or why there is not one. Construction and ``setup`` both
    count: a device that cannot open its hardware has not started."""
    # Its parent is across the link: the address says who it is, and the
    # link reaches it, as it reaches anything not registered on this side.
    lineage, _, name = address.rpartition("/")
    parent = connection.peer.ref_to(lineage) if lineage else None
    try:
        actor = pickle.loads(factory)()
        spawn(actor, parent=parent, name=name)
    except Exception as exc:
        return repr(exc)
    await _either(actor.started.wait(), actor.stopped.wait(), timeout=timeout)
    if actor.started.is_set():
        return actor
    if actor.stopped.is_set():
        return describe(actor.exit_error) or "the device stopped while starting"
    actor.stop()  # its setup is still running; leaving the process ends it
    return f"the device did not finish starting within {timeout} s"


async def _either(*waits, timeout: float | None) -> None:
    """Return when the first of ``waits`` does, or after ``timeout``. The rest
    are abandoned."""
    tasks = [asyncio.create_task(wait) for wait in waits]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED, timeout=timeout)
    finally:
        for task in tasks:
            task.cancel()


async def _bounded(awaitable, timeout: float = CONNECT_TIMEOUT):
    try:
        return await asyncio.wait_for(awaitable, timeout)
    except (TimeoutError, OSError):
        return None


def _field(frame: bytes | None, name: str) -> object:
    if frame is None:
        return None
    try:
        raw = msgpack.unpackb(frame, raw=False)
    except Exception:
        return None
    return raw.get(name) if isinstance(raw, dict) else None


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(f"usage: python -m {__spec__.name} <endpoint> <token> <address>", file=sys.stderr)
        return 2
    return asyncio.run(serve(*argv))


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
