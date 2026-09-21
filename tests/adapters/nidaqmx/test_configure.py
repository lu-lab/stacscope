"""N1, N2 -- what `configure` builds, and the two settings everything rests on.

These run only when asked for by name: the directory is ignored by the default
suite, because it needs the `nidaqmx` extra installed. See pyproject.

    uv run --extra nidaqmx pytest tests/adapters/nidaqmx
"""

import pytest
from fake_daqmx import FakeDaq
from nidaqmx.constants import AcquisitionType, LineGrouping, RegenerationMode

from stacscope.adapters.nidaqmx import NiSignalOutput
from stacscope.adapters.simulated import VirtualClock
from stacscope.core.ports import Channel, OutputConfig

RATE = 100_000.0
BUFFER = 8192

CONFIG = OutputConfig(
    rate=RATE,
    ao=(Channel("Dev1/ao0", "etl"), Channel("Dev1/ao1", "scan1")),
    do=(Channel("Dev1/port0/line0", "camera"), Channel("Dev1/port0/line1", "laser")),
    buffer_samples=BUFFER,
)


@pytest.fixture
def daq():
    return FakeDaq()


@pytest.fixture
async def configured(daq):
    output = NiSignalOutput(VirtualClock(), tasks=daq)
    await output.configure(CONFIG)
    return output


async def test_configure_builds_an_ao_task_and_a_do_task(configured, daq):
    """N1. One channel per configured Channel, in the order given: the labels
    are for humans, but the order is what the caller's array columns mean."""
    ao, do = daq.task("ao"), daq.task("do")

    assert ao.ao_channels.added == ["Dev1/ao0", "Dev1/ao1"]
    assert do.do_channels.added == ["Dev1/port0/line0", "Dev1/port0/line1"]
    assert do.do_channels.grouping is LineGrouping.CHAN_PER_LINE

    assert ao.timing.rate == RATE
    assert do.timing.rate == RATE
    assert ao.timing.sample_mode is AcquisitionType.CONTINUOUS
    assert do.timing.sample_mode is AcquisitionType.CONTINUOUS
    assert ao.out_stream.output_buf_size == BUFFER
    assert do.out_stream.output_buf_size == BUFFER


async def test_the_do_task_is_clocked_by_the_ao_task_and_neither_regenerates(configured, daq):
    """N2. The whole refill design rests on these two settings.

    Regeneration disabled is what makes a sample play exactly once, so falling
    behind is an error rather than a silently repeated block -- see the port.
    And the digital lines take their clock from the AO task's, so a trigger
    edge keeps its phase against the waveform instead of drifting on a second
    oscillator. Getting either wrong produces output that looks plausible and
    is wrong, which is why they are asserted rather than assumed."""
    ao, do = daq.task("ao"), daq.task("do")

    assert ao.out_stream.regen_mode is RegenerationMode.DONT_ALLOW_REGENERATION
    assert do.out_stream.regen_mode is RegenerationMode.DONT_ALLOW_REGENERATION

    assert ao.timing.source == ""  # its own onboard clock: the timing authority
    assert do.timing.source == "/Dev1/ao/SampleClock"
