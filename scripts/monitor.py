"""Launch the buffer monitor, building its environment if it is not there.

    uv run scripts/monitor.py <buffer-name>
    uv run scripts/monitor.py <buffer-name> --demo

Why a script rather than a documented command. The monitor draws with PySide6,
which publishes no free-threaded wheel, so it cannot live in this project's
`3.14t` environment and needs a standard-CPython one of its own. Pointing uv at
that environment means setting UV_PROJECT_ENVIRONMENT -- and in a shell that
keeps it, every later `uv run` in that window would quietly use the monitor's
environment instead of the project's. Setting it for one subprocess, here,
cannot do that.

The environment is built from `uv.lock`, so everyone who runs this gets the
same versions. It is a developer tool for an instrument, not only for people
who debug Python environments for a living.
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / ".venv-monitor"
PYTHON = ENV / ("Scripts/python.exe" if os.name == "nt" else "bin/python")


def sync() -> None:
    """Make the environment match the lock. Quick when it already does."""
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
