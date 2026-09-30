"""K1-K9 and Z1-Z4 -- UEyeFrameCapture on the uEye camera whose ID is in
STACSCOPE_UEYE: the FrameCapture port contract, and what the adapter must
do on the real camera that the fake cannot show.

RUN ON THE CAMERA'S MACHINE. This captures, triggered from software and
(with STACSCOPE_UEYE_TRIGGER) from the DAQ; the DAQ drives only that digital
line, clocked by a counter, never an analog output. Frames may be dark; no
light is needed. Nothing is put back, because nothing needs to be: every
open starts from the camera's defaults (Y8).

  K1-K9  The port contract, in two formats: 256 rows of RAW8, and the full
         sensor in RAW10. Software triggers, one at a time. The lost-frame
         test skips: the camera cannot be made to lose a frame on demand
         through the contract's rig (Z3 does it with the trigger line).
  Z1     In-flight depth, with measure_in_flight: triggered from software,
         and from the DAQ.
  Z2     The user's requirement through the adapter: the full sensor in
         RAW10, DAQ-triggered at 20 frames a second, every frame counted,
         in order, with no loss. The pixel clock is the lowest whose
         shortest trigger period fits in 90% of 50 ms. Ten seconds; set
         STACSCOPE_SOAK_SECONDS to run longer.
  Z3     The shortest trigger period, as the adapter reports it, against
         the camera: pulses 3% slower give every frame; 3% faster, the
         camera ignores some, and the adapter raises FramesLost.
  Z4     The adapter opens a camera whose capturing process was killed,
         waiting out IS_DEVICE_IN_USE (Y7).

Each adds its result to the "uEye findings" section at the end of the run;
paste that section back.

    PowerShell:  $env:STACSCOPE_UEYE = "1"
                 $env:STACSCOPE_UEYE_TRIGGER = "Dev1/port0/line2"
                 uv run --extra ueye --extra nidaqmx pytest tests/hardware/ueye/test_port.py
                 Remove-Item Env:STACSCOPE_UEYE, Env:STACSCOPE_UEYE_TRIGGER

    Git Bash:    STACSCOPE_UEYE=1 STACSCOPE_UEYE_TRIGGER=Dev1/port0/line2 \\
                     uv run --extra ueye --extra nidaqmx pytest tests/hardware/ueye/test_port.py

Without STACSCOPE_UEYE_TRIGGER, the DAQ-triggered tests skip.
"""

import asyncio
import contextlib
import os
import pathlib
import subprocess
import sys
import time
import uuid

import numpy as np
import pytest
import ueye_rig
from ueye_rig import GRACE, capture_with, software_trigger

from frame_capture_contract import *  # noqa: F403 - the contract's tests, Rig, buffers
from frame_capture_contract import Rig
from stacscope.adapters.in_flight import measure_in_flight
from stacscope.core.buffer import ImageBuffer
from stacscope.core.devices.image_streaming import layout_for
from stacscope.core.ports import FramesLost

pytestmark = pytest.mark.timeout(300)

SOAK = float(os.environ.get("STACSCOPE_SOAK_SECONDS", "10"))
"""Seconds Z2 triggers for."""

ROWS_256 = (0, 384, 1280, 256)
FULL = None

FORMATS = {
    "raw8-rows256": ("RAW8", ROWS_256),
    "raw10-full": ("RAW10", FULL),
}

TICK = 1e-7


@pytest.fixture(params=FORMATS.values(), ids=FORMATS.keys())
async def rig(request, camera):
    pixel_format, aoi = request.param
    async with capture_with(camera, pixel_format, aoi, exposure_ms=1.0) as capture:

        async def elapse(seconds: float) -> None:
            await asyncio.sleep(seconds + GRACE)

        yield Rig(capture, software_trigger(capture), elapse)


