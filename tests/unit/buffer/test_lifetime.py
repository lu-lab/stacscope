"""B7 — who creates, keeps and removes the segment, on every OS.

Windows removes a mapping when its last handle closes; POSIX keeps it in
/dev/shm until someone unlinks it, and its resource_tracker unlinks segments
a process merely opened when that process exits. Ownership is therefore
explicit: the producer creates, and removes on close; readers never remove;
a stale segment from a crashed producer is cleared on the next create.
These tests mean most on the Linux CI leg.
"""

import subprocess
import sys
import textwrap
from multiprocessing.shared_memory import SharedMemory

import pytest

from stacscope.core.buffer import FrameBuffer, Layout

LAYOUT = Layout(shape=(2, 2), dtype="<u2", n_slots=3)


def run_python(code):
    result = subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], capture_output=True, text=True, timeout=8
    )
    assert result.returncode == 0, result.stderr


def exists(name):
    try:
        SharedMemory(name, track=False).close()
    except FileNotFoundError:
        return False
    return True


def test_closing_the_producer_removes_the_segment(name):
    writer = FrameBuffer.create(name, capacity_bytes=4096, max_slots=4)
    writer.close()
    assert not exists(name)


def test_a_segment_left_by_a_crashed_producer_is_cleared_on_create(name):
    run_python(f"""
        import os
        from stacscope.core.buffer import FrameBuffer, Layout
        writer = FrameBuffer.create({name!r}, capacity_bytes=4096, max_slots=4)
        writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=3))
        os._exit(0)  # no close: a crash
    """)
    writer = FrameBuffer.create(name, capacity_bytes=1 << 16, max_slots=8)
    try:
        reader = FrameBuffer.open(name)
        assert reader.frames is None, "a fresh buffer, not the crashed one's layout"
        reader.close()
    finally:
        writer.close()


def test_a_reader_process_exiting_leaves_the_segment_alone(make_writer, name):
    writer = make_writer()
    writer.configure(LAYOUT)
    for close in ("reader.close()", "pass"):  # a tidy exit and an untidy one
        run_python(f"""
            from stacscope.core.buffer import FrameBuffer
            reader = FrameBuffer.open({name!r})
            assert reader.frames.shape == (3, 2, 2)
            {close}
        """)
        assert exists(name), f"segment removed after a reader exited ({close})"


def test_a_reader_closing_leaves_the_segment_alone(make_writer, name):
    writer = make_writer()
    writer.configure(LAYOUT)
    FrameBuffer.open(name).close()
    assert exists(name)


@pytest.mark.parametrize("bad", ["", "has space", "slash/name", "dash-name", "x" * 31, "ümlaut"])
def test_names_outside_the_portable_set_are_refused(bad):
    with pytest.raises(ValueError):
        FrameBuffer.create(bad, capacity_bytes=4096, max_slots=4)
    with pytest.raises(ValueError):
        FrameBuffer.open(bad)


def test_the_longest_portable_name_is_accepted():
    name = "B7_" + "x" * 27
    writer = FrameBuffer.create(name, capacity_bytes=4096, max_slots=4)
    writer.close()


def test_restarting_a_producer_while_a_reader_holds_the_old_segment(name):
    """Pins the one place the platforms differ, so it is known, not assumed.

    POSIX: the new producer gets a fresh segment and the old reader keeps an
    orphaned one. Windows: the old reader's handle keeps the name taken, so
    the new producer cannot start until that reader closes.
    """
    old = FrameBuffer.create(name, capacity_bytes=4096, max_slots=4)
    old.configure(LAYOUT)
    reader = FrameBuffer.open(name)
    old.close()
    try:
        if sys.platform == "win32":
            with pytest.raises(FileExistsError):
                FrameBuffer.create(name, capacity_bytes=4096, max_slots=4)
        else:
            new = FrameBuffer.create(name, capacity_bytes=4096, max_slots=4)
            assert reader.frames.shape == (3, 2, 2), "the orphan keeps its old layout"
            new.close()
    finally:
        reader.close()
