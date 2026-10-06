"""A simulated hardware-timed camera: triggers in, frames out.

It is in both the timing flow and the control flow, which makes it the device
that proves the two meet correctly. Triggers reach it on the simulated wire,
never through a message, because that is how a real camera works: the DAQ pulls
its trigger line and the driver DMAs a frame in without asking any actor.

Its frames come from a simulated frame grabber, which draws a bar moving
across the field, so that is what a viewer shows. The free-running camera
draws the same bar with a test image generator of its own.

The triggered camera records as the uEye does: an acquisition with a stem is
written to <save_directory>/<stem>_<name>.tif by a TIFF recorder reading its
buffer.

The triggered camera has properties, as a real camera does, read with
GetProperties and changed with SetProperty:

    pixel_format        MONO8 or MONO16; only while idle
    aoi_x, aoi_width    the columns read out, in pixels; only while idle.
                        aoi_x + aoi_width never passes the sensor's edge,
                        so moving one may need the other moved first
    exposure            seconds; stored, and changes nothing in the image
    test_pattern        on or off; stored, likewise
    sensor_temperature  degrees Celsius; read only
"""

import os
from collections.abc import Iterable
from pathlib import Path

import numpy as np

from stacscope.adapters.simulated.frame_capture import SimulatedFrameCapture
from stacscope.adapters.simulated.image_generator import TestImageGenerator
from stacscope.core.buffer import Layout
from stacscope.core.devices import BufferRecording, ImageStreaming, SoftwareTimed, TimedDevice
from stacscope.core.protocol import Phase, Property, PropertyAccess, PropertyKind

_PIXEL_FORMATS = {"MONO8": "|u1", "MONO16": "<u2"}
AOI_STEP = 4
"""Pixels: the simulated camera's AOI moves and sizes in steps of this."""


class SimulatedCamera(ImageStreaming, BufferRecording, TimedDevice):
    """Produces one frame per rising edge on its trigger line, while armed.

    Frames are captured by a ``SimulatedFrameCapture``, available as
    ``capture`` once the device has started; everything else, from the
    buffer's layout to publishing each frame, is the ``ImageStreaming``
    role's. ``shape``, ``dtype``, ``row_bytes``, ``frame_offset``,
    ``slot_alignment`` and ``in_flight`` set the frame format, as they do on
    the capture.

    An acquisition with a stem is recorded to TIFF in ``save_directory``,
    an existing directory, checked at launch, or one set by
    ``SetSaveDirectory``; without one, only acquisitions that are not
    recorded can start.

    ``shape`` is also the sensor: its properties (see the module) narrow the
    columns read out within it. ``settings``, ``(name, value)`` pairs, are
    applied in order at launch, and one refused fails the launch.
    """

    def __init__(
        self,
        buffer_name: str,
        *,
        settings: Iterable[tuple[str, bool | float | str]] = (),
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
        save_directory: str | os.PathLike | None = None,
    ) -> None:
        super().__init__()
        self.buffer_name = buffer_name
        self.capacity_bytes = capacity_bytes
        self.max_slots = max_slots
        self.n_slots = n_slots
        self.bus = bus
        self.trigger = trigger
        self.save_directory = None if save_directory is None else Path(save_directory)
        self.launch_settings = tuple(settings)
        self._format = dict(
            shape=shape,
            dtype=dtype,
            row_bytes=row_bytes,
            frame_offset=frame_offset,
            slot_alignment=slot_alignment,
            in_flight=in_flight,
        )
        self._sensor_width = shape[1]
        self._values = {"aoi_x": 0.0, "exposure": 0.01, "test_pattern": False}

    async def setup(self) -> None:
        self.check_save_directory()
        # The capture runs on the actor's clock, which exists only from here.
        self.capture = SimulatedFrameCapture(
            self.clock, self.bus, trigger=self.trigger, **self._format
        )
        await super().setup()

    async def read_properties(self) -> list[Property]:
        fmt = self.capture.frame_format()
        x, width = self._values["aoi_x"], float(fmt.shape[1])
        dtype = np.dtype(fmt.dtype)
        pixel_format = next(
            (label for label, d in _PIXEL_FORMATS.items() if np.dtype(d) == dtype), fmt.dtype
        )
        number, idle = PropertyKind.NUMBER, PropertyAccess.IDLE
        return [
            Property(
                "pixel_format", PropertyKind.LABEL, pixel_format,
                labels=tuple(_PIXEL_FORMATS), access=idle,
            ),
            Property(
                "aoi_x", number, x, "px",
                0.0, self._sensor_width - width, AOI_STEP, access=idle,
            ),
            Property(
                "aoi_width", number, width, "px",
                AOI_STEP, self._sensor_width - x, AOI_STEP, access=idle,
            ),
            Property("exposure", number, self._values["exposure"], "s", 1e-5, 10.0, 1e-5),
            Property("test_pattern", PropertyKind.FLAG, self._values["test_pattern"]),
            Property(
                "sensor_temperature", number, 20.0, "°C", access=PropertyAccess.READ_ONLY
            ),
        ]  # fmt: skip

    async def write_property(self, name: str, value: bool | float | str) -> None:
        match name:
            case "pixel_format":
                self.capture.reformat(dtype=_PIXEL_FORMATS[value])
            case "aoi_x":
                self._values["aoi_x"] = float(round(value / AOI_STEP) * AOI_STEP)
            case "aoi_width":
                rows = self.capture.frame_format().shape[0]
                self.capture.reformat(shape=(rows, round(value / AOI_STEP) * AOI_STEP))
            case "exposure":
                self._values["exposure"] = round(value, 5)
            case _:
                self._values[name] = value


class SimulatedFreeRunningCamera(SoftwareTimed, ImageStreaming, TimedDevice):
    """A camera with no trigger line, free-running once the acquisition reaches
    the phase it chose, a frame every ``interval`` seconds. Its one property,
    ``frame_interval``, is that interval, which may change at any time.
    ``settings`` are applied at launch, as for any device.

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
        settings: Iterable[tuple[str, bool | float | str]] = (),
    ) -> None:
        super().__init__()
        self.launch_settings = tuple(settings)
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

    async def read_properties(self) -> list[Property]:
        return [Property("frame_interval", PropertyKind.NUMBER, self.interval, "s", 1e-4, 10.0)]

    async def write_property(self, name: str, value: bool | float | str) -> None:
        self.interval = value
