"""FrameCapture port contract. Every capture adapter must pass this suite.

A capture adapter is a frame grabber's side of an image stream: it writes
frames into slot memory it is given, one per trigger, and reports how many
arrived. It never touches the buffer's counter, header or metadata table --
that is the ``ImageStreaming`` role's, above it, as refilling is
``WaveformSource``'s above ``SignalOutput``.

The tests live here, in a module pytest does not collect, and each adapter's
suite imports them beside a ``rig`` fixture of its own (see ``Rig``):

    from frame_capture_contract import *  # the tests, Rig, and the buffers fixture

    @pytest.fixture
    async def rig():
        ...

The simulated adapter's suite is ``test_frame_capture.py``, beside this file,
in virtual time. A fake DCAM DLL (5c) and the camera itself (5d) will bring
their own rigs.

Slot memory comes from a real ``ImageBuffer`` segment, laid out by the same
function the role uses, because the property that matters most -- that the
adapter lets go of the memory -- is only observable there: a view the adapter
still holds makes the segment's close raise ``BufferError``.
"""

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np
import pytest

from stacscope.core.buffer import ImageBuffer, Layout, MetaField
from stacscope.core.devices.image_streaming import layout_for
from stacscope.core.ports import STAMP_DTYPE, FrameCapture, FrameFormat, FramesLost

__all__ = [
    "Rig",
    "buffers",
    "test_a_closed_capture_refuses_to_be_used",
    "test_a_lost_frame_raises_frames_lost",
    "test_a_refused_attach_does_not_hold_the_segment_open",
    "test_after_detach_no_reference_to_any_slot_remains",
    "test_attach_accepts_slots_laid_out_for_the_format",
    "test_attach_over_memory_already_attached_is_refused",
    "test_attach_refuses_slots_that_do_not_fit_the_format",
    "test_attach_while_capturing_is_refused",
    "test_attaching_again_with_a_new_layout_works",
    "test_each_trigger_writes_one_frame_into_the_next_slot_from_slot_0",
    "test_frames_wrap_to_slot_0_after_the_last_slot",
    "test_no_trigger_no_frame",
    "test_release_is_safe_in_every_state_and_repeatable",
    "test_slots_laid_out_for_the_format_honour_its_alignment",
    "test_stamps_are_consecutive_frame_by_frame",
    "test_stamps_follow_their_frames_around_the_ring",
    "test_stamps_of_frames_not_in_the_slots_are_refused",
    "test_start_with_nothing_attached_is_refused",
    "test_stop_ends_a_wait_promptly",
    "test_the_frame_format_is_self_consistent",
    "test_triggers_before_start_and_after_stop_write_nothing",
    "test_wait_frames_returns_as_soon_as_a_frame_arrives",
    "test_wait_frames_returns_at_once_if_frames_are_already_there",
]


@dataclass
class Rig:
    """An adapter under test, open and with nothing attached, and the means
    to drive the thing behind it.

    ``trigger(n)`` sends ``n`` pulses to the capture's trigger input and
    returns once all of them have arrived; on a camera that may be a software
    trigger. ``elapse`` lets time pass: advancing a virtual clock, or sleeping
    on a real one. ``n_slots`` is how many slots the suite gives the adapter.
    ``lose(n)``, if the rig can do it, makes the hardware expose ``n`` frames
    it never delivers; without it the loss test is skipped.
    """

    capture: FrameCapture
    trigger: Callable[[int], Awaitable[None]]
    elapse: Callable[[float], Awaitable[None]]
    n_slots: int = 4
    lose: Callable[[int], Awaitable[None]] | None = None


class _Buffers:
    """Segments laid out for a ``FrameFormat``. ``buffers(fmt, n_slots)``
    returns a ``FrameWriter`` whose ``slots`` are the memory to attach, with
    every byte set to ``SENTINEL`` so a slot the hardware wrote is visible.

    ``close(writer)`` closes one now. A close that raises ``BufferError``
    means the adapter kept a view of the slots; the segment is unlinked all
    the same, so it does not outlive the run, and the error propagates.
    """

    def __init__(self):
        self._open = []

    def __call__(self, fmt: FrameFormat, n_slots: int):
        layout = layout_for(fmt, n_slots)
        writer = ImageBuffer.create(
            "k" + uuid.uuid4().hex[:20],
            capacity_bytes=layout.resolve().total_bytes,
            max_slots=n_slots,
        )
        self._open.append(writer)
        writer.configure(layout)
        writer.slots[...] = SENTINEL
        return writer

    def close(self, writer) -> None:
        self._open.remove(writer)
        try:
            writer.close()
        except BufferError:
            writer._shm.unlink()
            raise

    def close_all(self) -> None:
        for writer in list(self._open):
            self.close(writer)


