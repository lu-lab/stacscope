"""Per-frame metadata: one fixed record per slot, in a table outside the slots.

Record layout (little-endian, ``meta_record_bytes`` = 40):

======  ======  ==============  ===============================================
offset  format  field           meaning
======  ======  ==============  ===============================================
0       u64     sequence        global frame index; always written
8       u64     hw_frame        the device's own frame number
16      i64     device_ticks    device clock, raw ticks (see device_clock_hz)
24      i64     host_time_ns    host wall clock when the producer saw the frame
32      u64     flags           reserved, 0
======  ======  ==============  ===============================================

Record ``s`` sits at ``meta_offset + s * meta_record_bytes`` and describes the
frame in slot ``s``. The header's ``metadata_fields`` says which of
``hw_frame`` / ``device_ticks`` / ``host_time_ns`` the producer fills; device
time is kept as raw ticks, never converted, because it is the device's.
"""

import enum
from dataclasses import dataclass

import numpy as np

from stacscope.core.buffer.header import META_RECORD_BYTES

META_DTYPE = np.dtype(
    {
        "names": ["sequence", "hw_frame", "device_ticks", "host_time_ns", "flags"],
        "formats": ["<u8", "<u8", "<i8", "<i8", "<u8"],
        "offsets": [0, 8, 16, 24, 32],
        "itemsize": META_RECORD_BYTES,
    }
)


class MetaField(enum.IntFlag):
    """Which optional record fields the producer fills."""

    HW_FRAME = 1
    DEVICE_TIME = 2
    HOST_TIME = 4


class TimeSource(enum.IntEnum):
    """Where ``device_ticks`` come from. Clocks from different sources are not
    comparable, and a "camera" timestamp is not always the camera's (DCAM's may
    be the frame grabber's or the driver's)."""

    NONE = 0
    CAMERA = 1
    CAPTURE_DEVICE = 2
    DRIVER = 3


@dataclass(frozen=True)
class Metadata:
    """What the producer's per-frame metadata holds. The default declares
    nothing beyond ``sequence``."""

    fields: MetaField = MetaField(0)
    device_time_source: TimeSource = TimeSource.NONE
    device_clock_hz: float = 0.0
