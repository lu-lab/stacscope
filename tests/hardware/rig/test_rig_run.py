"""G13 -- the rig from its config file, on the instrument's hardware: both
cameras recording a cycle the Coordinator runs from start to stop.

RUN ON THE INSTRUMENT'S MACHINE. This plays digital pulses from the DAQ into
both cameras' trigger inputs, and records from both. Every analog output is
held at 0 V for the whole run; the test checks that before playing anything.

The rig is the one in STACSCOPE_RIG_CONFIG, with one change: the DAQ tasks
are not the instrument's. The run saves a pair of its own to NI MAX
(stacscope_test_rig_ao and stacscope_test_rig_do) and deletes them after, so
it cannot depend on, or disturb, the SPIM_* tasks. Their digital lines carry
the trigger names in the config, on the lines in STACSCOPE_CAMERA_TRIGGER
(the Flash's) and STACSCOPE_UEYE_TRIGGER (the uEye's). The analog task holds
the board's five outputs, at 0 V, only so that it clocks the digital lines
as the instrument's does.

  G13 A 20-cycle-a-second MAIN, as the instrument runs one with no session
      model behind it: one uEye frame per cycle (full frame, RAW10), and 31
      Flash frames packed at the start of the cycle (256 rows, synchronous
      readout), as fast as the Flash says it can take them, rounded up to
      the DAQ's 10 us sample. COOLDOWN plays one more Flash pulse, which
      reads out the last exposure. Both cameras record, each to a run folder
      inside its save directory from the config (one folder, if they share
      the directory). The Flash's file must hold 31 frames a cycle, the
      uEye's one page a cycle, and nothing may report a fault. The Flash's
      n_slots must be a multiple of 31, so the ring holds whole volumes.
      Five seconds; STACSCOPE_SOAK_SECONDS runs longer.

The recordings are deleted afterwards; set STACSCOPE_KEEP_RECORDINGS=1 to
keep them. Set STACSCOPE_MONITOR=1 to open a buffer monitor on each camera's
stream as the rig starts. They stay open after the test, showing the last
frames, until their windows are closed. The first one builds the monitor's
environment, which takes a while.

    PowerShell:  $env:STACSCOPE_RIG_CONFIG = "config/rig.toml"
                 $env:STACSCOPE_CAMERA_TRIGGER = "Dev1/port0/line0"
                 $env:STACSCOPE_UEYE_TRIGGER = "Dev1/port0/line2"
                 uv run --extra nidaqmx --extra ueye pytest tests/hardware/rig
                 Remove-Item Env:STACSCOPE_RIG_CONFIG, Env:STACSCOPE_CAMERA_TRIGGER, Env:STACSCOPE_UEYE_TRIGGER

    Git Bash:    STACSCOPE_RIG_CONFIG=config/rig.toml \\
                 STACSCOPE_CAMERA_TRIGGER=Dev1/port0/line0 STACSCOPE_UEYE_TRIGGER=Dev1/port0/line2 \\
                     uv run --extra nidaqmx --extra ueye pytest tests/hardware/rig

The summary's "rig findings" section has what the run measured; paste it
back.
"""

import asyncio
import contextlib
import dataclasses
import math
import os
import pathlib
import shutil
import subprocess
import sys
import time
import uuid

import numpy as np
import pytest

from stacscope.app import KINDS, load_config
from stacscope.app.rig import Rig
from stacscope.core.actors import Accepted, Actor, Fault, Subscribe, ask, spawn
from stacscope.core.ports import Channel, OutputConfig
from stacscope.core.protocol import (
    AcquisitionStarted,
    GetShortestTriggerPeriod,
    SaveDirectory,
    SetSaveDirectory,
    SetWaveform,
    ShortestTriggerPeriod,
    StartAcquisition,
    Waveform,
)
from stacscope.core.timing import CoordinatorState

pytestmark = pytest.mark.timeout(600)

ROOT = pathlib.Path(__file__).parents[3]

SOAK = float(os.environ.get("STACSCOPE_SOAK_SECONDS", "5"))
KEEP = os.environ.get("STACSCOPE_KEEP_RECORDINGS") == "1"
MONITOR = os.environ.get("STACSCOPE_MONITOR") == "1"

RATE = 100_000.0
"""The instrument's sample rate, samples a second."""
CYCLES_PER_SECOND = 20
CYCLE = int(RATE) // CYCLES_PER_SECOND  # 5000 samples: 50 ms
FLASH_FRAMES = 31
"""Flash frames a cycle, packed at its start."""
WIDTH = 10
"""Samples each trigger pulse is high: 100 us."""
QUIET = 1000
"""Samples of WARMUP and of COOLDOWN, all low: 10 ms each."""

