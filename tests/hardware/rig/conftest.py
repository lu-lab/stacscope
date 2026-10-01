"""Shared by the rig suite: the guard that keeps it off machines without the
instrument's hardware, and a summary of what each test measured, printed at
the end of the run so the numbers can be read (and pasted) without -s."""

import os
import pathlib
import sys

import pytest

# The NI MAX task helper the device suites use, and the DCIMG reader.
sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "adapters" / "nidaqmx"))
sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "adapters" / "dcam"))

CONFIG = os.environ.get("STACSCOPE_RIG_CONFIG", "")
"""The rig config to run (config/rig.toml on the instrument's machine). The
suite skips without it."""

FINDINGS: list[str] = []

HERE = pathlib.Path(__file__).parent


def pytest_collection_modifyitems(items):
    if CONFIG:
        return
    skip = pytest.mark.skip(reason="set STACSCOPE_RIG_CONFIG to the rig config to run")
    for item in items:
        # The hook sees the whole session's items, not only this directory's.
        if item.path.is_relative_to(HERE):
            item.add_marker(skip)


@pytest.fixture
def config_path() -> pathlib.Path:
    return pathlib.Path(CONFIG)


@pytest.fixture
def report(request):
    """Record one line of what this test measured, for the run's summary."""

    def add(line: str) -> None:
        FINDINGS.append(f"{request.node.name}: {line}")

    return add


def pytest_terminal_summary(terminalreporter):
    if FINDINGS:
        terminalreporter.section("rig findings")
        for line in FINDINGS:
            terminalreporter.write_line(line)
