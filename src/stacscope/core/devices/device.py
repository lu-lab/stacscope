"""TimedDevice: the participant side of the device protocol.

The base class owns the states and every protocol reply, so an adapter writes
only what preparing and releasing *its* hardware means.

A participant has no phases of its own: once armed, frames arrive when the
hardware says so, and the device learns the acquisition is over from
``StopDevice``. Devices that want to act on the acquisition's progress
subscribe to their parent, the Coordinator, which relays the source's
``SourcePhase``: control flow, never to be trusted for timing. Faults go to
the parent too, reported by address.
"""

from collections.abc import Awaitable, Callable, Sequence
from enum import Enum

from stacscope.core.actors import Actor, Fault, Rejected, describe
from stacscope.core.protocol import (
    DeviceReady,
    DeviceStopped,
    GetProperties,
    GetProperty,
    Properties,
    PropertiesChanged,
    PropertiesRefused,
    Property,
    PropertyAccess,
    PropertyKind,
    SetProperties,
    SetProperty,
    StartDevice,
    StopDevice,
)

type Value = bool | float | str


class DeviceState(Enum):
    IDLE = "idle"
    ARMING = "arming"
    """Armed but for a part a role holds open until one of its children
    reports -- a recorder saying it has started. Not yet answered."""
    ARMED = "armed"
    """Prepared, and from here until ``StopDevice`` the acquisition may be
    under way: a hardware-timed device cannot tell the difference, because
    nothing tells it when the first trigger arrives."""
    DISARMING = "disarming"
    """Disarmed but for a part a role holds open until one of its children
    reports -- a recorder saying the file holds every frame. Not yet
    answered; the next arm is refused until it is."""


class NotReady(Exception):
    """Raised by ``arm`` to refuse: the device cannot be armed right now, but
    nothing is wrong with it. Answered ``Rejected``, and it stays armable."""


class PropertyRefused(Exception):
    """A value a device's checks or its adapter would not take. ``name`` is
    the property, where the one raising it knows: an adapter taking a batch
    whole raises it to say which property it refused."""

    def __init__(self, reason: str, *, name: str = "") -> None:
        super().__init__(reason)
        self.name = name


