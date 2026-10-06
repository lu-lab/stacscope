"""An IDS uEye camera as a STACScope device.

The camera streams its frames into a shared-memory image buffer, which any
viewer or processor finds by the buffer's name, and takes its triggers from
the DAQ on its trigger input. Its properties are read with GetProperties
and changed with SetProperty, in seconds, hertz and pixels:

    pixel_format     RAW8 or RAW10
    aoi_x, aoi_y     where the area of interest starts on the sensor
    aoi_width, aoi_height
    pixel_clock      one of the clocks the camera offers, in Hz
    frame_rate       Hz; under a trigger it bounds the exposure
    exposure         seconds
    trigger_edge     rising or falling
    trigger_delay    seconds from the trigger to the exposure

Each is listed with the limits the camera gives now, which move as the
others are set: a slower pixel clock lowers the fastest frame rate, and a
lower frame rate allows a longer exposure, so set them in that order. A set
is answered with every property it changed. During an acquisition, the
pixel format and area of interest cannot change; the others change at once
if the camera accepts them. Settings given in the device's configuration
are applied, in order, when it launches; without any, the camera waits for
a rising edge with no delay.

A trigger that comes before the camera has finished the last frame is
ignored by the camera, and the acquisition stops with a lost frame. Ask
GetShortestTriggerPeriod for the shortest period the current settings
allow, and keep the trigger slower than that.

An acquisition with a file name is recorded to a TIFF file,
<save directory>/<name>_<device>.tif, by a recorder reading the buffer. The
directory comes from the configuration or from SetSaveDirectory. The disk
must keep up with the frame rate: a recorder that falls a buffer's length
behind stops the acquisition. benchmarks/tiff_write.py measures a disk.

The IDS Software Suite must be installed, with the "ueye" extra. Give the
camera a process of its own. Opening it can take a few seconds when
another program has just let it go.
"""

import asyncio
import contextlib
import os
from collections.abc import Iterable
from pathlib import Path

from stacscope.adapters.ueye.capture import PIXEL_FORMATS, TRIGGER_EDGES, UEyeFrameCapture
from stacscope.core.devices import BufferRecording, ImageStreaming, TimedDevice
from stacscope.core.protocol import (
    GetShortestTriggerPeriod,
    Property,
    PropertyAccess,
    PropertyKind,
    ShortestTriggerPeriod,
)

IN_FLIGHT = 2
"""Frames the camera's driver may be writing past the newest it reported:
measured 1 on the instrument's camera, from software triggers and from the
DAQ, and declared with a margin of 1."""

_NUMBER, _LABEL = PropertyKind.NUMBER, PropertyKind.LABEL
_IDLE, _ALWAYS = PropertyAccess.IDLE, PropertyAccess.ALWAYS


