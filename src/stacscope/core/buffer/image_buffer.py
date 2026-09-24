"""A named shared-memory ring of frames: one producer, any number of readers.

The producer never waits on a reader and never learns that readers exist.
Readers get views into the shared memory, not copies.
"""

import mmap
import re
import time
from multiprocessing.shared_memory import SharedMemory

import numpy as np

from stacscope.core.buffer import header
from stacscope.core.buffer.counter import Counter, require_ordered_memory
from stacscope.core.buffer.cursor import Cursor
from stacscope.core.buffer.header import HEADER_BYTES, MAGIC, META_RECORD_BYTES, VERSION
from stacscope.core.buffer.layout import Layout, LayoutError, ResolvedLayout
from stacscope.core.buffer.metadata import META_DTYPE, Metadata, MetaField, TimeSource


def _align(n, to):
    return -(-n // to) * to


# Portable on every OS: Windows kernel names, POSIX /dev/shm file names, and
# macOS's 31-character shm_open limit including the leading slash.
_NAME = re.compile(r"[A-Za-z0-9_]{1,30}")


def check_name(name: str) -> str:
    """The one place buffer names are validated."""
    if not isinstance(name, str) or not _NAME.fullmatch(name):
        raise ValueError(f"buffer name {name!r} must be 1-30 of [A-Za-z0-9_]")
    return name


def _take_over(name: str, capacity_bytes: int, max_slots: int) -> SharedMemory | None:
    """The segment a previous producer left under ``name``, if it is a frame
    buffer this producer can use; otherwise None.

    Taking it over is what lets readers that still hold it carry on across a
    producer restart, and it behaves the same on every OS.
    """
    shm = SharedMemory(name, track=False)
    buf = shm.buf
    fits = (
        shm.size >= HEADER_BYTES
        and header.read(buf, "magic") == MAGIC
        and header.read(buf, "version") == VERSION
        and header.read(buf, "capacity_bytes") >= capacity_bytes
        and header.read(buf, "max_slots") >= max_slots
    )
    del buf
    if fits:
        return shm
    shm.close()
    return None


def _replace_segment(name: str, size: int) -> SharedMemory:
    """Unlink a segment that can't be taken over and create afresh.

    POSIX can, and the old segment's readers keep an orphan. Windows cannot:
    a mapping lives while any handle is open, so the name stays taken until
    the last reader of the old segment closes.
    """
    stale = SharedMemory(name, track=False)
    stale.close()
    stale.unlink()
    try:
        return SharedMemory(name, create=True, size=size, track=False)
    except FileExistsError:
        raise FileExistsError(
            f"buffer {name!r} is held by another process and doesn't fit this producer; "
            "close its readers first"
        ) from None


class ImageBuffer:
    """Entry points: ``create`` for the producer, ``open`` for readers."""

    @staticmethod
    def create(name: str, *, capacity_bytes: int, max_slots: int) -> "FrameWriter":
        """Create the buffer, or take over the one a previous producer left
        under this name if it fits: its counter and acquisition ids carry on,
        and readers still holding it follow the next ``configure``."""
        require_ordered_memory()
        check_name(name)
        meta_offset = HEADER_BYTES
        data_offset = _align(meta_offset + max_slots * META_RECORD_BYTES, mmap.PAGESIZE)
        size = data_offset + capacity_bytes
        try:
            shm = SharedMemory(name, create=True, size=size, track=False)
        except FileExistsError:
            previous = _take_over(name, capacity_bytes, max_slots)
            if previous is not None:
                return FrameWriter(previous)
            shm = _replace_segment(name, size)
        buf = shm.buf
        header.write(buf, "magic", MAGIC)
        header.write(buf, "version", VERSION)
        header.write(buf, "header_bytes", HEADER_BYTES)
        header.write(buf, "meta_record_bytes", META_RECORD_BYTES)
        header.write(buf, "capacity_bytes", capacity_bytes)
        header.write(buf, "max_slots", max_slots)
        header.write(buf, "data_offset", data_offset)
        header.write(buf, "meta_offset", meta_offset)
        del buf
        return FrameWriter(shm)

    @staticmethod
    def open(name: str, *, timeout: float = 1.0) -> "FrameReader":
        """Map a buffer by name. If the producer is mid-way through a layout
        change, retry until it settles or ``timeout`` expires."""
        require_ordered_memory()
        return FrameReader(SharedMemory(check_name(name), track=False), timeout)


class _Mapped:
    """What producer and reader share: a mapped segment, the counter, and a
    frames view over the current layout."""

    def __init__(self, shm: SharedMemory):
        self._shm = shm
        self._counter = Counter(shm.buf)
        self._layout: ResolvedLayout | None = None
        self.frames: np.ndarray | None = None
        self.meta: np.ndarray | None = None
        self.metadata = Metadata()
        self.acquisition_id = 0
        self.acquisition_start = 0

    def published(self) -> int:
        """Frames published, ever. Frame ``i`` exists for ``i < published()``."""
        return self._counter.published()

    def oldest(self) -> int:
        """The oldest frame still safe to read: in this acquisition, and not in
        a slot the device may already be overwriting."""
        return self._window()[0]

    def still_valid(self, index: int) -> bool:
        """Whether frame ``index`` is published and not yet overwritten. Check it
        after using a view to know the view was not torn."""
        oldest, published = self._window()
        return oldest <= index < published

    def _window(self) -> tuple[int, int]:
        """(oldest safe frame, published) from one read of the counter."""
        published = self.published()
        return self._oldest(published), published

    def _oldest(self, published: int) -> int:
        layout = self._layout
        if layout is None:
            return published
        return max(self.acquisition_start, published + layout.in_flight - layout.n_slots)

    def slot_of(self, index: int) -> int:
        """The slot holding global frame ``index`` in the current acquisition."""
        return (index - self.acquisition_start) % self._layout.n_slots

    def _bytes(self) -> np.ndarray:
        """The whole segment as bytes. Build every view from this."""
        # np.frombuffer holds a buffer export for as long as it, or any view
        # derived from it, lives, so SharedMemory.close() raises BufferError
        # while a caller holds one. np.ndarray(buffer=self._shm.buf) keeps
        # only a reference to the mmap and releases the export, so the close
        # went through and unmapped the memory under live views: the next
        # touch was an access violation.
        return np.frombuffer(self._shm.buf, np.uint8)

    def _map(self, layout: ResolvedLayout, *, writable: bool):
        data_offset = header.read(self._shm.buf, "data_offset")
        frames = np.ndarray(
            (layout.n_slots, *layout.shape),
            layout.dtype,
            buffer=self._bytes(),
            offset=data_offset + layout.frame_offset,
            strides=(layout.slot_bytes, *layout.strides),
        )
        frames.flags.writeable = writable
        meta = np.ndarray(
            (layout.n_slots,), META_DTYPE, buffer=self._bytes(), offset=header.read(self._shm.buf, "meta_offset")
        )
        meta.flags.writeable = writable
        self._layout = layout
        self.frames = frames
        self.meta = meta

    def _release(self):
        """Drop our own views so the segment can close. Views a caller still
        holds keep it open, and ``close`` raises ``BufferError``."""
        self._layout = None
        self.frames = None
        self.meta = None

    def _close(self):
        self._release()
        self._counter.release()
        self._shm.close()


class FrameWriter(_Mapped):
    """The producer's side. Owns the segment and removes it on close."""

    def __init__(self, shm: SharedMemory):
        super().__init__(shm)
        self.slots: np.ndarray | None = None
        self._acquiring = False
        self._written = self._counter.published()
        self.acquisition_id = header.read(shm.buf, "acquisition_id")
        self.acquisition_start = header.read(shm.buf, "acquisition_start")

    def configure(self, layout: Layout, metadata: Metadata = Metadata()) -> None:
        """Set the frame layout and declare what per-frame metadata the
        producer fills. Ends any acquisition in progress."""
        resolved = layout.resolve()
        buf = self._shm.buf
        if resolved.total_bytes > header.read(buf, "capacity_bytes"):
            raise LayoutError(f"layout needs {resolved.total_bytes} bytes, buffer holds less")
        if resolved.n_slots > header.read(buf, "max_slots"):
            raise LayoutError(f"{resolved.n_slots} slots exceed the buffer's maximum")
        data_offset = header.read(buf, "data_offset")
        del buf
        pad = header.MAX_NDIM - len(resolved.shape)
        self._rewrite(
            n_slots=resolved.n_slots,
            slot_bytes=resolved.slot_bytes,
            frame_offset=resolved.frame_offset,
            dtype=resolved.dtype.str.encode(),
            ndim=len(resolved.shape),
            in_flight=resolved.in_flight,
            shape=resolved.shape + (0,) * pad,
            strides=resolved.strides + (0,) * pad,
            metadata_fields=int(metadata.fields),
            device_time_source=int(metadata.device_time_source),
            device_clock_hz=float(metadata.device_clock_hz),
        )
        self.metadata = metadata
        self._acquiring = False
        self._map(resolved, writable=True)
        self.slots = np.ndarray(
            (resolved.n_slots, resolved.slot_bytes), np.uint8, buffer=self._bytes(), offset=data_offset
        )

    def start_acquisition(self) -> int:
        """Begin a new acquisition at the next frame; it writes from slot 0.
        Returns its id."""
        if self._layout is None:
            raise RuntimeError("configure a layout before starting an acquisition")
        self.acquisition_id += 1
        self.acquisition_start = self._written
        self._rewrite(acquisition_id=self.acquisition_id, acquisition_start=self.acquisition_start)
        self._acquiring = True
        return self.acquisition_id

    def publish(self, n: int = 1) -> None:
        """Declare the next ``n`` frames written. The only way the counter moves."""
        if not self._acquiring:
            raise RuntimeError("publish outside an acquisition")
        for index in range(self._written, self._written + n):
            self.meta[self.slot_of(index)]["sequence"] = index
        self._written += n
        self._counter.publish(self._written)

    def stamp(self, index: int, *, hw_frame: int = 0, device_ticks: int = 0, host_time_ns: int = 0) -> None:
        """Record what the device reported for frame ``index``, before
        publishing it. Writes only the metadata table, never the slot."""
        record = self.meta[self.slot_of(index)]
        record["hw_frame"] = hw_frame
        record["device_ticks"] = device_ticks
        record["host_time_ns"] = host_time_ns

    def _rewrite(self, **fields) -> None:
        """Change the layout block under a seqlock: the generation is odd while
        fields change, so readers never see a half-written block."""
        buf = self._shm.buf
        # Already odd means a previous producer died mid-change: finish it.
        changing = header.read(buf, "layout_generation") | 1
        header.write(buf, "layout_generation", changing)
        for name, value in fields.items():
            header.write(buf, name, value)
        header.write(buf, "layout_generation", changing + 1)

    def _release(self):
        super()._release()
        self.slots = None

    def close(self) -> None:
        """Unmap the segment and remove it. Raises ``BufferError`` while any
        view into it (``frames``, ``slots``, ``meta``, or a slice of them) is
        still alive; drop those and close again."""
        self._close()
        self._shm.unlink()


class FrameReader(_Mapped):
    """A consumer's side. Read-only views; never removes the segment."""

    def __init__(self, shm: SharedMemory, timeout: float):
        super().__init__(shm)
        self._generation: int | None = None
        try:
            self._check_identity()
            deadline = time.monotonic() + timeout
            while not self.sync() and self._generation is None:
                if time.monotonic() > deadline:
                    raise LayoutError(f"{shm.name!r} never settled on a layout")
                time.sleep(0.001)
        except BaseException:
            self._close()
            raise

    def cursor(self, position: int | None = None) -> Cursor:
        """A reading position of this reader's own. Starts at the next frame to
        be published unless given one (e.g. ``oldest()``)."""
        return Cursor(self, self.published() if position is None else position)

    def sync(self) -> bool:
        """Catch up with a new layout or acquisition. True if either changed.

        A layout block seen mid-change is ignored; the current mapping stays.
        """
        if header.read(self._shm.buf, "layout_generation") == self._generation:
            return False
        seen = _read_stable(self._shm.buf)
        if seen is None:
            return False
        generation, layout, acquisition, metadata = seen
        if generation == self._generation:
            return False
        self._generation = generation
        if layout is None:
            self._release()
            return False
        self.acquisition_id, self.acquisition_start = acquisition
        self.metadata = metadata
        self._map(layout, writable=False)
        return True

    def _window(self) -> tuple[int, int]:
        # The producer changes the layout block before publishing frames under
        # it, so if the generation is unchanged after loading the counter,
        # every frame below that count belongs to the layout this reader holds.
        while True:
            published = self.published()
            if header.read(self._shm.buf, "layout_generation") == self._generation:
                return self._oldest(published), published
            if not self.sync():
                time.sleep(0)  # the producer is mid-change

    def _check_identity(self):
        buf = self._shm.buf
        if self._shm.size < HEADER_BYTES or header.read(buf, "magic") != MAGIC:
            raise LayoutError(f"{self._shm.name!r} is not an image buffer")
        version = header.read(buf, "version")
        if version != VERSION:
            raise LayoutError(f"image buffer version {version}, this reader speaks {VERSION}")

    def close(self) -> None:
        """Unmap the segment, leaving it for the producer and other readers.
        Raises ``BufferError`` while any view into it (``frames``, ``meta``, a
        ``Frame`` or ``Group``) is still alive; drop those and close again."""
        self._close()


def _read_stable(buf):
    """(generation, layout, (acquisition_id, acquisition_start)) if the layout
    block is stable, else None. Generation 0 is a buffer the producer has not
    configured yet, and has no layout."""
    before = header.read(buf, "layout_generation")
    if before % 2:
        return None
    try:
        layout = _read_layout(buf) if before else None
    except (TypeError, ValueError):
        # Half-written fields (a torn dtype, say) from a change that began
        # after the first read; the second read below tells which.
        layout = None
        if header.read(buf, "layout_generation") == before:
            raise
    acquisition = (header.read(buf, "acquisition_id"), header.read(buf, "acquisition_start"))
    try:
        metadata = Metadata(
            MetaField(header.read(buf, "metadata_fields")),
            TimeSource(header.read(buf, "device_time_source")),
            header.read(buf, "device_clock_hz"),
        )
    except ValueError:
        metadata = None  # torn, or garbage; the generation check decides
    if header.read(buf, "layout_generation") != before:
        return None
    if metadata is None:
        raise LayoutError("unreadable metadata declaration in a stable header")
    return before, layout, acquisition, metadata


def _read_layout(buf) -> ResolvedLayout:
    ndim = header.read(buf, "ndim")
    return ResolvedLayout(
        shape=header.read(buf, "shape")[:ndim],
        dtype=np.dtype(header.read(buf, "dtype").rstrip(b"\0").decode()),
        n_slots=header.read(buf, "n_slots"),
        strides=header.read(buf, "strides")[:ndim],
        frame_offset=header.read(buf, "frame_offset"),
        slot_bytes=header.read(buf, "slot_bytes"),
        in_flight=header.read(buf, "in_flight"),
    )
