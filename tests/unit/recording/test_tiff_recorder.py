"""L1-L10 -- the TIFF recorder: one acquisition of one buffer, to one file.

A camera whose driver cannot write files records through a TIFFRecorder,
which reads the camera's image buffer like any other consumer (thesis
§3.4.3). It is the one consumer for which a lost frame is a failure: a file
missing a plane is not the acquisition, so the recorder writes every frame
or ends with an error (user, 2026-10-01). Whether the disk keeps up is the
user's to arrange; benchmarks/tiff_write.py measures it.

These run on the real clock: the recorder writes on a thread of its own,
which virtual time cannot drive. Each wait has a deadline.
"""

import asyncio
import json
import time
import uuid

import numpy as np
import pytest
import tifffile

from stacscope.core.actors import Actor, ChildExited, Fault, ask, describe, spawn
from stacscope.core.buffer import ImageBuffer, Layout, MetaField, Metadata, Overrun, TimeSource
from stacscope.core.protocol import FinishRecording, RecorderStarted, RecordingFinished
from stacscope.core.recording import DEVICE_TICKS_TAG, HOST_TIME_TAG, TIFFRecorder

STAMPED = Metadata(
    fields=MetaField.HW_FRAME | MetaField.DEVICE_TIME | MetaField.HOST_TIME,
    device_time_source=TimeSource.CAMERA,
    device_clock_hz=1e7,
)
DEADLINE = 5.0


class Producer:
    """A camera's side of the buffer, publishing frames by hand: each one a
    pattern no other frame has, stamped with ticks a reader can predict."""

    def __init__(self, *, shape=(4, 6), n_slots=8, in_flight=1, row_pad=0, metadata=STAMPED):
        self.name = "l" + uuid.uuid4().hex[:20]
        self.shape = shape
        self.writer = ImageBuffer.create(self.name, capacity_bytes=1 << 20, max_slots=16)
        strides = None if not row_pad else ((shape[1] + row_pad) * 2, 2)
        self.writer.configure(Layout(shape, "<u2", n_slots, strides=strides, in_flight=in_flight), metadata)
        self.acquisition_id = self.writer.start_acquisition()
        self.sent: list[np.ndarray] = []

    def publish(self, n=1):
        for _ in range(n):
            index = self.writer.published()
            k = index - self.writer.acquisition_start
            pixels = (np.arange(np.prod(self.shape)).reshape(self.shape) + 100 * k).astype("<u2")
            self.writer.frames[self.writer.slot_of(index)][...] = pixels
            self.writer.stamp(index, hw_frame=k, device_ticks=1000 * k + 7, host_time_ns=10**12 + k)
            self.writer.publish(1)
            self.sent.append(pixels)

    def close(self):
        self.writer.close()


class Parent(Actor):
    """The camera's side of the actors: hears its recorder end."""

    def __init__(self):
        super().__init__()
        self.exits: list[ChildExited] = []

    async def handle(self, msg):
        if isinstance(msg, ChildExited):
            self.exits.append(msg)


@pytest.fixture
def producer():
    made = []

    def make(**kwargs):
        made.append(Producer(**kwargs))
        return made[-1]

    yield make
    for p in made:
        p.close()


async def launch(recorder):
    """Spawn ``recorder`` under a parent, and wait until it has started or
    failed to."""
    parent = Parent()
    spawn(parent, name="camera" + uuid.uuid4().hex[:8])
    ref = parent.spawn_child(recorder, name="recorder")
    await asyncio.wait(
        [asyncio.ensure_future(recorder.started.wait()), asyncio.ensure_future(recorder.stopped.wait())],
        return_when=asyncio.FIRST_COMPLETED,
        timeout=DEADLINE,
    )
    return parent, ref


async def finish(ref, frames):
    return await ask(ref, FinishRecording(frames), timeout=DEADLINE)


