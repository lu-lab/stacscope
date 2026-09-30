"""W1-W3 -- the UEyeCamera device on the uEye camera whose ID is in
STACSCOPE_UEYE: settings by message, and acquisitions streamed into its
buffer as the instrument runs them.

RUN ON THE CAMERA'S MACHINE. This captures, triggered from software and
from the DAQ (STACSCOPE_UEYE_TRIGGER); the DAQ drives only that digital
line, clocked by a counter, never an analog output. Frames may be dark.
Nothing is put back, because nothing needs to be: every open starts from
the camera's defaults (Y8).

  W1  Settings by message on the camera opened from a worker thread, each
      answered as the camera took it; the shortest trigger period; a new
      AOI refused during an acquisition, which goes on.
  W2  The user's requirement, end to end: a client picks the pixel clock by
      asking the shortest trigger period, then an acquisition of the full
      sensor in RAW10, DAQ-triggered at 20 frames a second, is published
      frame by frame to a reader of the buffer, with nothing lost. Ten
      seconds; set STACSCOPE_SOAK_SECONDS to run longer.
  W3  Pulses faster than the shortest trigger period: the camera ignores
      some, and the device ends the acquisition with a fatal Fault instead
      of publishing a stack with planes missing.

Each adds what it saw to the "uEye findings" section at the end of the run;
paste that section back.

    PowerShell:  $env:STACSCOPE_UEYE = "1"
                 $env:STACSCOPE_UEYE_TRIGGER = "Dev1/port0/line2"
                 uv run --extra ueye --extra nidaqmx pytest tests/hardware/ueye/test_device.py
                 Remove-Item Env:STACSCOPE_UEYE, Env:STACSCOPE_UEYE_TRIGGER

    Git Bash:    STACSCOPE_UEYE=1 STACSCOPE_UEYE_TRIGGER=Dev1/port0/line2 \\
                     uv run --extra ueye --extra nidaqmx pytest tests/hardware/ueye/test_device.py
"""

import asyncio
import contextlib
import os
import time
import uuid

import numpy as np
import pytest
from image_streaming_contract import Listener
from ueye_rig import GRACE, software_trigger

from stacscope.adapters.ueye import (
    AOI,
    CameraSettings,
    Exposure,
    GetCameraSettings,
    PixelClock,
    SetAOI,
    SetExposure,
    SetPixelClock,
    UEyeCamera,
)
from stacscope.core.actors import Actor, Fault, Rejected, Subscribe, ask, spawn
from stacscope.core.buffer import ImageBuffer, TimeSource
from stacscope.core.protocol import (
    DeviceReady,
    DeviceStopped,
    GetShortestTriggerPeriod,
    ShortestTriggerPeriod,
    StartDevice,
    StopDevice,
)

pytestmark = pytest.mark.timeout(300)

SOAK = float(os.environ.get("STACSCOPE_SOAK_SECONDS", "10"))

N_SLOTS = 20
CAPACITY = N_SLOTS * 1280 * 1024 * 2  # twenty full frames of RAW10

ROWS_256 = (0, 384, 1280, 256)


