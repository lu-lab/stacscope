"""E1-E7 and DP25-DP28: the UEyeCamera device over the fake pyueye, beyond
the ImageStreaming role contract (test_role_contract.py) and the property
contract (test_property_contract.py): its table of properties, the shortest
trigger period, the settings its config applies at launch, opening the
camera without stalling the actor runtime, and recording.

The capture takes frames in a thread, so this runs on a real clock, and
every step allows ``GRACE`` for a thread to hand its result back.

Not part of the default run:

    uv run pytest tests/adapters/ueye
"""

import asyncio
import importlib
import pathlib
import sys
import threading
import uuid

import pytest
from fake_ueye import (
    EXPOSURE_STEP_MS,
    IS_CM_SENSOR_RAW8,
    IS_CM_SENSOR_RAW10,
    IS_SET_TRIGGER_HI_LO,
    IS_SET_TRIGGER_LO_HI,
    PIXEL_CLOCKS,
    SENSOR,
    FakeUEye,
)

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))

from image_streaming_contract import Listener  # noqa: E402

from stacscope.adapters.ueye import UEyeCamera  # noqa: E402
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
    DeviceReady,
    DeviceStopped,
    GetProperties,
    GetShortestTriggerPeriod,
    PropertiesChanged,
    PropertiesRefused,
    Property,
    PropertyAccess,
    PropertyKind,
    SetProperties,
    SetProperty,
    ShortestTriggerPeriod,
    StartDevice,
    StopDevice,
)
from stacscope.core.transport import Codec  # noqa: E402

GRACE = 0.25
"""Seconds allowed for a thread to hand its result back."""

SMALL = (0, 0, 64, 32)
SMALL_SETTINGS = [("aoi_width", 64.0), ("aoi_height", 32.0)]
"""The AOI every launch starts at, unless a test says otherwise: frames of a
few kilobytes."""

NUMBER, LABEL = PropertyKind.NUMBER, PropertyKind.LABEL
IDLE = PropertyAccess.IDLE


@pytest.fixture
def fake():
    return FakeUEye()


