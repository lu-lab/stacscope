"""Q2-Q9: DcamFrameCapture over the fake DCAM library, beyond the port
contract (Q1, test_contract.py): what only a DCAM adapter has to get right.

Windows only, like the adapter, and not part of the default run:

    uv run pytest tests/adapters/dcam
"""
# For end use and not for commercial purposes.

import asyncio
import ctypes
import sys
import threading
import uuid

import numpy as np
import pytest
from fake_dcam import (
    FRAME_PERIOD_US,
    TIMESTAMP_START_US,
    FakeDcam,
    error_text,
)

from stacscope.adapters.dcam import DcamError, DcamFrameCapture, DcamUnavailable, _api
from stacscope.core.buffer import ImageBuffer, MetaField, TimeSource
from stacscope.core.devices.image_streaming import layout_for
from stacscope.core.ports import FramesLost

GRACE = 0.25
"""Seconds allowed for a DCAM wait's thread to return and its caller to run."""

N_SLOTS = 4


@pytest.fixture
def fake():
    fake = FakeDcam()
    yield fake
    assert fake.misuse == []


@pytest.fixture
async def open_capture(segments):
    """``open_capture(fake)`` opens a capture on it; each is closed after the
    test, and its fake checked for misuse. Closed before any segment is: the
    role's order, since memory the capture still holds cannot close."""
    opened = []

    def open_(fake, **options):
        options.setdefault("in_flight", 1)
        capture = DcamFrameCapture(library=fake, **options)
        opened.append((capture, fake))
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
            "q" + uuid.uuid4().hex[:20],
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


async def settle():
    await asyncio.sleep(GRACE)


# --- Q2: the frame format, from properties found by name --------------------------------


def test_q2_the_frame_format_comes_from_the_buffer_properties(open_capture):
    """DCAM describes what it will write into attached memory in the BUFFER
    properties, which can differ from the IMAGE ones (the grabber may pad
    rows, or put bytes before the first pixel). A format taken from the
    wrong ones would lay out a buffer the driver then overruns. The fake
    numbers its properties unlike dcamprop.h, so only a lookup by name
    finds them."""
    fake = FakeDcam(width=40, height=24, row_bytes=96, top_offset=64, frame_bytes=64 + 24 * 96 + 32)
    capture = open_capture(fake, in_flight=2, slot_alignment=512)

    fmt = capture.frame_format()

    assert fmt.shape == (24, 40)
    assert np.dtype(fmt.dtype) == np.dtype("<u2")
    assert fmt.strides == (96, 2)
    assert fmt.frame_offset == 64
    assert fmt.frame_bytes == 64 + 24 * 96 + 32
    assert fmt.in_flight == 2
    assert fmt.slot_alignment == 512


def test_q2_eight_bit_pixels_are_described_as_bytes(open_capture):
    fmt = open_capture(FakeDcam(pixel_type="MONO8")).frame_format()

    assert np.dtype(fmt.dtype) == np.dtype("u1")
    assert fmt.strides == (40, 1)


@pytest.mark.parametrize("pixel_type", ["MONO12", "MONO12P"])
def test_q2_a_pixel_type_that_cannot_be_described_is_refused(open_capture, pixel_type):
    """A packed 12-bit pixel has no numpy dtype, and whether MONO12 sits in
    the low or high bits of its 16 is not documented. Laid out as if it were
    MONO16, every consumer would read wrong values without a word; refused,
    the arm fails and says why."""
    capture = open_capture(FakeDcam(pixel_type=pixel_type))

    with pytest.raises(ValueError, match=pixel_type):
        capture.frame_format()


def test_q2_a_setting_change_shows_in_the_next_format(open_capture):
    """ROI changes between acquisitions; the role asks for the format on
    every arm, so the format must be read from the camera each time, not
    remembered from open."""
    fake = FakeDcam(width=40, height=24)
    capture = open_capture(fake)
    capture.frame_format()

    fake.set_property("IMAGE WIDTH", 32)
    fake.set_property("BUFFER ROWBYTES", 64)
    fake.set_property("BUFFER FRAMEBYTES", 64 * 24)

    fmt = capture.frame_format()
    assert fmt.shape == (24, 32)
    assert fmt.strides == (64, 2)


