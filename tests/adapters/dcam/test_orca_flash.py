"""O1-O12 and DP21-DP24: the OrcaFlash device over the fake DCAM library,
beyond the ImageStreaming role contract (test_role_contract.py) and the
property contract (test_property_contract.py): its table of the camera's own
properties, the settings its config applies at launch, recording, and
opening DCAM without stalling the actor runtime.

The capture runs DCAM's blocking calls in threads, so this runs on a real
clock, and every step allows ``GRACE`` for a thread to hand its result back.

Windows only, like the adapter, and not part of the default run:

    uv run pytest tests/adapters/dcam
"""
# For end use and not for commercial purposes.

import asyncio
import importlib
import pathlib
import shutil
import sys
import threading
import uuid

import pytest
from fake_dcam import (
    ERR_INVALIDPARAM,
    EXPOSURE_RANGE,
    EXPOSURE_STEP,
    RECORD_PER_FILE,
    RECORD_PER_SESSION,
    SENSOR_TEMPERATURE,
    SUBARRAY_STEP,
    FakeDcam,
    error_text,
)

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))

from image_streaming_contract import Listener  # noqa: E402

from stacscope.adapters.dcam import OrcaFlash  # noqa: E402
from stacscope.adapters.dcam import camera as camera_module  # noqa: E402
from stacscope.core.actors import (  # noqa: E402
    Actor,
    Fault,
    Heartbeat,
    HeartbeatReply,
    Rejected,
    Subscribe,
    ask,
    spawn,
)
from stacscope.core.buffer import ImageBuffer  # noqa: E402
from stacscope.core.devices import PropertyRefused  # noqa: E402
from stacscope.core.protocol import (  # noqa: E402
    MESSAGES,
    SaveDirectory,
    SetSaveDirectory,
    DeviceReady,
    DeviceStopped,
    GetProperties,
    GetProperty,
    GetShortestTriggerPeriod,
    PropertiesChanged,
    Property,
    PropertyAccess,
    PropertyKind,
    SetProperty,
    ShortestTriggerPeriod,
    StartDevice,
    StopDevice,
)
from stacscope.core.transport import Codec  # noqa: E402

NUMBER, LABEL = PropertyKind.NUMBER, PropertyKind.LABEL
IDLE, ALWAYS, READ_ONLY = PropertyAccess.IDLE, PropertyAccess.ALWAYS, PropertyAccess.READ_ONLY

GRACE = 0.25
"""Seconds allowed for a DCAM wait's thread to return and its caller to run."""

SHAPE = (24, 40)  # the fake's default image


@pytest.fixture
def fake():
    return FakeDcam()


@pytest.fixture(autouse=True)
def roomy_disk(monkeypatch):
    """Every disk reports 4 TiB, 3 free, so no test depends on the disk it
    runs on; the O9 tests set their own."""
    usage = shutil._ntuple_diskusage(4 << 40, 1 << 40, 3 << 40)
    monkeypatch.setattr(camera_module, "disk_usage", lambda path: usage)


@pytest.fixture
async def launch():
    """``launch(fake, **options)`` spawns an OrcaFlash over ``fake`` with a
    listener subscribed, and returns the device, its ref and the listener
    once its setup has ended, however it ended. Each is stopped after the
    test, and its fake checked for misuse."""
    parent = Actor()
    spawn(parent, name="coordinator")
    launched = []

    async def launch_(fake, *, wait=True, **options):
        options.setdefault("n_slots", 6)
        device = OrcaFlash(
            "o" + uuid.uuid4().hex[:20],
            library=fake,
            capacity_bytes=1 << 20,
            max_slots=16,
            **options,
        )
        ref = parent.spawn_child(device, name=f"camera{len(launched)}")
        listener = Listener()
        # A child reports to its parent, and the root publishes: listen there.
        parent.ref.tell(Subscribe(spawn(listener)))
        launched.append((device, listener, fake))
        if wait:
            await _either(device.started.wait(), device.stopped.wait())
        return device, ref, listener

    yield launch_
    for device, listener, fake in launched:
        if not device.stopped.is_set():
            device.stop()
            await device.stopped.wait()
        listener.stop()
        assert fake.misuse == []
    parent.stop()


async def _either(*waits) -> None:
    tasks = [asyncio.ensure_future(w) for w in waits]
    await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    for task in tasks:
        task.cancel()