def _segment(fmt, n_slots):
    layout = layout_for(fmt, n_slots)
    writer = ImageBuffer.create(
        "z" + uuid.uuid4().hex[:20], capacity_bytes=layout.resolve().total_bytes, max_slots=n_slots
    )
    writer.configure(layout)
    return writer


async def _count(capture, expected: int, patience: float) -> int:
    """Wait for frames as the role does, until ``expected`` have come or
    none has for ``patience`` seconds. Returns how many came; raises what
    the capture raises."""
    seen = 0
    while seen < expected:
        delivered = await capture.wait_frames(seen, patience)
        if delivered == seen:
            break
        seen = delivered
    return seen


# --- Z1: in-flight depth -------------------------------------------------------------------


@pytest.mark.parametrize("trigger", ["software", "daq"])
async def test_z1_in_flight_depth(camera, trigger, report):
    """The depth the adapter declares is the role's promise to consumers: a
    slot within it of being reused is not read. The driver writes only into
    memories handed back, and the adapter hands each back once its info is
    read, so the depth is how far the driver can run ahead of the count in
    that time. 256 rows of RAW8, 1 ms exposure, paced at 1.5 times the
    shortest trigger period. The result is a measurement, not a verdict:
    declare the largest seen plus a margin."""
    line = os.environ.get("STACSCOPE_UEYE_TRIGGER", "")
    if trigger == "daq" and not line:
        pytest.skip("set STACSCOPE_UEYE_TRIGGER to the DAQ line wired to the uEye's trigger input")
    frames, scan = 64, 8
    async with capture_with(camera, "RAW8", ROWS_256, exposure_ms=1.0) as capture:
        period = 1.5 * capture.shortest_trigger_period()
        writer = _segment(capture.frame_format(), frames + 2 * scan)
        try:
            with contextlib.ExitStack() as stack:
                if trigger == "daq":
                    from daq_pulses import daq_pulses

                    fire = stack.enter_context(daq_pulses(line, period, frames + scan))
                else:
                    fire = _paced(capture, period)
                result = await measure_in_flight(
                    capture, writer.slots, frames=frames, fire=fire, sentinel=0xA5, scan=scan
                )
        finally:
            writer.close()
    report(
        f"depth {result.depth}, partial {result.partial}, frames {result.frames}, polls {result.polls}; "
        f"period {period * 1e3:.3f} ms"
    )
    assert result.frames >= frames


def _paced(capture, interval):
    """A ``fire`` that sends a software trigger once ``interval`` has passed
    since the last, and otherwise only yields, so the tool keeps looking."""
    last = -float("inf")

    async def fire():
        nonlocal last
        now = time.perf_counter()
        if now - last >= interval:
            capture.fire_trigger()
            last = now
        await asyncio.sleep(0)

    return fire


# --- Z2: the user's requirement, through the adapter ---------------------------------------------


async def test_z2_raw10_full_frame_triggered_at_20_fps(camera, trigger_line, report):
    """The full sensor in RAW10, DAQ-triggered at 20 frames a second,
    sustained (user, 2026-09-30), now through the adapter: its consumer
    thread, its slot check, its frame-number check and its missed-trigger
    check all run. The pixel clock is chosen with the adapter's own
    shortest trigger period, as a client would."""
    from daq_pulses import daq_pulses

    rate = 20.0
    n = int(rate * SOAK)
    async with capture_with(camera, "RAW10", exposure_ms=10.0) as capture:
        chosen = None
        for mhz in capture.pixel_clocks():
            capture.set_pixel_clock(mhz)
            capture.set_exposure(10.0)
            if capture.shortest_trigger_period() <= 0.9 / rate:
                chosen = mhz
                break
        assert chosen is not None, "no pixel clock allows 20 fps in RAW10 at full frame"
        shortest = capture.shortest_trigger_period()
        exposure = capture.exposure()
        fmt = capture.frame_format()
        writer = _segment(fmt, 20)
        try:
            await capture.attach(writer.slots)
            await capture.start()
            with daq_pulses(trigger_line, 1 / rate, n) as fire:
                await fire()
                got = await _count(capture, n, 2.0)
                await asyncio.sleep(0.3)
                extra = await capture.wait_frames(got, 0.3)
            stamps = capture.stamps(max(0, got - 20), min(got, 20))
            await capture.stop()
            await capture.detach()
        finally:
            writer.close()
    intervals = np.diff(stamps["device_ticks"]) * TICK
    report(
        f"{fmt.shape[1]}x{fmt.shape[0]} RAW10 at {chosen} MHz, exposure {exposure:.3f} ms, shortest trigger "
        f"period {shortest * 1e3:.3f} ms; {n} pulses at {rate:.0f}/s, {got} frames; last intervals "
        f"{np.median(intervals) * 1e3:.3f} ms median"
    )
    assert got == n
    assert extra == n, "more frames than pulses"
    assert set(np.diff(stamps["hw_frame"].astype(np.int64)).tolist()) == {1}


