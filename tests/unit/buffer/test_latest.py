"""B5 — the most recent frame, or the most recent complete group of frames.

Most consumers want only recent data, and "recent" may mean a whole stack the
buffer knows nothing about. The consumer supplies the group size (learned on
the control flow); the buffer only does the arithmetic, counted from the
acquisition's first frame so a reader joining late still lines up.
"""

import numpy as np
import pytest

from buffer_support import produce
from stacscope.core.buffer import ImageBuffer, Layout


@pytest.fixture
def rig(make_writer, name):
    readers = []

    def build(n_slots=6, in_flight=1):
        writer = make_writer()
        writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=n_slots, in_flight=in_flight))
        writer.start_acquisition()
        reader = ImageBuffer.open(name)
        readers.append(reader)
        return writer, reader

    yield build
    for reader in readers:
        reader.close()


def values(frames):
    return [int(f[0, 0]) for f in frames]


# --- latest ------------------------------------------------------------------------


def test_latest_is_the_newest_frame_and_skipping_is_not_an_error(rig):
    writer, reader = rig()
    cursor = reader.cursor()
    assert cursor.latest() is None

    produce(writer, 3)
    frame = cursor.latest()
    assert frame.index == 2 and (frame.data == 2).all()

    assert cursor.latest() is None, "nothing newer than what was already seen"
    produce(writer, 20)  # laps the ring several times
    assert cursor.latest().index == 22


def test_next_carries_on_after_the_latest_frame(rig):
    writer, reader = rig()
    cursor = reader.cursor()
    produce(writer, 3)
    cursor.latest()
    produce(writer, 1)
    assert cursor.next().index == 3


# --- latest_group ------------------------------------------------------------------


def test_latest_group_is_the_newest_complete_group(rig):
    writer, reader = rig(n_slots=6)
    cursor = reader.cursor(reader.oldest())
    produce(writer, 7)  # groups of 3: [0-2] [3-5] complete, [6-] not yet

    group = cursor.latest_group(3)
    assert group.first == 3
    assert isinstance(group.data, np.ndarray) and group.data.shape == (3, 2, 2)
    assert values(group.data) == [3, 4, 5]


def test_groups_are_counted_from_the_acquisitions_first_frame(rig):
    writer, reader = rig(n_slots=6)
    produce(writer, 7)
    writer.start_acquisition()  # starts at frame 7
    produce(writer, 4)

    group = reader.cursor(reader.oldest()).latest_group(3)
    assert group.first == 7 and group.acquisition_id == 2
    assert values(group.data) == [7, 8, 9]


def test_a_group_that_divides_the_ring_is_one_contiguous_view(rig):
    writer, reader = rig(n_slots=6)
    produce(writer, 6)
    group = reader.cursor(reader.oldest()).latest_group(3)

    writer.frames[writer.slot_of(4)] = 99
    assert values(group.data) == [3, 99, 5], "a view, not a copy"


def test_a_group_that_wraps_the_ring_is_a_sequence_of_views(rig):
    writer, reader = rig(n_slots=5)
    produce(writer, 6)  # group [3-5] sits in slots 3, 4, 0

    group = reader.cursor(reader.oldest()).latest_group(3)
    assert not isinstance(group.data, np.ndarray)
    assert values(group.data) == [3, 4, 5]


def test_each_group_is_returned_once(rig):
    writer, reader = rig(n_slots=6)
    cursor = reader.cursor(reader.oldest())
    produce(writer, 3)
    assert cursor.latest_group(3).first == 0
    assert cursor.latest_group(3) is None

    produce(writer, 3)
    assert cursor.latest_group(3).first == 3


def test_a_group_already_being_overwritten_is_not_returned(rig):
    # Groups of 3 in 4 slots with one in flight: once frame 8 is published,
    # the newest complete group [3-5] has lost frames 3 and 4.
    writer, reader = rig(n_slots=4, in_flight=1)
    cursor = reader.cursor(reader.oldest())
    produce(writer, 8)

    assert cursor.latest_group(3) is None
    produce(writer, 1)  # [6-8] completes
    assert cursor.latest_group(3).first == 6


def test_a_group_the_ring_can_never_hold_is_refused(rig):
    writer, reader = rig(n_slots=4, in_flight=1)
    with pytest.raises(ValueError):
        reader.cursor().latest_group(4)
