"""A Hamamatsu ORCA-Flash4.0 as a STACScope device.

The camera streams its frames into a shared-memory image buffer, which any
viewer or processor finds by the buffer's name, and takes its triggers from
the DAQ on its trigger input. Its properties are the camera's own, by the
names and value labels the camera itself uses, with the limits and units the
camera states:

    GetProperties()                            every one
    SetProperty("EXPOSURE TIME", 0.01)         a number, in seconds
    SetProperty("SENSOR MODE", "PROGRESSIVE")  a mode, by its label

A set is answered with every property it changed, as the camera applied it,
or with Rejected and the reason. During an acquisition, only what the camera
allows while capturing can change, such as the exposure time; a change that
would alter the frame's size or layout is refused. Settings given in the
device's configuration are applied, in order, when it launches.
GetShortestTriggerPeriod answers with the shortest trigger period the
current settings allow, as the camera states it; keep the trigger slower
than that.

An acquisition started with a file name is also recorded, by the camera's
driver, to a DCIMG file named after it and the device, in the save
directory:

    SetSaveDirectory("E:/data")     then an acquisition named "run-01"
    records to E:/data/run-01_camera.dcimg  (for a device named "camera")

The save directory can also be given in the device's configuration. A file
is never written over; one that already exists refuses the acquisition.

A file takes its whole size on disk when it opens. It may fill the free
space on its disk, less the larger of 100 GiB and a tenth of the disk, and
the configuration can set a lower limit. An acquisition whose file ends up holding fewer frames than were streamed is
reported as a fatal fault when it stops.

Windows only, and DCAM-API must be installed. DCAM-API allows one camera
session per process, so give the camera a process of its own. Opening the
camera takes a few seconds.
"""
# For end use and not for commercial purposes.

import asyncio
import contextlib
import os
from collections.abc import Iterable
from pathlib import Path
from shutil import disk_usage

from stacscope.adapters.dcam import _api
from stacscope.adapters.dcam.capture import DcamError, DcamFrameCapture
from stacscope.core.actors import Fault
from stacscope.core.devices import (
    DeviceState,
    ImageStreaming,
    PropertyRefused,
    Recording,
    TimedDevice,
)
from stacscope.core.ports import FrameFormat
from stacscope.core.protocol import (
    GetShortestTriggerPeriod,
    Property,
    PropertyAccess,
    PropertyKind,
    ShortestTriggerPeriod,
)

IN_FLIGHT = 2
"""Frames the Flash's grabber may be writing past the newest it reported:
measured 1 on the dev box Flash in every readout tried, declared with a
margin of 1."""

RECORDER_FINISH = 5.0
"""Seconds a stop waits for the driver's recorder to finish writing before
it closes the file anyway, and reports it."""

HEADROOM_BYTES = 100 * 2**30
HEADROOM_FRACTION = 0.10
"""A recording leaves the larger of these free on its disk: 100 GiB, or a
tenth of the disk."""
# The legacy kept 100 GiB free; the tenth is the usual advice for NTFS, and
# the larger governs on a large disk (user, 2026-09-25). It also leaves room
# for anything else writing to the disk, such as a conversion to HDF5.

_MOST_FRAMES = 2**31 - 1  # DCAMREC_OPEN's frame limit is an int32

_UNITS = {
    _api.PROPUNIT_SECOND: "s",
    _api.PROPUNIT_CENTIGRADE: "°C",
    _api.PROPUNIT_KELVIN: "K",
    _api.PROPUNIT_METERPERSECOND: "m/s",
    _api.PROPUNIT_PERSECOND: "Hz",
    _api.PROPUNIT_DEGREE: "°",
    _api.PROPUNIT_MICROMETER: "µm",
}
"""DCAM's units as symbols. Not all SI (°C, °), but each is the symbol a
reader expects."""


