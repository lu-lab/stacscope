"""The SignalOutput port contract, run against NiSignalOutput on the board
named in STACSCOPE_HARDWARE.

RUN BY THE USER ONLY, like the rest of this directory: it starts real output
on the board, though every sample is 0 V and low.

The contract's tests are in tests/contract/signal_output_contract.py; this
module supplies only the rig. On a physical board it adds the one test a
simulated board cannot answer: that running dry mid-stream surfaces as
Underflow on the next call. Pointed at a simulated device, that test skips.

    PowerShell:  $env:STACSCOPE_HARDWARE = "Dev1"
                 uv run --extra nidaqmx pytest tests/hardware/nidaqmx/test_contract.py
                 Remove-Item Env:STACSCOPE_HARDWARE

Takes about fifteen seconds.
"""

import asyncio
import pathlib
import sys

import pytest
from saved_tasks import PREFIX, saved

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))

from signal_output_contract import *  # noqa: E402, F403 - the contract's tests, and Rig
from test_board import DEVICE, SIMULATED, UNSAFE  # noqa: E402 - the same guard

from stacscope.adapters.nidaqmx import NiSignalOutput  # noqa: E402
from stacscope.core.ports import Channel, MonotonicClock, OutputConfig  # noqa: E402

AO_TASK = f"{PREFIX}hw_contract_ao"
DO_TASK = f"{PREFIX}hw_contract_do"

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
    yield Rig(output, CONFIG, asyncio.sleep, slack=SLACK, runs_dry=not SIMULATED)
    await output.stop()
    await output.close()
