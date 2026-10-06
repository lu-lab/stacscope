"""W1-W4 and DPH2 -- the UEyeCamera device on the uEye camera whose ID is in
STACSCOPE_UEYE: properties by message, and acquisitions streamed into its
buffer as the instrument runs them.

RUN ON THE CAMERA'S MACHINE. This captures, triggered from software and
from the DAQ (STACSCOPE_UEYE_TRIGGER); the DAQ drives only that digital
line, clocked by a counter, never an analog output. Frames may be dark.
Nothing is put back, because nothing needs to be: every open starts from
the camera's defaults (Y8).

  W1  Properties by message on the camera opened from a worker thread,
      each answered as the camera took it; the shortest trigger period; a
      new AOI refused during an acquisition, which goes on.
  W2  The user's requirement, end to end: a client picks the pixel clock by
      asking the shortest trigger period, then an acquisition of the full
      sensor in RAW10, DAQ-triggered at 20 frames a second, is published
      frame by frame to a reader of the buffer, with nothing lost. Ten
      seconds; set STACSCOPE_SOAK_SECONDS to run longer.
  W3  Pulses faster than the shortest trigger period: the camera ignores
      some, and the device ends the acquisition with a fatal Fault instead
      of publishing a stack with planes missing.
  W4  W2, recorded: the TIFF file holds every frame published, with the
      camera's clock on each page, and its last frames equal the buffer's.
      Written in a folder of its own under STACSCOPE_RECORD_DIR
      (E:/stacscope_tests by default, the dev box's data drive), deleted
      after the test.
  DPH2  The table's limits are the driver's: a slower pixel clock lowers
      the fastest frame rate and the longest exposure, as the fake
      predicts. Reports the table, and how long a read and a set take
      (DPH3).

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
import pathlib
import shutil
import time
import uuid

import numpy as np
import pytest
from image_streaming_contract import Listener
from ueye_rig import GRACE, software_trigger

from stacscope.adapters.ueye import UEyeCamera
from stacscope.core.actors import Actor, Fault, Rejected, Subscribe, ask, spawn
from stacscope.core.buffer import ImageBuffer, TimeSource
from stacscope.core.protocol import (
    DeviceReady,
    DeviceStopped,
    GetProperties,
    GetShortestTriggerPeriod,
    PropertiesChanged,
    SetProperty,
    ShortestTriggerPeriod,
    StartDevice,
    StopDevice,
)

pytestmark = pytest.mark.timeout(300)

SOAK = float(os.environ.get("STACSCOPE_SOAK_SECONDS", "10"))

RECORD_DIR = pathlib.Path(os.environ.get("STACSCOPE_RECORD_DIR", "E:/stacscope_tests"))
"""Where W4 writes its file: on the dev box, the data drive (user)."""

N_SLOTS = 20
CAPACITY = N_SLOTS * 1280 * 1024 * 2  # twenty full frames of RAW10

ROWS_256 = (0, 384, 1280, 256)
ROWS_256_SETTINGS = [("aoi_height", 256.0), ("aoi_y", 384.0)]
"""The instrument's light-sheet rows: the height first, so the position
never puts it off the sensor."""


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
    # A child reports to its parent, and the root publishes: listen there.
    parent.ref.tell(Subscribe(spawn(listener)))
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


async def test_w1_properties_by_message_on_the_camera(camera, report):
    """The camera is opened in a worker thread, and everything after runs on
    the event loop's thread; this is where the driver would mind. Each
    answer is what the camera took. During an acquisition, a new AOI is
    refused and the acquisition goes on; the exposure changes at once."""
    async with ueye_camera(camera, report, pixel_format="RAW8", settings=ROWS_256_SETTINGS) as (
        device,
        ref,
        listener,
    ):
        table = (await _ask(ref, GetProperties())).properties
        clock = await _ask(ref, SetProperty("pixel_clock", 40e6))
        exposure = await _ask(ref, SetProperty("exposure", 2.5e-3))
        period = await _ask(ref, GetShortestTriggerPeriod())
        report(f"{clock!r}; {exposure!r}; {period!r}")
        aoi = tuple(int(table[n].value) for n in ("aoi_x", "aoi_y", "aoi_width", "aoi_height"))
        assert aoi == ROWS_256
        assert clock.properties["pixel_clock"].value == 40e6
        assert abs(exposure.properties["exposure"].value - 2.5e-3) < 0.1e-3
        assert isinstance(period, ShortestTriggerPeriod)
        assert period.seconds == pytest.approx(device.capture.shortest_trigger_period())

        assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
        refused = await _ask(ref, SetProperty("aoi_height", 480.0))
        during = await _ask(ref, SetProperty("exposure", 1e-3))
        await software_trigger(device.capture)(3)
        reader = ImageBuffer.open(device.buffer_name)
        try:
            published = reader.published() - reader.acquisition_start
        finally:
            reader.close()
        assert await _ask(ref, StopDevice()) == DeviceStopped()
    report(f"while armed: {refused!r}; {during!r}; {published} frames published")
    assert isinstance(refused, Rejected)
    assert isinstance(during, PropertiesChanged)
    assert abs(during.properties["exposure"].value - 1e-3) < 0.1e-3
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
    async with ueye_camera(camera, report, pixel_format="RAW10", settings=[("exposure", 10e-3)]) as (
        device,
        ref,
        listener,
    ):
        table = (await _ask(ref, GetProperties())).properties
        chosen = period = None
        for hz in table["pixel_clock"].choices:
            await _ask(ref, SetProperty("pixel_clock", hz))
            await _ask(ref, SetProperty("exposure", 10e-3))
            period = (await _ask(ref, GetShortestTriggerPeriod())).seconds
            if period <= 0.9 / rate:
                chosen = hz / 1e6
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

    async with ueye_camera(
        camera, report, pixel_format="RAW10", settings=[("pixel_clock", 31e6), ("exposure", 10e-3)]
    ) as (
        _,
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


@pytest.fixture
def record_dir():
    """A folder of this test's own, deleted afterwards with what is in it."""
    if not RECORD_DIR.parent.exists():
        pytest.skip(f"{RECORD_DIR.parent} does not exist; set STACSCOPE_RECORD_DIR")
    path = RECORD_DIR / uuid.uuid4().hex[:12]
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path)
    with contextlib.suppress(OSError):
        RECORD_DIR.rmdir()  # only if nothing else is in it


