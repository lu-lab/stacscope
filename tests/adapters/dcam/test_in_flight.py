"""Q10: the in-flight measurement recovers the depth the fake DCAM grabber
was given, exactly.

In-flight depth is how far past the newest frame a grabber reports it may
already be writing. The image-streaming role never publishes a frame older
than ``n_slots - in_flight``, so a depth declared too small lets a consumer
read a slot the grabber is overwriting. DCAM reports only the newest frame
and the count, so the depth has to be measured on the camera (5d, by the
user); this proves the measuring code first, on a grabber whose depth is
known. A tool that could not recover a known depth would make 5d's number
worthless.

Windows only, like the adapter, and not part of the default run:

    uv run pytest tests/adapters/dcam
"""
# For end use and not for commercial purposes.

import uuid

import pytest
from fake_dcam import FakeDcam

from stacscope.adapters.dcam import DcamFrameCapture
from stacscope.adapters.in_flight import measure_in_flight
from stacscope.core.buffer import ImageBuffer
from stacscope.core.devices.image_streaming import layout_for

FRAMES = 12
SCAN = 4
SENTINEL = 0xA5


@pytest.fixture
def segment():
    made = []

    def make(fmt, n_slots):
        layout = layout_for(fmt, n_slots)
        writer = ImageBuffer.create(
            "f" + uuid.uuid4().hex[:20],
            capacity_bytes=layout.resolve().total_bytes,
            max_slots=n_slots,
        )
        writer.configure(layout)
        made.append(writer)
        return writer

    yield make
    for writer in made:
        writer.close()


@pytest.fixture
async def rig(segment):
    """``rig(depth)``: a capture over a fake grabber ``depth`` frames deep,
    slots enough for the measurement, and a trigger. Closed after the test,
    before the segment."""
    opened = []

    def make(depth, **fake_options):
        fake = FakeDcam(in_flight=depth, **fake_options)
        capture = DcamFrameCapture(library=fake, in_flight=1)
        opened.append((capture, fake))
        writer = segment(capture.frame_format(), FRAMES + 2 * SCAN)

        async def fire():
            fake.pulse(1)

        return capture, writer, fire

    yield make
    for capture, fake in opened:
        await capture.close()
        assert fake.misuse == []


@pytest.mark.parametrize("depth", [0, 1, 2])
async def test_q10_the_measured_depth_is_the_grabbers(rig, depth):
    """Exactly, not at least: 0 matters as much as 2, since a tool that
    always saw one frame too many would have every camera declare more
    depth than it has, and one too few would let a slot be read while it
    is being written."""
    capture, writer, fire = rig(depth)

    result = await measure_in_flight(
        capture, writer.slots, frames=FRAMES, fire=fire, sentinel=SENTINEL, scan=SCAN
    )

    assert result.depth == depth
    assert result.frames >= FRAMES


@pytest.mark.parametrize("depth", [0, 1, 2])
async def test_q10_a_frame_begun_and_not_finished_is_counted_as_partial(rig, depth):
    """The grabber writes a frame top to bottom, so a frame past the newest
    with its first row written and its last not is one being written right
    then: the case a consumer must never see. The fake begins a frame with
    its first row."""
    capture, writer, fire = rig(depth)

    result = await measure_in_flight(
        capture, writer.slots, frames=FRAMES, fire=fire, sentinel=SENTINEL, scan=SCAN
    )

    assert (result.partial > 0) == (depth > 0)


async def test_q10_the_capture_is_stopped_and_detached_afterwards(rig):
    """The measurement runs on a camera that goes on to other work, and on
    memory the caller closes."""
    capture, writer, fire = rig(1)

    await measure_in_flight(capture, writer.slots, frames=FRAMES, fire=fire, sentinel=SENTINEL, scan=SCAN)

    # Detached: the same memory attaches again.
    await capture.attach(writer.slots)
    await capture.detach()


async def test_q10_too_few_slots_to_scan_past_the_burst_are_refused(rig, segment):
    """Slots that wrap during the burst would show old frames as frames
    written ahead, and the depth would come out as nonsense."""
    capture, _, fire = rig(0)
    small = segment(capture.frame_format(), FRAMES + SCAN)

    with pytest.raises(ValueError, match="slots"):
        await measure_in_flight(capture, small.slots, frames=FRAMES, fire=fire, sentinel=SENTINEL, scan=SCAN)
