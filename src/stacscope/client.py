"""Drive a running STACScope engine from Python: scripts, notebooks, hooks.

The engine must be running with its gateway (stacscope serve). The client
connects to it over a WebSocket, on this computer by default.

    from stacscope.client import Client

    with Client() as engine:
        print(engine.status())
        number = engine.start(cycles=10, root="fish-01")
        with engine.subscribe() as events:
            for msg in events:
                print(msg)          # Status changes, faults, phases
                if ...:
                    break
        engine.stop()

The Coordinator's own requests are methods: status, start, stop, relaunch.
So are a device's properties, by the device's name:

    engine.properties("flash")                 every property, by name
    engine.set("flash", "EXPOSURE TIME", 0.01)  what the set changed
    engine.set_many("ueye", {"aoi_x": 640, "aoi_width": 640})

They raise Refused, with the engine's reason, if it refuses. Anything else
is a message sent to an actor with ask (which waits for the answer and
returns it, whatever it is) or tell (which does not wait):

    engine.ask(engine.source(), SetWaveform(Waveform.MAIN, ao, do))

Every call that waits gives up after the client's timeout (10 s by
default), raising AskTimeout. If the connection drops, every call waiting
and every call after raises Disconnected; connect again with a new Client.
Nothing is queued to be sent later.
"""

import contextlib
import itertools
import queue
import threading

from websockets.exceptions import ConnectionClosed, InvalidHandshake, InvalidURI
from websockets.sync.client import connect

from stacscope.core.actors import (
    Accepted,
    ActorRef,
    AskTimeout,
    Envelope,
    Priority,
    Rejected,
    Subscribe,
    Unsubscribe,
)
from stacscope.core.protocol import (
    MESSAGES,
    AcquisitionStarted,
    AcquisitionStopped,
    GetProperties,
    GetStatus,
    Properties,
    PropertiesChanged,
    PropertiesRefused,
    Property,
    Relaunch,
    SetProperties,
    SetProperty,
    StartAcquisition,
    Status,
    StopAcquisition,
)
from stacscope.core.transport import Codec, CodecError, Wire
from stacscope.core.transport.stream import MAX_FRAME

__all__ = ["DEFAULT_URL", "AskTimeout", "Client", "Disconnected", "Refused", "Subscription"]

DEFAULT_URL = "ws://127.0.0.1:8765"
"""Where ``stacscope serve`` listens unless the rig's config says otherwise."""

TIMEOUT = 10.0
"""Seconds a call waits for its answer, by default."""

COORDINATOR = "coordinator"

_GONE = object()
"""Put in every waiting queue when the connection drops."""


class Disconnected(ConnectionError):
    """The connection to the engine is gone, or was never made."""


