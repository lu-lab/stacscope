"""ImageStreaming role contract (F1-F6). Every capture-backed image stream
must pass this suite.

The role owns the buffer: each acquisition's layout, publishing, and the
order in which the hardware and the segment are released. These tests are
written from a consumer's side of the buffer -- what a reader in another
process can see -- plus the one thing only the producer's side can show,
which is whether the hardware still holds the frame memory: a view it kept
makes the segment's close raise ``BufferError``.

The tests live here, in a module pytest does not collect, and each stream's
suite imports them beside a ``rig`` fixture of its own (see ``Rig``):

    from image_streaming_contract import *  # the tests, Rig, and the consumer fixture

    @pytest.fixture
    async def rig():
        ...

The simulated camera's suite is ``test_image_streaming_role.py``, beside
this file, in virtual time. For now the suite is run against capture-backed
streams only (user, 2026-09-24); a computational producer's rig comes when
one is built for real.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

import numpy as np
import pytest

from stacscope.core.actors import Actor, ActorRef, Fault, ask
from stacscope.core.buffer import ImageBuffer, MetaField
from stacscope.core.devices import ImageStreaming
from stacscope.core.protocol import DeviceReady, DeviceStopped, StartDevice, StopDevice

__all__ = [
    "Listener",
    "Rig",
    "consumer",
    "test_a_consumer_opened_at_ready_holds_the_layout_before_the_first_frame",
    "test_a_new_setting_reaches_a_consumer_as_a_new_layout",
    "test_a_publisher_that_falls_behind_publishes_nothing_it_may_have_lost",
    "test_death_mid_acquisition_lets_go_of_the_frame_memory_before_the_segment_closes",
    "test_disarm_gives_the_frame_memory_back",
    "test_each_acquisition_starts_again_at_slot_0",
    "test_every_frame_is_stamped_before_it_is_published",
    "test_frames_are_published_in_order_one_per_frame_delivered",
    "test_lost_frames_are_a_fatal_fault_and_end_publishing",
    "test_nothing_is_published_after_stop_and_the_frames_stay_readable",
    "test_nothing_is_published_before_a_frame_is_delivered",
    "test_stop_publishes_every_frame_delivered_before_it",
    "test_the_header_declares_the_stamps_the_hardware_gives",
]


class Listener(Actor):
    """Subscribed to the device: what it published, in order."""

    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)


@dataclass
class Rig:
    """A stream under test: the device, spawned in virtual or real time and
    past its setup, with ``listener`` subscribed to it, and the means to drive
    the hardware behind it.

    ``trigger(n)`` sends ``n`` pulses and returns once the device has had the
    chance to publish every frame they caused. ``elapse`` lets time pass.
    ``burst(n)``, if the rig can do it, makes ``n`` frames arrive at once,
    before the device can run at all -- a publisher stalled for that long.
    ``lose(n)`` makes the hardware expose ``n`` frames it never delivers, and
    ``reformat()`` changes a setting so that the next acquisition's frame
    format differs. ``image(i)``, if the rig knows what the hardware images,
    is frame ``i`` of an acquisition as a consumer should read it. Each of
    these is optional; a test that needs one the rig lacks is skipped.
    """

    device: ImageStreaming
    ref: ActorRef
    buffer_name: str
    trigger: Callable[[int], Awaitable[None]]
    elapse: Callable[[float], Awaitable[None]]
    ask: Callable[[object], Awaitable[object]]
    listener: Listener = field(default_factory=Listener)
    burst: Callable[[int], None] | None = None
    lose: Callable[[int], None] | None = None
    reformat: Callable[[], None] | None = None
    image: Callable[[int], np.ndarray] | None = None

    def faults(self) -> list[Fault]:
        return [m for m in self.listener.heard if isinstance(m, Fault)]


@pytest.fixture
def consumer(rig):
    """``consumer()`` opens a reader on the rig's buffer, as a viewer in
    another process would; each is closed after the test. Drop any frame
    taken from one before the test ends: a reader cannot close under its own
    views."""
    readers = []

    def open_reader():
        readers.append(ImageBuffer.open(rig.buffer_name))
        return readers[-1]

    yield open_reader
    for reader in readers:
        reader.close()


def _need(rig, what: str):
    if getattr(rig, what) is None:
        pytest.skip(f"this rig cannot {what}")
    return getattr(rig, what)


async def _start(rig) -> None:
    assert await rig.ask(StartDevice("", -1)) == DeviceReady()


async def _stop(rig) -> None:
    assert await rig.ask(StopDevice()) == DeviceStopped()


def _this_acquisition(reader) -> int:
    """Frames published in the acquisition the reader is following."""
    return reader.published() - reader.acquisition_start


def _frames(reader, n: int) -> list:
    """The first ``n`` frames of the reader's current acquisition."""
    cursor = reader.cursor(reader.acquisition_start)
    return [cursor.next() for _ in range(n)]


