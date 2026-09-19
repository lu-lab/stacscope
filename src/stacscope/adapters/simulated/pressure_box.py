"""A simulated software-timed device: the pressure box.

It drives an experimental apparatus over a serial link, at the scale of
seconds, against sub-second volume rates. It has no trigger line, so it is in
the control flow and not the timing flow: it hears the acquisition start
through the source's published phases and then follows its own clock.

Its routine is deliberately the shape a real one will be — a list of
(pressure, hold) steps — and its safe state is deliberately explicit, because
an experiment cut short must still vent.
"""

from stacscope.core.devices import SoftwareTimed, TimedDevice
from stacscope.core.protocol import Phase

SEQUENCE = ((5.0, 1.0), (10.0, 1.0), (15.0, 1.0))
"""(kPa, seconds to hold) — a plain ramp, as a placeholder for a real protocol."""


class SimulatedPressureBox(SoftwareTimed, TimedDevice):
    """Steps through a pressure sequence once per acquisition."""

    def __init__(
        self,
        sequence=SEQUENCE,
        *,
        start_phase: Phase = Phase.RUN,
        step: float = 1.0,
    ) -> None:
        super().__init__()
        self.sequence = sequence
        self.start_phase = start_phase
        self.step = step
        """Scales every hold, so a test can run the sequence inside a short
        acquisition without rewriting it."""
        self.pressure = 0.0
        self.log: list[tuple[float, float]] = []
        """(pressure, virtual time) for every step actually applied — the
        device's own record, to be reconciled with the frames afterwards."""

    async def routine(self) -> None:
        for pressure, hold in self.sequence:
            await self.clock.sleep(hold * self.step)
            self._set(pressure)

    async def disarm(self) -> None:
        await super().disarm()  # the routine is cancelled and unwound first
        self._set(0.0)  # vented: the safe state, however the acquisition ended

    def _set(self, pressure: float) -> None:
        self.pressure = pressure
        self.log.append((pressure, self.clock.now()))
