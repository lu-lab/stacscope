"""Abstract ports the core depends on. Adapters implement them."""

from stacscope.core.ports.clock import Clock, MonotonicClock
from stacscope.core.ports.frame_capture import STAMP_DTYPE, FrameCapture, FrameFormat, FramesLost
from stacscope.core.ports.signal_output import Channel, OutputConfig, SignalOutput, Underflow

__all__ = [
    "STAMP_DTYPE",
    "Channel",
    "Clock",
    "FrameCapture",
    "FrameFormat",
    "FramesLost",
    "MonotonicClock",
    "OutputConfig",
    "SignalOutput",
    "Underflow",
]
