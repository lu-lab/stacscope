"""G14-G15 -- the rig's NI source, on the NI MAX simulated PCI-6733 Dev1-sim.

The rig config names the NI source by its two tasks saved in NI MAX, and
nothing else: the channels, their order and the rate are MAX's. So a rig
started from it must read them from MAX, check each device's trigger
against MAX's digital lines, and run the board through the Coordinator.
The default suite cannot reach this path; the simulated board can, with
no output that reaches anything.

The tasks are this suite's own (saved_tasks.py), never the instrument's.
Every analog sample written is 0 V.

    uv run --extra nidaqmx pytest tests/adapters/nidaqmx/test_rig_source.py
"""

import asyncio
import time

import numpy as np
import pytest
from saved_tasks import PREFIX, saved

from stacscope.app import ConfigError, parse_config
from stacscope.app.rig import Rig
from stacscope.core.actors import Accepted, ask
from stacscope.core.ports import Channel, OutputConfig
from stacscope.core.protocol import AcquisitionStarted, SetWaveform, StartAcquisition, Waveform
from stacscope.core.timing import CoordinatorState

DEVICE = "Dev1-sim"
AO_TASK = f"{PREFIX}rig_sim_ao"
DO_TASK = f"{PREFIX}rig_sim_do"
RATE = 100_000.0
CYCLE = 5000  # 50 ms

TASKS = OutputConfig(
    rate=RATE,
    ao=tuple(Channel(f"{DEVICE}/ao{i}", f"ao{i}") for i in range(5)),
    do=(
        Channel(f"{DEVICE}/port0/line0", "HamamatsuCamTrigger"),
        Channel(f"{DEVICE}/port0/line2", "ThorCamTrigger"),
    ),
    buffer_samples=5000,
)


def _unsafe() -> str | None:
    """Why this suite must not run here, or None if it may. As in
    test_dev1.py: the name alone is not enough, the device must also be
    simulated, so nothing here can drive a real board."""
    try:
        import nidaqmx.system

        system = nidaqmx.system.System.local()
        if not any(d.name == DEVICE and d.is_simulated for d in system.devices):
            return f"{DEVICE} is absent or is not simulated (this suite never drives a real board)"
    except Exception as exc:
        return f"no usable NI-DAQmx driver: {exc!r}"
    return None


UNSAFE = _unsafe()
pytestmark = [pytest.mark.skipif(UNSAFE is not None, reason=UNSAFE or ""), pytest.mark.timeout(60)]


@pytest.fixture(scope="module", autouse=True)
def tasks():
    with saved(TASKS, ao=AO_TASK, do=DO_TASK):
        yield


def rig_file(trigger="HamamatsuCamTrigger"):
    return f"""
[source]
kind = "nidaqmx"
ao_task = "{AO_TASK}"
do_task = "{DO_TASK}"
ready_timeout = 5.0
stop_timeout = 10.0

[devices.camera]
kind = "simulated_free_running_camera"
trigger = "{trigger}"
ready_timeout = 5.0
buffer_name = "gn{time.monotonic_ns() % 10**12}"
shape = [4, 8]
"""


async def test_g14_a_rig_with_the_ni_source_reads_its_lines_from_max_and_runs(tmp_path):
    """The channels come from MAX, in MAX's order and with MAX's names,
    which is what a waveform's rows follow. An acquisition then runs through
    the Coordinator to its end. Closing releases the board: DAQmx refuses to
    load a task that is still loaded, so a second rig would not start."""
    rig = await Rig.start(parse_config(rig_file()))
    try:
        assert rig.output == TASKS
        assert rig.line(rig.triggers["camera"]) == 0

        zeros = np.zeros((5, CYCLE)), np.zeros((2, CYCLE), dtype=bool)
        zeros[1][0, :10] = True  # a pulse on the camera's line, reaching nothing
        for name in Waveform:
            assert await ask(rig.coordinator.source, SetWaveform(name, *zeros), timeout=5.0) == Accepted()
        assert await ask(rig.ref, StartAcquisition(4, ""), timeout=10.0) == AcquisitionStarted()
        deadline = time.monotonic() + 10.0
        while rig.coordinator.state is not CoordinatorState.IDLE:
            assert time.monotonic() < deadline, rig.coordinator.state
            await asyncio.sleep(0.02)
    finally:
        await rig.close()

    again = await Rig.start(parse_config(rig_file()))
    await again.close()


async def test_g15_a_trigger_not_among_maxs_lines_refuses_the_start():
    """With the NI source, the digital lines are known only once MAX is
    read, so a trigger naming none of them is refused when the rig starts,
    before any device is launched."""
    with pytest.raises(ConfigError, match="LaserEnable"):
        await Rig.start(parse_config(rig_file(trigger="LaserEnable")))
