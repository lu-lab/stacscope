"""Run the buffer monitor.

    python -m stacscope.monitor <buffer-name>
    python -m stacscope.monitor <buffer-name> --demo

The name is always required: it is how a consumer finds a segment, there being
no other link to the producer. ``--demo`` starts a moving-bar producer under
that same name rather than inventing one, so the buffer being watched is
always the one named on the command line.

Needs a Qt: ``pyqtgraph`` and ``PySide6``. Neither is a project dependency,
and PySide6 has no free-threaded build, so this needs an interpreter of its
own. Note that ``uv run --python 3.14 --with PySide6`` does **not** provide
one: ``uv run`` reuses the project's free-threaded ``.venv`` and the
resolution fails against ``cp314t``. Make the environment explicitly, once::

    uv venv --python 3.14 .venv-monitor
    uv pip install --python .venv-monitor -e . pyqtgraph PySide6

and then run it directly, with no environment variables involved::

    .venv-monitor\\Scripts\\python -m stacscope.monitor <buffer-name>
    .venv-monitor\\Scripts\\python -m stacscope.monitor --demo --fps 20

Running on a different interpreter from the engine is the point rather than a
workaround. The monitor is a pure consumer -- it opens a segment by name and
has no link to the engine -- so it can be a different process on a different
interpreter without touching what it watches.
"""

import argparse
import subprocess
import sys

from stacscope.core.buffer import check_name
from stacscope.monitor.follower import BufferMonitor


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="stacscope.monitor", description=__doc__)
    parser.add_argument("name", help="the frame buffer to watch, by segment name")
    parser.add_argument(
        "--demo",
        action="store_true",
        help="also start a moving-bar producer, in its own process, under NAME",
    )
    parser.add_argument(
        "--fps", type=float, default=20.0, help="demo frame rate (default: 20)"
    )
    parser.add_argument("--height", type=int, default=512, help="demo frame height")
    parser.add_argument("--width", type=int, default=512, help="demo frame width")
    parser.add_argument("--slots", type=int, default=16, help="demo ring size")
    args = parser.parse_args(argv)

    # Checked here rather than three layers down, so a typo is a sentence
    # rather than a traceback out of shared memory.
    try:
        name = check_name(args.name)
    except ValueError as exc:
        parser.error(str(exc))

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
