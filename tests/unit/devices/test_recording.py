"""D7 — one acquisition, one stem, one file per device.

The Coordinator issues a single root filename and no more: what a device does
with it is the device's own business, because only the device knows which
format is fastest for it (the Flash writes DCIMG through its driver).
The shared stem is what ties the files of one acquisition together.

An empty stem means do not record, and is not an error: it is how every
acquisition that is not being saved runs.
"""

from pathlib import Path

from stacscope.adapters.simulated import VirtualClock
from stacscope.core.actors import Actor, Fault, Subscribe, ask, spawn
from stacscope.core.devices import Recording, TimedDevice
from stacscope.core.protocol import DeviceReady, DeviceStopped, StartDevice, StopDevice

SAVE_DIR = Path("D:/data")


class Hardware(TimedDevice):
    """The adapter's own half: what preparing and releasing this device means.
    ``Recording`` goes in front of it, so a broken chain shows up here."""

    def __init__(self, close_fails=False):
        super().__init__()
        self.opened = []
        self.closed = 0
        self.calls = []
        self.close_fails = close_fails

    async def arm(self, root_filename, cycles):
        self.calls.append("arm")

    async def disarm(self):
        self.calls.append("disarm")


class FakeRecorder(Recording, Hardware):
    """A device whose recorder is a list of what it was asked to do."""

    save_directory = SAVE_DIR
    file_suffix = ".dcimg"

    async def open_recording(self, path):
        self.calls.append("open")
        self.opened.append(path)

    async def close_recording(self):
        self.calls.append("close")
        self.closed += 1
        if self.close_fails:
            raise OSError("the disk went away")


class Stamped(FakeRecorder):
    """A device that names its own file rather than taking the default."""

    def target(self, root_filename):
        default = super().target(root_filename)
        return None if default is None else default.with_name(f"{default.stem}-ch0.tif")


class Poison:
    pass


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)


class Rig:
    def __init__(self, cls=FakeRecorder, name="camera", **kwargs):
        self.clock = VirtualClock()
        self.parent = Actor()
        spawn(self.parent, clock=self.clock, name="coordinator")
        self.device = cls(**kwargs)
        self.ref = self.parent.spawn_child(self.device, name=name)
        self.listener = Listener()
        self.ref.tell(Subscribe(spawn(self.listener, clock=self.clock, name=f"{name}-listener")))

    def faults(self):
        return [m for m in self.listener.heard if isinstance(m, Fault)]

    async def ask(self, msg):
        return await ask(self.ref, msg, timeout=1.0, clock=self.clock)


async def test_the_shared_stem_becomes_this_devices_own_file():
    rig = Rig()

    assert await rig.ask(StartDevice("2026-09-19_run-01", 10)) == DeviceReady()
    assert rig.device.opened == [SAVE_DIR / "2026-09-19_run-01_camera.dcimg"]


async def test_devices_in_one_acquisition_share_the_stem_and_differ_by_name():
    camera = Rig(name="camera")
    brightfield = Rig(name="brightfield")

    await camera.ask(StartDevice("run-01", 10))
    await brightfield.ask(StartDevice("run-01", 10))

    assert camera.device.opened == [SAVE_DIR / "run-01_camera.dcimg"]
    assert brightfield.device.opened == [SAVE_DIR / "run-01_brightfield.dcimg"]


async def test_an_empty_stem_records_nothing_and_is_not_an_error():
    rig = Rig()

    assert await rig.ask(StartDevice("", 10)) == DeviceReady()
    assert rig.device.opened == []

    assert await rig.ask(StopDevice()) == DeviceStopped()
    assert rig.device.closed == 0, "nothing was opened, so nothing is closed"


async def test_stopping_closes_the_recording_exactly_once():
    rig = Rig()
    await rig.ask(StartDevice("run-01", 10))

    assert await rig.ask(StopDevice()) == DeviceStopped()
    assert rig.device.closed == 1

    await rig.ask(StopDevice())
    assert rig.device.closed == 1


async def test_a_device_that_dies_recording_still_closes_the_file():
    rig = Rig()
    await rig.ask(StartDevice("run-01", 10))

    rig.device.stop()  # the actor ends without a StopDevice
    rig.ref.tell(Poison())
    await rig.device.stopped.wait()

    assert rig.device.closed == 1, "an unclosed file may be an unreadable one"


async def test_the_next_acquisition_opens_the_next_file():
    rig = Rig()
    await rig.ask(StartDevice("run-01", 10))
    await rig.ask(StopDevice())
    await rig.ask(StartDevice("run-02", 10))

    assert rig.device.opened == [
        SAVE_DIR / "run-01_camera.dcimg",
        SAVE_DIR / "run-02_camera.dcimg",
    ]


async def test_a_device_may_name_its_own_file():
    rig = Rig(cls=Stamped)

    await rig.ask(StartDevice("run-01", 10))
    assert rig.device.opened == [SAVE_DIR / "run-01_camera-ch0.tif"]


async def test_a_device_that_names_nothing_records_nothing():
    rig = Rig(cls=Stamped)

    await rig.ask(StartDevice("", 10))
    assert rig.device.opened == []


async def test_the_device_is_prepared_before_the_recorder_starts():
    """The recorder attaches to a prepared device, and both are done before
    the first trigger can arrive."""
    rig = Rig()

    await rig.ask(StartDevice("run-01", 10))
    assert rig.device.calls == ["arm", "open"]

    await rig.ask(StopDevice())
    assert rig.device.calls == ["arm", "open", "close", "disarm"], "closed before safe state"


async def test_a_recorder_that_cannot_close_still_reaches_its_safe_state():
    """A full disk is not a reason to leave a shutter open."""
    rig = Rig(close_fails=True)
    await rig.ask(StartDevice("run-01", 10))

    assert await rig.ask(StopDevice()) == DeviceStopped()
    assert rig.device.calls == ["arm", "open", "close", "disarm"]
    assert not rig.device.stopped.is_set(), "a lost file is not a dead device"


async def test_a_file_that_will_not_close_is_reported_as_fatal():
    rig = Rig(close_fails=True)
    await rig.ask(StartDevice("run-01", 10))
    await rig.ask(StopDevice())
    await rig.clock.advance(0)

    [fault] = rig.faults()
    assert fault.fatal, "the data may be truncated; the user decides what follows"
    assert "disk went away" in fault.reason


async def test_a_failed_close_is_not_retried_on_the_way_out():
    rig = Rig(close_fails=True)
    await rig.ask(StartDevice("run-01", 10))
    await rig.ask(StopDevice())

    rig.device.stop()
    rig.ref.tell(Poison())
    await rig.device.stopped.wait()
    assert rig.device.closed == 1
