"""Hamamatsu cameras through DCAM-API.

OrcaFlash is the camera as a device: it streams frames into an image buffer
and takes its settings by message (SetProperty, SetPropertyLabel,
GetProperty), and records acquisitions to DCIMG files in a directory set by
SetSaveDirectory, a core message. DcamFrameCapture, beneath it, drives the
camera's frame grabber: the driver writes each frame straight into the
buffer. Windows only, and DCAM-API must be installed. Nothing in the core
imports this package.
"""
# For end use and not for commercial purposes.

from stacscope.adapters.dcam._api import DcamUnavailable
from stacscope.adapters.dcam.camera import OrcaFlash
from stacscope.adapters.dcam.capture import DcamError, DcamFrameCapture, RecordingStatus
from stacscope.adapters.dcam.messages import (
    GetProperty,
    PropertyValue,
    SetProperty,
    SetPropertyLabel,
)

__all__ = [
    "DcamError",
    "DcamFrameCapture",
    "DcamUnavailable",
    "GetProperty",
    "OrcaFlash",
    "PropertyValue",
    "RecordingStatus",
    "SetProperty",
    "SetPropertyLabel",
]
