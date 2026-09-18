"""Message types. Plain data, serializable by construction."""

from stacscope.core.protocol.messages import (
    AcquisitionStarted,
    AcquisitionStopped,
    DeviceReady,
    DeviceStopped,
    Finish,
    Go,
    Phase,
    Segment,
    SetSegment,
    SourceFinished,
    SourcePhase,
    StartAcquisition,
    StartDevice,
    StopAcquisition,
    StopDevice,
)
from stacscope.core.protocol.registry import MESSAGES, MessageRegistry, message

__all__ = [
    "MESSAGES",
    "AcquisitionStarted",
    "AcquisitionStopped",
    "DeviceReady",
    "DeviceStopped",
    "Finish",
    "Go",
    "MessageRegistry",
    "Phase",
    "Segment",
    "SetSegment",
    "SourceFinished",
    "SourcePhase",
    "StartAcquisition",
    "StartDevice",
    "StopAcquisition",
    "StopDevice",
    "message",
]
