"""U2-U12: UEyeFrameCapture over the fake pyueye, beyond the port contract
(U1, test_contract.py): what only a uEye adapter has to get right.

Not part of the default run:

    uv run pytest tests/adapters/ueye
"""

import asyncio
import subprocess
import sys
import threading
import time
import uuid

import numpy as np
import pytest
from fake_ueye import (
    FRAME_TICKS,
    HANDLE,
    IS_CM_MONO8,
    IS_CM_SENSOR_RAW8,
    IS_CM_SENSOR_RAW10,
    IS_DEVICE_IN_USE,
    IS_INVALID_PARAMETER,
    IS_NO_SUCCESS,
    IS_SET_TRIGGER_HI_LO,
    IS_SET_TRIGGER_LO_HI,
    PIXEL_CLOCKS,
    TICKS_START,
    FakeUEye,
)

from stacscope.adapters.ueye import UEyeError, UEyeFrameCapture, UEyeUnavailable
from stacscope.adapters.ueye.capture import WAIT_SLICE
from stacscope.core.buffer import ImageBuffer, MetaField, TimeSource
from stacscope.core.devices.image_streaming import layout_for
from stacscope.core.ports import FramesLost

GRACE = 0.25
"""Seconds allowed for a queue wait's thread to return and its caller to run."""

N_SLOTS = 4

AOI = (0, 0, 64, 32)
"""A small AOI, so that slots are a few kilobytes."""


@pytest.fixture
def fake():
    fake = FakeUEye()
    yield fake
    assert fake.misuse == []


@pytest.fixture
async def open_capture(segments):
    """``open_capture(fake)`` opens a capture on it, at the small AOI; each is
    closed after the test, and its fake checked for misuse. Closed before any
    segment is: the role's order, since memory the capture still holds
    cannot close."""
    opened = []

    def open_(fake, **options):
        options.setdefault("in_flight", 1)
        capture = UEyeFrameCapture(1, ueye=fake, **options)
        opened.append((capture, fake))
        capture.set_aoi(*AOI)
        return capture

    yield open_
    for capture, fake in opened:
        await capture.close()
        assert fake.misuse == []


@pytest.fixture
def segments():
    """``segments(fmt)`` makes a segment laid out for ``fmt``; each is closed
    after the test."""
    made = []

    def make(fmt, n_slots=N_SLOTS):
        layout = layout_for(fmt, n_slots)
        writer = ImageBuffer.create(
            "u" + uuid.uuid4().hex[:20],
            capacity_bytes=layout.resolve().total_bytes,
            max_slots=n_slots,
        )
        writer.configure(layout)
        made.append(writer)
        return writer

    yield make
    for writer in made:
        writer.close()


async def capturing(capture, segments, n_slots=N_SLOTS):
    writer = segments(capture.frame_format(), n_slots)
    await capture.attach(writer.slots)
    await capture.start()
    return writer


async def triggered(fake, n):
    fake.pulse(n)
    fake.settle()
    await asyncio.sleep(GRACE)


# --- U2: no pyueye needed to import; opening -------------------------------------------


def test_u2_importing_the_adapter_needs_no_pyueye():
    """pyueye loads the uEye DLL when it is imported, and raises without it.
    The package must import on any machine, so a configuration can name the
    camera, and a test can pass the fake, without the driver installed."""
    code = (
        "import sys; sys.modules['pyueye'] = None\n"
        "import stacscope.adapters.ueye as m\n"
        "try:\n"
        "    m.UEyeFrameCapture(1, in_flight=1)\n"
        "except m.UEyeUnavailable as e:\n"
        "    print('unavailable:', e)\n"
    )
    result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=60)

    assert result.returncode == 0, result.stderr
    assert result.stdout.startswith("unavailable:")
    assert issubclass(UEyeUnavailable, RuntimeError)


