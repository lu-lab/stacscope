"""L13 and L14, end to end: a recorded acquisition through the Coordinator.

D12 is the same rig in virtual time, and runs unsaved: its stop timeout
expires before a recorder writing on a real thread can finish. Here the
whole rig runs on the real clock, so a recorded acquisition goes through
the Coordinator as it will on the instrument. The waveforms are short, so
each acquisition takes a fraction of a second.

- L13: an acquisition with a stem ends with a file holding every frame the
  triggers caused, and no fault, even when the disk is behind at the stop.
- L14: a recorder that fails mid-acquisition reaches the Coordinator as the
  camera's fatal Fault, and the Coordinator aborts the acquisition then,
  not at the end of its cycles.

The camera's stop_timeout is set apart from its ready_timeout, as a
recording camera's must be on the instrument: a stop waits for the
recorder's backlog (user, 2026-10-01).
"""

import asyncio
import time
import uuid

import numpy as np
import pytest
import tifffile

from stacscope.adapters.simulated import (
    SimulatedCamera,
    SimulatedSignalOutput,
    TriggerBus,
)
from stacscope.core.actors import Accepted, Actor, Fault, Subscribe, ask, spawn
from stacscope.core.ports import Channel, MonotonicClock, OutputConfig
from stacscope.core.protocol import AcquisitionStarted, SetWaveform, StartAcquisition, Waveform
from stacscope.core.recording import TIFFRecorder
from stacscope.core.timing import Coordinator, CoordinatorState, DeviceSpec, WaveformSource

DEADLINE = 5.0
RATE = 100.0  # samples/s: a 10-sample MAIN is a 0.1 s cycle
CONFIG = OutputConfig(
    rate=RATE,
    ao=(Channel("Dev1/ao0", "etl"),),
    do=(Channel("Dev1/port0/line0", "camera_trigger"),),
    # A second of output queued: on the real clock a loop held up longer
    # than the buffer's slack runs the output dry and loses the source. At
    # 0.2 s, a Windows CI runner did (2026-10-01).
    buffer_samples=100,
)
SHAPE = (4, 8)
PER_CYCLE = 2  # camera pulses per MAIN cycle


def samples(n, *, camera_pulses=0):
    ao = np.zeros((1, n))
    do = np.zeros((1, n), dtype=bool)
    if camera_pulses:
        do[0, :: n // camera_pulses] = True
    return ao, do


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)

    def faults(self):
        return [m for m in self.heard if isinstance(m, Fault)]


class Failing(TIFFRecorder):
    """A disk that fills after two frames."""

    def write_frame(self, pixels, meta, description):
        if self.frames_written == 2:
            raise OSError("no space left on the device")
        super().write_frame(pixels, meta, description)


class FailingCamera(SimulatedCamera):
    recorder = Failing


class Slow(TIFFRecorder):
    """A disk slower than the camera: 80 ms a frame, against a frame every
    50 ms. It falls behind by under four frames in 0.5 s, inside the ring."""

    def write_frame(self, pixels, meta, description):
        time.sleep(0.08)
        super().write_frame(pixels, meta, description)


class SlowCamera(SimulatedCamera):
    recorder = Slow


