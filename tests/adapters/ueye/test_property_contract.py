"""The property contract (DP15), run against the UEyeCamera device over the
fake pyueye (fake_ueye.py).

The contract's tests are in tests/contract/property_contract.py; this module
supplies only the rig. A narrower AOI leaves room to move it right, so its
width moves the most its x may be; the pixel format may change only while
idle, and the exposure at any time.

Not part of the default run:

    uv run pytest tests/adapters/ueye
"""

import pathlib
import sys
import uuid

import pytest
from fake_ueye import FakeUEye

sys.path.insert(0, str(pathlib.Path(__file__).parents[2] / "contract"))

from property_contract import *  # noqa: E402, F403 - the contract's tests and Rig
from property_contract import Rig  # noqa: E402

from stacscope.adapters.ueye import UEyeCamera  # noqa: E402
from stacscope.core.actors import Actor, ask, spawn  # noqa: E402


@pytest.fixture
async def rig():
    fake = FakeUEye()
    parent = Actor()
    spawn(parent, name="coordinator")
    device = UEyeCamera(
        "p" + uuid.uuid4().hex[:20],
        ueye=fake,
        n_slots=6,
        capacity_bytes=1 << 20,
        max_slots=16,
        settings=[("aoi_width", 64.0), ("aoi_height", 32.0)],
    )
    ref = parent.spawn_child(device, name="camera")
    await device.started.wait()

    async def ask_device(msg):
        return await ask(ref, msg, timeout=2.0)

    yield Rig(
        ask=ask_device,
        moves=("aoi_width", 32.0, "aoi_x"),
        idle=("pixel_format", "RAW10"),
        always=("exposure", 0.002),
    )
    if not device.stopped.is_set():
        device.stop()
        await device.stopped.wait()
    parent.stop()
    assert fake.misuse == []
