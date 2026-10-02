"""L11-L17 -- BufferRecording: a streaming device that records through a
recorder of its own, started for each acquisition (thesis §3.4.3).

The role puts the recorder where ``Recording`` puts a file: it starts once
the device's hardware is armed and before the first frame can be captured,
and is told the final count once the last frames are published. What the
recorder does with the frames is L1-L10; these are about the device.

The camera runs in virtual time and the recorder writes on a real thread,
so waits on the recorder poll with a real deadline.
"""

import asyncio
import threading
import time
import uuid

import numpy as np
import pytest
import tifffile

from stacscope.adapters.simulated import SimulatedCamera, VirtualClock
from stacscope.core.actors import Actor, Fault, Rejected, Subscribe, ask, describe, spawn
from stacscope.core.buffer import ImageBuffer
from stacscope.core.devices import BufferRecording, TimedDevice
from stacscope.core.protocol import DeviceReady, DeviceStopped, FinishRecording, StartDevice, StopDevice
from stacscope.core.recording import TIFFRecorder

DEADLINE = 5.0


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)


class Rig:
    """A simulated camera under a parent, with a subscriber, on virtual time."""

    def __init__(self, cls=SimulatedCamera, **kwargs):
        self.clock = VirtualClock()
        self.parent = Actor()
        spawn(self.parent, clock=self.clock, name="coordinator")
        self.device = cls("b" + uuid.uuid4().hex[:20], shape=(4, 6), n_slots=8, **kwargs)
        self.ref = self.parent.spawn_child(self.device, name="camera")
        self.listener = Listener()
        # A child reports to its parent, and the root publishes: listen there.
        self.parent.ref.tell(Subscribe(spawn(self.listener, clock=self.clock, name="listener")))

    async def started(self):
        await self.clock.advance(0)
        assert self.device.started.is_set(), self.device.exit_error

    async def ask(self, msg):
        return await ask(self.ref, msg, timeout=DEADLINE, clock=self.clock)

    def trigger(self, n=1):
        for _ in range(n):
            self.device.capture._triggered()

    async def published(self, n):
        """Let the device publish ``n`` frames of this acquisition."""
        writer = self.device.writer
        for _ in range(1000):
            await self.clock.advance(0.01)
            if writer.published() - writer.acquisition_start >= n:
                return
        raise AssertionError(f"{writer.published() - writer.acquisition_start} of {n} published")

    def faults(self):
        return [m for m in self.listener.heard if isinstance(m, Fault)]

    async def stop(self):
        if not self.device.stopped.is_set():
            self.device.stop()
            self.ref.tell(StopDevice())
            await asyncio.wait_for(self.device.stopped.wait(), DEADLINE)
        # A close that failed (a recorder left running, a segment still held)
        # would otherwise end the device quietly.
        assert self.device.exit_error is None, describe(self.device.exit_error)


@pytest.fixture
async def rigs():
    made = []

    def make(**kwargs):
        made.append(Rig(**kwargs))
        return made[-1]

    yield make
    for rig in made:
        await rig.stop()


async def until(condition, what):
    deadline = time.monotonic() + DEADLINE
    while not condition():
        assert time.monotonic() < deadline, what
        await asyncio.sleep(0.005)


def read(path):
    with tifffile.TiffFile(path) as tf:
        return np.stack([page.asarray() for page in tf.pages]) if tf.pages else np.zeros((0, 0, 0))


def recorders(device):
    """The names of the device's children that are recorders."""
    return sorted(name for name in device._child_names if name.startswith("recorder"))


def writer_threads():
    return [t for t in threading.enumerate() if t.name.startswith("TIFFRecorder")]


# --- L11-L13 -----------------------------------------------------------------------------


async def test_l11_an_acquisition_with_no_stem_starts_no_recorder(rigs, tmp_path):
    rig = rigs(save_directory=tmp_path)
    await rig.started()

    assert await rig.ask(StartDevice("", -1)) == DeviceReady()
    rig.trigger(3)
    assert await rig.ask(StopDevice()) == DeviceStopped()

    assert recorders(rig.device) == [] and list(tmp_path.iterdir()) == []


