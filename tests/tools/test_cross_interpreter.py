"""V13 -- the monitor and the engine on different interpreters, one machine.

PySide6 ships only ``abi3`` wheels, so the monitor's window cannot run on the
free-threaded build, while the engine does. The monitor is therefore meant to
run on standard CPython beside a free-threaded generator, reading the same
segment. That works only because an image buffer is shared memory and a monitor
is a pure consumer: no link, no protocol, nothing to agree on but a name.

Not in the default suite, for two reasons. It needs both interpreters, which
is a property of a developer's machine rather than of the code, and every CI
leg installs exactly one. And it builds an environment of its own, which is
slow and must be kept well away from the one running the tests -- an earlier
version of this test re-synced the session's own virtualenv underneath it and
made two unrelated tests fail.

    uv run pytest tests/tools -q
"""

import json
import os
import subprocess
import sys
import textwrap
import uuid

import pytest

from stacscope.adapters.simulated import TestImageGenerator
from stacscope.core.buffer import ImageBuffer, Layout
from stacscope.monitor import BufferMonitor

SHAPE = (8, 16)
SLOTS = 4
OTHER = "3.14" if not sys._is_gil_enabled() else "3.14t"
"""Whichever build this run is not. The pairing is the point, so it does not
matter which side we start from."""

READER = textwrap.dedent("""
    import json, sys
    from stacscope.monitor import BufferMonitor

    monitor = BufferMonitor({name!r})
    seen = []
    for slot in range({slots}):
        monitor.seek(slot)
        view = monitor.refresh()
        seen.append(None if view is None else [view.slot, view.index, int(view.image.sum())])
    print(json.dumps({{
        "gil": sys._is_gil_enabled(),
        "slots": monitor.slots(),
        "shape": list(monitor.header()["shape"]),
        "seen": seen,
    }}))
    monitor.close()
""")


def _has_other() -> bool:
    found = subprocess.run(["uv", "python", "find", OTHER], capture_output=True, text=True)
    return found.returncode == 0


@pytest.mark.skipif(not _has_other(), reason=f"no {OTHER} interpreter on this machine")
def test_the_other_interpreter_reads_this_ones_buffer(tmp_path):
    name = "cross" + uuid.uuid4().hex[:12]
    writer = ImageBuffer.create(name, capacity_bytes=1 << 16, max_slots=16)
    try:
        generator = TestImageGenerator(writer, frame_period=0.01)
        generator.configure(Layout(shape=SHAPE, dtype="<u2", n_slots=SLOTS))
        generator.start_acquisition()
        generator.emit(SLOTS)  # a frame in every slot, so every read has something

        here = BufferMonitor(name)
        expected = []
        for slot in range(SLOTS):
            here.seek(slot)
            view = here.refresh()
            expected.append([view.slot, view.index, int(view.image.sum())])
        here.close()

        # An environment of its own, well away from the one running this test.
        env = {
            **os.environ,
            "UV_PROJECT_ENVIRONMENT": str(tmp_path / "env"),
            "VIRTUAL_ENV": "",
        }
        result = subprocess.run(
            ["uv", "run", "--python", OTHER, "python", "-c",
             READER.format(name=name, slots=SLOTS)],
            capture_output=True, text=True, timeout=600, env=env,
        )
        assert result.returncode == 0, result.stderr
        report = json.loads(result.stdout.strip().splitlines()[-1])

        assert report["gil"] is not sys._is_gil_enabled()  # or this proves nothing
        assert report["slots"] == SLOTS
        assert tuple(report["shape"]) == SHAPE
        assert report["seen"] == expected  # the same pixels, slot for slot
    finally:
        writer.close()
