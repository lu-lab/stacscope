"""Shared rig for the buffer monitor tests.

Not a ``conftest.py``: a bare ``from conftest import ...`` binds to whichever
of this suite's conftest files was imported first. See the note in
``tests/integration/process_support.py``.
"""

import uuid

from stacscope.adapters.simulated import TestImageGenerator
from stacscope.core.buffer import ImageBuffer, Layout

SHAPE = (8, 16)
SLOTS = 4
"""Deliberately tiny, so lapping the ring takes four frames rather than a wait."""

LAYOUT = Layout(shape=SHAPE, dtype="<u2", n_slots=SLOTS)


def fresh_name() -> str:
    return "mon" + uuid.uuid4().hex[:16]


class Rig:
    """A generator writing a moving bar into a segment of its own."""

    def __init__(self, layout: Layout = LAYOUT):
        self.name = fresh_name()
        self.writer = ImageBuffer.create(self.name, capacity_bytes=1 << 16, max_slots=16)
        self.generator = TestImageGenerator(self.writer, frame_period=0.01)
        self.generator.configure(layout)
        self.generator.start_acquisition()

    def emit(self, n: int = 1) -> None:
        self.generator.emit(n)

    def reconfigure(self, layout: Layout) -> None:
        """A new layout mid-life, as a device reconfiguring its camera would."""
        self.generator.configure(layout)
        self.generator.start_acquisition()

    def close(self) -> None:
        self.writer.close()