class TimedDevice(Actor):
    """IDLE ↔ ARMED, driven by ``StartDevice`` and ``StopDevice``.

    - ``StartDevice``: ``arm()``, then reply ``DeviceReady``.
    - ``StopDevice``: safe in any state, and always confirmed. Idle is a
      no-op; armed calls ``disarm()``, which is where a device reaches its own
      safe state.
    - A role whose part ends in a child's report calls ``hold()`` from
      ``arm`` or ``disarm``: the device is then ARMING or DISARMING, the
      answer waits, and the role ends it with ``complete_arm``, ``fail_arm``
      or ``complete_disarm``. A ``StopDevice`` while arming refuses the arm
      and disarms.
    - Death, however it comes, still disarms and closes: hardware is never
      left running because an actor died.

    Failing to arm and failing to disarm are not the same failure.
    ``NotReady`` is a refusal, answered and forgotten. Any other exception from
    ``arm`` is a hardware fault: the device is answered ``Rejected`` and
    reports a fatal ``Fault`` so the acquisition does not proceed, but it
    stays alive and idle, because nothing about it is unknown and a relaunch
    would help nobody. A ``disarm`` that fails is the opposite — the hardware's
    state is now unknown, so the device dies rather than confirm a stop it did
    not achieve, and the Coordinator reports the stop unconfirmed.

    Every device also answers ``GetProperties``, ``GetProperty`` and
    ``SetProperty`` from the table its adapter declares (``read_properties``
    and ``write_property``). A set is checked against the table before the
    adapter sees it: the name, read-only, the kind, the label, the limits or
    choices, and an IDLE property while not idle. It is answered, and
    published, as ``PropertiesChanged``: every property whose value or limits
    moved, found by reading the table before and after. A refusal, from the
    checks or from the adapter, is answered ``Rejected``, and the device
    carries on. ``launch_settings``, ``(name, value)`` pairs, are applied in
    order through the same checks during ``setup``, before an image buffer is
    created; a refused one fails the launch.

    ``SetProperties`` sets several, in the table's order whatever the map's.
    Everything but the limits is checked for the whole map first, and a
    failure refuses it all. Then each is written and the table read again,
    and the next checked against it; the first refused ends the batch, with
    nothing put back, answered ``PropertiesRefused``. An adapter whose
    hardware takes several settings in one call defines
    ``write_properties`` and is handed the batch whole instead; its driver
    then judges the limits.

    A role whose arm fails partway disarms what had armed below it. If that
    disarm fails too, the arm is still answered ``Rejected`` with a fatal
    ``Fault``, and then the device dies of the arm's error, as for any failed
    disarm.
    """

    launch_settings: tuple[tuple[str, Value], ...] = ()

    write_properties: Callable[[list[tuple[str, Value]]], Awaitable[None]] | None = None
    """Defined by an adapter that takes a batch of values whole, as
    ``async def write_properties(self, values)``: ``values`` is a list of
    ``(name, value)`` in the table's order, each already checked as
    ``write_property``'s are, but for the limits. Raise ``PropertyRefused``
    with the ``name`` of the property refused; any other exception refuses
    the batch without naming one. Left ``None``, a batch is written one
    property at a time."""

    _arm_undo_failed: bool = False
    _holding: bool = False
    _arm_asker = None
    _stop_askers: tuple = ()

    def __init_subclass__(cls, **kwargs: object) -> None:
        """Refuses, with ``TypeError``, a device whose roles are listed out of
        the order ``SoftwareTimed, ImageStreaming, Recording``, or listed
        after its adapter's class. See ``stacscope.core.devices``."""
        super().__init_subclass__(**kwargs)
        # Imported here: the roles' modules import this one.
        from stacscope.core.devices.image_streaming import ImageStreaming
        from stacscope.core.devices.recording import Recording
        from stacscope.core.devices.software_timed import SoftwareTimed

        # Checked on the MRO, not on __bases__: the MRO is the order the calls
        # run in, and it also covers a role inherited through a device class
        # or through a subclass of a role. A wrong order is otherwise silent
        # until an acquisition runs (user, 2026-09-24).
        mro = cls.__mro__
        listed = [role for role in (SoftwareTimed, ImageStreaming, Recording) if role in mro]
        for earlier, later in zip(listed, listed[1:]):
            if mro.index(later) < mro.index(earlier):
                raise TypeError(
                    f"{cls.__name__} lists {later.__name__} before {earlier.__name__}; "
                    f"list {later.__name__} after {earlier.__name__}, in the order "
                    "SoftwareTimed, ImageStreaming, Recording, then the adapter"
                )
        # TimedDevice's own arm and disarm call nothing further, so a role
        # below it in the MRO would never run.
        for role in listed:
            if mro.index(role) > mro.index(TimedDevice):
                raise TypeError(
                    f"{cls.__name__} lists {role.__name__} after its adapter; "
                    f"list {role.__name__} before TimedDevice and the adapter's class"
                )

    def __init__(self) -> None:
        super().__init__()
        self.state = DeviceState.IDLE

    # --- hooks for implementations -----------------------------------------

    async def arm(self, root_filename: str, cycles: int) -> None:
        """Prepare for acquisition: allocate, configure, start recording if
        ``root_filename`` is not empty. Return only when the hardware is ready
        for its first trigger. Raise ``NotReady`` to refuse.

        ``cycles`` is what the acquisition asked for (negative means until
        stopped). Most devices ignore it.
        """

    async def disarm(self) -> None:
        """End the acquisition and reach this device's safe state — vent the
        pressure, close the shutter, stop the recorder. Called whether or not
        the acquisition actually ran, and once at most per ``arm``."""

    async def close(self) -> None:
        """Release the hardware for good. Called once, however the actor ends,
        after ``disarm`` if it was armed."""

    async def read_properties(self) -> Sequence[Property]:
        """The device's properties as they stand, every one, in the order
        their writes depend on each other (a format before the region it
        bounds). The names must be the same at every call for the device's
        lifetime; everything else may change. Default: none."""
        return ()

    async def write_property(self, name: str, value: Value) -> None:
        """Apply ``value`` to the property ``name``. Called only once the
        value has passed every check against the table as just read: a known
        name, not read-only, of the property's kind (a NUMBER's value is
        always a float), a listed label, within the limits or among the
        choices, and an IDLE property only while idle. Raise to refuse, and
        leave the device as it was; the exception's message is the reason
        given. Applying a value the hardware rounds is not a refusal."""
        raise PropertyRefused(f"{type(self).__name__} cannot set {name}")

    async def _undo_arm(self, cause: BaseException, disarm: Callable[[], Awaitable[None]]) -> None:
        """For a role whose arm failed with ``cause`` after what is listed
        below it had armed: calls ``disarm``, the role's ``super().disarm``.
        If that fails too, its error is added to ``cause`` as a note, and the
        device dies of ``cause`` once the arm has been answered. The caller
        then re-raises ``cause``."""
        try:
            await disarm()
        except Exception as later:
            cause.add_note(f"disarm also failed: {later!r}")
            # Failed in both directions: nothing is known about the hardware.
            # Kept alive it would be idle, so the Coordinator's StopDevice would
            # do nothing and the next arm would land on it as it is (user,
            # 2026-09-24).
            self._arm_undo_failed = True

    # --- the protocol ------------------------------------------------------

    def hold(self) -> None:
        """Called by a role from ``arm`` or ``disarm`` when its part ends in
        one of its children's reports: the device's answer waits, ARMING or
        DISARMING, until the role calls ``complete_arm``, ``fail_arm`` or
        ``complete_disarm``."""
        # Held, not awaited: an actor waits on its children only as a state
        # (the parent/child rule). The handler returns, and the device goes on
        # answering queries and refusing changes meanwhile.
        self._holding = True

    def complete_arm(self) -> None:
        """The held part of the arm is done: ARMED, and ``DeviceReady``."""
        if self.state is DeviceState.ARMING:
            self.state = DeviceState.ARMED
            self._answer_arm(DeviceReady())

    async def fail_arm(self, reason: str) -> None:
        """The held part of the arm failed: disarm everything, answer
        ``Rejected`` and report a fatal ``Fault``, as for an arm that raised.
        If that disarm fails too, the device dies of it once answered."""
        if self.state is not DeviceState.ARMING:
            return
        reason = f"{type(self).__name__} failed to arm: {reason}"
        failed = None
        try:
            await self._disarm()
        except Exception as exc:
            failed = exc
            reason += f"; disarm also failed: {exc!r}"
        self._holding = False
        self.state = DeviceState.IDLE
        self._answer_arm(Rejected(reason))
        self.report(Fault(self.ref, reason, fatal=True))
        if failed is not None:
            raise failed  # failed in both directions: nothing is known about the hardware

    def complete_disarm(self, problem: str | None = None) -> None:
        """The held part of the disarm is done: IDLE, and ``DeviceStopped``.
        A ``problem`` -- a file short of its frames -- is reported as a fatal
        ``Fault`` first; the stop is still confirmed, because the hardware
        did reach its safe state."""
        if self.state is not DeviceState.DISARMING:
            return
        if problem is not None:
            self.report(Fault(self.ref, problem, fatal=True))
        self.state = DeviceState.IDLE
        askers, self._stop_askers = self._stop_askers, ()
        for asker in askers:
            asker.tell(DeviceStopped())

    def _answer_arm(self, answer: object) -> None:
        asker, self._arm_asker = self._arm_asker, None
        if asker is not None:
            asker.tell(answer)

    async def handle(self, msg: object) -> None:
        match msg:
            case StartDevice(root_filename, cycles):
                await self._start(msg, root_filename, cycles)
            case StopDevice():
                await self._stop()
            case GetProperties():
                await self._get_properties()
            case GetProperty(name):
                await self._get_property(name)
            case SetProperty(name, value):
                await self._set_property(name, value)
            case SetProperties(values):
                await self._set_properties(values)
            case _:
                # Ignored, told or asked (user, 2026-09-30), except a Fault,
                # which the base passes up. An asker that sent the wrong thing
                # learns it from its own timeout.
                await super().handle(msg)

    async def _start(self, msg: object, root_filename: str, cycles: int) -> None:
        if self.state is not DeviceState.IDLE:
            self._reject(f"{type(msg).__name__} is not valid while {self.state.name}")
            return
        self._holding = False
        try:
            await self.arm(root_filename, cycles)
        except NotReady as exc:
            self._reject(str(exc))
            return
        except Exception as exc:
            # describe(), not repr: a failed undo is a note on the arm's error.
            reason = f"{type(self).__name__} failed to arm: {describe(exc)}"
            self._reject(reason, fatal=True)
            if self._arm_undo_failed:
                raise  # answered first, so the Coordinator need not time out
            return
        if self._holding:
            self._holding = False
            self.state = DeviceState.ARMING
            self._arm_asker = self.sender
            return
        self.state = DeviceState.ARMED
        self.reply(DeviceReady())

    async def _stop(self) -> None:
        asker = () if self.sender is None else (self.sender,)
        if self.state is DeviceState.IDLE:
            self.reply(DeviceStopped())
            return
        if self.state is DeviceState.DISARMING:
            self._stop_askers += asker
            return
        if self.state is DeviceState.ARMING:
            self._answer_arm(Rejected("stopped before it was ready"))
        self._holding = False
        await self._disarm()
        if self._holding:
            self._holding = False
            self.state = DeviceState.DISARMING
            self._stop_askers = asker
            return
        self.reply(DeviceStopped())

    async def _disarm(self) -> None:
        self.state = DeviceState.IDLE  # never disarmed twice, even if it raises
        await self.disarm()

    async def setup(self) -> None:
        await super().setup()
        # Here, after the adapter has opened its hardware and before a role
        # creates its buffer: a device whose settings are refused never makes
        # a segment for a consumer to find.
        for name, value in self.launch_settings:
            try:
                await self._apply(await self._table(), name, value)
            except PropertyRefused as exc:
                raise PropertyRefused(f"launch setting {name} = {value!r} refused: {exc}") from None

    # --- properties --------------------------------------------------------

    async def _table(self) -> dict[str, Property]:
        return {p.name: p for p in await self.read_properties()}

    async def _get_properties(self) -> None:
        try:
            table = await self._table()
        except Exception as exc:
            self._reject(f"could not read the properties: {describe(exc)}")
            return
        self.reply(Properties(table))

    async def _get_property(self, name: str) -> None:
        try:
            table = await self._table()
        except Exception as exc:
            self._reject(f"could not read {name}: {describe(exc)}")
            return
        if name not in table:
            self._reject(f"{type(self).__name__} has no property {name!r}")
            return
        self.reply(table[name])

    async def _set_property(self, name: str, value: Value) -> None:
        try:
            before = await self._table()
            await self._apply(before, name, value)
            after = await self._table()
        except PropertyRefused as exc:
            self._reject(str(exc))
            return
        except Exception as exc:
            self._reject(f"could not read the properties: {describe(exc)}")
            return
        changed = PropertiesChanged(
            {n: p for n, p in after.items() if n == name or before.get(n) != p}
        )
        self.reply(changed)
        self.publish(changed)

    async def _set_properties(self, values: dict[str, Value]) -> None:
        try:
            before = await self._table()
        except Exception as exc:
            self._reject(f"could not read the properties: {describe(exc)}")
            return
        try:
            for name in values:
                self._admitted(before, name, values[name])
        except PropertyRefused as exc:
            self._reject(str(exc))
            return
        # The table's order is the adapter's statement of which writes depend
        # on which; a client's map order is not even kept by every language.
        order = list(before)
        batch = sorted(values.items(), key=lambda item: order.index(item[0]))
        refused: PropertyRefused | None = None
        try:
            if self.write_properties is not None:
                try:
                    await self.write_properties([(n, self._admitted(before, n, v)) for n, v in batch])
                except PropertyRefused as exc:
                    refused = exc
                except Exception as exc:
                    refused = PropertyRefused(describe(exc))
                table = await self._table()
            else:
                # Read again after every write: each is checked against limits
                # as the writes before it left them (user, 2026-10-06).
                table = before
                for name, value in batch:
                    try:
                        await self._apply(table, name, value)
                    except PropertyRefused as exc:
                        refused = PropertyRefused(str(exc), name=name)
                        break
                    table = await self._table()
        except Exception as exc:
            self._reject(f"could not read the properties: {describe(exc)}")
            return
        moved = {n: p for n, p in table.items() if before.get(n) != p}
        if refused is None:
            changed = PropertiesChanged({n: p for n, p in table.items() if n in values or n in moved})
            self.reply(changed)
        else:
            changed = PropertiesChanged(moved)
            if self.sender is None:
                self.report(Fault(self.ref, str(refused), fatal=False))
            self.reply(PropertiesRefused(refused.name, str(refused), moved))
        if changed.properties:
            self.publish(changed)

    async def _apply(self, table: dict[str, Property], name: str, value: Value) -> None:
        """Check ``value`` against ``table``, then write it. Raises
        ``PropertyRefused`` for either refusal."""
        value = self._checked(table, name, value)
        try:
            await self.write_property(name, value)
        except PropertyRefused:
            raise
        except Exception as exc:
            # A refusal, not a fault (decision 5): the device lives on. An
            # adapter that knows a failure is worse reports its own Fault.
            raise PropertyRefused(f"{name}: {describe(exc)}") from None

    def _checked(self, table: dict[str, Property], name: str, value: Value) -> Value:
        """``value`` as the adapter is handed it, or ``PropertyRefused``."""
        value = self._admitted(table, name, value)
        prop = table[name]
        if prop.kind is PropertyKind.NUMBER:
            unit = f" {prop.unit}" if prop.unit else ""
            if prop.choices and value not in prop.choices:
                choices = ", ".join(f"{c:g}" for c in prop.choices)
                raise PropertyRefused(f"{name} must be one of {choices}{unit}, not {value:g}")
            # Written so that NaN, which compares false, is refused.
            if prop.minimum is not None and not value >= prop.minimum:
                raise PropertyRefused(f"{name} must be at least {prop.minimum:g}{unit}, not {value:g}")
            if prop.maximum is not None and not value <= prop.maximum:
                raise PropertyRefused(f"{name} must be at most {prop.maximum:g}{unit}, not {value:g}")
        return value

    def _admitted(self, table: dict[str, Property], name: str, value: Value) -> Value:
        """``value`` as the adapter is handed it, checked for everything but
        the limits, which a batch's earlier writes may move; or
        ``PropertyRefused``."""
        prop = table.get(name)
        if prop is None:
            raise PropertyRefused(f"{type(self).__name__} has no property {name!r}")
        if prop.access is PropertyAccess.READ_ONLY:
            raise PropertyRefused(f"{name} is read-only")
        match prop.kind:
            case PropertyKind.NUMBER:
                # bool first: Python calls True an int.
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise PropertyRefused(f"{name} takes a number, not {value!r}")
                value = float(value)
            case PropertyKind.LABEL:
                if not isinstance(value, str):
                    raise PropertyRefused(f"{name} takes a label, not {value!r}")
                if value not in prop.labels:
                    labels = ", ".join(prop.labels)
                    raise PropertyRefused(f"{name} has no label {value!r}; its labels are {labels}")
            case PropertyKind.FLAG:
                if not isinstance(value, bool):
                    raise PropertyRefused(f"{name} takes a flag, true or false, not {value!r}")
        if prop.access is PropertyAccess.IDLE and self.state is not DeviceState.IDLE:
            raise PropertyRefused(
                f"{name} can be changed only while idle, not while {self.state.name}"
            )
        return value

    async def teardown(self) -> None:
        try:
            if self.state in (DeviceState.ARMED, DeviceState.ARMING):
                self._answer_arm(Rejected("the device stopped"))
                await self._disarm()
        finally:
            await self.close()

    def _reject(self, reason: str, *, fatal: bool = False) -> None:
        """Answer the request, and report to the parent anything nobody is
        waiting on. A fatal fault is reported as well as answered: the
        requester learns the acquisition cannot start, and whoever supervises
        the device learns why."""
        if self.sender is not None:
            self.reply(Rejected(reason))
        if fatal or self.sender is None:
            self.report(Fault(self.ref, reason, fatal=fatal))
