"""Hamamatsu cameras through DCAM-API.

DcamFrameCapture drives a camera's frame grabber for the image-streaming
role: the driver writes each frame straight into an image buffer. Windows
only, and DCAM-API must be installed. Nothing in the core imports this
package.
"""
# For end use and not for commercial purposes.

from stacscope.adapters.dcam._api import DcamUnavailable
from stacscope.adapters.dcam.capture import DcamError, DcamFrameCapture

__all__ = ["DcamError", "DcamFrameCapture", "DcamUnavailable"]
