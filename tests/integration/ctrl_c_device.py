"""A simulated camera that writes down how it was stopped, for the Ctrl-C tests.

It runs in a process of its own, where a test cannot watch it, so it leaves
a log: one line as its setup starts and ends, one as it disarms, and one as
it closes, saying whether it is closing because its task was cancelled -- a
Ctrl-C reaching it directly -- or because the engine stopped it in order.
"""

import asyncio
from pathlib import Path

from stacscope.adapters.simulated import SimulatedFreeRunningCamera


class LoggingCamera(SimulatedFreeRunningCamera):
    def __init__(
        self, buffer_name: str, *, log: str, setup_delay: float = 0.0, disarm_delay: float = 0.0, **options
    ) -> None:
        super().__init__(buffer_name, **options)
        self.log = log
        self.setup_delay = setup_delay
        self.disarm_delay = disarm_delay

    def note(self, line: str) -> None:
        with open(self.log, "a", encoding="utf-8") as f:
            f.write(line + "\n")

    async def setup(self) -> None:
        self.note("setup")
        await asyncio.sleep(self.setup_delay)  # a camera slow to open
        await super().setup()
        self.note("ready")

    async def disarm(self) -> None:
        self.note("disarm")
        await asyncio.sleep(self.disarm_delay)  # a camera slow to stop
        await super().disarm()

    async def close(self) -> None:
        task = asyncio.current_task()
        self.note(f"close cancelled={task is not None and task.cancelling() > 0}")
        await super().close()


def read_log(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines() if path.exists() else []
