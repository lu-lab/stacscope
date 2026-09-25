"""M2-M5 -- what the camera does where DCAM's reference is silent, and what
the adapter's design rests on.

RUN ON THE CAMERA'S MACHINE. This captures, as the contract suite does, and
puts back every setting it changes. Frames may be dark; no light is needed.

  M2  The adapter at the camera's fastest: 256 rows in area readout, free-
      running (about 800 frames a second), read the way the image-streaming
      role reads it, one wait per frame. Every frame must arrive, with no
      gap in its frame stamps. Reports how often a wait returned more than
      one frame, the frame interval from the time stamps, and whether any
      frame stamp read DCAMCONST_FRAMESTAMP_MISMATCH.
  M3  Whether frame stamps restart at 0 for each capture (D12), within one
      session and across sessions.
  M4  Whether a frame that arrives with no wait running is still seen by
      the next wait (whether FRAMEREADY latches).
  M5  Whether releasing the frame memory also releases the stamp buffers,
      or each kind must be released on its own.

Each adds its result to the "camera findings" section at the end of the
run; paste that section back.

    PowerShell:  $env:STACSCOPE_CAMERA = "0"
                 uv run pytest tests/hardware/dcam/test_behaviour.py
                 Remove-Item Env:STACSCOPE_CAMERA

    Git Bash:    STACSCOPE_CAMERA=0 uv run pytest tests/hardware/dcam/test_behaviour.py

Takes about half a minute. M2 streams for five seconds; set
STACSCOPE_SOAK_SECONDS to stream for longer.
"""
# For end use and not for commercial purposes.

import asyncio
import ctypes
import os
import time
import uuid

import numpy as np
import pytest
from dcam_rig import FORMATS, SOFTWARE, camera_with, delivered, software_trigger

from stacscope.adapters.dcam import _api
from stacscope.core.buffer import ImageBuffer
from stacscope.core.devices.image_streaming import layout_for

pytestmark = pytest.mark.timeout(300)

SOAK = float(os.environ.get("STACSCOPE_SOAK_SECONDS", "5"))

FREE_RUNNING = [("TRIGGER SOURCE", "INTERNAL"), ("EXPOSURE TIME", 0.001)]

MISMATCH = 0xFFFFFFFF
"""DCAMCONST_FRAMESTAMP_MISMATCH: a frame stamp DCAM could not match."""


class _Segment:
    def __init__(self, fmt, n_slots):
        layout = layout_for(fmt, n_slots)
        self.writer = ImageBuffer.create(
            "b" + uuid.uuid4().hex[:20], capacity_bytes=layout.resolve().total_bytes, max_slots=n_slots
        )
        self.writer.configure(layout)

    def __enter__(self):
        return self.writer

    def __exit__(self, *exc):
        self.writer.close()


async def test_m2_every_frame_arrives_at_800_frames_a_second(camera, report):
    """The role waits once per frame, in a worker thread. At 800 frames a
    second that is a wait every 1.25 ms; if the loop cannot keep up, frames
    arrive in batches, which costs only latency so long as the ring covers
    it, and a lost frame is fatal. The ring here holds a quarter of a
    second."""
    async with camera_with(camera, FREE_RUNNING + FORMATS["256 rows"]) as capture:
        fmt = capture.frame_format()
        rate = capture.get("INTERNAL FRAME RATE")
        n_slots = 200
        with _Segment(fmt, n_slots) as writer:
            await capture.attach(writer.slots)
            await capture.start()
            seen = 0
            returns = batched = biggest = 0
            hw_frames: list[np.ndarray] = []
            ticks: list[np.ndarray] = []
            cpu = time.process_time()
            end = time.perf_counter() + SOAK
            try:
                while time.perf_counter() < end:
                    count = await capture.wait_frames(seen, 1.0)
                    returns += 1
                    new = count - seen
                    assert new > 0, "no frame for a second from a free-running camera"
                    assert new < n_slots, f"{new} frames in one return: the ring was overrun"
                    batched += new > 1
                    biggest = max(biggest, new)
                    stamps = capture.stamps(seen, new)
                    hw_frames.append(stamps["hw_frame"].copy())
                    ticks.append(stamps["device_ticks"].copy())
                    seen = count
            finally:
                cpu = time.process_time() - cpu
                await capture.stop()
                await capture.detach()
        hw = np.concatenate(hw_frames).astype(np.int64)
        tk = np.concatenate(ticks)
        intervals = np.diff(tk)
        report(
            f"{seen} frames in {SOAK:.0f} s at {rate:.1f}/s set; {returns} returns, "
            f"{batched} with more than one frame, largest {biggest}; CPU {cpu / SOAK:.2f} cores"
        )
        report(
            f"frame interval from time stamps: median {np.median(intervals)} us, "
            f"min {intervals.min()} us, max {intervals.max()} us; "
            f"frame stamps from {hw[0]}, mismatches {(hw == MISMATCH).sum()}"
        )
        assert (np.diff(hw) == 1).all(), "a gap in the frame stamps"


