"""V1-V6 and DPH1 -- the OrcaFlash device on the camera: DCAM opened from a
worker thread, its properties by message, and an acquisition streamed into
its buffer.

RUN ON THE CAMERA'S MACHINE. This triggers the camera by software, and
changes its trigger source, exposure time and subarray; everything changed
is put back afterwards. It reads what the fake DLL cannot vouch for:

  V1  DCAM opened in a worker thread (so the device answers heartbeats while
      it starts) works from the event loop, and properties come back as the
      Flash states them.
  V2  An acquisition of software-triggered frames is published, stamped,
      under a header declaring two frames in flight.
  V3  While armed, which settings the Flash takes: an exposure time, and a
      subarray, which must be refused without ending the acquisition.
  V6  The shortest trigger period, asked as a client asks every camera: the
      Flash's own TIMING MIN TRIGGER INTERVAL, moving with the exposure.
  DPH1  The table of properties is what DCAM reports, and a set's answer is
      what moved between reading the table before and after it. Reports
      the whole table, and how long a read and a set take.
  DPH3  How long a batch of settings takes against the same settings sent
      one at a time: the evidence for keeping bulk writes or removing them.

Every test adds what it read to a "camera findings" section at the end of
the run; paste that section back.

It runs only against the camera whose DCAM index is in STACSCOPE_CAMERA:

    PowerShell:  $env:STACSCOPE_CAMERA = "0"
                 uv run pytest tests/hardware/dcam/test_device.py
                 Remove-Item Env:STACSCOPE_CAMERA

    Git Bash:    STACSCOPE_CAMERA=0 uv run pytest tests/hardware/dcam/test_device.py

Takes under a minute, most of it DCAM starting up.
"""
# For end use and not for commercial purposes.

import asyncio
import contextlib
import time
import uuid

import numpy as np
import pytest
from dcam_rig import GRACE, ROWS_256, SOFTWARE, restore, software_trigger
from image_streaming_contract import Listener

from stacscope.adapters.dcam import DcamFrameCapture, OrcaFlash
from stacscope.core.actors import Actor, Fault, Rejected, Subscribe, ask, spawn
from stacscope.core.buffer import ImageBuffer
from stacscope.core.protocol import (
    DeviceReady,
    DeviceStopped,
    GetProperties,
    GetProperty,
    GetShortestTriggerPeriod,
    PropertiesChanged,
    Property,
    PropertyAccess,
    PropertyKind,
    SetProperties,
    SetProperty,
    ShortestTriggerPeriod,
    StartDevice,
    StopDevice,
)

pytestmark = pytest.mark.timeout(120)

N_SLOTS = 16
CAPACITY = 16 * 2048 * 2048 * 2  # sixteen full frames, the largest format