async def _ask(ref, msg):
    return await ask(ref, msg, timeout=2.0)


async def _trigger(fake, n: int) -> None:
    fake.pulse(n)
    fake.settle()
    await asyncio.sleep(GRACE)


def _faults(listener) -> list[Fault]:
    return [m for m in listener.heard if isinstance(m, Fault)]


# --- O1, DP21: the camera's own properties, as a table ----------------------------------------


async def test_dp21_the_table_lists_every_property_the_camera_has_by_its_own_names(launch, fake):
    """Found by name at run time, never from the header's numbers (the
    licence; the fake numbers them differently), so a client sees what this
    camera has, whatever the model."""
    _, ref, _ = await launch(fake)

    table = (await _ask(ref, GetProperties())).properties

    assert list(table) == list(fake._properties)


async def test_dp21_each_property_carries_dcams_limits_step_unit_and_access(launch, fake):
    _, ref, _ = await launch(fake)

    table = (await _ask(ref, GetProperties())).properties

    assert table["EXPOSURE TIME"] == Property(
        "EXPOSURE TIME", NUMBER, 0.01, "s", *EXPOSURE_RANGE, EXPOSURE_STEP, access=ALWAYS
    )
    assert table["SUBARRAY VSIZE"] == Property(
        "SUBARRAY VSIZE", NUMBER, float(SHAPE[0]), "", SUBARRAY_STEP, SHAPE[0], SUBARRAY_STEP,
        access=IDLE,
    )  # fmt: skip
    assert table["SENSOR MODE"] == Property(
        "SENSOR MODE", LABEL, "AREA", labels=("AREA", "PROGRESSIVE"), access=IDLE
    )
    assert table["IMAGE WIDTH"].access is READ_ONLY
    assert table["SENSOR TEMPERATURE"] == Property(
        "SENSOR TEMPERATURE", NUMBER, SENSOR_TEMPERATURE, "°C", access=READ_ONLY
    )


async def test_dp21_a_step_of_zero_is_no_step(launch, fake):
    """A step of 0 would tell a client that no value can follow another,
    and a page would draw a control that cannot move."""
    fake.zero_step.add("EXPOSURE TIME")
    _, ref, _ = await launch(fake)

    assert (await _ask(ref, GetProperty("EXPOSURE TIME"))).step is None


async def test_dp21_a_property_dcam_cannot_read_is_not_listed(launch, fake):
    """It has no value to show. Decided once, from DCAM's attributes, so the
    names stay the same for the device's lifetime."""
    fake._properties["SENSOR TEMPERATURE"].readable = False
    _, ref, _ = await launch(fake)

    table = (await _ask(ref, GetProperties())).properties

    assert "SENSOR TEMPERATURE" not in table and "EXPOSURE TIME" in table


async def test_o1_a_set_is_answered_with_what_the_camera_applied(launch, fake):
    """The camera rounds a value to one it can do (exposure to whole line
    intervals), so the answer is what was applied, not what was asked for:
    a client recording the request would misstate its exposure."""
    _, ref, _ = await launch(fake)

    answer = await _ask(ref, SetProperty("EXPOSURE TIME", 0.012345))

    assert isinstance(answer, PropertiesChanged)
    assert answer.properties["EXPOSURE TIME"].value == pytest.approx(0.0123)
    assert fake._value("EXPOSURE TIME") == answer.properties["EXPOSURE TIME"].value


async def test_o1_a_mode_is_set_and_read_by_its_label(launch, fake):
    """A mode is chosen by the camera's own label, since the numbers behind
    the labels are the header's (the licence keeps them out of this
    repository), and the fake numbers them differently from it."""
    _, ref, _ = await launch(fake)

    answer = await _ask(ref, SetProperty("SENSOR MODE", "PROGRESSIVE"))

    assert answer.properties["SENSOR MODE"].value == "PROGRESSIVE"
    assert fake.label("SENSOR MODE") == "PROGRESSIVE"
    assert (await _ask(ref, GetProperty("TRIGGER SOURCE"))).value == "EXTERNAL"


