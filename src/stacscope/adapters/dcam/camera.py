"""A Hamamatsu ORCA-Flash4.0 as a STACScope device.

The camera streams its frames into a shared-memory image buffer, which any
viewer or processor finds by the buffer's name, and takes its triggers from
the DAQ on its trigger input. Its settings are changed by message, by the
names and value labels the camera itself uses:

    SetProperty("EXPOSURE TIME", 0.01)
    SetPropertyLabel("SENSOR MODE", "PROGRESSIVE")
    GetProperty("TRIGGER SOURCE")

Each is answered with what the camera applied, in a PropertyValue, or with
Rejected and the camera's reason. During an acquisition, a setting that
would change the frame's size or layout is refused; others, such as the
exposure time, change at once if the camera accepts them. Settings given in
the device's configuration are applied, in order, when it launches.

Windows only, and DCAM-API must be installed. DCAM-API allows one camera
session per process, so give the camera a process of its own. Opening the
camera takes a few seconds.
"""
# For end use and not for commercial purposes.

import asyncio
import contextlib
from collections.abc import Iterable

from stacscope.adapters.dcam.capture import DcamError, DcamFrameCapture
from stacscope.adapters.dcam.messages import (
    GetProperty,
    PropertyValue,
    SetProperty,
    SetPropertyLabel,
)
from stacscope.core.devices import DeviceState, ImageStreaming, TimedDevice
from stacscope.core.ports import FrameFormat

IN_FLIGHT = 2
"""Frames the Flash's grabber may be writing past the newest it reported:
measured 1 on the dev box Flash in every readout tried, declared with a
margin of 1."""


class OrcaFlash(ImageStreaming, TimedDevice):
    """DCAM camera number ``camera``, streaming into the image buffer
    ``buffer_name``.

    ``capacity_bytes`` and ``max_slots`` size the buffer once, for every
    acquisition; each acquisition lays out ``n_slots`` frames of whatever
    format the camera's settings give at the time. ``settings`` is a list
    of ``(name, value)`` pairs applied in order when the device launches, a
    value being a number or one of the camera's labels; one the camera
    refuses fails the launch. ``in_flight`` overrides the Flash's declared
    in-flight depth, for another camera or readout. ``library`` replaces
    ``dcamapi.dll``, for tests.

    Answers ``SetProperty``, ``SetPropertyLabel`` and ``GetProperty`` with
    ``PropertyValue``, or ``Rejected``. While armed, a change that alters the
    frame format is refused; if the camera accepts one anyway, the device
    puts it back, answers ``Rejected`` and publishes a fatal ``Fault``,
    since frames of the new format may already have been written.
    """

    def __init__(
        self,
        buffer_name: str,
        *,
        n_slots: int,
        capacity_bytes: int,
        max_slots: int,
        camera: int = 0,
        settings: Iterable[tuple[str, float | str]] = (),
        in_flight: int = IN_FLIGHT,
        library=None,
    ) -> None:
        super().__init__()
        self.buffer_name = buffer_name
        self.n_slots = n_slots
        self.capacity_bytes = capacity_bytes
        self.max_slots = max_slots
        self.camera_index = camera
        self.launch_settings = tuple(settings)
        self.in_flight = in_flight
        self._library = library

    async def setup(self) -> None:
        # DCAM takes seconds to start, and the runtime answers heartbeats on
        # this loop: opened here, a camera in its own process reads as hung.
        opening = asyncio.ensure_future(asyncio.to_thread(self._open))
        try:
            self.capture = await asyncio.shield(opening)
        except asyncio.CancelledError:
            # The thread cannot be stopped, and the camera it opens must reach
            # teardown to be closed: DCAM allows one session per process, so a
            # session left open refuses every later one here.
            with contextlib.suppress(Exception):
                self.capture = await opening
            raise
        for name, value in self.launch_settings:
            try:
                self.capture.set(name, value)
            except Exception as exc:
                exc.add_note(f"applying {name} = {value!r} from the device's settings")
                raise
        # Before the segment exists, so a device whose settings are refused
        # never creates one for a consumer to find. The layout is read at
        # each arm, not here.
        await super().setup()

    def _open(self) -> DcamFrameCapture:
        return DcamFrameCapture(self.camera_index, in_flight=self.in_flight, library=self._library)

    async def handle(self, msg: object) -> None:
        match msg:
            case GetProperty(name):
                self._answer(name)
            case SetProperty(name, value) | SetPropertyLabel(name, value):
                self._change(name, value)
            case _:
                await super().handle(msg)

    def _answer(self, name: str) -> None:
        try:
            value = self._read(name)
        except (ValueError, DcamError) as exc:
            self._reject(str(exc))
            return
        self.reply(value)

    def _change(self, name: str, value: float | str) -> None:
        armed = self.state is DeviceState.ARMED
        try:
            if armed:
                fmt = self.capture.frame_format()
                before = self.capture.get(name)
            self.capture.set(name, value)
            if armed and self._format_moved(fmt):
                self._put_back(name, before)
                return
            applied = self._read(name)
        except (ValueError, DcamError) as exc:
            self._reject(str(exc))
            return
        self.reply(applied)

    def _format_moved(self, fmt: FrameFormat) -> bool:
        # DCAM refuses a change to the frame's shape or readout while memory
        # is attached, which is what the slots rely on, but the reference
        # does not list which settings it refuses. Checked after the fact
        # because a list kept here would be a guess about every camera.
        try:
            return self.capture.frame_format() != fmt
        except Exception:
            return True  # a format that cannot be described has moved

    def _put_back(self, name: str, before: float) -> None:
        reason = f"{name} changed the frame format while armed; put back to {before}"
        try:
            self.capture.set(name, before)
        except Exception as exc:
            reason = f"{name} changed the frame format while armed, and could not be put back: {exc}"
        # Fatal either way: frames of the new format may already be in slots
        # laid out for the old one, under a header that describes the old.
        self._reject(reason, fatal=True)

    def _read(self, name: str) -> PropertyValue:
        value = self.capture.get(name)
        try:
            label = self.capture.label(name)
        except ValueError:  # a plain number, with no labels
            label = None
        return PropertyValue(name, value, label)