@contextlib.asynccontextmanager
async def flash(camera: int, settings, report):
    """An OrcaFlash launched on the camera with ``settings`` in its config,
    and a listener subscribed. The settings are read first and put back
    after the device has gone, since the camera keeps them between sessions;
    the device holds DCAM's one session per process while it runs."""
    capture = DcamFrameCapture(camera, in_flight=2)
    try:
        saved = [(name, capture.get(name)) for name, _ in settings]
    finally:
        await capture.close()
    parent = Actor()
    spawn(parent, name="coordinator")
    device = OrcaFlash(
        "v" + uuid.uuid4().hex[:20],
        camera=camera,
        settings=settings,
        n_slots=N_SLOTS,
        capacity_bytes=CAPACITY,
        max_slots=N_SLOTS,
    )
    opening = time.monotonic()
    ref = parent.spawn_child(device, name="flash")
    listener = Listener()
    # A child reports to its parent, and the root publishes: listen there.
    parent.ref.tell(Subscribe(spawn(listener)))
    try:
        await asyncio.wait(
            [asyncio.ensure_future(device.started.wait()), asyncio.ensure_future(device.stopped.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert device.started.is_set(), f"the device did not launch: {device.exit_error!r}"
        report(f"launched in {time.monotonic() - opening:.1f} s")
        yield device, ref, listener
    finally:
        if not device.stopped.is_set():
            device.stop()
            await device.stopped.wait()
        listener.stop()
        parent.stop()
        capture = DcamFrameCapture(camera, in_flight=2)
        try:
            restore(capture, saved)
        finally:
            await capture.close()


async def _ask(ref, msg):
    return await ask(ref, msg, timeout=10.0)


async def test_v1_properties_by_message_on_a_camera_opened_from_a_thread(camera, report):
    """The device opens DCAM in a worker thread, and everything after runs
    on the event loop's thread. Nothing in the reference says DCAM minds;
    this is where it would show."""
    async with flash(camera, SOFTWARE, report) as (_, ref, _):
        source = await _ask(ref, GetProperty("TRIGGER SOURCE"))
        exposure = await _ask(ref, GetProperty("EXPOSURE TIME"))
        report(f"{source!r}; {exposure!r}")
        assert isinstance(source, Property) and source.value == "SOFTWARE"
        assert isinstance(exposure, Property) and exposure.kind is PropertyKind.NUMBER
        assert exposure.unit == "s"
        assert exposure.value == pytest.approx(0.001, abs=1e-4)

        applied = await _ask(ref, SetProperty("EXPOSURE TIME", 0.0123456))
        report(f"SetProperty EXPOSURE TIME 0.0123456: {applied!r}")
        assert isinstance(applied, PropertiesChanged)
        assert applied.properties["EXPOSURE TIME"].value == pytest.approx(0.0123456, abs=1e-4)

        refused = await _ask(ref, SetProperty("TRIGGER SOURCE", "SOFT"))
        report(f"SetProperty TRIGGER SOURCE SOFT: {refused!r}")
        assert isinstance(refused, Rejected)


async def test_v2_an_acquisition_of_software_triggered_frames_is_published(camera, report):
    """The device end to end on the camera, as the Coordinator drives it:
    armed, triggered, stopped. Each frame is published once, in order,
    stamped with consecutive frame stamps, under the layout the launch
    settings gave, with the Flash's declared in-flight depth."""
    async with flash(camera, [*SOFTWARE, *ROWS_256], report) as (device, ref, listener):
        assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
        reader = ImageBuffer.open(device.buffer_name)
        try:
            await software_trigger(device.capture)(5)
            assert await _ask(ref, StopDevice()) == DeviceStopped()

            published = reader.published() - reader.acquisition_start
            stamps = reader.meta["hw_frame"][:published].astype(np.int64)
            report(
                f"frames {published}, shape {reader.frames.shape[1:]}, "
                f"in flight {reader._layout.in_flight}, frame stamps {stamps.tolist()}"
            )
            assert published == 5
            assert reader.frames.shape[1] == 256
            assert reader._layout.in_flight == 2
            assert np.all(np.diff(stamps) == 1)
            assert [m for m in listener.heard if isinstance(m, Fault)] == []
        finally:
            reader.close()


async def test_v3_while_armed_the_flash_takes_an_exposure_and_refuses_a_subarray(camera, report):
    """What the Flash takes while capturing decides what a client can
    adjust mid-acquisition. An exposure time is expected to be taken, and a
    subarray refused (it would reshape frames in slots already laid out);
    the refusal must leave the acquisition running."""
    async with flash(camera, [*SOFTWARE, *ROWS_256], report) as (device, ref, listener):
        assert await _ask(ref, StartDevice("", -1)) == DeviceReady()
        trigger = software_trigger(device.capture)
        await trigger(1)

        exposure = await _ask(ref, SetProperty("EXPOSURE TIME", 0.002))
        subarray = await _ask(ref, SetProperty("SUBARRAY VSIZE", 128))
        await trigger(1)
        await asyncio.sleep(GRACE)
        report(f"armed: EXPOSURE TIME 0.002 -> {exposure!r}; SUBARRAY VSIZE 128 -> {subarray!r}")

        assert isinstance(subarray, Rejected)
        assert [m for m in listener.heard if isinstance(m, Fault)] == []
        writer = device.writer
        assert writer.published() - writer.acquisition_start == 2
        assert await _ask(ref, StopDevice()) == DeviceStopped()



async def test_v6_the_shortest_trigger_period_is_the_flashs_own_floor(camera, report):
    """The query every triggered camera answers (user, 2026-09-30), so a
    client can check its waveform against any camera alike. On the Flash it
    is TIMING MIN TRIGGER INTERVAL (20.0 ms at full frame and 10 ms
    exposure, AREA, in the dev box survey), which must move with the
    exposure."""
    async with flash(camera, SOFTWARE, report) as (_, ref, _):
        periods = {}
        for exposure in (0.001, 0.010):
            assert isinstance(await _ask(ref, SetProperty("EXPOSURE TIME", exposure)), PropertiesChanged)
            answer = await _ask(ref, GetShortestTriggerPeriod())
            floor = await _ask(ref, GetProperty("TIMING MIN TRIGGER INTERVAL"))
            assert isinstance(answer, ShortestTriggerPeriod)
            assert answer.seconds == floor.value
            periods[exposure] = answer.seconds
    report(", ".join(f"{e * 1e3:.0f} ms exposure: {p * 1e3:.3f} ms" for e, p in periods.items()))
    assert periods[0.010] > periods[0.001]


async def test_dph1_the_table_is_what_dcam_reports(camera, report):
    """The table is built from DCAM's attributes, which only the fake has
    vouched for so far: every property the Flash can read, with its value
    as DCAM gives it. Read-only values that move on their own (a
    temperature) may differ between two reads, so only writable ones are
    compared. The whole table is reported, for the record of what the
    Flash offers, with the time a read takes (DPH3)."""
    async with flash(camera, SOFTWARE, report) as (device, ref, _):
        began = time.perf_counter()
        table = (await _ask(ref, GetProperties())).properties
        took = time.perf_counter() - began
        capture = device.capture
        report(f"GetProperties: {len(table)} properties in {took * 1e3:.1f} ms")
        for p in table.values():
            limits = p.labels if p.kind is PropertyKind.LABEL else (p.minimum, p.maximum, p.step)
            report(f"  {p.name}: {p.value!r} {p.unit} {limits} {p.access.name}")
        unlisted = [name for name in capture.names() if name not in table]
        report(f"not listed (unreadable): {unlisted}")

        for name, p in table.items():
            if p.access is PropertyAccess.READ_ONLY:
                continue
            held = capture.label(name) if p.kind is PropertyKind.LABEL else capture.get(name)
            assert p.value == held, name


async def test_dph1_a_sets_answer_is_what_moved(camera, report):
    """Decision 1: a set answers everything that moved, found by reading
    the whole table before and after. Read again by hand here, the
    difference must be the answer, give or take the values that move on
    their own. Reports the time a set takes with its two reads (DPH3)."""
    async with flash(camera, SOFTWARE, report) as (_, ref, _):
        before = (await _ask(ref, GetProperties())).properties
        began = time.perf_counter()
        answer = await _ask(ref, SetProperty("EXPOSURE TIME", 0.002))
        took = time.perf_counter() - began
        after = (await _ask(ref, GetProperties())).properties

        moved = {n for n, p in after.items() if before[n] != p}
        report(f"SetProperty EXPOSURE TIME 0.002: {took * 1e3:.1f} ms, answered {sorted(answer.properties)}")
        report(f"moved between reads by hand: {sorted(moved)}")
        assert isinstance(answer, PropertiesChanged)
        assert answer.properties["EXPOSURE TIME"] == after["EXPOSURE TIME"]
        settled = {n for n in moved if after[n].access is not PropertyAccess.READ_ONLY}
        assert settled <= set(answer.properties)


async def test_dph3_a_batch_against_the_same_settings_one_at_a_time(camera, report):
    """Bulk writes were added to be measured (user, 2026-10-05). A batch of N
    reads the table N + 1 times and is one message; N single writes read it
    2N times and are N messages. Each way is timed five times, alternating
    two sets of values so that every write changes something."""
    import statistics

    async with flash(camera, [*SOFTWARE, *ROWS_256], report) as (_, ref, _):
        sets = [
            {"EXPOSURE TIME": 0.002, "SUBARRAY VSIZE": 128, "SUBARRAY VPOS": 960},
            {"EXPOSURE TIME": 0.003, "SUBARRAY VSIZE": 256, "SUBARRAY VPOS": 896},
        ]
        batch, singles = [], []
        for i in range(10):
            values = sets[i % 2]
            began = time.perf_counter()
            if i < 5:
                assert isinstance(await _ask(ref, SetProperties(values)), PropertiesChanged)
                batch.append(time.perf_counter() - began)
            else:
                for name, value in values.items():
                    assert isinstance(await _ask(ref, SetProperty(name, value)), PropertiesChanged)
                singles.append(time.perf_counter() - began)
    report(
        f"3 settings: batch {statistics.median(batch) * 1e3:.1f} ms, "
        f"one at a time {statistics.median(singles) * 1e3:.1f} ms (medians of 5)"
    )