async def test_o1_a_set_answers_what_else_it_moved(launch, fake):
    """A smaller subarray is a smaller frame: the camera's sizes move with
    it, and a client sees them move without asking."""
    _, ref, _ = await launch(fake)

    answer = await _ask(ref, SetProperty("SUBARRAY VSIZE", 12))

    assert {"SUBARRAY VSIZE", "IMAGE HEIGHT", "BUFFER FRAMEBYTES"} <= set(answer.properties)
    assert "EXPOSURE TIME" not in answer.properties


# --- O2: refusals are answers, not faults --------------------------------------------------


@pytest.mark.parametrize(
    "request_, reason",
    [
        (SetProperty("FAN SPEED", 1.0), "'FAN SPEED'"),
        (SetProperty("SENSOR MODE", "SIDEWAYS"), "AREA, PROGRESSIVE"),
        (SetProperty("IMAGE WIDTH", 20.0), "read-only"),
        (SetProperty("EXPOSURE TIME", 20.0), "at most 10"),
        (GetProperty("FAN SPEED"), "'FAN SPEED'"),
    ],
    ids=["no such property", "no such label", "read-only", "out of range", "no such property to read"],
)
async def test_o2_a_setting_the_camera_refuses_is_rejected_and_the_device_carries_on(
    launch, fake, request_, reason
):
    """A mistyped name, or a setting the camera will not take, is the
    client's to correct: answered ``Rejected``, and nothing else. A fault
    here would abort an acquisition over a typo, and a device that stopped
    answering would need a relaunch."""
    _, ref, listener = await launch(fake)

    answer = await _ask(ref, request_)

    assert isinstance(answer, Rejected)
    assert reason in answer.reason
    assert "dcamprop_setgetvalue" not in fake.calls
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
    assert _faults(listener) == []
    assert await _ask(ref, StopDevice()) == DeviceStopped()


async def test_o2_a_value_dcam_refuses_is_rejected_in_dcams_words(launch, fake):
    _, ref, listener = await launch(fake)
    fake.fail("dcamprop_setgetvalue", ERR_INVALIDPARAM)

    answer = await _ask(ref, SetProperty("EXPOSURE TIME", 0.02))

    assert isinstance(answer, Rejected) and error_text(ERR_INVALIDPARAM) in answer.reason
    assert _faults(listener) == []
    assert isinstance(await _ask(ref, SetProperty("EXPOSURE TIME", 0.02)), PropertiesChanged)


# --- O3, DP23: while armed, only what leaves the frame format alone ---------------------------


async def test_o3_while_armed_a_setting_that_leaves_the_format_alone_is_applied(launch, fake):
    """Exposure can change mid-acquisition if the camera takes it (user,
    2026-09-25): the frames keep coming, and keep the layout the buffer
    declared at arm."""
    device, ref, listener = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
    reader = ImageBuffer.open(device.buffer_name)
    try:
        await _trigger(fake, 1)

        answer = await _ask(ref, SetProperty("EXPOSURE TIME", 0.02))
        await _trigger(fake, 1)

        assert answer.properties["EXPOSURE TIME"].value == pytest.approx(0.02)
        assert reader.published() - reader.acquisition_start == 2
        assert _faults(listener) == []
    finally:
        reader.close()
    assert await _ask(ref, StopDevice()) == DeviceStopped()


async def test_o3_while_armed_a_setting_dcam_allows_only_when_idle_is_refused(launch, fake):
    """DCAM says which properties it takes while capturing; the rest wait
    for the device to be idle, and are refused before DCAM is asked. The
    acquisition goes on, and the change can be made once the device is
    idle, where it reaches the next acquisition's layout."""
    device, ref, listener = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()

    answer = await _ask(ref, SetProperty("SUBARRAY VSIZE", 12))
    await _trigger(fake, 1)

    assert isinstance(answer, Rejected) and "idle" in answer.reason
    assert "dcamprop_setgetvalue" not in fake.calls
    assert _faults(listener) == []
    assert device.writer.published() - device.writer.acquisition_start == 1
    assert await _ask(ref, StopDevice()) == DeviceStopped()

    assert isinstance(await _ask(ref, SetProperty("SUBARRAY VSIZE", 12)), PropertiesChanged)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
    reader = ImageBuffer.open(device.buffer_name)
    try:
        assert reader.frames.shape[1:] == (12, SHAPE[1])
    finally:
        reader.close()
    assert await _ask(ref, StopDevice()) == DeviceStopped()