@pytest.fixture
async def launch():
    """``launch(fake, **options)`` spawns a UEyeCamera over ``fake`` with a
    listener subscribed, and returns the device, its ref and the listener
    once its setup has ended, however it ended. Each is stopped after the
    test, and its fake checked for misuse."""
    parent = Actor()
    spawn(parent, name="coordinator")
    launched = []

    async def launch_(fake, *, wait=True, **options):
        options.setdefault("n_slots", 6)
        options.setdefault("settings", SMALL_SETTINGS)
        device = UEyeCamera(
            "e" + uuid.uuid4().hex[:20],
            ueye=fake,
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


def _faults(listener) -> list[Fault]:
    return [m for m in listener.heard if isinstance(m, Fault)]


# --- E1, DP25: the camera's properties, in SI units, with the driver's limits -------------------


async def test_dp25_the_table_lists_the_ten_properties_in_si_units_with_the_drivers_limits(launch, fake):
    """In the order each bounds the next (the pixel format and AOI bound
    the pixel clock's frame time, which bounds the frame rate, which bounds
    the exposure), so a client setting several in turn sets them in an
    order that works. Seconds and hertz, not the driver's milliseconds,
    microseconds and megahertz."""
    _, ref, _ = await launch(fake)

    table = (await _ask(ref, GetProperties())).properties

    w, h = SMALL[2:]
    clocks = tuple(float(c * 1_000_000) for c in sorted(PIXEL_CLOCKS))
    assert table == {
        "pixel_format": Property("pixel_format", LABEL, "RAW8", labels=("RAW8", "RAW10"), access=IDLE),
        "aoi_x": Property("aoi_x", NUMBER, 0.0, "px", 0.0, SENSOR[0] - w, 2.0, access=IDLE),
        "aoi_y": Property("aoi_y", NUMBER, 0.0, "px", 0.0, SENSOR[1] - h, 2.0, access=IDLE),
        "aoi_width": Property("aoi_width", NUMBER, w, "px", 16.0, SENSOR[0], 4.0, access=IDLE),
        "aoi_height": Property("aoi_height", NUMBER, h, "px", 4.0, SENSOR[1], 2.0, access=IDLE),
        "pixel_clock": Property("pixel_clock", NUMBER, 24e6, "Hz", choices=clocks),
        "frame_rate": Property(
            "frame_rate", NUMBER, pytest.approx(fake.fps), "Hz", 0.5, pytest.approx(1 / fake.min_frame_time())
        ),
        "exposure": Property(
            "exposure", NUMBER, pytest.approx(fake.exposure_ms / 1e3), "s",
            pytest.approx(EXPOSURE_STEP_MS / 1e3), pytest.approx(1 / fake.fps),
            pytest.approx(EXPOSURE_STEP_MS / 1e3),
        ),
        "trigger_edge": Property("trigger_edge", LABEL, "rising", labels=("rising", "falling")),
        "trigger_delay": Property("trigger_delay", NUMBER, 0.0, "s", 0.0, 4.0, 1e-6),
    }  # fmt: skip


async def test_e1_a_set_is_answered_with_what_the_camera_applied(launch, fake):
    """The value the camera took may differ from the one asked for: the
    exposure is rounded to the camera's step."""
    _, ref, _ = await launch(fake)

    answer = await _ask(ref, SetProperty("exposure", 0.001234))

    assert answer.properties["exposure"].value == pytest.approx(0.00123)
    assert fake.exposure_ms == pytest.approx(1.23)


async def test_e1_each_property_reaches_the_camera_in_the_drivers_units(launch, fake):
    _, ref, _ = await launch(fake)

    for name, value in [
        ("pixel_clock", 40e6),
        ("trigger_edge", "falling"),
        ("trigger_delay", 250e-6),
        ("aoi_x", 2.0),
        ("aoi_y", 4.0),
        ("pixel_format", "RAW10"),
    ]:
        assert isinstance(await _ask(ref, SetProperty(name, value)), PropertiesChanged), name

    assert fake.pixel_clock == 40
    assert (fake.trigger_mode, fake.trigger_delay_us) == (IS_SET_TRIGGER_HI_LO, 250)
    assert fake.aoi_rect == (2, 4, *SMALL[2:])
    assert fake.color_mode == IS_CM_SENSOR_RAW10


# --- DP26: a set answers the limits it moved ---------------------------------------------------


async def test_dp26_a_slower_pixel_clock_answers_the_frame_rates_and_exposures_new_limits(launch, fake):
    """A slower clock reads out slower: the fastest frame rate falls, the
    frame rate with it if it was faster, and so the longest exposure. A
    client holding the old limits would offer a frame rate the camera can
    no longer give."""
    _, ref, _ = await launch(fake, settings=[*SMALL_SETTINGS, ("frame_rate", 500.0)])
    before = (await _ask(ref, GetProperties())).properties

    answer = await _ask(ref, SetProperty("pixel_clock", 7e6))

    assert set(answer.properties) == {"pixel_clock", "frame_rate", "exposure"}
    rate = answer.properties["frame_rate"]
    assert rate.maximum < before["frame_rate"].maximum and rate.value == pytest.approx(rate.maximum)
    assert answer.properties["exposure"].maximum > before["exposure"].maximum


async def test_dp26_a_frame_rate_answers_the_exposures_new_maximum(launch, fake):
    _, ref, _ = await launch(fake)

    answer = await _ask(ref, SetProperty("frame_rate", 10.0))

    assert set(answer.properties) == {"frame_rate", "exposure"}
    assert answer.properties["exposure"].maximum == pytest.approx(0.1)


# --- DP27: the AOI, one side at a time ------------------------------------------------------------


async def test_dp27_setting_the_width_keeps_the_position_and_height(launch, fake):
    _, ref, _ = await launch(fake)
    await _ask(ref, SetProperty("aoi_x", 100.0))

    answer = await _ask(ref, SetProperty("aoi_width", 200.0))

    assert fake.aoi_rect == (100, 0, 200, SMALL[3])
    assert answer.properties["aoi_x"].maximum == SENSOR[0] - 200


async def test_dp27_a_width_past_the_sensors_edge_is_refused_before_anything_changes(launch, fake):
    """From x = 1200, 80 pixels reach the edge. The driver would refuse the
    rectangle too, but the table says so first, with the limit, which a
    client can show before it asks."""
    _, ref, _ = await launch(fake)
    await _ask(ref, SetProperty("aoi_x", 1200.0))
    sets = fake.calls.count("is_AOI")

    answer = await _ask(ref, SetProperty("aoi_width", 128.0))

    assert isinstance(answer, Rejected) and "at most 80" in answer.reason
    assert fake.aoi_rect == (1200, 0, *SMALL[2:])
    assert fake.calls.count("is_AOI") - sets < 6  # read, never set: no rectangle was sent


# --- DP34: moving the AOI right while narrowing it ---------------------------------------------


async def test_dp34_a_batch_moves_the_aoi_right_while_narrowing_it(launch, fake):
    """The case bulk writes exist for: from the full width, moving x right
    needs the width narrowed first, and narrowing then moving is two writes
    that leave an AOI between. As one batch the camera takes both in one
    call, and neither is refused."""
    _, ref, _ = await launch(fake, settings=[("aoi_height", 32.0)])
    sets = []
    original = fake.is_AOI

    def recording(hCam, nCommand, pParam, cbSizeOfParam):
        if getattr(nCommand, "value", nCommand) == fake.IS_AOI_IMAGE_SET_AOI:
            sets.append(tuple(getattr(pParam, f).value for f in ("s32X", "s32Y", "s32Width", "s32Height")))
        return original(hCam, nCommand, pParam, cbSizeOfParam)

    fake.is_AOI = recording

    reply = await _ask(ref, SetProperties({"aoi_x": 640.0, "aoi_width": 640.0}))

    assert isinstance(reply, PropertiesChanged), reply
    assert fake.aoi_rect == (640, 0, 640, 32)
    assert sets == [(640, 0, 640, 32)]


async def test_dp34_the_same_two_writes_sent_singly_x_first_have_the_first_refused(launch, fake):
    _, ref, _ = await launch(fake, settings=[("aoi_height", 32.0)])

    first = await _ask(ref, SetProperty("aoi_x", 640.0))
    second = await _ask(ref, SetProperty("aoi_width", 640.0))

    assert isinstance(first, Rejected) and "at most 0" in first.reason
    assert isinstance(second, PropertiesChanged)
    assert fake.aoi_rect == (0, 0, 640, 32)


async def test_dp34_a_batch_the_camera_refuses_names_the_property(launch, fake):
    """Taken whole, the batch's limits are the driver's to judge: off the
    camera's grid, the AOI is refused, and named."""
    _, ref, _ = await launch(fake)

    reply = await _ask(ref, SetProperties({"aoi_x": 1.0, "exposure": 0.002}))

    assert isinstance(reply, PropertiesRefused)
    assert reply.name == "aoi_x" and "IS_INVALID_PARAMETER" in reply.reason
    assert "exposure" not in reply.changed


# --- E2: refusals ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("name", "value", "reason"),
    [
        ("pixel_format", "RAW12", "RAW8, RAW10"),
        ("trigger_edge", "software", "rising, falling"),
        ("aoi_x", 1.0, "IS_INVALID_PARAMETER"),  # off the camera's grid: the driver says so
        ("pixel_clock", 41e6, "must be one of"),
        ("exposure", 5.0, "at most"),
    ],
    ids=["pixel format", "trigger", "aoi", "pixel clock", "exposure"],
)
async def test_e2_a_setting_refused_is_rejected_with_the_reason_and_the_device_carries_on(
    launch, fake, name, value, reason
):
    """A refusal is the requester's to handle, not a fault: the camera is as
    it was, and the device goes on answering."""
    _, ref, listener = await launch(fake)

    reply = await _ask(ref, SetProperty(name, value))

    assert isinstance(reply, Rejected)
    assert reason in reply.reason
    assert _faults(listener) == []
    assert isinstance(await _ask(ref, SetProperty("exposure", 0.002)), PropertiesChanged)


