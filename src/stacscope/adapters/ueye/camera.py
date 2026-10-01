"""An IDS uEye camera as a STACScope device.

The camera streams its frames into a shared-memory image buffer, which any
viewer or processor finds by the buffer's name, and takes its triggers from
the DAQ on its trigger input. Each setting is changed by a message of its
own, answered with the setting as the camera took it:

    SetPixelFormat("RAW10")        SetAOI(0, 384, 1280, 256)
    SetPixelClock(40)              SetFrameRate(20.0)
    SetExposure(10.0)              SetTrigger("rising", 0)

and GetCameraSettings reads them all. During an acquisition, the pixel
format and area of interest cannot change; the others change at once if
the camera accepts them. Settings given in the device's configuration are
applied when it launches.

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
from pathlib import Path

from stacscope.adapters.ueye.capture import UEyeFrameCapture
from stacscope.adapters.ueye.messages import (
    AOI,
    CameraSettings,
    Exposure,
    FrameRate,
    GetCameraSettings,
    PixelClock,
    PixelFormat,
    SetAOI,
    SetExposure,
    SetFrameRate,
    SetPixelClock,
    SetPixelFormat,
    SetTrigger,
    Trigger,
)
from stacscope.core.devices import BufferRecording, ImageStreaming, TimedDevice
from stacscope.core.protocol import GetShortestTriggerPeriod, ShortestTriggerPeriod

IN_FLIGHT = 2
"""Frames the camera's driver may be writing past the newest it reported:
measured 1 on the instrument's camera, from software triggers and from the
DAQ, and declared with a margin of 1."""


class UEyeCamera(ImageStreaming, BufferRecording, TimedDevice):
    """uEye camera ``camera`` (its camera ID; 0 for the first one free),
    streaming into the image buffer ``buffer_name``.

    ``capacity_bytes`` and ``max_slots`` size the buffer once, for every
    acquisition; each acquisition lays out ``n_slots`` frames of whatever
    format the camera's settings give at the time. The settings given are
    applied at launch, in the camera's order: ``pixel_format``, ``aoi`` (x,
    y, width, height), ``pixel_clock`` (MHz), ``frame_rate_fps``,
    ``exposure_ms``, then the trigger, ``trigger_edge`` ("rising" or
    "falling") and ``trigger_delay_us``. Any left out keep the camera's
    defaults. One the camera refuses fails the launch. ``in_flight``
    overrides the declared in-flight depth, and ``open_timeout`` how long
    to wait for a camera another process holds. ``save_directory`` is where
    recordings go, an existing directory, checked at launch; without one,
    only acquisitions that are not recorded can start. ``ueye`` replaces
    pyueye, for tests.

    Answers each ``Set...`` message with the setting as the camera took it,
    ``GetCameraSettings`` with ``CameraSettings``, and
    ``GetShortestTriggerPeriod`` with ``ShortestTriggerPeriod``; or with
    ``Rejected``, whose reason says why. ``SetPixelFormat`` and ``SetAOI``
    are refused during an acquisition. ``SetSaveDirectory`` is answered
    with ``SaveDirectory``, or ``Rejected`` during an acquisition or for a
    directory that does not exist.
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
        aoi: tuple[int, int, int, int] | None = None,
        pixel_clock: int | None = None,
        frame_rate_fps: float | None = None,
        exposure_ms: float | None = None,
        trigger_edge: str = "rising",
        trigger_delay_us: int = 0,
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
        self.aoi = None if aoi is None else tuple(aoi)
        self.pixel_clock = pixel_clock
        self.frame_rate_fps = frame_rate_fps
        self.exposure_ms = exposure_ms
        self.trigger_edge = trigger_edge
        self.trigger_delay_us = trigger_delay_us
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
        # Each bounds the next: the AOI and pixel clock bound the frame rate,
        # which bounds the exposure.
        for name, value, apply in [
            ("aoi", self.aoi, lambda v: self.capture.set_aoi(*v)),
            ("pixel_clock", self.pixel_clock, self.capture.set_pixel_clock),
            ("frame_rate_fps", self.frame_rate_fps, self.capture.set_frame_rate),
            ("exposure_ms", self.exposure_ms, self.capture.set_exposure),
        ]:
            if value is None:
                continue
            try:
                apply(value)
            except Exception as exc:
                exc.add_note(f"applying {name} = {value!r} from the device's configuration")
                raise
        try:
            self.capture.set_trigger(self.trigger_edge, self.trigger_delay_us)
        except Exception as exc:
            exc.add_note("applying the trigger from the device's configuration")
            raise
        # Before the segment exists, so a device whose settings are refused
        # never creates one for a consumer to find.
        await super().setup()

    def _open(self) -> UEyeFrameCapture:
        options = {} if self.open_timeout is None else {"open_timeout": self.open_timeout}
        return UEyeFrameCapture(
            self.camera_id, in_flight=self.in_flight, pixel_format=self.pixel_format, ueye=self._ueye, **options
        )

    async def handle(self, msg: object) -> None:
        capture = self.capture
        match msg:
            case SetPixelFormat(name):
                self._answer(lambda: PixelFormat(capture.set_pixel_format(name)))
            case SetAOI(x, y, width, height):
                self._answer(lambda: AOI(*capture.set_aoi(x, y, width, height)))
            case SetPixelClock(mhz):
                self._answer(lambda: PixelClock(capture.set_pixel_clock(mhz)))
            case SetFrameRate(fps):
                self._answer(lambda: FrameRate(capture.set_frame_rate(fps)))
            case SetExposure(ms):
                self._answer(lambda: Exposure(capture.set_exposure(ms)))
            case SetTrigger(edge, delay_us):
                self._answer(lambda: Trigger(*capture.set_trigger(edge, delay_us)))
            case GetCameraSettings():
                self._answer(self._settings)
            case GetShortestTriggerPeriod():
                self._answer(lambda: ShortestTriggerPeriod(capture.shortest_trigger_period()))
            case _:
                await super().handle(msg)

    def _answer(self, make) -> None:
        # The capture refuses a change to the frame while its memory is
        # attached (RuntimeError), and a bad name (ValueError); the camera
        # refuses the rest (UEyeError). Each is the requester's to handle.
        try:
            answer = make()
        except (ValueError, RuntimeError) as exc:
            self._reject(str(exc))
            return
        self.reply(answer)

    def _settings(self) -> CameraSettings:
        capture = self.capture
        edge, delay_us = capture.trigger()
        return CameraSettings(
            pixel_format=capture.pixel_format(),
            aoi=capture.aoi(),
            pixel_clock_mhz=capture.pixel_clock(),
            pixel_clocks_mhz=tuple(capture.pixel_clocks()),
            frame_rate_fps=capture.frame_rate(),
            exposure_ms=capture.exposure(),
            trigger_edge=edge,
            trigger_delay_us=delay_us,
        )

