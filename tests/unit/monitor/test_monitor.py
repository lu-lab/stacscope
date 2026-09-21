"""V1-V12 -- the buffer monitor's model, which is all of it that can be tested.

The Qt window is a thin wiring of what is proven here: it polls, it draws, and
it makes no decisions. Everything that decides lives in ``BufferMonitor`` and
imports no Qt, so this runs in the default suite on both interpreters.
"""

import numpy as np
import pytest
from monitor_support import LAYOUT, SHAPE, SLOTS, Rig, fresh_name

from stacscope.core.buffer import Layout
from stacscope.monitor import BufferMonitor, State


@pytest.fixture
def rig():
    rig = Rig()
    yield rig
    rig.close()


@pytest.fixture
def monitor(rig):
    monitor = BufferMonitor(rig.name)
    yield monitor
    monitor.close()


def test_it_waits_for_a_producer_that_does_not_exist_yet(monitor):
    """V4. A monitor is started whenever the user feels like it, which is
    usually before the thing being monitored. Not finding a buffer is the
    normal first state, not an error."""
    lonely = BufferMonitor(fresh_name())
    try:
        assert lonely.poll() is None
        assert lonely.state is State.WAITING
    finally:
        lonely.close()


def test_it_attaches_once_the_buffer_appears(monitor, rig):
    """V4, second half."""
    assert monitor.state is State.WAITING
    rig.emit(1)

    assert monitor.poll() is not None
    assert monitor.state is State.LIVE


def test_following_watches_whichever_slot_just_took_a_frame(monitor, rig):
    """V1. Live follow moves the watched slot to wherever the newest frame
    landed, so the picture keeps up without the user chasing it."""
    rig.emit(3)
    first = monitor.poll()
    assert first.index == 2
    assert first.slot == 2 % SLOTS

    rig.emit(2)
    latest = monitor.poll()
    assert latest.index == 4
    assert latest.slot == 4 % SLOTS


def test_what_it_hands_over_is_a_copy(monitor, rig):
    """V2. A view into the ring keeps the segment mapped and changes under the
    viewer; both are unacceptable in a GUI that holds an image across frames.
    The model copies once, here, so the window cannot get it wrong."""
    rig.emit(1)
    held = monitor.poll().image
    before = held.copy()

    rig.emit(SLOTS * 2)  # the ring laps several times over

    np.testing.assert_array_equal(held, before)


def test_seeking_parks_on_a_slot_and_stops_following(monitor, rig):
    """V9. The slider picks a slot in the ring and the view stays on it."""
    rig.emit(3)
    monitor.poll()

    monitor.seek(1)
    assert monitor.follow is False
    parked = monitor.poll()
    assert parked.slot == 1
    assert parked.index == 1  # the frame that happens to be in slot 1

    rig.emit(1)  # frame 3 lands in slot 3, not the one being watched
    assert monitor.poll() is None


def test_the_slider_spans_the_ring_and_does_not_move(monitor, rig):
    """V9, second half. The slider covers the buffer, so its range is the slot
    count -- fixed. A range that shifted every poll would move the handle's
    meaning under the user's hand."""
    assert monitor.slots() is None  # not attached yet

    rig.emit(1)
    monitor.poll()
    assert monitor.slots() == SLOTS

    rig.emit(SLOTS * 3)
    monitor.poll()
    assert monitor.slots() == SLOTS  # unchanged, however much was produced


def test_a_parked_slot_updates_when_the_producer_writes_over_it(monitor, rig):
    """V12. This is the whole point of watching a slot: you see the ring come
    round and land on it again."""
    rig.emit(2)
    monitor.seek(1)
    assert monitor.poll().index == 1

    rig.emit(SLOTS)  # one full lap: slot 1 takes frame 1 + SLOTS

    update = monitor.poll()
    assert update is not None
    assert update.slot == 1
    assert update.index == 1 + SLOTS


def test_a_slot_nothing_has_landed_in_yet_says_so(monitor, rig):
    """V12. Early in a run most of the ring is untouched. The monitor shows
    the slot's raw contents regardless -- it is a buffer monitor -- and reports
    that no published frame has reached it."""
    rig.emit(1)
    monitor.seek(SLOTS - 1)

    update = monitor.poll()
    assert update.index == -1
    assert update.image.shape == SHAPE