@pytest.mark.parametrize(
    ("name", "value"), [("aoi_width", 32.0), ("pixel_format", "RAW10")], ids=["aoi", "pixel format"]
)
async def test_e2_while_armed_a_change_to_the_frame_is_refused(launch, fake, name, value):
    """The slots are laid out for the frame the camera had at the arm, and the
    driver was told each memory's size then; a new frame would overrun them.
    Refused, not faulted: nothing has changed."""
    _, ref, listener = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()

    reply = await _ask(ref, SetProperty(name, value))

    assert isinstance(reply, Rejected)
    assert "idle" in reply.reason
    assert (fake.aoi_rect, fake.color_mode) == (SMALL, IS_CM_SENSOR_RAW8)
    assert _faults(listener) == []
    assert await _ask(ref, StopDevice()) == DeviceStopped()


async def test_e2_while_armed_the_exposure_changes_at_once(launch, fake):
    """The exposure does not change the frame, so it may change mid-
    acquisition, as the Flash's does."""
    _, ref, _ = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()

    answer = await _ask(ref, SetProperty("exposure", 0.003))

    assert answer.properties["exposure"].value == pytest.approx(0.003)
    assert fake.exposure_ms == pytest.approx(3.0)
    assert await _ask(ref, StopDevice()) == DeviceStopped()


