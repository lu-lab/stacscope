"""Simulated implementations of every port. First-class, not test scaffolding."""

from stacscope.adapters.simulated.camera import SimulatedCamera, SimulatedStreamingCamera
from stacscope.adapters.simulated.clock import VirtualClock
from stacscope.adapters.simulated.device import SimulatedDevice
from stacscope.adapters.simulated.faulty import Fail, FaultyDevice
from stacscope.adapters.simulated.link import SimulatedLink
from stacscope.adapters.simulated.pattern_producer import PatternProducer
from stacscope.adapters.simulated.pressure_box import SimulatedPressureBox
from stacscope.adapters.simulated.signal_output import SimulatedSignalOutput
from stacscope.adapters.simulated.timing_source import SimulatedTimingSource
from stacscope.adapters.simulated.trigger_bus import TriggerBus

__all__ = [
    "Fail",
    "FaultyDevice",
    "PatternProducer",
    "SimulatedCamera",
    "SimulatedDevice",
    "SimulatedLink",
    "SimulatedPressureBox",
    "SimulatedSignalOutput",
    "SimulatedStreamingCamera",
    "SimulatedTimingSource",
    "TriggerBus",
    "VirtualClock",
]