def test_a_slot_the_producer_is_about_to_write_is_flagged(monitor, rig):
    """V12. The slots just ahead of the counter are where the producer is
    working, by ``in_flight``. Reading one can tear, which is accepted here --
    but the user is told, because an image that flickers for no visible reason
    is exactly what this tool exists to explain."""
    rig.emit(SLOTS + 2)  # published past a lap, so the oldest slots are in play

    live = monitor.poll()
    assert live.intact is True  # where the newest frame just landed

    monitor.seek((live.slot + 1) % SLOTS)  # next in line to be overwritten
    assert monitor.poll().intact is False


def test_pausing_stops_updates_and_refresh_forces_one(monitor, rig):
    """V11. Pause is for reading the metadata panel without it moving; refresh
    is the same trigger as nudging the slider, without having to."""
    rig.emit(1)
    monitor.poll()

    monitor.paused = True
    rig.emit(3)
    assert monitor.poll() is None

    forced = monitor.refresh()
    assert forced is not None and forced.index == 3
    assert monitor.paused is True  # refreshing does not resume


def test_the_header_is_reported_field_for_field(monitor, rig):
    """V10. The point of a buffer monitor: what the header actually says,
    not what the producer meant."""
    rig.emit(1)
    monitor.poll()
    header = monitor.header()

    assert header["n_slots"] == SLOTS
    assert header["shape"][: len(SHAPE)] == SHAPE
    assert header["dtype"] == "<u2"
    assert header["in_flight"] >= 1  # every producer writes ahead by at least one
    assert header["acquisition_id"] >= 1


def test_per_frame_metadata_comes_with_the_frame(monitor, rig):
    """V10, second half. The producer fills hardware frame numbers, a device
    clock and host time; a monitor that cannot show them cannot answer the
    question people will bring to it."""
    rig.emit(2)
    update = monitor.poll()

    assert set(update.meta) >= {"hw_frame", "device_ticks", "host_time_ns"}
    assert update.meta["host_time_ns"] > 0


def test_nothing_is_drawn_before_the_first_frame(monitor):
    """V4. The segment exists and has a layout, but nothing has been published.
    Attached and empty is a different thing from not there at all."""
    assert monitor.poll() is None
    assert monitor.state is State.LIVE


def test_refresh_looks_again_even_when_nothing_changed(monitor, rig):
    """V11. Its whole purpose: the same trigger as nudging the slider, for a
    user who wants the slot re-read without moving anything."""
    rig.emit(1)
    monitor.poll()
    assert monitor.poll() is None  # nothing new

    assert monitor.refresh() is not None


def test_the_slider_cannot_point_off_the_ring(monitor, rig):
    """V9. A slot number out of range is clamped, not an error: the caller is
    a slider, and a slider whose range just shrank should not crash the view."""
    rig.emit(1)

    monitor.seek(SLOTS + 10)
    assert monitor.poll().slot == SLOTS - 1

    monitor.seek(-3)
    assert monitor.poll().slot == 0


def test_a_new_acquisition_is_noticed(monitor, rig):
    """V5. Frames from two runs must never be mixed in one view.

    Each acquisition restarts at slot 0, so a frame's slot is counted from
    ``acquisition_start``. Slots still holding the previous run's bytes report
    no frame rather than an index worked out under the wrong origin."""
    rig.emit(2)
    first = monitor.poll()
    assert first.index == 1

    rig.reconfigure(LAYOUT)
    rig.emit(1)
    second = monitor.poll()

    assert second.acquisition_id != first.acquisition_id
    assert second.slot == 0  # the new run begins at slot 0
    assert second.index == 2  # counting up across acquisitions, as the header does

    monitor.seek(SLOTS - 1)
    assert monitor.poll().index == -1  # the old run's bytes, claimed by nobody


def test_a_layout_change_is_followed(monitor, rig):
    """V6. A device that reconfigures its camera changes the frame shape under
    a monitor that is already attached."""
    rig.emit(1)
    assert monitor.poll().image.shape == SHAPE

    taller = Layout(shape=(SHAPE[0] * 2, SHAPE[1]), dtype="<u2", n_slots=SLOTS)
    rig.reconfigure(taller)
    rig.emit(1)

    assert monitor.poll().image.shape == (SHAPE[0] * 2, SHAPE[1])
    assert monitor.header()["shape"][:2] == (SHAPE[0] * 2, SHAPE[1])


def test_closing_releases_the_segment(monitor, rig):
    """V8. A reader still holding a view cannot close on Windows, and the
    producer cannot either -- 'cannot close exported pointers'. The monitor
    holds no views, so both let go cleanly."""
    rig.emit(2)
    monitor.poll()

    monitor.close()
    rig.close()  # would raise BufferError if the monitor still held a view