async def test_m3_whether_frame_stamps_restart_for_each_capture(camera, report):
    """D12 numbers frames from 0 for each acquisition, provisionally, and the
    adapter's gap check compares stamps only within one capture. What the
    Flash does decides whether D12 stands."""
    firsts = []
    for session in range(2):
        async with camera_with(camera, SOFTWARE + FORMATS["256 rows"]) as capture:
            trigger = software_trigger(capture)
            with _Segment(capture.frame_format(), 4) as writer:
                for _ in range(2):
                    await capture.attach(writer.slots)
                    await capture.start()
                    await trigger(3)
                    assert await capture.wait_frames(0, 1.0) == 3
                    stamps = capture.stamps(0, 3)
                    hw = [int(s) for s in stamps["hw_frame"]]
                    firsts.append((session, hw, int(stamps["device_ticks"][0])))
                    await capture.stop()
                    await capture.detach()
    for session, hw, tick in firsts:
        report(f"session {session}: frame stamps {hw}, first time stamp {tick} us")


async def test_m4_whether_a_frame_with_no_wait_running_is_seen_by_the_next_wait(camera, report):
    """The adapter reads the frame count before every wait, so a frame that
    lands between two waits is never missed. Whether DCAM latches it
    decides how late it can be seen: at once, or only at the next frame or
    the end of the wait's slice. At slow trigger rates that could leave the
    last frame of an acquisition up to a slice late."""
    async with camera_with(camera, SOFTWARE + FORMATS["256 rows"]) as capture:
        with _Segment(capture.frame_format(), 4) as writer:
            await capture.attach(writer.slots)
            await capture.start()
            try:
                for trial in range(3):
                    before = delivered(capture)
                    capture.fire_trigger()
                    deadline = time.monotonic() + 2.0
                    while delivered(capture) == before and time.monotonic() < deadline:
                        time.sleep(0.001)
                    assert delivered(capture) == before + 1
                    await asyncio.sleep(0.02)  # the frame is well past
                    start = _api.DCAMWAIT_START.new(
                        eventmask=_api.CAPEVENT_FRAMEREADY, timeout=300
                    )
                    t0 = time.perf_counter()
                    code = await asyncio.to_thread(
                        capture._lib.dcamwait_start, capture._hwait, ctypes.pointer(start)
                    )
                    waited = time.perf_counter() - t0
                    name = _api.ERROR_NAMES.get(code, "success" if code > 0 else f"{code & 0xFFFFFFFF:#x}")
                    report(f"trial {trial}: the next wait returned {name} after {waited * 1e3:.1f} ms")
            finally:
                await capture.stop()
                await capture.detach()


async def test_m5_whether_each_kind_of_attached_memory_is_released_on_its_own(camera, report):
    """The adapter releases stamps first, then frames, each on its own. If
    releasing the frames let go of the stamp buffers too, the later
    releases might fail; if it did not, a single release would leave DCAM
    writing stamps into memory the adapter has let go of. On the Flash
    (2026-09-25) DCAM refuses none of it -- not a repeated release, not a
    stamp buffer attached over one already attached -- so the codes cannot
    tell which; this records them, and that releasing every kind is
    harmless."""
    async with camera_with(camera, SOFTWARE + FORMATS["256 rows"]) as capture:
        with _Segment(capture.frame_format(), 4) as writer:
            await capture.attach(writer.slots)
            steps = [
                ("release frame", lambda: _release(capture, _api.ATTACHKIND_FRAME)),
                ("attach timestamp again", lambda: _attach_again(capture, _api.ATTACHKIND_TIMESTAMP)),
                ("release timestamp", lambda: _release(capture, _api.ATTACHKIND_TIMESTAMP)),
                ("release framestamp", lambda: _release(capture, _api.ATTACHKIND_FRAMESTAMP)),
                ("release frame again", lambda: _release(capture, _api.ATTACHKIND_FRAME)),
            ]
            codes = [f"{name}: {_outcome(step())}" for name, step in steps]
            # Released by hand, so the adapter must not release them again.
            capture._forget()
            # The same memory attaches again: nothing of the first attach is left.
            await capture.attach(writer.slots)
            await capture.detach()
    report(", ".join(codes))


def _release(capture, kind):
    return capture._lib.dcambuf_release(capture._hdcam, kind)


def _attach_again(capture, kind):
    """Attach the adapter's own stamp buffer for ``kind`` a second time."""
    pointers = capture._pointers[kind]
    attach = _api.DCAMBUF_ATTACH.new(
        iKind=kind,
        buffer=ctypes.cast(pointers, ctypes.POINTER(ctypes.c_void_p)),
        buffercount=len(pointers),
    )
    return capture._lib.dcambuf_attach(capture._hdcam, ctypes.pointer(attach))


def _outcome(code: int) -> str:
    return "success" if code > 0 else f"{code & 0xFFFFFFFF:#010x}"
