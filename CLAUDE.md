# STACScope — orientation

Read this before touching anything. It is written for a fresh session.

## What this project is

**STACScope** — *Synchronous Timing, Asynchronous Control & Streaming* — (package
and import name `stacscope`, source in `src/stacscope/`) is a Python
reimplementation of the control software for a custom light-sheet
(SPIM) microscope, meant to generalize to other microscopes. The original is a
LabVIEW Actor Framework application described in thesis Chapter 3; it works, but
is being replaced for the reasons that chapter's §3.6 gives. Older notes under
`notes/` call it the "SPIM engine" — same project.

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
  never automatic; the user decides. While a `required` device is lost, the
  Coordinator refuses `StartAcquisition`, naming it, until it is relaunched.
- **Milestone 2's spec tests are complete** (B1–B10, the data flow), on
  `main`. The shared-memory image buffer is in `src/stacscope/core/buffer/`:
  the header spec (`header.py`), producer-defined `Layout`, the
  `publish`/`published` counter (`counter.py`, the only place memory ordering
  lives; x86-64 only by decision), `Cursor` (`next` / `latest` /
  `latest_group` / `wait`), per-frame metadata (`metadata.py`), and segment
  lifetime. `TestImageGenerator` in `adapters/simulated/` is the synthetic
  camera. `benchmarks/buffer_tiers.py` reports throughput against the
  interface tiers on demand. Decisions and their reasons are in the plan's
  "Open items → Consumer API". CI runs Linux + Windows
  (`.github/workflows/ci.yml`); the POSIX paths of B7 are exercised only
  there.
