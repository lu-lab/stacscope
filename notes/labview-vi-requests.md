# LabVIEW VIs needing wiring details — ANSWERED 2026-09-22

Every item on this list is now answered. The user supplied a full
`File → Print → VI Documentation` export of the `virtual sheet (shared buffer).vi`
hierarchy — 102 VIs, block diagrams included — at:

    reference/legacy/Labview/spimengine supporting files/
        virtual_sheet_(shared_buffer) hierarchy.html   + ~700 PNGs

Diagram PNGs are named `<VI path with underscores>d.png`, with `d1`, `d2`, … for
the further cases of a case structure. `p` = front panel, `c` = connector,
`i` = icon. The five VIs this file asked about are:

| # | VI | Diagram |
|---|---|---|
| 1 | `DAQ Timed SPIM.lvclass:Stop Core.vi` | `DAQ_Timed_SPIM_lvlib_DAQ_Timed_SPIM_lvclass_Stop_Cored.png` |
| 2 | `DAQ Timed SPIM.lvclass:Stop Triggered Acq.vi` | `…_Stop_Triggered_Acqd.png`, `…d1.png` |
| 3 | `DAQ Timed SPIM.lvclass:Run Main Loop.vi` | `…_Run_Main_Loopd.png` … `d3.png` |
| 4 | `DAQ Timed SPIM.lvclass:Start Triggered Acq.vi` | `…_Start_Triggered_Acqd.png` |
| 5 | `DAQ Timed SPIM.lvclass:Pre Launch Init.vi` | `…_Pre_Launch_Initd.png` |

The originals stay under `SPIM Engine/HW Timed SPIM/HW Timed SPIM/`.

---

## 1. `Stop Core.vi` — does the actor clear its tasks?

**Yes, and that is all it does.** The diagram holds exactly two nodes of
interest: the task-name constants `SPIM_Analog_Task` and `SPIM_Digital_Task`,
each wired straight into **`DAQmx Clear Task`**. Notable in what is *absent*:

- No `DAQmx Stop Task` first. Clear stops implicitly.
- No error wire between the two clears, and no error wire out of either. Both
  run unconditionally and **their errors are discarded**.
- The tasks are re-referenced **by name**, not carried through the actor's
  private data.

The rest of the diagram is the Call Parent Method and a `final error code`
numeric.

**Bearing on the `close()` decision.** The legacy's close-equivalent is
unconditional, at actor teardown, error-swallowing, and reached on every exit
path including a fault — because `Stop Core` is the Actor Framework's last hook.
Our `teardown` is the same position. So the legacy answer is: the core *does*
own the release, it is not the builder's job, and it must not be able to fail.

## 2. `Stop Triggered Acq.vi` — how does it drain?

Gated on **`DAQmx Is Task Done`** applied to `SPIM_Analog_Task`. The `True`
case (already done) is empty — skip straight to stopping the devices. The
`False` case does the work, in this order:

1. Write `Cooldown.Digital` to the digital task, then `Cooldown.Analog` to the
   analog task — **`auto start` = FALSE, `timeout` = 1.0 s** on both. Labelled
   *"Write final values (cooldown)"*.
2. **`DAQmx Wait Until Done` (timeout = −1, i.e. forever)** on the analog task,
   then **`DAQmx Stop Task`**.
3. The same pair on the digital task: Wait Until Done (−1), then Stop Task.
4. **General Error Handler** with `exception action` = *cancel error on match*
   and `exception code` = **−200290**, dialog suppressed.

Two comments on the diagram carry the intent:

> Wait until buffer is empty (and suppress errors about it)

> Note: Buffer underrun error is only suppressed when processing this STOP
> message. Unexpected stops still propagate the error.

Afterwards, outside the case structure, a For loop sends STOP to every entry of
`Camera Enqueuers` — *"Once DAQ task has finished, send STOP to devices"* — and
`Samples Queued` is reset to 0 in the class data.

**Corrections this forces.** See "Misunderstandings corrected" below; the
important one is that **−200290 `GEN_STOPPED_TO_PREVENT_REGEN_OF_OLD_SAMPLES`
is the code this board actually raises when the output runs dry**, and it is
not in our adapter's `UNDERFLOW_CODES`.

## 3. `Run Main Loop.vi` — poll for space, or block?

**Block.** No `Space Available` property is read anywhere in the VI. The
iteration is:

- Outer case: `No Error`. The `Error` case is **empty** — it passes the error
  through and, crucially, does **not** re-enqueue the message, so any write
  error ends the refill chain and propagates to the Actor Framework.
- Inner case: selector is **`DAQmx Is Task Done`** on `SPIM_Digital_Task`. The
  `True` case is empty. The `False` case refills:
  - Write `Main Cycle.Digital`, then `Main Cycle.Analog` — **`auto start` =
    FALSE, `timeout` = −1 (wait forever)** on both. The driver does the waiting,
    inside `DAQmx Write`.
  - `Samples Queued` (class data) += a numeric output of the digital
    `DAQmx Write` — read off the wiring, since the vendor VI's connector pane
    is not in the export; the sum's meaning is unambiguous either way.
- Then `Repeat?`:
  - **TRUE** → `Read Self Enqueuer` → send **`Run Main Loop Msg`** to itself at
    **Low** priority. Comment: *"Use low priority so STOP and data changes
    always go first"*.
  - **FALSE** → same enqueuer, send **`Stop Triggered Acq Msg`** at Low
    priority.

**Bearing on `wait_for_space()`.** The original has no equivalent — it never
asks how much room there is; it hands the driver a chunk with an infinite
timeout and lets the write return when the room appears.

