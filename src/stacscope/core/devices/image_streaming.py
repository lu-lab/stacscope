"""ImageStreaming: a device that streams images into a shared-memory buffer.

This is the seam between the two flows that otherwise never touch. On one side
the device protocol, which is asynchronous control; on the other the buffer,
which is throughput and nothing else. The role decides when the buffer is
created, laid out and released, and what is published into it; it never
touches a pixel.

**A stream does not wait for its readers.** Images are published whether or
not anyone consumes them, and a reader that falls behind loses frames rather
than slowing the device.

**Buffer lifetime is device lifetime.** The segment is created when the device
launches and released only when it goes, so a consumer can attach before the
first acquisition and keep the segment across every one that follows. STACScope
cannot assume it knows every consumer, and a consumer is not part of the
control flow, so it learns from the buffer itself that the layout changed or a
new acquisition began.

**The producer defines the structure.** Shape, dtype, padding and in-flight
depth are the device's to declare, through ``layout()``, because only the
device knows what its hardware writes. The header carries all of it, so a
consumer outside Python can read the buffer without asking anyone.

**Hardware sits under it, behind a frame capture.** A camera hands its frame
grabber the buffer's slots when it arms. From then on the grabber writes the
frames, and the device publishes each one, stamped, as it arrives. A lost
frame ends the publishing for that acquisition and is reported as a fatal
fault. A producer without a grabber, one that computes its images, writes its
slots itself instead.

**Where it sits among the device's base classes matters.** It wraps every
class listed after it: it lays out the buffer and hands the slots to the
grabber before they arm, and starts capture after; it stops capture before
they disarm, and takes the slots back after. A camera that records lists it
before Recording, as in class OrcaFlash(ImageStreaming, Recording,
TimedDevice). The package's own description (stacscope.core.devices)
explains the rule.
"""

import asyncio
import mmap

from stacscope.core.actors import Fault
from stacscope.core.buffer import FrameWriter, ImageBuffer, Layout, LayoutError, Metadata
from stacscope.core.ports import FrameCapture, FrameFormat, FramesLost

WAIT = 1.0
"""Seconds the publisher waits for frames before asking again. It bounds only
how long one wait lasts: stopping the capture ends a wait at once."""