async def test_dp23_while_armed_a_reshape_the_driver_lets_through_is_undone_and_fatal(launch, fake):
    """Nothing but DCAM's word keeps a larger frame out of slots laid out
    for a smaller one, and its attributes are a camera's own account. So the
    device checks the frame format after every change while armed. A change
    that moved it is put back and answered ``Rejected``, and the acquisition
    is ended by a fatal fault: frames written in between may not be what the
    header says."""
    fake.refused_while_attached.clear()
    fake.writable_while_capturing.add("SUBARRAY VSIZE")
    _, ref, listener = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()

    answer = await _ask(ref, SetProperty("SUBARRAY VSIZE", 12))
    await asyncio.sleep(GRACE)  # the fault is published after the answer

    assert isinstance(answer, Rejected)
    assert "frame format" in answer.reason
    (fault,) = _faults(listener)
    assert fault.fatal
    assert fake._value("SUBARRAY VSIZE") == SHAPE[0]
    assert fake._value("IMAGE HEIGHT") == SHAPE[0]
    assert await _ask(ref, StopDevice()) == DeviceStopped()


async def test_dp23_while_armed_a_change_that_leaves_a_format_that_cannot_be_described_is_undone(
    launch, fake, monkeypatch
):
    """A pixel type STACScope cannot describe (MONO12P, packed) makes the
    format unreadable rather than different. That is a change all the
    same, and the worst kind: put back, and fatal."""
    fake.refused_while_attached.clear()
    fake.writable_while_capturing.add("SUBARRAY VSIZE")
    device, ref, listener = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
    describe = device.capture.frame_format

    def after_the_change():
        if fake._value("SUBARRAY VSIZE") != SHAPE[0]:
            raise ValueError("BUFFER PIXEL TYPE is MONO12P, which STACScope cannot describe")
        return describe()

    monkeypatch.setattr(device.capture, "frame_format", after_the_change)

    answer = await _ask(ref, SetProperty("SUBARRAY VSIZE", 12))
    await asyncio.sleep(GRACE)

    assert isinstance(answer, Rejected)
    (fault,) = _faults(listener)
    assert fault.fatal
    assert fake._value("SUBARRAY VSIZE") == SHAPE[0]
    assert await _ask(ref, StopDevice()) == DeviceStopped()


# --- O4: settings from config, at launch ----------------------------------------------------


async def test_o4_settings_from_config_are_applied_in_order_at_launch(launch, fake):
    """DCAM settings depend on order (a subarray's mode before its sizes),
    so config gives a list, applied as written: the last of two writes to
    one setting is the one that stays. The first acquisition has their
    layout."""
    device, ref, _ = await launch(
        fake,
        settings=[
            ("EXPOSURE TIME", 0.02),
            ("SENSOR MODE", "PROGRESSIVE"),
            ("SUBARRAY VSIZE", 12),
            ("EXPOSURE TIME", 0.03),
        ],
    )
    assert device.started.is_set()

    assert fake._value("EXPOSURE TIME") == pytest.approx(0.03)
    assert fake.label("SENSOR MODE") == "PROGRESSIVE"
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
    reader = ImageBuffer.open(device.buffer_name)
    try:
        assert reader.frames.shape[1:] == (12, SHAPE[1])
    finally:
        reader.close()
    assert await _ask(ref, StopDevice()) == DeviceStopped()


async def test_o4_a_setting_config_gives_that_is_refused_fails_the_launch_and_closes_the_camera(
    launch, fake
):
    """A device that launched with a setting silently missing would image
    wrong without a word. The launch fails naming the setting, and the
    camera is closed on the way out: DCAM's one session per process would
    otherwise refuse the relaunch."""
    device, _, _ = await launch(fake, settings=[("SENSOR MODE", "SIDEWAYS")])

    assert not device.started.is_set()
    assert isinstance(device.exit_error, PropertyRefused)
    assert "SENSOR MODE" in str(device.exit_error) and "SIDEWAYS" in str(device.exit_error)
    assert "dcamapi_uninit" in fake.calls
    assert not fake._initialized


# --- O5: the Flash's declared in-flight depth ---------------------------------------------


