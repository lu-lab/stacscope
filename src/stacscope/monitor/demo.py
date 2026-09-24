"""A moving-bar test pattern generator, for the buffer monitor's --demo mode.

    python -m stacscope.monitor.demo <name> [--fps FPS] [--height H] [--width W] [--slots N]

Publishes frames under NAME until killed. Normally started automatically by
``stacscope.monitor --demo`` rather than run directly.
"""

# A process of its own, rather than a thread beside the window, because that
# is the topology the monitor is for -- a consumer attaching to a segment
# some other process owns. Running it that way here exercises the same path
# as real use instead of a shortcut past it.

import argparse
import time

from stacscope.adapters.simulated import TestImageGenerator
from stacscope.core.buffer import ImageBuffer, Layout


def produce(name: str, *, fps: float, shape: tuple[int, int], slots: int) -> None:
    """Publish frames until killed. The parent kills it on the way out."""
    period = 1.0 / fps
    writer = ImageBuffer.create(name, capacity_bytes=1 << 26, max_slots=max(slots, 4))
    try:
        generator = TestImageGenerator(writer, frame_period=period)
        generator.configure(Layout(shape=shape, dtype="<u2", n_slots=slots))
        generator.start_acquisition()
        due = time.monotonic()
        while True:
            generator.emit(1)
            due += period
            time.sleep(max(0.0, due - time.monotonic()))
    except (KeyboardInterrupt, BrokenPipeError):
        pass
    finally:
        writer.close()  # the generator owns the segment and removes it


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
