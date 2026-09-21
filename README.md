# STACScope

**S**ynchronous **T**iming, **A**synchronous **C**ontrol & **S**treaming for
microscopy — an actor-based hardware control engine. It began as a Python
reimplementation of the control software for a custom light-sheet (SPIM)
microscope and is built to generalize to other instruments. See `CLAUDE.md` for
orientation and `reference/notes/architecture-plan.md` for the design this is
built against.

## Buffer monitor

A developer tool for watching a frame buffer while it is being written — to
check imaging results by eye, or to see what the ring itself is doing.

### Running it

One command, on any platform. It builds the monitor's environment the first
time and is instant after that:

```
uv run scripts/monitor.py camera_main
```

The buffer name is always required — it is the only thing that identifies a
segment. To try it with no hardware and nothing else running, `--demo` starts
a moving-bar producer in its own process under the same name:

```
uv run scripts/monitor.py demo_buf --demo --fps 20
```

`--fps`, `--height`, `--width` and `--slots` shape that demo producer and are
ignored otherwise. `--help` lists everything.

Buffer names are 1–30 characters of `[A-Za-z0-9_]`.

### Why it has its own environment

The monitor draws with `pyqtgraph` and `PySide6`, and PySide6 publishes no
free-threaded wheel — so it cannot be installed into this project's `3.14t`
environment and gets a standard-CPython one of its own, `.venv-monitor`.

That costs nothing architecturally. The monitor only ever opens a
shared-memory segment by name and has no connection to the engine, so it is
free to be a different process on a different interpreter, and cannot disturb
what it watches. `tests/tools/test_cross_interpreter.py` proves the pairing.

Its versions come from this project's `uv.lock`, under the `viewer` extra, so
everyone gets the same ones. The script above is what keeps the environment in
step with that lock; if you would rather do it by hand:

```
uv venv --python 3.14 .venv-monitor
UV_PROJECT_ENVIRONMENT=.venv-monitor uv sync --locked --python 3.14 --extra viewer
.venv-monitor/bin/python -m stacscope.monitor NAME     # Scripts/python.exe on Windows
```

Note that `UV_PROJECT_ENVIRONMENT` persists in a shell that sets it, and every
later `uv run` in that window would then use the monitor's environment instead
of the project's. The script sets it for one subprocess only, which is the
main reason to prefer it.

### What you are looking at

| Control | What it does |
|---|---|
| Slider | Which **slot** of the ring to watch — not which frame. Its range is the ring size and does not move. |
| Live follow | Track whichever slot the newest frame just landed in. |
| Pause | Hold the view still while reading the metadata. |
| Refresh | Re-read the slot now: resolves a torn image, or takes a single update while paused. |
| Histogram | Drag its handles to set contrast. The colormap is magma. |

Beside the image are the buffer's full header and the selected frame's
metadata.

Watching one slot rather than one frame is what makes this a buffer monitor:
park on a slot and you see the producer come round the ring and overwrite it.
The status line names the frame currently occupying the slot, or reports that
no frame of the current acquisition has reached it yet.

Reading a slot the producer may be writing can catch it mid-write. That is
accepted rather than prevented — the status line says `TORN` when the slot is
one the producer is working in, and **Refresh** re-reads it.
