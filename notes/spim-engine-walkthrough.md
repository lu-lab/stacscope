# SPIM Engine walkthrough: launch → Run → Record

What executes when you run `Labview/SPIM Engine/HW Timed SPIM/virtual sheet (shared buffer).vi`,
press **Run** on the control window it opens, and then **Record**. Written as
a behavioral spec for reimplementing this functionality in a new application:
what the system does, which interfaces are load-bearing, and which parts are
vestigial and should not be carried forward.

All paths below are relative to the `Labview/` folder of the original project
and assume the same directory layout is preserved in a fresh copy.

## Sources and confidence

Four sources, in descending authority:

1. **The VI documentation export** at `reference/legacy/Labview/spimengine
   supporting files/` — `virtual_sheet_(shared_buffer) hierarchy.html` plus
   ~700 PNGs, produced by LabVIEW's `File → Print → VI Documentation` over the
   whole 102-VI hierarchy reachable from the entry point. **Block diagrams
   included**, one PNG per case-structure case. This is the code itself and
   settles anything the other three only describe. Diagram files are named
   `<VI path, underscored>d.png`, with `d1`, `d2`, … for further cases;
   `p` = panel, `c` = connector, `i` = icon.
2. **Thesis Chapter 3** ("Actor-Based Software Control for Complex
   Microscopes") — design intent plus annotated screenshots of the actual
   block diagrams (its Figures 3.7–3.13). Authoritative for *intent*; note
   that where it and the shipped code disagree, the code is what ran.
3. **Connector-pane / linked-VI strings** extracted from the binary `.vi`
   files — reliable for *what a VI references*, and specifically for
   **terminal labels on `Send *.vi` message constructors**, which is how
   message payloads become recoverable without opening LabVIEW.
4. **Inference** from naming and cross-references — flagged inline where
   relied on.

The DAQmx-path findings drawn out of source 1, and the errors in this document
they corrected, are written up in `labview-vi-requests.md`.

### Method note: analyzing LabVIEW binaries without LabVIEW

`.vi`/`.lvclass` files are binary; block-diagram and front-panel resources
are compressed, so wiring, case structures, and button captions are
invisible to text extraction. Extract printable runs with:

```
perl -ne 'while (/([\x20-\x7E]{6,})/g) { print "$1\n" }' "file.vi"
```

**Two specific pitfalls, both learned the hard way:**

- A message class's payload fields are **not** readable from its `.lvclass`
  (they sit inside a base64-encoded `FlattenedPrivateDataCTL` blob) but
  **are** readable as connector-pane terminal labels on its `Send *.vi`.
  Checking only the `.lvclass` and concluding "no payload" produced a wrong
  conclusion in the session that wrote this doc (see
  `claude-workflow-notes.md`).
- Absence of a string is weak evidence, not proof of absence. Cross-check
  a "this feature doesn't seem to exist" finding against a second source
  (a design doc, the actual user, or the LabVIEW editor) before asserting it.

If you have LabVIEW available: `File → Print → VI Documentation` can export
a VI (or a folder of them) to HTML/RTF with block-diagram images embedded.
That single feature removes most of the blindness described above — and it
has since been used, which is what source 1 is. Prefer it to everything else.

## Architecture: thesis concept → code artifact

| Thesis concept (Ch. 3) | Implementation |
|---|---|
| Central timing actor (§3.4.1, Fig 3.3) | `SPIM Engine/HW Timed SPIM/HW Timed SPIM/` — `DAQ Timed SPIM.lvclass` |
| Abstract hardware-timed device actor (§3.4.2, Fig 3.4) | `SPIM Engine/HW Timed SPIM/Abstract DAQ Triggered Actor/` |
| Camera actor (§3.4.3) | `SPIM Engine/HW Timed SPIM/HamamatsuFluor_v2/HamamatsuFluor/HamamatsuFluor.lvclass` |
| Basic control GUI (§3.5.4, Fig 3.10) | `SPIM Engine/HW Timed SPIM/Basic SPIM Controller/Actor Core.vi` — the Run/Record/Stop window |
| Launch GUI + scan generation (§3.5.2, Fig 3.5) | `SPIM Engine/HW Timed SPIM/virtual sheet (shared buffer).vi` + `gen scan waveforms - virtual.vi` |
| Lock-free shared-memory image buffer (§3.3.1) | `HamamatsuFluor Buffer.vi` (`WIN MMAP.lvlib`), named e.g. `HamamatsuFluorBuffer0` |
| napari live viewer (§3.5.7) | `Jupyter/napari_lightsheet/lightsheet_viewer.ipynb` + `lightsheet.py` |
| DCIMG → HDF5 conversion (§3.5.8) | `Labview/SPIM Image Converter/` (post-acquisition, separate project) |

Class hierarchy is flat by design: `DAQ Timed SPIM`, `Basic SPIM Controller`,
and `Abstract DAQ Triggered Actor` all descend directly from the Actor
Framework's `Actor.lvclass`; the camera classes descend from
`Abstract DAQ Triggered Actor`.

Also present but **out of scope for the LabVIEW side**: a `Micro-Manager/`
folder with `.cfg` device configs (ASI Tiger XY/Z stage, scanner, PLogic,
LED; Hamamatsu or IDS uEye camera; NI multi-analog-out) — this is a
separate, µManager-driven control path for stage/scanner hardware that the
LabVIEW actors do not touch. A full reimplementation needs to account for
this second control surface, not just the LabVIEW one.

## A. Launching `virtual sheet (shared buffer).vi`

1. The launch GUI's front panel holds the imaging parameters (path, laser
   exposure, frame/volume period, Z slices, per-axis scanner calibration
   offset/amplitude, ETL/scanner/laser signal delays, camera ROI).
   `gen scan waveforms - virtual.vi` builds the analog and digital
   waveforms — a linear ramp through each slice during laser exposure,
   cubic-spline flyback, MPC optimization for the ETL channel, rescale/offset
   per axis, resample to the 100 kHz output clock, then shift per the signal
   delays. Re-runs whenever a parameter changes during operation.
   - "Virtual" vs. the sibling `static sheet (shared buffer).vi` is an
     **optical** distinction (scanned beam synthesizing a sheet vs. a static
     cylindrical-lens sheet), not a timing one; the virtual scan waveform
     was added without disturbing the static path, and static is
     recoverable by zeroing that waveform's amplitude. Both feed the same
     DAQmx-timed engine.

