"""The property contract (DP15), run against the DcamCamera device over the
fake DCAM library (fake_dcam.py).

The contract's tests are in tests/contract/property_contract.py; this module
supplies only the rig. Its table is the fake camera's properties, described
by the fake's ``dcamprop_getattr``. A smaller subarray moves the camera's
image sizes; the sensor mode may change only while idle, and the exposure
time at any time.

Windows only, like the adapter, and not part of the default run:

    uv run pytest tests/adapters/dcam
"""
# For end use and not for commercial purposes.

import pathlib
import sys
import uuid

import pytest
from fake_dcam import FakeDcam

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))

from property_contract import *  # noqa: E402, F403 - the contract's tests and Rig
from property_contract import Rig  # noqa: E402

from stacscope.adapters.dcam import DcamCamera  # noqa: E402
from stacscope.core.actors import Actor, ask, spawn  # noqa: E402


@pytest.fixture
async def rig():
    fake = FakeDcam()
    parent = Actor()
    spawn(parent, name="coordinator")
    device = DcamCamera(
        "p" + uuid.uuid4().hex[:20], library=fake, n_slots=6, capacity_bytes=1 << 20, max_slots=16
    )
    ref = parent.spawn_child(device, name="camera")
    await device.started.wait()

    async def ask_device(msg):
        return await ask(ref, msg, timeout=2.0)

    yield Rig(
        ask=ask_device,
        moves=("SUBARRAY VSIZE", 12.0, "IMAGE HEIGHT"),
        idle=("SENSOR MODE", "PROGRESSIVE"),
        always=("EXPOSURE TIME", 0.02),
    )
    if not device.stopped.is_set():
        device.stop()
        await device.stopped.wait()
    parent.stop()
    assert fake.misuse == []
