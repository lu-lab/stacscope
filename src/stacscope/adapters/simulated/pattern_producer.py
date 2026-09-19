"""A synthetic camera: a small bar that moves a few columns each frame.

Each frame touches only its bar, plus the bar its slot held one ring ago,
so emitting costs almost nothing however large the frame, and a person
watching a viewer sees steady motion. Metadata is filled as a camera would:
its own frame numbers, a device clock, and host time.
"""

import time

import numpy as np

from stacscope.core.buffer import FrameWriter, Layout, MetaField, Metadata, TimeSource


class PatternProducer:
    def __init__(
        self,
        writer: FrameWriter,
        *,
        bar_width: int = 4,
        step: int = 1,
        device_clock_hz: float = 1e7,
        frame_period: float = 0.01,
    ):
        self._writer = writer
        self._bar_width = bar_width
        self._step = step
        self._ticks_per_frame = round(device_clock_hz * frame_period)
        self.metadata = Metadata(
            fields=MetaField.HW_FRAME | MetaField.DEVICE_TIME | MetaField.HOST_TIME,
            device_time_source=TimeSource.CAMERA,
            device_clock_hz=device_clock_hz,
        )
        self._bars: dict[int, np.ndarray] = {}
        self._hw_frame = 0
        self._value = 0

    def configure(self, layout: Layout) -> None:
        self._writer.configure(layout, self.metadata)
        self.reset()

    def reset(self) -> None:
        """Start drawing afresh against the writer's current layout. Separate
        from ``configure`` because a device may have configured the buffer
        itself — a streaming device does, as part of arming."""
        self._writer.frames[...] = 0  # once, so old pixels don't read as bars
        self._bars.clear()
        dtype = self._writer.frames.dtype
        self._value = np.iinfo(dtype).max if dtype.kind in "iu" else 1

    def start_acquisition(self) -> int:
        return self._writer.start_acquisition()

    def emit(self, n: int = 1) -> None:
        """Produce ``n`` frames: draw, stamp, publish."""
        writer = self._writer
        width = writer.frames.shape[-1]
        for _ in range(n):
            index = writer.published()
            slot = writer.slot_of(index)
            frame = writer.frames[slot]
            old = self._bars.get(slot)
            if old is not None:
                frame[..., old] = 0
            bar = (np.arange(self._bar_width) + index * self._step) % width
            frame[..., bar] = self._value
            self._bars[slot] = bar
            writer.stamp(
                index,
                hw_frame=self._hw_frame,
                device_ticks=self._hw_frame * self._ticks_per_frame,
                host_time_ns=time.time_ns(),
            )
            self._hw_frame += 1
            writer.publish(1)

    def drop(self, n: int = 1) -> None:
        """Lose ``n`` frames camera-side: they are counted, never delivered."""
        self._hw_frame += n