class Refused(Exception):
    """The engine refused a request. ``reason`` is why, in its words.
    ``changed`` holds the properties a refused ``set_many`` had already
    changed before the refusal, which stay changed; it is empty otherwise."""

    def __init__(self, reason: str, changed: dict | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.changed = {} if changed is None else changed


class Client:
    """A connection to the engine at ``url``. Safe to use from several
    threads at once. ``timeout`` is how many seconds a call waits for its
    answer, unless the call says otherwise.

    Raises ``Disconnected`` if the engine cannot be reached."""

    def __init__(self, url: str = DEFAULT_URL, *, timeout: float = TIMEOUT) -> None:
        self.url = url
        self.timeout = timeout
        self._wire = Wire(Codec(MESSAGES))
        self._lock = threading.Lock()  # guards _waiting and _gone
        self._sending = threading.Lock()
        self._waiting: dict[str, queue.SimpleQueue] = {}
        self._gone = False
        self._ids = itertools.count(1)
        self._connection = contextlib.ExitStack()
        try:
            # legacy=False and a context: the form websockets keeps when its
            # older, direct form is retired.
            self._ws = self._connection.enter_context(
                connect(url, open_timeout=timeout, max_size=MAX_FRAME, compression=None, legacy=False)
            )
        except (OSError, TimeoutError, InvalidHandshake, InvalidURI) as exc:
            raise Disconnected(f"cannot reach the engine at {url}: {exc}") from exc
        self.coordinator = self._ref(COORDINATOR)
        """The Coordinator, which starts and stops acquisitions."""
        self._reader = threading.Thread(target=self._read, name="stacscope client", daemon=True)
        self._reader.start()

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def close(self) -> None:
        """Disconnect. An acquisition this client started keeps running."""
        self._connection.close()
        self._reader.join(self.timeout)

    # --- the Coordinator's requests -------------------------------------------------

    def status(self) -> Status:
        """The Coordinator's state, its latest acquisition's number, and
        every device, the timing source first."""
        return self._expect(self.ask(self.coordinator, GetStatus()), Status)

    def start(self, cycles: int = -1, root: str = "") -> int:
        """Start an acquisition of ``cycles`` cycles (negative: until
        ``stop``), recording to files named from ``root`` (empty: not
        recorded). Returns the acquisition's number once every device is
        ready and output has been told to start. Raises ``Refused``."""
        started = self._expect(self.ask(self.coordinator, StartAcquisition(cycles, root)), AcquisitionStarted)
        return started.acquisition

    def stop(self) -> int:
        """Stop the acquisition running. Returns its number once output has
        ceased and every device has stopped. Raises ``Refused`` if none is."""
        return self._expect(self.ask(self.coordinator, StopAcquisition()), AcquisitionStopped).acquisition

    def relaunch(self, name: str) -> None:
        """Start a fresh instance of the lost device ``name``. Returns once it
        is starting; a failed start is then reported as a Fault, to
        subscribers. Raises ``Refused``."""
        self._expect(self.ask(self.coordinator, Relaunch(name)), Accepted)

    def device(self, name: str) -> ActorRef:
        """The device ``name``, to send its own messages to. Raises
        ``LookupError`` if no such device is running."""
        for device in self.status().devices:
            if device.name == name and device.present and device.ref is not None:
                return device.ref
        raise LookupError(f"no device {name!r} is running")

    def source(self) -> ActorRef:
        """The timing source, which takes the waveforms. Raises
        ``LookupError`` if it is not running."""
        for device in self.status().devices:
            if device.source and device.present and device.ref is not None:
                return device.ref
        raise LookupError("the timing source is not running")

    # --- a device's properties ------------------------------------------------------

    def properties(self, device: str | ActorRef) -> dict[str, Property]:
        """Every property of ``device`` (its name, or its ref), by name, in
        the device's own order. Raises ``LookupError`` if no such device is
        running, and ``Refused``."""
        return dict(self._expect(self.ask(self._device(device), GetProperties()), Properties).properties)

    def set(self, device: str | ActorRef, name: str, value: bool | float | str) -> dict[str, Property]:
        """Set the property ``name`` of ``device`` (its name, or its ref) to
        ``value``: a number, a label, or True or False. Returns every
        property that changed, as the device now holds it, the one set
        among them. Raises ``Refused`` with the device's reason, in which
        case nothing changed, and ``LookupError``."""
        answer = self.ask(self._device(device), SetProperty(name, value))
        return dict(self._expect(answer, PropertiesChanged).properties)

    def set_many(self, device: str | ActorRef, values: dict[str, bool | float | str]) -> dict[str, Property]:
        """Set several properties of ``device`` in one request. The device
        applies them in its own order, whatever the dict's, and checks each
        as it comes. Returns every property that changed. Raises ``Refused``
        if one is refused: those the device had applied before it stay
        applied, and are in the exception's ``changed``. Raises
        ``LookupError``."""
        answer = self.ask(self._device(device), SetProperties(values))
        if isinstance(answer, PropertiesRefused):
            raise Refused(answer.reason, dict(answer.changed))
        return dict(self._expect(answer, PropertiesChanged).properties)

    def _device(self, device: str | ActorRef) -> ActorRef:
        return device if isinstance(device, ActorRef) else self.device(device)

    # --- any message ----------------------------------------------------------------

    def ask(
        self,
        to: ActorRef | str,
        msg: object,
        *,
        timeout: float | None = None,
        priority: Priority = Priority.NORMAL,
    ) -> object:
        """Send ``msg`` to ``to`` (a ref, or an address such as
        "coordinator") and return its answer, ``Rejected`` included. Raises
        ``AskTimeout`` if none comes within ``timeout`` seconds (the
        client's own by default), and ``Disconnected``."""
        address = _address(to)
        local = f"ask-{next(self._ids)}"
        answers = self._expect_at(local)
        try:
            self._send(address, msg, local, priority)
            seconds = self.timeout if timeout is None else timeout
            try:
                answer = answers.get(timeout=seconds)
            except queue.Empty:
                raise AskTimeout(f"no answer from {address} to {type(msg).__name__} within {seconds} s") from None
            if answer is _GONE:
                raise Disconnected(f"the connection to {self.url} dropped")
            return answer
        finally:
            with self._lock:
                self._waiting.pop(local, None)

    def tell(self, to: ActorRef | str, msg: object, priority: Priority = Priority.NORMAL) -> None:
        """Send ``msg`` to ``to`` and do not wait. Any answer is discarded.
        Raises ``Disconnected``."""
        self._send(_address(to), msg, None, priority)

    def subscribe(self, to: ActorRef | str | None = None) -> "Subscription":
        """Hear what ``to`` publishes: the Coordinator by default, which
        sends its current ``Status`` first, then every change, fault and
        phase; or the timing source, which publishes which MAIN plays when.
        Use it as an iterator or with ``get``, and ``close`` it (or use it in
        a ``with``) when done."""
        address = COORDINATOR if to is None else _address(to)
        local = f"sub-{next(self._ids)}"
        messages = self._expect_at(local)
        try:
            self._send(address, Subscribe(ActorRef(None, local)), None, Priority.NORMAL)
        except BaseException:
            with self._lock:
                self._waiting.pop(local, None)
            raise
        return Subscription(self, address, local, messages)

    # --- the connection ---------------------------------------------------------------

    def _ref(self, address: str) -> ActorRef:
        return ActorRef(_Through(self, address), address)

    def _expect_at(self, local: str) -> queue.SimpleQueue:
        messages: queue.SimpleQueue = queue.SimpleQueue()
        with self._lock:
            if self._gone:
                raise Disconnected(f"the connection to {self.url} dropped")
            self._waiting[local] = messages
        return messages

    def _send(self, to: str, msg: object, sender: str | None, priority: Priority) -> None:
        envelope = Envelope(msg, None if sender is None else ActorRef(None, sender))
        frame = self._wire.pack(to, envelope, priority)
        with self._sending:
            if self._gone:
                raise Disconnected(f"the connection to {self.url} dropped")
            try:
                self._ws.send(frame)
            except ConnectionClosed as exc:
                raise Disconnected(f"the connection to {self.url} dropped") from exc

    def _read(self) -> None:
        try:
            for data in self._ws:
                try:
                    to, envelope, _ = self._wire.unpack(data, resolve=self._ref)
                except CodecError:
                    continue  # unreadable here; whoever waited on it times out
                with self._lock:
                    messages = self._waiting.get(to)
                if messages is not None:
                    messages.put(envelope.msg)
        except ConnectionClosed:
            pass
        finally:
            with self._lock:
                self._gone = True
                waiting = list(self._waiting.values())
            for messages in waiting:
                messages.put(_GONE)

    @staticmethod
    def _expect[T](answer: object, cls: type[T]) -> T:
        if isinstance(answer, Rejected):
            raise Refused(answer.reason)
        if not isinstance(answer, cls):
            raise RuntimeError(f"expected {cls.__name__}, got {answer!r}")
        return answer


class Subscription:
    """What one actor publishes, as the client hears it. Made by
    ``Client.subscribe``."""

    def __init__(self, client: Client, to: str, local: str, messages: queue.SimpleQueue) -> None:
        self._client = client
        self._to = to
        self._local = local
        self._messages = messages
        self._closed = False

    def get(self, timeout: float | None = None) -> object:
        """The next message, waiting up to ``timeout`` seconds (for ever by
        default). Raises ``TimeoutError``, and ``Disconnected`` once the
        connection has dropped."""
        try:
            msg = self._messages.get(timeout=timeout)
        except queue.Empty:
            raise TimeoutError(f"nothing from {self._to} within {timeout} s") from None
        if msg is _GONE:
            self._messages.put(_GONE)  # every later get says so too
            raise Disconnected(f"the connection to {self._client.url} dropped")
        return msg

    def __iter__(self) -> "Subscription":
        return self

    def __next__(self) -> object:
        return self.get()

    def close(self) -> None:
        """Stop hearing. Safe to call twice, and after a disconnection."""
        if self._closed:
            return
        self._closed = True
        try:
            self._client._send(self._to, Unsubscribe(ActorRef(None, self._local)), None, Priority.NORMAL)
        except Disconnected:
            pass
        finally:
            with self._client._lock:
                self._client._waiting.pop(self._local, None)

    def __enter__(self) -> "Subscription":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


class _Through:
    """The transport behind a ref the client hands out: a tell goes over
    the connection."""

    def __init__(self, client: Client, address: str) -> None:
        self._client = client
        self._address = address

    def deliver(self, envelope: Envelope, priority: Priority) -> None:
        self._client._send(self._address, envelope.msg, None, priority)


def _address(to: ActorRef | str) -> str:
    return to.address if isinstance(to, ActorRef) else to
