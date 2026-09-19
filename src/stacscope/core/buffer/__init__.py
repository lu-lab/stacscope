"""The data flow: a shared-memory ring of frames, one producer, many readers."""

from stacscope.core.buffer.frame_buffer import FrameBuffer, FrameReader, FrameWriter
from stacscope.core.buffer.layout import Layout, LayoutError

__all__ = ["FrameBuffer", "FrameReader", "FrameWriter", "Layout", "LayoutError"]
