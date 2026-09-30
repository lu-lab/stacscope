"""IDS uEye cameras through the uEye API (IDS Software Suite, with pyueye).

UEyeFrameCapture drives the camera's frame grabber: the driver writes each
frame straight into an image buffer's slots. The IDS Software Suite and the
"ueye" extra must be installed to use a camera; this package imports
without them. Nothing in the core imports this package.
"""

from stacscope.adapters.ueye.capture import UEyeError, UEyeFrameCapture, UEyeUnavailable

__all__ = ["UEyeError", "UEyeFrameCapture", "UEyeUnavailable"]