# --- Z3: the shortest trigger period ------------------------------------------------------------


@pytest.mark.parametrize("factor", [1.03, 0.97])
async def test_z3_triggers_faster_than_the_shortest_period_are_lost_frames(camera, trigger_line, report, factor):
    """The adapter's shortest trigger period is what a client checks its
    waveform against. 3% slower than it, every pulse must give a frame; 3%
    faster, the camera ignores some (every other, on the probe), and the
    adapter must raise FramesLost rather than count fewer frames in silence.
    Full frame RAW10 at 31 MHz and 10 ms, the setting Y9 first lost frames
    at."""
    from daq_pulses import daq_pulses

    n = 40
    async with capture_with(camera, "RAW10", pixel_clock=31, exposure_ms=10.0) as capture:
        shortest = capture.shortest_trigger_period()
        period = round(shortest * factor, 5)
        writer = _segment(capture.frame_format(), 16)
        lost = None
        try:
            await capture.attach(writer.slots)
            await capture.start()
            with daq_pulses(trigger_line, period, n) as fire:
                await fire()
                try:
                    got = await _count(capture, n, 1.0)
                except FramesLost as error:
                    lost = error
                    got = None
            await capture.stop()
            await capture.detach()
        finally:
            writer.close()
    report(
        f"shortest {shortest * 1e3:.3f} ms, pulses every {period * 1e3:.3f} ms ({factor}x): "
        + (f"FramesLost after {lost.after}, {lost.lost} lost" if lost else f"{got} of {n} frames")
    )
    if factor > 1:
        assert lost is None and got == n
    else:
        assert lost is not None and lost.lost >= 1


# --- Z4: opening after a crash -------------------------------------------------------------------


async def test_z4_the_adapter_opens_a_camera_whose_process_was_killed(camera, report):
    """A device's process can die with the camera capturing, and the user
    relaunches it. The camera answers IS_DEVICE_IN_USE for about 1.5 s
    after (Y7); the adapter waits that out rather than failing the launch."""
    child = subprocess.Popen(
        [sys.executable, str(pathlib.Path(ueye_rig.__file__)), str(camera)],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONWARNINGS": "ignore"},
    )
    try:
        line = child.stdout.readline()
        assert line.strip() == "capturing", f"the child never captured: {line!r}"
        child.kill()
        child.wait(10)
    finally:
        if child.poll() is None:
            child.kill()
    started = time.monotonic()
    async with capture_with(camera, "RAW8", ROWS_256, exposure_ms=1.0) as capture:
        opened = time.monotonic() - started
        writer = _segment(capture.frame_format(), 4)
        try:
            await capture.attach(writer.slots)
            await capture.start()
            await software_trigger(capture)(2)
            got = await capture.wait_frames(0, 1.0)
            await capture.detach()
        finally:
            writer.close()
    report(f"opened {opened:.2f} s after the kill; {got} frames")
    assert got == 2