async def test_o5_the_buffer_declares_two_frames_in_flight(launch, fake):
    """Measured 1 on the dev box Flash in all nine conditions (M1), and
    declared with a margin of 1 (user, 2026-09-25). A consumer skips slots
    within this depth of the newest frame, so declaring the bare measurement
    leaves nothing for a condition never measured."""
    device, ref, _ = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
    reader = ImageBuffer.open(device.buffer_name)
    try:
        assert reader._layout.in_flight == 2
    finally:
        reader.close()
    assert await _ask(ref, StopDevice()) == DeviceStopped()


# --- O6: opening DCAM does not stall the runtime -------------------------------------------


class _HeldInit:
    """Holds ``dcamapi_init`` until ``gate`` is set, as DCAM starting up
    holds it for seconds on the dev box. ``entered`` is set once a call is
    held. ``timed_out`` records a hold that nobody released within ``hold``
    seconds, which is what an open that blocks the event loop looks like
    from here: the test that would release it cannot run."""

    def __init__(self, fake, hold: float = 2.0) -> None:
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.timed_out = False
        init = fake.dcamapi_init

        def held(param):
            self.entered.set()
            if not self.gate.wait(hold):
                self.timed_out = True
            return init(param)

        fake.dcamapi_init = held


async def test_o6_heartbeats_are_answered_while_dcam_opens(launch, fake):
    """A camera in a process of its own must answer heartbeats (a decision of
    Milestone 4), and DCAM takes seconds to start. The runtime answers a
    heartbeat on receipt, but only while its event loop runs: opened on the
    loop, DCAM's start-up is a silence that reads as a hung process, and the
    engine declares the camera dead before it has finished opening."""
    held = _HeldInit(fake)
    device, ref, _ = await launch(fake, wait=False)
    try:
        assert await asyncio.to_thread(held.entered.wait, 5.0), "the open is under way"
        assert await ask(ref, Heartbeat(), timeout=1.0) == HeartbeatReply()
        assert not device.started.is_set()
    finally:
        held.gate.set()
    await device.started.wait()

    assert not held.timed_out, "the open held the event loop until its hold ran out"


async def test_o6_a_device_cancelled_while_dcam_opens_still_closes_the_camera(launch, fake):
    """The thread opening DCAM cannot be cancelled. If the setup waiting on
    it is (the engine's event loop shutting down), the camera it opens must
    still be closed by the device's teardown, or DCAM stays initialized
    under a device that is gone."""
    held = _HeldInit(fake)
    device, _, _ = await launch(fake, wait=False)
    assert await asyncio.to_thread(held.entered.wait, 5.0), "the open is under way"

    device._task.cancel()
    held.gate.set()
    await device.stopped.wait()

    assert "dcamapi_uninit" in fake.calls
    assert not fake._initialized


# --- O7, DP24: the core's messages, and none of the adapter's own --------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        SetProperty("EXPOSURE TIME", 0.01),
        SetProperty("SENSOR MODE", "PROGRESSIVE"),
        GetProperty("TRIGGER SOURCE"),
        Property("TRIGGER SOURCE", LABEL, "EXTERNAL", labels=("INTERNAL", "EXTERNAL")),
        SetSaveDirectory("E:/data"),
        SaveDirectory("E:/data"),
    ],
    ids=lambda m: type(m).__name__,
)
def test_o7_the_settings_messages_cross_a_process_boundary(msg):
    """The camera runs in a process of its own (DCAM allows one session per
    process), so every settings message crosses the codec both ways."""
    codec = Codec(MESSAGES)

    assert codec.decode(codec.encode(msg)) == msg


def test_dp24_the_adapter_has_no_messages_of_its_own():
    """A client configures the camera through the core's property messages,
    and imports nothing of the adapter's."""
    import stacscope.adapters.dcam as dcam

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("stacscope.adapters.dcam.messages")
    assert not {"SetProperty", "SetPropertyLabel", "GetProperty", "PropertyValue"} & set(dir(dcam))


# --- O8: recording an acquisition to DCIMG ---------------------------------------------------


def _published(device) -> int:
    return device.writer.published() - device.writer.acquisition_start


