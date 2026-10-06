"""Recorders: consumers of an image buffer that write its frames to disk.

A camera whose driver writes its own files (a DCAM camera's DCIMG) does not
need one. Any other camera that streams into an image buffer records through
a recorder it starts for each acquisition. TIFFRecorder writes a BigTIFF
stack, which is the usual format for a raw sequence of images.
"""

from stacscope.core.recording.tiff_recorder import DEVICE_TICKS_TAG, HOST_TIME_TAG, TIFFRecorder

__all__ = ["DEVICE_TICKS_TAG", "HOST_TIME_TAG", "TIFFRecorder"]
