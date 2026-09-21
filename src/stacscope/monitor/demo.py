"""A producer to watch when there is no real one: the moving-bar pattern.

It runs in a process of its own, launched by ``--demo``, rather than in a
thread beside the window. That is the topology the monitor is for -- a
consumer attaching to a segment some other process owns -- so the demo
exercises the same path as real use instead of a shortcut past it.
"""

import argparse
import time

from stacscope.adapters.simulated import PatternProducer
from stacscope.core.buffer import FrameBuffer, Layout


def produce(name: str, *, fps: float, shape: tuple[int, int], slots: int) -> None:
    """Publish frames until killed. The parent kills it on the way out."""
    period = 1.0 / fps
    writer = FrameBuffer.create(name, capacity_bytes=1 << 26, max_slots=max(slots, 4))
    try:
        producer = PatternProducer(writer, frame_period=period)
        producer.configure(Layout(shape=shape, dtype="<u2", n_slots=slots))
        producer.start_acquisition()
        due = time.monotonic()
        while True:
            producer.emit(1)
            due += period
            time.sleep(max(0.0, due - time.monotonic()))
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        writer.close()  # the producer owns the segment and removes it


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("name")
    parser.add_argument("--fps", type=float, default=20.0)
    parser.add_argument("--height", type=int, default=512)
    parser.add_argument("--width", type=int, default=512)
    parser.add_argument("--slots", type=int, default=16)
    args = parser.parse_args()
    produce(args.name, fps=args.fps, shape=(args.height, args.width), slots=args.slots)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
