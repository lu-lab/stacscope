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

### Setup, once

The monitor draws with `pyqtgraph` and `PySide6`. PySide6 publishes no
free-threaded build, so it cannot be installed into this project's
`3.14t` environment and gets one of its own:

```
uv venv --python 3.14 .venv-monitor
uv pip install --python .venv-monitor -e . pyqtgraph PySide6
```

That separation costs nothing. The monitor only ever opens a shared-memory
segment by name and has no connection to the engine, so it is free to be a
different process on a different interpreter, and cannot disturb what it
watches.

### Running it

The buffer name is always required — it is the only thing that identifies a
segment. On Windows:

```
.venv-monitor\Scripts\python -m stacscope.monitor camera_main
```

On Linux or macOS, use `.venv-monitor/bin/python` instead.

To try it with no hardware and nothing else running, `--demo` starts a
moving-bar producer in its own process under the same name:

```
.venv-monitor\Scripts\python -m stacscope.monitor demo_buf --demo --fps 20
```

`--fps`, `--height`, `--width` and `--slots` shape that demo producer and are
ignored otherwise. `--help` lists everything.

Buffer names are 1–30 characters of `[A-Za-z0-9_]`.

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
