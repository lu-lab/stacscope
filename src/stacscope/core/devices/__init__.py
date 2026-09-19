"""The participant side of the device protocol. No hardware."""

from stacscope.core.devices.device import DeviceState, NotReady, TimedDevice
from stacscope.core.devices.recording import Recording
from stacscope.core.devices.software_timed import SoftwareTimed
from stacscope.core.devices.streaming import Streaming

__all__ = [
    "DeviceState",
    "NotReady",
    "Recording",
    "SoftwareTimed",
    "Streaming",
    "TimedDevice",
]