async def test_w4_raw10_full_frame_at_20_fps_recorded_to_tiff(camera, trigger_line, record_dir, report):
    """The reason the recorder exists (user, 2026-10-01): the uEye has no
    recorder of its own, so the device records its buffer. Every frame
    published must be in the file, in order, stamped with the camera's
    clock, or the stop reports it."""
    import tifffile
    from daq_pulses import daq_pulses

    from stacscope.core.recording import DEVICE_TICKS_TAG

    rate = 20.0
    n = int(rate * SOAK)
    async with ueye_camera(
        camera,
        report,
        pixel_format="RAW10",
        settings=[("pixel_clock", 40e6), ("exposure", 10e-3)],
        save_directory=record_dir,
    ) as (device, ref, listener):
        period = (await _ask(ref, GetShortestTriggerPeriod())).seconds
        assert period <= 0.9 / rate, f"the shortest trigger period is {period * 1e3:.1f} ms"
        assert await _ask(ref, StartDevice("w4", -1)) == DeviceReady()
        reader = ImageBuffer.open(device.buffer_name)
        try:
            with daq_pulses(trigger_line, 1 / rate, n) as fire:
                await fire()
                deadline = time.monotonic() + n / rate + 3.0
                while reader.published() - reader.acquisition_start < n and time.monotonic() < deadline:
                    await asyncio.sleep(0.05)
                await asyncio.sleep(GRACE)
            stopping = time.monotonic()
            assert await _ask(ref, StopDevice()) == DeviceStopped()
            stop_s = time.monotonic() - stopping
            published = reader.published() - reader.acquisition_start
            last = reader.acquisition_start + published
            first = max(reader.acquisition_start, last - N_SLOTS + reader._layout.in_flight)
            slots = [reader.slot_of(i) for i in range(first, last)]
            buffer_frames = np.stack([reader.frames[s] for s in slots])  # copies
            buffer_ticks = [int(t) for t in reader.meta["device_ticks"][slots]]
            kept = last - first
        finally:
            reader.close()
    path = record_dir / "w4_ueye.tif"
    size = path.stat().st_size
    with tifffile.TiffFile(path) as tf:
        pages = len(tf.pages)
        ticks = [page.tags[DEVICE_TICKS_TAG].value for page in tf.pages]
        file_frames = np.stack([page.asarray() for page in tf.pages[pages - kept :]])
    intervals = np.diff(np.array(ticks, dtype=np.int64)) * 1e-7
    report(
        f"{n} pulses, {published} published, {pages} pages in {size / 1e6:.0f} MB; stop {stop_s:.2f} s; "
        f"page intervals median {np.median(intervals) * 1e3:.3f} ms, "
        f"min {intervals.min() * 1e3:.3f}, max {intervals.max() * 1e3:.3f}"
    )
    assert published == n
    assert pages == published
    assert _faults(listener) == []
    np.testing.assert_array_equal(file_frames, buffer_frames)
    assert ticks[pages - kept :] == buffer_ticks
    assert np.all(intervals > 0)


async def test_dph2_a_slower_pixel_clock_moves_the_limits_as_the_fake_predicts(camera, report):
    """The fake's model: the fastest frame rate is the readout at the pixel
    clock, and the longest exposure one frame period. A camera that moved
    them otherwise would leave a client offering values it refuses. The
    whole table is reported, with how long a read and a set take (DPH3)."""
    async with ueye_camera(camera, report, pixel_format="RAW10", settings=[("pixel_clock", 86e6)]) as (
        _,
        ref,
        _,
    ):
        began = time.perf_counter()
        before = (await _ask(ref, GetProperties())).properties
        read = time.perf_counter() - began
        for p in before.values():
            report(f"  {p.name}: {p.value!r} {p.unit} {p.minimum}..{p.maximum} step {p.step} {p.choices or p.labels}")
        await _ask(ref, SetProperty("frame_rate", before["frame_rate"].maximum))
        began = time.perf_counter()
        answer = await _ask(ref, SetProperty("pixel_clock", min(before["pixel_clock"].choices)))
        took = time.perf_counter() - began
    report(f"GetProperties {read * 1e3:.1f} ms; SetProperty pixel_clock {took * 1e3:.1f} ms")
    report(f"answered: {sorted(answer.properties)}")
    assert isinstance(answer, PropertiesChanged)
    assert answer.properties["frame_rate"].maximum < before["frame_rate"].maximum
    assert answer.properties["exposure"].maximum > 0
    assert {"frame_rate", "exposure"} <= set(answer.properties)