async def ended(actor):
    """Wait for ``actor`` to end, and check it ended whole: a teardown that
    failed (a reader that would not close because a frame was still held,
    K9) rides on the exit error as a note, and would otherwise pass."""
    await asyncio.wait_for(actor.stopped.wait(), DEADLINE)
    assert "teardown also failed" not in (describe(actor.exit_error) or "")


async def exit_of(parent):
    """The parent's one ChildExited, once it has arrived."""
    deadline = time.monotonic() + DEADLINE
    while not parent.exits:
        assert time.monotonic() < deadline, "the parent never heard its recorder end"
        await asyncio.sleep(0.005)
    [exit] = parent.exits
    return exit


async def written(recorder, n):
    """Wait for the writer thread to have written ``n`` frames."""
    deadline = time.monotonic() + DEADLINE
    while recorder.frames_written < n:
        assert time.monotonic() < deadline, f"{recorder.frames_written} of {n} written"
        await asyncio.sleep(0.005)


def read(path):
    """Every page of the file, stacked: (frames, rows, columns)."""
    with tifffile.TiffFile(path) as tf:
        return np.stack([page.asarray() for page in tf.pages]) if tf.pages else np.zeros((0, 0, 0))


def tags(path):
    """Each page's tags, by code."""
    with tifffile.TiffFile(path) as tf:
        return [{tag.code: tag.value for tag in page.tags.values()} for page in tf.pages]


# --- L1-L2: what the file holds ------------------------------------------------------------


async def test_l1_every_frame_is_written_in_order_and_packed(producer, tmp_path):
    """Rows a driver pads (the uEye's pitch) are not image: the file holds
    the frames as a reader of the TIFF expects them, one page per frame, in
    the order they were published."""
    cam = producer(row_pad=10)
    path = tmp_path / "run-01_camera.tif"
    recorder = TIFFRecorder(cam.name, cam.acquisition_id, path)
    _, ref = await launch(recorder)

    cam.publish(5)
    assert await finish(ref, 5) == RecordingFinished(5)
    await ended(recorder)

    data = read(path)
    assert data.shape == (5, 4, 6)
    np.testing.assert_array_equal(data, np.stack(cam.sent))
    assert recorder.exit_error is None


async def test_l2_each_page_carries_its_frames_time_stamps(producer, tmp_path):
    """The camera's clock and the computer's, per frame, in private tags the
    first page's description names, with the clock's source and rate: what
    is needed to put frames on a time axis after the fact. The frame number
    is left out: frames are lost only by ending the recording, so a page's
    index is its frame's (user, 2026-10-01)."""
    cam = producer()
    path = tmp_path / "run-01_camera.tif"
    recorder = TIFFRecorder(cam.name, cam.acquisition_id, path, source="camera")
    _, ref = await launch(recorder)

    cam.publish(3)
    await finish(ref, 3)
    await ended(recorder)

    stamps = tags(path)
    assert [t[DEVICE_TICKS_TAG] for t in stamps] == [7, 1007, 2007]
    assert [t[HOST_TIME_TAG] for t in stamps] == [10**12, 10**12 + 1, 10**12 + 2]
    with tifffile.TiffFile(path) as tf:
        description = json.loads(tf.pages[0].description)
    assert description == {
        "source": "camera",
        "buffer": cam.name,
        "acquisition_id": cam.acquisition_id,
        "device_time_source": "CAMERA",
        "device_clock_hz": 1e7,
        "frame_tags": {str(DEVICE_TICKS_TAG): "device_ticks", str(HOST_TIME_TAG): "host_time_ns"},
    }


async def test_l2_a_producer_with_no_clock_gets_no_time_tags(producer, tmp_path):
    """A tag for a field the producer never fills would be a column of
    zeros that looks like data."""
    cam = producer(metadata=Metadata())
    path = tmp_path / "run-01_camera.tif"
    recorder = TIFFRecorder(cam.name, cam.acquisition_id, path)
    _, ref = await launch(recorder)

    cam.publish(2)
    await finish(ref, 2)
    await ended(recorder)

    assert all(DEVICE_TICKS_TAG not in t and HOST_TIME_TAG not in t for t in tags(path))
    with tifffile.TiffFile(path) as tf:
        assert json.loads(tf.pages[0].description)["frame_tags"] == {}