2. `Launch Root Actor.vi` starts the **`DAQ Timed SPIM`** root actor.
   - `Pre Launch Init.vi` (Fig 3.7): imports the `SPIM_Analog_Task` /
     `SPIM_Digital_Task` configured in NI MAX, sets `AO.DataXferMech` and
     `DO.DataXferMech` to **DMA**, and issues
     `DAQmx Control Task → commit` so resources aren't reloaded on every
     start/stop cycle. Confirmed complete: those are the *only* properties
     the code sets. Channels, ranges, terminal configuration, sample rate,
     clock source, sample mode and regeneration mode all live in MAX, and the
     tasks are referenced everywhere by name rather than carried as refnums.
     The launch VI even *reads* `SampClk.Rate` back off `SPIM_Analog_Task` to
     build its waveforms against.

3. The launch VI then sends the root actor its configuration:
   - **Add Camera Msg** → `Add Triggered Device.vi` (Fig 3.8) calls
     `Launch Nested Actor.vi` and appends the new actor's enqueuer to
     `Camera Enqueuers`. The camera is launched *as a child of the timing
     actor* specifically to get Actor Framework **LAST-ACK** delivery, which
     guarantees the parent is notified if the child dies — that's what keeps
     the registry accurate (`Handle Last Ack Core.vi`).
   - **Get Device Enqueuers Msg**, sent via
     `Send Message And Wait For Response.vi`, returns the camera's enqueuer
     to the launch VI so it can address the camera directly.
   - Using that enqueuer: **Set Save Directory Msg**, **Set ROI v2 Msg**,
     **Set Exposure Msg**, **Set Buffer Length Msg** go straight to the
     camera actor.
   - **Launch Basic Controller Msg** → `Launch Basic Controller.vi` launches
     the **Basic SPIM Controller** GUI actor. Its front panel is the control
     window with **Run / Record / Stop**.
   - **Set Warmup Data / Set Main Loop Data / Set Cooldown Data Msg** load
     the three waveform buffers (`WARMUP` = three ETL thermal-equilibration
     cycles, `MAIN` = the imaging cycle, `COOLDOWN` = return ETL and
     scanners to neutral).

