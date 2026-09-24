"""A simulated hardware-timed camera: triggers in, frames out.

It is in both the timing flow and the control flow, which makes it the device
that proves the two meet correctly. Triggers reach it on the simulated wire,
never through a message, because that is how a real camera works: the DAQ pulls
its trigger line and the driver DMAs a frame in without asking any actor.

Its frames come from a simulated frame grabber, which draws a bar moving
across the field, so that is what a viewer shows. The free-running camera
draws the same bar with a test image generator of its own.
"""

from stacscope.adapters.simulated.frame_capture import SimulatedFrameCapture
from stacscope.adapters.simulated.image_generator import TestImageGenerator
from stacscope.core.buffer import Layout
from stacscope.core.devices import ImageStreaming, SoftwareTimed, TimedDevice
from stacscope.core.protocol import Phase


class SimulatedCamera(ImageStreaming, TimedDevice):
    """Produces one frame per rising edge on its trigger line, while armed.

    Frames are captured by a ``SimulatedFrameCapture``, available as
    ``capture`` once the device has started; everything else, from the
    buffer's layout to publishing each frame, is the ``ImageStreaming``
    role's. ``shape``, ``dtype``, ``row_bytes``, ``frame_offset``,
    ``slot_alignment`` and ``in_flight`` set the frame format, as they do on
    the capture.
    """

    def __init__(
        self,
        buffer_name: str,
        *,
        bus=None,
        trigger: str = "camera_trigger",
        shape: tuple[int, int] = (64, 64),
        dtype: str = "<u2",
        row_bytes: int | None = None,
        frame_offset: int = 0,
        slot_alignment: int = 64,
        in_flight: int = 1,
        n_slots: int = 16,
        capacity_bytes: int = 1 << 24,
        max_slots: int = 64,
    ) -> None:
        super().__init__()
        self.buffer_name = buffer_name
        self.capacity_bytes = capacity_bytes
        self.max_slots = max_slots
        self.n_slots = n_slots
        self.bus = bus
        self.trigger = trigger
        self._format = dict(
            shape=shape,
            dtype=dtype,
            row_bytes=row_bytes,
            frame_offset=frame_offset,
            slot_alignment=slot_alignment,
            in_flight=in_flight,
        )

    async def setup(self) -> None:
        # The capture runs on the actor's clock, which exists only from here.
        self.capture = SimulatedFrameCapture(
            self.clock, self.bus, trigger=self.trigger, **self._format
        )
        await super().setup()


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