def _slots_and_in_flight(reader) -> tuple[int, int]:
    return reader.frames.shape[0], reader._layout.in_flight


def _room_for(reader, n: int) -> None:
    """Fail on a rig whose ring cannot keep ``n`` frames readable at once:
    the slots the hardware may be writing into are not."""
    n_slots, in_flight = _slots_and_in_flight(reader)
    assert n_slots - in_flight >= n, f"the rig needs room for {n} readable frames"


# --- F1: each arm declares the layout before capture starts ---------------------


async def test_a_consumer_opened_at_ready_holds_the_layout_before_the_first_frame(
    rig, consumer
):
    """A consumer that opens the buffer as soon as the device is ready must
    already read the layout the hardware is about to write, and the new
    acquisition; the first frame then needs no catching up. A layout declared
    after capture started would leave a window in which a frame lands in
    memory the header describes as something else."""
    await _start(rig)
    await _stop(rig)
    await _start(rig)
    reader = consumer()
    assert reader.acquisition_id == 2
    assert _this_acquisition(reader) == 0

    await rig.trigger(1)

    assert not reader.sync(), "nothing changed between ready and the first frame"
    assert _this_acquisition(reader) == 1
    if rig.image is not None:
        (frame,) = _frames(reader, 1)
        assert np.array_equal(frame.data, rig.image(0)), "read through the declared layout"
        del frame


async def test_a_new_setting_reaches_a_consumer_as_a_new_layout(rig, consumer):
    """F1 and F5. A setting changed between acquisitions changes what the
    hardware writes. A consumer that kept the segment open learns it from
    the header alone, and reads the new frames correctly through it. The
    role must have taken the frame memory back from the hardware before
    laying the segment out again, or the new attach is refused, or the
    hardware writes the old geometry into the new layout."""
    reformat = _need(rig, "reformat")
    await _start(rig)
    await rig.trigger(1)
    reader = consumer()
    before = reader.frames.shape
    await _stop(rig)

    reformat()
    await _start(rig)
    await rig.trigger(1)

    assert reader.sync(), "the consumer sees a new layout"
    assert reader.frames.shape != before
    assert _this_acquisition(reader) == 1
    if rig.image is not None:
        (frame,) = _frames(reader, 1)
        assert np.array_equal(frame.data, rig.image(0))
        del frame


# --- F2: in order, from slot 0, one per frame delivered --------------------------


async def test_frames_are_published_in_order_one_per_frame_delivered(rig, consumer):
    """Frame ``i`` of an acquisition is the ``i``-th frame the hardware
    delivered, and there is one per delivered frame: a consumer that counts
    frames counts triggers, as D12 relies on."""
    await _start(rig)
    reader = consumer()
    _room_for(reader, 3)

    await rig.trigger(3)

    assert _this_acquisition(reader) == 3
    frames = _frames(reader, 3)
    assert [f.index - reader.acquisition_start for f in frames] == [0, 1, 2]
    if rig.image is not None:
        for i, frame in enumerate(frames):
            assert np.array_equal(frame.data, rig.image(i)), f"frame {i}"
    del frames


async def test_each_acquisition_starts_again_at_slot_0(rig, consumer):
    """The hardware writes each acquisition from slot 0 (K2), so the role
    must start the buffer's acquisition there too. Otherwise a consumer reads
    frame 0 from whichever slot the previous acquisition would have used
    next -- a stale image under a new frame number."""
    image = _need(rig, "image")
    await _start(rig)
    await rig.trigger(3)
    await _stop(rig)

    await _start(rig)
    await rig.trigger(1)

    reader = consumer()
    (frame,) = _frames(reader, 1)
    assert np.array_equal(frame.data, image(0))
    assert np.array_equal(reader.frames[0], image(0)), "and it is in slot 0"
    del frame


async def test_nothing_is_published_before_a_frame_is_delivered(rig, consumer):
    """A frame published before the hardware has delivered it is read while
    the DMA is still writing it, or before it has begun."""
    await _start(rig)
    reader = consumer()

    await rig.elapse(5.0)

    assert _this_acquisition(reader) == 0


# --- F3: stamped before published, and declared --------------------------------------


async def test_the_header_declares_the_stamps_the_hardware_gives(rig, consumer):
    """A consumer outside Python learns from the header alone which per-frame
    fields mean something and what clock device time counts; those are what
    the hardware says, not what the role assumes."""
    await _start(rig)
    reader = consumer()

    assert reader.metadata == rig.device.capture.metadata()


