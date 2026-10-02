"""A simulated frame grabber: triggers in, frames written into the slots it was
given, as a camera driver's DMA would.

Triggers reach it on the simulated wire (``TriggerBus``), never through a
message. Each frame shows a bar that moves one column further per frame
(or more, if asked), so a viewer shows steady motion. It writes pixels and
nothing else: publishing the frames is the job of whatever gave it the
memory.
"""

import asyncio
import time

import numpy as np

from stacscope.core.buffer import MetaField, Metadata, TimeSource
from stacscope.core.ports import STAMP_DTYPE, Clock, FrameFormat, FramesLost


class SimulatedFrameCapture:
    """Implements ``FrameCapture`` on a ``Clock``, listening for rising edges
    on ``trigger`` if given a ``bus``.

    The format is set here, as a camera's settings would set it: ``shape`` and
    ``dtype`` of the pixels, ``row_bytes`` for a row pitch wider than the
    pixels, ``frame_offset`` for bytes before the first pixel, and the
    ``slot_alignment`` and ``in_flight`` depth the hardware asks for.
    ``reformat`` changes any of them later, between captures.
    """

    def __init__(
        self,
        clock: Clock,
        bus=None,
        *,
        trigger: str = "camera_trigger",
        shape: tuple[int, int] = (64, 64),
        dtype: str = "<u2",
        row_bytes: int | None = None,
        frame_offset: int = 0,
        slot_alignment: int = 64,
        in_flight: int = 1,
        device_clock_hz: float = 1e7,
        bar_width: int = 4,
        step: int = 1,
    ) -> None:
        self.clock = clock
        self.bus = bus
        self.trigger = trigger
        self._bar_width = bar_width
        self._step = step
        self._settings = dict(
            shape=tuple(shape),
            dtype=dtype,
            row_bytes=row_bytes,
            frame_offset=frame_offset,
            slot_alignment=slot_alignment,
            in_flight=in_flight,
        )
        self._set_format()
        self._metadata = Metadata(
            fields=MetaField.HW_FRAME | MetaField.DEVICE_TIME | MetaField.HOST_TIME,
            device_time_source=TimeSource.CAMERA,
            device_clock_hz=device_clock_hz,
        )
        self._slots: np.ndarray | None = None
        self._frames: np.ndarray | None = None
        """A typed view of the slots, as the format places pixels in them."""
        self._stamps: np.ndarray | None = None
        """The stamps of the frame each slot holds."""
        self._capturing = False
        self._delivered = 0
        self._hw_frame = 0
        self._next_hw_frame = 0
        self._lost: FramesLost | None = None
        self._dropping = 0
        self._changed = asyncio.Event()
        self.closed = False
        self.missed = 0
        """Triggers that arrived when no capture was running to take them."""
        if bus is not None:
            bus.subscribe(trigger, self._triggered)

    # --- FrameCapture ----------------------------------------------------------

    def frame_format(self) -> FrameFormat:
        self._open()
        return self._format

    def metadata(self) -> Metadata:
        self._open()
        return self._metadata

    async def attach(self, slots: np.ndarray) -> None:
        try:
            self._open()
            if self._slots is not None:
                raise RuntimeError("frame memory is already attached; detach it first")
            fmt = self._format
            if slots.dtype != np.uint8 or slots.ndim != 2 or slots.strides[1] != 1:
                raise ValueError("slots must be an (n_slots, slot_bytes) array of contiguous bytes")
            if slots.shape[1] < fmt.frame_bytes:
                raise ValueError(f"slots of {slots.shape[1]} bytes cannot hold {fmt.frame_bytes}")
            # Every slot, not only the first: a stride off the alignment puts the
            # second slot off it too.
            if slots.ctypes.data % fmt.slot_alignment or slots.strides[0] % fmt.slot_alignment:
                raise ValueError(f"slots must start on {fmt.slot_alignment}-byte boundaries")
            if not slots.flags.writeable:
                raise ValueError("slots must be writable")
            self._slots = slots
            self._frames = _typed(slots, fmt)
            # One stamp per slot, overwritten with the slot: as on a camera, only
            # frames still in the slots have stamps to read.
            self._stamps = np.zeros(len(slots), STAMP_DTYPE)
        except BaseException:
            # The error's traceback keeps this frame, and the slots, alive:
            # while anyone holds it the segment cannot close (K9).
            del slots
            raise

    async def start(self) -> None:
        self._open()
        if self._slots is None:
            raise RuntimeError("attach frame memory before starting")
        # Every capture counts from 0: delivered frames, and the camera's own
        # frame numbers, which the next delivery checks for a gap.
        self._delivered = 0
        self._hw_frame = 0
        self._next_hw_frame = 0
        self._lost = None
        self._capturing = True

    async def wait_frames(self, seen: int, timeout: float) -> int:
        self._open()
        try:
            delivered = await self.clock.wait_for(self._arrival(seen), timeout)
        except TimeoutError:
            delivered = self._delivered  # nothing new in time: report the count so far
        # A loss is raised on every wait from then on, so a caller cannot miss it.
        if self._lost is not None:
            raise self._lost
        return delivered

    def stamps(self, first: int, n: int) -> np.ndarray:
        self._open()
        if self._stamps is None:
            raise RuntimeError("no frame memory is attached")
        # Frames older than one ring have had their slots, and stamps, reused.
        held = len(self._stamps)
        if n < 0 or first < max(0, self._delivered - held) or first + n > self._delivered:
            raise ValueError(
                f"frames {first} to {first + n - 1} are not all in the slots, "
                f"which hold {max(0, self._delivered - held)} to {self._delivered - 1}"
            )
        return self._stamps[np.arange(first, first + n) % held]

    async def stop(self) -> None:
        self._capturing = False
        self._wake()

    async def detach(self) -> None:
        # Stopped first: memory is never taken back from a running capture.
        await self.stop()
        self._slots = None
        self._frames = None
        self._stamps = None

    async def close(self) -> None:
        if self.closed:
            return
        await self.detach()
        if self.bus is not None:
            self.bus.unsubscribe(self.trigger, self._triggered)
        self.closed = True

    # --- simulation controls ------------------------------------------------------

    def drop(self, n: int = 1) -> None:
        """Lose the next ``n`` frames: exposed and counted, never delivered."""
        self._dropping += n

    def reformat(self, **settings) -> None:
        """Change any of the format settings given to the constructor, as a
        camera setting such as ROI would. ``frame_format()`` reports the change
        at once, and the next memory attached is written in it. Raises
        ``RuntimeError`` while memory is attached."""
        if self._slots is not None:
            raise RuntimeError("frame memory is attached; detach it first")
        self._settings.update(settings)
        self._set_format()

    # --- internals ---------------------------------------------------------------

    def _triggered(self) -> None:
        """A trigger pulse arrived. Outside any mailbox, like a DMA completion."""
        if not self._capturing:
            self.missed += 1
            return
        if self._dropping:
            # Exposed, never delivered: the camera counted it, the slots never
            # saw it. Only the gap in hw_frame at the next delivery shows it,
            # as it would in DCAM's frame stamps.
            self._dropping -= 1
            self._hw_frame += 1
            return
        # A gap in the camera's numbering since the last delivery: frames were
        # exposed and never delivered. The first gap is the loss reported, and
        # this frame is still delivered, as a driver's would be.
        if self._hw_frame != self._next_hw_frame and self._lost is None:
            self._lost = FramesLost(self._delivered, self._hw_frame - self._next_hw_frame)
        # Written as DMA would: pixels into slot i mod n, then its stamp.
        slot = self._delivered % len(self._frames)
        frame = self._frames[slot]
        frame[...] = 0
        bar = (np.arange(self._bar_width) + self._hw_frame * self._step) % frame.shape[-1]
        frame[..., bar] = self._bright
        self._stamps[slot] = (
            self._hw_frame,
            round(self.clock.now() * self._metadata.device_clock_hz),
            time.monotonic_ns(),  # the actors' clock, not the wall clock, which can jump
        )
        self._hw_frame += 1
        self._next_hw_frame = self._hw_frame
        self._delivered += 1
        self._wake()

    async def _arrival(self, seen: int) -> int:
        # Until there is something new, the capture stops, or a loss is found.
        while self._capturing and self._delivered <= seen and self._lost is None:
            await self._changed.wait()
        return self._delivered

    def _wake(self) -> None:
        # One event per change, replaced as it is set, so every waiter wakes
        # and a later waiter never sees a stale set event.
        self._changed.set()
        self._changed = asyncio.Event()

    def _open(self) -> None:
        if self.closed:
            raise RuntimeError("the capture has been closed")

    def _set_format(self) -> None:
        settings = self._settings
        shape, dtype = tuple(settings["shape"]), np.dtype(settings["dtype"])
        self._bright = np.iinfo(dtype).max if dtype.kind in "iu" else 1  # the bar: full scale
        row_bytes = settings["row_bytes"] or shape[1] * dtype.itemsize
        self._format = FrameFormat(
            shape=shape,
            dtype=settings["dtype"],
            strides=(row_bytes, dtype.itemsize),
            frame_bytes=settings["frame_offset"] + shape[0] * row_bytes,
            frame_offset=settings["frame_offset"],
            slot_alignment=settings["slot_alignment"],
            in_flight=settings["in_flight"],
        )


class _Pixels:
    """Presents the pixels in a set of byte slots as one typed array. It holds
    the slots, so the typed view keeps them alive as a slice would."""

    def __init__(self, slots: np.ndarray, fmt: FrameFormat) -> None:
        self.slots = slots
        self.__array_interface__ = {
            "version": 3,
            "shape": (len(slots), *fmt.shape),
            "typestr": np.dtype(fmt.dtype).str,
            "data": (slots.ctypes.data + fmt.frame_offset, False),
            "strides": (slots.strides[0], *fmt.strides),
        }


def _typed(slots: np.ndarray, fmt: FrameFormat) -> np.ndarray:
    # np.ndarray(buffer=...) needs a contiguous buffer, and slots whose stride
    # exceeds their width are valid but not contiguous.
    return np.asarray(_Pixels(slots, fmt))
