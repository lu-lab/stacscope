"""Run the buffer monitor.

    python -m stacscope.monitor <buffer-name>
    python -m stacscope.monitor --demo

Needs a Qt: ``pyqtgraph`` and ``PySide6``. Neither is a project dependency,
and PySide6 has no free-threaded build, so the usual way to run this is on
standard CPython beside a free-threaded engine:

    uv run --python 3.14 --with pyqtgraph --with PySide6 -m stacscope.monitor <name>

That separation is the point rather than a workaround. The monitor is a pure
consumer -- it opens a segment by name and has no link to the engine -- so it
can be a different process on a different interpreter without touching what it
watches.
"""

import argparse
import subprocess
import sys
import uuid

from stacscope.monitor.follower import BufferMonitor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stacscope.monitor", description=__doc__)
    parser.add_argument("name", nargs="?", help="the frame buffer to watch")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="start a moving-bar producer in its own process and watch that",
    )
    parser.add_argument(
        "--fps", type=float, default=20.0, help="demo frame rate (default: 20)"
    )
    parser.add_argument("--height", type=int, default=512, help="demo frame height")
    parser.add_argument("--width", type=int, default=512, help="demo frame width")
    parser.add_argument("--slots", type=int, default=16, help="demo ring size")
    args = parser.parse_args(argv)

    if not args.name and not args.demo:
        parser.error("give a buffer name, or --demo to make one to look at")

    name = args.name or f"demo{uuid.uuid4().hex[:12]}"
    producer = _start_demo(name, args) if args.demo else None
    try:
        from stacscope.monitor.window import run  # imported late: needs a Qt
    except ImportError as exc:
        _stop(producer)
        print(
            f"the monitor needs pyqtgraph and PySide6 ({exc}).\nTry:\n  uv run "
            f"--python 3.14 --with pyqtgraph --with PySide6 -m stacscope.monitor ...",
            file=sys.stderr,
        )
        return 2

    monitor = BufferMonitor(name)
    try:
        return run(monitor)
    finally:
        monitor.close()
        _stop(producer)


def _start_demo(name: str, args) -> subprocess.Popen:
    return subprocess.Popen([
        sys.executable, "-m", "stacscope.monitor.demo", name,
        "--fps", str(args.fps),
        "--height", str(args.height),
        "--width", str(args.width),
        "--slots", str(args.slots),
    ])


def _stop(producer: subprocess.Popen | None) -> None:
    """The demo producer owns its segment, so it has to be the one to remove
    it. Ask, then insist."""
    if producer is None or producer.poll() is not None:
        return
    producer.terminate()
    try:
        producer.wait(timeout=5)
    except subprocess.TimeoutExpired:
        producer.kill()


if __name__ == "__main__":
    raise SystemExit(main())
