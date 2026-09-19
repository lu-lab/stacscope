"""Frame-buffer throughput against camera interface tiers. Run on demand:

    uv run python benchmarks/buffer_tiers.py

Not a pass/fail test. Missing a tier is a failure only for a component that
was designed to reach it; this reports where each path's ceiling sits.

Three ceilings are measured, in frames per second:

- DMA producer: the device writes pixels itself, so software only stamps
  metadata and publishes. Cost is per frame, independent of frame size.
- Copying producer: software copies each frame in (USB-class cameras, or an
  SDK that owns its memory). Cost is per byte.
- Consumer: ``cursor.next()`` plus ``still_valid()``, reading views, no copy.
"""

import time

import numpy as np

from stacscope.core.buffer import FrameBuffer, Layout, MetaField, Metadata

GB = 1e9
TIERS = {  # interface: sustained bytes/s
    "USB 2": 0.04 * GB,
    "USB 3": 0.35 * GB,
    "Camera Link (Flash)": 0.8 * GB,
    "CXP-12 x4": 5 * GB,
    "CLHS / CXP x8": 10 * GB,
}
FRAMES = {  # name: shape (uint16)
    "full 2048x2048": (2048, 2048),
    "ROI 2048x256": (256, 2048),
    "ROI 2048x16": (16, 2048),
}
N_SLOTS = 32
METADATA = Metadata(fields=MetaField.HW_FRAME | MetaField.DEVICE_TIME | MetaField.HOST_TIME)


def rate(fn, frames):
    start = time.perf_counter()
    fn(frames)
    return frames / (time.perf_counter() - start)


def measure(shape):
    frame_bytes = int(np.prod(shape)) * 2
    writer = FrameBuffer.create("bench_tiers", capacity_bytes=N_SLOTS * frame_bytes, max_slots=N_SLOTS)
    try:
        writer.configure(Layout(shape=shape, dtype="<u2", n_slots=N_SLOTS), METADATA)
        writer.start_acquisition()
        source = np.random.default_rng(0).integers(0, 4096, shape, dtype=np.uint16)

        def dma(n):
            for _ in range(n):
                index = writer.published()
                writer.stamp(index, hw_frame=index, device_ticks=index, host_time_ns=time.time_ns())
                writer.publish(1)

        def copying(n):
            for _ in range(n):
                index = writer.published()
                writer.frames[writer.slot_of(index)] = source
                writer.stamp(index, hw_frame=index, device_ticks=index, host_time_ns=time.time_ns())
                writer.publish(1)

        reader = FrameBuffer.open("bench_tiers")
        try:
            def consume(rounds):
                # Publish most of a ring, then time reading it back; repeat.
                cursor = reader.cursor()
                elapsed, read = 0.0, 0
                for _ in range(rounds):
                    dma(N_SLOTS - 2)
                    start = time.perf_counter()
                    while (frame := cursor.next()) is not None:
                        reader.still_valid(frame.index)
                        read += 1
                    elapsed += time.perf_counter() - start
                return read / elapsed

            results = {
                "DMA producer": rate(dma, 20_000),
                "copying producer": rate(copying, max(50, int(2e9 / frame_bytes) // 20)),
                "consumer": consume(500),
            }
        finally:
            reader.close()
    finally:
        writer.close()
    return frame_bytes, results


def main():
    print(f"{'frame':<16} {'path':<18} {'ceiling fps':>12}   " + "  ".join(f"{t:>20}" for t in TIERS))
    for name, shape in FRAMES.items():
        frame_bytes, results = measure(shape)
        for path, fps in results.items():
            cells = []
            for tier_bytes in TIERS.values():
                needed = tier_bytes / frame_bytes
                cells.append(f"{'meets' if fps >= needed else 'below'} ({needed:>8.0f} fps)")
            print(f"{name:<16} {path:<18} {fps:>12.0f}   " + "  ".join(f"{c:>20}" for c in cells))


if __name__ == "__main__":
    main()
