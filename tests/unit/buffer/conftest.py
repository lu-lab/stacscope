"""Fixtures for the buffer tests.

The shared ``produce`` helper lives in ``buffer_support.py``, not here -- see
the note at the top of that file.
"""

import uuid

import pytest

from stacscope.core.buffer import ImageBuffer

MiB = 1 << 20


@pytest.fixture
def name():
    return "t" + uuid.uuid4().hex[:20]


@pytest.fixture
def make_writer(name):
    """Create a producer-side buffer; closed (and unlinked) after the test."""
    writers = []

    def make(capacity_bytes=4 * MiB, max_slots=16):
        writer = ImageBuffer.create(name, capacity_bytes=capacity_bytes, max_slots=max_slots)
        writers.append(writer)
        return writer

    yield make
    for writer in writers:
        writer.close()