async def test_o8_an_acquisition_with_a_stem_is_recorded_to_the_devices_own_file(launch, fake, tmp_path):
    """The Coordinator's stem, joined to the device's directory and name,
    is where the driver's recorder writes: every frame published is in
    the file, and the file is closed with the acquisition."""
    device, ref, listener = await launch(fake, save_directory=tmp_path)

    assert await _ask(ref, StartDevice("run-01", -1)) == DeviceReady()
    await _trigger(fake, 3)
    assert await _ask(ref, StopDevice()) == DeviceStopped()
    await asyncio.sleep(GRACE)

    [file] = fake.recordings
    assert file.path == str(tmp_path / "run-01_camera0.dcimg")
    assert file.closed and len(file.frames) == 3 == _published(device)
    assert _faults(listener) == []


async def test_o8_an_acquisition_without_a_stem_records_nothing(launch, fake, tmp_path):
    _, ref, listener = await launch(fake, save_directory=tmp_path)

    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
    await _trigger(fake, 2)
    assert await _ask(ref, StopDevice()) == DeviceStopped()

    assert fake.recordings == []
    assert not any(call.startswith("dcamrec") for call in fake.calls)
    assert _faults(listener) == []


async def test_o8_each_acquisition_gets_its_own_file(launch, fake, tmp_path):
    """dcamcap_record binds a file to one capture only (the reference).
    Each file is held to its own acquisition's frames, not the buffer's
    running count."""
    _, ref, listener = await launch(fake, save_directory=tmp_path)

    for stem, frames in [("run-01", 2), ("run-02", 3)]:
        assert await _ask(ref, StartDevice(stem, -1)) == DeviceReady()
        await _trigger(fake, frames)
        assert await _ask(ref, StopDevice()) == DeviceStopped()

    assert [(pathlib.Path(f.path).name, len(f.frames)) for f in fake.recordings] == [
        ("run-01_camera0.dcimg", 2),
        ("run-02_camera0.dcimg", 3),
    ]
    await asyncio.sleep(GRACE)
    assert _faults(listener) == []


async def test_o8_a_device_that_dies_recording_closes_its_file(launch, fake, tmp_path):
    """The reference: a file not closed may lose data. A device stopped
    while armed closes the file before DCAM is uninitialized (the fake
    records the reverse as misuse, which the fixture checks)."""
    device, ref, _ = await launch(fake, save_directory=tmp_path)
    assert await _ask(ref, StartDevice("run-01", -1)) == DeviceReady()
    await _trigger(fake, 2)

    device.stop()
    await device.stopped.wait()

    assert fake.open_recordings == []
    assert len(fake.recordings[0].frames) == 2


# --- O9: how many frames a file may hold ------------------------------------------------------

GIB = 1 << 30


@pytest.fixture
def disk(monkeypatch):
    """``disk(free, total)`` makes every disk report these bytes."""

    def set_(free: int, total: int) -> None:
        usage = shutil._ntuple_diskusage(total, total - free, free)
        monkeypatch.setattr(camera_module, "disk_usage", lambda path: usage)

    return set_


def _room(fake, frames: int) -> int:
    """Free bytes that leave room for exactly ``frames`` frames, beyond the
    headroom of a disk of 500 GiB (100 GiB, which is more than its 10%)."""
    per_frame = int(fake._value("RECORD FIXED BYTES PER FRAME"))
    return 100 * GIB + RECORD_PER_FILE + RECORD_PER_SESSION + frames * per_frame + per_frame - 1


async def _limit(ref, fake) -> int:
    stem = f"run-{len(fake.recordings):02d}"  # a file is never recorded over
    assert await _ask(ref, StartDevice(stem, 10)) == DeviceReady()
    limit = fake.recordings[-1].max_frames
    assert await _ask(ref, StopDevice()) == DeviceStopped()
    return limit


async def test_o9_a_file_may_fill_the_disk_but_its_headroom(launch, fake, tmp_path, disk):
    """A file's length is never known in advance (user, 2026-09-30: taking
    it from the trigger line's pulse count tied the camera to the
    waveforms). So a file may use the disk, less headroom for the file
    system and for anything else writing to the disk (user, 2026-09-25):
    the larger of 100 GiB, the legacy's constant, and 10%."""
    disk(_room(fake, 1000), 500 * GIB)
    _, ref, _ = await launch(fake, save_directory=tmp_path)

    assert await _limit(ref, fake) == 1000