async def test_u2_opening_sets_a_raw_pixel_format_and_the_rising_edge(fake):
    """The camera opens in 8-bit BGR, even though it is mono (Y8), which would
    make every frame three times its size and pass it through the LUT. The
    user chose RAW (decision 4). And the instrument triggers it on the DAQ's
    rising edge; freerun would give frames no waveform accounts for."""
    capture = UEyeFrameCapture(1, ueye=fake, in_flight=1)
    try:
        assert fake.color_mode == IS_CM_SENSOR_RAW8
        assert fake.trigger_mode == IS_SET_TRIGGER_LO_HI
        assert capture.pixel_format() == "RAW8"
    finally:
        await capture.close()


async def test_u2_raw10_can_be_chosen_at_open(fake):
    capture = UEyeFrameCapture(1, ueye=fake, in_flight=1, pixel_format="RAW10")
    try:
        assert fake.color_mode == IS_CM_SENSOR_RAW10
    finally:
        await capture.close()


async def test_u2_a_camera_still_held_by_a_killed_process_is_waited_for(fake):
    """After the process that had the camera is killed, is_InitCamera answers
    IS_DEVICE_IN_USE for about 1.5 s (Y7). A relaunch that gave up at the
    first refusal would fail every time it was needed most."""
    fake.in_use = 3

    capture = UEyeFrameCapture(1, ueye=fake, in_flight=1, open_timeout=5.0)

    assert fake.is_open
    assert fake.calls.count("is_InitCamera") == 4
    await capture.close()


async def test_u2_a_camera_that_stays_in_use_fails_the_open_once_the_time_is_up(fake):
    fake.in_use = 10_000
    started = time.monotonic()

    with pytest.raises(UEyeError) as caught:
        UEyeFrameCapture(1, ueye=fake, in_flight=1, open_timeout=0.3)

    assert caught.value.code == IS_DEVICE_IN_USE
    assert "IS_DEVICE_IN_USE" in str(caught.value)
    assert time.monotonic() - started < 2.0
    assert not fake.is_open


def test_u2_a_camera_that_is_not_there_is_an_error_at_once(fake):
    started = time.monotonic()

    with pytest.raises(UEyeError):
        UEyeFrameCapture(5, ueye=fake, in_flight=1, open_timeout=5.0)

    assert time.monotonic() - started < 1.0
    assert not fake.is_open


def test_u2_a_failed_open_exits_the_camera_it_opened(fake):
    """A camera left initialised is refused to every later open, in this
    process and the next, until this one exits."""
    fake.fail("is_SetExternalTrigger", IS_NO_SUCCESS)

    with pytest.raises(UEyeError):
        UEyeFrameCapture(1, ueye=fake, in_flight=1)

    assert not fake.is_open
    assert fake.calls[-1] == "is_ExitCamera"


def test_u2_an_unknown_pixel_format_is_refused_before_the_camera_opens(fake):
    with pytest.raises(ValueError, match="RAW8"):
        UEyeFrameCapture(1, ueye=fake, in_flight=1, pixel_format="RAW12")

    assert "is_InitCamera" not in fake.calls


# --- U3: the frame format ------------------------------------------------------------------


@pytest.mark.parametrize(("pixel_format", "dtype", "size"), [("RAW8", "u1", 1), ("RAW10", "<u2", 2)])
def test_u3_the_frame_format_comes_from_the_aoi_and_the_pixel_format(open_capture, fake, pixel_format, dtype, size):
    """RAW10 sits in the low 10 bits of 16 (Y5), which numpy describes exactly
    as little-endian uint16. Rows are not padded (Y2), so a frame is its
    pixels and nothing else."""
    capture = open_capture(fake, in_flight=2, pixel_format=pixel_format)
    capture.set_aoi(8, 4, 48, 20)

    fmt = capture.frame_format()

    assert fmt.shape == (20, 48)
    assert np.dtype(fmt.dtype) == np.dtype(dtype)
    assert fmt.strides == (48 * size, size)
    assert fmt.frame_bytes == 48 * 20 * size
    assert fmt.frame_offset == 0
    assert fmt.in_flight == 2
    assert fmt.slot_alignment == 1