class UEyeCamera(ImageStreaming, BufferRecording, TimedDevice):
    """uEye camera ``camera`` (its camera ID; 0 for the first one free),
    streaming into the image buffer ``buffer_name``.

    ``capacity_bytes`` and ``max_slots`` size the buffer once, for every
    acquisition; each acquisition lays out ``n_slots`` frames of whatever
    format the camera's properties give at the time. ``pixel_format`` is
    the format the camera opens in. ``settings`` is a list of ``(name,
    value)`` pairs, by the property names in the module's description,
    applied in order at launch; one refused fails the launch. The camera
    opens triggered on a rising edge with no delay, and any other property
    left out keeps the camera's default. ``in_flight`` overrides
    the declared in-flight depth, and ``open_timeout`` how long to wait for
    a camera another process holds. ``save_directory`` is where recordings
    go, an existing directory, checked at launch; without one, only
    acquisitions that are not recorded can start. ``ueye`` replaces pyueye,
    for tests.

    Answers ``GetShortestTriggerPeriod`` with ``ShortestTriggerPeriod``,
    and ``SetSaveDirectory`` with ``SaveDirectory``, or ``Rejected`` during
    an acquisition or for a directory that does not exist.
    """

    def __init__(
        self,
        buffer_name: str,
        *,
        n_slots: int,
        capacity_bytes: int,
        max_slots: int,
        camera: int = 0,
        pixel_format: str = "RAW8",
        settings: Iterable[tuple[str, float | str]] = (),
        in_flight: int = IN_FLIGHT,
        open_timeout: float | None = None,
        save_directory: str | os.PathLike | None = None,
        ueye=None,
    ) -> None:
        super().__init__()
        self.buffer_name = buffer_name
        self.n_slots = n_slots
        self.capacity_bytes = capacity_bytes
        self.max_slots = max_slots
        self.camera_id = camera
        self.pixel_format = pixel_format
        self.launch_settings = tuple(settings)
        self.in_flight = in_flight
        self.open_timeout = open_timeout
        self.save_directory = None if save_directory is None else Path(save_directory)
        self._ueye = ueye

    async def setup(self) -> None:
        self.check_save_directory()
        # Opened in a thread: the runtime answers heartbeats on this loop,
        # and an open waits out a camera a crashed process held (Y7).
        opening = asyncio.ensure_future(asyncio.to_thread(self._open))
        try:
            self.capture = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # The thread cannot be stopped, and the camera it opens must reach
            # teardown to be exited, or every later open is refused.
            with contextlib.suppress(Exception):
                self.capture = await opening
            raise
        # The capture opens on the rising edge, and the camera opens with no
        # trigger delay (Y8), as the launch arguments this replaced defaulted.
        # The launch settings are applied in here, before the segment exists.
        await super().setup()

    def _open(self) -> UEyeFrameCapture:
        options = {} if self.open_timeout is None else {"open_timeout": self.open_timeout}
        return UEyeFrameCapture(
            self.camera_id, in_flight=self.in_flight, pixel_format=self.pixel_format, ueye=self._ueye, **options
        )

    async def read_properties(self) -> list[Property]:
        # In the order each bounds the next: the format and AOI bound the
        # frame time at a pixel clock, which bounds the frame rate, which
        # bounds the exposure. A batch of writes is applied in this order.
        capture = self.capture
        grid = capture.aoi_grid()
        x, y, width, height = capture.aoi()
        (sensor_w, sensor_h), (min_w, min_h) = grid.sensor, grid.smallest
        shortest, longest, _ = capture.frame_time_range()
        exposure_min, exposure_max, exposure_step = capture.exposure_range()
        delay_min, delay_max, delay_step = capture.trigger_delay_range()
        edge, delay_us = capture.trigger()
        clocks = tuple(float(mhz * 1_000_000) for mhz in capture.pixel_clocks())

        def pixels(name, value, low, high, step):
            return Property(name, _NUMBER, float(value), "px", float(low), float(high), float(step), access=_IDLE)

        return [
            Property("pixel_format", _LABEL, capture.pixel_format(), labels=tuple(PIXEL_FORMATS), access=_IDLE),
            pixels("aoi_x", x, 0, sensor_w - width, grid.position_step[0]),
            pixels("aoi_y", y, 0, sensor_h - height, grid.position_step[1]),
            pixels("aoi_width", width, min_w, sensor_w - x, grid.size_step[0]),
            pixels("aoi_height", height, min_h, sensor_h - y, grid.size_step[1]),
            Property("pixel_clock", _NUMBER, float(capture.pixel_clock() * 1_000_000), "Hz", choices=clocks),
            # No step: a frame time's step is not one of the frame rate's.
            Property("frame_rate", _NUMBER, capture.frame_rate(), "Hz", 1 / longest, 1 / shortest),
            Property(
                "exposure", _NUMBER, capture.exposure() / 1e3, "s",
                exposure_min / 1e3, exposure_max / 1e3, exposure_step / 1e3,
            ),
            Property("trigger_edge", _LABEL, edge, labels=tuple(TRIGGER_EDGES)),
            Property(
                "trigger_delay", _NUMBER, delay_us / 1e6, "s", delay_min / 1e6, delay_max / 1e6, delay_step / 1e6
            ),
        ]  # fmt: skip

    async def write_property(self, name: str, value: float | str) -> None:
        capture = self.capture
        match name:
            case "pixel_format":
                capture.set_pixel_format(value)
            case "aoi_x" | "aoi_y" | "aoi_width" | "aoi_height":
                aoi = dict(zip(("aoi_x", "aoi_y", "aoi_width", "aoi_height"), capture.aoi(), strict=True))
                aoi[name] = round(value)
                capture.set_aoi(*aoi.values())
            case "pixel_clock":
                capture.set_pixel_clock(round(value / 1_000_000))
            case "frame_rate":
                capture.set_frame_rate(value)
            case "exposure":
                capture.set_exposure(value * 1e3)
            case "trigger_edge":
                capture.set_trigger(value, capture.trigger()[1])
            case "trigger_delay":
                capture.set_trigger(capture.trigger()[0], round(value * 1e6))

    async def handle(self, msg: object) -> None:
        match msg:
            case GetShortestTriggerPeriod():
                try:
                    seconds = self.capture.shortest_trigger_period()
                except (ValueError, RuntimeError) as exc:
                    self._reject(str(exc))
                    return
                self.reply(ShortestTriggerPeriod(seconds))
            case _:
                await super().handle(msg)