def layout_for(fmt: FrameFormat, n_slots: int) -> Layout:
    """The buffer layout for ``n_slots`` frames of ``fmt``: each slot holds
    ``frame_bytes``, rounded up so every slot starts on ``slot_alignment``.

    Raises ``LayoutError`` for an alignment that is not a power of two, or is
    larger than a page, which is the most any segment guarantees.
    """
    alignment = fmt.slot_alignment
    if alignment < 1 or alignment & (alignment - 1):
        raise LayoutError(f"slot alignment {alignment} is not a power of two")
    # The segment's slot area starts on a page boundary (ImageBuffer.create);
    # nothing beyond that is promised on every OS, so a coarser alignment could
    # not be honoured for the first slot.
    if alignment > mmap.PAGESIZE:
        raise LayoutError(f"slot alignment {alignment} exceeds the {mmap.PAGESIZE}-byte page")
    return Layout(
        shape=fmt.shape,
        dtype=fmt.dtype,
        n_slots=n_slots,
        strides=fmt.strides,
        frame_offset=fmt.frame_offset,
        slot_bytes=-(-fmt.frame_bytes // alignment) * alignment,
        in_flight=fmt.in_flight,
    )


class ImageStreaming:
    """Mixin for a ``TimedDevice`` that streams images.

    **Where it goes in the bases decides what it wraps.** Like every role, it
    acts around whatever is listed after it, and with a capture it acts on
    both sides:

    - ``arm``: lay out the buffer, attach the slots to the capture, *then*
      everything after it arms, *then* capture starts.
    - ``disarm``: stop capture and publish the last frames, *then*
      everything after it disarms, *then* the slots are detached.

    Disarm is arm in reverse, so everything listed after it arms with the
    slots attached and disarms before they are taken back. List it before
    ``Recording``, so a file opens between attach and start and closes
    between stop and detach::

        class OrcaFlash(ImageStreaming, Recording, TimedDevice)

        arm:     attach frame memory, arm camera, open file, start capture
        disarm:  stop capture, publish last frames, close file,
                 disarm camera, detach frame memory

    A ``SoftwareTimed`` role goes before it, so its routine stops before
    capture does. See ``stacscope.core.devices`` for all the roles together.

    ``buffer_name``, ``capacity_bytes`` and ``max_slots`` come from config:
    the name because a consumer has to find the buffer without talking to the
    engine, and the capacity because it is an allocation, made once, that every
    acquisition's layout must fit inside.

    A device with hardware sets ``capture`` in its ``setup``, before calling
    ``super().setup()``, and ``n_slots`` from config. Each acquisition is then
    laid out from the capture's frame format, every frame it delivers is
    stamped and published, and the capture is closed with the device. A device
    without one overrides ``layout()`` and publishes through ``writer`` itself.
    """

    buffer_name: str = ""
    capacity_bytes: int = 0
    max_slots: int = 0
    n_slots: int = 0
    capture: FrameCapture | None = None

    writer: FrameWriter | None = None
    acquisition_id: int = 0

    _publisher: asyncio.Task | None = None
    _capture_stopping: bool = False

    def layout(self) -> Layout:
        """The structure of this acquisition's frames, which only the device
        knows. Called on every ``arm``, so it may change between acquisitions.
        With a ``capture``: ``n_slots`` frames of its current format."""
        if self.capture is None:
            raise NotImplementedError("a device without a capture declares its own layout")
        return layout_for(self.capture.frame_format(), self.n_slots)

    def metadata(self) -> Metadata:
        """Which per-frame fields this producer fills: the capture's stamps,
        or, with no capture, none, which is what a device with no frame
        counter or clock reports."""
        return Metadata() if self.capture is None else self.capture.metadata()

    async def setup(self) -> None:
        await super().setup()
        self.writer = ImageBuffer.create(
            self.buffer_name, capacity_bytes=self.capacity_bytes, max_slots=self.max_slots
        )

    async def arm(self, root_filename: str, cycles: int) -> None:
        """Configures the buffer for ``layout()`` and starts a new acquisition,
        **before** everything listed after this class arms, so the slots are
        laid out when the hardware is armed and no frame can arrive ahead of
        its acquisition. With a capture, attaches the slots to it before
        everything after this class arms, and starts it after. If any of that
        fails, the slots are detached before the error is raised, and if the
        start is what failed, everything after this class is disarmed first.
        A failure of that disarm is added to the start's error as a note.
        """
        # Laid out before anything can see the slots: the capture is attached
        # to them below, and a device without one may point its own driver at
        # them in its arm (DCAM attaches slot pointers), so the layout and the
        # new acquisition must already be in the header.
        layout = self.layout()
        self.writer.configure(layout, self.metadata())
        self.acquisition_id = self.writer.start_acquisition()
        if self.capture is None:
            await super().arm(root_filename, cycles)
            return
        await self.capture.attach(self.writer.slots)
        # A failed arm is not followed by a disarm, and the next arm lays the
        # segment out again: the hardware must not keep these slots.
        try:
            await super().arm(root_filename, cycles)
        except BaseException:
            # The chain undid its own part (Recording disarms below it when
            # its file will not open), so only the slots are left to give back.
            await self.capture.detach()
            raise
        try:
            await self.capture.start()
        except BaseException as exc:
            # Everything after this class is armed by now: on the Flash, the
            # camera and an open DCIMG file. Undone in disarm's order, file
            # before slots. The start's error is the one raised, as in disarm
            # (user, 2026-09-24). The detach also stops a half-started capture.
            try:
                await super().disarm()
            except Exception as later:
                exc.add_note(f"disarm also failed: {later!r}")
            finally:
                await self.capture.detach()
            raise
        # Capture is running: publish what it delivers until disarm says stop.
        self._capture_stopping = False
        self._publisher = asyncio.create_task(self._publish_frames(layout))

    async def disarm(self) -> None:
        """With a capture: stops it and publishes every frame it delivered,
        *then* everything listed after this class disarms, *then* the slots
        are detached, even if that disarm failed.

        If the capture will not stop, everything after this class still
        disarms, but the slots stay attached, and the stop's error is
        raised; closing the device releases them. Any later failure is added
        to it as a note."""
        if self._publisher is None:
            await super().disarm()
            return
        # Read by the publisher before each wait: once set, its next pass is
        # its last, and waits for nothing.
        self._capture_stopping = True
        try:
            await self.capture.stop()
        except BaseException as exc:
            # Listed first, this class holds the file's close (Recording, after
            # it) and the camera's safe state behind its stop, the step most
            # likely to fail on real hardware. The device is idle by now, so
            # teardown will not disarm again: whatever is skipped here is
            # skipped for good. No detach: the driver may still be writing
            # into the slots. close() cancels the publisher and closes the
            # capture, and leaves the segment mapped if that fails too.
            try:
                await super().disarm()
            except Exception as later:
                exc.add_note(f"disarm also failed: {later!r}")
            raise
        await self._publisher  # ends once the delivered frames are out
        self._publisher = None
        # Arm mirrored: attach, chain, start; stop, chain, detach. The chain
        # includes a Recording listed after this class, and the file must
        # close before the slots go back. Hamamatsu's recording sample does
        # cap_stop, rec_close, buf_release, and nothing in the reference says
        # the driver's recorder has finished with the frames before rec_close.
        # A buffer-reading recorder needs the final pass above to be done
        # before it closes, which this order also gives it.
        try:
            await super().disarm()
        finally:
            await self.capture.detach()

    async def close(self) -> None:
        """Releases the capture, then the segment, then whatever is below.
        Whatever is below is released even if the capture or the segment
        failed; the first failure is raised afterwards, with any later one
        added to it as a note.

        If the capture failed to release, the segment is not closed but left
        mapped, since its hardware may still be writing into it. ``BufferError`` from the
        segment means this process still holds a view of it (``frames``,
        ``slots``, ``meta``, or a slice of them).

        Stopping an acquisition deliberately does not release the segment:
        the last frames stay readable until the next acquisition overwrites
        them, because nothing says a consumer has finished reading when the
        producer has finished writing."""
        errors: list[Exception] = []
        publisher, self._publisher = self._publisher, None
        if publisher is not None:  # set only if disarm never ran, or its stop failed
            publisher.cancel()
            await asyncio.gather(publisher, return_exceptions=True)
        released = True
        if self.capture is not None:
            try:
                await self.capture.close()
            except Exception as exc:
                errors.append(exc)
                released = False
        # Not left to the BufferError alone: a capture that gave its view back
        # and then failed to release would let the close unmap memory its
        # driver is still writing into. Left mapped, the segment goes with the
        # process; on POSIX its name stays, and the next producer under that
        # name takes it over.
        if self.writer is not None and released:
            try:
                self.writer.close()
                self.writer = None
            except Exception as exc:
                errors.append(exc)
        try:
            await super().close()
        except Exception as exc:
            errors.append(exc)
        if errors:
            for later in errors[1:]:
                errors[0].add_note(f"close also failed: {later!r}")
            raise errors[0]

    async def _publish_frames(self, layout: Layout) -> None:
        """Publishes each frame the capture delivers, until the capture stops
        or a frame is lost."""
        seen = 0
        try:
            while True:
                # Read before waiting: a stop that lands during the wait gets
                # one more pass, which publishes whatever arrived before it.
                stopping = self._capture_stopping
                delivered = await self.capture.wait_frames(seen, 0 if stopping else WAIT)
                seen = self._publish(seen, delivered, layout)
                if stopping:
                    return
        except Exception as exc:
            # Nothing more is published this acquisition: a consumer counting
            # frames would misplace every one after the gap.
            reason = f"{type(self).__name__} stopped publishing: {exc!r}"
            self.publish(Fault(self.ref, reason, fatal=True))

    def _publish(self, seen: int, delivered: int, layout: Layout) -> int:
        """Stamps and publishes frames ``seen`` to ``delivered - 1`` of this
        acquisition, and returns ``delivered``. Raises ``FramesLost`` if the
        hardware may already be writing into the slot of any of them."""
        if delivered <= seen:  # a wait that timed out with nothing new
            return seen
        # The hardware may be writing up to in_flight frames past the newest
        # it reported, into the slots of frames one ring earlier.
        oldest = delivered + layout.in_flight - layout.n_slots
        if seen < oldest:
            raise FramesLost(seen, oldest - seen)
        # Stamp every frame before publishing any: a consumer may read a frame
        # the moment the count covers it, and must find its metadata there.
        first = self.writer.acquisition_start + seen
        for offset, stamp in enumerate(self.capture.stamps(seen, delivered - seen)):
            self.writer.stamp(
                first + offset,
                hw_frame=int(stamp["hw_frame"]),
                device_ticks=int(stamp["device_ticks"]),
                host_time_ns=int(stamp["host_time_ns"]),
            )
        self.writer.publish(delivered - seen)
        return delivered
