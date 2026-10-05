"""Devices in processes of their own.

A child is a fresh interpreter that connects back to an endpoint this host
listens on, identifying itself with a one-time token. It is given the actor's
factory as a pickle, which is why an actor must be constructible from plain
config: the parent's object graph does not cross, and a factory that cannot be
rebuilt is refused before any process starts.

Only control traffic crosses here. Frames go through shared memory, which is
machine-local, so the data flow never touches this link.
"""

import asyncio
import pickle
import secrets
import sys
from collections.abc import Callable
from dataclasses import dataclass, field

import msgpack

from stacscope.core.actors import (
    Actor,
    ActorDied,
    ActorRef,
    ChildWatch,
    HeartbeatPolicy,
    adopt,
)
from stacscope.core.protocol import MESSAGES
from stacscope.core.transport.codec import Codec
from stacscope.core.transport.stream import Connection, read_frame, runtime_address, send_frame
from stacscope.core.transport.wire import Wire

CHILD_MODULE = "stacscope.core.transport.child"

DEFAULT_HEARTBEAT = HeartbeatPolicy(interval=1.0, timeout=5.0)
"""Mandatory for a process child, never optional. The link catches a process
that dies; only a heartbeat catches one that is still alive and no longer
answering, which for a wedged driver is the likelier failure -- and the safe
answer must not be one a config can forget."""

STOP_GRACE = 5.0
"""How long a child gets to run its device's ``teardown`` before it is killed.
Releasing the hardware is worth waiting for; waiting forever is not."""


class LaunchFailed(Exception):
    """A device could not be started in a process of its own."""


@dataclass
class _Child:
    name: str
    address: str
    process: asyncio.subprocess.Process
    connection: Connection
    watch: ChildWatch | None = None
    tasks: list[asyncio.Task] = field(default_factory=list)
    last_word: str | None = None
    """Why the child's actor ended, if its runtime got the chance to say."""
    heard_last_word: bool = False


