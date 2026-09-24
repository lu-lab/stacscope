"""A simulated hardware-timed camera: triggers in, frames out.

It is in both the timing flow and the control flow, which makes it the device
that proves the two meet correctly. Triggers reach it on the simulated wire,
never through a message, because that is how a real camera works: the DAQ pulls
its trigger line and the driver DMAs a frame in without asking any actor.

The images themselves come from ``TestImageGenerator``, so what a viewer shows is
a bar moving across the field.
"""

from stacscope.adapters.simulated.image_generator import TestImageGenerator
from stacscope.core.buffer import Layout
from stacscope.core.devices import DeviceState, ImageStreaming, SoftwareTimed, TimedDevice
from stacscope.core.protocol import Phase


class SimulatedCamera(ImageStreaming, TimedDevice):
    """Produces one frame per rising edge on its trigger line, while armed.

    Everything outside ``arm`` and the trigger callback is inherited: the
    buffer's lifetime, the layout, and the protocol replies are not this
    device's business.
    """

    def __init__(
        self,
        buffer_name: str,
        *,
        bus=None,
        trigger: str = "camera_trigger",
        shape: tuple[int, ...] = (64, 64),
        dtype: str = "<u2",
        n_slots: int = 16,
        capacity_bytes: int = 1 << 24,
        max_slots: int = 64,
    ) -> None:
        super().__init__()
        self.buffer_name = buffer_name
        self.capacity_bytes = capacity_bytes
        self.max_slots = max_slots
        self.bus = bus
        self.trigger = trigger
        self.shape = shape
        self.dtype = dtype
        self.n_slots = n_slots
        self.generator: TestImageGenerator | None = None
        self.missed = 0
        """Triggers that arrived when the camera was not armed to take them."""

    def layout(self) -> Layout:
        return Layout(shape=self.shape, dtype=self.dtype, n_slots=self.n_slots)

    def metadata(self):
        return self.generator.metadata

    async def setup(self) -> None:
        await super().setup()
        self.generator = TestImageGenerator(self.writer)
        if self.bus is not None:
            self.bus.subscribe(self.trigger, self._triggered)

    async def arm(self, root_filename: str, cycles: int) -> None:
        await super().arm(root_filename, cycles)  # buffer configured, acquisition started
        self.generator.reset()

    async def close(self) -> None:
        if self.bus is not None:
            self.bus.unsubscribe(self.trigger, self._triggered)
        await super().close()

    def _triggered(self) -> None:
        """A trigger pulse arrived. Outside the mailbox, like a DMA callback."""
        if self.state is DeviceState.ARMED:
            self.generator.emit(1)
        else:
            self.missed += 1


class SimulatedFreeRunningCamera(SoftwareTimed, ImageStreaming, TimedDevice):
    """A camera with no trigger line, free-running once the acquisition reaches
    the phase it chose.

    Not how the instrument works -- the DAQ's pulse reaches a camera's hardware
    whichever process owns the driver -- but a trigger is a wire, and a wire
    does not cross a process boundary. What this stands in for is the part that
    does: control over the link, frames through shared memory. A frame count
    here is therefore timed by the camera and not caused by the source, which
    is the one thing an out-of-process camera cannot demonstrate in simulation.
    """

    def __init__(
        self,
        buffer_name: str,
        *,
        interval: float = 0.005,
        start_phase: Phase = Phase.RUN,
        shape: tuple[int, ...] = (64, 64),
        dtype: str = "<u2",
        n_slots: int = 16,
        capacity_bytes: int = 1 << 24,
        max_slots: int = 64,
    ) -> None:
        super().__init__()
        self.buffer_name = buffer_name
        self.capacity_bytes = capacity_bytes
        self.max_slots = max_slots
        self.interval = interval
        self.start_phase = start_phase
        self.shape = shape
        self.dtype = dtype
        self.n_slots = n_slots
        self.generator: TestImageGenerator | None = None

    def layout(self) -> Layout:
        return Layout(shape=self.shape, dtype=self.dtype, n_slots=self.n_slots)

    def metadata(self):
        return self.generator.metadata

    async def setup(self) -> None:
        await super().setup()
        self.generator = TestImageGenerator(self.writer)

    async def arm(self, root_filename: str, cycles: int) -> None:
        await super().arm(root_filename, cycles)  # buffer configured, acquisition started
        self.generator.reset()

    async def routine(self) -> None:
        while True:
            await self.clock.sleep(self.interval)
            self.generator.emit(1)