def test_q2_a_camera_without_a_property_the_format_needs_is_refused_at_open(fake):
    """Found at open, naming the property, rather than as a KeyError at the
    first arm."""
    fake.remove_property("BUFFER TOP OFFSET BYTES")

    with pytest.raises(DcamUnavailable, match="BUFFER TOP OFFSET BYTES"):
        DcamFrameCapture(library=fake, in_flight=1)

    assert fake.calls[-1] == "dcamapi_uninit"


# --- Q3: DcamError -------------------------------------------------------------------------


async def test_q3_a_failed_call_raises_dcam_error_with_its_name_and_dcams_text(
    open_capture, fake, segments
):
    """The operator sees this message when an arm fails, and "0x80000103"
    alone tells them nothing: the call, the error's name and DCAM's own
    text do."""
    capture = open_capture(fake)
    writer = segments(capture.frame_format())
    await capture.attach(writer.slots)
    fake.fail("dcamcap_start", _api.ERR_NOTREADY)

    with pytest.raises(DcamError) as caught:
        await capture.start()

    error = caught.value
    assert error.code == _api.ERR_NOTREADY
    assert error.name == "DCAMERR_NOTREADY"
    assert "dcamcap_start" in str(error)
    assert "DCAMERR_NOTREADY" in str(error)
    assert error_text(_api.ERR_NOTREADY) in str(error)
    await capture.detach()


async def test_q3_an_error_without_a_name_here_is_reported_by_number(open_capture, fake, segments):
    capture = open_capture(fake)
    writer = segments(capture.frame_format())
    code = _api._signed(0x83001002)  # DCAMERR_FAILREADCAMERA, not in the adapter's table
    fake.fail("dcambuf_attach", code)

    with pytest.raises(DcamError) as caught:
        await capture.attach(writer.slots)

    got, message = caught.value.code, str(caught.value)
    del caught  # its traceback holds the slots, which would keep the segment open
    assert got == code
    assert "0x83001002" in message
    assert error_text(code) in message


async def test_q3_an_error_whose_text_cannot_be_read_is_still_reported(
    open_capture, fake, segments
):
    """Asking DCAM for the text is a second call, which can fail too. The
    original error must still come out, not the lookup's."""
    capture = open_capture(fake)
    writer = segments(capture.frame_format())
    fake.fail("dcambuf_attach", _api.ERR_BUSY)
    fake.fail("dcamdev_getstring", _api.ERR_NOTSUPPORT)

    with pytest.raises(DcamError) as caught:
        await capture.attach(writer.slots)

    code = caught.value.code
    del caught  # its traceback holds the slots, which would keep the segment open
    assert code == _api.ERR_BUSY


async def test_q3_any_positive_return_is_success(open_capture, fake, segments):
    """The reference: success is any positive value, not only
    DCAMERR_SUCCESS. Treating 88 as a failure would refuse a call that
    worked, leaving the memory attached and nobody tracking it."""
    capture = open_capture(fake)
    writer = segments(capture.frame_format())
    fake.succeed_with("dcambuf_attach", 88)

    await capture.attach(writer.slots)

    assert fake.attached
    await capture.detach()


# --- Q4: closing -----------------------------------------------------------------------------


def _after(calls: list[str], mark: int) -> list[str]:
    return [c for c in calls[mark:] if c != "dcamdev_getstring"]


async def test_q4_close_releases_everything_in_order(fake, segments):
    """The reverse of opening. A buffer released while capturing is refused
    (DCAMERR_BUSY), and a wait handle closed under a running wait is
    undefined, so stop and abort come first; the device closes before the
    API that owns it."""
    capture = DcamFrameCapture(library=fake, in_flight=1)
    writer = await capturing(capture, segments)
    mark = len(fake.calls)

    await capture.close()

    assert _after(fake.calls, mark) == [
        "dcamcap_stop",
        "dcamwait_abort",
        "dcambuf_release",
        "dcambuf_release",
        "dcambuf_release",
        "dcamwait_close",
        "dcamdev_close",
        "dcamapi_uninit",
    ]
    del writer


