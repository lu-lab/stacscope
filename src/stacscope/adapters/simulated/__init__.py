"""Simulated implementations of every port. First-class, not test scaffolding."""

from stacscope.adapters.simulated.clock import VirtualClock
from stacscope.adapters.simulated.device import SimulatedDevice
from stacscope.adapters.simulated.link import SimulatedLink
from stacscope.adapters.simulated.pattern_producer import PatternProducer
from stacscope.adapters.simulated.signal_output import SimulatedSignalOutput
from stacscope.adapters.simulated.timing_source import SimulatedTimingSource

__all__ = [
    "PatternProducer",
    "SimulatedDevice",
    "SimulatedLink",
    "SimulatedSignalOutput",
    "SimulatedTimingSource",
    "VirtualClock",
]
