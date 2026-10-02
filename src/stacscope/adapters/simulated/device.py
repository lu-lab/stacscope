"""A participant that follows the device protocol and nothing else."""

from stacscope.core.actors import Actor
from stacscope.core.protocol import DeviceReady, DeviceStopped, StartDevice, StopDevice


class SimulatedDevice(Actor):
    """Answers ``StartDevice`` after ``ready_delay`` (or never, if
    ``never_ready``) and ``StopDevice`` at once. Records every command with
    the virtual time it arrived. ``fail_in_setup`` simulates a device that
    cannot be initialized, and ``setup_time`` one slow to initialize."""

    def __init__(
        self,
        ready_delay: float = 0.0,
        never_ready: bool = False,
        fail_in_setup: bool = False,
        setup_time: float = 0.0,
    ) -> None:
        super().__init__()
        self.ready_delay = ready_delay
        self.never_ready = never_ready
        self.fail_in_setup = fail_in_setup
        self.setup_time = setup_time
        self.calls: list[tuple] = []

    def names(self) -> list[str]:
        return [call[0] for call in self.calls]

    async def setup(self) -> None:
        if self.setup_time:
            await self.clock.sleep(self.setup_time)
        if self.fail_in_setup:
            raise RuntimeError("simulated device failed to initialize")

    async def handle(self, msg: object) -> None:
        match msg:
            case StartDevice(root_filename, _):
                self.calls.append(("start", root_filename, self.clock.now()))
                if self.never_ready:
                    return
                if self.ready_delay:
                    await self.clock.sleep(self.ready_delay)
                self.reply(DeviceReady())
            case StopDevice():
                self.calls.append(("stop", self.clock.now()))
                self.reply(DeviceStopped())
