"""The data flow: a shared-memory ring of frames, one producer, many readers."""

from stacscope.core.buffer.counter import UnsupportedPlatform
from stacscope.core.buffer.cursor import Cursor, Frame, Group, Overrun
from stacscope.core.buffer.frame_buffer import (
    FrameBuffer,
    FrameReader,
    FrameWriter,
    check_name,
)
from stacscope.core.buffer.layout import Layout, LayoutError
from stacscope.core.buffer.metadata import MetaField, Metadata, TimeSource

__all__ = [
    "Cursor",
    "Frame",
    "FrameBuffer",
    "FrameReader",
    "FrameWriter",
    "Group",
    "Layout",
    "LayoutError",
    "MetaField",
    "Metadata",
    "Overrun",
    "TimeSource",
    "UnsupportedPlatform",
    "check_name",
]
