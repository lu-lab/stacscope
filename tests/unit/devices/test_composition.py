"""The order of a device's base classes is the order its roles act in.

A device is a ``TimedDevice`` with roles mixed in, and each role wraps every
class listed after it: arming runs down the list, disarming runs back up. So
where a device lists ``Recording`` decides where in its arm the file opens,
and that is the device's choice, not the mixin's (user, 2026-09-24).

These tests pin the composition the Flash needs,
``OrcaFlash(ImageStreaming, Recording, TimedDevice)``. DCAM binds its DCIMG
recorder after the frame memory is attached and before capture starts
(API reference, "Recording control"), and Hamamatsu's recording sample
closes the file after capture stops and before the buffer is released:
alloc, rec_open, cap_record, cap_start, cap_stop, rec_close, buf_release.
The legacy ``Start Acq`` does the same on the way in. Getting it wrong on a
real camera is a failed arm at best, and a file that never saw its last
frames at worst.

The same order serves a recorder that reads the buffer (the uEye route,
thesis §3.4.3): it opens before the first frame can be published, and closes
only after the last one has been.
"""

import uuid
from pathlib import Path

import pytest

from stacscope.adapters.simulated import SimulatedFrameCapture, VirtualClock
from stacscope.core.actors import Actor, Fault, Rejected, Subscribe, ask, spawn
from stacscope.core.devices import ImageStreaming, Recording, SoftwareTimed, TimedDevice
from stacscope.core.protocol import DeviceReady, DeviceStopped, StartDevice, StopDevice

MiB = 1 << 20
SHAPE = (4, 6)


class LoggingCapture(SimulatedFrameCapture):
    """A capture that logs what the role asks of it. ``stop`` and ``detach``
    log only when they change something, so the stop inside ``detach`` and
    the detach inside ``close`` do not show up as second calls. A stop told
    to fail logs that it failed, and fails once."""

    def __init__(self, clock, log):
        super().__init__(clock, shape=SHAPE)
        self.log = log
        self.stop_fails = False
        """Fail the next stop, once, as a driver that will not stop would."""
        self.start_fails = False
        """Fail every start, before capture begins, as a driver that
        rejects the start would."""

    async def attach(self, slots):
        await super().attach(slots)
        self.log.append("attach")

    async def start(self):
        if self.start_fails:
            self.log.append("start failed")
            raise RuntimeError("the capture would not start")
        await super().start()
        self.log.append("start")

    async def stop(self):
        if self.stop_fails:
            self.stop_fails = False
            self.log.append("stop failed")
            raise RuntimeError("the capture would not stop")
        if self._capturing:
            self.log.append("stop")
        await super().stop()

    async def detach(self):
        if self._slots is not None:
            self.log.append("detach")
        await super().detach()

    async def close(self):
        self.log.append("capture closed")
        await super().close()


class CameraHardware(TimedDevice):
    """The adapter's own half, below every role: what arming the camera means."""

    def __init__(self):
        super().__init__()
        self.buffer_name = "t" + uuid.uuid4().hex[:20]
        self.capacity_bytes = MiB
        self.max_slots = 16
        self.n_slots = 8
        self.log = []
        self.disarm_fails = False

    async def setup(self):
        self.capture = LoggingCapture(self.clock, self.log)
        await super().setup()

    async def arm(self, root_filename, cycles):
        self.log.append("arm")

    async def disarm(self):
        self.log.append("disarm")
        if self.disarm_fails:
            raise RuntimeError("the camera would not disarm")


class DriverRecording(Recording):
    """A recorder the driver runs, as DCIMG is. It notes how many frames
    were published when its file closed, which is what a recorder reading
    the buffer would have seen."""

    save_directory = Path("D:/data")
    file_suffix = ".dcimg"
    open_fails = False
    published_at_close = None

    async def open_recording(self, path):
        self.log.append("open file")
        if self.open_fails:
            raise OSError("the disk is full")

    async def close_recording(self):
        self.log.append("close file")
        self.published_at_close = self.writer.published()


class Flash(ImageStreaming, DriverRecording, CameraHardware):
    """The Flash's composition: the role, then the recorder, then the camera."""


class ArmErrorProbe(Flash):
    """The Flash, keeping the exception its arm raised. The protocol reports
    only its ``repr``, which leaves out any notes on it."""

    arm_error = None

    async def arm(self, root_filename, cycles):
        try:
            await super().arm(root_filename, cycles)
        except Exception as exc:
            self.arm_error = exc
            raise


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)


