"""SoftwareTimed: a participant that acts during the acquisition, on the clock.

The pressure box is the case this exists for. It takes serial commands, drives
an experimental apparatus, and works at the scale of seconds against sub-second
volume rates. It has no trigger line and never will, so it **can never be part
of the acquisition loop** — but it is part of the imaging system, and what it
does needs to line up with the acquisition.

It lines up with the acquisition's *edges*, and nothing finer. The timing
source reports its phases to the Coordinator, which relays them to every
device that subscribed -- a software-timed device subscribes as it starts --
and the device starts its routine on the phase it chose: a shutter at WARMUP, so it is already
moving; a driven variable at RUN, with the first image.

**That edge is control flow.** It crosses a mailbox, and possibly a process, so
it is good to milliseconds, never microseconds. Nothing whose timing matters to
the data may depend on it. A device whose own timing is experimentally
important keeps its own record, to be reconciled afterwards, the same way a
camera is responsible for its own recording.
"""

import asyncio

from stacscope.core.actors import Fault, Subscribe
from stacscope.core.devices.device import DeviceState
from stacscope.core.protocol import Phase, SourcePhase


class SoftwareTimed:
    """Mixin for a ``TimedDevice`` that runs a routine. List it first among
    the device's bases, before any other role:
    ``class PressureBox(SoftwareTimed, TimedDevice)``. Its ``disarm`` cancels
    the routine before everything listed after it disarms, so listed first,
    the routine stops before anything else does. See
    ``stacscope.core.devices`` for all the roles together.

    The routine is an ordinary coroutine on ``self.clock``, which is what lets
    it be written the way the experiment reads — ramp, hold, step — rather than
    as a state machine. It runs as its own task, so a routine that sleeps for a
    minute never blocks the device's mailbox; the cost is that it interleaves
    with ``handle`` at every await, so a routine and a command handler must not
    both write the same attribute.

    A routine is cancelled when the device stops, before the adapter's
    ``disarm`` reaches its safe state: an interrupted experiment vents the
    pressure and closes the shutter like any other. A routine that raises is
    reported as a fatal ``Fault`` when it does; the device stays armed until
    it is stopped.
    """

    start_phase: Phase = Phase.RUN
    """Which relayed phase starts the routine."""

    _routine: asyncio.Task | None = None

    async def routine(self) -> None:
        """This device's own sequence, on ``self.clock``. Runs once per
        acquisition, may finish early, and is cancelled if the acquisition
        stops first."""

    async def setup(self) -> None:
        # The phases are its parent's to relay: it asks, by its parent's
        # address, rather than being subscribed by it.
        if self.parent is not None:
            self.parent.tell(Subscribe(self.ref))
        await super().setup()

    async def handle(self, msg: object) -> None:
        match msg:
            case SourcePhase(phase):
                if phase is self.start_phase and self.state is DeviceState.ARMED:
                    self._start_routine()
            case _:
                await super().handle(msg)

    def _start_routine(self) -> None:
        if self._routine is None:
            self._routine = asyncio.create_task(self.routine())
            self._routine.add_done_callback(self._routine_ended)

    def _routine_ended(self, routine: asyncio.Task) -> None:
        # Reported here, as it happens: otherwise the exception waits in the
        # task until disarm gathers it, which drops it, and the acquisition
        # runs on without the experiment the routine was driving. A
        # cancellation is how every stop mid-routine ends, so it is not one.
        if routine.cancelled() or routine.exception() is None:
            return
        reason = f"{type(self).__name__} routine failed: {routine.exception()!r}"
        self.report(Fault(self.ref, reason, fatal=True))

    async def disarm(self) -> None:
        routine, self._routine = self._routine, None
        if routine is not None:
            routine.cancel()
            await asyncio.gather(routine, return_exceptions=True)
        await super().disarm()