async def test_q4_close_tries_every_step_and_reports_every_failure(fake, segments):
    """An unreleased camera handle is what wedges DCAM until a reboot. So a
    step that fails never skips the steps after it: the first failure is
    raised, and each later one is a note on it."""
    capture = DcamFrameCapture(library=fake, in_flight=1)
    await capturing(capture, segments)
    fake.fail("dcambuf_release", _api.ERR_BUSY)
    fake.fail("dcamdev_close", _api.ERR_NOTREADY)

    with pytest.raises(DcamError) as caught:
        await capture.close()

    assert caught.value.code == _api.ERR_BUSY
    notes = "\n".join(getattr(caught.value, "__notes__", []))
    assert "dcamdev_close" in notes
    assert fake.calls[-1] == "dcamapi_uninit"


async def test_q4_closing_twice_does_nothing_the_second_time(fake):
    capture = DcamFrameCapture(library=fake, in_flight=1)
    await capture.close()
    mark = len(fake.calls)

    await capture.close()

    assert fake.calls[mark:] == []


async def test_q4_close_waits_for_a_wait_that_is_running(fake, segments):
    """The role cancels its publishing task, which leaves DCAM's wait running
    in its thread. Closing the wait handle under it is undefined; close must
    abort it and let the thread come out first. The fake records the
    misuse."""
    capture = DcamFrameCapture(library=fake, in_flight=1)
    await capturing(capture, segments)
    fake.linger = 0.3  # the aborted wait takes its time to come out
    waiting = asyncio.create_task(capture.wait_frames(0, 3600))
    await settle()
    assert fake.waiting() == 1
    waiting.cancel()

    await capture.close()

    assert fake.misuse == []
    assert fake.waiting() == 0


async def test_q4_a_failed_open_closes_what_it_opened(fake):
    """A camera left open by a failed open is held until the process dies,
    and the next open fails with "already in use"."""
    fake.fail("dcamwait_open", _api.ERR_NOTSUPPORT)

    with pytest.raises(DcamError, match="dcamwait_open"):
        DcamFrameCapture(library=fake, in_flight=1)

    assert fake.calls[-2:] == ["dcamdev_close", "dcamapi_uninit"]


def test_q4_no_camera_found_is_an_error_and_leaves_dcam_uninitialized():
    """DCAM's init fails when it finds no camera. Whether it has initialized
    anything then is not documented, so the adapter uninitializes anyway."""
    fake = FakeDcam(cameras=0)

    with pytest.raises(DcamError) as caught:
        DcamFrameCapture(library=fake, in_flight=1)

    assert caught.value.code == _api.ERR_NOCAMERA
    assert fake.calls[-1] == "dcamapi_uninit"
    assert fake.misuse == []


def test_q4_a_camera_index_past_those_found_is_refused():
    fake = FakeDcam(cameras=1)

    with pytest.raises(ValueError, match="1 camera"):
        DcamFrameCapture(1, library=fake, in_flight=1)

    assert fake.calls[-1] == "dcamapi_uninit"
    assert fake.misuse == []


# --- Q5: attaching -----------------------------------------------------------------------------


async def test_q5_attach_hands_dcam_one_address_per_slot_in_slot_order(open_capture, fake, segments):
    """DCAM writes frame i into the i-th address it was given, modulo their
    number, and the role reads frame i from slot i mod n. Any other order,
    and every consumer reads the wrong image under the right frame number.
    Time and frame stamps get a buffer per slot, in the same order."""
    capture = open_capture(fake)
    writer = segments(capture.frame_format())
    slots = writer.slots

    await capture.attach(slots)

    assert fake._frames == [slots[i].ctypes.data for i in range(N_SLOTS)]
    assert len(fake._timestamps) == N_SLOTS
    assert len(fake._framestamps) == N_SLOTS
    del slots
    await capture.detach()


async def test_q5_detach_while_capturing_stops_before_it_releases(open_capture, fake, segments):
    """DCAM refuses to release a buffer while capturing. On the death path
    nobody stopped first, so detach must."""
    capture = open_capture(fake)
    await capturing(capture, segments)
    mark = len(fake.calls)

    await capture.detach()

    calls = _after(fake.calls, mark)
    assert calls.index("dcamcap_stop") < calls.index("dcambuf_release")
    assert not fake.attached


