"""TimedDevice: the participant side of the device protocol.

The Coordinator's half was built in Milestone 1; this is the other end of the
same handshake. The base class owns the states and every protocol reply, so an
adapter writes only what preparing and releasing *its* hardware means.

Deliberately smaller than ``TimingSource``. A participant has no phases of its
own: once armed, frames arrive when the hardware says so, and the device learns
the acquisition is over from ``StopDevice``. Devices that want to act on the
acquisition's progress subscribe to the Coordinator's republished
``SourcePhase`` instead, which is control flow and never to be trusted for
timing.
"""

from enum import Enum

from stacscope.core.actors import Actor, Fault, Rejected
from stacscope.core.protocol import DeviceReady, DeviceStopped, StartDevice, StopDevice


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
    """

    def __init__(self) -> None:
        super().__init__()
        self.state = DeviceState.IDLE

    # --- hooks for implementations -----------------------------------------

    async def arm(self, root_filename: str, cycles: int) -> None:
        """Prepare for acquisition: allocate, configure, start recording if
        ``root_filename`` is not empty. Return only when the hardware is ready
        for its first trigger. Raise ``NotReady`` to refuse.

        ``cycles`` is what the acquisition asked for (negative means until
        stopped). Most devices ignore it; a recorder may use it to bound a file.
        """

    async def disarm(self) -> None:
        """End the acquisition and reach this device's safe state — vent the
        pressure, close the shutter, stop the recorder. Called whether or not
        the acquisition actually ran, and once at most per ``arm``."""

    async def close(self) -> None:
        """Release the hardware for good. Called once, however the actor ends,
        after ``disarm`` if it was armed."""

    # --- the protocol ------------------------------------------------------

    async def handle(self, msg: object) -> None:
        match msg:
            case StartDevice(root_filename, cycles):
                await self._start(msg, root_filename, cycles)
            case StopDevice():
                if self.state is DeviceState.ARMED:
                    await self._disarm()
                self.reply(DeviceStopped())
            case _:
                self._reject(f"{type(msg).__name__} is not understood by {type(self).__name__}")

    async def _start(self, msg: object, root_filename: str, cycles: int) -> None:
        if self.state is not DeviceState.IDLE:
            self._reject(f"{type(msg).__name__} is not valid while {self.state.name}")
            return
        try:
            await self.arm(root_filename, cycles)
        except NotReady as exc:
            self._reject(str(exc))
            return
        except Exception as exc:
            reason = f"{type(self).__name__} failed to arm: {exc!r}"
            self._reject(reason, fatal=True)
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
