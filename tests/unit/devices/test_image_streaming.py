"""D10–D11 — where the control flow meets the data flow.

Milestones 1 and 2 have not touched until now. A streaming device is the seam:
the Coordinator's protocol on one side, the shared-memory buffer on the other,
and the rule that keeps them apart is the buffer's lifetime. **The buffer
belongs to the device, not to the acquisition.** The software cannot assume it
knows every consumer, so a consumer that has the segment open keeps it open
across acquisitions, and learns from the buffer itself that a new one began.
"""

import uuid

import numpy as np
import pytest

from stacscope.adapters.simulated import SimulatedFrameCapture, VirtualClock
from stacscope.core.actors import Actor, Rejected, ask, spawn
from stacscope.core.buffer import ImageBuffer, Layout
from stacscope.core.devices import ImageStreaming, NotReady, TimedDevice
from stacscope.core.protocol import DeviceReady, DeviceStopped, StartDevice, StopDevice

MiB = 1 << 20
SHAPE = (4, 6)


class CameraHardware(TimedDevice):
    """The adapter's own half. A real driver is pointed at the frame memory
    here, so what it can see of the buffer is what this records."""

    def __init__(self, name, *, shape=SHAPE, n_slots=8):
        super().__init__()
        self.buffer_name = name
        self.capacity_bytes = MiB
        self.max_slots = 16
        self.shape = shape
        self.n_slots = n_slots
        self.calls = []
        self.refuse = False

    async def arm(self, root_filename, cycles):
        if self.refuse:
            raise NotReady("not now")
        self.calls.append(("arm", self.writer.frames.shape, self.writer.acquisition_id))

    async def disarm(self):
        self.calls.append("disarm")

    async def close(self):
        self.calls.append("hardware closed")


class FakeCamera(ImageStreaming, CameraHardware):
    """A camera that produces frames only when a test says so."""

    def layout(self):
        return Layout(shape=self.shape, dtype="<u2", n_slots=self.n_slots)

    def expose(self, n=1):
        """What a trigger would cause: fill the next slot and publish it."""
        for _ in range(n):
            index = self.writer.published()
            self.writer.frames[self.writer.slot_of(index)] = index
            self.writer.publish(1)


class Rig:
    def __init__(self, kind=None, **kwargs):
        self.clock = VirtualClock()
        self.parent = Actor()
        spawn(self.parent, clock=self.clock, name="coordinator")
        self.name = "t" + uuid.uuid4().hex[:20]
        self.device = (kind or FakeCamera)(self.name, **kwargs)
        self.ref = self.parent.spawn_child(self.device, name="camera")

    async def ask(self, msg):
        return await ask(self.ref, msg, timeout=1.0, clock=self.clock)

    async def launched(self):
        await self.clock.advance(0)  # setup runs on the loop
        return self.device

    async def shut_down(self):
        self.device.stop()
        self.ref.tell(StopDevice())
        await self.device.stopped.wait()


@pytest.fixture
async def rig():
    rigs = []

    def make(kind=None, **kwargs):
        rigs.append(Rig(kind, **kwargs))
        return rigs[-1]

    yield make
    for r in rigs:
        if not r.device.stopped.is_set():
            await r.shut_down()


# --- D10: the buffer is the device's, and each acquisition is announced -------


async def test_the_buffer_exists_from_launch_not_from_the_first_acquisition(rig):
    """A consumer can attach before anything is acquired — which is how a
    viewer is started and left running."""
    r = rig()
    await r.launched()

    reader = ImageBuffer.open(r.name)
    try:
        assert reader.published() == 0
    finally:
        reader.close()


async def test_starting_configures_the_layout_and_begins_an_acquisition(rig):
    r = rig()
    await r.launched()

    assert await r.ask(StartDevice("run-01", 4)) == DeviceReady()
    r.device.expose(3)

    reader = ImageBuffer.open(r.name)
    try:
        assert reader.frames.shape == (8, *SHAPE)
        assert reader.published() == 3
    finally:
        reader.close()


