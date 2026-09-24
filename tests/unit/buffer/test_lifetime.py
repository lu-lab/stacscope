"""B7 — who creates, keeps and removes the segment, on every OS.

Windows removes a mapping when its last handle closes; POSIX keeps it in
/dev/shm until someone unlinks it, and its resource_tracker unlinks segments
a process merely opened when that process exits. Ownership is therefore
explicit: the producer creates, and removes on close; readers never remove.

A restarted producer takes over a segment its predecessor left, if it fits:
readers that still hold it carry on, and the counter keeps counting. That is
the same on both OSes. Only a segment that does not fit behaves differently:
POSIX replaces it, Windows cannot while a reader holds it.
"""

import struct
import subprocess
import sys
import textwrap
from multiprocessing.shared_memory import SharedMemory

import pytest

from buffer_support import produce
from stacscope.core.buffer import ImageBuffer, Layout

LAYOUT = Layout(shape=(2, 2), dtype="<u2", n_slots=3)
GENERATION = 64  # header offset of layout_generation


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


class CrashingProducer:
    """A producer in another process that sets up, waits, then dies without
    closing, while this process holds a reader on its segment."""

    def __init__(self, name, setup):
        code = textwrap.dedent(f"""
            import os, struct, sys
            from buffer_support import produce
            from stacscope.core.buffer import ImageBuffer, Layout
            writer = ImageBuffer.create({name!r}, capacity_bytes=1 << 16, max_slots=8)
        """) + textwrap.dedent(setup) + textwrap.dedent("""
            print("ready", flush=True)
            sys.stdin.readline()
            os._exit(0)  # no close: a crash
        """)
        self.child = subprocess.Popen(
            [sys.executable, "-c", code],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=__import__("pathlib").Path(__file__).parent,
        )
        assert self.child.stdout.readline().strip() == "ready", self.child.stderr.read()

    def crash(self):
        self.child.stdin.write("\n")
        self.child.stdin.flush()
        assert self.child.wait(timeout=8) == 0


def test_closing_the_producer_removes_the_segment(name):
    writer = ImageBuffer.create(name, capacity_bytes=4096, max_slots=4)
    writer.close()
    assert not exists(name)


def test_closing_the_producer_again_does_nothing(name):
    """A second close is harmless on every OS, and above all it never removes
    a segment that is no longer its own.

    POSIX removes by name, so a second unlink would remove whatever holds the
    name now: here a producer restarted under it, whose readers would lose
    their buffer to a close that belonged to its predecessor. Windows cannot
    show this (removal there is by last handle), so only the Linux leg of CI
    is red without the fix. Closing twice is also what cleanup code does when
    a test closed the producer itself, and that raised on POSIX alone."""
    old = ImageBuffer.create(name, capacity_bytes=4096, max_slots=4)
    old.close()
    successor = ImageBuffer.create(name, capacity_bytes=4096, max_slots=4)
    try:
        old.close()
        assert exists(name), "the successor's segment survives its predecessor's second close"
    finally:
        successor.close()


def test_a_restarted_producer_takes_over_the_crashed_ones_segment(name):
    crashed = CrashingProducer(name, """
        writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=3))
        writer.start_acquisition()
        produce(writer, 5)
    """)
    reader = ImageBuffer.open(name)
    crashed.crash()
    try:
        writer = ImageBuffer.create(name, capacity_bytes=1 << 16, max_slots=8)
        try:
            assert writer.published() == 5, "the counter carries on"
            cursor = reader.cursor()
            writer.configure(Layout(shape=(4, 4), dtype="<u2", n_slots=4))
            assert writer.start_acquisition() == 2, "so do acquisition ids"
            produce(writer, 2)

            frames = [cursor.next(), cursor.next()]
            assert [f.index for f in frames] == [5, 6]
            assert frames[0].data.shape == (4, 4), "the old reader follows the new layout"
            assert (frames[1].data == 6).all()
            del frames
        finally:
            writer.close()
    finally:
        reader.close()


def test_a_restart_during_a_layout_change_is_completed_not_trusted(name):
    crashed = CrashingProducer(name, f"""
        writer.configure(Layout(shape=(2, 2), dtype="<u2", n_slots=3))
        g = struct.unpack_from("<Q", writer._shm.buf, {GENERATION})[0]
        struct.pack_into("<Q", writer._shm.buf, {GENERATION}, g + 1)  # died mid-change
    """)
    holder = SharedMemory(name, track=False)  # keeps the segment alive on Windows
    crashed.crash()
    try:
        writer = ImageBuffer.create(name, capacity_bytes=1 << 16, max_slots=8)
        try:
            writer.configure(Layout(shape=(3, 3), dtype="<u2", n_slots=2))
            generation = struct.unpack_from("<Q", holder.buf, GENERATION)[0]
            assert generation % 2 == 0, "a settled layout"
            reader = ImageBuffer.open(name, timeout=0.1)
            assert reader.frames.shape == (2, 3, 3)
            reader.close()
        finally:
            writer.close()
    finally:
        holder.close()