AO_CHANNELS = 5
"""The board's five outputs, held at 0 V: the analog task clocks the digital
lines, as on the instrument. Fewer would also raise the board's floor on the
buffer depth above the instrument's 5000 samples."""

LINES = {"dcam": "STACSCOPE_CAMERA_TRIGGER", "ueye": "STACSCOPE_UEYE_TRIGGER"}
"""Where each camera's trigger line comes from, by device kind."""


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)

    def faults(self) -> list[str]:
        return [m.reason for m in self.heard if isinstance(m, Fault)]


def device_of_kind(config, kind):
    found = [d for d in config.devices if d.target == KINDS[kind]]
    assert len(found) == 1, f"the rig config must have one {kind} device, not {len(found)}"
    return found[0]


def line_for(kind: str) -> str:
    line = os.environ.get(LINES[kind], "")
    if not line:
        pytest.skip(f"set {LINES[kind]} to the DAQ line wired to the {kind} camera's trigger input")
    return line


def task_pair(config, flash, ueye) -> OutputConfig:
    """The task pair this run saves: the config's trigger names on the lines
    the environment names, and the board's analog outputs."""
    flash_line, ueye_line = line_for("dcam"), line_for("ueye")
    board = flash_line.split("/")[0]
    assert ueye_line.split("/")[0] == board, "both trigger lines must be on one board"
    return OutputConfig(
        rate=RATE,
        ao=tuple(Channel(f"{board}/ao{i}", f"ao{i}") for i in range(AO_CHANNELS)),
        do=(Channel(flash_line, flash.trigger), Channel(ueye_line, ueye.trigger)),
        buffer_samples=config.source.buffer_samples or 5000,
    )


def pulses(n: int, starts) -> np.ndarray:
    line = np.zeros(n, dtype=bool)
    for start in starts:
        line[start : start + WIDTH] = True
    return line


@contextlib.contextmanager
def record_folders(config, devices):
    """A fresh folder for this run inside each device's own save directory,
    deleted afterwards unless the recordings are to be kept. Devices that
    share a save directory share the folder too, as they would share the
    directory: their files differ by the device's name."""
    folders = {}
    tag = f"stacscope_rig_{uuid.uuid4().hex[:8]}"
    for device in devices:
        base = device.options.get("save_directory")
        assert base, f"devices.{device.name} has no save_directory in the rig config"
        folders[device.name] = pathlib.Path(base) / tag
    try:
        for folder in set(folders.values()):
            folder.mkdir(parents=True)
        yield folders
    finally:
        if not KEEP:
            for folder in set(folders.values()):
                shutil.rmtree(folder, ignore_errors=True)


def open_monitors(rig) -> None:
    """A buffer monitor on each camera's stream, each its own process. Left
    open for the user to close."""
    for name in rig.buffers.values():
        subprocess.Popen([sys.executable, str(ROOT / "scripts" / "monitor.py"), name], cwd=ROOT)


