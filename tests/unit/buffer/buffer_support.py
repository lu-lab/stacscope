"""Shared helper for the buffer tests.

Kept out of ``conftest.py`` because a bare ``from conftest import ...`` binds to
whichever of this suite's several ``conftest.py`` files was imported first, so
it depends on how pytest was invoked. See the note in
``tests/integration/process_support.py``.

It matters twice over here: ``test_lifetime`` imports this by name inside a
child process it launches with ``cwd`` set to this directory.
"""


def produce(writer, n=1):
    """Write ``n`` frames, each filled with its own global index, and publish
    them one at a time the way a producer would."""
    for _ in range(n):
        index = writer.published()
        writer.frames[writer.slot_of(index)] = index
        writer.publish(1)
