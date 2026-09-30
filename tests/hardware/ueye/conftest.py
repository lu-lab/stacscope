"""Shared by the uEye suite: the guard that keeps it off machines without the
camera, and a summary of what each test measured, printed at the end of the
run so the numbers can be read (and pasted) without -s."""

import os
import pathlib
import sys

import pytest

# The DAQ's pulse train, shared with the DCAM suite.
sys.path.insert(0, str(pathlib.Path(__file__).parents[1]))

CAMERA = os.environ.get("STACSCOPE_UEYE", "")
"""The uEye camera ID of the camera under test. The suite skips without it."""

TRIGGER = os.environ.get("STACSCOPE_UEYE_TRIGGER", "")
"""The DAQ line wired to the uEye's trigger input (Dev1/port0/line2 on the
instrument). The DAQ-triggered tests skip without it."""

FINDINGS: list[str] = []

HERE = pathlib.Path(__file__).parent


def pytest_collection_modifyitems(items):
    if CAMERA.isdigit():
        return
    skip = pytest.mark.skip(reason="set STACSCOPE_UEYE to the uEye camera ID of the camera to test")
    for item in items:
        # The hook sees the whole session's items, not only this directory's.
        if item.path.is_relative_to(HERE):
            item.add_marker(skip)


@pytest.fixture
def camera() -> int:
    return int(CAMERA)


@pytest.fixture
def trigger_line() -> str:
    if not TRIGGER:
        pytest.skip("set STACSCOPE_UEYE_TRIGGER to the DAQ line wired to the uEye's trigger input")
    return TRIGGER


@pytest.fixture
def report(request):
    """Record one line of what this test measured, for the run's summary."""

    def add(line: str) -> None:
        FINDINGS.append(f"{request.node.name}: {line}")

    return add


def pytest_terminal_summary(terminalreporter):
    if FINDINGS:
        terminalreporter.section("uEye findings")
        for line in FINDINGS:
            terminalreporter.write_line(line)