class OrcaFlash(ImageStreaming, Recording, TimedDevice):
    """DCAM camera number ``camera``, streaming into the image buffer
    ``buffer_name``.

    ``capacity_bytes`` and ``max_slots`` size the buffer once, for every
    acquisition; each acquisition lays out ``n_slots`` frames of whatever
    format the camera's settings give at the time. ``settings`` is a list
    of ``(name, value)`` pairs applied in order when the device launches, a
    value being a number, or a mode's label; one refused fails the launch. ``in_flight`` overrides the Flash's declared
    in-flight depth, for another camera or readout. ``save_directory`` is
    where recordings go, an existing directory, checked at launch; without
    one, only acquisitions that are not recorded can start.
    ``max_record_frames`` caps how many frames a file may hold. ``library``
    replaces ``dcamapi.dll``, for tests.

    Its properties are every property the camera can read, by the camera's
    names: a mode as a LABEL of the camera's labels, anything else as a
    NUMBER with the camera's limits, step and unit. One the camera may
    change while capturing is ALWAYS, any other it may change is IDLE. If
    the camera accepts a change while armed that alters the frame format,
    the device puts it back, answers ``Rejected`` and reports a fatal
    ``Fault``, since frames of the new format may already have been written.

    Answers ``GetShortestTriggerPeriod`` with ``ShortestTriggerPeriod``: the
    camera's own TIMING MIN TRIGGER INTERVAL, for its current settings.

    Answers ``SetSaveDirectory`` with ``SaveDirectory``, or ``Rejected``
    while armed or for a directory that does not exist. An acquisition with
    a file name records to ``<save directory>/<name>_<device>.dcimg``. A
    file may hold what the disk has room for beyond ``HEADROOM_BYTES`` or
    ``HEADROOM_FRACTION`` of it, whichever is larger, capped by
    ``max_record_frames``. The arm is refused if no directory is set, the
    file already exists, or the disk has no room for one frame. A file that holds fewer frames than were
    published, missed any, or was still being written when closed is
    reported at the stop as a fatal ``Fault``.
    """

    file_suffix = ".dcimg"

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
        save_directory: str | os.PathLike | None = None,
        max_record_frames: int | None = None,
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
        self.save_directory = None if save_directory is None else Path(save_directory)
        if max_record_frames is not None and max_record_frames < 1:
            raise ValueError(f"max_record_frames must be at least 1, not {max_record_frames}")
        self.max_record_frames = max_record_frames
        self._library = library
        self._recording_to: Path | None = None

    async def setup(self) -> None:
        # Checked before DCAM opens: a mistyped directory would otherwise be
        # found only by the first recorded acquisition.
        self.check_save_directory()
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
        # Decided once: the names a device lists may not change.
        self._listed = [n for n in self.capture.names() if self.capture.attributes(n).readable]
        # The launch settings are applied in here, before the segment exists.
        # The layout is read at each arm, not here.
        await super().setup()

    def _open(self) -> DcamFrameCapture:
        return DcamFrameCapture(self.camera_index, in_flight=self.in_flight, library=self._library)

    async def read_properties(self) -> list[Property]:
        return [self._describe(name) for name in self._listed]

    def _describe(self, name: str) -> Property:
        attr = self.capture.attributes(name)
        if not attr.writable:
            access = PropertyAccess.READ_ONLY
        elif attr.while_capturing:
            access = PropertyAccess.ALWAYS
        else:
            access = PropertyAccess.IDLE
        if attr.labelled:
            labels = tuple(self.capture.labels(name))
            return Property(
                name, PropertyKind.LABEL, self.capture.label(name), labels=labels, access=access
            )
        return Property(
            name,
            PropertyKind.NUMBER,
            self.capture.get(name),
            _UNITS.get(attr.unit, ""),
            attr.minimum,
            attr.maximum,
            attr.step,
            access=access,
        )

    async def write_property(self, name: str, value: bool | float | str) -> None:
        # Memory is attached in every state but IDLE.
        armed = self.state is not DeviceState.IDLE
        if armed:
            fmt = self.capture.frame_format()
            before = self.capture.get(name)
        self.capture.set(name, value)
        if armed and self._format_moved(fmt):
            self._put_back(name, before)

    async def handle(self, msg: object) -> None:
        match msg:
            case GetShortestTriggerPeriod():
                self._shortest_trigger_period()
            case _:
                await super().handle(msg)

    def _shortest_trigger_period(self) -> None:
        # DCAM states the floor itself, for the current exposure, readout and
        # trigger mode; nothing is computed here.
        try:
            seconds = self.capture.get("TIMING MIN TRIGGER INTERVAL")
        except (ValueError, DcamError) as exc:
            self._reject(str(exc))
            return
        self.reply(ShortestTriggerPeriod(seconds))

    def _format_moved(self, fmt: FrameFormat) -> bool:
        # DCAM refuses a change to the frame's shape or readout while memory
        # is attached, which is what the slots rely on, and its attributes
        # say what it takes while capturing, but neither is a promise about
        # the frame format. Checked after the fact because a list kept here
        # would be a guess about every camera.
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
        # Reported here, since the core answers any refusal as a mere one.
        self.report(Fault(self.ref, reason, fatal=True))
        raise PropertyRefused(reason)

    # --- recording: the driver's own DCIMG writer ---------------------------------------

    async def open_recording(self, path: Path) -> None:
        if path.exists():
            raise FileExistsError(f"{path} already exists, and is never recorded over")
        self.capture.record(path, self._frame_limit(path.parent))
        self._recording_to = path

    async def close_recording(self) -> None:
        path, self._recording_to = self._recording_to, None
        status = await self.capture.close_recording(RECORDER_FINISH)
        # Published is the count to hold the file to: by now every frame
        # the capture delivered has been published, unless frames were lost,
        # which is its own fatal fault.
        published = self.writer.published() - self.writer.acquisition_start
        problems = []
        if status.frames < published:
            problems.append(f"it holds {status.frames} of the {published} frames published")
        if status.missing:
            problems.append(f"the recorder missed {status.missing} frame(s)")
        if status.recording:
            problems.append("the recorder was still writing when the file was closed")
        if problems:
            raise RuntimeError(f"{path}: {'; '.join(problems)}")

    def _frame_limit(self, directory: Path) -> int:
        """The most frames a file in ``directory`` may hold: what the disk
        has room for beyond its headroom, capped by the config. Raises
        ``OSError`` if the disk has no room for a frame."""
        # DCIMG reserves a file's whole size when it opens (V4), so the limit
        # is space taken from the disk for the whole acquisition. Sizing it
        # to the acquisition's length would need the trigger line's pulse
        # count, and so would tie the camera to the waveforms; that was
        # built and taken out (user, 2026-09-30). DCAM's own sizes are used,
        # as the SDK's recording sample does.
        fixed = self.capture.get("RECORD FIXED BYTES PER FILE") + self.capture.get(
            "RECORD FIXED BYTES PER SESSION"
        )
        per_frame = self.capture.get("RECORD FIXED BYTES PER FRAME")
        usage = disk_usage(directory)
        headroom = max(HEADROOM_BYTES, usage.total * HEADROOM_FRACTION)
        room = max(0, int((usage.free - headroom - fixed) // per_frame))
        if room < 1:
            raise OSError(
                f"{directory} has no room for a frame: {usage.free} bytes free, "
                f"{int(headroom)} kept free, {int(fixed + per_frame)} needed"
            )
        limit = min(room, _MOST_FRAMES)
        if self.max_record_frames is not None:
            limit = min(limit, self.max_record_frames)
        return limit