@pytest.mark.parametrize(
    ("capacity_bytes", "max_slots"), [(1 << 20, 4), (4096, 8)], ids=["capacity", "slots"]
)
def test_a_segment_that_does_not_fit_is_replaced_or_refused(name, capacity_bytes, max_slots):
    """Pins the one place the platforms still differ. A segment too small for
    the new config can't be taken over: POSIX unlinks it and starts afresh
    (its readers keep an orphan); Windows can't free a name a reader holds."""
    old = ImageBuffer.create(name, capacity_bytes=4096, max_slots=4)
    old.configure(LAYOUT)
    reader = ImageBuffer.open(name)
    old._close()  # the old producer is gone; the reader still holds the segment
    try:
        if sys.platform == "win32":
            with pytest.raises(FileExistsError):
                ImageBuffer.create(name, capacity_bytes=capacity_bytes, max_slots=max_slots)
        else:
            new = ImageBuffer.create(name, capacity_bytes=capacity_bytes, max_slots=max_slots)
            assert new.published() == 0, "a fresh segment"
            assert reader.frames.shape == (3, 2, 2), "the orphan keeps its old layout"
            new.close()
    finally:
        reader.close()


@pytest.mark.parametrize(
    ("magic", "version"), [(b"JUNK", 1), (b"STCB", 2)], ids=["foreign", "other version"]
)
def test_a_segment_that_is_not_this_image_buffer_is_never_taken_over(name, magic, version):
    foreign = SharedMemory(name, create=True, size=4096, track=False)
    try:
        # Plausible in every other respect: big enough, enough slots.
        struct.pack_into("<4sI", foreign.buf, 0, magic, version)
        struct.pack_into("<QQ", foreign.buf, 16, 1 << 20, 64)
        if sys.platform == "win32":
            with pytest.raises(FileExistsError):
                ImageBuffer.create(name, capacity_bytes=4096, max_slots=4)
        else:
            new = ImageBuffer.create(name, capacity_bytes=4096, max_slots=4)
            assert bytes(foreign.buf[:4]) == magic, "the foreign segment was not written"
            new.close()
    finally:
        foreign.close()
        if sys.platform != "win32" and exists(name):
            foreign.unlink()


def test_a_reader_process_exiting_leaves_the_segment_alone(make_writer, name):
    writer = make_writer()
    writer.configure(LAYOUT)
    for close in ("reader.close()", "pass"):  # a tidy exit and an untidy one
        run_python(f"""
            from stacscope.core.buffer import ImageBuffer
            reader = ImageBuffer.open({name!r})
            assert reader.frames.shape == (3, 2, 2)
            {close}
        """)
        assert exists(name), f"segment removed after a reader exited ({close})"


def test_a_reader_closing_leaves_the_segment_alone(make_writer, name):
    writer = make_writer()
    writer.configure(LAYOUT)
    ImageBuffer.open(name).close()
    assert exists(name)


# A view a caller can hold, and whose close it must block: every view the
# buffer hands out, and what a Cursor builds from them.
HELD = {
    "writer frames": ("writer", lambda writer, reader: writer.frames),
    "writer frame": ("writer", lambda writer, reader: writer.frames[1]),
    "writer slots": ("writer", lambda writer, reader: writer.slots),
    "writer slot": ("writer", lambda writer, reader: writer.slots[0]),
    "writer meta": ("writer", lambda writer, reader: writer.meta),
    "reader frames": ("reader", lambda writer, reader: reader.frames),
    "reader meta": ("reader", lambda writer, reader: reader.meta),
    "reader Frame": ("reader", lambda writer, reader: reader.cursor(0).latest()),
    "reader Group": ("reader", lambda writer, reader: reader.cursor(0).latest_group(2)),
}


@pytest.mark.parametrize("closing, hold", HELD.values(), ids=HELD.keys())
def test_closing_under_a_live_view_raises_instead_of_unmapping(name, closing, hold):
    """Close with a view still alive raises ``BufferError`` and leaves the
    segment mapped; dropping the view lets the next close through.

    The alternative is worse than it looks: the segment is unmapped under the
    view, and the next touch of it (a viewer redrawing, a consumer finishing a
    frame) crashes the process with an access violation, with no traceback
    pointing at the close. An error at the close names the mistake where it
    was made. It is also what lets a test prove that something, a capture
    adapter after ``detach`` say, kept no view: without it, closing the
    segment proves nothing.

    Never touches a view after a close that went through: on the red side
    that is the crash itself."""
    writer = ImageBuffer.create(name, capacity_bytes=1 << 16, max_slots=8)
    reader = None
    try:
        writer.configure(LAYOUT)
        writer.start_acquisition()
        produce(writer, 2)
        reader = ImageBuffer.open(name)
        held = hold(writer, reader)
        assert held is not None
        side = writer if closing == "writer" else reader

        with pytest.raises(BufferError):
            side.close()
        del held
        side.close()
    finally:
        if reader is not None:
            reader.close()
        writer.close()
    assert not exists(name)


@pytest.mark.parametrize("bad", ["", "has space", "slash/name", "dash-name", "x" * 31, "ümlaut"])
def test_names_outside_the_portable_set_are_refused(bad):
    with pytest.raises(ValueError):
        ImageBuffer.create(bad, capacity_bytes=4096, max_slots=4)
    with pytest.raises(ValueError):
        ImageBuffer.open(bad)


def test_the_longest_portable_name_is_accepted():
    name = "B7_" + "x" * 27
    writer = ImageBuffer.create(name, capacity_bytes=4096, max_slots=4)
    writer.close()