# --- E3: the shortest trigger period ---------------------------------------------------------


async def test_e3_the_shortest_trigger_period_is_asked_for_on_its_own(launch, fake):
    """A trigger the camera is too busy for is ignored (Y9), and costs the
    acquisition a frame. A client checks its waveform against this before
    it starts (user, 2026-09-30: a query of its own, not an answer to each
    setting). It moves with the settings."""
    _, ref, _ = await launch(
        fake, settings=[*SMALL_SETTINGS, ("pixel_clock", 40e6), ("exposure", 10e-3)]
    )

    first = await _ask(ref, GetShortestTriggerPeriod())
    await _ask(ref, SetProperty("trigger_delay", 5e-3))
    second = await _ask(ref, GetShortestTriggerPeriod())

    assert first == ShortestTriggerPeriod(pytest.approx(fake.min_frame_time() + 10e-3))
    assert second == ShortestTriggerPeriod(pytest.approx(first.seconds + 5e-3))


async def test_e3_the_shortest_trigger_period_is_answered_while_armed(launch, fake):
    _, ref, _ = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()

    assert isinstance(await _ask(ref, GetShortestTriggerPeriod()), ShortestTriggerPeriod)

    assert await _ask(ref, StopDevice()) == DeviceStopped()


# --- E4, DP28: settings from config, at launch -------------------------------------------------


async def test_dp28_settings_from_config_are_applied_in_order_at_launch(launch, fake):
    """The exposure is bounded by the frame rate, which is bounded by the
    pixel clock and AOI, so config lists them in that order: an exposure
    set before a slower frame rate would be cut short by it."""
    device, _, _ = await launch(
        fake,
        pixel_format="RAW10",
        settings=[
            ("aoi_width", 128.0),
            ("aoi_height", 64.0),
            ("pixel_clock", 10e6),
            ("frame_rate", 5.0),
            ("exposure", 0.15),
            ("trigger_edge", "falling"),
            ("trigger_delay", 100e-6),
        ],
    )

    assert device.started.is_set()
    assert fake.color_mode == IS_CM_SENSOR_RAW10
    assert fake.aoi_rect == (0, 0, 128, 64)
    assert fake.pixel_clock == 10
    assert fake.exposure_ms == pytest.approx(150.0)
    assert (fake.trigger_mode, fake.trigger_delay_us) == (IS_SET_TRIGGER_HI_LO, 100)


async def test_dp28_without_trigger_settings_the_camera_waits_for_a_rising_edge(launch, fake):
    """The camera's own default is to run free (Y8); STACScope's, as
    before, is the trigger input's rising edge, with no delay."""
    device, _, _ = await launch(fake)

    assert device.started.is_set()
    assert (fake.trigger_mode, fake.trigger_delay_us) == (IS_SET_TRIGGER_LO_HI, 0)


async def test_dp28_the_old_launch_arguments_are_gone(fake):
    """Each is a setting now; a config that still gives one fails loudly
    rather than being ignored."""
    for old in ("aoi", "pixel_clock", "frame_rate_fps", "exposure_ms", "trigger_edge", "trigger_delay_us"):
        with pytest.raises(TypeError):
            UEyeCamera("e", n_slots=4, capacity_bytes=1 << 20, max_slots=4, ueye=fake, **{old: 1})


async def test_e4_a_setting_from_config_that_is_refused_fails_the_launch_and_exits_the_camera(launch, fake):
    """A camera that launched with settings other than its config says would
    take an acquisition the user did not configure. And one left initialised
    refuses every later open until this process exits."""
    device, _, _ = await launch(fake, settings=[("pixel_clock", 41e6)])

    assert device.stopped.is_set()
    assert not device.started.is_set()
    assert isinstance(device.exit_error, PropertyRefused) and "pixel_clock" in str(device.exit_error)
    assert not fake.is_open


async def test_e4_the_buffer_declares_two_frames_in_flight(launch, fake):
    """Measured 1 on the camera, from software and from the DAQ (Z1), and
    declared with a margin of 1, as the Flash is."""
    device, ref, _ = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
    reader = ImageBuffer.open(device.buffer_name)
    try:
        assert reader._layout.in_flight == 2
    finally:
        reader.close()
    assert await _ask(ref, StopDevice()) == DeviceStopped()


# --- E5: opening does not stall the runtime -----------------------------------------------------


