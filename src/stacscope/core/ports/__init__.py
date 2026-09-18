"""Abstract ports the core depends on. Adapters implement them."""

from stacscope.core.ports.clock import Clock, MonotonicClock
from stacscope.core.ports.signal_output import Channel, OutputConfig, SignalOutput, Underflow

__all__ = ["Channel", "Clock", "MonotonicClock", "OutputConfig", "SignalOutput", "Underflow"]