async def test_a_consumer_follows_a_new_acquisition_without_reopening(rig):
    """The consumer is not part of the control flow and may never hear of a
    new acquisition any other way."""
    r = rig()
    await r.launched()
    await r.ask(StartDevice("run-01", 4))
    r.device.expose(2)

    reader = ImageBuffer.open(r.name)
    try:
        cursor = reader.cursor(reader.oldest())
        first = [cursor.next().index for _ in range(2)]

        await r.ask(StopDevice())
        await r.ask(StartDevice("run-02", 4))
        r.device.expose(1)

        frame = cursor.next()
        assert first == [0, 1]
        assert frame.acquisition_id == 2, "the consumer sees a new acquisition"
        assert frame.index == 2, "and a counter that never goes backwards"
        del frame  # its pixels are a view: the reader cannot close under it
    finally:
        reader.close()


async def test_a_new_layout_reaches_a_consumer_that_was_already_attached(rig):
    r = rig()
    await r.launched()
    await r.ask(StartDevice("run-01", 4))

    reader = ImageBuffer.open(r.name)
    try:
        assert reader.frames.shape == (8, *SHAPE)

        await r.ask(StopDevice())
        r.device.shape = (2, 3)
        r.device.n_slots = 4
        await r.ask(StartDevice("run-02", 4))

        assert reader.sync(), "the layout changed"
        assert reader.frames.shape == (4, 2, 3)
    finally:
        reader.close()


# --- D11: stopping an acquisition is not the end of the buffer ----------------


async def test_the_last_frames_stay_readable_after_the_acquisition_stops(rig):
    """Nothing says a consumer has finished reading when the producer has
    finished writing."""
    r = rig()
    await r.launched()
    await r.ask(StartDevice("run-01", 4))
    r.device.expose(3)

    assert await r.ask(StopDevice()) == DeviceStopped()

    reader = ImageBuffer.open(r.name)
    try:
        assert reader.published() == 3
        assert (reader.frames[reader.slot_of(2)] == 2).all(), "the pixels are still there"
    finally:
        reader.close()


async def test_a_consumer_keeps_its_segment_across_a_stop(rig):
    r = rig()
    await r.launched()
    await r.ask(StartDevice("run-01", 4))
    r.device.expose(1)

    reader = ImageBuffer.open(r.name)
    try:
        await r.ask(StopDevice())
        frame = reader.cursor(0).next()
        assert (np.asarray(frame.data) == 0).all()
        del frame  # its pixels are a view: the reader cannot close under it
    finally:
        reader.close()


async def test_the_segment_goes_when_the_device_does(rig):
    """Buffer lifetime is device lifetime: the segment outlives every
    acquisition and nothing else."""
    r = rig()
    await r.launched()
    await r.ask(StartDevice("run-01", 4))

    await r.shut_down()

    with pytest.raises(FileNotFoundError):
        ImageBuffer.open(r.name, timeout=0.05)


async def test_the_buffer_is_ready_before_the_hardware_is_armed(rig):
    """A driver is pointed at the frame memory while it arms — DCAM attaches
    the slot pointers — so the layout must be in place first."""
    r = rig()
    await r.launched()

    await r.ask(StartDevice("run-01", 4))
    assert r.device.calls == [("arm", (8, *SHAPE), 1)], "layout and acquisition both set"


# --- F5: releasing in order, however the device goes (ImageStreaming.close) ------


class ReleasingCapture(SimulatedFrameCapture):
    """A capture that logs its close and, if told to, fails it the dangerous
    way: it gives the frame memory back and then cannot release the device."""

    def __init__(self, clock, log, *, fails=False):
        super().__init__(clock, shape=SHAPE)
        self.log = log
        self.fails = fails

    async def close(self):
        self.log.append("capture closed")
        if self.fails:
            await self.detach()
            raise RuntimeError("the device would not release")
        await super().close()


class CapturingCamera(ImageStreaming, CameraHardware):
    """A camera whose frames come from a capture, logging each release. With
    ``leak``, the device's own code keeps a view of its frames past close."""

    def __init__(self, name, *, fails=False, leak=False):
        super().__init__(name)
        self.fails = fails
        self.leak = leak
        self.leaked = None

    async def setup(self):
        self.capture = ReleasingCapture(self.clock, self.calls, fails=self.fails)
        await super().setup()
        close = self.writer.close

        def logged_close():
            self.calls.append("segment closed")
            close()

        self.writer.close = logged_close

    async def arm(self, root_filename, cycles):
        await super().arm(root_filename, cycles)
        if self.leak:
            self.leaked = self.writer.frames[0]