@pytest.fixture
async def buffers(rig):
    """See ``_Buffers``. Whatever a test leaves open is closed after it, once
    the capture has detached: the role's order, since memory an adapter still
    holds cannot close. Requesting ``rig`` here sets it up first, so its own
    teardown, which closes the capture, runs after this one."""
    made = _Buffers()
    yield made
    await rig.capture.detach()
    made.close_all()


SENTINEL = 0xA5
"""What every byte of a fresh slot holds. No frame from real hardware is
uniformly this, so a slot whose pixels still all read it was not written."""


def _written(writer) -> list[bool]:
    """For each slot, whether any of its pixels differ from the sentinel."""
    blank = np.frombuffer(bytes([SENTINEL]) * writer.frames.itemsize, writer.frames.dtype)[0]
    return [bool(np.any(frame != blank)) for frame in writer.frames]


def _blank(writer, *slots: int) -> None:
    for slot in slots:
        writer.slots[slot] = SENTINEL


async def _capturing(rig, buffers):
    """The capture, attached to fresh slots and started. Returns the writer."""
    writer = buffers(rig.capture.frame_format(), rig.n_slots)
    await rig.capture.attach(writer.slots)
    await rig.capture.start()
    return writer


PATIENCE = 1.0
"""Seconds a test lets ``wait_frames`` wait when it expects nothing to come."""

LONG = 3600.0
"""A timeout no test reaches, so that only frames or ``stop`` end the wait."""


def _extent(fmt: FrameFormat) -> int:
    """Bytes a frame spans from its first pixel, as ``Layout`` works it out."""
    probe = Layout(shape=fmt.shape, dtype=fmt.dtype, n_slots=2, strides=fmt.strides).resolve()
    return probe.slot_bytes


# --- K1: the frame format -------------------------------------------------------


def test_the_frame_format_is_self_consistent(rig):
    """The role lays out every acquisition's buffer from this alone, and a
    consumer reads pixels through it. A format whose pixels overran its own
    ``frame_bytes`` would put a DMA write into the next slot; one whose
    strides overlapped would describe pixels that share bytes; one that
    changed between two asks, with no setting changed, would have the role
    lay out one buffer and the hardware write another."""
    fmt = rig.capture.frame_format()

    assert fmt == rig.capture.frame_format()
    assert np.dtype(fmt.dtype).kind in "biuf"
    assert len(fmt.strides) == len(fmt.shape)
    assert fmt.frame_offset >= 0
    assert fmt.frame_offset + _extent(fmt) <= fmt.frame_bytes
    assert fmt.slot_alignment >= 1
    assert fmt.slot_alignment & (fmt.slot_alignment - 1) == 0, "alignment is a power of two"
    assert 1 <= fmt.in_flight < rig.n_slots


def test_slots_laid_out_for_the_format_honour_its_alignment(rig, buffers):
    """The hardware asks for its slots to start on a boundary (DMA engines
    commonly do); the layout the role builds from the format must give every
    slot one, not only the first, and room for every byte the hardware
    writes."""
    fmt = rig.capture.frame_format()
    writer = buffers(fmt, rig.n_slots)

    slots = writer.slots
    assert slots.shape[0] == rig.n_slots
    assert slots.shape[1] >= fmt.frame_bytes
    for slot in slots:
        assert slot.ctypes.data % fmt.slot_alignment == 0


async def test_attach_accepts_slots_laid_out_for_the_format(rig, buffers):
    """The ordinary path: memory the role laid out is memory the adapter
    takes, and gives back."""
    writer = buffers(rig.capture.frame_format(), rig.n_slots)

    await rig.capture.attach(writer.slots)
    await rig.capture.detach()


