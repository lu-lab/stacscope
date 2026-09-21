"""Launch the buffer monitor.

    uv run scripts/monitor.py <buffer-name>
    uv run scripts/monitor.py <buffer-name> --demo --fps 20

The buffer name is required. Any other arguments are passed straight to the
monitor, which lists them under --help.

The monitor runs in an environment of its own, .venv-monitor, pinned by
uv.lock so the versions are the same on every machine. This creates it on
first use and checks it afterwards, so the first run takes a moment to install
and later ones do not.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# The monitor draws with PySide6, which publishes no free-threaded wheel, so it
# cannot be installed into the project's 3.14t environment and gets one of its
# own. Nothing is lost by that: it only ever opens a shared-memory segment by
# name, so which interpreter it runs on does not matter.
ENV = ROOT / ".venv-monitor"
PYTHON = ENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def sync() -> None:
    """Make the monitor's environment match the lock, creating it if needed."""
    # Set for this subprocess only, deliberately. A shell that exported
    # UV_PROJECT_ENVIRONMENT would keep it, and every later `uv run` in that
    # window would use the monitor's environment instead of the project's --
    # which is the main reason this is a script and not a documented command.
    env = {
        **os.environ,
        "UV_PROJECT_ENVIRONMENT": str(ENV),
        "VIRTUAL_ENV": "",  # an active venv would otherwise win, and warn
    }
    subprocess.run(
        ["uv", "sync", "--locked", "--python", "3.14", "--extra", "viewer"],
        cwd=ROOT, env=env, check=True,
    )


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        sync()
    except FileNotFoundError:
        print("uv is not on PATH; see the README for a manual setup.", file=sys.stderr)
        return 2
    except subprocess.CalledProcessError as exc:
        print(f"could not build the monitor's environment (uv exited {exc.returncode})",
              file=sys.stderr)
        return exc.returncode
    return subprocess.run([str(PYTHON), "-m", "stacscope.monitor", *argv], cwd=ROOT).returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
