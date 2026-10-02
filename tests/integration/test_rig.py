"""G7-G11 and G16 -- the rig, built from its config file.

``Rig.start`` is the composition root: it turns a rig config into a running
Coordinator with its source and devices, each placed where the file says,
and ``Rig.close`` takes all of it down again. Until now every rig was put
together by hand inside a test; this is the one the instrument will use.

The simulated rig here has the instrument's shape: a 20-cycle-a-second MAIN
whose start triggers one camera once (the uEye's place) and another camera
several times, packed at the start of the cycle (the Flash's). Each camera
records to a directory of its own (user, 2026-10-01), or both to one they
share (G16). The rate is lowered
so the waveforms stay small; the cycle is the instrument's 50 ms.

These run on the real clock, as L13 does: the cameras record through TIFF
recorders writing on real threads, and one device runs in a process of its
own. Every wait has a deadline.
"""

import asyncio
import time
import uuid

import numpy as np
import pytest
import tifffile

from stacscope.app import ConfigError, parse_config
from stacscope.app.rig import Rig
from stacscope.core.actors import Accepted, Actor, Fault, Subscribe, ask, spawn
from stacscope.core.buffer import ImageBuffer
from stacscope.core.protocol import (
    AcquisitionStarted,
    Phase,
    SaveDirectory,
    SetSaveDirectory,
    SetWaveform,
    SourcePhase,
    StartAcquisition,
    Waveform,
)
from stacscope.core.timing import CoordinatorState
from stacscope.core.transport import LaunchFailed

pytestmark = pytest.mark.timeout(30)

DEADLINE = 10.0
RATE = 1000.0  # samples/s
CYCLE = 50  # samples: 50 ms, the instrument's 20 cycles a second
FLASH_PULSES = 5  # per cycle, packed at the start
FLASH_PERIOD = 4  # samples apart


def rig_file(save, *, flash_directory=None, ueye_directory=None, extra=""):
    """The simulated rig, with buffer names of this test's own."""
    tag = uuid.uuid4().hex[:12]
    flash_directory = flash_directory or save / "flash"
    ueye_directory = ueye_directory or save / "ueye"
    return f"""
[source]
kind = "simulated"
rate = {RATE}
buffer_samples = 1000
ready_timeout = 2.0
stop_timeout = {DEADLINE}
ao = {{ etl = "Dev1/ao0" }}
do = {{ flash_trigger = "Dev1/port0/line0", laser_enable = "Dev1/port0/line1", ueye_trigger = "Dev1/port0/line2" }}

[devices.flash]
kind = "simulated_camera"
trigger = "flash_trigger"
ready_timeout = 2.0
stop_timeout = {DEADLINE}
buffer_name = "gf{tag}"
shape = [4, 8]
n_slots = 64
save_directory = '{flash_directory}'

[devices.ueye]
kind = "simulated_camera"
trigger = "ueye_trigger"
ready_timeout = 2.0
stop_timeout = {DEADLINE}
buffer_name = "gu{tag}"
shape = [8, 8]
n_slots = 16
save_directory = '{ueye_directory}'
{extra}
"""


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)

    def faults(self):
        return [m.reason for m in self.heard if isinstance(m, Fault)]


def cycle(rig, n):
    """``n`` samples of MAIN as the instrument's will be: the uEye pulsed at
    the start, the Flash pulsed FLASH_PULSES times from the start. Every
    analog sample is 0 V, and every other line stays low."""
    ao = np.zeros((len(rig.output.ao), n))
    do = np.zeros((len(rig.output.do), n), dtype=bool)
    do[rig.line(rig.triggers["ueye"]), 0] = True
    for k in range(FLASH_PULSES):
        do[rig.line(rig.triggers["flash"]), k * FLASH_PERIOD] = True
    return ao, do


async def load_waveforms(rig):
    quiet = np.zeros((len(rig.output.ao), 4)), np.zeros((len(rig.output.do), 4), dtype=bool)
    for name, (ao, do) in [(Waveform.WARMUP, quiet), (Waveform.MAIN, cycle(rig, CYCLE)), (Waveform.COOLDOWN, quiet)]:
        assert await ask(await rig.source(), SetWaveform(name, ao, do), timeout=DEADLINE) == Accepted()