async def test_every_frame_is_stamped_before_it_is_published(rig, consumer):
    """Publishing is what tells a consumer a frame is there to read, meta
    and all. A frame whose stamps were written after the counter moved is
    one a fast consumer reads with the previous occupant's stamps. Checked
    at the moment of publishing, since afterwards the two orders look the
    same."""
    await rig.elapse(1.0)  # so device time is not zero at the first frame
    await _start(rig)
    reader = consumer()
    writer = rig.device.writer
    at_publish = []
    publish = writer.publish

    def watching(n=1):
        first = writer.published()
        for index in range(first, first + n):
            slot = reader.slot_of(index)
            at_publish.append(reader.meta[[slot]].copy()[0])
        publish(n)

    writer.publish = watching
    await rig.trigger(3)

    assert len(at_publish) == 3
    stamps = rig.device.capture.stamps(0, 3)
    fields = reader.metadata.fields
    for name, flag in [
        ("hw_frame", MetaField.HW_FRAME),
        ("device_ticks", MetaField.DEVICE_TIME),
        ("host_time_ns", MetaField.HOST_TIME),
    ]:
        if flag in fields:
            assert [int(m[name]) for m in at_publish] == [int(s) for s in stamps[name]], name


# --- F4: stop publishes everything delivered, and the buffer stays ---------------------


async def test_stop_publishes_every_frame_delivered_before_it(rig, consumer):
    """Frames that arrived just before the stop, before the role could
    publish them, are the last planes of the stack. Dropping them at stop
    loses data the hardware delivered."""
    burst = _need(rig, "burst")
    await _start(rig)
    await rig.trigger(1)

    burst(2)
    await _stop(rig)

    reader = consumer()
    assert _this_acquisition(reader) == 3


async def test_nothing_is_published_after_stop_and_the_frames_stay_readable(rig, consumer):
    """D11, for a stream with hardware under it: pulses after the stop (a
    COOLDOWN still playing) publish nothing, and the last frames can still
    be read, because nothing says a consumer has finished when the producer
    has."""
    await _start(rig)
    await rig.trigger(3)
    await _stop(rig)

    await rig.trigger(2)

    reader = consumer()
    _room_for(reader, 3)
    assert _this_acquisition(reader) == 3
    if rig.image is not None:
        frames = _frames(reader, 3)
        assert np.array_equal(frames[-1].data, rig.image(2))
        del frames


# --- F5: the hardware lets go first -------------------------------------------------------


async def test_disarm_gives_the_frame_memory_back(rig):
    """Between acquisitions the hardware holds no slot. Otherwise a DCAM
    driver keeps its DMA pointers into memory the role may lay out again
    before the next arm. The segment's own close is the check: it refuses
    while any view of it is alive."""
    await _start(rig)
    await rig.trigger(2)
    await _stop(rig)

    rig.device.writer.close()  # raises BufferError if the capture kept a view


async def test_death_mid_acquisition_lets_go_of_the_frame_memory_before_the_segment_closes(rig):
    """The death path: the device stops while frames are arriving, with no
    StopDevice to disarm it in order first. The hardware must still let go
    of the slots before the segment closes. If the segment closed first, the
    close would be refused (the pin), the segment would be left behind, and
    the device would die with an error."""
    await _start(rig)
    await rig.trigger(2)

    rig.device.stop()
    await rig.device.stopped.wait()

    assert rig.device.exit_error is None
    with pytest.raises(FileNotFoundError):
        ImageBuffer.open(rig.buffer_name, timeout=0.05)


# --- F6: lost frames are fatal --------------------------------------------------------------


async def test_lost_frames_are_a_fatal_fault_and_end_publishing(rig, consumer):
    """A stack with a plane missing is not a stack, and a consumer counting
    frames would misplace every one after the gap. So the loss is a fatal
    fault, for the Coordinator to abort on, and the frames after it are not
    published, even as more arrive."""
    lose = _need(rig, "lose")
    await _start(rig)
    reader = consumer()
    await rig.trigger(2)

    lose(2)
    await rig.trigger(2)

    assert _this_acquisition(reader) == 2, "only the frames before the gap"
    (fault,) = rig.faults()
    assert fault.fatal
    assert fault.source == rig.ref
    await _stop(rig)
    assert _this_acquisition(reader) == 2, "and the stop publishes nothing after it"


@pytest.mark.parametrize("behind", ["within reach", "one too far"])
async def test_a_publisher_that_falls_behind_publishes_nothing_it_may_have_lost(
    rig, consumer, behind
):
    """While the role is stalled the hardware goes on writing round the ring.
    Once it may be writing into a delivered frame's slot (more than
    ``n_slots - in_flight`` frames behind), that frame may already hold a
    later image, and publishing it would label that image with the wrong
    frame number. Those frames are lost frames, and so fatal. Up to that
    point, falling behind costs nothing."""
    burst = _need(rig, "burst")
    await _start(rig)
    reader = consumer()
    n_slots, in_flight = _slots_and_in_flight(reader)
    arrived = n_slots - in_flight + (behind == "one too far")

    burst(arrived)
    await rig.elapse(0)
    await rig.trigger(1)

    if behind == "within reach":
        assert _this_acquisition(reader) == arrived + 1
        assert rig.faults() == []
    else:
        assert _this_acquisition(reader) == 0, "none of them, and nothing after"
        (fault,) = rig.faults()
        assert fault.fatal