class Rig:
    def __init__(self, kind):
        self.clock = VirtualClock()
        self.parent = Actor()
        spawn(self.parent, clock=self.clock, name="coordinator")
        self.device = kind()
        self.ref = self.parent.spawn_child(self.device, name="camera")
        self.listener = Listener()
        self.ref.tell(Subscribe(spawn(self.listener, clock=self.clock)))

    async def ask(self, msg):
        return await ask(self.ref, msg, timeout=1.0, clock=self.clock)

    def faults(self):
        return [m for m in self.listener.heard if isinstance(m, Fault)]


@pytest.fixture
async def rig():
    rigs = []

    def make(kind=Flash):
        rigs.append(Rig(kind))
        return rigs[-1]

    yield make
    for r in rigs:
        if not r.device.stopped.is_set():
            r.device.stop()
            r.ref.tell(StopDevice())
            await r.device.stopped.wait()
        r.listener.stop()
        r.parent.stop()


async def test_the_flash_opens_its_file_between_attach_and_start(rig):
    """DCAM refuses to bind a recorder before the buffer is attached, and a
    recorder bound after capture starts records nothing. ImageStreaming
    attaches before everything listed after it and starts after it, so
    listing Recording after ImageStreaming puts the open between the two."""
    r = rig()

    assert await r.ask(StartDevice("run-01", 4)) == DeviceReady()

    assert r.device.log == ["attach", "arm", "open file", "start"]


async def test_the_flash_closes_its_file_after_capture_stops_and_before_detach(rig):
    """The sample's order on the way out: stop, close the file, release the
    buffer. Until the file is closed, nothing says the driver has finished
    with the frames it was recording, so the memory is given back last, after
    the camera below has disarmed too: disarm is arm, mirrored."""
    r = rig()
    await r.ask(StartDevice("run-01", 4))
    r.device.log.clear()

    assert await r.ask(StopDevice()) == DeviceStopped()

    assert r.device.log == ["stop", "close file", "disarm", "detach"]


async def test_the_file_closes_only_after_the_last_frame_is_published(rig):
    """A recorder that reads the buffer would lose whatever the stop's final
    pass publishes, if it closed first. This one reads the count as it
    closes: the frame delivered just before the stop must be in it."""
    r = rig()
    await r.ask(StartDevice("run-01", 4))
    r.device.capture._triggered()  # delivered; the publisher has not yet run

    await r.ask(StopDevice())

    assert r.device.published_at_close == 1


def test_listing_the_recorder_before_image_streaming_is_refused():
    """Listed first, the recorder would open its file after capture starts
    and close it before capture stops: on the Flash, a DCIMG that misses its
    first and last frames, or an arm DCAM refuses. Nothing about that order is
    visible until an acquisition runs, so it is refused where it is written,
    when the class is defined (user, 2026-09-24)."""
    with pytest.raises(TypeError, match="Recording.*after.*ImageStreaming"):

        class RecorderFirst(DriverRecording, ImageStreaming, CameraHardware):
            pass


def test_listing_software_timed_after_image_streaming_is_refused():
    """A routine on the clock may be what triggers the frames, so it must
    stop before capture does, which means listing it first."""
    with pytest.raises(TypeError, match="ImageStreaming.*after.*SoftwareTimed"):

        class RoutineInside(ImageStreaming, SoftwareTimed, CameraHardware):
            pass


def test_a_role_listed_after_the_adapter_is_refused():
    """The chain ends at TimedDevice, whose arm and disarm call nothing
    further, so a role that falls below it in the bases is never called at
    all: a recorder that never records, silently."""
    with pytest.raises(TypeError, match="Recording.*before.*TimedDevice"):

        class RecorderLast(CameraHardware, Recording):
            pass


def test_every_role_in_order_is_accepted_and_so_is_a_subclass_of_a_device():
    """The documented order, and a device class derived from a composed one:
    its own class comes first in the bases, which is not a role and is
    allowed anywhere above them."""

    class Everything(SoftwareTimed, ImageStreaming, Recording, CameraHardware):
        pass

    class Derived(Everything):
        pass

    assert Derived.__mro__.index(SoftwareTimed) < Derived.__mro__.index(Recording)