def test_u3_a_colour_mode_set_behind_the_adapters_back_is_refused(open_capture, fake):
    """Only RAW8 and RAW10 are described (user). A frame format guessed for
    any other mode would lay out slots the driver then overruns."""
    capture = open_capture(fake)
    fake.color_mode = IS_CM_MONO8

    with pytest.raises(ValueError, match="RAW8 or RAW10"):
        capture.frame_format()


def test_u3_the_metadata_declares_the_cameras_clock(open_capture, fake):
    """Device time is the camera's raw stamp at the end of the transfer, in
    0.1 us ticks (user, 2026-09-30, from Y1). Frame numbers step by one
    within a capture (Y6). Host time is when the adapter took the frame."""
    capture = open_capture(fake)

    metadata = capture.metadata()

    assert metadata.fields == MetaField.HW_FRAME | MetaField.DEVICE_TIME | MetaField.HOST_TIME
    assert metadata.device_time_source is TimeSource.CAMERA
    assert metadata.device_clock_hz == 1e7


# --- U4: attach -----------------------------------------------------------------------------


async def test_u4_attach_gives_the_driver_every_slot_in_slot_order(open_capture, fake, segments):
    """The buffer assumes frame k lands in slot k mod n, and the driver fills
    its sequence in the order memories were added (Y3). The fake numbers its
    memories unlike the camera, so the adapter must use the IDs it was
    given."""
    capture = open_capture(fake)
    writer = segments(capture.frame_format())

    await capture.attach(writer.slots)

    addresses = [writer.slots.ctypes.data + i * writer.slots.strides[0] for i in range(N_SLOTS)]
    assert [fake.memories[i] for i in fake.sequence] == addresses
    await capture.detach()


async def test_u4_a_driver_that_pads_rows_is_refused_and_given_nothing(open_capture, fake, segments):
    """The camera does not pad rows (Y2), and the format says so. If a driver
    did, every row after the first would land in the wrong place, and the
    last rows in the next slot."""
    capture = open_capture(fake)
    writer = segments(capture.frame_format())
    fake.row_padding = 4

    with pytest.raises(ValueError, match="pitch"):
        await capture.attach(writer.slots)

    assert fake.memories == {}
    assert fake.sequence == []


async def test_u4_a_refused_memory_gives_back_those_already_given(open_capture, fake, segments):
    """Memory the driver holds is memory it may write into after the role
    believes it free."""
    capture = open_capture(fake)
    writer = segments(capture.frame_format())
    fake.fail("is_SetAllocatedImageMem", IS_INVALID_PARAMETER, after=2)

    with pytest.raises(UEyeError, match="is_SetAllocatedImageMem"):
        await capture.attach(writer.slots)

    assert fake.memories == {}
    assert fake.sequence == []
    # And the capture is still usable: nothing is left half-attached.
    await capture.attach(writer.slots)
    assert len(fake.sequence) == N_SLOTS
    await capture.detach()


async def test_u4_a_refused_attach_does_not_hold_the_segment_open(open_capture, fake):
    """K9, for a refusal only the driver makes."""
    capture = open_capture(fake)
    fmt = capture.frame_format()
    layout = layout_for(fmt, N_SLOTS)
    writer = ImageBuffer.create("u" + uuid.uuid4().hex[:20], capacity_bytes=layout.resolve().total_bytes, max_slots=N_SLOTS)
    writer.configure(layout)
    fake.fail("is_AddToSequence", IS_NO_SUCCESS)

    with pytest.raises(UEyeError) as caught:
        await capture.attach(writer.slots)

    writer.close()  # raises BufferError if the error holds the slots
    assert caught.value is not None


# --- U5: start ------------------------------------------------------------------------------