## 4. `Start Triggered Acq.vi` — task start order and auto-start

**Auto-start is TRUE here, and the order is digital then analog.** There is no
`DAQmx Start Task` anywhere in the VI. Sequence:

1. A For loop over `Camera Enqueuers` ("Ready devices") sends each device a
   round-trip Start Acq and waits for the reply; the file paths come back
   indexed as `Latest file set`. Comment: *"DAQ starts once all devices are
   ready"*.
2. Write `Warmup.Digital` to the digital task — **`auto start` = TRUE**,
   `timeout` = 1.0 s. The error wire then sequences the analog write:
   `Warmup.Analog`, **`auto start` = TRUE**, `timeout` = 1.0 s.
3. `Samples Queued` in the class data is set from the write.

So the slave is started (by its own write) before the master, and the master's
first sample clock edge cannot precede it. Same invariant our adapter keeps,
reached by an explicit `start()` pair instead of auto-start.

## 5. `Pre Launch Init.vi` — anything beyond DMA and commit?

**No.** The diagram is exactly: load `SPIM_Analog_Task` by name → DAQmx Channel
property `AO.DataXferMech` := **DMA** → `DAQmx Control Task` := **commit**; and
load `SPIM_Digital_Task` → `DO.DataXferMech` := **DMA** → commit. A comment
reads *"Commit resources so DAQmx doesn't reload everything for each start/stop
cycle"*. Nothing else is set. Everything else — channels, ranges, terminal
configuration, rate, clock source, regeneration mode, sample mode — comes from
NI MAX.

---

## Corroborating find: the rate is read *out of* MAX

`virtual sheet (shared buffer).vi` holds a **DAQmx Timing property node reading
`SampClk.Rate`** from `SPIM_Analog_Task`, labelled `DAQmx Task Name`. The
waveform generator is built against the rate MAX reports; the code never sets
it. This settles the direction of dependency the user asked for: MAX is the
source of truth and the software reads from it.

(The nearby comment *"(Causes crash if changed)"* sits beside the `Volume
period (s)` control, not the rate property — worth not misreading.)

---

## Misunderstandings this corrects

1. **−200290 is missing from `UNDERFLOW_CODES`.** Our set is
   `OUTPUT_FIFO_UNDERFLOW` (−200016), `OUTPUT_FIFO_UNDERFLOW_2` (−200621) and
   `DAC_UNDERFLOW` (−200018). The code the legacy suppresses, and the one a
   regeneration-disabled output task raises when it runs out of samples, is
   **−200290 `GEN_STOPPED_TO_PREVENT_REGEN_OF_OLD_SAMPLES`**. The three we have
   are FIFO/DAC-level faults. This is a real defect in
   `adapters/nidaqmx/signal_output.py`.

2. **`wait_until_done` is not useless on a continuous task.** Stage 5a-2
   concluded it "can only ever time out", and rewrote `drain()` to poll
   `total_samp_per_chan_generated`. The legacy uses Wait Until Done with an
   infinite timeout and *expects it to return −200290* — running dry is how the
   wait ends. The observation that it timed out on `Dev1` was right; the
   inference that it must therefore time out on hardware was wrong, because a
   simulated device stalls instead of underflowing.

3. **The refill loop never measures free space.** Our `wait_for_space()` polling
   `space_avail` every 1 ms has no counterpart in the original, which blocks in
   `DAQmx Write` with `timeout = −1`.

4. **The walkthrough's open discrepancy is closed.** It flagged that the thesis
   (§3.4.1) describes START as carrying a *cycle count* while the shipped
   `Send Start Triggered Acq.vi` exposes only a boolean `Repeat?`. `Run Main
   Loop.vi` confirms the boolean is the whole mechanism: it is re-read every
   MAIN cycle, and `FALSE` makes the loop send itself `Stop Triggered Acq`.
   There is no counter. The finite multi-cycle capability was dropped between
   design and this build.

5. **Cooldown writes have a 1 s timeout, not an infinite one.** Only the MAIN
   refill blocks forever; the WARMUP and COOLDOWN writes use `timeout = 1.0`.

6. **A write error stops the acquisition rather than being retried.** The
   `Error` case of `Run Main Loop.vi` is empty, so the self-message chain simply
   ends and the error reaches the Actor Framework.

## Still not settled by any of this

- **Underflow remains unverified.** −200290 can now be named with confidence,
  but a simulated `Dev1` will not produce it, so the path stays untested until
  the instrument. The 5a-3 question stands: whether
  `total_samp_per_chan_generated` is readable *while handling* a failed write,
  which is how the adapter computes `Underflow.at`.
- **`DataXferMech` := DMA** is still unset in our adapter. The driver default
  for this board is very likely DMA already, but the original sets it
  explicitly.

## Critical gaps in the printout set

None for the PCI-6733 path — every DAQmx node in the acquisition path is
visible. Two smaller absences, neither blocking:

- Vendor VIs from `vi.lib` (`DAQmx Write`, `Wait Until Done`, `Clear Task`,
  Actor Framework internals) appear as icons only, which is expected and fine —
  they are publicly documented.
- `Get Minimum Trigger Interval.vi` is present on the camera classes
  (`max(Readout time + 0.001, Internal frame interval) × 1000`, in ms) but no
  VI in this hierarchy *consumes* it, so what the timing actor did with a
  device's declared floor is still unknown. Our `TimedDevice` has no equivalent
  capability. Worth raising separately if devices should be able to constrain
  the timing source.
