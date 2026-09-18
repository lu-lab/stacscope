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

**Milestone 1 is in progress** (actor runtime + Coordinator + simulated timing
source, test-first). Its acceptance tests are listed, in agreed order, in the
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
  `core/timing/coordinator.py`, and `SimulatedDevice`.
- **Next:** W1–W6 (`WaveformSource` over the `SignalOutput` port). Each test
  goes red before its implementation goes green. Tests that pass on the first
  implementation get a mutation check: break the code on purpose and confirm a
  test fails. Each test
  goes red before its implementation goes green.

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
  from config, not from the device registry.

Channel map and the rest of the hardware facts are in the architecture plan.

## Do not

- Re-analyze the legacy LabVIEW binaries for architecture. That work is done and
  captured in the walkthrough.
- Treat the legacy implementation as the specification.
- Run anything that asserts analog or digital output, or triggers a camera.
