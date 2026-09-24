"""B3 — the frame counter only counts up; acquisitions restart the ring.

The buffer lives as long as its device, so resetting the counter would make
a restart indistinguishable from a reader that fell behind. Instead the
header marks where each acquisition began, and each acquisition writes from
slot 0 so config can align stacks to slots.
"""

import struct
from multiprocessing.shared_memory import SharedMemory

import pytest

from stacscope.core.buffer import ImageBuffer, Layout

FRAMES_WRITTEN = 256  # header offset, alone on its own 128-byte line
LAYOUT = Layout(shape=(2, 2), dtype="<u2", n_slots=3)


@pytest.fixture
def writer(make_writer):
    writer = make_writer()
    writer.configure(LAYOUT)
    return writer


def test_the_counter_keeps_counting_across_acquisitions(writer):
    writer.start_acquisition()
    writer.publish(5)
    writer.start_acquisition()
    assert writer.published() == 5, "a new acquisition must not reset the counter"
    writer.publish(3)
    assert writer.published() == 8


def test_a_new_acquisition_records_its_id_and_first_frame(writer):
    assert writer.start_acquisition() == 1
    assert (writer.acquisition_id, writer.acquisition_start) == (1, 0)
    writer.publish(5)

    assert writer.start_acquisition() == 2
    assert (writer.acquisition_id, writer.acquisition_start) == (2, 5)


def test_each_acquisition_writes_from_slot_zero(writer):
    writer.start_acquisition()
    writer.publish(5)
    assert writer.slot_of(4) == 1

    writer.start_acquisition()
    assert [writer.slot_of(i) for i in range(5, 11)] == [0, 1, 2, 0, 1, 2]


def test_a_reader_sees_a_new_acquisition(writer, name):
    writer.start_acquisition()
    writer.publish(4)
    reader = ImageBuffer.open(name)
    try:
        assert (reader.acquisition_id, reader.acquisition_start) == (1, 0)
        assert reader.published() == 4

        writer.start_acquisition()
        writer.publish(2)
        assert reader.published() == 6, "the counter needs no sync"
        assert reader.sync() is True
        assert (reader.acquisition_id, reader.acquisition_start) == (2, 4)
        assert reader.slot_of(4) == 0
    finally:
        reader.close()


def test_publishing_outside_an_acquisition_is_an_error(writer):
    with pytest.raises(RuntimeError):
        writer.publish(1)


def test_a_layout_change_ends_the_acquisition(writer):
    writer.start_acquisition()
    writer.publish(2)
    writer.configure(Layout(shape=(4, 4), dtype="<u2", n_slots=2))
    with pytest.raises(RuntimeError):
        writer.publish(1)

    writer.start_acquisition()
    writer.publish(1)
    assert writer.published() == 3


def test_the_counter_lives_at_its_documented_offset(writer, name):
    writer.start_acquisition()
    writer.publish(7)
    shm = SharedMemory(name, track=False)
    try:
        assert struct.unpack_from("<Q", shm.buf, FRAMES_WRITTEN)[0] == 7
    finally:
        shm.close()
