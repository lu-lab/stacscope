"""The simulated wire: rising edges become callbacks when they play.

Nothing in core knows this exists. It stands in for the copper between a DO
line and a device's trigger input, so that a test can assert frames were
*caused by* pulses rather than counted out by a fake.
"""

import numpy as np

from stacscope.adapters.simulated import SimulatedSignalOutput, TriggerBus, VirtualClock
from stacscope.core.ports import Channel, OutputConfig

RATE = 8.0
CONFIG = OutputConfig(
    rate=RATE,
    ao=(Channel("Dev1/ao0", "etl"),),
    do=(
        Channel("Dev1/port0/line0", "camera_trigger"),
        Channel("Dev1/port0/line2", "ueye_trigger"),
    ),
    buffer_samples=64,
)


class Rig:
    def __init__(self):
        self.clock = VirtualClock()
        self.bus = TriggerBus(self.clock)
        self.output = SimulatedSignalOutput(self.clock, bus=self.bus)
        self.fired = {}

    def watch(self, label, tag=None):
        tag = tag or label
        self.fired.setdefault(tag, [])
        self.bus.subscribe(label, lambda: self.fired[tag].append(self.clock.now()))

    async def configure(self):
        await self.output.configure(CONFIG)

    async def write(self, camera, ueye=None):
        n = len(camera)
        do = np.zeros((2, n), dtype=bool)
        do[0] = camera
        if ueye is not None:
            do[1] = ueye
        await self.output.write(np.zeros((1, n)), do)


async def test_an_edge_fires_when_its_sample_plays():
    rig = Rig()
    await rig.configure()
    rig.watch("camera_trigger")
    await rig.write([False, True, False, False, True, False, False, False])

    await rig.output.go()
    await rig.clock.advance(1.0)

    assert rig.fired["camera_trigger"] == [1 / RATE, 4 / RATE]


async def test_nothing_fires_before_output_starts():
    """Waveforms are written during the pre-roll, while the board is idle."""
    rig = Rig()
    await rig.configure()
    rig.watch("camera_trigger")
    await rig.write([True, False, True, False])

    await rig.clock.advance(10.0)
    assert rig.fired["camera_trigger"] == []

    await rig.output.go()
    await rig.clock.advance(1.0)
    assert len(rig.fired["camera_trigger"]) == 2, "and then they all fire, in order"


async def test_only_a_rising_edge_fires_however_long_the_line_stays_high():
    rig = Rig()
    await rig.configure()
    rig.watch("camera_trigger")
    await rig.write([True, True, True, False, True, True])

    await rig.output.go()
    await rig.clock.advance(1.0)

    assert rig.fired["camera_trigger"] == [0.0, 4 / RATE]


async def test_a_line_held_high_across_two_writes_does_not_fire_twice():
    rig = Rig()
    await rig.configure()
    rig.watch("camera_trigger")
    await rig.write([False, True, True, True])
    await rig.write([True, True, False, True])

    await rig.output.go()
    await rig.clock.advance(2.0)

    assert rig.fired["camera_trigger"] == [1 / RATE, 7 / RATE]


async def test_devices_subscribe_by_label_and_a_line_may_have_two_watchers():
    rig = Rig()
    await rig.configure()
    rig.watch("camera_trigger", "camera")
    rig.watch("camera_trigger", "recorder")
    rig.watch("ueye_trigger")
    await rig.write([True, False], ueye=[False, True])

    await rig.output.go()
    await rig.clock.advance(1.0)

    assert rig.fired["camera"] == [0.0] and rig.fired["recorder"] == [0.0]
    assert rig.fired["ueye_trigger"] == [1 / RATE]


async def test_a_line_nobody_watches_still_plays():
    """The uEye is triggered by the DAQ and controlled by µManager: no actor
    claims its line, and it must be emitted anyway."""
    rig = Rig()
    await rig.configure()
    await rig.write([False, False], ueye=[True, False])

    await rig.output.go()
    await rig.clock.advance(1.0)

    assert rig.output.do_played()[1].tolist() == [True, False]


async def test_stopping_drops_edges_that_never_played():
    rig = Rig()
    await rig.configure()
    rig.watch("camera_trigger")
    await rig.write([True] + [False] * 7 + [True] + [False] * 7)

    await rig.output.go()
    await rig.clock.advance(0.5)
    assert len(rig.fired["camera_trigger"]) == 1

    await rig.output.stop()
    await rig.clock.advance(10.0)

    assert len(rig.fired["camera_trigger"]) == 1, "an edge that never played never fires"


async def test_configuring_again_forgets_the_previous_line_states():
    rig = Rig()
    await rig.configure()
    rig.watch("camera_trigger")
    await rig.write([False, True])  # ends high

    await rig.configure()
    await rig.write([True, False])
    await rig.output.go()
    await rig.clock.advance(1.0)

    assert rig.fired["camera_trigger"] == [0.0], "a fresh start is a rising edge"