async def until_idle(rig, listener):
    deadline = time.monotonic() + DEADLINE
    while (state := (await rig.status()).state) is not CoordinatorState.IDLE:
        assert time.monotonic() < deadline, f"still {state}: {listener.faults()}"
        await asyncio.sleep(0.01)


def pages(path):
    with tifffile.TiffFile(path) as tf:
        return len(tf.pages)


def gone(buffer_name):
    try:
        ImageBuffer.open(buffer_name).close()
    except FileNotFoundError:
        return True
    return False


@pytest.fixture
async def rigs():
    """Starts rigs, and closes whatever a failed test left running."""
    started = []

    async def start(text, **kwargs):
        rig = await Rig.start(parse_config(text), **kwargs)
        started.append(rig)
        return rig

    yield start
    for rig in started:
        await rig.close()


@pytest.fixture
def directories(tmp_path):
    for name in ("flash", "ueye"):
        (tmp_path / name).mkdir()
    return tmp_path


async def test_g7_a_rig_from_its_file_records_both_cameras_each_to_its_own_directory(rigs, directories):
    """The run the instrument will make, simulated: the Coordinator starts
    both cameras and the source, the cycle's pulses cause every frame, and
    each camera's file lands in its own directory, holding one page per
    pulse on its own line. A rig that sent both files to one directory, or
    a camera on the wrong line, would fail here."""
    cycles = 10
    rig = await rigs(rig_file(directories))
    listener = Listener()
    rig.ref.tell(Subscribe(spawn(listener)))
    await load_waveforms(rig)

    assert isinstance(await ask(rig.ref, StartAcquisition(cycles, "run-01"), timeout=DEADLINE), AcquisitionStarted)
    await until_idle(rig, listener)

    assert pages(directories / "flash" / "run-01_flash.tif") == cycles * FLASH_PULSES
    assert pages(directories / "ueye" / "run-01_ueye.tif") == cycles
    assert sorted(p.name for p in (directories / "flash").iterdir()) == ["run-01_flash.tif"]
    assert sorted(p.name for p in (directories / "ueye").iterdir()) == ["run-01_ueye.tif"]
    assert listener.faults() == []


async def test_g16_two_devices_may_share_a_save_directory(rigs, tmp_path):
    """A save directory is each device's own setting, but two devices may
    give the same one (user, 2026-10-01): the device's name is in its file's
    name, so their files differ. Both must land there, whole. A file named
    by the stem alone would collide, and the second recorder, which never
    overwrites a file, would refuse the acquisition."""
    cycles = 4
    rig = await rigs(rig_file(tmp_path, flash_directory=tmp_path, ueye_directory=tmp_path))
    listener = Listener()
    rig.ref.tell(Subscribe(spawn(listener)))
    await load_waveforms(rig)

    assert isinstance(await ask(rig.ref, StartAcquisition(cycles, "run-01"), timeout=DEADLINE), AcquisitionStarted)
    await until_idle(rig, listener)

    assert sorted(p.name for p in tmp_path.iterdir()) == ["run-01_flash.tif", "run-01_ueye.tif"]
    assert pages(tmp_path / "run-01_flash.tif") == cycles * FLASH_PULSES
    assert pages(tmp_path / "run-01_ueye.tif") == cycles
    assert listener.faults() == []


async def test_g8_closing_a_rig_mid_acquisition_stops_it_and_lets_everything_go(rigs, directories):
    """Close is how a session ends, and it may come while an acquisition
    runs with no end. It must stop the acquisition through the Coordinator,
    so each camera disarms and closes its file, and then release every
    buffer. A close that only stopped actors would leave a file unfinished
    and a segment alive, and the Coordinator would see its devices die
    mid-acquisition: a close is not a failure, and raises no Fault. It also
    ends through the source's COOLDOWN, which on the instrument parks the
    outputs; a source simply stopped would leave them mid-MAIN."""
    rig = await rigs(rig_file(directories))
    listener = Listener()
    rig.ref.tell(Subscribe(spawn(listener)))
    await load_waveforms(rig)
    buffers = dict(rig.buffers)
    assert set(buffers) == {"flash", "ueye"}
    assert isinstance(await ask(rig.ref, StartAcquisition(-1, "run-01"), timeout=DEADLINE), AcquisitionStarted)
    await asyncio.sleep(0.3)

    await rig.close()

    assert pages(directories / "ueye" / "run-01_ueye.tif") >= 3, "the file is whole and readable"
    assert all(gone(name) for name in buffers.values()), "every segment released"
    assert listener.faults() == []
    assert Phase.COOLDOWN in [m.phase for m in listener.heard if isinstance(m, SourcePhase)]