4. Camera `Actor Core.vi` (Fig 3.11) allocates the named shared memory, then
   runs an "image notifier" helper loop that polls camera BUSY state and
   frame count and writes the count (little-endian) into the buffer header.
   Documented quirk: the Windows memory-map call **only succeeds from
   LabVIEW's UI thread**, which forces the camera actor's front panel to
   stay visible on screen.

## B. Pressing Run

The GUI actor is just an event handler over three buttons in a helper loop.

1. **Run** sends `Start Triggered Acq Msg` to its caller (the timing actor)
   with `Repeat? = TRUE`, **`Write to disk? = FALSE`** — the two terminals on
   `Send Start Triggered Acq.vi`.
2. Timing actor enters `Start` (Fig 3.9 = `Start Triggered Acq.vi`): sends
   `Start Acq Msg` to every registered camera via
   `Send Message And Wait For Response`, blocking until each replies READY
   or times out ("Ready devices").
3. Camera `Start Acq.vi` (Fig 3.12): writes the buffer header for the
   current image format, resets `frames_written` to 0 (invalidating prior
   frames), builds the pointer array for the frames in the buffer, and hands
   those pointers to the DCAM driver via `tm_prepare_attach_40` — so the
   camera **DMAs frames straight into shared memory over PCIe**, with the
   actor only observing. Then `tm_startcapture_40`. Replies READY.
4. Timing actor → `Warmup`: writes the WARMUP buffers to the hardware output
   buffers, starts output, and immediately self-sends `Run Main Loop Msg`.
   There is no `DAQmx Start Task` — output starts because both warmup writes
   set **`auto start` = TRUE**, digital first and analog second (sequenced by
   the error wire). Starting the slave by writing to it before the master
   produces its first sample clock edge is the whole of the start-order
   discipline. Both warmup writes use `timeout = 1.0 s`.
5. `Run` state (`Run Main Loop.vi`) is maintained by **low-priority
   self-messaging**: each iteration writes another copy of the MAIN buffer,
   digital then analog, with **`auto start` = FALSE and `timeout` = −1** — so
   the wait for room happens *inside* `DAQmx Write`. Nothing ever reads a
   space-available property. Because the self-message is low priority, a STOP
   always preempts it, and normal-priority waveform updates land between
   cycles — this is what allows live retuning of scan/timing signals without
   tearing a cycle. Details:
   - The refill is skipped entirely if `DAQmx Is Task Done` reports the
     digital task finished.
   - `Samples Queued` in the class data accumulates what each write took.
   - The VI's `Error` case is **empty**: on any write error the self-message
     chain simply stops and the error propagates to the Actor Framework. No
     retry, no recovery.
   - `Repeat?` is re-read every cycle. TRUE re-sends `Run Main Loop Msg`;
     FALSE sends **`Stop Triggered Acq Msg`** — both at Low priority.
6. STOP (or `Repeat? = FALSE`) → `Cooldown` (`Stop Triggered Acq.vi`) writes
   the COOLDOWN buffer, digital then analog, `auto start` = FALSE,
   `timeout = 1.0 s`. Then, per task — analog first, then digital —
   **`DAQmx Wait Until Done` with `timeout = −1` followed by `DAQmx Stop
   Task`**, under the comment *"Wait until buffer is empty (and suppress
   errors about it)"*. The wait ends by erroring: with regeneration disabled
   the board reports **−200290** when it runs out of samples, and a General
   Error Handler set to *cancel error on match* on exactly that code discards
   it. The diagram is explicit that this suppression is scoped to the STOP
   path: *"Unexpected stops still propagate the error."* The whole cooldown
   is skipped if `DAQmx Is Task Done` already reports the analog task
   finished. Only then → `Stop` sends `Stop Acq Msg` to all cameras → camera
   `Stop Acq.vi` calls `tm_stopcapture_40` / `tm_unpreparecapture_40`;
   `Samples Queued` is reset to 0. Back to Idle.