async def test_u5_start_opens_the_queue_before_the_capture(open_capture, fake, segments):
    """A frame that arrived before the queue existed would be in a memory the
    adapter never hears of, and the next would be taken for the first."""
    capture = open_capture(fake)
    writer = segments(capture.frame_format())
    await capture.attach(writer.slots)
    fake.calls.clear()

    await capture.start()

    assert [c for c in fake.calls if c in ("is_ImageQueue", "is_CaptureVideo")] == ["is_ImageQueue", "is_CaptureVideo"]
    await capture.detach()


async def test_u5_a_second_capture_on_the_same_slots_starts_afresh_from_slot_0(open_capture, fake, segments):
    """Stopping and starting again without detaching must not leave the first
    capture's queue behind: its entries would be counted as the second's."""
    capture = open_capture(fake)
    writer = await capturing(capture, segments)
    await triggered(fake, 3)
    await capture.wait_frames(0, 1.0)
    await capture.stop()
    writer.slots[:] = 0

    await capture.start()
    await triggered(fake, 2)

    assert await capture.wait_frames(0, 1.0) == 2
    assert [bool(slot.any()) for slot in writer.slots] == [True, True, False, False]
    await capture.detach()


async def test_u5_a_capture_that_will_not_start_leaves_no_queue(open_capture, fake, segments):
    capture = open_capture(fake)
    writer = segments(capture.frame_format())
    await capture.attach(writer.slots)
    fake.fail("is_CaptureVideo", IS_NO_SUCCESS)

    with pytest.raises(UEyeError, match="is_CaptureVideo"):
        await capture.start()

    assert not fake.queue_active
    await capture.start()  # and it can be tried again
    await capture.detach()


async def test_u5_missed_triggers_before_the_start_are_not_this_captures(open_capture, fake, segments):
    """The camera's count of missed triggers is read once at the start and
    dropped: a count left from an earlier capture is not a loss in this one."""
    capture = open_capture(fake)
    writer = segments(capture.frame_format())
    await capture.attach(writer.slots)
    fake.missed_triggers = 3  # an earlier capture's, never read

    await capture.start()
    await triggered(fake, 1)

    assert await capture.wait_frames(0, 1.0) == 1
    await capture.detach()


# --- U6: waiting: counting, slots and losses -------------------------------------------------


async def test_u6_each_frame_is_given_back_to_the_driver_once_its_info_is_read(open_capture, fake, segments):
    """A memory stays locked until the adapter unlocks it, and a locked memory
    is skipped when its turn comes (Y3). The info is read first: the manual
    says it matches the image only while locked (the fake records a read
    after the unlock as misuse)."""
    capture = open_capture(fake)
    await capturing(capture, segments)

    await triggered(fake, 6)

    assert await capture.wait_frames(0, 1.0) == 6
    assert fake.locked() == []


async def test_u6_every_frame_waiting_in_the_queue_is_counted_at_once(open_capture, fake, segments):
    """Frames queued while the role was publishing are all counted by the next
    wait, not one per call: the driver writes on into the slots of frames
    the role has not heard of."""
    capture = open_capture(fake)
    await capturing(capture, segments)
    await triggered(fake, 3)

    assert await capture.wait_frames(0, 1.0) == 3


async def test_u6_a_frame_out_of_place_is_an_error_and_stays_one(open_capture, fake, segments):
    """The driver skips a memory in silence (Y3). The frame after it would be
    published from the wrong slot, so the adapter checks where each lands."""
    capture = open_capture(fake)
    await capturing(capture, segments)
    await triggered(fake, 1)
    fake.skip = 1
    await triggered(fake, 1)

    for _ in range(2):
        with pytest.raises(RuntimeError, match="slot 2, not slot 1"):
            await capture.wait_frames(0, 1.0)


