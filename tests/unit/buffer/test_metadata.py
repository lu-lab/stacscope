"""B6 — per-frame metadata, written outside the slots.

The metadata table is the producer's own: it never touches what the hardware
wrote into a slot. ``sequence`` is always present (the producer knows which
frame it published); the rest is whatever the device can give, declared in
the header so a reader knows what to trust.
"""

import struct
from multiprocessing.shared_memory import SharedMemory

import pytest

from stacscope.core.buffer import ImageBuffer, Layout, MetaField, Metadata, Overrun, TimeSource

LAYOUT = Layout(shape=(2, 2), dtype="<u2", n_slots=4)
ALL = Metadata(
    fields=MetaField.HW_FRAME | MetaField.DEVICE_TIME | MetaField.HOST_TIME,
    device_time_source=TimeSource.CAPTURE_DEVICE,
    device_clock_hz=1e7,
)


@pytest.fixture
def rig(make_writer, name):
    readers = []

    def build(metadata=ALL, layout=LAYOUT):
        writer = make_writer()
        writer.configure(layout, metadata)
        writer.start_acquisition()
        reader = ImageBuffer.open(name)
        readers.append(reader)
        return writer, reader

    yield build
    for reader in readers:
        reader.close()


def camera(writer, hw_frames):
    """Frames as a camera reports them: its own frame numbers and clock."""
    for hw in hw_frames:
        index = writer.published()
        writer.stamp(index, hw_frame=hw, device_ticks=1000 * hw, host_time_ns=5_000_000 + index)
        writer.publish(1)


def test_each_frame_carries_its_metadata(rig):
    writer, reader = rig()
    cursor = reader.cursor()
    camera(writer, [10, 11])

    frames = [cursor.next(), cursor.next()]
    assert [int(f.meta["sequence"]) for f in frames] == [0, 1]
    assert [int(f.meta["hw_frame"]) for f in frames] == [10, 11]
    assert [int(f.meta["device_ticks"]) for f in frames] == [10_000, 11_000]
    assert [int(f.meta["host_time_ns"]) for f in frames] == [5_000_000, 5_000_001]


def test_a_camera_drop_and_a_lapped_reader_are_told_apart(rig):
    writer, reader = rig()
    cursor = reader.cursor()

    camera(writer, [0, 1, 3])  # the camera never delivered its frame 2
    frames = [cursor.next() for _ in range(3)]
    assert [int(f.meta["sequence"]) for f in frames] == [0, 1, 2], "the buffer lost nothing"
    assert [int(f.meta["hw_frame"]) for f in frames] == [0, 1, 3], "the camera did"

    camera(writer, [4, 5, 6, 7, 8, 9])  # now the reader falls a ring behind
    with pytest.raises(Overrun):
        cursor.next()
    hw = [int(cursor.next().meta["hw_frame"]) for _ in range(3)]
    assert hw == [7, 8, 9], "no camera gap: this loss was the reader's"


def test_the_header_declares_what_the_metadata_holds(rig):
    writer, reader = rig()
    assert reader.metadata == ALL


def test_no_per_frame_metadata_is_declared_as_such(rig):
    writer, reader = rig(metadata=Metadata())
    cursor = reader.cursor()
    writer.publish(1)

    assert reader.metadata.fields == MetaField(0)
    assert int(cursor.next().meta["sequence"]) == 0, "sequence needs nothing from the device"


def test_metadata_is_written_outside_the_slots(rig):
    writer, reader = rig()
    writer.slots[:] = 0xAB
    camera(writer, [1, 2, 3, 4])
    assert (writer.slots == 0xAB).all()


def test_a_group_carries_each_frames_metadata(rig):
    writer, reader = rig(layout=Layout(shape=(2, 2), dtype="<u2", n_slots=5))
    camera(writer, [20, 21, 22, 23, 24, 25])  # group [3-5] wraps the ring

    group = reader.cursor(reader.oldest()).latest_group(3)
    assert [int(m["hw_frame"]) for m in group.meta] == [23, 24, 25]


def test_metadata_sits_at_its_documented_offsets(rig, name):
    writer, reader = rig()
    camera(writer, [7])

    shm = SharedMemory(name, track=False)
    try:
        buf = shm.buf
        fields, source, hz = struct.unpack_from("<IId", buf, 208)
        assert (fields, source, hz) == (int(ALL.fields), int(TimeSource.CAPTURE_DEVICE), 1e7)

        meta_offset, record_bytes = struct.unpack_from("<Q", buf, 40)[0], struct.unpack_from("<I", buf, 12)[0]
        record = meta_offset + writer.slot_of(0) * record_bytes
        assert struct.unpack_from("<QQqqQ", buf, record) == (0, 7, 7000, 5_000_000, 0)
        del buf
    finally:
        shm.close()