async def test_cm50_the_rig_reads_the_coordinator_through_messages(rigs, directories):
    """The rig holds the Coordinator object only to start and stop it. Its
    state and its devices come from ``GetStatus``, as the gateway will get
    them, so a device relaunched is found where it now is."""
    rig = await rigs(rig_file(directories))

    status = await rig.status()
    flash = await rig.device("flash")
    source = await rig.source()

    assert status.state is CoordinatorState.IDLE
    assert [d.name for d in status.devices] == ["source", "flash", "ueye"]
    assert flash.address == "coordinator/flash" and source.address == "coordinator/source"
    folder = directories / "elsewhere"
    folder.mkdir()
    assert await ask(flash, SetSaveDirectory(str(folder)), timeout=DEADLINE) == SaveDirectory(str(folder))
    with pytest.raises(KeyError):
        await rig.device("no such device")


async def test_g9_a_device_that_cannot_launch_fails_the_start_and_nothing_is_left_running(directories):
    """A rig that starts without one of its devices is not the rig the file
    describes. Rig.start raises naming the device and its reason, and what
    had already launched is shut down, not left holding its buffer."""
    text = rig_file(directories, ueye_directory=directories / "no such directory")
    flash_buffer = parse_config(text).devices[0].options["buffer_name"]

    with pytest.raises(LaunchFailed) as failed:
        await Rig.start(parse_config(text))

    assert "ueye" in str(failed.value)
    assert "no such directory" in str(failed.value)
    assert gone(flash_buffer)


async def test_g10_every_devices_trigger_must_be_a_line_the_source_plays(directories):
    """A camera whose trigger line is not played would arm and wait for
    pulses that never come. With a simulated source the lines are in the
    file, so any device's trigger, not only a simulated camera's, is
    checked when the file is read."""
    extra = """
[devices.camera]
kind = "simulated_free_running_camera"
trigger = "ThorCamTrigger"
ready_timeout = 2.0
buffer_name = "unused"
"""
    with pytest.raises(ConfigError, match="ThorCamTrigger"):
        parse_config(rig_file(directories, extra=extra))


async def test_g11_a_device_placed_in_a_process_by_the_file_runs_there(rigs, directories):
    """Placement is config: the file says ``placement = "process"``, and the
    device runs in a process of its own, its frames read here by its
    buffer's name, as a monitor reads them. Closing the rig ends that
    process. (A free-running camera, because a simulated trigger is a wire
    in this process and cannot reach another.)"""
    extra = f"""
[devices.camera]
kind = "simulated_free_running_camera"
placement = "process"
ready_timeout = 10.0
heartbeat = {{ interval = 0.2, timeout = 2.0 }}
buffer_name = "gp{uuid.uuid4().hex[:12]}"
interval = 0.005
shape = [4, 8]
"""
    rig = await rigs(rig_file(directories, extra=extra))
    await load_waveforms(rig)
    process = rig.host.process("camera")
    assert process is not None and process.returncode is None, "in a process of its own"

    assert isinstance(await ask(rig.ref, StartAcquisition(4, ""), timeout=DEADLINE), AcquisitionStarted)
    reader = ImageBuffer.open(rig.buffers["camera"])
    try:
        deadline = time.monotonic() + DEADLINE
        while reader.published() < 3:
            assert time.monotonic() < deadline, "no frames from the camera's process"
            await asyncio.sleep(0.01)
    finally:
        reader.close()

    await rig.close()
    assert process.returncode is not None, "closing the rig ended the process"