async def test_q5_a_stamp_buffer_dcam_refuses_undoes_the_attach(open_capture, fake, segments):
    """Frames attached but the attach reported failed would leave DCAM
    writing into memory the role believes free."""
    capture = open_capture(fake)
    writer = segments(capture.frame_format())
    fake.fail("dcambuf_attach", _api.ERR_NOTSUPPORT, after=1)  # frames go in, stamps fail

    with pytest.raises(DcamError):
        await capture.attach(writer.slots)

    assert not fake.attached
    # Nothing was left half-attached: the same memory attaches again.
    await capture.attach(writer.slots)
    await capture.detach()


# --- Q6: a stop that comes before the wait ------------------------------------------------------


async def test_q6_a_stop_before_the_wait_begins_still_ends_it(open_capture, fake, segments):
    """dcamwait_abort with nobody waiting does nothing (documented). The
    publishing thread is between checking and waiting when the stop comes,
    so the abort is lost; if the wait then ran its full timeout, disarm
    would hang for it. The wait must end promptly regardless."""
    capture = open_capture(fake)
    await capturing(capture, segments)
    fake.wait_gate = threading.Event()
    waiting = asyncio.create_task(capture.wait_frames(0, 3600))
    await asyncio.to_thread(fake.at_gate.wait, 5)

    await capture.stop()  # the abort finds nobody waiting
    fake.wait_gate.set()  # and now the thread begins its wait
    fake.wait_gate = None

    await asyncio.wait_for(waiting, 1.0)
    assert waiting.result() == 0



# --- Q7: lost frames ---------------------------------------------------------------------------


async def test_q7_a_gap_in_the_frame_stamps_is_lost_frames(open_capture, segments):
    """Frames the camera exposed and the grabber never delivered show only as
    a jump in the frame stamps, if DCAM says nothing else. Missed, a stack
    comes out with planes missing and nobody told."""
    fake = FakeDcam(loss_report="gap")
    capture = open_capture(fake)
    await capturing(capture, segments)
    fake.pulse(2)
    fake.lose(3)
    fake.pulse(1)
    await settle()

    for _ in range(2):
        with pytest.raises(FramesLost) as caught:
            await capture.wait_frames(0, 1.0)
        assert (caught.value.after, caught.value.lost) == (2, 3)


async def test_q7_dcams_lost_frame_error_is_lost_frames(open_capture, segments):
    """DCAM may instead return DCAMERR_LOSTFRAME from a wait, once, with no
    count. It is still fatal, raised on every wait from then on, and says
    the count is unknown rather than inventing one."""
    fake = FakeDcam(loss_report="error")
    capture = open_capture(fake)
    await capturing(capture, segments)
    fake.pulse(2)
    fake.settle()
    await settle()
    assert await capture.wait_frames(0, 1.0) == 2

    fake.lose(1)
    for _ in range(2):
        with pytest.raises(FramesLost) as caught:
            await capture.wait_frames(2, 1.0)
        assert caught.value.after == 2
        assert caught.value.lost is None
        assert "unknown" in str(caught.value)


async def test_q7_a_gap_is_found_even_when_the_frames_around_it_were_overwritten(
    open_capture, segments
):
    """A caller that fell a ring behind cannot see the frames on either side
    of the gap, only that the frame stamps and the count disagree."""
    fake = FakeDcam(loss_report="gap")
    capture = open_capture(fake)
    await capturing(capture, segments)
    fake.pulse(1)
    await settle()
    assert await capture.wait_frames(0, 1.0) == 1

    fake.pulse(1)
    fake.lose(1)
    fake.pulse(2 * N_SLOTS)
    await settle()

    with pytest.raises(FramesLost) as caught:
        await capture.wait_frames(1, 1.0)
    assert caught.value.lost == 1


async def test_q7_the_next_capture_starts_clean(open_capture, segments):
    fake = FakeDcam(loss_report="both")
    capture = open_capture(fake)
    await capturing(capture, segments)
    fake.pulse(1)
    fake.lose(1)
    fake.pulse(1)
    await settle()
    with pytest.raises(FramesLost):
        await capture.wait_frames(0, 1.0)
    await capture.stop()

    await capture.start()
    fake.pulse(2)
    await settle()

    assert await capture.wait_frames(0, 1.0) == 2


