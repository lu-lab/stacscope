# SPIM Engine — orientation

Read this before touching anything. It is written for a fresh session.

## What this project is

A Python reimplementation of the control software for a custom light-sheet
(SPIM) microscope. The original is a LabVIEW Actor Framework application
described in thesis Chapter 3; it works, but is being replaced for the reasons
that chapter's §3.6 gives.

**The user is the original author** of both the instrument and the thesis. They
have deep domain knowledge of the optics, the hardware timing, and the design
intent. Ask them — they answer precisely, and they would rather be asked than
have you guess.

## Current state

Planning is complete. **No application code exists yet.** The tracked repo holds
only this file and `.gitignore`; everything else is local-only reference
material under `reference/`.

**The next task is: set up the development environment, then Milestone 1.**

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
4. **`reference/thesis/ch3.pdf`** — authoritative for design intent. Section
   numbers cited throughout the plan refer to it.
5. **`reference/legacy/`** — the original LabVIEW, Jupyter and µManager tree.
   Read-only reference, never a build target.

Do not re-derive these. They took full sessions to produce.

### Reading the thesis PDF

There is no `pdftotext`/poppler on this machine, so the Read tool cannot render
it. Perl with `Compress::Zlib` works — inflate the content streams and filter to
lines that are mostly letters. Embedded font binaries are interleaved with the
prose and must be filtered out. Spaces are stripped by this method; the text is
still readable as concatenated words.

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

- **Nothing is installed on this machine** — no `python`, `python3`, `py`,
  `conda` or `uv`. This is the development box, not the control PC.
- Target is **uv** with **Python 3.14 free-threaded (`3.14t`)**. Verify
  `pydantic-core` free-threaded wheels and ctypes behaviour at first install; if
  either fails, fall back to standard 3.14. The code must be correct on both.
- The test suite must pass green with **zero hardware and zero vendor SDKs**.
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
