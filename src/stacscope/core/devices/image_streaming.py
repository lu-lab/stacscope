"""ImageStreaming: a device that streams images into a shared-memory buffer.

This is the seam between the two flows that otherwise never touch. On one side
the device protocol, which is asynchronous control; on the other the buffer,
which is throughput and nothing else. The mixin is thin on purpose — it decides
*when* the buffer is created, configured and released, and never touches a
frame.

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
"""

import mmap

from stacscope.core.buffer import FrameWriter, ImageBuffer, Layout, LayoutError, Metadata
from stacscope.core.ports import FrameFormat


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
    """Mixin for a ``TimedDevice`` that streams images. Put it first:
    ``class Camera(ImageStreaming, TimedDevice)``.

    ``buffer_name``, ``capacity_bytes`` and ``max_slots`` come from config:
    the name because a consumer has to find the buffer without talking to the
    engine, and the capacity because it is an allocation, made once, that every
    acquisition's layout must fit inside.
    """

    buffer_name: str = ""
    capacity_bytes: int = 0
    max_slots: int = 0

    writer: FrameWriter | None = None
    acquisition_id: int = 0

    def layout(self) -> Layout:
        """The structure of this acquisition's frames, which only the device
        knows. Called on every ``arm``, so it may change between acquisitions."""
        raise NotImplementedError

    def metadata(self) -> Metadata:
        """Which per-frame fields this producer fills. The default declares
        none, which is what a device with no frame counter or clock reports."""
        return Metadata()

    async def setup(self) -> None:
        await super().setup()
        self.writer = ImageBuffer.create(
            self.buffer_name, capacity_bytes=self.capacity_bytes, max_slots=self.max_slots
        )

    async def arm(self, root_filename: str, cycles: int) -> None:
        """Configures the buffer for ``layout()`` and starts a new acquisition,
        **before** the rest of the chain arms, so the slots are laid out when
        the hardware is armed and no frame can arrive ahead of its acquisition.
        """
        # Before, not after: a driver is pointed at the slot memory as it arms
        # (DCAM attaches the slot pointers), so the layout must already be there.
        self.writer.configure(self.layout(), self.metadata())
        self.acquisition_id = self.writer.start_acquisition()
        await super().arm(root_filename, cycles)

    async def close(self) -> None:
        """Releases the segment. Stopping an acquisition deliberately does not:
        the last frames stay readable until the next acquisition overwrites
        them, because nothing says a consumer has finished reading when the
        producer has finished writing."""
        if self.writer is not None:
            self.writer.close()
            self.writer = None
        await super().close()
