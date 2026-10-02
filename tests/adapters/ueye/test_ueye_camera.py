"""E1-E7: the UEyeCamera device over the fake pyueye, beyond the
ImageStreaming role contract (test_role_contract.py): its settings messages,
the shortest trigger period, the settings its config applies at launch,
opening the camera without stalling the actor runtime, and recording.

The capture takes frames in a thread, so this runs on a real clock, and
every step allows ``GRACE`` for a thread to hand its result back.

Not part of the default run:

    uv run pytest tests/adapters/ueye
"""

import asyncio
import pathlib
import sys
import threading
import uuid

import pytest
from fake_ueye import (
    IS_CM_SENSOR_RAW8,
    IS_CM_SENSOR_RAW10,
    IS_SET_TRIGGER_HI_LO,
    PIXEL_CLOCKS,
    FakeUEye,
)

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))

from image_streaming_contract import Listener  # noqa: E402

from stacscope.adapters.ueye import (  # noqa: E402
    AOI,
    CameraSettings,
    Exposure,
    FrameRate,
    GetCameraSettings,
    PixelClock,
    PixelFormat,
    SetAOI,
    SetExposure,
    SetFrameRate,
    SetPixelClock,
    SetPixelFormat,
    SetTrigger,
    Trigger,
    UEyeCamera,
)
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
from stacscope.core.protocol import (  # noqa: E402
    MESSAGES,
    DeviceReady,
    DeviceStopped,
    GetShortestTriggerPeriod,
    ShortestTriggerPeriod,
    StartDevice,
    StopDevice,
)
from stacscope.core.transport import Codec  # noqa: E402

GRACE = 0.25
"""Seconds allowed for a thread to hand its result back."""

SMALL = (0, 0, 64, 32)
"""The AOI every launch starts at, unless a test says otherwise: frames of a
few kilobytes."""


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
        options.setdefault("aoi", SMALL)
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


# --- E1: each setting by its own message, answered with what the camera took --------------


async def test_e1_each_setting_is_answered_with_what_the_camera_applied(launch, fake):
    """One message per setting (user, decision 3), each answered with the
    value the camera took, which may differ from the one asked for: the
    exposure is rounded to the camera's step, and the frame rate is bounded
    by the pixel clock and AOI."""
    _, ref, _ = await launch(fake)

    assert await _ask(ref, SetPixelClock(40)) == PixelClock(40)
    assert fake.pixel_clock == 40
    assert await _ask(ref, SetExposure(1.234)) == Exposure(pytest.approx(1.23))
    reply = await _ask(ref, SetFrameRate(1e6))
    assert reply == FrameRate(pytest.approx(1 / fake.min_frame_time()))
    assert await _ask(ref, SetTrigger("falling", 250)) == Trigger("falling", 250)
    assert (fake.trigger_mode, fake.trigger_delay_us) == (IS_SET_TRIGGER_HI_LO, 250)
    assert await _ask(ref, SetAOI(2, 4, 128, 64)) == AOI(2, 4, 128, 64)
    assert await _ask(ref, SetPixelFormat("RAW10")) == PixelFormat("RAW10")
    assert fake.color_mode == IS_CM_SENSOR_RAW10


async def test_e1_the_settings_are_read_back_together(launch, fake):
    """A client that did not set them, or set one that moved another (a
    new pixel clock can shorten the longest exposure), reads them all at
    once. The pixel clocks the camera offers come with them, since only
    those can be set."""
    _, ref, _ = await launch(fake, pixel_clock=40, exposure_ms=5.0)

    settings = await _ask(ref, GetCameraSettings())

    assert settings == CameraSettings(
        pixel_format="RAW8",
        aoi=SMALL,
        pixel_clock_mhz=40,
        pixel_clocks_mhz=tuple(sorted(PIXEL_CLOCKS)),
        frame_rate_fps=pytest.approx(fake.fps),
        exposure_ms=pytest.approx(5.0),
        trigger_edge="rising",
        trigger_delay_us=0,
    )


# --- E2: refusals ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("msg", "reason"),
    [
        (SetPixelFormat("RAW12"), "RAW8 or RAW10"),
        (SetTrigger("software", 0), "rising or falling"),
        (SetAOI(1, 0, 64, 32), "IS_INVALID_PARAMETER"),
        (SetPixelClock(41), "IS_INVALID_PARAMETER"),
        (SetExposure(5000.0), "IS_INVALID_PARAMETER"),
    ],
    ids=["pixel format", "trigger", "aoi", "pixel clock", "exposure"],
)
async def test_e2_a_setting_refused_is_rejected_with_the_reason_and_the_device_carries_on(
    launch, fake, msg, reason
):
    """A refusal is the requester's to handle, not a fault: the camera is as
    it was, and the device goes on answering."""
    _, ref, listener = await launch(fake)

    reply = await _ask(ref, msg)

    assert isinstance(reply, Rejected)
    assert reason in reply.reason
    assert _faults(listener) == []
    assert await _ask(ref, SetExposure(2.0)) == Exposure(pytest.approx(2.0))


