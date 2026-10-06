"""IDS uEye cameras through the uEye API (IDS Software Suite, with pyueye).

UEyeCamera is the camera as a device: it streams frames into an image
buffer, lists its properties for GetProperties and SetProperty (pixel
format, area of interest, pixel clock, frame rate, exposure, trigger), and
says the shortest trigger period its settings allow
(GetShortestTriggerPeriod). UEyeFrameCapture, beneath it, drives the
camera's frame grabber: the driver writes each frame straight into the
buffer. The IDS Software Suite and the "ueye" extra must be installed to
use a camera; this package imports without them. Nothing in the core
imports this package.
"""

from stacscope.adapters.ueye.camera import UEyeCamera
from stacscope.adapters.ueye.capture import AOIGrid, UEyeError, UEyeFrameCapture, UEyeUnavailable

__all__ = [
    "AOIGrid",
    "UEyeCamera",
    "UEyeError",
    "UEyeFrameCapture",
    "UEyeUnavailable",
]
