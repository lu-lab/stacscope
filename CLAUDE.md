# STACScope — orientation

Read this before touching anything. It is written for a fresh session.

## What this project is

**STACScope** — *Synchronous Timing, Asynchronous Control & Streaming* — (package
and import name `stacscope`, source in `src/stacscope/`) is a Python
reimplementation of the control software for a custom light-sheet
(SPIM) microscope, meant to generalize to other microscopes. The original is a
LabVIEW Actor Framework application described in thesis Chapter 3; it works, but
is being replaced for the reasons that chapter's §3.6 gives. Older notes under
`reference/` call it the "SPIM engine" — same project.

**The user is the original author** of both the instrument and the thesis. They
have deep domain knowledge of the optics, the hardware timing, and the design
intent. Ask them — they answer precisely, and they would rather be asked than
have you guess.

## Current state

**Milestone 1's spec tests are complete** (actor runtime + Coordinator +
simulated timing source, test-first), merged into `main`. Its acceptance tests are listed, in agreed order, in the
architecture plan under "Milestone 1"; they are the spec. The list was revised
when the thesis's central timing actor was split into a `Coordinator` and a
single configured `TimingSource`. The plan's "Coordinator and timing source"
section explains why.

- **Done:** R1–R5, the actor runtime in `src/stacscope/core/actors/` (priority
  mailbox, `ask`, lifecycle, supervision, heartbeat, hierarchical addresses),
  the `Clock` port in `core/ports/`, and `VirtualClock` / `SimulatedLink` in
  `adapters/simulated/`. P1: message types in `core/protocol/`, and the strict
  msgpack `Codec` in `core/transport/`. T1: the `TimingSource` base in
  `core/timing/` and `SimulatedTimingSource`, plus runtime publish/subscribe
  and the `Rejected` / `Fault` error messages. C1–C5: the `Coordinator` in
  `core/timing/coordinator.py`, and `SimulatedDevice`. W1–W6: `WaveformSource`
  (`core/timing/waveform.py`) over the `SignalOutput` port
  (`core/ports/signal_output.py`), with `SimulatedSignalOutput` and its port
  contract suite in `tests/contract/`. Plus end-to-end tests in
  `tests/integration/`.
  Relaunch: `Relaunch(name)` to the Coordinator respawns a lost device. It is
  never automatic; the user decides.
- **Milestone 2's spec tests are complete** (B1–B10, the data flow), on
  `main`. The shared-memory frame buffer is in `src/stacscope/core/buffer/`:
  the header spec (`header.py`), producer-defined `Layout`, the
  `publish`/`published` counter (`counter.py`, the only place memory ordering
  lives; x86-64 only by decision), `Cursor` (`next` / `latest` /
  `latest_group` / `wait`), per-frame metadata (`metadata.py`), and segment
  lifetime. `PatternProducer` in `adapters/simulated/` is the synthetic
  camera. `benchmarks/buffer_tiers.py` reports throughput against the
  interface tiers on demand. Decisions and their reasons are in the plan's
  "Open items → Consumer API". CI runs Linux + Windows
  (`.github/workflows/ci.yml`); the POSIX paths of B7 are exercised only
  there.