class _HeldInit:
    """Holds ``is_InitCamera`` until ``gate`` is set. ``entered`` is set once
    a call is held; ``timed_out`` records a hold nobody released within
    ``hold`` seconds, which is what an open that blocks the event loop looks
    like from here."""

    def __init__(self, fake, hold: float = 2.0) -> None:
        self.gate = threading.Event()
        self.entered = threading.Event()
        self.timed_out = False
        init = fake.is_InitCamera

        def held(h, hwnd):
            self.entered.set()
            if not self.gate.wait(hold):
                self.timed_out = True
            return init(h, hwnd)

        fake.is_InitCamera = held


async def test_e5_heartbeats_are_answered_while_the_camera_opens(launch, fake):
    """A camera in a process of its own must answer heartbeats (Milestone 4),
    and an open waits up to seconds for a camera a crashed process held
    (Y7). Opened on the event loop, that wait reads as a hung process."""
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


async def test_e5_a_device_cancelled_while_the_camera_opens_still_exits_it(launch, fake):
    """The thread opening the camera cannot be cancelled. If the setup
    waiting on it is, the camera it opens must still be exited by the
    device's teardown, or the next process to open it is refused."""
    held = _HeldInit(fake)
    device, _, _ = await launch(fake, wait=False)
    assert await asyncio.to_thread(held.entered.wait, 5.0), "the open is under way"

    device._task.cancel()
    held.gate.set()
    await device.stopped.wait()

    assert "is_ExitCamera" in fake.calls
    assert not fake.is_open


# --- E6, DP28: the core's messages, and none of the adapter's own --------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        SetProperty("pixel_clock", 40e6),
        SetProperty("trigger_edge", "rising"),
        GetProperties(),
        GetShortestTriggerPeriod(),
        ShortestTriggerPeriod(0.0449),
    ],
    ids=lambda m: type(m).__name__,
)
def test_e6_the_messages_cross_a_process_boundary(msg):
    """The camera runs in a process of its own, where a vendor driver
    belongs, so every message crosses the codec both ways."""
    codec = Codec(MESSAGES)

    assert codec.decode(codec.encode(msg)) == msg


def test_dp28_the_adapter_has_no_messages_of_its_own():
    """A client configures the camera through the core's property messages,
    and imports nothing of the adapter's."""
    import stacscope.adapters.ueye as ueye

    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("stacscope.adapters.ueye.messages")
    assert not {"SetAOI", "SetExposure", "GetCameraSettings", "CameraSettings"} & set(dir(ueye))


# --- E7: recording, through a TIFF recorder reading the buffer (user, 2026-10-01) -----------


async def test_e7_an_acquisition_with_a_stem_is_recorded_to_tiff(launch, fake, tmp_path):
    """The uEye has no recorder of its own, so the device records its own
    buffer: every frame published, in order, with the camera's clock on
    each page. The directory is set by the core message, as for the Flash."""
    import numpy as np
    import tifffile

    from stacscope.core.protocol import SaveDirectory, SetSaveDirectory
    from stacscope.core.recording import DEVICE_TICKS_TAG

    device, ref, listener = await launch(fake)
    assert await _ask(ref, SetSaveDirectory(str(tmp_path))) == SaveDirectory(str(tmp_path))
    assert await _ask(ref, StartDevice("run-01", -1)) == DeviceReady()
    fake.pulse(4)
    await asyncio.sleep(GRACE)
    assert await _ask(ref, StopDevice()) == DeviceStopped()

    path = tmp_path / "run-01_camera0.tif"
    with tifffile.TiffFile(path) as tf:
        pages = np.stack([page.asarray() for page in tf.pages])
        ticks = [page.tags[DEVICE_TICKS_TAG].value for page in tf.pages]
        description = tf.pages[0].description
    reader = ImageBuffer.open(device.buffer_name)
    try:
        start = reader.acquisition_start
        assert reader.published() - start == 4
        slots = [reader.slot_of(start + i) for i in range(4)]
        np.testing.assert_array_equal(pages, np.stack([reader.frames[s] for s in slots]))
        assert ticks == [int(t) for t in reader.meta["device_ticks"][slots]]
    finally:
        reader.close()
    assert '"device_time_source": "CAMERA"' in description
    assert _faults(listener) == []


async def test_e7_a_save_directory_from_config_that_does_not_exist_fails_before_the_camera_opens(
    launch, fake, tmp_path
):
    """A typo in config is found at launch, not by the first acquisition
    that records, and without holding the camera open meanwhile."""
    device, _, _ = await launch(fake, save_directory=tmp_path / "typo")

    assert not device.started.is_set()
    assert "typo" in str(device.exit_error)
    assert not fake.is_open
