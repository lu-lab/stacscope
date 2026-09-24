"""B4 — reading every frame, in order, and knowing when you could not.

No backpressure: a reader that falls a ring behind loses frames. It must be
told how many, never handed an overwritten frame as if it were fine. Every
producer writes at least the next frame before publishing it (a DMA device
may write more), so ``in_flight`` slots are unsafe before the counter says so.
"""

import pytest

from buffer_support import produce
from stacscope.core.buffer import ImageBuffer, Layout, Overrun


def layout(n_slots=4, in_flight=1, shape=(2, 2)):
    return Layout(shape=shape, dtype="<u2", n_slots=n_slots, in_flight=in_flight)


@pytest.fixture
def rig(make_writer, name):
    readers = []

    def build(**kwargs):
        writer = make_writer()
        writer.configure(layout(**kwargs))
        writer.start_acquisition()
        reader = ImageBuffer.open(name)
        readers.append(reader)
        return writer, reader

    yield build
    for reader in readers:
        reader.close()


def drain(cursor):
    frames = []
    while (frame := cursor.next()) is not None:
        frames.append(frame)
    return frames


def test_next_returns_each_new_frame_in_order_then_none(rig):
    writer, reader = rig()
    cursor = reader.cursor()
    assert cursor.next() is None

    produce(writer, 3)
    frames = drain(cursor)
    assert [f.index for f in frames] == [0, 1, 2]
    assert all((f.data == f.index).all() for f in frames)
    assert cursor.next() is None


def test_a_cursor_starts_now_unless_told_otherwise(rig):
    writer, reader = rig()
    produce(writer, 2)

    assert drain(reader.cursor()) == []
    assert [f.index for f in drain(reader.cursor(reader.oldest()))] == [0, 1]


def test_a_lapped_cursor_is_told_how_many_frames_it_lost(rig):
    writer, reader = rig(n_slots=4)
    cursor = reader.cursor()
    produce(writer, 6)  # 0 and 1 overwritten by 4 and 5; 2's slot takes 6 next

    with pytest.raises(Overrun) as lost:
        cursor.next()
    assert lost.value.skipped == 3
    assert [f.index for f in drain(cursor)] == [3, 4, 5]


def test_frames_in_flight_are_already_unsafe(rig):
    # A DMA device two frames ahead is writing frames 4 and 5 into slots 0
    # and 1 before the counter says so.
    writer, reader = rig(n_slots=4, in_flight=2)
    cursor = reader.cursor()
    produce(writer, 4)

    with pytest.raises(Overrun) as lost:
        cursor.next()
    assert lost.value.skipped == 2
    assert [f.index for f in drain(cursor)] == [2, 3]


def test_still_valid_is_exact_at_both_ends(rig):
    writer, reader = rig(n_slots=4, in_flight=1)
    produce(writer, 6)  # frame 6 is in flight into frame 2's slot

    assert [reader.still_valid(i) for i in range(2, 7)] == [False, True, True, True, False]
    assert reader.oldest() == 3


def test_a_view_from_next_can_be_checked_after_use(rig):
    writer, reader = rig(n_slots=4)
    cursor = reader.cursor()
    produce(writer, 1)
    frame = cursor.next()
    assert reader.still_valid(frame.index)

    produce(writer, 4)  # frame 4 lands in frame 0's slot
    assert not reader.still_valid(frame.index)


def test_a_new_acquisition_after_everything_was_read_loses_nothing(rig):
    writer, reader = rig(n_slots=4)
    cursor = reader.cursor()
    produce(writer, 3)
    drain(cursor)

    writer.start_acquisition()
    produce(writer, 2)
    frames = drain(cursor)
    assert [(f.index, f.acquisition_id) for f in frames] == [(3, 2), (4, 2)]
    assert (frames[0].data == 3).all(), "frame 3 is the new acquisition's slot 0"


def test_unread_frames_of_a_finished_acquisition_count_as_lost(rig):
    writer, reader = rig(n_slots=4)
    cursor = reader.cursor()
    produce(writer, 3)
    cursor.next()  # frames 1 and 2 remain unread

    writer.start_acquisition()
    produce(writer, 1)
    with pytest.raises(Overrun) as lost:
        cursor.next()
    assert lost.value.skipped == 2
    assert [f.index for f in drain(cursor)] == [3]


def test_a_cursor_follows_a_layout_change(rig):
    writer, reader = rig(n_slots=4, shape=(2, 2))
    cursor = reader.cursor()
    produce(writer, 1)
    drain(cursor)

    writer.configure(layout(n_slots=3, shape=(3, 5)))
    writer.start_acquisition()
    produce(writer, 1)
    [frame] = drain(cursor)
    assert frame.data.shape == (3, 5)
    assert (frame.data == 1).all()


@pytest.mark.parametrize("n_slots, in_flight", [(2, 2), (4, 0)])
def test_in_flight_is_at_least_one_and_leaves_a_readable_slot(make_writer, n_slots, in_flight):
    from stacscope.core.buffer import LayoutError

    writer = make_writer()
    with pytest.raises(LayoutError):
        writer.configure(layout(n_slots=n_slots, in_flight=in_flight))