@pytest.mark.parametrize("loss_report", ["status", "gap"])
async def test_u6_a_lost_frame_is_seen_either_way_the_camera_shows_it(loss_report, open_capture, segments):
    """A frame the camera exposed but never delivered shows as a capture
    status in the queue, in its place, or as a gap in the frame numbers
    (Y6: they step by one within a capture). Either alone must be enough."""
    fake = FakeUEye(loss_report=loss_report)
    capture = open_capture(fake)
    await capturing(capture, segments)
    await triggered(fake, 2)
    fake.lose(2)
    await triggered(fake, 1)

    with pytest.raises(FramesLost) as caught:
        await capture.wait_frames(2, 1.0)

    assert (caught.value.after, caught.value.lost) == (2, 2)


async def test_u6_a_trigger_the_sensor_was_too_busy_for_is_a_lost_frame(open_capture, fake, segments):
    """Under a trigger the sensor does not expose while it reads out, and a
    trigger that comes too soon is ignored (Y9). Nothing reaches the queue,
    but the camera counts it (probe, 2026-09-30). A pulse with no frame is a
    plane missing from the stack, so it is a loss. Where it fell is known
    only to within the frames the adapter had taken when it read the count,
    so ``after`` may be early, never late."""
    capture = open_capture(fake)
    await capturing(capture, segments)
    await triggered(fake, 2)
    fake.miss(3)
    await triggered(fake, 1)

    with pytest.raises(FramesLost) as caught:
        await capture.wait_frames(0, 1.0)

    assert caught.value.lost == 3
    assert caught.value.after <= 3


async def test_u6_a_missed_trigger_is_seen_even_with_no_frame_after_it(open_capture, fake, segments):
    """The last pulse of an acquisition may be the one missed. No frame comes
    to prompt a look, so the count is read while waiting too."""
    capture = open_capture(fake)
    await capturing(capture, segments)
    await triggered(fake, 2)
    assert await capture.wait_frames(0, 1.0) == 2
    fake.miss(1)

    with pytest.raises(FramesLost) as caught:
        await capture.wait_frames(2, 1.0)

    assert (caught.value.after, caught.value.lost) == (2, 1)


async def test_u6_an_error_from_the_queue_is_raised(open_capture, fake, segments):
    capture = open_capture(fake)
    await capturing(capture, segments)
    fake.fail("is_ImageQueue", IS_INVALID_PARAMETER)

    with pytest.raises(UEyeError, match="is_ImageQueue"):
        await capture.wait_frames(0, 1.0)


# --- U7: stamps -----------------------------------------------------------------------------


async def test_u7_stamps_come_from_each_frames_info(open_capture, fake, segments):
    """hw_frame is u64FrameNumber and device_ticks u64TimestampDevice, as the
    driver gave them for that frame; host time is when the adapter took it."""
    capture = open_capture(fake)
    await capturing(capture, segments)
    before = time.time_ns()
    await triggered(fake, 3)
    await capture.wait_frames(0, 1.0)
    after = time.time_ns()

    stamps = capture.stamps(0, 3)

    first = int(stamps["hw_frame"][0])
    assert list(stamps["hw_frame"]) == [first, first + 1, first + 2]
    assert list(stamps["device_ticks"]) == [TICKS_START + n * FRAME_TICKS for n in range(first, first + 3)]
    assert all(before <= t <= after for t in stamps["host_time_ns"])


# --- U8: stop -------------------------------------------------------------------------------


async def test_u8_stop_cancels_the_wait_inside_the_driver(open_capture, fake, segments):
    """A wait left inside the driver outlives the stop, and the queue cannot
    be exited under it. Without the cancel, the stop would still end, but
    only when the wait ran out: a slice later, on every disarm."""
    capture = open_capture(fake)
    await capturing(capture, segments)
    waiting = asyncio.create_task(capture.wait_frames(0, 3600))
    await asyncio.sleep(GRACE)
    assert fake.waiting() == 1

    started = time.monotonic()
    await capture.stop()

    assert time.monotonic() - started < WAIT_SLICE / 2
    assert fake.waiting() == 0
    assert await waiting == 0


