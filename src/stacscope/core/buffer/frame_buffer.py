"""A named shared-memory ring of frames: one producer, any number of readers.

The producer never waits on a reader and never learns that readers exist.
Readers get views into the shared memory, not copies.
"""

import mmap
import time
from multiprocessing.shared_memory import SharedMemory

import numpy as np

from stacscope.core.buffer import header
from stacscope.core.buffer.counter import Counter
from stacscope.core.buffer.header import HEADER_BYTES, MAGIC, META_RECORD_BYTES, VERSION
from stacscope.core.buffer.layout import Layout, LayoutError, ResolvedLayout


def _align(n, to):
    return -(-n // to) * to


class FrameBuffer:
    """Entry points: ``create`` for the producer, ``open`` for readers."""

    @staticmethod
    def create(name: str, *, capacity_bytes: int, max_slots: int) -> "FrameWriter":
        meta_offset = HEADER_BYTES
        data_offset = _align(meta_offset + max_slots * META_RECORD_BYTES, mmap.PAGESIZE)
        shm = SharedMemory(name, create=True, size=data_offset + capacity_bytes, track=False)
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
        return FrameReader(SharedMemory(name, track=False), timeout)


class _Mapped:
    """What producer and reader share: a mapped segment, the counter, and a
    frames view over the current layout."""

    def __init__(self, shm: SharedMemory):
        self._shm = shm
        self._counter = Counter(shm.buf)
        self._layout: ResolvedLayout | None = None
        self.frames: np.ndarray | None = None
        self.acquisition_id = 0
        self.acquisition_start = 0

    def published(self) -> int:
        """Frames published, ever. Frame ``i`` exists for ``i < published()``."""
        return self._counter.published()

    def slot_of(self, index: int) -> int:
        """The slot holding global frame ``index`` in the current acquisition."""
        return (index - self.acquisition_start) % self._layout.n_slots

    def _map(self, layout: ResolvedLayout, *, writable: bool):
        data_offset = header.read(self._shm.buf, "data_offset")
        frames = np.ndarray(
            (layout.n_slots, *layout.shape),
            layout.dtype,
            buffer=self._shm.buf,
            offset=data_offset + layout.frame_offset,
            strides=(layout.slot_bytes, *layout.strides),
        )
        frames.flags.writeable = writable
        self._layout = layout
        self.frames = frames

    def _release(self):
        """Drop our own views so the segment can close. Views a caller still
        holds keep it open, and ``close`` raises ``BufferError``."""
        self._layout = None
        self.frames = None

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

    def configure(self, layout: Layout) -> None:
        """Set the frame layout. Ends any acquisition in progress."""
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
            shape=resolved.shape + (0,) * pad,
            strides=resolved.strides + (0,) * pad,
        )
        self._acquiring = False
        self._map(resolved, writable=True)
        self.slots = np.ndarray(
            (resolved.n_slots, resolved.slot_bytes), np.uint8, buffer=self._shm.buf, offset=data_offset
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
        self._written += n
        self._counter.publish(self._written)

    def _rewrite(self, **fields) -> None:
        """Change the layout block under a seqlock: the generation is odd while
        fields change, so readers never see a half-written block."""
        buf = self._shm.buf
        generation = header.read(buf, "layout_generation")
        header.write(buf, "layout_generation", generation + 1)
        for name, value in fields.items():
            header.write(buf, name, value)
        header.write(buf, "layout_generation", generation + 2)

    def _release(self):
        super()._release()
        self.slots = None

    def close(self) -> None:
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

    def sync(self) -> bool:
        """Catch up with a new layout or acquisition. True if either changed.

        A layout block seen mid-change is ignored; the current mapping stays.
        """
        seen = _read_stable(self._shm.buf)
        if seen is None:
            return False
        generation, layout, acquisition = seen
        if generation == self._generation:
            return False
        self._generation = generation
        if layout is None:
            self._release()
            return False
        self.acquisition_id, self.acquisition_start = acquisition
        self._map(layout, writable=False)
        return True

    def _check_identity(self):
        buf = self._shm.buf
        if self._shm.size < HEADER_BYTES or header.read(buf, "magic") != MAGIC:
            raise LayoutError(f"{self._shm.name!r} is not a frame buffer")
        version = header.read(buf, "version")
        if version != VERSION:
            raise LayoutError(f"frame buffer version {version}, this reader speaks {VERSION}")

    def close(self) -> None:
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
    if header.read(buf, "layout_generation") != before:
        return None
    return before, layout, acquisition


def _read_layout(buf) -> ResolvedLayout:
    ndim = header.read(buf, "ndim")
    return ResolvedLayout(
        shape=header.read(buf, "shape")[:ndim],
        dtype=np.dtype(header.read(buf, "dtype").rstrip(b"\0").decode()),
        n_slots=header.read(buf, "n_slots"),
        strides=header.read(buf, "strides")[:ndim],
        frame_offset=header.read(buf, "frame_offset"),
        slot_bytes=header.read(buf, "slot_bytes"),
    )
