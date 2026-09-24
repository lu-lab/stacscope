"""Simulated implementations of every port. First-class, not test scaffolding."""

from stacscope.adapters.simulated.camera import SimulatedCamera, SimulatedFreeRunningCamera
from stacscope.adapters.simulated.clock import VirtualClock
from stacscope.adapters.simulated.device import SimulatedDevice
from stacscope.adapters.simulated.faulty import Fail, FaultyDevice
from stacscope.adapters.simulated.image_generator import TestImageGenerator
from stacscope.adapters.simulated.link import SimulatedLink
from stacscope.adapters.simulated.pressure_box import SimulatedPressureBox
from stacscope.adapters.simulated.signal_output import SimulatedSignalOutput
from stacscope.adapters.simulated.timing_source import SimulatedTimingSource
from stacscope.adapters.simulated.trigger_bus import TriggerBus

__all__ = [
    "Fail",
    "FaultyDevice",
    "SimulatedCamera",
    "SimulatedDevice",
    "SimulatedFreeRunningCamera",
    "SimulatedLink",
    "SimulatedPressureBox",
    "SimulatedSignalOutput",
    "SimulatedTimingSource",
    "TestImageGenerator",
    "TriggerBus",
    "VirtualClock",
]
