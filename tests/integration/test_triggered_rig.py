"""D12 — the whole of Milestone 3, end to end, with all three flows present.

The timing flow emits trigger pulses on a DO line. The simulated trigger bus is
the wire: it turns rising edges into frames at the virtual time they play, so
the frame count here is **caused by** the trigger count rather than by a fake
that was told how many frames to make. The control flow starts and stops
everyone. The data flow carries the frames, and a consumer reads them without
being part of either of the other two.

The software-timed pressure box is in the control flow and not the timing flow:
it hears the acquisition's phases and runs its own routine on the clock.

The acquisitions are not saved (an empty stem). Since 2026-10-01 the simulated
camera records through a TIFF recorder writing on a real thread, and the
Coordinator's stop timeout, in virtual time, expires before the thread can
finish. Recording is tested where real time is used: L11-L17, and this rig
recording through the Coordinator in test_recording_rig.py. On the
instrument the same wait is real: a recording camera's stop_timeout must
allow for its recorder's backlog (user, 2026-10-01).
"""

import asyncio
import uuid

import numpy as np

from stacscope.adapters.simulated import (
    SimulatedCamera,
    SimulatedPressureBox,
    SimulatedSignalOutput,
    TriggerBus,
    VirtualClock,
)
from stacscope.core.actors import Accepted, Actor, Fault, Subscribe, ask, spawn
from stacscope.core.buffer import ImageBuffer
from stacscope.core.ports import Channel, OutputConfig
from stacscope.core.protocol import (
    AcquisitionStarted,
    Phase,
    SetWaveform,
    StartAcquisition,
    Waveform,
)
from stacscope.core.timing import Coordinator, CoordinatorState, DeviceSpec, WaveformSource

RATE = 8.0  # samples/s: an 8-sample MAIN is a 1 s cycle
CONFIG = OutputConfig(
    rate=RATE,
    ao=(Channel("Dev1/ao0", "etl"),),
    do=(
        Channel("Dev1/port0/line0", "camera_trigger"),
        Channel("Dev1/port0/line2", "ueye_trigger"),  # claimed by no actor
    ),
    buffer_samples=12,
)
SHAPE = (4, 8)
CYCLES = 3
PER_CYCLE = 2  # camera pulses per MAIN cycle


def samples(n, *, camera_pulses=0):
    ao = np.zeros((1, n))
    do = np.zeros((2, n), dtype=bool)
    if camera_pulses:
        do[0, :: n // camera_pulses] = True
    do[1, 0] = True  # the uEye is triggered whether or not anything claims it
    return ao, do


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)

    def phases(self):
        return [m.phase for m in self.heard if hasattr(m, "phase")]


class System:
    def __init__(self):
        self.clock = VirtualClock()
        self.bus = TriggerBus(self.clock)
        self.output = SimulatedSignalOutput(self.clock, bus=self.bus)
        self.buffer_name = "t" + uuid.uuid4().hex[:20]
        self.camera = SimulatedCamera(
            self.buffer_name, bus=self.bus, trigger="camera_trigger", shape=SHAPE, n_slots=8
        )
        self.pressure = SimulatedPressureBox(step=0.4)
        self.coordinator = Coordinator(
            source=DeviceSpec("source", lambda: WaveformSource(self.output, CONFIG), ready_timeout=2.0),
            participants=[
                DeviceSpec("camera", lambda: self.camera, ready_timeout=2.0),
                DeviceSpec("pressure", lambda: self.pressure, ready_timeout=2.0),
            ],
        )
        self.ref = spawn(self.coordinator, clock=self.clock, name="coordinator")
        self.listener = Listener()
        self.ref.tell(Subscribe(spawn(self.listener)))

    def request(self, ref, msg):
        return asyncio.create_task(ask(ref, msg, timeout=100.0, clock=self.clock))

    async def load_waveforms(self):
        await self.clock.advance(0)
        source = self.coordinator.source
        waveforms = [
            (Waveform.WARMUP, samples(4)),
            (Waveform.MAIN, samples(8, camera_pulses=PER_CYCLE)),
            (Waveform.COOLDOWN, samples(4)),
        ]
        for name, (ao, do) in waveforms:
            reply = self.request(source, SetWaveform(name, ao, do))
            await self.clock.advance(0)
            assert await reply == Accepted()

    def pulses_played(self):
        """Rising edges the hardware actually emitted on the camera line."""
        line = self.output.do_played()[0]
        return int(np.count_nonzero(line & ~np.concatenate(([False], line[:-1]))))