async def test_u8_frames_queued_before_the_stop_are_still_delivered_after_it(open_capture, fake, segments):
    """The role's last pass after a stop publishes whatever arrived before it.
    Frames stay in the queue across a stop (probe, 2026-09-30); leaving them
    there would drop the end of every acquisition."""
    capture = open_capture(fake)
    fake.wait_gate = threading.Event()  # the consumer is held at its next wait
    await capturing(capture, segments)
    assert await asyncio.to_thread(fake.at_gate.wait, 5)
    await triggered(fake, 3)

    stopping = asyncio.create_task(capture.stop())
    await asyncio.sleep(GRACE)
    fake.wait_gate.set()
    await stopping

    assert await capture.wait_frames(0, 0) == 3


# --- U9: detach and close -----------------------------------------------------------------------


async def test_u9_detach_gives_every_memory_back(open_capture, fake, segments):
    """The queue is exited, the sequence cleared and every memory freed, so
    the driver holds no address in the segment."""
    capture = open_capture(fake)
    await capturing(capture, segments)
    await triggered(fake, 2)

    await capture.detach()

    assert not fake.queue_active
    assert fake.sequence == []
    assert fake.memories == {}


async def test_u9_close_exits_the_camera(fake, segments):
    capture = UEyeFrameCapture(1, ueye=fake, in_flight=1)
    capture.set_aoi(*AOI)
    await capturing(capture, segments)

    await capture.close()
    await capture.close()

    assert not fake.is_open
    assert fake.calls.count("is_ExitCamera") == 1


async def test_u9_close_waits_for_a_wait_that_is_running(fake, segments):
    """Exiting the camera under a running wait is undefined: the cancelled
    wait comes out first."""
    capture = UEyeFrameCapture(1, ueye=fake, in_flight=1)
    capture.set_aoi(*AOI)
    await capturing(capture, segments)
    fake.linger = 0.3
    waiting = asyncio.create_task(capture.wait_frames(0, 3600))
    await asyncio.sleep(GRACE)

    await capture.close()

    assert not fake.is_open
    assert fake.misuse == []
    await asyncio.gather(waiting, return_exceptions=True)


async def test_u9_close_tries_every_step_and_reports_the_first_failure(fake, segments):
    """A step that fails must not skip the ones after it: exiting the camera
    is what lets the next process open it."""
    capture = UEyeFrameCapture(1, ueye=fake, in_flight=1)
    capture.set_aoi(*AOI)
    await capturing(capture, segments)
    fake.fail("is_FreeImageMem", IS_NO_SUCCESS)

    with pytest.raises(UEyeError, match="is_FreeImageMem"):
        await capture.close()

    assert not fake.is_open


# --- U10: settings ----------------------------------------------------------------------------


def test_u10_the_pixel_format_is_set_by_name(open_capture, fake):
    capture = open_capture(fake)

    assert capture.set_pixel_format("RAW10") == "RAW10"

    assert fake.color_mode == IS_CM_SENSOR_RAW10
    assert capture.frame_format().dtype == "<u2"
    with pytest.raises(ValueError, match="RAW8 or RAW10"):
        capture.set_pixel_format("MONO8")


def test_u10_the_aoi_is_read_back_as_the_camera_took_it(open_capture, fake):
    capture = open_capture(fake)

    assert capture.set_aoi(2, 4, 128, 64) == (2, 4, 128, 64)
    assert capture.aoi() == (2, 4, 128, 64)
    with pytest.raises(UEyeError, match="IS_INVALID_PARAMETER"):
        capture.set_aoi(1, 0, 128, 64)  # x on an odd pixel


