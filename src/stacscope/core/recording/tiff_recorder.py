"""TIFFRecorder: writes one acquisition from an image buffer to a TIFF file.

A camera whose driver cannot write files records through one of these. The
camera starts a recorder for each acquisition it is asked to save. The
recorder reads every frame the camera publishes to its image buffer and
writes it to the file, one page per frame, in order. At the stop the camera
tells it how many frames the acquisition held; it finishes writing them,
closes the file, and ends.

The file is a BigTIFF, which tifffile, Fiji and most TIFF readers open as a
stack. Each page also carries its frame's time stamps in private tags, which
the first page's description lists along with the clock they come from:

    65000  device_ticks   the camera's clock, in raw ticks
    65001  host_time_ns   the computer's clock when the frame was seen

The computer's clock is its monotonic clock, in nanoseconds: steady, and the
same clock every part of STACScope keeps its own record on, but with no date
of its own. The description's wall_clock_anchor pairs one reading of it with
the date and time (wall_time, UTC), taken as the file opened; a page's date
is wall_time_ns + (host_time_ns - anchor host_time_ns).

Every frame is written, or the recording fails. A recorder that falls so far
behind that the camera overwrites a frame it has not written yet ends with
an error, and so does one whose disk is full. The camera then stops the
acquisition. How far behind it may fall is set by the number of frames the
buffer holds; benchmarks/tiff_write.py measures what a disk keeps up with.

For another file format, copy this class and change what it writes.
"""

import asyncio
import contextlib
import json
import threading
import time
import traceback
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import tifffile

from stacscope.core.actors import Actor, Priority
from stacscope.core.buffer import Frame, ImageBuffer, MetaField, Overrun
from stacscope.core.protocol import FinishRecording, RecorderStarted, RecordingFinished

DEVICE_TICKS_TAG = 65000
"""TIFF tag holding a page's device time, in the device's raw ticks."""

HOST_TIME_TAG = 65001
"""TIFF tag holding a page's host time, in nanoseconds."""

POLL = 0.001
"""Seconds the writer sleeps when it has caught up with the camera."""

# Private tags (65000 and up are reusable by anyone), SLONG8: BigTIFF only.
# The frame number is not written: a recording that loses a frame ends, so a
# page's index is its frame's (user, 2026-10-01).
_TAGS = (
    (MetaField.DEVICE_TIME, DEVICE_TICKS_TAG, "device_ticks"),
    (MetaField.HOST_TIME, HOST_TIME_TAG, "host_time_ns"),
)


@dataclass(frozen=True)
class _WriterEnded:
    """From the writer thread to its own actor, in-process only: the thread
    is done, with ``error`` or ``None``."""

    error: BaseException | None


