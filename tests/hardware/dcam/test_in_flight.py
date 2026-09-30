"""M1 -- how far ahead of the frame count the camera's grabber writes: its
in-flight depth, measured with measure_in_flight.

RUN ON THE CAMERA'S MACHINE. This captures, as the contract suite does, and
puts back every setting it changes. Frames may be dark; no light is needed.

The depth is what DcamFrameCapture must be told as ``in_flight``: the role
never publishes a frame whose slot the grabber may already be rewriting.
The result is a measurement, not a verdict: declare the largest depth seen
plus whatever margin you want, since a depth the camera never showed cannot
be ruled out by watching.

It is measured in each frame format of the contract suite, under three
triggers:

  software  one frame at a time, paced at 1.5 times the camera's minimum
            trigger interval, each fired from a worker thread: the call
            blocks until the camera can take it, and the tool must go on
            looking meanwhile;
  internal  the camera free-running at its fastest, which is the case that
            stresses the grabber (about 800 frames a second at 256 rows);
  daq       pulses from the DAQ into the camera's trigger input, hardware-
            timed as on the instrument, at 1.5 times the minimum trigger
            interval. Only with STACSCOPE_CAMERA_TRIGGER set to the digital
            line wired to the camera (Dev1/port0/line0 on the dev box),
            and with the nidaqmx extra: the line is clocked by the DAQ's
            counter 0, and no analog output is driven.

Every slot is first filled with a sentinel byte, 0xA5 unless
STACSCOPE_SENTINEL says otherwise (as a number, such as 0x5A). A dark frame
at the 1 ms exposure used sits near the camera's offset, so no written row
can read as the sentinel throughout.

    PowerShell:  $env:STACSCOPE_CAMERA = "0"
                 $env:STACSCOPE_CAMERA_TRIGGER = "Dev1/port0/line0"
                 uv run --extra nidaqmx pytest tests/hardware/dcam/test_in_flight.py
                 Remove-Item Env:STACSCOPE_CAMERA, Env:STACSCOPE_CAMERA_TRIGGER

    Git Bash:    export STACSCOPE_CAMERA=0 STACSCOPE_CAMERA_TRIGGER=Dev1/port0/line0
                 uv run --extra nidaqmx pytest tests/hardware/dcam/test_in_flight.py

Without STACSCOPE_CAMERA_TRIGGER (and the nidaqmx extra), the DAQ condition
skips and the rest run.

Each condition adds its result to the "camera findings" section at the end
of the run; paste that section back. Takes about a minute.
"""
# For end use and not for commercial purposes.

import asyncio
import contextlib
import os
import time
import uuid

import pytest
from daq_pulses import daq_pulses
from dcam_rig import FORMATS, camera_with

from stacscope.adapters.in_flight import measure_in_flight
from stacscope.core.buffer import ImageBuffer
from stacscope.core.devices.image_streaming import layout_for

pytestmark = pytest.mark.timeout(120)

SENTINEL = int(os.environ.get("STACSCOPE_SENTINEL", "0xA5"), 0)
SCAN = 8
BUDGET = 400 * 2**20
"""Bytes of slots a condition may use; full frames are 8 MiB each."""

MAX_FRAMES = 400

TRIGGERS = {
    "software": [("TRIGGER SOURCE", "SOFTWARE"), ("EXPOSURE TIME", 0.001)],
    "internal": [("TRIGGER SOURCE", "INTERNAL"), ("EXPOSURE TIME", 0.001)],
    "daq": [("TRIGGER SOURCE", "EXTERNAL"), ("EXPOSURE TIME", 0.001)],
}

LINE = os.environ.get("STACSCOPE_CAMERA_TRIGGER", "")
"""The DAQ's digital line wired to the camera's trigger input, if any."""


@pytest.mark.parametrize("trigger", TRIGGERS)
@pytest.mark.parametrize("fmt_name", FORMATS)
async def test_m1_in_flight_depth(camera, fmt_name, trigger, report):
    if trigger == "daq" and not LINE:
        pytest.skip("set STACSCOPE_CAMERA_TRIGGER to the DAQ line wired to the camera's trigger input")
    async with camera_with(camera, TRIGGERS[trigger] + FORMATS[fmt_name]) as capture:
        fmt = capture.frame_format()
        interval = capture.get("TIMING MIN TRIGGER INTERVAL")
        rate = capture.get("INTERNAL FRAME RATE")
        frames = min(MAX_FRAMES, BUDGET // fmt.frame_bytes - 2 * SCAN)
        if trigger == "software":
            frames = min(frames, 64)
        writer = _segment(fmt, frames + 2 * SCAN)
        try:
            with contextlib.ExitStack() as stack:
                if trigger == "software":
                    fire = _paced(capture, 1.5 * interval)
                elif trigger == "daq":
                    # Pulses past the last frame measured land on a stopped
                    # camera; no more than the scan's worth, so nothing wraps.
                    fire = stack.enter_context(daq_pulses(LINE, 1.5 * interval, frames + SCAN))
                else:
                    fire = _yield
                result = await measure_in_flight(
                    capture, writer.slots, frames=frames, fire=fire, sentinel=SENTINEL, scan=SCAN
                )
        finally:
            writer.close()
        report(
            f"depth {result.depth}, partial {result.partial}, frames {result.frames}, "
            f"polls {result.polls}; shape {fmt.shape}, min trigger interval "
            f"{interval * 1e3:.3f} ms, internal frame rate {rate:.1f}/s"
        )
        assert result.frames >= frames


def _segment(fmt, n_slots):
    layout = layout_for(fmt, n_slots)
    writer = ImageBuffer.create(
        "m" + uuid.uuid4().hex[:20], capacity_bytes=layout.resolve().total_bytes, max_slots=n_slots
    )
    writer.configure(layout)
    return writer


async def _yield():
    await asyncio.sleep(0)


def _paced(capture, interval):
    """A ``fire`` that sends a software trigger once ``interval`` has passed
    since the last, and otherwise only yields, so the tool keeps looking
    while each frame is exposed and read out. The trigger goes from a worker
    thread, one at a time: DCAM's call returns only once the camera can take
    it, up to a frame time later."""
    last = -float("inf")
    pending: asyncio.Future | None = None

    async def fire():
        nonlocal last, pending
        now = time.perf_counter()
        if now - last >= interval and (pending is None or pending.done()):
            if pending is not None:
                pending.result()  # a trigger that failed fails the measurement
            pending = asyncio.ensure_future(asyncio.to_thread(capture.fire_trigger))
            last = now
        await asyncio.sleep(0)

    return fire
