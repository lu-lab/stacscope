"""The image buffer's binary header — the contract for every consumer.

======  ======  ====================  ==============================================
offset  format  field                 meaning
======  ======  ====================  ==============================================
0       4s      magic                 ``b"STCB"``
4       u32     version               ``1``
8       u32     header_bytes          bytes before the metadata table
12      u32     meta_record_bytes     size of one per-slot metadata record
16      u64     capacity_bytes        size of the data region, fixed at creation
24      u64     max_slots             rows in the metadata table, fixed at creation
32      u64     data_offset           start of slot 0; page-aligned
40      u64     meta_offset           start of the metadata table
64      u64     layout_generation     changes whenever the layout block changes
72      u64     n_slots               slots in the ring (<= max_slots)
80      u64     slot_bytes            distance between consecutive slots
88      u64     frame_offset          first pixel's offset within its slot
96      8s      dtype                 numpy typestr, NUL-padded (e.g. ``<u2``)
104     u32     ndim                  number of frame dimensions (<= 6)
108     u32     in_flight             frames the device may be writing ahead
112     6*u64   shape                 frame shape; first ``ndim`` entries used
160     6*i64   strides               frame strides in bytes; first ``ndim`` used
208     u32     metadata_fields       which metadata record fields are present
212     u32     device_time_source    where device time comes from
216     f64     device_clock_hz       device tick rate; 0 if unknown
224     u64     acquisition_id        current acquisition
232     u64     acquisition_start     global index of its first frame
256     u64     frames_written        frames published, ever; alone on its line
======  ======  ====================  ==============================================

The per-frame metadata record, and the meaning of ``metadata_fields`` and
``device_time_source``, are specified in ``metadata.py``. All fields are
little-endian.

Frame ``i`` of slot ``s`` starts at ``data_offset + s * slot_bytes +
frame_offset``. Bytes in a slot outside the frame (padding, embedded device
data) belong to the producer's hardware and are described, never rewritten.
"""
# IMPORTANT: this table is what consumers outside Python build against, not
# FIELDS below -- keep the two in sync by hand.

import struct

MAGIC = b"STCB"
VERSION = 1
MAX_NDIM = 6

# frames_written sits alone on a 128-byte line: the conservative cache-line
# size (64 on x86-64, 128 on some ARM), so reader polls never false-share.
COUNTER_OFFSET = 256
HEADER_BYTES = COUNTER_OFFSET + 128

# One metadata record per slot; its fields are defined with the metadata.
META_RECORD_BYTES = 40

FIELDS = {
    "magic": (0, "4s"),
    "version": (4, "<I"),
    "header_bytes": (8, "<I"),
    "meta_record_bytes": (12, "<I"),
    "capacity_bytes": (16, "<Q"),
    "max_slots": (24, "<Q"),
    "data_offset": (32, "<Q"),
    "meta_offset": (40, "<Q"),
    "layout_generation": (64, "<Q"),
    "n_slots": (72, "<Q"),
    "slot_bytes": (80, "<Q"),
    "frame_offset": (88, "<Q"),
    "dtype": (96, "8s"),
    "ndim": (104, "<I"),
    "in_flight": (108, "<I"),
    "shape": (112, f"<{MAX_NDIM}Q"),
    "strides": (160, f"<{MAX_NDIM}q"),
    "metadata_fields": (208, "<I"),
    "device_time_source": (212, "<I"),
    "device_clock_hz": (216, "<d"),
    "acquisition_id": (224, "<Q"),
    "acquisition_start": (232, "<Q"),
}


def read(buf, name):
    offset, fmt = FIELDS[name]
    values = struct.unpack_from(fmt, buf, offset)
    return values if len(values) > 1 else values[0]


def write(buf, name, value):
    offset, fmt = FIELDS[name]
    values = value if isinstance(value, tuple) else (value,)
    struct.pack_into(fmt, buf, offset, *values)