- **Milestone 3's spec tests are complete** (D1–D12, device actors), on
  `main`. The participant side of the protocol is in `src/stacscope/core/devices/`:
  `TimedDevice` (IDLE ↔ ARMED, `arm` / `disarm` / `close`), and three mixins
  that compose in that order — `Recording` (the acquisition's shared stem
  becomes this device's own file), `SoftwareTimed` (a routine on the clock,
  started by the published `SourcePhase` the device chose), and `Streaming`
  (a `FrameBuffer` for the device's lifetime). The Coordinator gained
  `DeviceSpec.required`, an abort on a live device's fatal `Fault`, and
  relaying of `SourcePhase` to participants. `adapters/simulated/` gained
  `TriggerBus` — the wire, in simulation, so an end-to-end frame count is
  caused by the pulses that played — plus `SimulatedCamera` and
  `SimulatedPressureBox`. Decisions are in the plan's "Devices — the
  participant side".
- **Milestone 4's spec tests are complete** (X1–X11, the process boundary), on
  `main`. `core/transport/` now holds the wire frame (`wire.py`), the address
  book for one link (`routing.py`, `Peer`), framing over a stream
  (`stream.py`, `Connection`), the parent side (`process.py`, `ProcessHost`)
  and the child entry point (`child.py`, run as `python -m
  stacscope.core.transport.child`). Placement is config: `DeviceSpec.placement`
  plus `Launcher` in `core/timing/launcher.py`, so the Coordinator asks for a
  device and never learns where it went. The runtime gained `adopt` (watch a
  child it did not start), `spawn(address=...)`, an `Actor.started` event, and
  a `stop()` that works from outside so `teardown` still releases the hardware.
  `adapters/simulated/` gained `FaultyDevice` and `SimulatedStreamingCamera`.
  **A heartbeat is mandatory for a process child**, by decision: the link
  catches a process that dies, only a heartbeat catches one that is alive and
  no longer answering. Decisions and findings are in the plan's "Milestone 4".
- **Buffer monitor (devtool).** `src/stacscope/monitor/` — a pyqtgraph window
  for watching a frame buffer live: a slider over the ring's slots, live
  follow, pause and refresh, the full header and per-frame metadata, and the
  magma colormap embedded as a table. The model (`follower.py`) holds every
  decision and imports no Qt, so it is tested in the default suite; the window
  is thin wiring and is not. **PySide6 has no free-threaded wheel**, so it
  needs an environment of its own, `.venv-monitor`, built from `uv.lock`
  under the `viewer` extra so its versions are reproducible. One command does
  everything, including creating the environment the first time:

      uv run scripts/monitor.py <buffer-name>
      uv run scripts/monitor.py <buffer-name> --demo --fps 20

  The name is always required — it is the only way a consumer finds a segment
  — and `--demo` starts its moving-bar producer under that same name. Do not
  reach for `UV_PROJECT_ENVIRONMENT` by hand: a shell that keeps it will send
  every later `uv run` to the monitor's environment. The script sets it for
  one subprocess.

  The monitor runs on standard 3.14 beside a `3.14t` engine — which works because it is a pure consumer of shared memory, proven
  by `tests/tools/test_cross_interpreter.py`.
- **Method:** each test goes red before its implementation goes green. Tests
  that pass on the first implementation get a mutation check: break the code
  on purpose and confirm a test fails. It earns its keep: in Milestone 3 it
  found a stale-`ActorRef` bug in the runtime, a failed file close that could
  leave a shutter open, and two vacuous assertions; in Milestone 4 it found a
  shutdown backstop that was masking the bug it was added for.
- **Real-clock tests.** The process tests in `tests/integration/` cannot run in
  virtual time — a fresh interpreter has a real clock. They are bounded twice:
  each has its own deadline, and the `host` fixture kills whatever is left. A
  child that cannot reach its endpoint exits rather than waits, as does one
  whose link home drops. Never add a wait here without a deadline.

Names follow the plan: `setup` / `run` / `teardown` hooks; "segment" for
WARMUP/MAIN/COOLDOWN (reserve "buffer" for the data flow); "actor runtime", not
"actor core".

## Read these first, in this order

All of it lives under **`reference/`, which is gitignored and local-only** — it
is deliberately not published to the remote. A fresh clone will not have it; ask
the user for a copy if it is missing.

1. **`reference/notes/architecture-plan.md`** — the approved architecture. This
   is the spec to build against and supersedes anything that disagrees with it.
2. **`reference/notes/spim-engine-walkthrough.md`** — what the legacy LabVIEW
   code actually does, and a map of dead code to ignore.
3. **`reference/notes/claude-workflow-notes.md`** — process lessons, including
   the **hardware safety** section. Read that section before running anything on
   the instrument machine.
4. **`reference/thesis/ch3.md`** — authoritative for design intent. A Markdown
   conversion of the author's `chapter3.tex` (same folder); `ch3.pdf` is the
   typeset original, and section numbers cited throughout the plan refer to it.
5. **`reference/legacy/`** — the original LabVIEW, Jupyter and µManager tree.
   Read-only reference, never a build target.

Do not re-derive these. They took full sessions to produce.

### Reading the thesis

Read `ch3.md`. The PDF is hard to use here: there is no `pdftotext`/poppler on
this machine and its text is in CID fonts, so plain stream extraction fails.
Fall back to `chapter3.tex` if the Markdown seems to have lost something (its
figures are summarized from captions, and citations are dropped).

## Working agreements

These come from the user directly and override default behaviour.

- **Red/green TDD.** Test first, always.
- **Maintain MVC separation** so the system stays nimble.
- **Separation of concerns is the governing philosophy.** Always think in terms
  of the three information flows: the hardware-timed high-precision path, the
  asynchronous actor communication path, and the high-throughput buffer path.
  Check every design decision against that separation.
- **Favor the thesis or the user's description over the legacy implementation.**
  The legacy code was experimental and prioritized working for a single
  configuration. It is evidence of what was built, not of what is wanted.
- **Abstract aggressively, and ship a simulated implementation of everything.**
  Adding new hardware or processors should require minimal interaction with core
  logic. The port contract test suite is the mechanism that enforces this.
- **The user runs anything that actuates hardware.** Claude reads, analyzes and
  writes code; the user executes and reports back. Improper camera shutdown can
  wedge the DCAM drivers badly enough to require a reboot.
- **Report before deciding.** When the user asks for an investigation, report
  findings and let them choose. Don't fold conclusions into the plan uninvited.
- **Write documentation for its audience.** The audience changes with the
  level, and so should the language.
  - **Module and script docstrings** are read by *users*, and by engineers
    with neither the knowledge nor the concern to develop the software. Say
    what the thing is for and how to use it, in general terms. No design
    argument. Remember these are often what a `--help` or no-argument run
    prints, so keep them plain text — no RST markup.
  - **Function and method docstrings** are read by developers calling this
    code from *other* code or projects. Say what it does, what to pass, what
    comes back, and any contract or failure a caller must know. Not why it is
    built this way.
  - **Code comments** are read by whoever modifies *this* code, or needs its
    behaviour at depth. Rationale, tradeoffs, what was tried and failed, and
    anything non-obvious — beside the line it explains.

  The only rationale that earns a place in a docstring is a short note when
  behaviour would otherwise surprise a caller ("creates its environment on
  first use, so the first run takes a moment"). Everything else is a comment.

  **This applies to production code only.** Tests are exempt: anyone reading
  them is developing this code directly and needs the rationale. A test's
  docstring is where the spec lives — what it protects and why that failure
  would matter — and the acceptance-test lists in the plan depend on it.

## Environment

- This is the development box, not the control PC. **uv** is installed, with
  both CPython 3.14 and 3.14 free-threaded; `.python-version` pins
  `3.14+freethreaded`. Run everything through `uv run` (e.g. `uv run pytest`).
- A `VIRTUAL_ENV` pointing at miniconda makes uv print a mismatch warning. It is
  harmless — uv uses the project's `.venv`.
- **Python 3.14 free-threaded (`3.14t`)** is the target. `pydantic-core`'s
  free-threaded wheel and ctypes behaviour were verified at first install and
  are pinned by `tests/unit/test_environment.py`. Standard 3.14 remains the
  fallback, and the code must be correct on both.
- **Testing on standard 3.14 needs an environment of its own.**
  `uv run --python 3.14 pytest` does **not** do what it looks like: it reuses
  the project's `.venv`, which is free-threaded, so it runs `3.14t` twice and
  reports a pass for a leg that never ran. Use instead:
  `UV_PROJECT_ENVIRONMENT=.venv314 uv run --python 3.14 pytest`
  (`.venv3*/` is gitignored). CI was never affected — each leg is its own
  runner with its own `.venv` — but local "green on both" claims made with the
  short form were worth nothing.
- The test suite must pass green with **zero hardware and zero vendor SDKs**.
  Tests run in virtual time (`VirtualClock`), where a missing reply waits
  forever rather than timing out. `pytest-timeout` (10 s) is the backstop, so a
  test that times out usually means an expected message never arrived.
- Git Bash's `python` writes cp1252 by default. Scripted file edits must pass
  `encoding="utf-8"`.
- **CI runs Linux and Windows.** The instrument is Windows, but the core and
  especially buffer management must stay OS-agnostic, and only CI keeps that
  honest.

## Hardware, for reference

- NI PCI-6733 analog out, 100 kS/s, regeneration disabled, digital slaved to
  `ao/SampleClock`. It is the sole timing authority.
- Hamamatsu ORCA-Flash4.0 V3 via DCAM-API, DMA-ing frames straight into shared
  memory. We write our own thin ctypes layer over the official SDK — not
  `pyDCAM`.
- ASI Tiger stack and a uEye camera live in µManager
  (`reference/legacy/Micro-Manager/tiger_uEye.cfg` is the operative config) and
  are **out of scope**. µManager is a *client* of this engine, not a device
  behind it.
- The uEye is hardware-triggered by the DAQ while being controlled by µManager —
  a device in the timing flow but not the control flow. Trigger channels come
  from config, not from the device registry. A STACScope uEye producer is
  planned as an option, secondary to the sCMOS path; its specifics are deferred.

Channel map and the rest of the hardware facts are in the architecture plan.

## Do not

- Re-analyze the legacy LabVIEW binaries for architecture. That work is done and
  captured in the walkthrough.
- Treat the legacy implementation as the specification.
- Run anything that asserts analog or digital output, or triggers a camera.
