"""TimedDevice: the participant side of the device protocol.

The base class owns the states and every protocol reply, so an adapter writes
only what preparing and releasing *its* hardware means.

A participant has no phases of its own: once armed, frames arrive when the
hardware says so, and the device learns the acquisition is over from
``StopDevice``. Devices that want to act on the acquisition's progress
subscribe to the Coordinator's republished ``SourcePhase`` instead, which is
control flow and never to be trusted for timing.
"""

from collections.abc import Awaitable, Callable
from enum import Enum

from stacscope.core.actors import Actor, Fault, Rejected, describe
from stacscope.core.protocol import (
    DeviceLaunched,
    DeviceReady,
    DeviceStopped,
    SourcePhase,
    StartDevice,
    StopDevice,
)


class DeviceState(Enum):
    IDLE = "idle"
    ARMED = "armed"
    """Prepared, and from here until ``StopDevice`` the acquisition may be
    under way: a hardware-timed device cannot tell the difference, because
    nothing tells it when the first trigger arrives."""


class NotReady(Exception):
    """Raised by ``arm`` to refuse: the device cannot be armed right now, but
    nothing is wrong with it. Answered ``Rejected``, and it stays armable."""


class TimedDevice(Actor):
    """IDLE ↔ ARMED, driven by ``StartDevice`` and ``StopDevice``.

    - ``StartDevice``: ``arm()``, then reply ``DeviceReady``.
    - ``StopDevice``: safe in either state, and always confirmed. Idle is a
      no-op; armed calls ``disarm()``, which is where a device reaches its own
      safe state.
    - Death, however it comes, still disarms and closes: hardware is never
      left running because an actor died.

    Failing to arm and failing to disarm are not the same failure.
    ``NotReady`` is a refusal, answered and forgotten. Any other exception from
    ``arm`` is a hardware fault: the device is answered ``Rejected`` and
    publishes a fatal ``Fault`` so the acquisition does not proceed, but it
    stays alive and idle, because nothing about it is unknown and a relaunch
    would help nobody. A ``disarm`` that fails is the opposite — the hardware's
    state is now unknown, so the device dies rather than confirm a stop it did
    not achieve, and the Coordinator reports the stop unconfirmed.

    A role whose arm fails partway disarms what had armed below it. If that
    disarm fails too, the arm is still answered ``Rejected`` with a fatal
    ``Fault``, and then the device dies of the arm's error, as for any failed
    disarm.
    """

    _arm_undo_failed: bool = False

    expected_frames: int = -1
    """How many pulses this device's trigger line will carry in the
    acquisition being armed, as the timing source counted them, or -1 if
    that is not known. Set from ``StartDevice`` before ``arm`` is called."""

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
        stopped). Most devices ignore it. A recorder bounding a file wants
        ``expected_frames``, already set when this is called.
        """

    async def disarm(self) -> None:
        """End the acquisition and reach this device's safe state — vent the
        pressure, close the shutter, stop the recorder. Called whether or not
        the acquisition actually ran, and once at most per ``arm``."""

    async def close(self) -> None:
        """Release the hardware for good. Called once, however the actor ends,
        after ``disarm`` if it was armed."""

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

    async def handle(self, msg: object) -> None:
        match msg:
            case StartDevice(root_filename, cycles, frames):
                await self._start(msg, root_filename, cycles, frames)
            case StopDevice():
                if self.state is DeviceState.ARMED:
                    await self._disarm()
                self.reply(DeviceStopped())
            case Fault() | DeviceLaunched() | SourcePhase():
                pass  # broadcasts: a device acts only on the ones it subscribed for
            case _:
                self._reject(f"{type(msg).__name__} is not understood by {type(self).__name__}")

    async def _start(self, msg: object, root_filename: str, cycles: int, frames: int) -> None:
        if self.state is not DeviceState.IDLE:
            self._reject(f"{type(msg).__name__} is not valid while {self.state.name}")
            return
        # An attribute, not an argument to arm: every role's arm would change
        # for what one kind of role (a recorder) uses.
        self.expected_frames = frames
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
        self.state = DeviceState.ARMED
        self.reply(DeviceReady())

    async def _disarm(self) -> None:
        self.state = DeviceState.IDLE  # never disarmed twice, even if it raises
        await self.disarm()

    async def teardown(self) -> None:
        try:
            if self.state is DeviceState.ARMED:
                await self._disarm()
        finally:
            await self.close()

    def _reject(self, reason: str, *, fatal: bool = False) -> None:
        """Answer the request, and publish anything nobody is waiting on. A
        fatal fault is published as well as answered: the requester learns the
        acquisition cannot start, and subscribers learn why."""
        if self.reply_to is not None:
            self.reply(Rejected(reason))
        if fatal or self.reply_to is None:
            self.publish(Fault(self.ref, reason, fatal=fatal))
