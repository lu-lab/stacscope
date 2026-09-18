"""A timing source whose phases do nothing but take time."""

from stacscope.core.timing import TimingSource


class SimulatedTimingSource(TimingSource):
    """Records every hook call; each cycle lasts ``cycle_period`` on the clock,
    and ``drain`` takes ``drain_time`` — as a real buffered output keeps playing
    what was already committed. ``arm`` takes ``arm_time``. ``fail_on_cycle``
    makes that cycle raise, killing the source.

    Deliberately assumes nothing about AO: it is what proves the Coordinator
    works with any source, including a camera.
    """

    def __init__(
        self,
        cycle_period: float = 1.0,
        drain_time: float = 0.0,
        arm_time: float = 0.0,
        fail_on_cycle: int | None = None,
    ) -> None:
        super().__init__()
        self.cycle_period = cycle_period
        self.drain_time = drain_time
        self.arm_time = arm_time
        self.fail_on_cycle = fail_on_cycle
        self.calls: list[object] = []
        self.cycles_started = 0
        self.cycles_completed = 0

    async def arm(self, root_filename: str, cycles: int) -> None:
        if self.arm_time:
            await self.clock.sleep(self.arm_time)
        self.calls.append(("arm", root_filename, cycles))

    async def disarm(self) -> None:
        self.calls.append("disarm")

    async def warmup(self) -> None:
        self.calls.append("warmup")

    async def run_cycle(self) -> None:
        self.cycles_started += 1
        if self.cycles_started == self.fail_on_cycle:
            raise RuntimeError(f"simulated source failure on cycle {self.cycles_started}")
        await self.clock.sleep(self.cycle_period)
        self.cycles_completed += 1
        self.calls.append("cycle")

    async def cooldown(self) -> None:
        self.calls.append("cooldown")

    async def drain(self) -> None:
        if self.drain_time:
            await self.clock.sleep(self.drain_time)
        self.calls.append("drain")