async def test_l12_a_recorder_that_cannot_start_refuses_the_arm_and_undoes_it(rigs, tmp_path):
    """A repeated stem would write over an acquisition already taken. The
    arm fails as a file that will not open does: refused, fatal, and with
    the camera disarmed and its frame memory given back."""
    existing = tmp_path / "run-01_camera.tif"
    existing.write_bytes(b"an earlier acquisition")
    rig = rigs(save_directory=tmp_path)
    await rig.started()

    answer = await rig.ask(StartDevice("run-01", -1))
    await rig.clock.advance(0)

    assert isinstance(answer, Rejected) and "run-01_camera.tif" in answer.reason
    assert existing.read_bytes() == b"an earlier acquisition"
    assert rig.faults() and rig.faults()[0].fatal
    assert rig.device.capture._slots is None, "the frame memory was given back"
    assert await rig.ask(StartDevice("", -1)) == DeviceReady(), "and the camera still streams"


async def test_l13_at_the_stop_the_file_holds_every_frame_published(rigs, tmp_path):
    """The count is told once the last frames are published, and the stop
    is answered once the file holds them all."""
    rig = rigs(save_directory=tmp_path)
    await rig.started()
    assert await rig.ask(StartDevice("run-01", -1)) == DeviceReady()
    rig.trigger(5)
    await rig.published(5)
    rig.trigger(2)  # published only by the stop's final pass

    assert await rig.ask(StopDevice()) == DeviceStopped()

    frames = read(tmp_path / "run-01_camera.tif")
    reader = ImageBuffer.open(rig.device.buffer_name)
    try:
        start = reader.acquisition_start
        assert reader.published() - start == 7
        np.testing.assert_array_equal(frames, np.stack([reader.frames[reader.slot_of(start + i)] for i in range(7)]))
    finally:
        reader.close()
    assert rig.faults() == []


# --- L14: a recorder that fails ------------------------------------------------------------


class Failing(TIFFRecorder):
    """A disk that fills after two frames."""

    def write_frame(self, pixels, meta, description):
        if self.frames_written == 2:
            raise OSError("no space left on the device")
        super().write_frame(pixels, meta, description)


class FailingCamera(SimulatedCamera):
    recorder = Failing


async def test_l14_a_recorder_that_fails_mid_acquisition_is_a_fatal_fault_at_once(rigs, tmp_path):
    """The camera hears it as its recorder's exit and publishes it, so the
    Coordinator aborts now rather than at the stop. The stop that follows
    adds no second fault for the same failure."""
    rig = rigs(cls=FailingCamera, save_directory=tmp_path)
    await rig.started()
    assert await rig.ask(StartDevice("run-01", -1)) == DeviceReady()
    rig.trigger(4)
    await rig.published(4)

    await until(lambda: rig.faults(), "the camera never published the recorder's failure")
    [fault] = rig.faults()
    assert fault.fatal and "no space left" in fault.reason

    assert await rig.ask(StopDevice()) == DeviceStopped()
    await rig.clock.advance(0)
    assert len(rig.faults()) == 1
    assert len(read(tmp_path / "run-01_camera.tif")) == 2


async def test_l14_a_recorder_that_fails_finishing_is_a_fatal_fault_at_the_stop(rigs, tmp_path):
    """Frames published only by the stop's final pass reach the recorder
    while it finishes: the failure is the stop's, reported once."""
    rig = rigs(cls=FailingCamera, save_directory=tmp_path)
    await rig.started()
    assert await rig.ask(StartDevice("run-01", -1)) == DeviceReady()
    rig.trigger(4)  # none published before the stop

    assert await rig.ask(StopDevice()) == DeviceStopped()
    await until(lambda: rig.faults(), "no fault")
    await asyncio.sleep(0.05)
    await rig.clock.advance(0)

    [fault] = rig.faults()
    assert fault.fatal and "no space left" in fault.reason


class Short(TIFFRecorder):
    """A recorder adapted from TIFFRecorder that loses count by one, and
    says nothing: the bug a copy can bring."""

    async def handle(self, msg):
        if isinstance(msg, FinishRecording):
            msg = FinishRecording(msg.frames - 1)
        await super().handle(msg)