@pytest.mark.parametrize("change", ["pixel format", "aoi"])
async def test_u10_a_change_to_the_frame_is_refused_while_memory_is_attached(open_capture, fake, segments, change):
    """The slots are laid out for the format the capture had at attach, and
    the driver was told each memory's size then. The uEye does not refuse a
    new AOI itself, so the adapter does."""
    capture = open_capture(fake)
    writer = segments(capture.frame_format())
    await capture.attach(writer.slots)

    with pytest.raises(RuntimeError, match="attached"):
        if change == "pixel format":
            capture.set_pixel_format("RAW10")
        else:
            capture.set_aoi(0, 0, 32, 16)

    assert fake.color_mode == IS_CM_SENSOR_RAW8
    assert fake.aoi_rect == AOI
    await capture.detach()


def test_u10_pixel_clocks_are_listed_in_order_and_set(open_capture, fake):
    capture = open_capture(fake)

    assert capture.pixel_clocks() == sorted(PIXEL_CLOCKS)
    assert capture.set_pixel_clock(40) == 40
    assert capture.pixel_clock() == 40


def test_u10_frame_rate_and_exposure_answer_what_the_camera_applied(open_capture, fake):
    capture = open_capture(fake)

    applied = capture.set_frame_rate(1e6)  # far beyond the camera
    assert applied == pytest.approx(1 / fake.min_frame_time())
    assert capture.frame_rate() == applied
    assert capture.set_exposure(1.234) == pytest.approx(1.23)
    assert capture.exposure() == pytest.approx(1.23)


def test_u10_the_trigger_is_an_edge_and_a_delay(open_capture, fake):
    capture = open_capture(fake)

    assert capture.set_trigger("falling", 250) == ("falling", 250)

    assert fake.trigger_mode == IS_SET_TRIGGER_HI_LO
    assert fake.trigger_delay_us == 250
    assert capture.trigger() == ("falling", 250)
    with pytest.raises(ValueError, match="rising or falling"):
        capture.set_trigger("software")


# --- U11: the shortest trigger period ---------------------------------------------------------


def test_u11_the_shortest_trigger_period_is_readout_plus_exposure_plus_delay(open_capture, fake):
    """Under a trigger this sensor exposes and then reads out, and ignores a
    trigger until both are done (Y9). The minimum frame time is the readout;
    on the camera, the sum was exact: at 0.97 of it every other trigger was
    missed, at 1.0 none (probe, 2026-09-30). A trigger delay adds to it."""
    capture = open_capture(fake)
    capture.set_pixel_clock(40)
    capture.set_exposure(10.0)
    capture.set_trigger("rising", 5000)

    period = capture.shortest_trigger_period()

    assert period == pytest.approx(fake.min_frame_time() + 10.0e-3 + 5000e-6)


# --- U12: a software trigger and errors ---------------------------------------------------------


async def test_u12_fire_trigger_exposes_one_frame(open_capture, fake, segments):
    """is_ForceTrigger gives a frame in a hardware trigger mode (probe), so a
    capture can be driven without the DAQ."""
    capture = open_capture(fake)
    await capturing(capture, segments)

    capture.fire_trigger()
    fake.settle()

    assert await capture.wait_frames(0, 1.0) == 1


def test_u12_fire_trigger_outside_a_capture_is_an_error(open_capture, fake):
    capture = open_capture(fake)

    with pytest.raises(UEyeError, match="is_ForceTrigger"):
        capture.fire_trigger()


def test_u12_an_error_names_the_call_and_its_code(open_capture, fake):
    capture = open_capture(fake)
    fake.fail("is_PixelClock", IS_INVALID_PARAMETER)

    with pytest.raises(UEyeError) as caught:
        capture.pixel_clock()

    assert caught.value.call == "is_PixelClock"
    assert caught.value.code == IS_INVALID_PARAMETER
    assert str(caught.value) == "is_PixelClock failed: IS_INVALID_PARAMETER (125)"


def test_u12_the_handle_the_camera_gives_is_the_one_used(open_capture, fake):
    """is_InitCamera writes the handle back over the ID it was given. The fake
    makes the two differ, and records a call on any other as misuse."""
    open_capture(fake).pixel_clock()

    assert fake.misuse == []
    assert HANDLE != fake.camera
