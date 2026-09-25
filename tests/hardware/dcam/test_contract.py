"""K1-K9 -- the FrameCapture port contract, run against DcamFrameCapture on
the camera whose DCAM index is in STACSCOPE_CAMERA.

RUN ON THE CAMERA'S MACHINE. This captures: the camera exposes and the
grabber writes frames into shared memory. Nothing else is driven, and no
light source is needed; frames may be dark. The camera is triggered in
software, so nothing need be wired to its trigger input. Every setting a
test changes (trigger source, exposure, sensor mode, subarray) is put back
afterwards.

The contract's tests are in tests/contract/frame_capture_contract.py; this
module supplies only the rig, in three frame formats: full frame, the
256-row light-sheet ROI in area readout, and the same ROI in lightsheet
(PROGRESSIVE) readout. The one contract test a camera cannot be made to
fail on demand, a lost frame, skips.

    PowerShell:  $env:STACSCOPE_CAMERA = "0"
                 uv run pytest tests/hardware/dcam/test_contract.py
                 Remove-Item Env:STACSCOPE_CAMERA

    Git Bash:    STACSCOPE_CAMERA=0 uv run pytest tests/hardware/dcam/test_contract.py

Takes a few minutes: every test opens DCAM afresh, which takes a second or
two.
"""
# For end use and not for commercial purposes.

import asyncio

import pytest
from dcam_rig import FORMATS, GRACE, SOFTWARE, camera_with, software_trigger

from frame_capture_contract import *  # noqa: F403 - the contract's tests, Rig, buffers
from frame_capture_contract import Rig

pytestmark = pytest.mark.timeout(120)
# A timeout ends the whole process on Windows, with the camera open, so it is
# a last resort: every wait in the contract has a deadline of its own.


@pytest.fixture(params=FORMATS.values(), ids=FORMATS.keys())
async def rig(request, camera):
    async with camera_with(camera, SOFTWARE + request.param) as capture:

        async def elapse(seconds: float) -> None:
            await asyncio.sleep(seconds + GRACE)

        yield Rig(capture, software_trigger(capture), elapse)