async def test_g13_both_cameras_record_a_cycle_the_coordinator_runs(config_path, report):
    """The rig as the instrument will run it, short of a session model: the
    composition root builds it from the file, each camera in a process of
    its own, and the Coordinator starts and stops the whole cycle. Every
    frame recorded is caused by a pulse the DAQ played, and each camera's
    file is in its own directory, so the counts below are the claim."""
    from saved_tasks import PREFIX, saved

    config = load_config(config_path)
    flash, ueye = device_of_kind(config, "dcam"), device_of_kind(config, "ueye")
    # Whole volumes in the ring, as a volume readout will want (user).
    slots = flash.options.get("n_slots")
    assert slots and slots % FLASH_FRAMES == 0, (
        f"devices.{flash.name}.n_slots is {slots}, not a multiple of {FLASH_FRAMES}"
    )
    tasks = task_pair(config, flash, ueye)
    ao_task, do_task = f"{PREFIX}rig_ao", f"{PREFIX}rig_do"
    config = dataclasses.replace(
        config, source=dataclasses.replace(config.source, ao_task=ao_task, do_task=do_task)
    )
    cycles = round(SOAK * CYCLES_PER_SECOND)

    with saved(tasks, ao=ao_task, do=do_task), record_folders(config, [flash, ueye]) as folders:
        launching = time.monotonic()
        rig = await Rig.start(config)
        try:
            launched = time.monotonic() - launching
            listener = Listener()
            rig.ref.tell(Subscribe(spawn(listener)))
            if MONITOR:
                open_monitors(rig)

            # The cycle, from what each camera says it can take.
            floors = {}
            for device in (flash, ueye):
                answer = await ask(await rig.device(device.name), GetShortestTriggerPeriod(), timeout=10.0)
                assert isinstance(answer, ShortestTriggerPeriod), f"{device.name}: {answer!r}"
                floors[device.name] = answer.seconds
            period = math.ceil(floors[flash.name] * RATE - 1e-6)  # samples, rounded up
            assert FLASH_FRAMES * period <= CYCLE, (
                f"{FLASH_FRAMES} Flash frames at {period / RATE * 1e3:.3f} ms take "
                f"{FLASH_FRAMES * period / RATE * 1e3:.2f} ms, more than the {CYCLE / RATE * 1e3:.0f} ms cycle"
            )
            assert floors[ueye.name] * RATE <= CYCLE, (
                f"the uEye's shortest trigger period is {floors[ueye.name] * 1e3:.2f} ms, "
                f"longer than the {CYCLE / RATE * 1e3:.0f} ms cycle"
            )

            ao = np.zeros((len(rig.output.ao), CYCLE))
            do = np.zeros((len(rig.output.do), CYCLE), dtype=bool)
            do[rig.line(flash.trigger)] = pulses(CYCLE, range(0, FLASH_FRAMES * period, period))
            do[rig.line(ueye.trigger)] = pulses(CYCLE, [0])
            quiet = np.zeros((len(rig.output.ao), QUIET)), np.zeros((len(rig.output.do), QUIET), dtype=bool)
            # In synchronous readout a pulse ends one exposure and starts the
            # next, so the run's first pulse ends nothing and its last starts
            # an exposure only a further pulse reads out: N pulses, N - 1
            # frames (measured, 2026-10-01). COOLDOWN plays that pulse, where
            # the next cycle's first would have been (user, 2026-10-01).
            closing = np.zeros((len(rig.output.ao), QUIET)), np.zeros((len(rig.output.do), QUIET), dtype=bool)
            closing[1][rig.line(flash.trigger)] = pulses(QUIET, [0])
            waveforms = [(Waveform.WARMUP, quiet), (Waveform.MAIN, (ao, do)), (Waveform.COOLDOWN, closing)]
            for name, (a, d) in waveforms:
                assert not a.any(), "every analog output stays at 0 V"
                assert await ask(await rig.source(), SetWaveform(name, a, d), timeout=10.0) == Accepted()

            for device in (flash, ueye):
                answer = await ask(await rig.device(device.name), SetSaveDirectory(str(folders[device.name])), timeout=10.0)
                assert isinstance(answer, SaveDirectory), f"{device.name}: {answer!r}"

            starting = time.monotonic()
            answer = await ask(rig.ref, StartAcquisition(cycles, "g13"), timeout=120.0)
            assert isinstance(answer, AcquisitionStarted), answer
            started = time.monotonic() - starting
            deadline = time.monotonic() + cycles / CYCLES_PER_SECOND + 180.0
            while (state := (await rig.status()).state) is not CoordinatorState.IDLE:
                assert time.monotonic() < deadline, f"still {state}: {listener.faults()}"
                await asyncio.sleep(0.05)
            ran = time.monotonic() - starting
            faults = listener.faults()
        finally:
            closing = time.monotonic()
            await rig.close()
            closed = time.monotonic() - closing

        flash_frames = dcimg_frames(folders[flash.name] / f"g13_{flash.name}.dcimg")
        ueye_pages = tiff_pages(folders[ueye.name] / f"g13_{ueye.name}.tif")
        report(
            f"launched in {launched:.1f} s; shortest trigger periods: flash {floors[flash.name] * 1e3:.4f} ms "
            f"(played every {period / RATE * 1e3:.2f} ms, {FLASH_FRAMES} take "
            f"{FLASH_FRAMES * period / RATE * 1e3:.2f} ms of {CYCLE / RATE * 1e3:.0f}), "
            f"ueye {floors[ueye.name] * 1e3:.3f} ms"
        )
        report(
            f"{cycles} cycles: started in {started:.2f} s, idle again after {ran:.2f} s, closed in {closed:.2f} s; "
            f"flash {flash_frames} frames (expected {FLASH_FRAMES * cycles}), "
            f"ueye {ueye_pages} pages (expected {cycles}); faults {faults}"
        )
        if KEEP:
            report(f"kept: {', '.join(sorted(str(f) for f in set(folders.values())))}")

    assert faults == []
    assert flash_frames == FLASH_FRAMES * cycles
    assert ueye_pages == cycles


def dcimg_frames(path) -> int:
    from dcimg_reader import DcimgFile

    with DcimgFile(path) as file:
        return file.frames


def tiff_pages(path) -> int:
    import tifffile

    with tifffile.TiffFile(path) as tf:
        return len(tf.pages)
