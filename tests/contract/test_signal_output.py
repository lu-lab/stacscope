"""The SignalOutput port contract (signal_output_contract.py), run against the
simulated adapter in virtual time, where every count and time is exact."""

import pytest
from signal_output_contract import *  # noqa: F403 - the contract's tests, and Rig

from stacscope.adapters.simulated import SimulatedSignalOutput, VirtualClock
from stacscope.core.ports import Channel, OutputConfig

RATE = 8.0  # samples per second: binary-exact virtual times
CONFIG = OutputConfig(
    rate=RATE,
    ao=(Channel("Dev1/ao0", "etl"),),
    do=(Channel("Dev1/port0/line0", "camera_trigger"),),
    buffer_samples=16,
)


@pytest.fixture(params=[SimulatedSignalOutput], ids=lambda cls: cls.__name__)
def rig(request):
    clock = VirtualClock()
    return Rig(request.param(clock), CONFIG, clock.advance)