# --- L3-L5: the start and the finish -------------------------------------------------------


class Slow(TIFFRecorder):
    """A disk that takes ``delay`` seconds a frame."""

    delay = 0.02

    def write_frame(self, pixels, meta, description):
        time.sleep(self.delay)
        super().write_frame(pixels, meta, description)


async def test_l3_finish_waits_for_frames_published_but_not_yet_written(producer, tmp_path):
    """The stop comes the moment capture ends, with the writer behind it:
    the answer must wait until the file holds every frame, or the camera
    would report a complete file that is not."""
    cam = producer(n_slots=16)
    path = tmp_path / "run-01_camera.tif"
    recorder = Slow(cam.name, cam.acquisition_id, path)
    _, ref = await launch(recorder)

    cam.publish(10)
    assert await finish(ref, 10) == RecordingFinished(10)

    assert len(read(path)) == 10


async def test_l4_a_file_that_exists_is_never_written_over(producer, tmp_path):
    """A repeated stem would otherwise replace an acquisition already taken.
    The recorder does not start, and the file is left as it was."""
    cam = producer()
    path = tmp_path / "run-01_camera.tif"
    path.write_bytes(b"an earlier acquisition")
    recorder = TIFFRecorder(cam.name, cam.acquisition_id, path)

    await launch(recorder)
    await ended(recorder)

    assert isinstance(recorder.exit_error, FileExistsError)
    assert path.read_bytes() == b"an earlier acquisition"


async def test_l4_a_directory_that_is_gone_fails_the_launch(producer, tmp_path):
    cam = producer()
    recorder = TIFFRecorder(cam.name, cam.acquisition_id, tmp_path / "gone" / "run-01_camera.tif")

    await launch(recorder)
    await ended(recorder)

    assert isinstance(recorder.exit_error, FileNotFoundError)


async def test_l4_a_recorder_for_another_acquisition_does_not_start(producer, tmp_path):
    """Started for an acquisition the buffer is no longer on, it would write
    the wrong frames under the right name."""
    cam = producer()
    path = tmp_path / "run-01_camera.tif"
    recorder = TIFFRecorder(cam.name, cam.acquisition_id + 1, path)

    await launch(recorder)
    await ended(recorder)

    assert "acquisition" in str(recorder.exit_error)
    assert not path.exists()


async def test_l5_an_acquisition_with_no_frames_leaves_an_empty_file(producer, tmp_path):
    """Stopped before the first trigger: the file says the acquisition was
    recorded and held nothing, which a missing file would not (user,
    2026-10-01)."""
    cam = producer()
    path = tmp_path / "run-01_camera.tif"
    recorder = TIFFRecorder(cam.name, cam.acquisition_id, path)
    _, ref = await launch(recorder)

    assert await finish(ref, 0) == RecordingFinished(0)
    await ended(recorder)

    with tifffile.TiffFile(path) as tf:
        assert len(tf.pages) == 0


# --- L6-L9: every frame, or an error -------------------------------------------------------


async def test_l6_a_recorder_the_camera_laps_ends_with_an_overrun(producer, tmp_path):
    """The user's rule: the writer handles every frame or faults. The
    frames it did write are closed into a readable file, because they are
    still the user's data."""
    cam = producer(n_slots=4)
    path = tmp_path / "run-01_camera.tif"
    recorder = Slow(cam.name, cam.acquisition_id, path)
    parent, _ = await launch(recorder)
    cam.publish(1)
    await written(recorder, 1)

    cam.publish(20)
    await ended(recorder)

    assert isinstance(recorder.exit_error, Overrun)
    kept = read(path)
    assert 0 < len(kept) < 20
    np.testing.assert_array_equal(kept, np.stack(cam.sent[: len(kept)]))
    exit = await exit_of(parent)
    assert "Overrun" in exit.error, "the camera hears why, without asking"


