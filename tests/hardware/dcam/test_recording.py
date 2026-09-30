"""V4-V5 -- the OrcaFlash device recording to DCIMG through the camera's
driver, read back through dcimgapi.dll.

RUN ON THE CAMERA'S MACHINE. This triggers the camera by software, changes
its trigger source, exposure time and subarray (all put back afterwards), and
writes DCIMG files, each in a folder of its own under STACSCOPE_RECORD_DIR
(E:/stacscope_tests by default, the dev box's data drive), deleted after the
test:

  V4  Ten frames recorded: the file holds every frame published, pixel for
      pixel, with the same frame stamps and time stamps; whether the file
      is sized before the first frame (preallocation), and how long opening
      and closing it take.
  V5  A file whose frame limit is reached before the stop: the recorder
      stops by itself, and the device reports it as a fatal fault.

No test records without a limit: DCIMG reserves a file's whole size when it
opens (V4), so an unlimited recording would claim most of the disk.

Every test adds what it read to a "camera findings" section at the end of
the run; paste that section back.

    PowerShell:  $env:STACSCOPE_CAMERA = "0"
                 uv run pytest tests/hardware/dcam/test_recording.py
                 Remove-Item Env:STACSCOPE_CAMERA

    Git Bash:    STACSCOPE_CAMERA=0 uv run pytest tests/hardware/dcam/test_recording.py
"""
# For end use and not for commercial purposes.

import asyncio
import contextlib
import os
import pathlib
import shutil
import time
import uuid

import numpy as np
import pytest
from dcam_rig import GRACE, ROWS_256, SOFTWARE, restore, software_trigger
from dcimg_reader import DcimgFile
from image_streaming_contract import Listener

from stacscope.adapters.dcam import DcamFrameCapture, OrcaFlash
from stacscope.core.actors import Actor, Fault, Subscribe, ask, spawn
from stacscope.core.buffer import ImageBuffer
from stacscope.core.protocol import DeviceReady, DeviceStopped, StartDevice, StopDevice

pytestmark = pytest.mark.timeout(120)

RECORD_DIR = pathlib.Path(os.environ.get("STACSCOPE_RECORD_DIR", "E:/stacscope_tests"))
"""Where the suite writes its files: on the dev box, the data drive (user)."""

N_SLOTS = 16
CAPACITY = 16 * 2048 * 2048 * 2
SETTINGS = [*SOFTWARE, *ROWS_256]


@pytest.fixture
def folder():
    """A folder of this test's own under RECORD_DIR, removed afterwards."""
    if not RECORD_DIR.parent.exists():
        pytest.skip(f"{RECORD_DIR.parent} does not exist; set STACSCOPE_RECORD_DIR")
    path = RECORD_DIR / uuid.uuid4().hex[:12]
    path.mkdir(parents=True)
    yield path
    shutil.rmtree(path)
    with contextlib.suppress(OSError):
        RECORD_DIR.rmdir()  # only if nothing else is in it


@contextlib.asynccontextmanager
async def flash(camera: int, folder: pathlib.Path, settings=SETTINGS, **options):
    """An OrcaFlash on the camera, recording into ``folder``, with a
    listener subscribed; the settings it changes are put back after."""
    capture = DcamFrameCapture(camera, in_flight=2)
    try:
        saved = [(name, capture.get(name)) for name, _ in settings]
    finally:
        await capture.close()
    parent = Actor()
    spawn(parent, name="coordinator")
    device = OrcaFlash(
        "r" + uuid.uuid4().hex[:20],
        camera=camera,
        settings=settings,
        n_slots=N_SLOTS,
        capacity_bytes=CAPACITY,
        max_slots=N_SLOTS,
        save_directory=folder,
        **options,
    )
    ref = parent.spawn_child(device, name="flash")
    listener = Listener()
    ref.tell(Subscribe(spawn(listener)))
    try:
        await asyncio.wait(
            [asyncio.ensure_future(device.started.wait()), asyncio.ensure_future(device.stopped.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        assert device.started.is_set(), f"the device did not launch: {device.exit_error!r}"
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
    return await ask(ref, msg, timeout=30.0)


def _faults(listener) -> list[Fault]:
    return [m for m in listener.heard if isinstance(m, Fault)]


async def test_v4_a_recorded_acquisition_holds_every_frame_published(camera, report, folder):
    """The file is what the acquisition is kept as. Read back through
    Hamamatsu's own reader, it must hold what the buffer was given: the
    same pixels, frame stamps and time stamps, frame for frame."""
    async with flash(camera, folder, max_record_frames=50) as (device, ref, listener):
        began = time.monotonic()
        assert await _ask(ref, StartDevice("v4", -1)) == DeviceReady()
        armed = time.monotonic() - began
        path = folder / "v4_flash.dcimg"
        size_at_open = path.stat().st_size

        await software_trigger(device.capture)(10)
        began = time.monotonic()
        assert await _ask(ref, StopDevice()) == DeviceStopped()
        stopped = time.monotonic() - began
        await asyncio.sleep(GRACE)

        reader = ImageBuffer.open(device.buffer_name)
        try:
            published = reader.published() - reader.acquisition_start
            slots = [(reader.acquisition_start + i) % N_SLOTS for i in range(published)]
            buffered = [np.array(reader.frames[s]) for s in slots]
            stamps = reader.meta[slots]
        finally:
            reader.close()

    size_at_close = path.stat().st_size
    with DcimgFile(path) as file:
        recorded = [file.frame(i) for i in range(file.frames)]
        shape, sessions = file.shape, file.sessions
    report(
        f"arm {armed:.2f} s, stop {stopped:.2f} s; file {size_at_open} bytes at open, "
        f"{size_at_close} at close ({size_at_close / max(published, 1):.0f} per frame); "
        f"{len(recorded)} frames of {shape}, {sessions} session(s); published {published}"
    )
    report(
        "frame stamps: file "
        f"{[fs for _, fs, _ in recorded]}, buffer {stamps['hw_frame'].tolist()}; "
        f"first time stamps: file {recorded[0][2]}, buffer {int(stamps['device_ticks'][0])}"
    )
    assert _faults(listener) == []
    assert published == 10 == len(recorded)
    assert shape == (256, 2048)
    for (image, framestamp, timestamp), expected, stamp in zip(recorded, buffered, stamps, strict=True):
        assert np.array_equal(image, expected)
        assert framestamp == int(stamp["hw_frame"])
        assert timestamp == int(stamp["device_ticks"])


async def test_v5_a_file_that_reaches_its_limit_is_a_fatal_fault_at_the_stop(camera, report, folder):
    """DCAM's recorder stops by itself at the file's limit while the
    stream goes on. The device must see it at the stop, and the file must
    still be readable, holding the frames before the limit."""
    async with flash(camera, folder, max_record_frames=3) as (device, ref, listener):
        assert await _ask(ref, StartDevice("v5", -1)) == DeviceReady()
        await software_trigger(device.capture)(5)
        assert await _ask(ref, StopDevice()) == DeviceStopped()
        await asyncio.sleep(GRACE)
        faults = _faults(listener)

    with DcimgFile(folder / "v5_flash.dcimg") as file:
        frames = file.frames
        framestamps = [file.frame(i)[1] for i in range(frames)]
    report(f"faults {[f.reason for f in faults]}; file holds {frames}, frame stamps {framestamps}")
    [fault] = faults
    assert fault.fatal and "3 of the 5" in fault.reason
    assert frames == 3
    assert framestamps == [0, 1, 2]