7. On actor shutdown by any route, `Stop Core.vi` calls **`DAQmx Clear Task`**
   on both tasks, by name, unconditionally, with the errors discarded and no
   preceding Stop Task.

Throughout, the napari viewer is an independent process that opened the
same named buffer read-only and simply watches `frames_written`; it needs
no coordination with LabVIEW and tolerates the counter resetting to 0 at
each acquisition start.

## C. Pressing Record — what differs

Record is a **toggle**, not a momentary button, and its purpose is gapless
switching between live view and recording (for catching spontaneous worm
activity).

- **Toggle Record ON** → sends `Stop Triggered Acq Msg`, immediately
  followed by `Start Triggered Acq Msg` with **`Write to disk? = TRUE`**.
- **Toggle Record OFF** → sends STOP followed by START with
  `Write to disk? = FALSE` — i.e. drops back to live view rather than
  stopping. (Pressing **Stop** instead sends STOP alone and returns to
  Idle.)

Everything downstream is the same acquisition path as Run, with exactly two
additions:

1. **In `Start Triggered Acq.vi`:** when `Write to disk?` is true, the
   current timestamp is formatted (`%y-%m-%d…`) into a placeholder root
   filename and retained as `Latest file set`. This root filename is passed
   to the cameras with the `Start Acq Msg`, so every device's files for one
   acquisition share an identifying stem.
2. **In camera `Start Acq.vi`:** a case structure gated on `Write to disk`
   (Fig 3.13) that, when true, joins the stem to `Save Path` (set earlier
   via `Set Save Directory Msg`), computes `Max frames` from free space on
   the storage drive when no duration is given, and starts the driver's
   **native DCIMG writer** (`tm_startrecorder_40`) before capture begins.
   When false, this case is skipped entirely and frames only flow to the
   shared buffer. `Stop Acq.vi` correspondingly calls `tm_stoprecorder_40`.

So: identical timing, identical waveform generation, identical
shared-memory streaming and live view. Record only adds a filename and
enables an in-driver file writer. Recording is deliberately the camera
actor's own responsibility (§3.3.3) so each device can use whatever format
is fastest for it — DCIMG here — with consolidation to HDF5 deferred to
post-processing.

## The shared-memory buffer contract (the key interface to preserve)

A single-producer/multi-consumer lock-free circular buffer in an OS-named
shared memory block. Header, little-endian `c_int` fields, exactly as
consumed by `lightsheet.py`:

```
buffer_frames, frames_written, frame_bytes, frame_width, frame_height, buffer_offset
```

`frames_written` is monotonic and may exceed `buffer_frames` (index modulo
size). Only the producer writes. Consumers (viewer, file writer,
processors) take read-only views by name and poll `frames_written`;
overrun is defined only from the consumer's side and is the consumer's
problem to detect. The header describes data only as the producer
understands it — the camera does not know its frames are slices of a
volume. Mixed image geometries require separate parallel buffers, not one
shared buffer. No metadata travels through the buffer; it comes from
configuration or actor messages.

## Dead / irrelevant code (do not follow when reading this path)

**Caveat added after the VI documentation export.** That export is the
*static* hierarchy of `virtual sheet (shared buffer).vi`, so presence in it
proves a link, not that the code runs: whole classes are pulled in by dynamic
dispatch and by `.lvlib` membership, which is why the superseded
`HamamatsuFluor` (non-`_v2`), `ThorIR` and `Logger` classes all appear there
despite being dead. One item below is contradicted, though:
**`ramp with quintic reset.vi` is in the hierarchy**, so something in the
waveform-generation path statically calls it, and "not in this path" is wrong.
Treat the rest of this list as still-good but re-checkable against the export.

- `HW Timed SPIM/HamamatsuFluor/` and `HamamatsuFluor Messages/` (no `_v2`)
  — superseded lineage; `Start Rec.backup.vi` inside it is the last fossil
  of a `Start Rec.vi` override from a since-deleted
  `Abstract Camera Actor.lvclass` (its `_OriginalInstancePath` is still
  embedded in both current `Start Acq.vi` files). Unreachable: the renamed
  extension breaks override-by-name dispatch.