class Lapped(TIFFRecorder):
    """The camera laps the slot while its frame is being copied out."""

    camera: Producer

    def copy_frame(self, frame):
        pixels = super().copy_frame(frame)
        if frame.index == self.camera.writer.acquisition_start:
            self.camera.publish(4)
        return pixels


async def test_l7_a_frame_overwritten_while_it_is_copied_is_not_written(producer, tmp_path):
    """A copy taken while the camera writes the slot is half one frame and
    half another. Written, it would sit in the file looking like data, so
    the recorder checks the frame is still whole after copying it."""
    cam = producer(n_slots=4, in_flight=1)
    path = tmp_path / "run-01_camera.tif"
    recorder = Lapped(cam.name, cam.acquisition_id, path)
    recorder.camera = cam
    await launch(recorder)

    cam.publish(1)
    await ended(recorder)

    assert isinstance(recorder.exit_error, Overrun)
    with tifffile.TiffFile(path) as tf:
        assert len(tf.pages) == 0, "the torn frame never reached the file"


class LappedLate(TIFFRecorder):
    """The camera laps the slot after the frame was checked, before it is
    written."""

    camera: Producer

    def write_frame(self, pixels, meta, description):
        if self.frames_written == 0:
            self.camera.publish(4)
        super().write_frame(pixels, meta, description)


async def test_l7_the_page_written_is_the_frame_as_it_was_checked(producer, tmp_path):
    """The check covers the copy, so the copy is what is written: a camera
    that reaches the slot during the write cannot change the page. The
    recording still ends, since the next frame is gone."""
    cam = producer(n_slots=4, in_flight=1)
    path = tmp_path / "run-01_camera.tif"
    recorder = LappedLate(cam.name, cam.acquisition_id, path)
    recorder.camera = cam
    await launch(recorder)

    cam.publish(1)
    await ended(recorder)

    assert isinstance(recorder.exit_error, Overrun)
    np.testing.assert_array_equal(read(path), cam.sent[:1])


async def test_l8_a_new_acquisition_under_the_recorder_ends_it(producer, tmp_path):
    """Its frames are not this recording's, whatever their count."""
    cam = producer()
    path = tmp_path / "run-01_camera.tif"
    recorder = TIFFRecorder(cam.name, cam.acquisition_id, path)
    await launch(recorder)
    cam.publish(2)
    await written(recorder, 2)

    cam.writer.start_acquisition()
    cam.publish(2)
    await ended(recorder)

    assert "acquisition" in str(recorder.exit_error)
    assert len(read(path)) == 2


async def test_l8_a_new_acquisition_says_so_even_to_a_recorder_behind(producer, tmp_path):
    """Behind, the recorder finds its frames gone before it finds new ones,
    which looks like being lapped. The error names the real cause, so the
    user is not sent to benchmark a disk that was keeping up."""
    cam = producer(n_slots=8)
    path = tmp_path / "run-01_camera.tif"
    recorder = Slow(cam.name, cam.acquisition_id, path)
    await launch(recorder)
    cam.publish(4)
    await written(recorder, 1)

    cam.writer.start_acquisition()
    cam.publish(1)
    await ended(recorder)

    assert "started acquisition" in str(recorder.exit_error)


class Failing(TIFFRecorder):
    """A disk that fills after two frames."""

    def write_frame(self, pixels, meta, description):
        if self.frames_written == 2:
            raise OSError("no space left on the device")
        super().write_frame(pixels, meta, description)


