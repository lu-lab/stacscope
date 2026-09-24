"""The buffer monitor's model: what to show for a given slot, with no Qt import.

Opens an image-buffer segment by name and reports its state, slot count,
header fields, and a view of any slot on request. See
``stacscope.monitor.window`` for the window that draws this.
"""

# No Qt import here, on purpose: it is what makes the monitor a pure consumer
# of shared memory, free to run in a process of its own on a different
# interpreter from the engine it watches (the GUI toolkit has no free-threaded
# build; shared memory does not care). It is also what is tested -- the
# window cannot be tested on a machine with no display, and this can, so
# everything that could be wrong lives here.

from dataclasses import dataclass
from enum import Enum
from typing import Any

import numpy as np

from stacscope.core.buffer import ImageBuffer, LayoutError, header


class State(Enum):
    WAITING = "waiting"
    """No such segment yet. The usual first state: a monitor is started before
    the thing it monitors more often than not."""
    LIVE = "live"
    """Attached to a buffer."""


@dataclass(frozen=True, eq=False)
class View:
    """One look at one slot. ``image`` is a copy: the window may hold it for
    as long as it likes without keeping the segment mapped."""

    slot: int
    index: int
    """The frame currently occupying the slot, or -1 if no published frame has
    reached it yet. The slot's contents are shown either way."""
    image: np.ndarray
    meta: dict[str, int]
    acquisition_id: int
    intact: bool
    """False when this slot is one the producer may be writing right now -- it
    is within ``in_flight`` of the counter, or the ring has moved past it. The
    image may be torn. Shown anyway, and flagged: a monitor for debugging a
    buffer should report what is in the slot rather than hide it."""
    published: int


class BufferMonitor:
    def __init__(self, name: str, *, follow: bool = True) -> None:
        self.name = name
        self.follow = follow
        self.paused = False
        self.position = 0
        self._reader = None
        self._shown: View | None = None

    @property
    def state(self) -> State:
        return State.LIVE if self._reader is not None else State.WAITING

    def seek(self, slot: int) -> None:
        """Park on a slot. Stops following, as moving the slider means the user
        wants this part of the ring rather than wherever frames are landing."""
        self.follow = False
        self.position = slot

    def slots(self) -> int | None:
        """How many slots the ring has -- the slider's range, which does not
        move. None until attached to a buffer with a layout."""
        if self._reader is None:
            return None
        n_slots = header.read(self._reader._shm.buf, "n_slots")
        return n_slots or None

    def header(self) -> dict[str, Any]:
        """Every header field, as the header actually reads. The point of the
        tool is to show what is there, so nothing is prettified away."""
        if self._reader is None:
            return {}
        buf = self._reader._shm.buf
        fields = {name: header.read(buf, name) for name in header.FIELDS}
        fields["magic"] = fields["magic"].decode("ascii", "replace")
        fields["dtype"] = fields["dtype"].rstrip(b"\0").decode("ascii", "replace")
        ndim = fields["ndim"]
        fields["shape"] = tuple(fields["shape"][:ndim])
        fields["strides"] = tuple(fields["strides"][:ndim])
        return fields

    def poll(self) -> View | None:
        """A look, if anything changed. None when there is nothing new to draw."""
        return self._look(forced=False)

    def refresh(self) -> View | None:
        """Look now, whatever the state -- the same trigger as moving the
        slider, for when the user wants it without moving anything. Does not
        resume a paused monitor."""
        return self._look(forced=True)

    def close(self) -> None:
        if self._reader is not None:
            self._reader.close()
            self._reader = None
        self._shown = None

    # --- internals ------------------------------------------------------------

    def _look(self, *, forced: bool) -> View | None:
        if self.paused and not forced:
            return None
        if not self._attach():
            return None
        self._reader.sync()  # a new layout or acquisition, if there is one
        n_slots = self.slots()
        if n_slots is None:
            return None
        published = self._reader.published()
        if published == 0:
            return None

        slot = (
            self._reader.slot_of(published - 1)
            if self.follow
            else min(max(self.position, 0), n_slots - 1)
        )
        self.position = slot
        index = self._occupant(slot, published, n_slots)
        if not (forced or self._changed(slot, index)):
            return None
        self._shown = self._read(slot, index, published)
        return self._shown

    def _occupant(self, slot: int, published: int, n_slots: int) -> int:
        """The newest frame of this acquisition living in ``slot``, or -1 if
        none has reached it -- early in a run most of the ring is untouched.

        Every acquisition starts writing at slot 0, so the slot of a frame is
        counted from ``acquisition_start``, not from the frame index. A slot
        still holding the previous acquisition's bytes reports -1 rather than
        an index computed under the wrong origin, which would be fiction.
        """
        start = self._reader.acquisition_start
        newest = published - 1
        if newest < start:
            return -1
        here = (newest - start) % n_slots  # where the newest frame landed
        index = newest - (here - slot) % n_slots
        return index if index >= start else -1

    def _changed(self, slot: int, index: int) -> bool:
        """The slider moved, or the ring came round and dropped a new frame
        into the slot being watched. Nothing else changes what is on screen."""
        shown = self._shown
        return shown is None or shown.slot != slot or shown.index != index

    def _read(self, slot: int, index: int, published: int) -> View:
        reader = self._reader
        image = np.array(reader.frames[slot])  # copied before anything else
        # Copied before its fields are read one by one, so the record shown is
        # one moment's rather than stitched from several as the producer wrote
        # over it. No test pins this: the race cannot be made deterministic,
        # and its absence would show only as a metadata row mixing two frames.
        record = reader.meta[slot].copy()
        intact = index >= 0 and reader.still_valid(index)
        return View(
            slot=slot,
            index=index,
            image=image,
            meta={name: record[name].item() for name in record.dtype.names},
            acquisition_id=reader.acquisition_id,
            intact=intact,
            published=published,
        )

    def _attach(self) -> bool:
        if self._reader is not None:
            return True
        try:
            self._reader = ImageBuffer.open(self.name)
        except (FileNotFoundError, LayoutError):
            return False  # not there yet, or not settled on a layout
        return True
