"""V1-V3 -- the OrcaFlash device on the camera: DCAM opened from a worker
thread, settings by message, and an acquisition streamed into its buffer.

RUN ON THE CAMERA'S MACHINE. This triggers the camera by software, and
changes its trigger source, exposure time and subarray; everything changed
is put back afterwards. It reads what the fake DLL cannot vouch for:

  V1  DCAM opened in a worker thread (so the device answers heartbeats while
      it starts) works from the event loop, and settings come back as the
      Flash states them, including what it says of a plain number's label.
  V2  An acquisition of software-triggered frames is published, stamped,
      under a header declaring two frames in flight.
  V3  While armed, which settings the Flash takes: an exposure time, and a
      subarray, which must be refused without ending the acquisition.

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

from stacscope.adapters.dcam import (
    DcamFrameCapture,
    GetProperty,
    OrcaFlash,
    PropertyValue,
    SetProperty,
    SetPropertyLabel,
)
from stacscope.core.actors import Actor, Fault, Rejected, Subscribe, ask, spawn
from stacscope.core.buffer import ImageBuffer
from stacscope.core.protocol import DeviceReady, DeviceStopped, StartDevice, StopDevice

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
    ref.tell(Subscribe(spawn(listener)))
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


async def test_v1_settings_by_message_on_a_camera_opened_from_a_thread(camera, report):
    """The device opens DCAM in a worker thread, and everything after runs
    on the event loop's thread. Nothing in the reference says DCAM minds;
    this is where it would show. The fake answers a plain number's label
    with DCAMERR_NOVALUETEXT, as the reference says; if the Flash answers
    otherwise, GetProperty of an exposure time is refused here."""
    async with flash(camera, SOFTWARE, report) as (_, ref, _):
        source = await _ask(ref, GetProperty("TRIGGER SOURCE"))
        exposure = await _ask(ref, GetProperty("EXPOSURE TIME"))
        report(f"{source!r}; {exposure!r}")
        assert isinstance(source, PropertyValue) and source.label == "SOFTWARE"
        assert isinstance(exposure, PropertyValue) and exposure.label is None
        assert exposure.value == pytest.approx(0.001, abs=1e-4)

        applied = await _ask(ref, SetProperty("EXPOSURE TIME", 0.0123456))
        report(f"SetProperty EXPOSURE TIME 0.0123456: {applied!r}")
        assert isinstance(applied, PropertyValue)
        assert applied.value == pytest.approx(0.0123456, abs=1e-4)

        refused = await _ask(ref, SetPropertyLabel("TRIGGER SOURCE", "SOFT"))
        report(f"SetPropertyLabel TRIGGER SOURCE SOFT: {refused!r}")
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