# The point: a vendor driver that segfaults should kill one process, not the
# engine. Process exit is also a better death signal than any in-process
# callback -- nothing can be missed, and nothing has to be running to notice
# it.
class ProcessHost:
    """Launches actors in their own processes and holds this end of each link.

    Bound to the loopback interface: a child is a local process, and the token
    is what distinguishes it from anything else that can reach the port.
    """

    def __init__(
        self,
        *,
        interface: str = "127.0.0.1",
        port: int = 0,
        connect_timeout: float = 15.0,
        heartbeat: HeartbeatPolicy = DEFAULT_HEARTBEAT,
        python: str | None = None,
    ) -> None:
        self._interface = interface
        self._port = port
        self._connect_timeout = connect_timeout
        self._heartbeat = heartbeat
        self._python = python or sys.executable
        self._wire = Wire(Codec(MESSAGES))
        self._server: asyncio.Server | None = None
        self._expected: dict[str, asyncio.Future] = {}
        self._children: dict[str, _Child] = {}
        self.endpoint = ""
        self.errors: list[Exception] = []
        """Frames that arrived but could not be delivered. Diagnostic."""

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, self._interface, self._port)
        host, port = self._server.sockets[0].getsockname()[:2]
        self.endpoint = f"{host}:{port}"

    def running(self) -> int:
        """How many child processes this host is responsible for."""
        return len(self._children)

    def process(self, name: str) -> asyncio.subprocess.Process | None:
        """The process a device is running in, while it is still running."""
        child = self._children.get(name)
        return child.process if child is not None else None

    async def launch(
        self,
        parent: Actor,
        *,
        name: str,
        factory: Callable[[], Actor],
        heartbeat: HeartbeatPolicy | None = None,
    ) -> ActorRef:
        """Start ``factory`` as an actor in a new process, supervised by ``parent``.

        Returns once the actor is running and reachable -- or raises
        ``LaunchFailed``, so that a device which cannot start says so now
        rather than at the beginning of an experiment. The device's ``setup``
        must finish within the host's ``connect_timeout``.
        """
        if self._server is None:
            raise LaunchFailed(f"{name}: the process host has not been started")
        try:
            payload = pickle.dumps(factory)
        except Exception as exc:
            raise LaunchFailed(
                f"{name} cannot be rebuilt in another process: {exc!r}. "
                "A device is launched from plain config, not from a live object."
            ) from None

        address = f"{parent.ref.address}/{name}"
        token = secrets.token_hex(16)
        arrived: asyncio.Future = asyncio.get_running_loop().create_future()
        self._expected[token] = arrived
        process = await asyncio.create_subprocess_exec(
            self._python, "-m", CHILD_MODULE, self.endpoint, token, address
        )
        try:
            child = await self._bootstrap(process, name, address, arrived, payload)
        except Exception:
            await self._kill(process)
            raise
        finally:
            self._expected.pop(token, None)

        self._children[name] = child
        ref = child.connection.peer.ref_to(address)
        child.connection.peer.register(parent.ref)  # so the device can answer home
        child.connection.peer.register(  # and somewhere to say why it ended
            ActorRef(_LastWord(child), runtime_address(address))
        )
        child.watch = adopt(
            parent,
            ref,
            heartbeat=heartbeat or self._heartbeat,
            on_release=lambda: self._release(name),
            on_stop=lambda: self._stop(name),
        )
        child.tasks = [
            asyncio.create_task(child.connection.serve()),
            asyncio.create_task(self._reap(child)),
        ]
        return ref

    async def close(self) -> None:
        """Shut every child down, then stop listening.

        Closing the link is how a child is asked to go: it stops its device,
        which runs the ``teardown`` that releases the hardware, and exits. One
        that does not is killed, because a shutdown that can hang is not one.
        """
        children, self._children = list(self._children.values()), {}
        await self._shut_down(children)
        if self._server is not None:
            self._server.close()
            # Bounded: waiting on it means waiting for every connection to be
            # let go, and a shutdown that can hang is not a shutdown.
            try:
                await asyncio.wait_for(self._server.wait_closed(), STOP_GRACE)
            except TimeoutError:
                pass
            self._server = None

    async def _stop(self, name: str) -> None:
        """Shut one child down, as ``close`` does: its parent is exiting."""
        child = self._children.pop(name, None)
        if child is not None:
            await self._shut_down([child])

    async def _shut_down(self, children: list[_Child]) -> None:
        for child in children:
            await child.connection.close()
        for child in children:
            try:
                await asyncio.wait_for(child.process.wait(), STOP_GRACE)
            except TimeoutError:
                await self._kill(child.process)
            for task in child.tasks:
                task.cancel()

    # --- the bootstrap handshake -------------------------------------------
    #
    # Three frames, outside the wire protocol, before any message crosses:
    #   child -> host   {"token": ...}      this is who I am
    #   host  -> child  {"factory": ...}    this is what to be
    #   child -> host   {"ready": ...} | {"error": ...}

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        frame = await asyncio.wait_for(read_frame(reader), self._connect_timeout)
        token = _field(frame, "token")
        arrived = self._expected.get(token) if isinstance(token, str) else None
        if arrived is None or arrived.done():
            writer.close()  # not a child of ours, or one we have stopped waiting for
            return
        arrived.set_result((reader, writer))

    async def _bootstrap(self, process, name, address, arrived, payload) -> _Child:
        try:
            reader, writer = await asyncio.wait_for(arrived, self._connect_timeout)
        except TimeoutError:
            raise LaunchFailed(
                f"{name}: no process connected within {self._connect_timeout} s"
            ) from None
        send_frame(writer, msgpack.packb({"factory": payload}))
        try:
            # The device's setup runs inside this wait: opening a camera can be
            # the slowest step of a launch.
            frame = await asyncio.wait_for(read_frame(reader), self._connect_timeout)
        except TimeoutError:
            raise LaunchFailed(
                f"{name}: did not finish starting within {self._connect_timeout} s"
            ) from None
        error = _field(frame, "error")
        if error is not None:
            raise LaunchFailed(f"{name}: {error}")
        if _field(frame, "ready") != address:
            raise LaunchFailed(f"{name}: the process did not start the device")
        connection = Connection(reader, writer, self._wire, on_error=self.errors.append)
        return _Child(name, address, process, connection)

    # --- death --------------------------------------------------------------

    async def _reap(self, child: _Child) -> None:
        """The prompt path: no heartbeat has to expire for a dead child.

        Two independent signals, because neither can be trusted alone. A
        process exit carries a status and a reason; a closed link carries
        neither but cannot be missed. Waiting only for the exit was enough
        everywhere but one build, where a child killed by a signal is not
        reaped promptly and its death went unreported until the heartbeat --
        which is the backstop, not the mechanism.
        """
        exited = asyncio.create_task(child.process.wait())
        closed = asyncio.create_task(child.connection.closed.wait())
        try:
            await asyncio.wait({exited, closed}, return_when=asyncio.FIRST_COMPLETED)
            # Let the other catch up: the exit status and the child's own last
            # word both say more than "the link went". Briefly, then report.
            # This wait is also what keeps the device's last answer ahead of
            # its exit at the parent: the link's ``closed`` is set only once
            # its last frame is delivered, and the parent's runtime puts an
            # exit behind the child's queued mail. A link still draining after
            # the second loses that order; a local socket whose writer has
            # gone drains in milliseconds (CM58).
            await asyncio.wait({exited, closed}, timeout=1.0)
        finally:
            exited.cancel()
            closed.cancel()
        if child.watch is not None:
            child.watch.report(self._why(child))

    @staticmethod
    def _why(child: _Child) -> str | None:
        """What killed it, said the way an in-process death would say it."""
        if child.heard_last_word:
            return child.last_word
        code = child.process.returncode
        if code is None:
            return "the link to its process closed"
        return None if code == 0 else f"process exited with status {code}"

    def _release(self, name: str) -> None:
        """Runs just before ``ChildExited``, however the death was noticed. A
        child declared dead by heartbeat is still running, and must not be."""
        child = self._children.pop(name, None)
        if child is not None and child.process.returncode is None:
            _terminate(child.process)

    async def _kill(self, process: asyncio.subprocess.Process) -> None:
        _terminate(process)
        try:
            await asyncio.wait_for(process.wait(), STOP_GRACE)
        except TimeoutError:
            pass  # killed and unreaped is still gone; nothing is served by waiting


class _LastWord:
    """Where a child runtime's ``ActorDied`` lands. Not an actor: nothing
    handles it, and it exists only so the reason survives the boundary."""

    def __init__(self, child: _Child) -> None:
        self._child = child

    def deliver(self, envelope, priority) -> None:
        if isinstance(envelope.msg, ActorDied):
            self._child.last_word = envelope.msg.reason
            self._child.heard_last_word = True


def _terminate(process: asyncio.subprocess.Process) -> None:
    try:
        process.kill()
    except (ProcessLookupError, OSError):
        pass  # already gone, which is all we wanted


def _field(frame: bytes | None, name: str) -> object:
    """One field of a bootstrap frame. Anything unexpected reads as absent."""
    if frame is None:
        return None
    try:
        raw = msgpack.unpackb(frame, raw=False)
    except Exception:
        return None
    return raw.get(name) if isinstance(raw, dict) else None
