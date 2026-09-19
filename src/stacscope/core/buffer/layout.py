"""The producer's description of where frames sit in the buffer."""

from dataclasses import dataclass

import numpy as np

from stacscope.core.buffer.header import MAX_NDIM


class LayoutError(ValueError):
    """A layout that cannot describe a frame, or a segment that is not a buffer."""


@dataclass(frozen=True)
class Layout:
    """How the producer's hardware places frames in slots.

    ``strides`` default to C order; ``slot_bytes`` defaults to the smallest
    slot that holds ``frame_offset`` plus the frame. Padding and embedded
    device data are expressed through these, never removed.
    """

    shape: tuple[int, ...]
    dtype: str
    n_slots: int
    strides: tuple[int, ...] | None = None
    frame_offset: int = 0
    slot_bytes: int | None = None

    def resolve(self) -> "ResolvedLayout":
        dtype = np.dtype(self.dtype)
        if dtype.kind not in "biufc" or len(dtype.str) > 8:
            raise LayoutError(f"unsupported dtype {self.dtype!r}")
        shape = tuple(int(n) for n in self.shape)
        if not 1 <= len(shape) <= MAX_NDIM:
            raise LayoutError(f"a frame has 1 to {MAX_NDIM} dimensions, not {len(shape)}")
        if any(n < 1 for n in shape):
            raise LayoutError(f"empty frame shape {shape}")
        if self.n_slots < 1:
            raise LayoutError("a buffer needs at least one slot")
        strides = _c_strides(shape, dtype.itemsize) if self.strides is None else tuple(self.strides)
        if len(strides) != len(shape):
            raise LayoutError(f"{len(strides)} strides for a {len(shape)}-d frame")
        extent = _extent(shape, strides, dtype.itemsize)
        if self.frame_offset < 0:
            raise LayoutError("negative frame offset")
        needed = self.frame_offset + extent
        slot_bytes = needed if self.slot_bytes is None else self.slot_bytes
        if slot_bytes < needed:
            raise LayoutError(f"frame needs {needed} bytes of its slot, slot has {slot_bytes}")
        return ResolvedLayout(shape, dtype, self.n_slots, strides, self.frame_offset, slot_bytes)


@dataclass(frozen=True)
class ResolvedLayout:
    shape: tuple[int, ...]
    dtype: np.dtype
    n_slots: int
    strides: tuple[int, ...]
    frame_offset: int
    slot_bytes: int

    @property
    def total_bytes(self) -> int:
        return self.n_slots * self.slot_bytes


def _c_strides(shape, itemsize):
    strides, step = [], itemsize
    for n in reversed(shape):
        strides.append(step)
        step *= n
    return tuple(reversed(strides))


def _extent(shape, strides, itemsize):
    """Bytes spanned by a frame, requiring each dimension to nest cleanly
    inside the one before it, so no two pixels share bytes."""
    inner = itemsize
    for n, stride in zip(reversed(shape), reversed(strides)):
        if stride < inner and n > 1:
            raise LayoutError(f"stride {stride} overlaps an inner extent of {inner} bytes")
        inner = (n - 1) * stride + inner
    return inner