class TIFFRecorder(Actor):
    """Records acquisition ``acquisition_id`` of the image buffer
    ``buffer_name`` to a new TIFF file at ``path``. ``source`` names the
    device, for the file's description.

    Launching opens the buffer and creates the file, then reports
    ``RecorderStarted`` to its parent. It fails if the buffer is on another
    acquisition, if ``path`` exists (a file is never written over) or if
    its directory does not.

    Told ``FinishRecording(frames)``, it writes up to that many frames,
    closes the file, answers ``RecordingFinished(frames)`` and stops; it
    goes on handling messages while it writes. It ends with an error
    instead, as soon as it happens, if a frame was overwritten before it
    was written (``Overrun``), the buffer started another acquisition, or a
    write failed; the file is closed first, so the frames written stay
    readable. Stopped before it is told, it closes the file with what it
    has; stopped once told, it writes them all first. ``frames_written``
    counts them.
    """

    def __init__(self, buffer_name: str, acquisition_id: int, path: Path, *, source: str = "") -> None:
        super().__init__()
        self.buffer_name = buffer_name
        self.acquisition_id = acquisition_id
        self.path = Path(path)
        self.source = source
        self.frames_written = 0
        # Set on the loop, read by the writer thread: each is one reference,
        # written once, so neither needs a lock.
        self._target: int | None = None
        self._finisher = None
        self._abort = False
        self._reader: ImageBuffer | None = None
        # (tag code, metadata field) for each field the producer fills.
        self._tags: list[tuple[int, str]] = []
        self._file = None
        self._tiff: tifffile.TiffWriter | None = None
        self._anchor: dict[str, object] = {}
        self._thread: threading.Thread | None = None

    async def setup(self) -> None:
        self._reader = ImageBuffer.open(self.buffer_name)
        if self._reader.acquisition_id != self.acquisition_id:
            raise RuntimeError(
                f"buffer {self.buffer_name!r} is on acquisition {self._reader.acquisition_id}, "
                f"not {self.acquisition_id}"
            )
        # "x": created here or not at all. Checking first and opening after
        # would leave a window for another writer.
        self._file = open(self.path, "xb")
        self._tiff = tifffile.TiffWriter(self._file, bigtiff=True)
        # Host times are on the monotonic clock, which can be compared with
        # every actor's record but says nothing of the date: one reading of
        # each, together, converts them all (CM48, CM49).
        host, wall = time.monotonic_ns(), time.time_ns()
        self._anchor = {
            "host_time_ns": host,
            "wall_time_ns": wall,
            "wall_time": datetime.fromtimestamp(wall / 1e9, UTC).isoformat(),
        }
        loop = asyncio.get_running_loop()
        self._thread = threading.Thread(target=self._run, args=(loop,), name=f"TIFFRecorder {self.path.name}")
        self._thread.start()
        self.report(RecorderStarted())

    async def handle(self, msg: object) -> None:
        match msg:
            case FinishRecording(frames):
                # Not awaited: the answer is the thread's to give, when it is
                # done (the parent/child rule: a wait is a state). The thread
                # reads the target on its next pass.
                self._target = frames
                self._finisher = self.sender
            case _WriterEnded(error):
                if error is not None:
                    raise error  # the parent hears it as this actor's exit
                if self._finisher is not None:
                    # URGENT, as its exit will be: at NORMAL, the ChildExited
                    # that follows the stop would overtake it at the parent.
                    self._finisher.tell(RecordingFinished(self.frames_written), Priority.URGENT)
                self.stop()
            case _:
                await super().handle(msg)

    async def teardown(self) -> None:
        # Told to finish, it finishes, whoever stops it: a parent's exit waits
        # for a child that is finishing, never cuts it short (user,
        # 2026-10-02). Otherwise it closes the file with what it has.
        if self._target is None:
            self._abort = True
        try:
            if self._thread is not None:
                # The thread closes the file itself, after its last write.
                await asyncio.to_thread(self._thread.join)
            else:
                self._close_file()
        finally:
            if self._reader is not None:
                self._reader.close()

    # --- the writer thread ---------------------------------------------------------------

    def _run(self, loop: asyncio.AbstractEventLoop) -> None:
        error = None
        try:
            self._write_all()
        except BaseException as exc:
            # The traceback holds the writer's locals, a view of the buffer
            # among them, for as long as the error lives, and the reader cannot
            # close while one does (K9). Cleared here, every way out is covered,
            # including those of a recorder copied from this one.
            traceback.clear_frames(exc.__traceback__)
            exc.add_note(f"{self.frames_written} frame(s) written to {self.path}")
            error = exc
        finally:
            try:
                self._close_file()
            except Exception as exc:
                # A file that does not close may have lost its last pages.
                if error is None:
                    error = exc
                else:
                    error.add_note(f"closing the file also failed: {exc!r}")
        # The loop may already be gone if the process is going down; nothing
        # is left to tell then.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(self._writer_ended, error)

    def _writer_ended(self, error: BaseException | None) -> None:
        # URGENT: a failure mid-acquisition must reach the camera now, so the
        # acquisition stops.
        self.ref.tell(_WriterEnded(error), Priority.URGENT)

    def _write_all(self) -> None:
        reader = self._reader
        fields = reader.metadata.fields
        tags = [(code, name) for field, code, name in _TAGS if field in fields]
        description = json.dumps(
            {
                "source": self.source,
                "buffer": self.buffer_name,
                "acquisition_id": self.acquisition_id,
                "device_time_source": reader.metadata.device_time_source.name,
                "device_clock_hz": reader.metadata.device_clock_hz,
                "frame_tags": {str(code): name for code, name in tags},
                "wall_clock_anchor": self._anchor,
            }
        )
        self._tags = tags
        cursor = reader.cursor(reader.acquisition_start)
        while not self._abort:
            target = self._target
            if target is not None and self.frames_written >= target:
                return
            try:
                frame = cursor.next()
            except Overrun:
                # A recorder behind finds its frames gone before it finds the
                # new acquisition's, which looks like being lapped (L8).
                if reader.acquisition_id != self.acquisition_id:
                    raise self._superseded(reader.acquisition_id) from None
                raise
            if frame is None:
                time.sleep(POLL)
                continue
            if frame.acquisition_id != self.acquisition_id:
                raise self._superseded(frame.acquisition_id)
            index, meta = frame.index, frame.meta
            pixels = self.copy_frame(frame)
            # A view must not outlive its use: the reader cannot close while
            # one is alive (K9).
            del frame
            # Checked after the copy: a copy taken while the camera was writing
            # the slot is two frames at once, and must never reach the file.
            if not reader.still_valid(index):
                exc = Overrun(1)
                exc.add_note(f"frame {index} was overwritten while it was being copied")
                raise exc
            self.write_frame(pixels, meta, description if self.frames_written == 0 else None)
            self.frames_written += 1

    def _superseded(self, acquisition_id: int) -> RuntimeError:
        return RuntimeError(
            f"the buffer started acquisition {acquisition_id} before "
            f"acquisition {self.acquisition_id} finished recording"
        )

    def copy_frame(self, frame: Frame) -> np.ndarray:
        """The frame's pixels, as a packed copy of its view."""
        # Copied, not written from the view, so the check after it covers
        # every byte that is written. Packed drops a driver's row padding.
        return np.array(frame.data, order="C", copy=True)

    def write_frame(self, pixels: np.ndarray, meta: np.void, description: str | None) -> None:
        """Appends one page: ``pixels``, with the time stamps in ``meta``, and
        ``description`` on the first page only."""
        self._tiff.write(
            pixels,
            contiguous=False,  # a page of its own, so it can carry its own tags
            metadata=None,  # no tifffile shape description: the pages are the shape
            description=description,
            extratags=[(code, "q", 1, int(meta[name]), True) for code, name in self._tags],
        )

    def _close_file(self) -> None:
        tiff, self._tiff = self._tiff, None
        file, self._file = self._file, None
        try:
            if tiff is not None:
                tiff.close()
        finally:
            if file is not None:
                file.close()