class System:
    """D12's rig, recording, on the real clock."""

    def __init__(self, save_directory, camera_cls=SimulatedCamera):
        self.clock = MonotonicClock()
        self.bus = TriggerBus(self.clock)
        self.output = SimulatedSignalOutput(self.clock, bus=self.bus)
        self.camera = camera_cls(
            "r" + uuid.uuid4().hex[:20],
            bus=self.bus,
            trigger="camera_trigger",
            shape=SHAPE,
            n_slots=8,
            save_directory=save_directory,
        )
        self.source = WaveformSource(self.output, CONFIG)
        self.coordinator = Coordinator(
            # The source's stop drains the queued second of output.
            source=DeviceSpec("source", lambda: self.source, ready_timeout=2.0, stop_timeout=DEADLINE),
            participants=[
                DeviceSpec("camera", lambda: self.camera, ready_timeout=2.0, stop_timeout=DEADLINE),
            ],
        )
        self.ref = spawn(self.coordinator, name="coordinator")
        self.listener = Listener()
        self.ref.tell(Subscribe(spawn(self.listener, name="listener")))

    async def started(self):
        await asyncio.wait_for(self.coordinator.started.wait(), DEADLINE)
        await asyncio.wait_for(self.camera.started.wait(), DEADLINE)
        source = self.coordinator.source
        waveforms = [
            (Waveform.WARMUP, samples(4)),
            (Waveform.MAIN, samples(10, camera_pulses=PER_CYCLE)),
            (Waveform.COOLDOWN, samples(4)),
        ]
        for name, (ao, do) in waveforms:
            assert await ask(source, SetWaveform(name, ao, do), timeout=DEADLINE) == Accepted()

    async def acquire(self, cycles, stem):
        """Start an acquisition and wait for the Coordinator to be idle again."""
        answer = await ask(self.ref, StartAcquisition(cycles, stem), timeout=DEADLINE)
        assert answer == AcquisitionStarted(), answer
        started = time.monotonic()
        deadline = started + DEADLINE
        while self.coordinator.state is not CoordinatorState.IDLE:
            assert time.monotonic() < deadline, (
                f"still {self.coordinator.state} after {DEADLINE} s: {[f.reason for f in self.listener.faults()]}"
            )
            await asyncio.sleep(0.01)
        return time.monotonic() - started

    def pulses_played(self):
        line = self.output.do_played()[0]
        return int(np.count_nonzero(line & ~np.concatenate(([False], line[:-1]))))

    async def close(self):
        # Stopping the Coordinator would now stop the others too (CM10); each
        # is stopped and waited for here, camera first, all the same.
        for actor in (self.camera, self.source, self.coordinator):
            if not actor.stopped.is_set():
                actor.stop()
                await asyncio.wait_for(actor.stopped.wait(), DEADLINE)


@pytest.fixture
async def systems():
    made = []

    def make(*args, **kwargs):
        made.append(System(*args, **kwargs))
        return made[-1]

    yield make
    for system in made:
        await system.close()


def read(path):
    with tifffile.TiffFile(path) as tf:
        return np.stack([page.asarray() for page in tf.pages]) if tf.pages else np.zeros((0, 0, 0))


async def test_l13_end_to_end_a_recorded_acquisition_holds_every_frame_triggered(systems, tmp_path):
    """D12's claim, saved: the frames in the file are the triggers that
    played. The disk is behind when the triggers end, and the camera's
    stop waits for it, so the file is whole once the Coordinator is idle.
    A stop that let go of the recorder early, or a stop timeout too short
    for the backlog, would each be a fault here."""
    system = systems(tmp_path, camera_cls=SlowCamera)
    await system.started()

    await system.acquire(5, "run-01")

    assert system.pulses_played() == 5 * PER_CYCLE
    frames = read(tmp_path / "run-01_camera.tif")
    assert len(frames) == 5 * PER_CYCLE, "one page per trigger"
    assert system.listener.faults() == []


async def test_l14_end_to_end_a_recorder_that_fails_aborts_the_acquisition(systems, tmp_path):
    """A disk that fills cannot be recovered from mid-acquisition: the
    frames it missed are gone from the ring. The camera publishes it as a
    fatal Fault, and the Coordinator stops everyone at once, rather than
    running its cycles to the end with nothing saved."""
    cycles = 100  # 10 s of MAIN, were it to run on
    system = systems(tmp_path, camera_cls=FailingCamera)
    await system.started()

    took = await system.acquire(cycles, "run-01")

    # The fault comes at the third frame; the abort then waits for the
    # queued second of output to drain.
    assert took < 3.0, f"the acquisition ran on for {took:.1f} s"
    assert system.pulses_played() < cycles * PER_CYCLE, "the source was stopped early"
    reasons = [f.reason for f in system.listener.faults()]
    assert any("no space left" in r for r in reasons), reasons
    assert any(r.startswith("stopped by a fatal fault") for r in reasons), reasons
    assert not any("stop not confirmed" in r for r in reasons), reasons
    assert len(read(tmp_path / "run-01_camera.tif")) == 2, "what was written stays readable"
