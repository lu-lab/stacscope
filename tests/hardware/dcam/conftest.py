"""Shared by the camera suite: the guard that keeps it off machines without
the camera, and a summary of what each test measured, printed at the end of
the run so the numbers can be read (and pasted) without -s."""
# For end use and not for commercial purposes.

import os
import pathlib
import sys

import pytest

# The port contract's tests, which test_contract.py runs on the camera, and
# the DCIMG reader, which test_recording.py reads its files back with.
sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))
sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "adapters" / "dcam"))

CAMERA = os.environ.get("STACSCOPE_CAMERA", "")
"""The DCAM index of the camera under test. The suite skips without it."""

FINDINGS: list[str] = []

HERE = pathlib.Path(__file__).parent


def pytest_collection_modifyitems(items):
    if CAMERA.isdigit():
        return
    skip = pytest.mark.skip(reason="set STACSCOPE_CAMERA to the DCAM index of the camera to test")
    for item in items:
        # The hook sees the whole session's items, not only this directory's.
        if item.path.is_relative_to(HERE):
            item.add_marker(skip)


@pytest.fixture
def camera() -> int:
    return int(CAMERA)


@pytest.fixture
def report(request):
    """Record one line of what this test measured, for the run's summary."""

    def add(line: str) -> None:
        FINDINGS.append(f"{request.node.name}: {line}")

    return add


def pytest_terminal_summary(terminalreporter):
    if FINDINGS:
        terminalreporter.section("camera findings")
        for line in FINDINGS:
            terminalreporter.write_line(line)
