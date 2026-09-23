"""Shared by the physical-board suite: the saved-task helper, and a summary of
what each test measured, printed at the end of the run so the numbers can be
read (and pasted) without -s."""

import pathlib
import sys

import pytest

# The helper that saves test-owned tasks to NI MAX lives with the simulated
# suite; both suites must build their tasks the same way.
sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "adapters" / "nidaqmx"))

FINDINGS: list[str] = []


@pytest.fixture
def report(request):
    """Record one line of what this test measured, for the run's summary."""

    def add(line: str) -> None:
        FINDINGS.append(f"{request.node.name}: {line}")

    return add


def pytest_terminal_summary(terminalreporter):
    if FINDINGS:
        terminalreporter.section("hardware findings")
        for line in FINDINGS:
            terminalreporter.write_line(line)