async def test_l9_a_write_that_fails_ends_the_recorder_with_its_error(producer, tmp_path):
    """No headroom is kept: the file system says when the disk is full
    (user, 2026-10-01), and the camera hears it at once, mid-acquisition,
    not at the stop."""
    cam = producer()
    path = tmp_path / "run-01_camera.tif"
    recorder = Failing(cam.name, cam.acquisition_id, path)
    parent, _ = await launch(recorder)

    cam.publish(5)
    await ended(recorder)

    assert isinstance(recorder.exit_error, OSError)
    assert len(read(path)) == 2
    exit = await exit_of(parent)
    assert "no space left" in exit.error


# --- L10: stopped from outside -------------------------------------------------------------


async def test_l10_a_recorder_stopped_before_its_finish_closes_the_file(producer, tmp_path):
    """The camera stops its recorder when the camera itself goes, clean or
    not. The thread must be joined and the reader closed with no frame
    held (K9), or the stop would fail, and the file holds what was
    written."""
    cam = producer()
    path = tmp_path / "run-01_camera.tif"
    recorder = TIFFRecorder(cam.name, cam.acquisition_id, path)
    await launch(recorder)
    cam.publish(3)
    await written(recorder, 3)

    recorder.stop()
    await ended(recorder)

    assert recorder.exit_error is None
    assert len(read(path)) == 3


# --- CM33, CM39: the recorder as a child that reports ------------------------------


class Slower(Slow):
    delay = 0.05


class Hearing(Parent):
    """A parent that keeps everything its recorder tells it."""

    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)
        await super().handle(msg)


async def test_cm33_a_recorder_reports_to_its_parent_once_its_file_is_open(producer, tmp_path):
    """The camera knows its recorder only by its reports: started, which
    completes the camera's arm, and finished. Nothing is read off the
    recorder object."""
    cam = producer()
    parent = Hearing()
    spawn(parent, name="camera" + uuid.uuid4().hex[:8])
    recorder = TIFFRecorder(cam.name, cam.acquisition_id, tmp_path / "run-01_camera.tif")
    parent.spawn_child(recorder, name="recorder")

    deadline = time.monotonic() + DEADLINE
    while RecorderStarted() not in parent.heard:
        assert time.monotonic() < deadline, "never reported started"
        await asyncio.sleep(0.005)
    assert (tmp_path / "run-01_camera.tif").exists()
    recorder.stop()
    await ended(recorder)


async def test_cm39_finishing_does_not_hold_the_recorders_mailbox(producer, tmp_path):
    """Told to finish, it sets its target and goes on handling messages
    while its thread writes; the answer comes when the thread is done. A
    Fault told to it meanwhile is passed up at once -- the proof its handler
    is free."""
    cam = producer(n_slots=16)
    parent = Hearing()
    spawn(parent, name="camera" + uuid.uuid4().hex[:8])
    recorder = Slower(cam.name, cam.acquisition_id, tmp_path / "run-01_camera.tif")
    ref = parent.spawn_child(recorder, name="recorder")
    await asyncio.wait_for(recorder.started.wait(), DEADLINE)
    cam.publish(10)  # half a second of writing

    finishing = asyncio.ensure_future(finish(ref, 10))
    await asyncio.sleep(0.05)
    probe = Fault(ref, "probe", False)
    ref.tell(probe)
    await asyncio.sleep(0.05)

    assert probe in parent.heard and not finishing.done()
    assert await finishing == RecordingFinished(10)
    await ended(recorder)


async def test_cm39_a_recorder_stopped_while_finishing_finishes_first(producer, tmp_path):
    """Its handler no longer waits for the thread, so a stop now lands while
    it is finishing. A parent's exit waits for such a child, and the child
    writes every frame it was told of: finishing is never cut short."""
    cam = producer(n_slots=16)
    path = tmp_path / "run-01_camera.tif"
    recorder = Slow(cam.name, cam.acquisition_id, path)
    _, ref = await launch(recorder)
    cam.publish(6)

    ref.tell(FinishRecording(6))
    await asyncio.sleep(0.02)
    recorder.stop()
    await ended(recorder)

    assert len(read(path)) == 6