async def test_o9_on_a_large_disk_the_headroom_is_a_tenth_of_it(launch, fake, tmp_path, disk):
    per_frame = int(fake._value("RECORD FIXED BYTES PER FRAME"))
    total = 4096 * GIB
    disk(total // 10 + RECORD_PER_FILE + RECORD_PER_SESSION + 1000 * per_frame + per_frame - 1, total)
    _, ref, _ = await launch(fake, save_directory=tmp_path)

    assert await _limit(ref, fake) == 1000


async def test_o9_config_can_lower_the_limit(launch, fake, tmp_path, disk):
    disk(_room(fake, 1000), 500 * GIB)
    _, ref, _ = await launch(fake, save_directory=tmp_path, max_record_frames=5)

    assert await _limit(ref, fake) == 5


async def test_o9_a_limit_beyond_what_dcam_can_count_is_dcams_largest(launch, fake, tmp_path, disk):
    disk(1 << 62, 1 << 62)
    _, ref, _ = await launch(fake, save_directory=tmp_path)

    assert await _limit(ref, fake) == 2**31 - 1


async def test_o9_a_disk_without_room_for_a_frame_refuses_the_acquisition(launch, fake, tmp_path, disk):
    """Recording nothing while the acquisition runs would look like a
    success. The arm is refused, naming the directory, and the camera is
    left as it was: memory detached, nothing capturing, no file open."""
    disk(_room(fake, 0) - int(fake._value("RECORD FIXED BYTES PER FRAME")), 500 * GIB)
    _, ref, listener = await launch(fake, save_directory=tmp_path)

    answer = await _ask(ref, StartDevice("run-01", -1))
    await asyncio.sleep(GRACE)

    assert isinstance(answer, Rejected)
    assert tmp_path.name in answer.reason and "no room" in answer.reason
    assert fake.recordings == []
    assert not fake.attached and not fake.capturing
    assert [f.fatal for f in _faults(listener)] == [True]


# --- O10: a file that falls short is fatal -----------------------------------------------------


async def test_o10_a_file_that_reached_its_limit_before_the_stop_is_a_fatal_fault(
    launch, fake, tmp_path
):
    """Streaming goes on after the recorder stops at its limit, so the
    acquisition looks whole while the file is not. Found at the stop,
    fatal (user, 2026-09-25), and the stop is still confirmed."""
    _, ref, listener = await launch(fake, save_directory=tmp_path, max_record_frames=2)
    assert await _ask(ref, StartDevice("run-01", -1)) == DeviceReady()
    await _trigger(fake, 3)

    assert await _ask(ref, StopDevice()) == DeviceStopped()
    await asyncio.sleep(GRACE)

    [fault] = _faults(listener)
    assert fault.fatal
    assert "2 of the 3" in fault.reason and "run-01_camera0.dcimg" in fault.reason


async def test_o10_frames_the_recorder_missed_are_a_fatal_fault(launch, fake, tmp_path):
    _, ref, listener = await launch(fake, save_directory=tmp_path)
    assert await _ask(ref, StartDevice("run-01", -1)) == DeviceReady()
    fake.miss_recording(1)
    await _trigger(fake, 3)

    assert await _ask(ref, StopDevice()) == DeviceStopped()
    await asyncio.sleep(GRACE)

    [fault] = _faults(listener)
    assert fault.fatal and "missed 1" in fault.reason


async def test_o10_a_recorder_still_writing_at_the_close_is_a_fatal_fault(
    launch, fake, tmp_path, monkeypatch
):
    """A file closed under a recorder that had not finished may be missing
    its end, whatever its count said."""
    monkeypatch.setattr(camera_module, "RECORDER_FINISH", 0.2)
    fake.recorder_lag = -1
    _, ref, listener = await launch(fake, save_directory=tmp_path)
    assert await _ask(ref, StartDevice("run-01", -1)) == DeviceReady()
    await _trigger(fake, 2)

    assert await _ask(ref, StopDevice()) == DeviceStopped()
    await asyncio.sleep(GRACE)

    [fault] = _faults(listener)
    assert fault.fatal and "still writing" in fault.reason
    assert fake.open_recordings == []


# --- O11: where the files go ---------------------------------------------------------------------


async def test_o11_the_save_directory_is_set_by_message(launch, fake, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _, ref, _ = await launch(fake, save_directory=tmp_path)

    assert await _ask(ref, SetSaveDirectory(str(other))) == SaveDirectory(str(other))
    assert await _ask(ref, StartDevice("run-01", -1)) == DeviceReady()

    assert fake.recordings[0].path == str(other / "run-01_camera0.dcimg")
    assert await _ask(ref, StopDevice()) == DeviceStopped()


async def test_o11_a_directory_that_does_not_exist_is_refused(launch, fake, tmp_path):
    """Refused rather than created: a mistyped path would otherwise put an
    acquisition somewhere nobody looks."""
    _, ref, _ = await launch(fake, save_directory=tmp_path)

    answer = await _ask(ref, SetSaveDirectory(str(tmp_path / "typo")))

    assert isinstance(answer, Rejected) and "typo" in answer.reason
    assert not (tmp_path / "typo").exists()
    assert await _ask(ref, StartDevice("run-01", -1)) == DeviceReady()
    assert fake.recordings[0].path == str(tmp_path / "run-01_camera0.dcimg")
    assert await _ask(ref, StopDevice()) == DeviceStopped()


async def test_o11_the_directory_does_not_change_during_an_acquisition(launch, fake, tmp_path):
    other = tmp_path / "other"
    other.mkdir()
    _, ref, _ = await launch(fake, save_directory=tmp_path)
    assert await _ask(ref, StartDevice("run-01", -1)) == DeviceReady()

    assert isinstance(await _ask(ref, SetSaveDirectory(str(other))), Rejected)

    assert await _ask(ref, StopDevice()) == DeviceStopped()
    assert await _ask(ref, SetSaveDirectory(str(other))) == SaveDirectory(str(other))


async def test_o11_a_stem_with_no_directory_set_refuses_the_acquisition(launch, fake):
    """No directory is no place to record, not the working directory."""
    _, ref, _ = await launch(fake)

    answer = await _ask(ref, StartDevice("run-01", -1))

    assert isinstance(answer, Rejected) and "save directory" in answer.reason
    assert fake.recordings == [] and not fake.attached
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady(), "streaming alone still works"
    assert await _ask(ref, StopDevice()) == DeviceStopped()


async def test_o11_an_existing_file_is_never_overwritten(launch, fake, tmp_path):
    """A repeated stem would otherwise replace an acquisition already
    taken. The arm is refused and the file left as it was."""
    existing = tmp_path / "run-01_camera0.dcimg"
    existing.write_bytes(b"an earlier acquisition")
    _, ref, _ = await launch(fake, save_directory=tmp_path)

    answer = await _ask(ref, StartDevice("run-01", -1))

    assert isinstance(answer, Rejected) and "run-01_camera0.dcimg" in answer.reason
    assert existing.read_bytes() == b"an earlier acquisition"
    assert fake.recordings == [] and not fake.attached


async def test_o11_the_directory_from_config_is_checked_at_launch(launch, fake, tmp_path):
    device, _, _ = await launch(fake, save_directory=tmp_path / "typo")

    assert not device.started.is_set()
    assert "typo" in str(device.exit_error)
    assert not fake._initialized


# --- O12: the shortest trigger period ------------------------------------------------------


async def test_o12_the_shortest_trigger_period_is_the_cameras_own_floor(launch, fake):
    """A trigger the camera cannot take yet costs the acquisition a frame, so
    a client checks its waveform against this first. It is the same query
    the uEye answers (user, 2026-09-30), so a client asks every camera
    alike. DCAM states the floor itself, as TIMING MIN TRIGGER INTERVAL,
    and it moves with the exposure and readout, so it is read each time."""
    _, ref, _ = await launch(fake)

    first = await _ask(ref, GetShortestTriggerPeriod())
    fake.set_property("TIMING MIN TRIGGER INTERVAL", 0.035)
    second = await _ask(ref, GetShortestTriggerPeriod())

    assert first == ShortestTriggerPeriod(0.02)
    assert second == ShortestTriggerPeriod(0.035)


async def test_o12_a_camera_that_does_not_state_its_floor_says_so(launch, fake):
    fake.remove_property("TIMING MIN TRIGGER INTERVAL")
    _, ref, listener = await launch(fake)

    answer = await _ask(ref, GetShortestTriggerPeriod())

    assert isinstance(answer, Rejected)
    assert "TIMING MIN TRIGGER INTERVAL" in answer.reason
    assert _faults(listener) == []
