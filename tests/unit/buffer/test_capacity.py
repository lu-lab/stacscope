"""B2 — capacity is fixed at creation; layouts change inside it.

The buffer lives as long as its device, so a new ROI or slot count is a new
layout in the same segment. Readers the producer never heard of must notice
the change from the header alone, and never map a half-written layout.
"""

import struct
from multiprocessing.shared_memory import SharedMemory

import pytest

from stacscope.core.buffer import FrameBuffer, Layout, LayoutError

GENERATION = 64  # header offset of layout_generation


def generation(name):
    shm = SharedMemory(name, track=False)
    try:
        return struct.unpack_from("<Q", shm.buf, GENERATION)[0]
    finally:
        shm.close()


def test_a_layout_larger_than_capacity_is_rejected(make_writer):
    writer = make_writer(capacity_bytes=1000, max_slots=16)
    with pytest.raises(LayoutError):
        writer.configure(Layout(shape=(10, 10), dtype="<u2", n_slots=6))  # 1200 bytes


def test_more_slots_than_the_metadata_table_holds_are_rejected(make_writer):
    writer = make_writer(capacity_bytes=1 << 20, max_slots=4)
    with pytest.raises(LayoutError):
        writer.configure(Layout(shape=(2,), dtype="<u2", n_slots=5))


def test_a_layout_smaller_than_capacity_is_fine(make_writer, name):
    writer = make_writer(capacity_bytes=1 << 20, max_slots=16)
    writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=3))
    reader = FrameBuffer.open(name)
    try:
        assert reader.frames.shape == (3, 2, 2)
    finally:
        reader.close()


def test_a_rejected_layout_leaves_the_current_one_in_place(make_writer, name):
    writer = make_writer(capacity_bytes=1000, max_slots=16)
    writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=3))
    before = generation(name)
    with pytest.raises(LayoutError):
        writer.configure(Layout(shape=(10, 10), dtype="<u2", n_slots=6))

    assert generation(name) == before
    reader = FrameBuffer.open(name)
    try:
        assert reader.frames.shape == (3, 2, 2)
    finally:
        reader.close()


def test_each_layout_change_moves_the_generation_to_a_new_stable_value(make_writer, name):
    writer = make_writer()
    seen = [generation(name)]
    for n_slots in (2, 3, 4):
        writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=n_slots))
        seen.append(generation(name))

    assert len(set(seen)) == len(seen)
    assert all(g % 2 == 0 for g in seen), "an odd generation means a change in progress"


def test_every_layout_field_is_written_while_the_generation_is_odd(make_writer, name, monkeypatch):
    from stacscope.core.buffer import header

    writer = make_writer()
    shm = SharedMemory(name, track=False)
    seen = {}
    real_write = header.write

    def spying_write(buf, field, value):
        if field != "layout_generation":
            seen[field] = struct.unpack_from("<Q", shm.buf, GENERATION)[0]
        real_write(buf, field, value)

    monkeypatch.setattr(header, "write", spying_write)
    try:
        writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=3))
    finally:
        shm.close()

    assert {"n_slots", "shape", "strides", "dtype"} <= seen.keys()
    assert all(g % 2 == 1 for g in seen.values()), seen


def test_a_reader_remaps_when_the_layout_changes(make_writer, name):
    writer = make_writer()
    writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=3))
    reader = FrameBuffer.open(name)
    try:
        assert reader.sync() is False, "nothing changed yet"

        writer.configure(Layout(shape=(4, 8), dtype="<u2", n_slots=5))
        assert reader.frames.shape == (3, 2, 2), "a reader remaps only when it asks"
        assert reader.sync() is True
        assert reader.frames.shape == (5, 4, 8)

        writer.frames[4] = 9
        assert (reader.frames[4] == 9).all()
    finally:
        reader.close()


def test_a_reader_may_open_before_the_producer_configures(make_writer, name):
    writer = make_writer()
    reader = FrameBuffer.open(name)
    try:
        assert reader.frames is None
        assert reader.sync() is False

        writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=3))
        assert reader.sync() is True
        assert reader.frames.shape == (3, 2, 2)
    finally:
        reader.close()


def test_a_reader_never_maps_a_layout_that_is_mid_change(make_writer, name):
    writer = make_writer()
    writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=3))
    reader = FrameBuffer.open(name)
    shm = SharedMemory(name, track=False)
    try:
        # Freeze the producer halfway through a change: odd generation, new
        # shape already written, slot count not yet.
        g = struct.unpack_from("<Q", shm.buf, GENERATION)[0]
        struct.pack_into("<Q", shm.buf, GENERATION, g + 1)
        struct.pack_into("<Q", shm.buf, 112, 4)  # shape[0]

        assert reader.sync() is False
        assert reader.frames.shape == (3, 2, 2)
        with pytest.raises(LayoutError):
            FrameBuffer.open(name, timeout=0.05)
    finally:
        shm.close()
        reader.close()