async def test_a_file_that_will_not_open_leaves_nothing_armed(rig):
    """A failed arm is never followed by a disarm, so each role undoes its own
    part: Recording disarms the camera it armed, ImageStreaming takes back the
    slots it attached. The device is refused, fatally, and can arm again."""
    r = rig()
    r.device.open_fails = True

    assert isinstance(await r.ask(StartDevice("run-01", 4)), Rejected)
    await r.clock.advance(0)

    assert r.device.log == ["attach", "arm", "open file", "disarm", "detach"]
    [fault] = r.faults()
    assert fault.fatal and "disk is full" in fault.reason

    r.device.open_fails = False
    assert await r.ask(StartDevice("run-02", 4)) == DeviceReady()


async def test_a_capture_that_will_not_start_leaves_nothing_armed(rig):
    """The start is the last step of the arm, so by then the camera is armed
    and the file is open. A failed arm is never followed by a disarm, so
    ImageStreaming disarms what armed after it before giving the slots back,
    in the disarm's own order: close the file, disarm the camera, detach.
    Left alone, the Flash would keep a DCIMG file open and the camera
    armed with nothing to ever disarm them."""
    r = rig()
    await r.device.started.wait()  # the capture is built in setup
    r.device.capture.start_fails = True

    assert isinstance(await r.ask(StartDevice("run-01", 4)), Rejected)
    await r.clock.advance(0)

    assert r.device.log == [
        "attach", "arm", "open file", "start failed",
        "close file", "disarm", "detach",
    ]  # fmt: skip
    [fault] = r.faults()
    assert fault.fatal and "would not start" in fault.reason

    r.device.capture.start_fails = False
    assert await r.ask(StartDevice("run-02", 4)) == DeviceReady()


async def test_when_the_rollback_also_fails_the_start_is_the_error_and_the_slots_go_back(rig):
    """If the camera then fails to disarm, the arm still fails with the
    start's error, the first failure, and the disarm's is a note on it, as
    in ImageStreaming's own disarm (user, 2026-09-24). The slots are given
    back anyway: the capture never started, so no driver is writing into
    them."""
    r = rig(ArmErrorProbe)
    await r.device.started.wait()
    r.device.capture.start_fails = True
    r.device.disarm_fails = True

    assert isinstance(await r.ask(StartDevice("run-01", 4)), Rejected)

    assert r.device.log[-2:] == ["disarm", "detach"]
    error = r.device.arm_error
    assert "would not start" in str(error)
    assert any("would not disarm" in note for note in getattr(error, "__notes__", []))


async def test_the_frame_memory_is_given_back_even_if_the_camera_fails_to_disarm(rig):
    """A camera that fails to disarm kills the device, and closing the capture
    would detach in the end. But the slots are given back as part of the
    disarm that failed, not left to whatever runs next."""
    r = rig()
    await r.ask(StartDevice("run-01", 4))
    r.device.disarm_fails = True
    r.device.log.clear()

    r.ref.tell(StopDevice())
    await r.device.stopped.wait()

    assert r.device.log == ["stop", "close file", "disarm", "detach", "capture closed"]
    assert isinstance(r.device.exit_error, RuntimeError), "and the device died of it"


async def test_a_capture_that_will_not_stop_still_closes_the_file_and_disarms_the_camera(rig):
    """The cost of listing ImageStreaming first: the file now closes inside
    its disarm, after the step most likely to fail on real hardware. If
    ``stop`` failing ended the disarm there, the file would never be closed,
    since the device is already idle when it dies and teardown does not
    disarm again, and the camera would never reach its safe state. So the
    rest of the chain disarms anyway. The slots are not detached: the driver
    may still be writing into them, and closing the capture is what releases
    them, with the segment left mapped if that fails too. The device dies of
    the stop's error, since its hardware's state is now unknown."""
    r = rig()
    await r.ask(StartDevice("run-01", 4))
    r.device.capture.stop_fails = True
    r.device.log.clear()

    r.ref.tell(StopDevice())
    await r.device.stopped.wait()

    assert r.device.log[:4] == ["stop failed", "close file", "disarm", "capture closed"]
    assert isinstance(r.device.exit_error, RuntimeError)
    assert "would not stop" in str(r.device.exit_error)


async def test_when_the_camera_also_fails_to_disarm_the_stop_is_the_error_and_both_are_told(rig):
    """The first failure is the one the device dies of; the second is kept
    as a note on it, so the supervisor hears of both."""
    r = rig()
    await r.ask(StartDevice("run-01", 4))
    r.device.capture.stop_fails = True
    r.device.disarm_fails = True

    r.ref.tell(StopDevice())
    await r.device.stopped.wait()

    error = r.device.exit_error
    assert "would not stop" in str(error)
    assert any("would not disarm" in note for note in getattr(error, "__notes__", []))
