import uuid

import pytest

from stacscope.core.buffer import FrameBuffer

MiB = 1 << 20


@pytest.fixture
def name():
    return "t" + uuid.uuid4().hex[:20]


@pytest.fixture
def make_writer(name):
    """Create a producer-side buffer; closed (and unlinked) after the test."""
    writers = []

    def make(capacity_bytes=4 * MiB, max_slots=16):
        writer = FrameBuffer.create(name, capacity_bytes=capacity_bytes, max_slots=max_slots)
        writers.append(writer)
        return writer

    yield make
    for writer in writers:
        writer.close()


def produce(writer, n=1):
    """Write ``n`` frames, each filled with its own global index, and publish
    them one at a time the way a producer would."""
    for _ in range(n):
        index = writer.published()
        writer.frames[writer.slot_of(index)] = index
        writer.publish(1)
