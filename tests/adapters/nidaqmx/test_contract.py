"""The SignalOutput port contract, run against NiSignalOutput on the simulated
PCI-6733 named Dev1-sim, loading tasks this module saves to NI MAX itself.

The contract's tests are in tests/contract/signal_output_contract.py; this
module supplies only the rig. It is the check that the NI adapter can stand
in for any other: the same tests the simulated adapter passes in virtual
time, here on a real clock and a real driver, with the slack real hardware
needs (see SLACK).

A simulated board stalls when it runs out instead of reporting it, so the
running-dry test skips here. The physical board runs it:
tests/hardware/nidaqmx/test_contract.py.

    uv run --extra nidaqmx pytest tests/adapters/nidaqmx
"""

import asyncio
import pathlib
import sys

import pytest
from saved_tasks import PREFIX, saved

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))

from signal_output_contract import *  # noqa: E402, F403 - the contract's tests, and Rig
from test_dev1 import UNSAFE  # noqa: E402 - the same guard: Dev1-sim, and simulated

from stacscope.adapters.nidaqmx import NiSignalOutput  # noqa: E402
from stacscope.core.ports import Channel, MonotonicClock, OutputConfig  # noqa: E402

DEVICE = "Dev1-sim"
AO_TASK = f"{PREFIX}contract_ao"
DO_TASK = f"{PREFIX}contract_do"

CONFIG = OutputConfig(
    rate=100_000.0,
    ao=tuple(Channel(f"{DEVICE}/ao{i}", f"analog{i}") for i in range(5)),
    do=tuple(Channel(f"{DEVICE}/port0/line{i}", f"trigger{i}") for i in range(3)),
    buffer_samples=100_000,
)
"""The instrument's shape, with a one-second buffer: long enough that the
contract's fractions of it are far larger than SLACK."""

SLACK = 8_000
"""80 ms of samples. The onboard FIFOs take up to 3276 (analog) and 2047
(digital) samples out of the host buffer ahead of the clock, the driver moves
them in blocks of about 2050, and a sleep on Windows wakes up to 16 ms late."""

pytestmark = pytest.mark.skipif(UNSAFE is not None, reason=UNSAFE or "")


@pytest.fixture(scope="module", autouse=True)
def tasks():
    with saved(CONFIG, ao=AO_TASK, do=DO_TASK):
        yield


@pytest.fixture
async def rig():
    """The adapter, stopped and closed afterwards whatever the test did: a
    task left loaded makes the next test fail to load it."""
    output = NiSignalOutput(MonotonicClock(), ao_task=AO_TASK, do_task=DO_TASK)
    yield Rig(output, CONFIG, asyncio.sleep, slack=SLACK, runs_dry=False)
    await output.stop()
    await output.close()