class ShortCamera(SimulatedCamera):
    recorder = Short


async def test_l14_a_file_short_of_the_frames_published_is_a_fatal_fault(rigs, tmp_path):
    """The camera checks the count itself rather than trust the recorder's
    word, as the Flash checks its DCIMG file: a recorder may be any class."""
    rig = rigs(cls=ShortCamera, save_directory=tmp_path)
    await rig.started()
    assert await rig.ask(StartDevice("run-01", -1)) == DeviceReady()
    rig.trigger(3)

    assert await rig.ask(StopDevice()) == DeviceStopped()
    await rig.clock.advance(0)

    [fault] = rig.faults()
    assert fault.fatal and "holds 2 of the 3 frames published" in fault.reason


# --- L15-L16 -------------------------------------------------------------------------------


async def test_l15_each_acquisition_gets_its_own_recorder_and_file(rigs, tmp_path):
    rig = rigs(save_directory=tmp_path)
    await rig.started()
    for stem, n in (("run-01", 2), ("run-02", 3)):
        assert await rig.ask(StartDevice(stem, -1)) == DeviceReady()
        rig.trigger(n)
        assert await rig.ask(StopDevice()) == DeviceStopped()

    assert len(read(tmp_path / "run-01_camera.tif")) == 2
    assert len(read(tmp_path / "run-02_camera.tif")) == 3
    assert rig.faults() == []


async def test_l16_a_camera_that_goes_while_recording_takes_its_recorder_with_it(rigs, tmp_path):
    """A recorder outliving its camera would hold the file open and the
    buffer mapped. A parent's exit stops its children before its own
    teardown (CM10), so the recorder is stopped before the camera's disarm
    could tell it how many frames to write: it closes the file with what it
    had written, which a shutdown mid-acquisition may leave short. An
    acquisition stopped first (``StopDevice``) records every frame."""
    rig = rigs(save_directory=tmp_path)
    await rig.started()
    assert await rig.ask(StartDevice("run-01", -1)) == DeviceReady()
    rig.trigger(3)
    await rig.published(3)
    assert len(recorders(rig.device)) == 1

    await rig.stop()

    assert len(read(tmp_path / "run-01_camera.tif")) <= 3, "readable, with what was written"
    assert writer_threads() == [], "the writer thread was joined"


class Slow(TIFFRecorder):
    """A disk that takes 50 ms a frame."""

    def write_frame(self, pixels, meta, description):
        time.sleep(0.05)
        super().write_frame(pixels, meta, description)


class SlowCamera(SimulatedCamera):
    recorder = Slow


async def test_l16_a_camera_cancelled_mid_stop_waits_for_its_recorder(rigs, tmp_path):
    """A shutdown cancels every task, including a camera's whose stop is
    still waiting for its recorder. The camera's close then waits for the
    recorder in its place: told to finish, it writes every frame published
    and closes the file (user, 2026-10-01). Without that, the camera would
    be gone while its writer thread still held the file and the segment."""
    rig = rigs(cls=SlowCamera, save_directory=tmp_path)
    await rig.started()
    assert await rig.ask(StartDevice("run-01", -1)) == DeviceReady()
    rig.trigger(6)
    await rig.published(6)

    rig.ref.tell(StopDevice())
    await rig.clock.advance(0)  # the stop is now waiting on the recorder, 50 ms a frame
    rig.device._task.cancel()
    await asyncio.wait_for(rig.device.stopped.wait(), DEADLINE)

    assert writer_threads() == [], "the camera ended after its recorder, not before"
    assert len(read(tmp_path / "run-01_camera.tif")) == 6


# --- L17: composition ----------------------------------------------------------------------


def test_l17_buffer_recording_needs_a_buffer_to_record():
    """It reads the device's own image buffer: without ImageStreaming there
    is nothing to read, which is a class-definition error, not a failure at
    the first recorded acquisition."""
    with pytest.raises(TypeError, match="ImageStreaming"):

        class NoBuffer(BufferRecording, TimedDevice):
            pass