async def test_d12_the_frames_a_consumer_reads_are_the_triggers_that_were_emitted():
    system = System()
    await system.load_waveforms()

    starting = system.request(system.ref, StartAcquisition(CYCLES, ""))
    await system.clock.advance(0.5)
    assert await starting == AcquisitionStarted()

    reader = ImageBuffer.open(system.buffer_name)
    try:
        await system.clock.advance(10.0)

        assert system.coordinator.state is CoordinatorState.IDLE, "it ended on its own"
        assert system.output.underflow is None
        assert system.pulses_played() == CYCLES * PER_CYCLE

        assert reader.published() == system.pulses_played(), "one frame per trigger"
        cursor = reader.cursor(reader.oldest())
        frames = [cursor.next() for _ in range(reader.published())]
        assert [f.index for f in frames] == list(range(CYCLES * PER_CYCLE))
        assert [f.meta["hw_frame"] for f in frames] == list(range(CYCLES * PER_CYCLE))
        assert {f.acquisition_id for f in frames} == {1}

        last = np.asarray(frames[-1].data)
        lit = set(np.flatnonzero(last.any(axis=0)).tolist())
        drawn = {(c + frames[-1].index) % SHAPE[1] for c in range(4)}  # the producer's bar
        assert lit == drawn, "the frame holds the picture that frame index should draw"
        del frames, last  # views of the segment: the reader cannot close under them
    finally:
        reader.close()


async def test_d12_a_clean_acquisition_raises_no_fault():
    """The source publishes events the Coordinator has no use for:
    ``MainApplied`` at cycle 0 of every acquisition. Until 2026-09-30 the
    Coordinator answered each one with a Fault to every participant and
    client, on every acquisition."""
    system = System()
    await system.load_waveforms()

    starting = system.request(system.ref, StartAcquisition(CYCLES, ""))
    await system.clock.advance(0.5)
    assert await starting == AcquisitionStarted()
    await system.clock.advance(10.0)

    assert system.coordinator.state is CoordinatorState.IDLE
    assert [m for m in system.listener.heard if isinstance(m, Fault)] == []


async def test_d12_nothing_is_produced_before_go_or_after_the_stop():
    system = System()
    await system.load_waveforms()

    starting = system.request(system.ref, StartAcquisition(CYCLES, ""))
    await system.clock.advance(0)
    assert system.camera.writer.published() == 0, "armed, but the source has not gone"

    assert await starting == AcquisitionStarted()
    await system.clock.advance(10.0)
    produced = system.camera.writer.published()

    await system.clock.advance(10.0)
    assert system.camera.writer.published() == produced, "no triggers, no frames"
    assert system.camera.capture.missed == 0, "and no pulse reached a camera that was not armed"


async def test_d12_the_software_timed_device_runs_against_the_same_acquisition():
    system = System()
    await system.load_waveforms()

    starting = system.request(system.ref, StartAcquisition(CYCLES, ""))
    await system.clock.advance(0.5)
    assert await starting == AcquisitionStarted()
    await system.clock.advance(10.0)

    steps = system.pressure.log
    assert [p for p, _ in steps] == [5.0, 10.0, 15.0, 0.0], "the whole ramp, then vented"
    # RUN is entered at t = 0 here: no device is slow to arm, and a phase is
    # announced when it is committed rather than when it plays.
    assert [round(t, 6) for _, t in steps[:3]] == [0.4, 0.8, 1.2], "its own clock, its own scale"
    assert steps[-1][1] > steps[-2][1], "vented after the last step, when the run ended"
    assert system.pressure.pressure == 0.0
    assert Phase.RUN in system.listener.phases()


async def test_d12_a_second_acquisition_counts_its_own_triggers():
    """Nothing carries over: not the bus's idea of which lines were high, not
    the drawing, not the acquisition id a consumer reads."""
    system = System()
    await system.load_waveforms()

    for acquisition in (1, 2):
        starting = system.request(system.ref, StartAcquisition(CYCLES, ""))
        await system.clock.advance(0.5)
        assert await starting == AcquisitionStarted()
        await system.clock.advance(10.0)

        assert system.coordinator.state is CoordinatorState.IDLE
        assert system.camera.acquisition_id == acquisition
        assert system.camera.writer.published() == acquisition * CYCLES * PER_CYCLE
        assert system.camera.capture.missed == 0

    reader = ImageBuffer.open(system.buffer_name)
    try:
        frames = [reader.cursor(i).next() for i in range(CYCLES * PER_CYCLE, 2 * CYCLES * PER_CYCLE)]
        assert {f.acquisition_id for f in frames} == {2}, "the consumer sees the second one"
        # The stamps are the camera's own frame numbers, which restart with
        # each capture, not the buffer's indices, which never go back. (Until
        # the rig was rebuilt over a capture port, 2026-09-24, the simulated
        # camera numbered on across acquisitions; DCAM's reference does not
        # say which a real camera does.)
        assert [f.meta["hw_frame"] for f in frames] == list(
            range(CYCLES * PER_CYCLE)
        ), "the camera's own frame numbers, from its own start"
        del frames  # views of the segment: the reader cannot close under them
    finally:
        reader.close()