@pytest.mark.parametrize("start", [2**31 - 2, 2**32 - 2], ids=["sign", "wrap"])
async def test_q7_frame_stamps_that_wrap_are_not_a_gap(open_capture, segments, start):
    """DCAM's frame stamp is an int32. Crossing its sign, or wrapping, is
    counting on, not losing frames."""
    fake = FakeDcam(framestamp_start=start)
    capture = open_capture(fake)
    await capturing(capture, segments)
    fake.pulse(3)
    await settle()

    assert await capture.wait_frames(0, 1.0) == 3


@pytest.mark.parametrize("start", [2**31 - 2, 2**32 - 2], ids=["sign", "wrap"])
async def test_q7_a_gap_across_the_wrap_is_still_found(open_capture, segments, start):
    """Stamps 2**31 - 1 then -2**31 + 1 are two apart, as are -1 then 1: a
    gap there is still a gap, not a step backwards."""
    fake = FakeDcam(framestamp_start=start, loss_report="gap")
    capture = open_capture(fake)
    await capturing(capture, segments)
    fake.pulse(2)
    fake.lose(1)
    fake.pulse(1)
    await settle()

    with pytest.raises(FramesLost) as caught:
        await capture.wait_frames(0, 1.0)
    assert (caught.value.after, caught.value.lost) == (2, 1)


# --- Q8: stamps ----------------------------------------------------------------------------------


def test_q8_the_metadata_declares_the_drivers_microsecond_clock(open_capture, fake):
    """The survey found the Flash's time stamps come from the DCAM module, a
    host clock (user's decision: accept them as the driver's). A consumer
    lining frames up with the DAQ must not mistake them for the camera's."""
    metadata = open_capture(fake).metadata()

    assert metadata.fields == MetaField.HW_FRAME | MetaField.DEVICE_TIME
    assert metadata.device_time_source is TimeSource.DRIVER
    assert metadata.device_clock_hz == 1e6


async def test_q8_stamps_are_dcams_frame_stamp_and_time_stamp(open_capture, fake, segments):
    """``hw_frame`` is DCAM's frame stamp and ``device_ticks`` its time stamp
    in microseconds, both exactly, never rescaled or rebased."""
    capture = open_capture(fake)
    await capturing(capture, segments)
    fake.pulse(2)
    await settle()

    stamps = capture.stamps(0, 2)

    assert list(stamps["hw_frame"]) == [1000, 1001]
    assert list(stamps["device_ticks"]) == [TIMESTAMP_START_US, TIMESTAMP_START_US + FRAME_PERIOD_US]


async def test_q8_a_camera_without_stamp_buffers_declares_none(open_capture, segments):
    """The capability check decides. A camera that cannot attach stamp
    buffers gets none attached, and declares no stamps, rather than
    publishing zeros as if they were frame numbers."""
    fake = FakeDcam(stamps=False)
    capture = open_capture(fake)

    assert capture.metadata().fields == MetaField(0)
    await capturing(capture, segments)
    fake.pulse(2)
    await settle()

    assert await capture.wait_frames(0, 1.0) == 2
    assert fake._timestamps is None and fake._framestamps is None


# --- Q9: a library that cannot load ----------------------------------------------------------------


def test_q9_off_windows_the_adapter_says_it_is_windows_only(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")

    with pytest.raises(DcamUnavailable, match="Windows only"):
        DcamFrameCapture(in_flight=1)


def test_q9_without_dcam_api_installed_the_adapter_says_so(monkeypatch):
    def missing(name):
        raise OSError(f"could not find module {name!r}")

    monkeypatch.setattr(ctypes, "WinDLL", missing, raising=False)

    with pytest.raises(DcamUnavailable, match="DCAM-API"):
        DcamFrameCapture(in_flight=1)


def test_q9_a_dcam_api_without_a_needed_function_is_named(monkeypatch):
    """An older DCAM-API (3.x) lacks the version 4 functions; the message
    names the first one missing rather than failing on a call later."""

    class Old:
        def __getattr__(self, name):
            if name == "dcamwait_open":
                raise AttributeError(name)
            return lambda *args: _api.SUCCESS

    monkeypatch.setattr(ctypes, "WinDLL", lambda name: Old(), raising=False)

    with pytest.raises(DcamUnavailable, match="dcamwait_open"):
        DcamFrameCapture(in_flight=1)
