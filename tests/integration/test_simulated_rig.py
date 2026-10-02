"""The whole Milestone 1 system, simulated: Coordinator + WaveformSource over a
simulated SignalOutput + participants, driven the way a client will drive it.
"""

import asyncio

import numpy as np

from stacscope.adapters.simulated import SimulatedDevice, SimulatedSignalOutput, VirtualClock
from stacscope.core.actors import Accepted, Actor, Fault, Subscribe, ask, spawn
from stacscope.core.ports import Channel, OutputConfig
from stacscope.core.protocol import (
    AcquisitionStarted,
    AcquisitionStopped,
    SetWaveform,
    StartAcquisition,
    StopAcquisition,
    Waveform,
)
from stacscope.core.timing import Coordinator, CoordinatorState, DeviceSpec, WaveformSource

CONFIG = OutputConfig(
    rate=8.0,
    ao=(Channel("Dev1/ao0", "etl"),),
    do=(Channel("Dev1/port0/line0", "camera_trigger"), Channel("Dev1/port0/line2", "ueye_trigger")),
    buffer_samples=12,
)


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)


class System:
    def __init__(self):
        self.clock = VirtualClock()
        self.output = SimulatedSignalOutput(self.clock)
        self.camera = SimulatedDevice(ready_delay=0.5)
        self.coordinator = Coordinator(
            source=DeviceSpec("source", lambda: WaveformSource(self.output, CONFIG), ready_timeout=2.0),
            participants=[DeviceSpec("camera", lambda: self.camera, ready_timeout=2.0)],
        )
        self.ref = spawn(self.coordinator, clock=self.clock, name="coordinator")
        self.listener = Listener()
        self.ref.tell(Subscribe(spawn(self.listener)))

    def request(self, ref, msg):
        return asyncio.create_task(ask(ref, msg, timeout=100.0, clock=self.clock))

    async def load_waveforms(self):
        """A client talks to the source directly for waveforms, not via the Coordinator."""
        await self.clock.advance(0)
        source = self.coordinator.source
        for waveform, n in [(Waveform.WARMUP, 4), (Waveform.MAIN, 8), (Waveform.COOLDOWN, 4)]:
            do = np.zeros((2, n), dtype=bool)
            do[:, 0] = True
            reply = self.request(source, SetWaveform(waveform, np.zeros((1, n)), do))
            await self.clock.advance(0)
            assert await reply == Accepted()


async def test_a_full_acquisition_end_to_end():
    system = System()
    await system.load_waveforms()

    starting = system.request(system.ref, StartAcquisition(-1, "run-01"))
    await system.clock.advance(1.0)
    assert isinstance(await starting, AcquisitionStarted)

    await system.clock.advance(4.0)
    stopping = system.request(system.ref, StopAcquisition())
    await system.clock.advance(5.0)

    assert isinstance(await stopping, AcquisitionStopped)
    assert system.output.underflow is None
    labels = [label for label, _ in system.output.timeline]
    assert labels[0] == "warmup" and labels[-1] == "cooldown"
    assert set(labels[1:-1]) == {"main"}
    assert system.output.samples_played() == system.output.samples_written
    assert [c[0] for c in system.camera.calls] == ["start", "stop"]
    assert system.coordinator.state is CoordinatorState.IDLE


async def test_an_underflow_loses_the_source_and_stops_the_camera():
    system = System()
    await system.load_waveforms()
    starting = system.request(system.ref, StartAcquisition(-1, "run-01"))
    await system.clock.advance(1.0)
    assert isinstance(await starting, AcquisitionStarted)

    system.output.stall_next_write(5.0)
    await system.clock.advance(10.0)

    # The source died of it: a lost required device (CM22), nothing latched.
    assert system.coordinator.state is CoordinatorState.IDLE
    assert system.coordinator.source is None
    assert system.output.stopped
    assert "stop" in [c[0] for c in system.camera.calls]
    lost = [m for m in system.listener.heard if isinstance(m, Fault) and "timing source lost" in m.reason]
    assert lost and "Underflow" in lost[0].reason
