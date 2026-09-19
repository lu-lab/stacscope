"""B10 — a synthetic camera, and waiting for frames.

The pattern producer touches only a small bar of pixels per frame (erasing
the bar that slot held a ring ago), so it runs far faster than any real
camera, while a human watching a viewer still sees motion. Automated checks
use the metadata, which it fills like a camera would.
"""

import asyncio
import threading
import time

import numpy as np
import pytest

from stacscope.adapters.simulated import PatternProducer
from stacscope.core.buffer import FrameBuffer, Layout, MetaField, TimeSource

MiB = 1 << 20


@pytest.fixture
def rig(make_writer, name):
    readers = []

    def build(shape=(8, 32), n_slots=4, bar_width=2, step=3):
        writer = make_writer(capacity_bytes=64 * MiB)
        producer = PatternProducer(writer, bar_width=bar_width, step=step)
        producer.configure(Layout(shape=shape, dtype="<u2", n_slots=n_slots))
        producer.start_acquisition()
        reader = FrameBuffer.open(name)
        readers.append(reader)
        return producer, reader

    yield build
    for reader in readers:
        reader.close()


def bar_columns(frame):
    return [int(c) for c in np.flatnonzero(frame.any(axis=tuple(range(frame.ndim - 1))))]


def test_each_frame_shows_only_its_own_bar(rig):
    producer, reader = rig(shape=(8, 32), n_slots=4, bar_width=2, step=3)
    producer.emit(11)  # every slot rewritten at least twice

    cursor = reader.cursor(reader.oldest())
    while (frame := cursor.next()) is not None:
        start = (frame.index * 3) % 32
        assert bar_columns(frame.data) == sorted({start % 32, (start + 1) % 32})


def test_metadata_is_filled_like_a_camera(rig):
    producer, reader = rig()
    cursor = reader.cursor()
    producer.emit(3)

    assert reader.metadata.fields == MetaField.HW_FRAME | MetaField.DEVICE_TIME | MetaField.HOST_TIME
    assert reader.metadata.device_time_source is TimeSource.CAMERA
    frames = [cursor.next() for _ in range(3)]
    assert [int(f.meta["hw_frame"]) for f in frames] == [0, 1, 2]
    ticks = [int(f.meta["device_ticks"]) for f in frames]
    assert ticks[1] - ticks[0] == ticks[2] - ticks[1] > 0
    assert all(int(f.meta["host_time_ns"]) > 0 for f in frames)


def test_a_camera_drop_can_be_simulated(rig):
    producer, reader = rig()
    cursor = reader.cursor()
    producer.emit(2)
    producer.drop(3)  # the camera loses three frames: the buffer never sees them
    producer.emit(1)

    frames = [cursor.next() for _ in range(3)]
    assert [int(f.meta["sequence"]) for f in frames] == [0, 1, 2]
    assert [int(f.meta["hw_frame"]) for f in frames] == [0, 1, 5]


def test_a_frame_touches_only_the_bar_columns(rig, name):
    # What keeps it fast, checked exactly rather than timed: every pixel
    # outside the old and new bar columns is left as it was.
    producer, reader = rig(shape=(8, 32), n_slots=4, bar_width=2, step=3)
    producer.emit(4)  # slot 0 last held frame 0, bar at columns 0-1
    writer = producer._writer
    slot = writer.slot_of(writer.published())
    writer.frames[slot][:, 2:] = 7  # sentinel everywhere but the old bar
    producer.emit()  # frame 4: bar at columns 12-13, erases 0-1

    untouched = np.delete(writer.frames[slot], [0, 1, 12, 13], axis=1)
    assert (untouched == 7).all()


def test_it_runs_well_above_the_fastest_real_camera_rate(rig):
    # A full Flash frame; the bar keeps the per-frame cost tiny. 100 fps is
    # the Flash's full-frame rate over Camera Link; demand 10x with margin.
    producer, reader = rig(shape=(2048, 2048), n_slots=4, bar_width=4)
    frames = 500
    start = time.perf_counter()
    producer.emit(frames)
    rate = frames / (time.perf_counter() - start)
    assert rate > 1000, f"{rate:.0f} frames/s"


# --- waiting ------------------------------------------------------------------------


def test_wait_returns_at_once_when_a_frame_is_ready(rig):
    producer, reader = rig()
    cursor = reader.cursor()
    producer.emit(1)
    assert cursor.wait(timeout=0) is True


def test_wait_times_out_when_nothing_arrives(rig):
    producer, reader = rig()
    start = time.monotonic()
    assert reader.cursor().wait(timeout=0.05) is False
    assert time.monotonic() - start >= 0.05


def test_wait_wakes_when_the_producer_publishes(rig):
    producer, reader = rig()
    cursor = reader.cursor()
    timer = threading.Timer(0.05, producer.emit)
    timer.start()
    try:
        assert cursor.wait(timeout=5) is True
        assert cursor.next().index == 0
    finally:
        timer.cancel()


async def test_wait_async_lets_the_event_loop_run_while_polling(rig):
    producer, reader = rig()
    cursor = reader.cursor()

    async def publish_later():
        await asyncio.sleep(0.05)
        producer.emit()

    publisher = asyncio.create_task(publish_later())
    assert await cursor.wait_async(timeout=5) is True
    await publisher
    assert await reader.cursor().wait_async(timeout=0.02) is False
