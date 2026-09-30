"""A Hamamatsu ORCA-Flash4.0 as a STACScope device.

The camera streams its frames into a shared-memory image buffer, which any
viewer or processor finds by the buffer's name, and takes its triggers from
the DAQ on its trigger input. Its settings are changed by message, by the
names and value labels the camera itself uses:

    SetProperty("EXPOSURE TIME", 0.01)
    SetPropertyLabel("SENSOR MODE", "PROGRESSIVE")
    GetProperty("TRIGGER SOURCE")

Each is answered with what the camera applied, in a PropertyValue, or with
Rejected and the camera's reason. GetShortestTriggerPeriod answers with the
shortest trigger period the current settings allow, as the camera states
it; keep the trigger slower than that. During an acquisition, a setting that
would change the frame's size or layout is refused; others, such as the
exposure time, change at once if the camera accepts them. Settings given in
the device's configuration are applied, in order, when it launches.

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

from stacscope.adapters.dcam.capture import DcamError, DcamFrameCapture
from stacscope.adapters.dcam.messages import (
    GetProperty,
    PropertyValue,
    SaveDirectory,
    SetProperty,
    SetPropertyLabel,
    SetSaveDirectory,
)
from stacscope.core.devices import DeviceState, ImageStreaming, Recording, TimedDevice
from stacscope.core.protocol import GetShortestTriggerPeriod, ShortestTriggerPeriod
from stacscope.core.ports import FrameFormat

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


class OrcaFlash(ImageStreaming, Recording, TimedDevice):
    """DCAM camera number ``camera``, streaming into the image buffer
    ``buffer_name``.

    ``capacity_bytes`` and ``max_slots`` size the buffer once, for every
    acquisition; each acquisition lays out ``n_slots`` frames of whatever
    format the camera's settings give at the time. ``settings`` is a list
    of ``(name, value)`` pairs applied in order when the device launches, a
    value being a number or one of the camera's labels; one the camera
    refuses fails the launch. ``in_flight`` overrides the Flash's declared
    in-flight depth, for another camera or readout. ``save_directory`` is
    where recordings go, an existing directory, checked at launch; without
    one, only acquisitions that are not recorded can start.
    ``max_record_frames`` caps how many frames a file may hold. ``library``
    replaces ``dcamapi.dll``, for tests.

    Answers ``SetProperty``, ``SetPropertyLabel`` and ``GetProperty`` with
    ``PropertyValue``, or ``Rejected``. While armed, a change that alters the
    frame format is refused; if the camera accepts one anyway, the device
    puts it back, answers ``Rejected`` and publishes a fatal ``Fault``,
    since frames of the new format may already have been written.

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
        if self.save_directory is not None and not self.save_directory.is_dir():
            raise NotADirectoryError(f"save_directory {self.save_directory} is not an existing directory")
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
            case SetSaveDirectory(path):
                self._set_save_directory(path)
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

    def _set_save_directory(self, path: str) -> None:
        if self.state is DeviceState.ARMED:
            self._reject("the save directory cannot change during an acquisition")
        elif not Path(path).is_dir():
            # Not created: a mistyped path would put acquisitions where
            # nobody looks.
            self._reject(f"{path} is not an existing directory")
        else:
            self.save_directory = Path(path)
            self.reply(SaveDirectory(path))

    # --- recording: the driver's own DCIMG writer ---------------------------------------

    def target(self, root_filename: str) -> Path | None:
        if root_filename and self.save_directory is None:
            raise RuntimeError(
                "no save directory is set: give save_directory in the device's "
                "configuration, or send SetSaveDirectory"
            )
        return super().target(root_filename)

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

    def _read(self, name: str) -> PropertyValue:
        value = self.capture.get(name)
        try:
            label = self.capture.label(name)
        except ValueError:  # a plain number, with no labels
            label = None
        return PropertyValue(name, value, label)