async def test_closing_releases_the_capture_then_the_segment_then_the_rest(rig):
    """The hardware lets go of the frame memory before the segment closes,
    and whatever sits below the role closes last."""
    r = rig(CapturingCamera)
    await r.launched()
    await r.ask(StartDevice("run-01", 4))

    await r.shut_down()

    assert r.device.exit_error is None
    assert r.device.calls[-3:] == ["capture closed", "segment closed", "hardware closed"]


async def test_a_capture_that_fails_to_release_leaves_the_segment_mapped(rig):
    """If the device would not release, its driver may still be writing into
    the slots, so the segment stays mapped until the process goes. The pin
    alone cannot be relied on: this adapter gave its view back before
    failing, so closing the segment would have gone through. The rest of the
    device is still released, and the death reason is the capture's."""
    r = rig(CapturingCamera, fails=True)
    await r.launched()
    await r.ask(StartDevice("run-01", 4))

    await r.shut_down()

    try:
        assert "segment closed" not in r.device.calls
        assert r.device.calls[-2:] == ["capture closed", "hardware closed"]
        assert isinstance(r.device.exit_error, RuntimeError)
        ImageBuffer.open(r.name).close()  # still there
    finally:
        r.device.writer.close()


async def test_a_leaked_view_fails_the_close_only_after_everything_else_is_released(rig):
    """A view the producer's own code kept makes the segment refuse to close.
    That is a bug to report, as the device's death reason, but not a reason
    to skip releasing what comes after it."""
    r = rig(CapturingCamera, leak=True)
    await r.launched()
    await r.ask(StartDevice("run-01", 4))

    await r.shut_down()

    try:
        assert r.device.calls[-3:] == ["capture closed", "segment closed", "hardware closed"]
        assert isinstance(r.device.exit_error, BufferError)
    finally:
        r.device.leaked = None
        r.device.writer.close()


async def test_a_refused_arm_gives_the_frame_memory_back(rig):
    """An arm that fails after the slots were attached must detach them, or
    the next arm, which lays the segment out again, finds the hardware still
    holding the old slots, and its attach is refused."""
    r = rig(CapturingCamera)
    await r.launched()
    r.device.refuse = True
    assert isinstance(await r.ask(StartDevice("run-01", 4)), Rejected)

    r.device.refuse = False
    assert await r.ask(StartDevice("run-02", 4)) == DeviceReady()


# --- F4: the pass after the stop ---------------------------------------------------


class StopMidTransferCapture(SimulatedFrameCapture):
    """A frame is in transfer when the capture is stopped, and lands after the
    wait under way has already reported. A driver whose wait runs in a thread
    does this: the count it gave the role is one short by the time the role
    resumes. The simulated capture alone never does, because it counts as the
    role resumes."""

    def __init__(self, clock):
        super().__init__(clock, shape=SHAPE)
        self.in_transfer = False

    async def stop(self):
        self.in_transfer = self._capturing
        await super().stop()

    async def wait_frames(self, seen, timeout):
        delivered = await super().wait_frames(seen, timeout)
        if self.in_transfer:
            self.in_transfer = False
            self._capturing = True  # the transfer completes; no new trigger is taken
            self._triggered()
            self._capturing = False
        return delivered


class StopMidTransferCamera(ImageStreaming, CameraHardware):
    async def setup(self):
        self.capture = StopMidTransferCapture(self.clock)
        await super().setup()


async def test_stop_publishes_a_frame_that_landed_after_the_last_count(rig):
    """The stop takes one more count once the capture has stopped, and
    publishes what it finds. Without it, a frame that completed as the stop
    came is delivered and never published: the last plane of the stack,
    missing, with no fault to say so."""
    r = rig(StopMidTransferCamera)
    await r.launched()
    await r.ask(StartDevice("run-01", 4))

    assert await r.ask(StopDevice()) == DeviceStopped()

    assert r.device.writer.published() == 1