- **Milestone 3's spec tests are complete** (D1–D12, device actors), on
  `main`. The participant side of the protocol is in `src/stacscope/core/devices/`:
  `TimedDevice` (IDLE ↔ ARMED, `arm` / `disarm` / `close`), and three mixins
  — `Recording` (the acquisition's shared stem becomes this device's own
  file), `SoftwareTimed` (a routine on the clock, started by the published
  `SourcePhase` the device chose), and `ImageStreaming` (an `ImageBuffer` for
  the device's lifetime). **The order of a device's bases is the order of its
  calls**: each role wraps every class listed after it, so list them
  `SoftwareTimed, ImageStreaming, Recording`, then the adapter. The package
  docstring (`core/devices/__init__.py`) is the full statement, and
  `tests/unit/devices/test_composition.py` pins it. The Coordinator gained
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
  `adapters/simulated/` gained `FaultyDevice` and `SimulatedFreeRunningCamera`.
  **A heartbeat is mandatory for a process child**, by decision: the link
  catches a process that dies, only a heartbeat catches one that is alive and
  no longer answering. Decisions and findings are in the plan's "Milestone 4".
- **Milestone 5 is in progress** (real adapters). The ordered work list is in
  the plan under "Milestone 5 → Next steps". **5a, the NI-DAQmx adapter,
  is done**, through 5a-3 on the physical board. **5b's shape is decided**
  (2026-09-24): `ImageStreaming` is a device role whose primary
  responsibility is the buffer, and hardware sits under it behind a
  `FrameCapture` port. Camera settings are each device's own messages, not
  port methods. **K1–K9 (the port contract) are green in simulation**:
  `core/ports/frame_capture.py`, `SimulatedFrameCapture`, and
  `tests/contract/frame_capture_contract.py`. **Buffer views now pin the
  segment**: every view is built over `np.frombuffer`, so closing a buffer
  while a caller still holds a view (`frames`, `slots`, a `Frame`) raises
  `BufferError`. Tests must drop their frames before closing a reader.
  An error's traceback holds its frames' arguments too, so a function
  that takes slots `del`s them before its error leaves (K9).
  **F1–F6 (the role contract) are green in simulation**:
  `ImageStreaming` takes an optional `capture` and publishes what it
  delivers, and `SimulatedCamera` is now that role over
  `SimulatedFrameCapture`. The contract is in
  `tests/contract/image_streaming_contract.py`. A mixin shares the actor's
  attribute namespace: a role flag named `_stopping` once stopped the whole
  actor. **The `Recording` change is done**: the device picks where its
  file opens by where it lists `Recording`. `OrcaFlash(ImageStreaming,
  Recording, TimedDevice)` arms as attach, arm camera, open file, start, and
  disarms as stop, publish, close file, disarm camera, detach (DCAM's order).
  `ImageStreaming.disarm` is now `arm` mirrored, and a file that fails to
  open disarms what armed below it. A capture that will not stop still lets
  the file close and the camera disarm. A capture that will not *start*
  disarms what armed after `ImageStreaming`, then detaches. If a role's
  undo of a failed arm cannot disarm, the device answers `Rejected` and
  then dies (`TimedDevice._undo_arm`): it has failed in both directions. **The base-class
  order is enforced**: `TimedDevice.__init_subclass__` raises `TypeError`
  for roles out of order or listed after the adapter. **5b is done.**
  **5c is green in simulation** (Q1–Q10). **The DCAM adapter targets
  Windows only** (user): its tests live in `tests/adapters/dcam/`, stay out
  of CI and the default run, and are run by name,
  `uv run pytest tests/adapters/dcam`.
  - The pieces:
    - `DcamFrameCapture` (`adapters/dcam/capture.py`), over a private
      ctypes binding (`_api.py`);
    - a fake of the DLL's function table (`fake_dcam.py`), which numbers
      properties unlike the header, so an ID hard-coded in the adapter fails;
    - `measure_in_flight` (`adapters/in_flight.py`), which works through
      the port alone.
  - Set `STACSCOPE_DCAM_SDK` to the SDK folder and the conformance test
    checks our structs against the installed headers.
  - Stamps come from stamp buffers attached one per slot, and lost frames
    are caught both by DCAM's error and by gaps in the frame stamps (user).
  - Slots are packed at DCAM's own `BUFFER FRAMEBYTES`: `slot_alignment`
    defaults to 1 (user, 2026-09-25).
  - Settings are adapter methods, not port methods: `get`, `label`,
    `labels`, `set` (by property name, and by the camera's own value
    labels, never header numbers) and `fire_trigger` (Q11, Q12).
  - **5d's measurements are done, on the dev box Flash** (2026-09-25).
    The camera suite is `tests/hardware/dcam/`, gated on
    `STACSCOPE_CAMERA` (the DCAM index); it captures, and puts back
    every setting it changes. S1–S6 open the real DLL; K1–K9 pass on
    the camera in three formats; M1 measured in-flight depth 1 in all
    nine conditions; M2 streamed 800 frames a second with no loss. The
    DAQ-triggered condition also needs `STACSCOPE_CAMERA_TRIGGER`
    (`Dev1/port0/line0`, wired to the camera's BNC input here) and
    `--extra nidaqmx`.
  - The Flash declares `in_flight=2`: measured 1, plus a margin (user,
    2026-09-25). `fire_trigger` stays synchronous; fast acquisitions are
    DAQ-triggered (user).
  - **The `OrcaFlash` device is green over the fake and on the camera**
    (O1–O12, the role contract F1–F6 in
    `tests/adapters/dcam/test_role_contract.py`, and V1–V6 in
    `tests/hardware/dcam/`): `OrcaFlash(ImageStreaming, Recording,
    TimedDevice)` in `adapters/dcam/camera.py`. Its messages are in
    `adapters/dcam/messages.py` (user, 2026-09-25):
    - `SetProperty(name, value)`, `SetPropertyLabel(name, label)` and
      `GetProperty(name)`, each answered `PropertyValue(name, value,
      label)` or `Rejected`. The value and label are separate because
      the codec carries no `float | str`.
    - While armed, DCAM decides what it takes. A change that moves the
      frame format anyway is put back and made a fatal `Fault`.
    - `settings=[(name, value), ...]` in config is applied in order at
      launch, and a refusal fails the launch.
    - DCAM is opened in a worker thread, so heartbeats are answered while
      it starts.
    - **5e, DCIMG recording** (user, 2026-09-25): an acquisition with a
      stem records through the driver (`record` / `close_recording` on
      the capture, `dcamrec_*`) to `<save_directory>/<stem>_<name>.dcimg`.
      The directory comes from config and from `SetSaveDirectory`
      (answered `SaveDirectory`; refused while armed or if missing). A
      file is never overwritten. A file short of the frames published,
      with missed frames, or still being written at close is a fatal
      `Fault` at the stop.
    - **DCIMG reserves a file's whole size at open (V4)**. The frame
      limit is the free space less the larger of 100 GiB and 10% of the
      disk, capped by `max_record_frames` (user, 2026-09-25). A disk
      with no room for one frame refuses the arm.
    - **A recording's length is always indeterminate** (user,
      2026-09-30). Sizing the file from the trigger line's pulse count
      (`StartDevice.frames`, `CountPulses`, `DeviceSpec.trigger`) was
      built, then taken out: it mixed waveform logic and camera logic.
      It may be revisited. The Recording fix from the same work stays:
      the file is named before anything arms.
    - Test files go to `E:/stacscope_tests` on the dev box
      (`STACSCOPE_RECORD_DIR`), and are deleted after. They are read back
      with `tests/adapters/dcam/dcimg_reader.py` over `dcimgapi.dll`.
      `STACSCOPE_DCIMG_SDK` names the DCIMG SDK for its conformance test;
      the DCIMG SDK's licence has the DCAM SDK's terms, and the same policy
      applies.
  - **An adapter's own messages cross processes**: the codec checks a type
    registered after it was built the first time it meets one (a child
    builds its codec before unpickling the device).
  - **5e is done**, on the camera (V4–V5). **Next, for a new
    session: the 5e entry at the top of the plan's "Milestone 5" status
    block**, then 5f or Milestone 6 (user's choice).

  Time stamps are the driver's (`TimeSource.DRIVER`), by decision. **The DCAM
  SDK licence forbids distributing any of the SDK**: never copy its headers,
  samples or `dcamapi4.py` into this repository, and put "For end use and
  not for commercial purposes." in a comment in the DCAM adapter's files
  and its fake. **No copyright line or licence on those files**: the
  licence forbids claiming copyright in a derivative work. Keep the layer
  thin (property IDs looked up by name at runtime). The plan's "The DCAM
  SDK licence" has the policy the user decided, and the reasons. Start from
  the status block at the top of the plan's "Milestone 5": it has the ordered
  next steps. The plan's "5b — the image-streaming role and its capture port"
  has the decisions, the tests, and a survey of the dev box camera.
  What 5a leaves open is in the plan's "Open items".
  `NiSignalOutput` is in `adapters/nidaqmx/`, tested against a fake `Task`
  and against an NI MAX simulated PCI-6733 named **`Dev1-sim`**. Step 1 is
  done: `SignalOutput.close()`, which `WaveformSource.teardown` always calls.
  Step 2 is done: the tasks come from NI MAX by name
  (`NiSignalOutput(clock, ao_task=..., do_task=...)`), `configure` checks
  them against the `OutputConfig` and sets only DMA and buffer depth, and
  `read_output_config` builds the config from them. Step 3, the instrument, has
  been run on the board (see below). Device-adapter tests live
  in `tests/adapters/<vendor>/`, which the default run ignores, so run them by
  name: `uv run --extra nidaqmx pytest tests/adapters/nidaqmx`.
  **Some dev machines have the physical board installed as `Dev1`.** The
  simulated suite runs only against a device that is both named `Dev1-sim`
  and simulated. **The port contract suite runs against every adapter.** Its
  tests are in `tests/contract/signal_output_contract.py`. Each adapter's
  suite imports them with a `rig` of its own (config, clock, slack, and
  whether it can run dry): the simulated adapter in `tests/contract/`, and
  the NI adapter on `Dev1-sim` and in
  `tests/hardware/nidaqmx/test_contract.py`.
  **Device suites build their own tasks** (user, 2026-09-23):
  `tests/adapters/nidaqmx/saved_tasks.py` saves a pair from an
  `OutputConfig` as `stacscope_test_*`, and deletes them afterwards, so a
  change to the rig's tasks in NI MAX never breaks a test. The production
  adapter still loads the instrument's tasks from MAX by name; that is the
  deployment mechanism. Never load the instrument's `SPIM_*_Task`s from a
  test. (`DAQmxConfiguration_sim.txt` still imports a `_sim` copy of them
  for trying the rig's config by hand; no test uses it.)
  `WaveformSource` also takes a bounded queue of MAINs (`QueueMain`,
  W9–W13) for sweeps and closed loops. It publishes `MainApplied(tag, cycle,
  sample)` and `MainDiscarded`, and `SetWaveform` still means "this, now"
  and clears the queue. On the board, `buffer_samples` may not go below
  the AO onboard FIFO (3276 with five channels), and `space_available` is
  the smaller of the two tasks' free space. The plan's "Refill experiments"
  says why. The instrument uses 5000 (`BUFFER_SAMPLES`, the user's choice).
  The plan lists the measurements that would call for going higher.
  **Step 3's suite is `tests/hardware/nidaqmx/`**, and runs only on the
  device named in `STACSCOPE_HARDWARE`. Claude may run it only with
  `STACSCOPE_HARDWARE=Dev1-sim`, as a rehearsal; the user runs it on `Dev1`.
  H1–H10 cover running dry, draining, refilling, buffer depth, update
  latency, stall tolerance, a queued sequence, a long run at
  `BUFFER_SAMPLES` (`STACSCOPE_SOAK_SECONDS` lengthens it), and whether
  the last digital samples reach a pin. H10 needs P0.1 wired to a PFI
  line named in `STACSCOPE_FEEDBACK` (PFI9 on this rig), and skips
  otherwise. **Step 3 is done**, run on `Dev1`. The board drops the last
  64 digital samples of output that has wrapped its buffer, so `drain`
  pads the end with `TAIL_PAD` samples that hold the last values (user's
  choice; the plan, "The last digital samples (H10)").
  Draining and refilling by polling (decisions A and B) were confirmed on
  the board. If either misbehaves later, start from the H2/H3 records in
  the plan.
- **Buffer monitor (devtool).** `src/stacscope/monitor/` — a pyqtgraph window
  for watching an image buffer live: a slider over the ring's slots, live
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
- **An actor ignores a message it has no use for, told or asked** (user,
  2026-09-30): no answer, no Fault. Be conservative about sending messages.
  The Coordinator still relays `SourcePhase`. Direct subscription for
  software-timed devices, and a deep dive on the communication model, are
  deferred: the plan's "Open items → The communication model".
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

Names follow the plan's "Vocabulary" section, which settles image / frame /
slot / segment / buffer / waveform / phase. In short: "image" at the flow level
(`ImageStreaming`, `ImageBuffer`), "frame" for the mechanics inside the buffer
and the grabber (`Frame`, `FrameCapture`); "waveform" for WARMUP/MAIN/COOLDOWN,
"phase" only for `Phase`, "segment" only for shared memory, "buffer" for the
data flow. Also `setup` / `run` / `teardown` hooks, and "actor runtime", not
"actor core".

## Read these first, in this order

The notes (1–3) live in **`notes/`, which is tracked**, so every dev machine's
sessions share them — keep them up to date and commit them with the work they
describe. The source material (4–5) lives under **`reference/`, which is
gitignored and local-only** — it is deliberately not published to the remote. A
fresh clone will not have it; ask the user for a copy if it is missing.

1. **`notes/architecture-plan.md`** — the approved architecture. This
   is the spec to build against and supersedes anything that disagrees with it.
2. **`notes/spim-engine-walkthrough.md`** — what the legacy LabVIEW
   code actually does, and a map of dead code to ignore.
3. **`notes/claude-workflow-notes.md`** — process lessons, including
   the **hardware safety** section. Read that section before running anything on
   the instrument machine.
4. **`reference/thesis/ch3.md`** — authoritative for design intent. A Markdown
   conversion of the author's `chapter3.tex` (same folder); `ch3.pdf` is the
   typeset original, and section numbers cited throughout the plan refer to it.
5. **`reference/legacy/`** — the original LabVIEW, Jupyter and µManager tree.
   Read-only reference, never a build target. It includes
   `Labview/spimengine supporting files/` — a LabVIEW VI-documentation export
   of the whole 102-VI hierarchy under the entry point, **block diagrams
   included** as PNGs. That is the legacy code itself and outranks every
   description of it; the walkthrough explains how the files are named.

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

- This is a development box, not the control PC. Some dev boxes do have a
  physical PCI-6733 installed (see Milestone 5 above). **uv** is installed, with
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
