"""O1-O7: the OrcaFlash device over the fake DCAM library, beyond the
ImageStreaming role contract (test_role_contract.py): its own settings
messages, the settings its config applies at launch, and opening DCAM
without stalling the actor runtime.

The capture runs DCAM's blocking calls in threads, so this runs on a real
clock, and every step allows ``GRACE`` for a thread to hand its result back.

Windows only, like the adapter, and not part of the default run:

    uv run pytest tests/adapters/dcam
"""
# For end use and not for commercial purposes.

import asyncio
import pathlib
import sys
import threading
import uuid

import pytest
from fake_dcam import ERR_ACCESSDENY, ERR_NOTWRITABLE, FakeDcam, error_text

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))

from image_streaming_contract import Listener  # noqa: E402

from stacscope.adapters.dcam import (  # noqa: E402
    GetProperty,
    OrcaFlash,
    PropertyValue,
    SetProperty,
    SetPropertyLabel,
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
    StartDevice,
    StopDevice,
)
from stacscope.core.transport import Codec  # noqa: E402

GRACE = 0.25
"""Seconds allowed for a DCAM wait's thread to return and its caller to run."""

SHAPE = (24, 40)  # the fake's default image


@pytest.fixture
def fake():
    return FakeDcam()


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
        ref.tell(Subscribe(spawn(listener)))
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


# --- O1: settings by the camera's own names and labels -------------------------------------


async def test_o1_a_setting_is_set_by_name_and_answered_with_what_the_camera_applied(launch, fake):
    """The camera rounds a value to one it can do (exposure to whole line
    intervals), so the answer is what was applied, not what was asked for:
    a client recording the request would misstate its exposure."""
    _, ref, _ = await launch(fake)

    answer = await _ask(ref, SetProperty("EXPOSURE TIME", 0.012345))

    assert answer == PropertyValue("EXPOSURE TIME", pytest.approx(0.0123), None)
    assert fake._value("EXPOSURE TIME") == answer.value


async def test_o1_a_setting_is_set_by_label_and_answered_with_its_label_and_number(launch, fake):
    """A mode is chosen by the camera's own label, since the numbers behind
    the labels are the header's (the licence keeps them out of this
    repository), and the fake numbers them differently from it."""
    _, ref, _ = await launch(fake)

    answer = await _ask(ref, SetPropertyLabel("SENSOR MODE", "PROGRESSIVE"))

    assert answer == PropertyValue("SENSOR MODE", 32.0, "PROGRESSIVE")
    assert fake.label("SENSOR MODE") == "PROGRESSIVE"


@pytest.mark.parametrize(
    "name, expected",
    [
        ("TRIGGER SOURCE", PropertyValue("TRIGGER SOURCE", 52.0, "EXTERNAL")),
        ("EXPOSURE TIME", PropertyValue("EXPOSURE TIME", 0.01, None)),
    ],
    ids=["labelled", "numeric"],
)
async def test_o1_a_setting_is_read_with_its_label_if_it_has_one(launch, fake, name, expected):
    """A client showing a mode wants its label; one computing with a value
    wants the number. Every answer carries both where both exist, so a
    client never has to ask twice or know which kind a property is."""
    _, ref, _ = await launch(fake)

    assert await _ask(ref, GetProperty(name)) == expected


# --- O2: refusals are answers, not faults --------------------------------------------------


@pytest.mark.parametrize(
    "request_, reason",
    [
        (SetProperty("FAN SPEED", 1.0), "no FAN SPEED property"),
        (SetPropertyLabel("SENSOR MODE", "SIDEWAYS"), "AREA, PROGRESSIVE"),
        (SetProperty("IMAGE WIDTH", 20.0), error_text(ERR_NOTWRITABLE)),
        (GetProperty("FAN SPEED"), "no FAN SPEED property"),
    ],
    ids=["no such property", "no such label", "dcam refuses", "no such property to read"],
)
async def test_o2_a_setting_the_camera_refuses_is_rejected_and_the_device_carries_on(
    launch, fake, request_, reason
):
    """A mistyped name, or a setting the camera will not take, is the
    client's to correct: answered ``Rejected``, with the camera's own words
    where it gave some, and nothing else. A fault here would abort an
    acquisition over a typo, and a device that stopped answering would need
    a relaunch."""
    _, ref, listener = await launch(fake)

    answer = await _ask(ref, request_)

    assert isinstance(answer, Rejected)
    assert reason in answer.reason
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
    assert _faults(listener) == []
    assert await _ask(ref, StopDevice()) == DeviceStopped()