@pytest.mark.parametrize("msg", [SetAOI(0, 0, 32, 16), SetPixelFormat("RAW10")], ids=["aoi", "pixel format"])
async def test_e2_while_armed_a_change_to_the_frame_is_refused(launch, fake, msg):
    """The slots are laid out for the frame the camera had at the arm, and the
    driver was told each memory's size then; a new frame would overrun them.
    Refused, not faulted: nothing has changed."""
    _, ref, listener = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()

    reply = await _ask(ref, msg)

    assert isinstance(reply, Rejected)
    assert "attached" in reply.reason
    assert (fake.aoi_rect, fake.color_mode) == (SMALL, IS_CM_SENSOR_RAW8)
    assert _faults(listener) == []
    assert await _ask(ref, StopDevice()) == DeviceStopped()


async def test_e2_while_armed_the_exposure_changes_at_once(launch, fake):
    """The exposure does not change the frame, so it may change mid-
    acquisition, as the Flash's does."""
    _, ref, _ = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()

    assert await _ask(ref, SetExposure(3.0)) == Exposure(pytest.approx(3.0))

    assert fake.exposure_ms == pytest.approx(3.0)
    assert await _ask(ref, StopDevice()) == DeviceStopped()


# --- E3: the shortest trigger period ---------------------------------------------------------


async def test_e3_the_shortest_trigger_period_is_asked_for_on_its_own(launch, fake):
    """A trigger the camera is too busy for is ignored (Y9), and costs the
    acquisition a frame. A client checks its waveform against this before
    it starts (user, 2026-09-30: a query of its own, not an answer to each
    setting). It moves with the settings."""
    _, ref, _ = await launch(fake, pixel_clock=40, exposure_ms=10.0)

    first = await _ask(ref, GetShortestTriggerPeriod())
    await _ask(ref, SetTrigger("rising", 5000))
    second = await _ask(ref, GetShortestTriggerPeriod())

    assert first == ShortestTriggerPeriod(pytest.approx(fake.min_frame_time() + 10e-3))
    assert second == ShortestTriggerPeriod(pytest.approx(first.seconds + 5e-3))


async def test_e3_the_shortest_trigger_period_is_answered_while_armed(launch, fake):
    _, ref, _ = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()

    assert isinstance(await _ask(ref, GetShortestTriggerPeriod()), ShortestTriggerPeriod)

    assert await _ask(ref, StopDevice()) == DeviceStopped()


# --- E4: settings from config, at launch ------------------------------------------------------


async def test_e4_settings_from_config_are_applied_in_the_cameras_order_at_launch(launch, fake):
    """The exposure is bounded by the frame rate, which is bounded by the
    pixel clock and AOI, so they go in that order: AOI, pixel clock, frame
    rate, exposure, then the trigger. An exposure set before a slower frame
    rate would be cut short by it."""
    device, _, _ = await launch(
        fake,
        pixel_format="RAW10",
        aoi=(0, 0, 128, 64),
        pixel_clock=10,
        frame_rate_fps=5.0,
        exposure_ms=150.0,
        trigger_edge="falling",
        trigger_delay_us=100,
    )

    assert device.started.is_set()
    order = [c for c in fake.calls if c in ("is_AOI", "is_PixelClock", "is_SetFrameRate", "is_Exposure")]
    firsts = [order.index(c) for c in ("is_AOI", "is_PixelClock", "is_SetFrameRate", "is_Exposure")]
    assert firsts == sorted(firsts)
    assert fake.color_mode == IS_CM_SENSOR_RAW10
    assert fake.aoi_rect == (0, 0, 128, 64)
    assert fake.pixel_clock == 10
    assert fake.exposure_ms == pytest.approx(150.0)
    assert (fake.trigger_mode, fake.trigger_delay_us) == (IS_SET_TRIGGER_HI_LO, 100)


async def test_e4_a_setting_from_config_that_is_refused_fails_the_launch_and_exits_the_camera(launch, fake):
    """A camera that launched with settings other than its config says would
    take an acquisition the user did not configure. And one left initialised
    refuses every later open until this process exits."""
    device, _, _ = await launch(fake, pixel_clock=41)

    assert device.stopped.is_set()
    assert not device.started.is_set()
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


# --- E6: the messages cross a process boundary ------------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        SetPixelFormat("RAW10"),
        PixelFormat("RAW10"),
        SetAOI(0, 384, 1280, 256),
        AOI(0, 384, 1280, 256),
        SetPixelClock(40),
        PixelClock(40),
        SetFrameRate(20.0),
        FrameRate(20.0),
        SetExposure(10.0),
        Exposure(10.0),
        SetTrigger("rising", 0),
        Trigger("rising", 0),
        GetCameraSettings(),
        CameraSettings("RAW8", (0, 0, 1280, 1024), 40, (7, 40, 86), 15.0, 10.0, "rising", 0),
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
