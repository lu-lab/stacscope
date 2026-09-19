"""A named shared-memory ring of frames: one producer, any number of readers.

The producer never waits on a reader and never learns that readers exist.
Readers get views into the shared memory, not copies.
"""

import mmap
import time
from multiprocessing.shared_memory import SharedMemory

import numpy as np

from stacscope.core.buffer import header
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
    """What producer and reader share: a mapped segment and a frames view."""

    def __init__(self, shm: SharedMemory):
        self._shm = shm
        self.frames: np.ndarray | None = None

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
        self.frames = frames

    def _release(self):
        """Drop our own views so the segment can close. Views a caller still
        holds keep it open, and ``close`` raises ``BufferError``."""
        self.frames = None


class FrameWriter(_Mapped):
    """The producer's side. Owns the segment and removes it on close."""

    def __init__(self, shm: SharedMemory):
        super().__init__(shm)
        self.slots: np.ndarray | None = None

    def configure(self, layout: Layout) -> None:
        resolved = layout.resolve()
        buf = self._shm.buf
        if resolved.total_bytes > header.read(buf, "capacity_bytes"):
            raise LayoutError(f"layout needs {resolved.total_bytes} bytes, buffer holds less")
        if resolved.n_slots > header.read(buf, "max_slots"):
            raise LayoutError(f"{resolved.n_slots} slots exceed the buffer's maximum")
        pad = header.MAX_NDIM - len(resolved.shape)
        # A seqlock: odd while the layout block is being rewritten, so readers
        # never map a half-written layout.
        generation = header.read(buf, "layout_generation")
        header.write(buf, "layout_generation", generation + 1)
        header.write(buf, "n_slots", resolved.n_slots)
        header.write(buf, "slot_bytes", resolved.slot_bytes)
        header.write(buf, "frame_offset", resolved.frame_offset)
        header.write(buf, "dtype", resolved.dtype.str.encode())
        header.write(buf, "ndim", len(resolved.shape))
        header.write(buf, "shape", resolved.shape + (0,) * pad)
        header.write(buf, "strides", resolved.strides + (0,) * pad)
        header.write(buf, "layout_generation", generation + 2)
        data_offset = header.read(buf, "data_offset")
        del buf
        self._map(resolved, writable=True)
        self.slots = np.ndarray(
            (resolved.n_slots, resolved.slot_bytes), np.uint8, buffer=self._shm.buf, offset=data_offset
        )

    def _release(self):
        super()._release()
        self.slots = None

    def close(self) -> None:
        self._release()
        self._shm.close()
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
            self._release()
            shm.close()
            raise

    def sync(self) -> bool:
        """Re-map if the producer changed the layout. True if it did.

        A layout seen mid-change is ignored; the current mapping stays.
        """
        seen = _read_stable_layout(self._shm.buf)
        if seen is None:
            return False
        generation, layout = seen
        if generation == self._generation:
            return False
        self._generation = generation
        if layout is None:
            self._release()
            return False
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
        self._release()
        self._shm.close()


def _read_stable_layout(buf) -> tuple[int, ResolvedLayout | None] | None:
    """(generation, layout) if the layout block is stable, else None.
    Generation 0 is a buffer the producer has not configured yet."""
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
    if header.read(buf, "layout_generation") != before:
        return None
    return before, layout


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
