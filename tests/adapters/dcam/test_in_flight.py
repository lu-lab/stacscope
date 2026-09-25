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

from stacscope.adapters.dcam import DcamError, DcamFrameCapture, _api
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


class _Lagging:
    """The capture, with frames that finish after the tool has read the
    count and before it scans: what a camera at hundreds of frames a second
    does to a tool that is not fast enough to see each frame alone. After
    each trigger, the first count read is followed by ``extra`` more pulses
    before the tool can look; the grabber finishes frames as it always does,
    leaving its own depth in flight."""

    def __init__(self, capture, fake, extra):
        self._capture = capture
        self._fake = fake
        self._extra = extra
        self.fired = False

    def __getattr__(self, name):
        return getattr(self._capture, name)

    async def wait_frames(self, seen, timeout):
        delivered = await self._capture.wait_frames(seen, timeout)
        if self.fired:
            self.fired = False
            self._fake.pulse(self._extra)
        return delivered


@pytest.mark.parametrize("depth", [0, 1])
async def test_q10_frames_that_finish_while_the_tool_looks_are_not_depth(rig, depth):
    """A frame that finished while the tool was scanning was in flight when
    the count was read, but it was finished and would have been reported by
    the time the scan saw it: the tool's own lag, not the grabber's depth. At
    800 frames a second a scan of a few milliseconds spans several frames,
    and a tool counting them would have the camera declare a depth it does
    not have, and the role hold back frames it could publish."""
    capture, writer, _ = rig(depth)
    lagging = _Lagging(capture, capture._lib, extra=2)

    async def fire():
        lagging.fired = True
        capture._lib.pulse(1)

    result = await measure_in_flight(
        lagging, writer.slots, frames=FRAMES, fire=fire, sentinel=SENTINEL, scan=SCAN
    )

    assert result.depth == depth


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


@pytest.mark.parametrize("failure", ["start refused", "trigger fails"])
async def test_q10_a_failed_measurement_does_not_hold_the_segment_open(rig, failure):
    """The measurement is run from a script (5d), which closes its segment
    in a ``finally``. Its error's traceback keeps the measurement's own
    arguments alive, the slots among them, and a segment cannot close while
    a view of it is alive: the script would get ``BufferError`` in place of
    the camera's error, and a segment left mapped. A failure after the
    attach is the case here; the capture's own refusals are the contract's
    K9."""
    capture, writer, fire = rig(1)
    if failure == "start refused":
        capture._lib.fail("dcamcap_start", _api.ERR_NOTREADY)
    else:

        async def fire():
            raise RuntimeError("the trigger line is not connected")

    with pytest.raises((DcamError, RuntimeError)) as caught:
        await measure_in_flight(capture, writer.slots, frames=FRAMES, fire=fire, sentinel=SENTINEL, scan=SCAN)

    writer.close()  # BufferError if the error still reaches the slots
    assert caught.value is not None  # still held: that is the case under test