def _slots(n: int, slot_bytes: int, alignment: int, *, skew: int = 0) -> np.ndarray:
    """``n`` slots of ``slot_bytes``, each starting ``skew`` bytes past an
    ``alignment`` boundary, so each fault below is the only one present."""
    stride = -(-slot_bytes // alignment) * alignment
    raw = np.zeros(n * stride + 2 * alignment, np.uint8)
    start = (-raw.ctypes.data) % alignment + skew
    return np.lib.stride_tricks.as_strided(raw[start:], shape=(n, slot_bytes), strides=(stride, 1))


@pytest.mark.parametrize("fault", ["too small", "unaligned", "not bytes"])
async def test_attach_refuses_slots_that_do_not_fit_the_format(rig, fault):
    """Handing a driver memory it cannot write into safely is refused at
    ``attach``, not discovered when a frame lands short of its slot or in the
    one after it. DCAM would take the pointers and write regardless."""
    fmt = rig.capture.frame_format()
    n, size, alignment = rig.n_slots, fmt.frame_bytes, fmt.slot_alignment
    if fault == "too small":
        slots = _slots(n, size - 1, alignment)
    elif fault == "unaligned":
        if alignment == 1:
            pytest.skip("this format asks for no alignment")
        slots = _slots(n, size, alignment, skew=1)
    else:
        slots = _slots(n, 2 * size, alignment).view(np.uint16)  # size elements: big enough

    with pytest.raises(ValueError):
        await rig.capture.attach(slots)


# --- K2: one frame per trigger, from slot 0 --------------------------------------


async def test_each_trigger_writes_one_frame_into_the_next_slot_from_slot_0(rig, buffers):
    """The role publishes frame ``i`` of an acquisition from slot ``i mod n``
    without looking at the pixels. A capture that began anywhere but slot 0,
    or wrote two slots for one trigger, would have every consumer read the
    wrong image under the right frame number."""
    writer = await _capturing(rig, buffers)
    n = rig.n_slots

    await rig.trigger(n - 1)

    assert await rig.capture.wait_frames(0, PATIENCE) == n - 1
    assert _written(writer) == [True] * (n - 1) + [False]


async def test_frames_wrap_to_slot_0_after_the_last_slot(rig, buffers):
    """The ring is reused: frame ``n`` goes to slot 0 again, and ``n + 1``
    to slot 1, leaving the others as they were."""
    writer = await _capturing(rig, buffers)
    n = rig.n_slots
    await rig.trigger(n)
    _blank(writer, *range(n))

    await rig.trigger(2)

    assert await rig.capture.wait_frames(n, PATIENCE) == n + 2
    assert _written(writer) == [True, True] + [False] * (n - 2)


# --- K3: no trigger, no frame ------------------------------------------------------


async def test_no_trigger_no_frame(rig, buffers):
    """A camera in the timing flow exposes only when the source says so. A
    frame without a trigger would be an image no waveform accounts for."""
    writer = await _capturing(rig, buffers)

    waiting = asyncio.create_task(rig.capture.wait_frames(0, PATIENCE))
    await rig.elapse(PATIENCE)

    assert waiting.done()
    assert await waiting == 0
    assert _written(writer) == [False] * rig.n_slots


async def test_triggers_before_start_and_after_stop_write_nothing(rig, buffers):
    """Pulses on the line outside a capture -- the DAQ's pre-roll, or a
    COOLDOWN still playing after the camera stopped -- land nowhere. Written
    into attached memory, they would overwrite frames a consumer is still
    reading after the acquisition ended."""
    writer = buffers(rig.capture.frame_format(), rig.n_slots)
    await rig.trigger(1)  # not attached
    await rig.capture.attach(writer.slots)
    await rig.trigger(1)  # attached, not started
    await rig.capture.start()
    await rig.capture.stop()
    await rig.trigger(1)  # stopped

    assert _written(writer) == [False] * rig.n_slots


# --- K4: waiting --------------------------------------------------------------------


async def test_wait_frames_returns_as_soon_as_a_frame_arrives(rig, buffers):
    """The role's loop sleeps in ``wait_frames``; it must wake per frame, not
    per timeout, or every consumer sees frames late."""
    await _capturing(rig, buffers)
    waiting = asyncio.create_task(rig.capture.wait_frames(0, LONG))
    await rig.elapse(PATIENCE)
    assert not waiting.done()

    await rig.trigger(1)

    assert waiting.done()
    assert await waiting == 1


async def test_wait_frames_returns_at_once_if_frames_are_already_there(rig, buffers):
    """Frames that arrived while the role was busy publishing are not waited
    for again."""
    await _capturing(rig, buffers)
    await rig.trigger(3)

    waiting = asyncio.create_task(rig.capture.wait_frames(1, LONG))
    await rig.elapse(0)

    assert waiting.done()
    assert await waiting == 3


async def test_stop_ends_a_wait_promptly(rig, buffers):
    """Stopping is how an acquisition ends, and the role's loop is always in
    ``wait_frames`` when it does. If ``stop`` did not end the wait, disarm
    would hang until the timeout -- or, for DCAM, until a frame that is never
    coming."""
    await _capturing(rig, buffers)
    await rig.trigger(2)
    waiting = asyncio.create_task(rig.capture.wait_frames(2, LONG))
    await rig.elapse(PATIENCE)
    assert not waiting.done()

    await rig.capture.stop()
    await rig.elapse(0)

    assert waiting.done()
    assert await waiting == 2


# --- K5: stamps and lost frames ---------------------------------------------------


async def test_stamps_are_consecutive_frame_by_frame(rig, buffers):
    """The stamps are what the role copies into the metadata table, and what
    a consumer uses to line a frame up with the waveform that caused it. A gap
    in ``hw_frame`` that was not a lost frame, or device time running
    backwards, would misplace every frame after it."""
    await _capturing(rig, buffers)
    await rig.trigger(3)
    fields = rig.capture.metadata().fields

    stamps = rig.capture.stamps(0, 3)

    assert stamps.dtype == STAMP_DTYPE
    assert len(stamps) == 3
    if MetaField.HW_FRAME in fields:
        assert list(np.diff(stamps["hw_frame"].astype(np.int64))) == [1, 1]
    if MetaField.DEVICE_TIME in fields:
        assert (np.diff(stamps["device_ticks"]) > 0).all()
    if MetaField.HOST_TIME in fields:
        assert (np.diff(stamps["host_time_ns"]) >= 0).all()


async def test_stamps_follow_their_frames_around_the_ring(rig, buffers):
    """Stamps are kept per slot, like the frames: after a wrap, the newest
    ``n_slots`` frames still have theirs."""
    await _capturing(rig, buffers)
    n = rig.n_slots
    await rig.trigger(n + 2)
    fields = rig.capture.metadata().fields

    stamps = rig.capture.stamps(2, n)

    if MetaField.HW_FRAME in fields:
        assert list(np.diff(stamps["hw_frame"].astype(np.int64))) == [1] * (n - 1)
    if MetaField.DEVICE_TIME in fields:
        assert (np.diff(stamps["device_ticks"]) > 0).all()


@pytest.mark.parametrize("which", ["not yet delivered", "overwritten"])
async def test_stamps_of_frames_not_in_the_slots_are_refused(rig, buffers, which):
    """A stamp returned for a frame the slots do not hold would be another
    frame's, silently."""
    await _capturing(rig, buffers)
    n = rig.n_slots
    await rig.trigger(n + 1)
    first = n + 1 if which == "not yet delivered" else 0

    with pytest.raises(ValueError):
        rig.capture.stamps(first, 1)


async def test_a_lost_frame_raises_frames_lost(rig, buffers):
    """Lost frames break the contract with every consumer downstream -- a
    stack with a plane missing is not a stack -- so they are an error, never
    a gap the role papers over. The loss shows at the next frame that does
    arrive, and stays shown."""
    if rig.lose is None:
        pytest.skip("this rig cannot make the hardware lose a frame")
    await _capturing(rig, buffers)
    await rig.trigger(2)
    await rig.lose(2)
    await rig.trigger(1)

    for _ in range(2):
        with pytest.raises(FramesLost) as caught:
            await rig.capture.wait_frames(2, PATIENCE)
        assert (caught.value.after, caught.value.lost) == (2, 2)


# --- K6: giving the memory back ---------------------------------------------------


@pytest.mark.parametrize("stopped", [True, False], ids=["stopped first", "while capturing"])
async def test_after_detach_no_reference_to_any_slot_remains(rig, buffers, stopped):
    """Once DCAM is attached it DMAs into the segment, so a segment closed or
    re-laid-out while the adapter still holds its slots is memory the driver
    writes into and nobody owns. The segment's own close is the check: it
    refuses while any view of it is alive. Detaching mid-capture is the
    death path, where nobody stopped first."""
    writer = await _capturing(rig, buffers)
    await rig.trigger(2)
    if stopped:
        await rig.capture.stop()

    await rig.capture.detach()

    buffers.close(writer)  # raises BufferError if a view survived


async def test_attaching_again_with_a_new_layout_works(rig, buffers):
    """Between acquisitions a setting may change the layout, so the role
    detaches, lays the segment out again, and attaches the new slots. The
    new capture starts afresh: from slot 0, counting from 0, across all of
    the new slots."""
    first = await _capturing(rig, buffers)
    await rig.trigger(2)
    await rig.capture.stop()
    await rig.capture.detach()
    buffers.close(first)
    n = rig.n_slots + 1
    writer = buffers(rig.capture.frame_format(), n)

    await rig.capture.attach(writer.slots)
    await rig.capture.start()
    await rig.trigger(n)

    assert await rig.capture.wait_frames(0, PATIENCE) == n
    assert _written(writer) == [True] * n


# --- K7: release is safe in every state ----------------------------------------------


async def _reach(state: str, rig, buffers) -> None:
    capture = rig.capture
    if state == "fresh":
        return
    writer = buffers(capture.frame_format(), rig.n_slots)
    await capture.attach(writer.slots)
    if state == "attached":
        return
    await capture.start()
    await rig.trigger(1)
    if state == "capturing":
        return
    await capture.stop()
    if state == "stopped":
        return
    await capture.detach()
    if state == "detached":
        return
    await capture.close()  # "closed"


STATES = ["fresh", "attached", "capturing", "stopped", "detached", "closed"]


@pytest.mark.parametrize("release", ["stop", "detach", "close"])
@pytest.mark.parametrize("state", STATES)
async def test_release_is_safe_in_every_state_and_repeatable(rig, buffers, state, release):
    """The role's teardown runs stop, detach and close on every exit path,
    including a device that failed half-way into an acquisition, and a
    fixture may run them again. Any of them raising would skip the release
    after it -- and an unreleased camera handle is what wedges DCAM."""
    await _reach(state, rig, buffers)
    call = getattr(rig.capture, release)

    await call()
    await call()


@pytest.mark.parametrize(
    "use",
    [
        lambda capture, writer: capture.frame_format(),
        lambda capture, writer: capture.metadata(),
        lambda capture, writer: capture.attach(writer.slots),
        lambda capture, writer: capture.start(),
        lambda capture, writer: capture.wait_frames(0, PATIENCE),
        lambda capture, writer: capture.stamps(0, 1),
    ],
    ids=["frame_format", "metadata", "attach", "start", "wait_frames", "stamps"],
)
async def test_a_closed_capture_refuses_to_be_used(rig, buffers, use):
    """Closing gives the device back. A capture that went on answering, or
    took memory again, would hold a camera its owner believes released."""
    writer = buffers(rig.capture.frame_format(), rig.n_slots)
    await rig.capture.close()

    with pytest.raises(RuntimeError):
        result = use(rig.capture, writer)
        if asyncio.iscoroutine(result):
            await result


# --- K8: attach and start in their order --------------------------------------------


async def test_attach_while_capturing_is_refused(rig, buffers):
    """Swapping memory under a running capture would have DMA land in one set
    of slots while the role publishes from the other. The running capture
    keeps the memory it has."""
    writer = await _capturing(rig, buffers)
    other = buffers(rig.capture.frame_format(), rig.n_slots)

    with pytest.raises(RuntimeError):
        await rig.capture.attach(other.slots)

    await rig.trigger(1)
    assert _written(writer)[0]
    assert not any(_written(other))


async def test_attach_over_memory_already_attached_is_refused(rig, buffers):
    """Memory is given back by ``detach`` and nothing else, so the role always
    knows which slots the hardware may write. A second attach that quietly
    replaced the first would leave the first's release unaccounted for."""
    writer = buffers(rig.capture.frame_format(), rig.n_slots)
    other = buffers(rig.capture.frame_format(), rig.n_slots)
    await rig.capture.attach(writer.slots)

    with pytest.raises(RuntimeError):
        await rig.capture.attach(other.slots)


async def test_start_with_nothing_attached_is_refused(rig):
    """A capture with nowhere to put frames would lose every one. Refused at
    ``start``, the mistake is the role's and says so, instead of surfacing as
    lost frames at the first trigger."""
    with pytest.raises(RuntimeError):
        await rig.capture.start()


# --- K9: a refusal lets go of the slots ------------------------------------------------


@pytest.mark.parametrize("refusal", ["does not fit", "already attached"])
async def test_a_refused_attach_does_not_hold_the_segment_open(rig, buffers, refusal):
    """K9. A refused call's error keeps the call's arguments alive, through
    its traceback, for as long as anyone holds the error, and a segment
    cannot close while a view of it is alive. So a caller closing its
    segment in a ``finally``, or a notebook keeping the last error, would
    get ``BufferError`` in place of the refusal, and a segment that stays
    mapped. An adapter drops the slots it was offered before its error
    leaves ``attach``. Both refusals here are ones every adapter makes."""
    fmt = rig.capture.frame_format()
    if refusal == "already attached":
        await rig.capture.attach(buffers(fmt, rig.n_slots).slots)
    writer = buffers(fmt, rig.n_slots)
    offered = writer.slots if refusal == "already attached" else writer.slots[:, : fmt.frame_bytes - 1]

    with pytest.raises((ValueError, RuntimeError)) as caught:
        await rig.capture.attach(offered)
    del offered

    buffers.close(writer)  # raises BufferError if the error holds the slots
    assert caught.value is not None  # still held: that is the case under test
