"""B1 — the producer defines the frame layout; the header describes it fully.

The header is the contract for consumers outside Python, so one test reads a
frame using nothing but the documented byte offsets below. If this table and
the implementation disagree, the implementation is wrong.
"""

import struct
from multiprocessing.shared_memory import SharedMemory

import numpy as np
import pytest

from stacscope.core.buffer import FrameBuffer, Layout, LayoutError

# The documented header (little-endian). Offsets are bytes from the start.
MAGIC = b"STCB"
HEADER_SPEC = {
    "magic": (0, "4s"),
    "version": (4, "<I"),
    "header_bytes": (8, "<I"),
    "data_offset": (32, "<Q"),
    "n_slots": (72, "<Q"),
    "slot_bytes": (80, "<Q"),
    "frame_offset": (88, "<Q"),
    "dtype": (96, "8s"),
    "ndim": (104, "<I"),
    "shape": (112, "<6Q"),
    "strides": (160, "<6q"),
}


def field(buf, name):
    offset, fmt = HEADER_SPEC[name]
    values = struct.unpack_from(fmt, buf, offset)
    return values if len(values) > 1 else values[0]


def ramp(shape, dtype, start):
    return (np.arange(np.prod(shape)) + start).astype(dtype).reshape(shape)


def test_consumer_sees_exactly_the_frames_the_producer_wrote(make_writer, name):
    writer = make_writer()
    writer.configure(Layout(shape=(4, 6), dtype="<u2", n_slots=3))
    for slot in range(3):
        writer.frames[slot] = ramp((4, 6), "<u2", start=100 * slot)

    reader = FrameBuffer.open(name)
    try:
        assert reader.frames.shape == (3, 4, 6)
        assert reader.frames.dtype == np.dtype("<u2")
        for slot in range(3):
            np.testing.assert_array_equal(reader.frames[slot], ramp((4, 6), "<u2", 100 * slot))
    finally:
        reader.close()


def test_consumer_frames_are_views_not_copies(make_writer, name):
    writer = make_writer()
    writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=2))
    reader = FrameBuffer.open(name)
    try:
        view = reader.frames[1]
        writer.frames[1] = 7
        assert (view == 7).all(), "the reader's frame must track the shared memory"
        del view
    finally:
        reader.close()


def test_consumer_frames_are_read_only(make_writer, name):
    writer = make_writer()
    writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=2))
    reader = FrameBuffer.open(name)
    try:
        with pytest.raises(ValueError):
            reader.frames[0] = 1
    finally:
        reader.close()


def test_padding_and_embedded_bytes_are_described_not_exposed(make_writer, name):
    # A DMA-style slot: 16 leading bytes the hardware owns (say, an embedded
    # timestamp), then 4 rows of 5 pixels, each row padded to 16 bytes.
    layout = Layout(shape=(4, 5), dtype="<u2", n_slots=2, strides=(16, 2), frame_offset=16, slot_bytes=96)
    writer = make_writer()
    writer.configure(layout)

    raw = writer.slots[1]  # the whole slot as bytes, as the hardware sees it
    raw[:] = 0xAB  # fill everything, padding included
    writer.frames[1] = ramp((4, 5), "<u2", start=1)
    before = bytes(raw)

    reader = FrameBuffer.open(name)
    try:
        np.testing.assert_array_equal(reader.frames[1], ramp((4, 5), "<u2", 1))
        assert reader.frames.strides == (96, 16, 2)
    finally:
        reader.close()

    assert bytes(writer.slots[1]) == before, "reading must not touch the slot"
    assert bytes(writer.slots[1][:16]) == b"\xab" * 16, "embedded bytes survive"
    assert bytes(writer.slots[1][16 + 10 : 16 + 16]) == b"\xab" * 6, "row padding survives"


def test_a_reader_using_only_the_documented_header_reads_the_same_frame(make_writer, name):
    layout = Layout(shape=(3, 5), dtype="<u2", n_slots=4, strides=(12, 2), frame_offset=8, slot_bytes=48)
    writer = make_writer()
    writer.configure(layout)
    writer.frames[2] = ramp((3, 5), "<u2", start=500)

    shm = SharedMemory(name, track=False)
    try:
        buf = shm.buf
        assert field(buf, "magic") == MAGIC
        assert field(buf, "version") == 1
        assert field(buf, "header_bytes") > 0

        ndim = field(buf, "ndim")
        shape = field(buf, "shape")[:ndim]
        strides = field(buf, "strides")[:ndim]
        dtype = np.dtype(field(buf, "dtype").rstrip(b"\0").decode())
        slot_start = field(buf, "data_offset") + 2 * field(buf, "slot_bytes")

        frame = np.ndarray(
            shape, dtype, buffer=buf, offset=slot_start + field(buf, "frame_offset"), strides=strides
        ).copy()
        assert field(buf, "n_slots") == 4
        np.testing.assert_array_equal(frame, ramp((3, 5), "<u2", 500))
        del buf
    finally:
        shm.close()


def test_data_region_starts_on_a_page_boundary(make_writer, name):
    import mmap

    writer = make_writer()
    writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=2))
    shm = SharedMemory(name, track=False)
    try:
        assert field(shm.buf, "data_offset") % mmap.PAGESIZE == 0
    finally:
        shm.close()


@pytest.mark.parametrize(
    "layout",
    [
        Layout(shape=(4, 5), dtype="<u2", n_slots=2, strides=(8, 2)),  # rows overlap
        Layout(shape=(4, 5), dtype="<u2", n_slots=2, slot_bytes=39),  # frame spills out of its slot
        Layout(shape=(4, 5), dtype="<u2", n_slots=2, frame_offset=8, slot_bytes=40),  # offset pushes it out
        Layout(shape=(1,) * 7, dtype="<u2", n_slots=2),  # more dimensions than the header holds
        Layout(shape=(4, 5), dtype="<u2", n_slots=0),
        Layout(shape=(4, 5), dtype="<u2", n_slots=2, strides=(10,)),  # strides don't match shape
    ],
)
def test_layouts_that_cannot_describe_a_frame_are_rejected(make_writer, layout):
    writer = make_writer()
    with pytest.raises(LayoutError):
        writer.configure(layout)


@pytest.mark.parametrize(
    ("magic", "version"),
    [(b"NOPE", 1), (MAGIC, 2)],
    ids=["foreign segment", "unknown version"],
)
def test_opening_a_segment_this_reader_cannot_trust_fails(name, magic, version):
    shm = SharedMemory(name, create=True, size=4096, track=False)
    try:
        struct.pack_into("<4sI", shm.buf, 0, magic, version)
        with pytest.raises(LayoutError):
            FrameBuffer.open(name)
    finally:
        shm.close()
        shm.unlink()