# --- O3: while armed, only what leaves the frame format alone ---------------------------------


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

        assert answer == PropertyValue("EXPOSURE TIME", pytest.approx(0.02), None)
        assert reader.published() - reader.acquisition_start == 2
        assert _faults(listener) == []
    finally:
        reader.close()
    assert await _ask(ref, StopDevice()) == DeviceStopped()


async def test_o3_while_armed_a_setting_that_reshapes_the_frame_is_refused(launch, fake):
    """The buffer's layout is fixed at arm; a frame of another size would
    not fit its slots. DCAM refuses such a change while memory is attached,
    and the refusal is the client's answer. The acquisition goes on, and
    the change can be made once the device is idle, where it reaches the
    next acquisition's layout."""
    device, ref, listener = await launch(fake)
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()

    answer = await _ask(ref, SetProperty("SUBARRAY VSIZE", 12))
    await _trigger(fake, 1)

    assert isinstance(answer, Rejected)
    assert error_text(ERR_ACCESSDENY) in answer.reason
    assert _faults(listener) == []
    assert device.writer.published() - device.writer.acquisition_start == 1
    assert await _ask(ref, StopDevice()) == DeviceStopped()

    assert await _ask(ref, SetProperty("SUBARRAY VSIZE", 12)) == PropertyValue(
        "SUBARRAY VSIZE", 12.0, None
    )
    assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
    reader = ImageBuffer.open(device.buffer_name)
    try:
        assert reader.frames.shape[1:] == (12, SHAPE[1])
    finally:
        reader.close()
    assert await _ask(ref, StopDevice()) == DeviceStopped()


async def test_o3_while_armed_a_reshape_the_driver_lets_through_is_undone_and_fatal(launch, fake):
    """Nothing but DCAM's refusal keeps a larger frame out of slots laid
    out for a smaller one, and the reference does not list which settings
    it refuses. So the device checks the frame format after every change
    while armed. A change that moved it is put back and answered
    ``Rejected``, and the acquisition is ended by a fatal fault: frames
    written in between may not be what the header says."""
    fake.refused_while_attached.clear()
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


async def test_o3_while_armed_a_change_that_leaves_a_format_that_cannot_be_described_is_undone(
    launch, fake, monkeypatch
):
    """A pixel type STACScope cannot describe (MONO12P, packed) makes the
    format unreadable rather than different. That is a change all the
    same, and the worst kind: put back, and fatal."""
    fake.refused_while_attached.clear()
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
    assert isinstance(device.exit_error, ValueError)
    assert any("SENSOR MODE" in note for note in device.exit_error.__notes__)
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


# --- O7: the messages cross a process boundary -------------------------------------------


@pytest.mark.parametrize(
    "msg",
    [
        SetProperty("EXPOSURE TIME", 0.01),
        SetPropertyLabel("SENSOR MODE", "PROGRESSIVE"),
        GetProperty("TRIGGER SOURCE"),
        PropertyValue("TRIGGER SOURCE", 52.0, "EXTERNAL"),
        PropertyValue("EXPOSURE TIME", 0.01, None),
    ],
    ids=lambda m: type(m).__name__,
)
def test_o7_the_settings_messages_cross_a_process_boundary(msg):
    """The camera runs in a process of its own (DCAM allows one session per
    process), so every settings message crosses the codec both ways."""
    codec = Codec(MESSAGES)

    assert codec.decode(codec.encode(msg)) == msg
