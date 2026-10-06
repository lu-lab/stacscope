"""Hamamatsu cameras through DCAM-API.

DcamCamera is the camera as a device: it streams frames into an image buffer,
lists the camera's own properties for GetProperties and SetProperty, and
records acquisitions to DCIMG files in a directory set by SetSaveDirectory.
DcamFrameCapture, beneath it, drives the camera's frame grabber: the driver
writes each frame straight into the buffer. Windows only, and DCAM-API must be installed. Nothing in the core
imports this package.
"""
# For end use and not for commercial purposes.

from stacscope.adapters.dcam._api import DcamUnavailable
from stacscope.adapters.dcam.camera import DcamCamera
from stacscope.adapters.dcam.capture import (
    DcamError,
    DcamFrameCapture,
    PropertyAttributes,
    RecordingStatus,
)

__all__ = [
    "DcamCamera",
    "DcamError",
    "DcamFrameCapture",
    "DcamUnavailable",
    "PropertyAttributes",
    "RecordingStatus",
]