@contextlib.asynccontextmanager
async def ueye_camera(camera: int, report, **config):
    """A UEyeCamera launched on the camera with ``config``, and a listener
    subscribed; stopped afterwards, whatever happened."""
    parent = Actor()
    spawn(parent, name="coordinator")
    device = UEyeCamera(
        "w" + uuid.uuid4().hex[:20],
        camera=camera,
        n_slots=N_SLOTS,
        capacity_bytes=CAPACITY,
        max_slots=N_SLOTS,
        **config,
    )
    opening = time.monotonic()
    ref = parent.spawn_child(device, name="ueye")
    listener = Listener()
    ref.tell(Subscribe(spawn(listener)))
    try:
        await asyncio.wait(
            [asyncio.ensure_future(device.started.wait()), asyncio.ensure_future(device.stopped.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert device.started.is_set(), f"the device did not launch: {device.exit_error!r}"
        report(f"launched in {time.monotonic() - opening:.2f} s")
        yield device, ref, listener
    finally:
        if not device.stopped.is_set():
            device.stop()
            await device.stopped.wait()
        listener.stop()
        parent.stop()


async def _ask(ref, msg):
    return await ask(ref, msg, timeout=10.0)


def _faults(listener) -> list[Fault]:
    return [m for m in listener.heard if isinstance(m, Fault)]


async def test_w1_settings_by_message_on_the_camera(camera, report):
    """The camera is opened in a worker thread, and everything after runs on
    the event loop's thread; this is where the driver would mind. Each
    answer is what the camera took. During an acquisition, a new AOI is
    refused and the acquisition goes on; the exposure changes at once."""
    async with ueye_camera(camera, report, pixel_format="RAW8", aoi=ROWS_256) as (device, ref, listener):
        settings = await _ask(ref, GetCameraSettings())
        clock = await _ask(ref, SetPixelClock(40))
        exposure = await _ask(ref, SetExposure(2.5))
        period = await _ask(ref, GetShortestTriggerPeriod())
        report(f"{settings!r}; {clock!r}; {exposure!r}; {period!r}")
        assert isinstance(settings, CameraSettings) and settings.aoi == ROWS_256
        assert clock == PixelClock(40)
        assert isinstance(exposure, Exposure) and abs(exposure.ms - 2.5) < 0.1
        assert isinstance(period, ShortestTriggerPeriod)
        assert period.seconds == pytest.approx(device.capture.shortest_trigger_period())

        assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
        refused = await _ask(ref, SetAOI(0, 0, 640, 480))
        during = await _ask(ref, SetExposure(1.0))
        await software_trigger(device.capture)(3)
        reader = ImageBuffer.open(device.buffer_name)
        try:
            published = reader.published() - reader.acquisition_start
        finally:
            reader.close()
        assert await _ask(ref, StopDevice()) == DeviceStopped()
    report(f"while armed: {refused!r}; {during!r}; {published} frames published")
    assert isinstance(refused, Rejected)
    assert isinstance(during, Exposure) and abs(during.ms - 1.0) < 0.1
    assert published == 3
    assert _faults(listener) == []


async def test_w2_raw10_full_frame_at_20_fps_through_the_device(camera, trigger_line, report):
    """The user's requirement, as a client would run it: choose the pixel
    clock by asking the camera its shortest trigger period, then acquire.
    A reader of the buffer sees every frame published, stamped with the
    camera's clock and frame number, under a header declaring two frames in
    flight."""
    from daq_pulses import daq_pulses

    rate = 20.0
    n = int(rate * SOAK)
    async with ueye_camera(camera, report, pixel_format="RAW10", exposure_ms=10.0) as (device, ref, listener):
        settings = await _ask(ref, GetCameraSettings())
        chosen = period = None
        for mhz in settings.pixel_clocks_mhz:
            await _ask(ref, SetPixelClock(mhz))
            await _ask(ref, SetExposure(10.0))
            period = (await _ask(ref, GetShortestTriggerPeriod())).seconds
            if period <= 0.9 / rate:
                chosen = mhz
                break
        assert chosen is not None, "no pixel clock allows 20 fps in RAW10 at full frame"
        assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
        reader = ImageBuffer.open(device.buffer_name)
        try:
            with daq_pulses(trigger_line, 1 / rate, n) as fire:
                await fire()
                deadline = time.monotonic() + n / rate + 3.0
                while reader.published() - reader.acquisition_start < n and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                await asyncio.sleep(GRACE)
            assert await _ask(ref, StopDevice()) == DeviceStopped()
            published = reader.published() - reader.acquisition_start
            in_flight = reader._layout.in_flight
            source = reader.metadata.device_time_source
            # The frames still readable: the newest, short of those in flight.
            last = reader.acquisition_start + published
            first = max(reader.acquisition_start, last - N_SLOTS + in_flight)
            meta = reader.meta[[reader.slot_of(i) for i in range(first, last)]]  # a copy
        finally:
            reader.close()
    ticks = np.diff(meta["device_ticks"].astype(np.int64)) * 1e-7
    report(
        f"pixel clock {chosen} MHz, shortest trigger period {period * 1e3:.3f} ms; {n} pulses, "
        f"{published} published; in flight {in_flight}; {source!r}; last intervals "
        f"{np.median(ticks) * 1e3:.3f} ms median"
    )
    assert published == n
    assert _faults(listener) == []
    assert in_flight == 2
    assert source == TimeSource.CAMERA
    assert set(np.diff(meta["hw_frame"].astype(np.int64)).tolist()) == {1}


async def test_w3_triggers_too_fast_end_the_acquisition_with_a_fatal_fault(camera, trigger_line, report):
    """The camera ignores a trigger that comes while it is busy, and a
    stack with a plane missing is not a stack. So pulses faster than the
    shortest trigger period must end publishing with a fatal Fault, which
    the Coordinator turns into an abort."""
    from daq_pulses import daq_pulses

    async with ueye_camera(camera, report, pixel_format="RAW10", pixel_clock=31, exposure_ms=10.0) as (
        device,
        ref,
        listener,
    ):
        period = (await _ask(ref, GetShortestTriggerPeriod())).seconds
        assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
        with daq_pulses(trigger_line, round(0.97 * period, 5), 40) as fire:
            await fire()
            await asyncio.sleep(40 * period + 0.5)
        stopped = await _ask(ref, StopDevice())
    faults = _faults(listener)
    report(f"shortest {period * 1e3:.3f} ms, pulses at 0.97 of it: {[f.reason for f in faults]}")
    assert stopped == DeviceStopped()
    assert faults and faults[0].fatal
    assert "FramesLost" in faults[0].reason