- `HW Timed SPIM/ThorIR/` and `HW Timed SPIM/Logger/` — abandoned
  integration attempts, explicitly discontinued (§3.5.6). The IR brightfield
  camera was run from its own vendor software on continuous
  hardware-triggered recording instead.
- `HW Timed SPIM/Data Access DLL/`, `Generic Frame Buffer.vi` — an alternate
  buffering scheme not wired into `HamamatsuFluor_v2`.
- Superseded siblings: `gen scan waveforms.vi` / `v2` / `v3`;
  `Set ROI Msg` (vs `Set ROI v2 Msg`); `etl measurement.vi` (vs `v2`);
  `Configure Msg` (exists, never sent by this launch VI).
- Not in this path: `calibration.vi`, `burst settings.vi`,
  `load waveforms from MAT file.vi`, `ramp with quintic reset.vi`,
  `Lightsheet Session Migrate.vi`, `convert.vi`, `napari viewer/`,
  `napari test.vi`, `viewer test.vi`.
- Scratch/backup: `testing.vi`, `test.vi`, `temp.vi`,
  `testing - static sheet.vi`, `HamamatsuFluor_v2/testing.vi`,
  `Abstract DAQ Triggered Actor/test reply 2.vi`,
  `Testing VIs/Generate filler images.vi`.
- Separate applications: `Labview/WormSPIM/`, `Labview/pressurebox/`,
  `Labview/Scan generators/`, `Ligh Sheet Control.lvproj`.
  **`Labview/SPIM Image Converter/` is separate but not irrelevant** — it
  is the post-acquisition DCIMG→HDF5 step (§3.5.8, ImageJ-style `/t#/c#`
  groups, lossless compression).

## Carry-forward notes for a reimplementation

Worth preserving:

- The three-buffer WARMUP/MAIN/COOLDOWN model with software→hardware double
  buffering; it's what makes live waveform edits safe mid-acquisition.
- Priority-queued self-messaging for the Run state, so STOP always preempts
  queued work.
- START/READY handshake before any hardware output; per-device READY or
  timeout.
- The shared-memory header contract above — keeping it byte-compatible
  means the existing napari viewer and any converter tooling keep working
  against a new backend.
- File writing as a per-device responsibility, with a shared root filename
  issued by the timing actor.
- LAST-ACK-equivalent child-death notification to keep the device registry
  honest.

Known pain points the thesis flags as reasons *not* to carry the LabVIEW
implementation forward (§3.6): shared-memory allocation forced into the
privileged UI thread (coupling camera background work to a visible front
panel), actor error handling that couples actors instead of letting one
fail and relaunch cleanly, and LabVIEW's poor fit for version control.
Suggested directions were **Pycro-Manager** (µManager acquisition hooks
over sockets) and **Python-Microscope** (already close to this
actor/device model, client-server, hardware-trigger synchronized) — worth
evaluating both against the requirements above before choosing a target
for the rewrite.

## Resolved discrepancy: cycle count vs. `Repeat?`

The thesis (§3.4.1) describes START as carrying a *number of cycles to
acquire* (negative = infinite), but the shipped `Send Start Triggered Acq.vi`
only exposes a boolean `Repeat?`.

**Settled: the boolean is the whole mechanism.** `Run Main Loop.vi` re-reads
`Repeat?` on every MAIN cycle and branches to either another `Run Main Loop
Msg` or a `Stop Triggered Acq Msg`. There is no counter anywhere in the class
data or on the wire. `Repeat? = FALSE` therefore means warmup, exactly one
MAIN cycle, cooldown. Finite multi-cycle acquisition was designed but never
built; a reimplementation that wants it is adding a capability, not restoring
one.

## Still unresolved

`Get Minimum Trigger Interval.vi` exists on the camera classes and computes
`max(Readout time + 0.001 s, Internal frame interval) × 1000` in ms — a
device declaring the shortest trigger period it can honour. No VI in the
exported hierarchy consumes it. Whether the timing actor ever validated the
MAIN cycle against its devices' floors, or whether the message was only ever
answered for the launch GUI's benefit, is not recoverable from this export.
