# Python SPIM engine — architecture plan

## Context

The existing controller is a LabVIEW Actor Framework application (thesis Ch. 3).
It works and produced the thesis data, but §3.6 names three reasons not to carry
it forward: shared-memory allocation forced into LabVIEW's privileged UI thread
(coupling camera background work to a visible front panel), actor error handling
that couples actors instead of letting one fail and relaunch, and LabVIEW's
resistance to version control.

We are reimplementing the *ideas*, not porting the code. The legacy
implementation was experimental and prioritized working for a single
configuration; **where it disagrees with the thesis or with the author, the
thesis and the author win.**

Deferred for now: visualization (a new napari plugin comes later) and scan
waveform generation (treated as opaque arrays supplied from outside).

## Organizing principle: three information flows

Every design decision is checked against this separation. These flows have
irreconcilable requirements, which is why §3.2.3 insists they not share a
software entity.

| Flow | Carries | Requirement | Mechanism |
|---|---|---|---|
| **Timing** | Analog/digital signals | Sub-ms precision, no gaps | Hardware-clocked DAQ buffers |
| **Control** | Config, START/READY/STOP, errors | Responsive, never blocked | Async actor messages, priority mailbox |
| **Data** | Frames | Source-limited throughput | Lock-free shared-memory ring buffers |

**The rule:** no module participates in more than one flow except at a
designated seam.

- A buffer producer knows only "N frames of this shape and dtype" — not volumes, channels, or timepoints.
- The timing source knows how many triggers to emit per cycle — not what the images mean.
- Only the **session model** holds semantics, and distributes derived facts.

## Vocabulary *(settled with the user, 2026-09-24)*

Each of these words means one thing, in code and in prose. Before naming
anything new, check it against this list.

| Word | Means | Level | Examples |
|---|---|---|---|
| **image** | What a device streams and a consumer interprets: pixels as the layout describes them | flow | `ImageStreaming`, `ImageBuffer`, `TestImageGenerator` |
| **frame** | One delivery from hardware into a slot: counted, stamped, published, lost | mechanical, inside the buffer and the grabber | `Frame`, `FrameWriter` / `FrameReader`, `FrameCapture`, `wait_frames`, `FramesLost`, `frames_written`, `hw_frame` |
| **slot** | A fixed region of the ring, reused as frames wrap | mechanical | `n_slots`, `slot_of` |
| **segment** | A shared-memory segment, and nothing else | OS | the buffer's backing memory |
| **buffer** | The data flow's ring (plus, when qualified, a device's hardware FIFO) | flow | `ImageBuffer`, `buffer_samples` |
| **waveform** | WARMUP, MAIN or COOLDOWN: the samples an AO source holds | timing | `Waveform`, `SetWaveform`; the `(ao, do)` pair is `Samples` |
| **phase** | The acquisition's state as the source publishes it | timing | `Phase`: IDLE, ARMED, WARMUP, RUN, COOLDOWN |
| **cycle** | One play of MAIN within RUN | timing | `cycles`, `MainApplied.cycle` |
| **routine** | A software-timed device's own work on the clock | control | `SoftwareTimed.routine` |

Why "waveform", not "phase": RUN plays MAIN `cycles` times, and may play
several queued MAINs, so a phase is not a waveform. Why "frame" for the
capture port: it is the grabber's side, where frames are pulled into slots;
"image" belongs to the stream. Transport's wire "frames"
(`transport/stream.py`) and vendor terms (DCAM `FRAMESTAMP`) keep their own
meaning.

Renamed 2026-09-24, so older records may use the old names: `Streaming` →
`ImageStreaming`, `FrameBuffer` → `ImageBuffer`, `PatternProducer` →
`TestImageGenerator`, `SimulatedStreamingCamera` →
`SimulatedFreeRunningCamera`, `Segment` → `Waveform`, `SetSegment` →
`SetWaveform`, and the old `Waveform` alias → `Samples`. The planned
`FrameProducer` role is `ImageStreaming`.

## Core invariants

**1. The Run loop never blocks on a device mid-acquisition.**
Anything that must be cycle-synchronous is absorbed as a *hardware-timed event*
and lives in the timing flow. Everything else is either asynchronous (control
flow) or slow enough to sit above START. Coordinated motion use cases split
cleanly along this line: continuous control responding to image processing
(tracking, autofocus) is asynchronous; large-scale movement between locations is
slow enough that the acquisition responds to a START once hardware is in place.
**There is no per-cycle control-flow coordination, ever.**

**2. Recording is a per-device responsibility.** The Coordinator issues one root
filename; each device writes whatever format is fastest for it (§3.3.3).

**3. `teardown` always runs.** Failure to close a DCAM handle cleanly can wedge
the driver badly enough to require a reboot (§3.5.5).

**4. Messages are serializable by construction.** Plain dataclasses of
primitives and arrays — never live object references. *(P1, 2026-09-18:)*
messages are frozen dataclasses registered by class name; a strict `Codec`
checks every registered type's annotations at construction and every value at
encode. Wire format is **msgpack** — a plain map with a `type` key, readable
from any language; arrays as `{dtype, shape, data}`; an `ActorRef` as its
**hierarchical address** (`coordinator/camera0`, names `[A-Za-z0-9_.-]+`,
unique among siblings), resolved back to a ref on decode. This is the precondition
for the process and network transports below; if in-process messages are allowed
to carry live handles, those paths silently become unadoptable. Enforced by a
test that round-trips every message type.

**5. Coordination and timing are separate roles.** The `Coordinator` is pure
control flow and knows nothing about waveforms. Exactly one configured
`TimingSource` — always a software-controlled device — owns hardware time and
must progress Warmup → Run → Cooldown, but what each phase *does* is the
implementation's business. *(Revised 2026-09-18. This replaces an earlier rule
rejecting a camera source with warmup/cooldown at config load: once warmup
content belongs to the source implementation, that conflict does not exist.)*

**6. Error handling and logging are first-class, not an afterthought.**
*(User, 2026-09-22.)* They were a frequent pain point in the legacy
implementation and are something this rewrite should measurably improve on.
§3.6 names the architectural half — actor error handling that couples actors
together instead of letting one fail and relaunch cleanly — and the LabVIEW
printouts show the operational half: errors discarded at `Stop Core`'s task
clears, the refill loop's `Error` case left empty so a fault ends the
acquisition silently, and a blanket "cancel error on match" at shutdown that is
safe only because a comment explains which stop it is scoped to. The abandoned
`Logger` actor is the third piece of the same story.

What this commits us to, and what to check a design against:

- **An error's audience is part of its definition.** A fault the operator must
  act on, a fault a supervisor should relaunch through, and a condition that is
  the expected end of a phase are three different things and must not share a
  channel. `Underflow` vs. `Fault` vs. a drained buffer is exactly that
  distinction, and it is why the underflow code set is adapter business.
- **Never discard an error to make a path simple.** Where the legacy swallowed
  one, say why in a comment and keep the swallow narrow — one code, one phase.
- **Teardown may not raise past the thing that caused it.** Releasing hardware
  has to succeed on every exit path, so a failure to release is reported, not
  propagated over the fault that started the shutdown.
- **Diagnostics come from the actor runtime, not from each device.** Logging is
  infrastructure; a device that has to remember to log is a device that will
  not.

No logging subsystem is designed yet — this is the standard it has to meet when
it is. Deferred to its own milestone; see "Open items".

## System boundary

**In scope:** the NI DAQ timing path, the Hamamatsu camera, the shared-memory
data path, the actor system, and the external control surface.

**Out of scope:** µManager and the ASI Tiger stack. MMCore is *not* encapsulated
by an actor and we do not model Tiger's devices. Communication with Tiger is
inherently funnelled through a single serial link to a hub device, and wrapping
that adds nothing.

**Instead, the direction inverts: µManager is a _client_ of the engine, not a
device behind it.** Pycro-Manager acquisition hooks position hardware, then fire
StartAcquisition at the Coordinator and wait. The engine exposes a control surface; every
controller is an equal client of it:

```
Pycro-Manager hooks ─┐
CLI / scripts ───────┼─→ [message gateway] ─→ actor system
napari plugin ───────┘                        (timing, camera, buffers)
```

This is what makes "extension requires minimum interaction with core logic"
real: a new controller is a new client, not a core change.

## Why Python fits

All hard real-time lives in hardware: the DAQ clocks at 100 kS/s and the camera
DMAs frames straight into shared memory over PCIe. Software never touches a pixel
on the acquisition path. The GIL is not in the data path, and `nidaqmx`/DCAM
calls release it while blocking.

The one real constraint: the DAQ runs with **regeneration disabled**
(`RegenMode = Do Not Allow Regeneration`, digital slaved to `ao/SampleClock`), so
software must keep feeding or the output gaps. That is why the Run state is a
self-messaging write loop, and it is what the concurrency model must never stall.

## Architecture: ports and adapters

Core logic depends only on abstract ports. Every port has a **simulated adapter
that is a first-class deliverable**, not test scaffolding — it is what lets the
whole suite run with no hardware and no vendor SDKs.

**Portability is a property of the core, not the adapters.** The instrument is
Windows, but `core/` must run anywhere: no OS-specific calls, no drive letters,
`pathlib` throughout. Vendor adapters may be OS-bound — the DCIMG recorder is
Windows-only, and NI-DAQmx and DCAM-API have Linux builds of varying
practicality — but since the simulated adapters are pure Python, the entire test
suite is portable.

**CI runs on Linux and Windows both.** This is what keeps the property real: OS
agnosticism decays silently unless something checks it every commit, and the
Linux leg is where the POSIX shared-memory semantics below actually get
exercised.

```
stacscope/
  core/
    actors/      the actor runtime: priority mailbox, ask/timeout,
                 supervision, heartbeat, ActorRef + LocalTransport. No hardware.
    timing/      Coordinator, TimingSource base, WaveformSource. No hardware.
    model/       Session state machine + semantics. Pure data, no I/O, no asyncio.
    protocol/    all message types. Plain data, serializable by construction.
    transport/   msgpack Codec; Process / Zmq transports (depend on actors/,
                 never the reverse)
    ports/       SignalOutput, FrameCapture, Clock  (revised 2026-09-24:
                 ImageBuffer lives in buffer/, ImageStreaming is a device
                 role, FrameSink and Recorder are retired; see 5b)
  adapters/
    simulated/   complete implementation of every port
    nidaqmx/     NI PCI-6733                      [optional extra]
    dcam/        thin ctypes layer over dcamapi.dll [optional extra]
  app/           composition root, TOML config, CLI, gateway
tests/
  contract/      one suite per port; EVERY adapter must pass it
  unit/          model + actor core, virtual time
  integration/   full simulated system
```

### Ports

- **`TimingSource`** — *an actor role, not a port* (see "Coordinator and timing
  source" below). The DAQ's onboard clock via `WaveformSource` today; a camera
  master pulse or PLogic are future implementations.
- **`SignalOutput`** — emits analog/digital waveforms; the port under
  `WaveformSource`. `configure(channels, rate)`, `space_available()`,
  `write(waveform)`, `arm()`, `go()`, `stop()`, `commit()`. The simulated adapter
  models a virtual sample clock that *consumes* samples on a schedule and reports
  **underflow** when not refilled — making the hardest real failure mode cheap to test.

  **Why these are two ports, not one.** The LabVIEW version conflates "central
  timing actor" with "the device generating the clock", but §3.4.1 is
  conditional — *"For microscopes which use dedicated hardware for analog and
  digital signal generation, it must also serve as the device adapter."* §3.2.1
  notes high-end cameras generally can serve as timing sources, and the
  Flash 4.0 V3 has a Master Pulse generator built for exactly that. Splitting the
  port costs a config field now; retrofitting it once the Start FSM assumes
  NI-as-master is expensive.

  **`arm()` separate from `go()`** is what makes clock ownership configurable:
  which device goes last depends on who holds the clock. With the NI board as
  source, the camera is armed and the DAQ goes. With a camera as source, the DAQ
  is armed waiting on a trigger and the camera goes.

  For this rig the NI board stays the source — a dedicated AO generator at
  100 kS/s gives 10 µs resolution off a stable onboard clock, where camera
  master-pulse timing is quantized to frame/readout boundaries with its own
  jitter. Only the internal-clock case gets implemented now.
- **`TimedDevice`** — actor base for the Idle/Start/Run/Stop contract of §3.4.2,
  hardware- and software-timed alike. The Coordinator's registry holds these as
  **participants**.
- **`ImageBuffer`** — named ring buffer; `create(...)` for producers, `open(name)` read-only for consumers.
- **`ImageStreaming`** — *a device role, not a port* (revised 2026-09-24, see
  "5b"; first called `FrameProducer`). Its primary responsibility is management of the buffer: lifetime,
  layout, acquisitions, publishing, and seeing that the hardware lets go of
  the frame memory before the segment closes.
- **`FrameCapture`** — the port under a hardware `ImageStreaming` device: attach, start,
  wait for frames, stamps, stop, detach, close. Settings are the device's own
  messages, not port methods.
- ~~`FrameSink` / `Recorder`~~ — retired 2026-09-24. The buffer is the seam
  to consumers; a driver-native recorder belongs to capture; a generic
  recorder is a consumer.
- **`Clock`** — injectable, so timeout logic is tested in virtual time.

### Device taxonomy — three categories, not two

| Category | Control flow | Timing flow | Example |
|---|---|---|---|
| Hardware-timed actor (§3.4.2) | yes | yes | Hamamatsu sCMOS |
| Software-timed actor (§3.4.4) | yes | no | pressure box, shutter |
| **Timing-only participant** | **no** | **yes** | uEye on `port0/line2` |

The third category is real and in use: the uEye brightfield camera is
hardware-triggered by the DAQ but controlled entirely by µManager.

**Consequence:** the set of trigger channels the timing source emits is **not**
derivable from the device registry — it comes from config. A device can be
triggered without being registered, and must keep being triggered whether or not
an actor claims it. Deriving trigger generation from registered actors would
silently stop triggering the uEye.

Migration: when the uEye eventually gets its own actor, it registers against an
already-configured trigger channel — an addition, not a restructure.

### Actor core

Three semantics, which no library provides together — hence ~300 lines in-repo:

1. **Priority mailbox** (`asyncio.PriorityQueue`) with `URGENT`/`NORMAL`/`LOW`.
   STOP preempts queued LOW self-messages maintaining the Run state; NORMAL
   waveform updates land between cycles without tearing one (§3.5.1). Entries are
   `(priority, monotonic_seq, msg)` so unorderable payloads never compare.
2. **`await ask(msg, timeout)`** — the START→READY handshake, per device.
3. **`spawn_child()`** whose task posts `ChildExited(child, error)` to the parent at
   URGENT priority — the LAST-ACK equivalent keeping the registry honest.
   `error` is a string (the exception's `repr`, `"cancelled"`, or a heartbeat
   timeout description), never the exception object: death may be detected
   across a process boundary where no exception exists. Exactly one
   `ChildExited` per child, whichever detection path fires first.

*Implementation notes (R1–R5, 2026-09-18):* the reply address travels in an
`Envelope`, not the message, so message types stay pure data; `self.reply()` is
a no-op for a told message. **Heartbeats prove the runtime is alive, not the
handler:** they are answered on the receive side before the mailbox, so a
long handler (e.g. multi-second DCAM allocation) is never declared dead; a hung
handler is caught by per-request `ask` timeouts instead. The parent pings, with a
`HeartbeatPolicy(interval, timeout)` chosen per child. All timeouts go through
the injected `Clock`; `VirtualClock.advance()` fires them deterministically.

Lifecycle mirrors LabVIEW's three hooks (`Pre Launch Init` / `Actor Core` /
`Stop Core`), renamed to avoid overloading "core": `setup()` → `run()` →
`teardown()`, the last in a `finally`. Blocking driver calls go to a per-device
thread via `asyncio.to_thread`. This library is called **the actor runtime**.

**An actor ignores a message it has no use for, told or asked (user,
2026-09-30).** It sends no answer and publishes nothing, and an asker that
sent the wrong thing learns it from its own timeout. Until then, a told
message with no case was published as a non-fatal `Fault`, which reaches
every subscriber. The Coordinator hears everything its source publishes, so
each `MainApplied` became a Fault to every participant and client, on every
acquisition. A known command in the wrong state is still refused, with
`Rejected`, or with a `Fault` if it was told, because it was meant for that
actor. **Be conservative about sending messages** (user): an actor may keep an
address book for its own function, for example a source's list of who wants
its phases. By default it knows its parent, its children, and the sender of
the message it is handling, and a message may carry an address for
subscribe and unsubscribe.

### Coordinator and timing source

*(Decided 2026-09-18.)* The thesis's central timing actor merges two roles;
§3.4.1 ("for microscopes which use dedicated hardware... it must also serve as
the device adapter") is what licenses splitting them.

**`Coordinator`** — control flow only. First messaged, last to act.
FSM: Idle → Starting → Running → Stopping → Idle, plus **Faulted**.
- `StartAcquisition(cycles, root_filename)`: `StartDevice` to every participant
  *and the source*, each `ask`ed with timeout. Only when all reply
  `DeviceReady` does the source receive `Go`.
- READY timeout: abort — `StopDevice` to those that were ready, publish an
  error, return to Idle. The laggard stays registered (death is heartbeat's
  job), and the source never receives `Go`.
- Running does not loop; it waits for `StopAcquisition`, `SourceFinished`, or
  `ChildExited`. `StopAcquisition` → `Finish` to the source → on
  `SourceFinished`, `StopDevice` to all → Idle.
- Loss of the source is a **major error**: `StopDevice` to all, publish a fatal
  error, enter Faulted; `StartAcquisition` is refused until a source is
  re-established. Loss of an ordinary participant only drops it from the
  registry.

**`TimingSource`** — exactly one per Coordinator, **configured, not
registered**, always a software-controlled device. The base actor enforces
Idle → Armed → Warmup → Run → Cooldown → Idle and publishes `SourcePhase` on
each transition and `SourceFinished` when output has ceased. Subclasses supply
phase behaviour only. The source alone knows cycle boundaries, so it counts
cycles (from `StartDevice`), deciding at *commit* time rather than play time —
which is what allows a gapless handoff.

*T1 implementation (2026-09-18):* the base class drives everything; hooks are
`arm(root_filename, cycles)`, `disarm()`, `warmup()`, `run_cycle()`,
`cooldown()`, `drain()`. `Finish` and an exhausted count both take effect at
the next cycle boundary (the LOW `_NextCycle` self-message lives in the base).
`StopDevice` is safe in every phase: no-op when idle, `disarm` when armed, and
when running it finishes (cooldown + drain) *before* replying `DeviceStopped`.
`SourcePhase` is announced when a phase is entered, i.e. committed, ahead of
when a buffered source plays it.

*C1–C5 implementation (2026-09-18):* the Coordinator spawns the source and
every participant as its children from `DeviceSpec(name, factory,
ready_timeout, stop_timeout=ready_timeout, heartbeat=None)` — agreed with the
user, and it stays valid for remote devices (see the proxy idea under
distribution). Client replies: `AcquisitionStarted`, `AcquisitionStopped`, or
`Rejected`. On a READY abort the client is answered immediately, and
`StopDevice` then goes to **every** device, the laggard included, so a late
READY cannot leave anything armed (a deliberate widening of "stop those that
were ready"; `StopDevice` is safe in every state). Source faults are
republished to the Coordinator's subscribers. Faulted is currently terminal;
**Relaunch** *(user, 2026-09-18)*: faults are reported all the way up to the
user, who decides what happens next (a fault may be a hardware stop they must
deal with first). STACScope makes relaunch *possible* but never does it on its
own; when it is appropriate is policy for later. `Relaunch(name)` to the
Coordinator, from Idle or Faulted, spawns a fresh instance from the device's
spec; relaunching the source leaves Faulted, and a relaunch whose setup fails
lands back in Faulted with the cause in the `Fault`. A child's name is freed
the moment its parent hears it is gone, by exit or heartbeat. Every launch is
published as `DeviceLaunched(name, ref)`, because a ref to a previous
incarnation reaches nothing. Clients refresh from that, and must re-send what
lived only in the old instance (e.g. `SetWaveform`). Late subscribers miss
earlier `DeviceLaunched` events; a query is left for the gateway milestone.

**Errors** *(agreed 2026-09-18)*: a request in the wrong state is answered
`Rejected(reason)`, so `ask` returns immediately rather than timing out; if
the command was told rather than asked, the rejection is published instead as
`Fault(source, reason, fatal=False)`. Errors that answer nobody are published
as `Fault`. Both live in `core/actors/messages.py` beside `Subscribe` /
`Unsubscribe`: publish/subscribe is a runtime facility (`self.publish(msg)`).

**`WaveformSource(TimingSource)`** — the AO implementation over `SignalOutput`.
Holds WARMUP/MAIN/COOLDOWN **waveforms** (the thesis's "buffers"; see
"Vocabulary" for why neither "buffer", "segment" nor "phase"). Run is the LOW-priority self-messaging refill loop;
`Finish` (URGENT) preempts it, `SetWaveform` (NORMAL, sent by clients directly to
the source) lands between cycles. COOLDOWN is committed directly behind the last
MAIN: the handoff must be gapless. Empty WARMUP/COOLDOWN is a valid no-op phase.

*W1–W6 implementation (2026-09-18); defaults confirmed by the user the same
day, on the understanding that none binds another implementation:* (1) a waveform is `SetWaveform(waveform, ao, do)` — AO channels ×
samples (volts, float64) plus DO lines × samples (bool), same length, in the
configured channel order; (2) trigger lines are ordinary DO rows, configured as
`Channel(physical, label)` in `OutputConfig`, so an unclaimed line (uEye) is
emitted like any other; (3) hardware buffer depth is `OutputConfig.buffer_samples`.
Output starts only after `Go`, once the pre-roll (WARMUP + first MAIN) is
queued or the buffer fills; `go()` with nothing written is an error, as in
DAQmx. Waveforms longer than the buffer are written in chunks. An `Underflow`
kills the source, which the Coordinator treats as source loss (Faulted).
`arm` may raise `NotReady` (e.g. no MAIN set), answered `Rejected`.

**Camera sources** are not built yet but must remain possible: nothing in the
Coordinator or base role assumes AO. Some cameras need a software stop before a
final trigger; `SourcePhase(COOLDOWN)` is the hook for a future per-participant
`stop_at = "cooldown" | "finished"` config field (default `finished`).
AO timing working smoothly takes priority.

### Devices — the participant side

*(Decided with the user 2026-09-19, ahead of Milestone 3.)* Milestone 1 gave
the source a base actor but left ordinary participants as bare `Actor`s that
answer the device protocol by hand. `TimedDevice` is that missing base:
Idle → Armed → Running → Idle, with `arm(root_filename, cycles)` / `disarm()`
hooks, `StopDevice` safe in every phase, and hardware released in `teardown`
even when the actor dies mid-acquisition. An `arm` that raises is answered
`Rejected` and leaves the device idle and reusable, as `NotReady` already does
for the source.

**`ImageStreaming` is a mixin, not a base class** — nearly any device type might
stream images, and a shutter never will. It owns an `ImageBuffer` for the device's
lifetime and configures a layout per acquisition.

**Buffer names come from config**, not from runtime announcement. Two
producers on one name is a config error, not a runtime condition; the
takeover rule (see Open items) already assumes config keeps names unique. A
runtime announcement may still be offered as a convenience for clients
already connected, but config is the source of truth, so an external consumer
can be pointed at a buffer without talking to the engine at all.

**Two distinct failures, which `required` does not conflate:**
- *The device dies* (`ChildExited`). `DeviceSpec.required` (default **true**)
  decides: abort the acquisition, or drop it from the registry and carry on.
  A lost required device also refuses every later `StartAcquisition`, naming
  it, until it is relaunched; nothing is latched *(user, 2026-09-24)*.
  An optional device is a deliberate config choice — an experiment that should
  finish without a non-essential feature.
- *The device lives and reports a hardware fault.* `required` never fires,
  because nothing exited. A device's `Fault(fatal=True)` drives the system to
  stop and then to **Idle** — not to a latched Faulted state. If the user does
  not fix the external cause, the next acquisition simply faults again, which
  is a truthful enough signal without the engine holding a lock the user has
  to clear.

**Software-timed devices** (taxonomy row 2: pressure box, shutter) are the
half of the taxonomy Milestone 1 left hollow — until now they only answered
start and stop. What they add is work *during* a run, on the system clock.
- **They synchronize to published `SourcePhase`.** The Coordinator, which
  currently swallows them, republishes them to its subscribers; a device that
  cares subscribes, and one that does not never does. Devices stay ignorant of
  the source's identity, and the device protocol gains no message.
- **Per-device phase choice falls out of this** *(user, 2026-09-19)*: a
  software-timed shutter starts moving at WARMUP, while a driven experimental
  variable starts at RUN, with the first image.
- **That edge is good to milliseconds, not microseconds** — it crosses a
  mailbox and possibly a process. It must never be documented as better, or it
  will eventually be trusted. A software-timed device can NEVER be part of the
  acquisition loop; the pressure box works at seconds against sub-second volume
  rates.
- **The device owns its routine** ("routine", because waveform / phase / cycle /
  protocol are all taken). Not cycle counts, which would couple it to the loop
  rate; and not a central scheduler in core, which earns its place only when a
  second device needs one. The pressure box will likely also have its own UI
  for unsynchronized actions, and run a planned sequence stored in a
  per-recording config alongside the waveforms.
- **The device defines its own safe state**, reached in `StopDevice` before it
  replies `DeviceStopped` — as the source cools down before confirming. What
  "safe" means is the apparatus's business, and may differ between a clean stop
  and a fault abort.
- **It keeps its own record**, the same way cameras are responsible for their
  own recording. Timing that matters experimentally is consolidated later. The
  frame metadata table is the wrong home: that belongs to frames.
- Its simulated implementation is a `SerialPort` port with a fake adapter, with
  the same contract-suite treatment as `SignalOutput` — not the trigger bus,
  since it has no line to watch.

**Simulated triggers: a trigger bus** *(decided 2026-09-19, simulation only)*.
`SimulatedSignalOutput` already keeps every DO sample written and computes what
has played from the clock; the bus turns rising edges on a configured line into
events at the virtual time they play. Edges are computed at write time but
become events only after `go()` (writes happen during pre-roll); a stop cancels
anything pending past the stop point. Devices subscribe **by line label**, so
the uEye case works unchanged: an unclaimed line still plays, and a line two
devices claim reaches both. The rig composition root builds the bus and hands
it to both sides. Real hardware has no equivalent object — the wire is the bus.
It exists so that an end-to-end test's frame count is *caused by* the trigger
count rather than by a fake told how many frames to make.

**Flow is one-way** *(user, 2026-09-19)*: nothing returns from a device into
the timing loop. No exposure-active or frame-ready line feeding back to the
DAQ, and the timing source waits on nothing. Bidirectional feedback would be a
very different system.

### Placement and transport

`ActorRef` is **location-transparent**, with pluggable transports chosen at the
composition root:

| Transport | Use |
|---|---|
| `LocalTransport` (asyncio.Queue) | same process; keeps the test suite fast and deterministic in virtual time |
| `ProcessTransport` | fault isolation for vendor-C++ boundaries (DCAM above all) |
| `ZmqTransport` | cross-machine, and language-neutral clients |

Multiprocessing is **not an alternative actor model — it is a placement
decision**. Its usual cost, serializing bulk data, does not apply here: the data
path bypasses messages entirely, so only small control traffic crosses
boundaries. The largest message is a waveform retune at roughly 200 KB, which
pickles in well under a millisecond. **`spawn` is forced on every platform**, not
just Windows where it is the only option: it makes behaviour uniform across OSes,
and `fork` in a multithreaded process is unsafe — which matters considerably more
under free-threading. Actors are therefore constructible from plain config rather
than live objects, the same discipline invariant 4 already requires. The payoff is §3.6's explicit goal: a segfaulting
driver kills one process, not the engine, and process-exit watching is a *more*
reliable death signal than an in-process callback.

**Priority survives any transport** because it is enforced at the actor, not on
the wire: messages are received from whatever transport and pushed into the local
`PriorityQueue`. ZeroMQ has no priority queues, and does not need them.

**Liveness is heartbeat-based**, not callback-based. In-process task callbacks
cannot detect a dead remote actor, so `ChildExited` is driven by a heartbeat
timeout that works identically across thread, process and machine boundaries.
Designing this in is cheap; retrofitting it is awkward.

Distribution stance: **build cross-process now, cross-machine later.** The
process transport is implemented and tested so the DCAM fault-isolation benefit
is real; the network hop is an additive transport requiring no core changes.
Note the flows distribute differently — control crosses machines freely, **data
cannot** (shared memory is machine-local, so a remote tracker consumes its own
local buffer and only its *results* cross the network), and timing crosses as
hardware signals only. No discovery mechanism: static endpoints in config.

### Session model — the "M" in MVC

Plain state machine and dataclasses. No asyncio, no hardware, no UI. It is the
**only** place that knows a cycle means "N slices × C channels via image
splitter, plus trailing frames to discard", and derives trigger counts and the
geometry descriptor from that.

**The discarded frame is hardware-intrinsic, not configuration.** To stay
hardware-timed at high speed the camera must expose continuously, including
through scan reset, so every cycle yields one more frame than usable slices. The
producer emits `slices + discard` frames and neither knows nor cares that one is
unusable; only a consumer assembling volumes discards it.

*Noted 2026-10-01 (G13), for when this is designed; not decided.* G13's
Flash timing was in synchronous readout (see "The rig config"). There, N
pulses give N − 1 frames, so COOLDOWN plays one closing pulse. Each cycle's
first frame is exposed from the previous cycle's last pulse, about 11 ms
against 1.3 ms for the rest. That first frame is the one exposed through
the gap, as the discarded frame above is. How the session model counts
pulses, frames and discards for this mode is open.

### External control surface

Protocol first, transport later. `core/protocol/` defines the message types as
plain data — START (cycle count, root filename), STOP, config updates, state
publications — and milestone 1 drives them in-process. **ZeroMQ** is the wire
format when the gateway lands: it is what Pycro-Manager already uses, so hook
code lives in a familiar world, and it handles state publication as naturally as
commands.

`StartAcquisition` carries a **cycle count (negative = infinite) and a root
filename (empty = do not record)**, per §3.4.1. Client→Coordinator messages
(`StartAcquisition`, `StopAcquisition`) are distinct types from
Coordinator→device messages (`StartDevice` → `DeviceReady`, `StopDevice` →
`DeviceStopped`). The shipped LabVIEW collapsed this to a `Repeat?`
boolean; we take the thesis version.

## Buffer redesign

Free to break the old contract. The header describes **only what the producer
knows**, and is generic across producers:

```
magic u32 | version u32 | header_bytes u32 | n_frames u32
frame_bytes u64 | data_offset u64
dtype_str char[8]  (numpy typestr, e.g. '<u2')
ndim u32 | shape u32[6]
--- padding to a cache line ---
frames_written u64
```

Each change from the legacy 24-byte header earns its place: **u64 counter** (the
old `c_int` overflowed at 2^31); **dtype + n-dim shape** instead of fixed
width/height (supports float32 MIPs, other geometries, non-image producers);
**magic + version + header_bytes** (consumers detect mismatch instead of
misreading); **`frames_written` on its own cache line** (no false sharing).
**No volume or channel fields** — those are semantics and travel on the control
flow.

`multiprocessing.shared_memory.SharedMemory` provides this, and the §3.6
UI-thread quirk simply disappears — there is no privileged thread to fight.

### OS-agnostic buffer management

The instrument is Windows, but nothing in the buffer design may assume it.
`SharedMemory` is already cross-platform stdlib — Windows uses
`CreateFileMapping`/`OpenFileMapping`, POSIX uses `shm_open` + `mmap`. The
portability traps are elsewhere, and five are real:

1. **Lifetime semantics differ fundamentally.** On Windows the mapping is
   refcounted by the kernel and vanishes when the last handle closes, so a
   crashed producer leaves nothing behind. On POSIX the segment **persists in
   `/dev/shm` until explicitly unlinked**, so a crashed producer leaks it and the
   next producer finds a stale segment. The producer must therefore unlink
   defensively on create, and ownership must be explicit rather than inherited
   from platform behaviour.
2. **The POSIX `resource_tracker` will fight us.** It tries to clean up segments
   and can unlink one still in use by another process — which is exactly our
   producer/consumer split — while being a no-op on Windows. Python 3.13 added
   `track=False` for precisely this; on 3.14 we use it.
3. **Memory ordering is not free outside x86.** x86-64 is TSO, so a plain store
   to `frames_written` after the frame data happens to be safe. **ARM is weakly
   ordered and it is not** — the counter store can be reordered ahead of the
   pixel writes, handing a consumer a torn frame. Under free-threading there is
   no GIL providing incidental ordering either. Mitigation: `frames_written` is
   updated *only* through a single `publish(n)` method that carries the
   release barrier, so the platform-specific concern is localized to one function
   and the consumer's read path stays untouched.
   **Corrected and decided 2026-09-19:** the consumer is *not* untouched. On ARM
   it needs an acquire when reading `frames_written`, and its post-copy re-read
   (to detect the ring lapping it) needs ordering too. So the seam is a pair,
   `publish(n)` / `published()`, and nothing else touches the counter. **Decision
   (user): option A.** Both are plain stores/loads, correct on x86-64 because it
   is TSO and every Python-level store is an opaque C call the compiler cannot
   reorder across. The buffer refuses to start on any machine that is not
   x86-64, so the limitation is explicit, not silent. The upgrade path, if ARM
   ever matters, is a tiny C11 helper (`atomic_store_explicit` /
   `atomic_load_explicit`) called via ctypes, which changes only those two
   functions. Rejected: a cross-process lock, because a consumer holding it
   would stall the producer; and thin PyPI atomics packages, which have
   unverified `cp314t` wheels and the same risk profile as `pyDCAM`.
4. **Alignment constants must be runtime, not hardcoded.** Page size varies
   (4 KiB typical, 16 KiB on ARM macOS), so `data_offset` aligns to the runtime
   page size. Cache lines are 64 bytes on x86-64 but 128 on some ARM, so the pad
   isolating `frames_written` uses the conservative 128.
5. **Names must be portable.** Windows wants a kernel-namespace name, POSIX maps
   to a `/dev/shm` filename, and macOS `shm_open` caps names at 31 characters
   including the leading slash. Buffer names are therefore `[A-Za-z0-9_]`, capped
   at 30 characters, normalized in one place.

## Hardware facts — configuration, not code

| Signal | Channel |
|---|---|
| ETL | `Dev1/ao0` |
| Scan1 | `Dev1/ao1` |
| Scan2 | `Dev1/ao3` |
| Scan3 | `Dev1/ao2` |
| Scan4 | `Dev1/ao4` |
| HamamatsuCamTrigger | `Dev1/port0/line0` |
| LaserEnable | `Dev1/port0/line1` |
| ThorCamTrigger (uEye) | `Dev1/port0/line2` |

All AO 0–5 V RSE, 100 kS/s, continuous, no regeneration, digital slaved to
`ao/SampleClock`. **Scan2→ao3 and Scan3→ao2 are transposed** — physical wiring
that can change, so config carries both channel and a human-meaningful label.
`Scan1..4` are not names to bake into code.

**No AO contention, and a single timing authority.** Only one µManager config is
loaded at a time; the operative one is `tiger_uEye.cfg`, which loads only the
Tiger stack plus the uEye — no `NIMultiAnalogOutHub`, no Hamamatsu. PLogic is
configured but unused, so the NI board is the sole timing authority and the
timing path is not split.

## Toolchain

**uv**, targeting **Python 3.14 free-threaded (`3.14t`)**, with vendor adapters
as optional extras behind lazy imports. `uv sync && uv run pytest` must pass
green with zero hardware and zero vendor SDKs installed.

Free-threading is viable because dropping pymmcore removed the only hard
blocker. Verified: `pyzmq` ships `cp314t`/`cp315t` Windows wheels, `nidaqmx` is
pure Python (`py3-none-any`) and so ABI-agnostic, our DCAM layer is ctypes, and
numpy ships FT wheels. **Verify at first install:** `pydantic-core` (Rust/PyO3)
FT wheel availability, and ctypes behaviour under FT. If either fails, fall back
to the standard 3.14 build — the code is written to be correct on both.

**Why it matters, precisely:** FFI calls already release the GIL, so a blocking
`nidaqmx` or DCAM call never stalls other actors. The real exposure is CPU-bound
*Python bytecode*, which yields the GIL only every `sys.setswitchinterval()` —
**5 ms by default**. That is multi-millisecond scheduling jitter injected into
the refill loop by unrelated Python work in the same process, and free-threading
removes the coupling structurally. Accordingly: no reliance on GIL atomicity for
shared mutable state, and explicit locks where it is shared. Note the process
boundary remains the stronger isolation guarantee; this is belt-and-braces.

Development happens on this machine against simulated adapters only; the control
PC runs the real thing. (As of 2026-09-18, uv and CPython 3.14/3.14t are
installed here — see CLAUDE.md.)

## Development method

**Red/green TDD throughout.** The mechanism that makes "new hardware requires
minimum interaction with core logic" enforceable rather than aspirational is the
**port contract test suite**: one suite per port, which the simulated adapter and
every real adapter must pass. Adding a camera means writing an adapter and
running the existing suite against it.

Hardware safety stands: Claude writes and analyzes; the user runs anything that
actuates the microscope.

## Milestone 1 — actor core + simulated DAQ timing

No data path, no hardware. Written test-first, in this order *(revised
2026-09-18 for the Coordinator/source split; original test numbers in
brackets)*:

Actor runtime
- R1. Priority mailbox: a STOP enqueued behind 100 LOW self-messages is handled next; FIFO within a priority; unorderable payloads never compared. [1]
- R2. `ask` returns the reply; with no reply it times out in virtual time. [2]
- R3. `teardown` runs even when `run` raises. [4]
- R4. A child that raises posts `ChildExited(ref, exc)` to its parent at URGENT. [3]
- R5. Heartbeat timeout produces `ChildExited` for an actor that stops responding without raising. [10]

Protocol
- P1. Every message type round-trips through the serializer unchanged — the test that keeps the process and network transports adoptable. [9]

Timing source base
- T1. Phase progression is enforced and published; out-of-order commands are rejected.

Coordinator (against a no-op `SimulatedTimingSource`, proving AO-independence)
- C1. `ChildExited` for a participant drops it from the registry. [3]
- C2. READY timeout aborts: ready participants get `StopDevice`, the source never gets `Go`, the laggard stays registered. [2]
- C3. The source gets `Go` only after every participant, itself included, is READY. [11]
- C4. `StopAcquisition` → `Finish` → `SourceFinished` → `StopDevice` to all → Idle.
- C5. Source loss → Faulted; `StartAcquisition` refused.

`WaveformSource` (against simulated `SignalOutput`)
- W1. The simulated timebase records WARMUP, MAIN×n, COOLDOWN contiguously — no gap at the MAIN→COOLDOWN handoff. [5]
- W2. n cycles emits exactly n MAIN waveforms; negative n runs until `Finish`, then cools down seamlessly. [6]
- W3. Advancing the timebase without a refill surfaces an underflow error rather than a silent gap. [7]
- W4. Trigger lines configured but unclaimed by any actor are still emitted (the uEye case). [8]
- W5. Empty WARMUP/COOLDOWN is handled cleanly. [replaces 12]
- W6. `SetWaveform(MAIN)` mid-run takes effect at a cycle boundary; no torn cycle.

Original test 12 (config rejects a camera source with warmup/cooldown) is
dropped with the old invariant 5.

## Milestone 2 — data flow

*Approved 2026-09-19; all ten green on `main` the same day (commits 7310e95 to 03e5367, benchmark 3ed45b7). B8 found the `in_flight` >= 1 correction. B7's POSIX paths are verified only by the Linux CI leg.*


- B1. Layout: the producer defines shape, dtype, strides, padding and `frame_offset`; the consumer's `frames` array shows exactly what was written; a reader using only header fields (as a non-Python consumer would) reads the same frame.
- B2. Capacity: a layout larger than the configured capacity is rejected, a smaller one is fine; a layout change bumps `layout_generation` and consumers re-map.
- B3. Counting: `frames_written` keeps counting up across acquisitions; a new acquisition sets `acquisition_id` / `acquisition_start` and writes from slot 0; consumers detect the change.
- B4. `next()` returns frames in order; a lapped consumer gets `Overrun(skipped)`, counting in-flight frames; `still_valid()` is right at the boundary.
- B5. `latest()`, and `latest_group(P)` aligned to `acquisition_start`, across the ring's wraparound, and as a contiguous slice when `n_slots % P == 0`.
- B6. Metadata: `sequence` matches each frame; a `hw_frame` gap tells a camera drop from a lap; a header mask says which fields are present.
- B7. Lifetime: a stale segment is removed on create; with `track=False` a consumer exiting leaves the segment alone; the producer's close unlinks it; names are normalized. Runs on both CI legs.
- B8. A consumer in a separate process reads frames from the producer.
- B9. The buffer refuses to start on anything other than x86-64.
- B10. The test producer draws a small moving bar and fills metadata; `wait()` polls.

The benchmark (tiers USB 2 → 10 GB/s) is a script run on demand, not part of the pass/fail suite.

## Milestone 3 — device actors against simulation

*Approved 2026-09-19 after the decisions in "Devices — the participant side";
all twelve green the same day (commits 603f640 to 797ef34), 293 tests on both
interpreters.*

Milestone 1 built the Coordinator's half of the device protocol; this builds
the device's half, and joins the control flow to the data flow for the first
time. Still no hardware and no SDKs.

Device base (`TimedDevice`, `core/devices/`)
- D1. `StartDevice` when idle arms and replies `DeviceReady`; a second while armed is `Rejected`; `StopDevice` answers `DeviceStopped` from every phase.
- D2. An `arm` that raises is answered `Rejected` and leaves the device idle and reusable; a raise anywhere else publishes a `Fault`.
- D3. `teardown` releases the hardware when the actor dies mid-acquisition.

Registry, timeouts and failure
- D4. Four participants with different ready delays and different per-device timeouts: `Go` waits for the slowest, and a device inside its own timeout is not aborted by a shorter one elsewhere.
- D5. A `required` participant lost mid-run aborts the acquisition; an optional one is dropped and the run continues. While a required one is lost (mid-run or while idle), `StartAcquisition` is refused, naming it, until `Relaunch`; a lost optional one is left out *(user, 2026-09-24)*.
- D6. A live device's `Fault(fatal=True)` mid-run stops everyone and returns the Coordinator to Idle — the next `StartAcquisition` is accepted, because the state is not latched.

Root filename
- D7. Each device joins the shared stem to its own save target; an empty root means do not record, and the device still replies `DeviceReady`.

Software-timed participation
- D8. The Coordinator republishes `SourcePhase`; a subscribed software-timed device starts its routine on the phase it chose — a shutter at WARMUP, a driven variable at RUN — and neither is in the timing flow.
- D9. A routine runs on the clock through the acquisition; a stop mid-routine drives the device to its safe state before `DeviceStopped`; a slow `arm` does not delay another device's READY (the handshake is concurrent).

The streaming seam
- D10. The buffer is created at device launch and lives for the device's lifetime; `StartDevice` configures the layout and starts a new acquisition; a consumer already holding the segment follows without reopening.
- D11. `StopDevice` leaves the buffer intact and readable until the next acquisition overwrites it.

End to end
- D12. Coordinator + simulated source + a simulated camera on the trigger bus + a software-timed device: `StartAcquisition(n)` → n cycles → `SourceFinished` → stop, with a consumer reading exactly the frames the triggers caused.

Deferred out of this milestone, with reasons: the `stop_at =
"cooldown" | "finished"` hook for cameras needing a software stop before a
final trigger (wanted, but it should be tested against hardware); and a
`SerialPort` port with a contract suite (the port earns its place when the
real pressure box lands and there is a second implementation to swap — the
Milestone 3 software-timed device needs only the `Clock`).

**What it built.** `core/devices/`: `TimedDevice` (IDLE ↔ ARMED), and three
mixins that compose — `Recording` (the stem becomes this device's file),
`SoftwareTimed` (a routine on the clock, started by a chosen phase),
`ImageStreaming` (a buffer for the device's lifetime). `adapters/simulated/`
gains `TriggerBus`, `SimulatedCamera` and `SimulatedPressureBox`.

**Mixin order matters and is now fixed by test.** `ImageStreaming.arm` configures
the buffer and starts the acquisition *before* the chain arms the hardware,
because a driver is pointed at the frame memory as it arms and no frame may
arrive before its acquisition exists. `Recording.arm` goes the other way,
opening its file after the chain returns. So
`class Camera(Recording, ImageStreaming, Hardware)` configures the buffer, arms
the camera, then starts the recorder — which is the DCAM order.
*Corrected 2026-09-24:* it is not. DCAM binds its recorder after attaching
the buffer and **before** starting capture, and closes the file after capture
stops, so the recorder opens in the middle of the hardware's arm. The
legacy `Start Acq` does the same. The user decided that the device, not the
mixin, chooses where in `arm` the file opens (see "5b"). *Resolved 2026-09-24:* the device lists
`ImageStreaming, Recording` in that order, and the order of the bases is the
order of the calls (see the `Recording` entry in Milestone 5's status). With
`Hardware` as a base after `ImageStreaming`, `ImageStreaming.close` would also unmap
the segment before the hardware lets go of it; F5 pins that order.

**Found while building it**, both by mutation checks rather than by the
suite: (1) `ActorRef` is equal by address, so re-subscribing a relaunched
actor kept its dead predecessor's ref as the dict key and published into the
void — the runtime now replaces a stale key; (2) an exception from
`close_recording` skipped whatever an adapter does after its own
`super().disarm()`, so a full disk could have left a shutter open — a file
that will not close is now published as a fatal fault, never raised.

## Milestone 4 — process transport

*Approved 2026-09-19, with the four decisions below; all eleven green the same
day (commits 20d1404 to 3bb10ef), 331 tests on both interpreters, CI green on
all four legs.*

§3.6's explicit goal: a segfaulting driver kills one process, not the engine.
Proving it in simulation means the DCAM adapter lands into a boundary already
known to work.

**This milestone adds no new policy.** Every failure rule already exists and is
tested — `required` aborts, an optional device is dropped, `Relaunch` is manual,
exactly one `ChildExited` per child. M4 proves the boundary is *invisible*: the
same events arrive, and the Coordinator cannot tell where a device lives.

Three pieces of the runtime turn out to be positioned for this already.
`ChildWatch` never touches the child actor, only its `ActorRef`, so supervision
and the exactly-once `report()` work unchanged over a wire. The `Codec` already
refuses what cannot cross and already encodes an `ActorRef` as its address with
a `resolve` hook on the way back — built for this, never yet used. What is
missing is that `spawn()` hardcodes `LocalTransport` and `spawn_child` takes a
live actor; a process child must be built from plain config in a fresh
interpreter, which is the discipline invariant 4 already demands. The `Envelope`
wrapper and `Priority` are also outside the codec, so the wire frame is
`(priority, reply_to address | None, encoded msg)`.

**Decisions (user, 2026-09-19):**
1. **Connect to an endpoint**, not an inherited handle. The parent listens; the
   child is given an address on its command line. Cross-process and
   cross-machine then differ only in the endpoint string, so the network
   transport is additive rather than a rewrite. The cost, accepted: the engine
   opens a listening socket.
2. **Real-clock tests are accepted** for this milestone — a real subprocess has
   a real clock — **provided nothing can hang for significant real time.** Each
   such test carries its own deadline well under pytest's 10 s; heartbeats run
   at tens of milliseconds; every child is spawned in a fixture that kills it in
   a `finally`; **a child that cannot reach its endpoint exits rather than
   waits**, as does one whose parent connection drops. No orphans.
3. **A heartbeat is mandatory for a process child**, not opt-in. The connection
   catches death, but not a wedged driver still holding its process open — which
   for DCAM is the likelier failure. The safe answer is not one a config can
   forget.
4. **The timing flow does not cross the process boundary, and will not pretend
   to.** In reality the DAQ's pulse reaches the camera's hardware whichever
   process owns the driver, so an out-of-process camera gets its own free-running
   trigger source inside the child. Consequence, noted deliberately: X10's frame
   count is timed by the child, not *caused* by the parent's DO samples the way
   D12's was. Where that matters, it is to be reserved for testing against real
   hardware with external signals rather than simulated more elaborately.

The wire (a loopback transport pair, in virtual time — most of the transport is
proven deterministically before any process exists)
- X1. An envelope round-trips: priority and `reply_to` survive, and an unregistered message is refused at the sending end, naming the type.
- X2. Priority is enforced at the receiving actor, not on the wire: frames arriving in arrival order are handled in priority order, and a `Heartbeat` is answered by the runtime without reaching the mailbox.
- X3. A ref that crosses is usable from the far side — `DeviceLaunched` carries one — and an address the local table does not know routes back to the peer. This is what makes `ask` work in both directions.

A real process
- X4. A device launches in a fresh interpreter and runs a whole acquisition — START/READY, `Go`, `StopDevice` — with assertions identical to the in-process case.
- X5. A spec that cannot be rebuilt in a fresh interpreter (a closure, a live object) fails at launch with a clear error, not silently at acquisition time.

Death — the point of the milestone
- X6. A clean child exit → exactly one `ChildExited(ref, None)`, and the name is free for relaunch.
- X7. A hard crash (`ctypes.string_at(0)` in the child — the DCAM case) → one `ChildExited` naming the exit status, promptly, without waiting out a heartbeat. The engine keeps running.
- X8. A child alive but silent (a handler that never returns) → the heartbeat fires `ChildExited`, and the hung process is terminated rather than left behind holding the driver.
- X9. `Relaunch(name)` after each of the above brings a fresh process up at the same address, and it takes part in the next acquisition — across a real boundary, where the stale-`ActorRef` bug of M3 would have bitten hardest.

Both flows across the boundary
- X10. The camera out-of-process: control over the wire, frames through shared memory, a consumer in the parent reading exactly the frames produced. The two flows cross different boundaries and meet correctly.
- X11. Its process killed mid-acquisition: `required` aborts, the buffer stays readable, and the Coordinator is Idle and answering, and refuses to start without the camera until it is relaunched (D5, changed 2026-09-24; it used to start without it).

Deferred out of this milestone: cross-machine / `ZmqTransport`; any automatic
restart policy (relaunch stays the user's call); and placement config beyond a
single field on `DeviceSpec`.

**What it built.** `core/transport/` gains the wire frame (`wire.py`:
destination, priority and reply address around an encoded message), the
address book for one link (`routing.py`: `Peer`), framing over a stream
(`stream.py`: `Connection`), the parent side (`process.py`: `ProcessHost`) and
the child entry point (`child.py`). `core/timing/launcher.py` holds `Placement`
and `Launcher`, so the Coordinator asks for a device and never learns where it
went; `DeviceSpec` gained `placement`. The runtime gained `adopt` — supervision
of a child it did not start, which `ChildWatch` already supported because it
only ever needed an address — an explicit `address` for `spawn`, a `started`
event, and a `stop()` that works from outside the actor (it wakes the mailbox,
so `teardown` runs and the hardware is released). `adapters/simulated/` gained
`FaultyDevice` and `SimulatedFreeRunningCamera`.

**Three frames of bootstrap, then only messages.** The child connects and
names its one-time token; the host sends the pickled factory; the child answers
`ready` or the reason it could not be built. So a device that cannot start says
so at launch, with the child's own error, rather than at the beginning of an
experiment.

**Found while building it.** (1) This side never closed a dead child's socket,
so shutdown waited forever on connections nobody owned — the suite hung, which
is the exact failure this milestone exists to rule out. (2) A device that
raised in another process was reported only as "exited with status 0": the
exception was being dropped at the boundary. A child runtime now gets a *last
word* (`ActorDied`, addressed to `<address>#runtime`), so a death across a link
reads the same as a death at home. (3) A bounded `wait_closed` added as a
shutdown backstop was quietly masking (1) from the mutation check; shutdown
after a death is now timed, which is what pins it.

**A free-threading finding, from CI.** On the free-threaded build a fatal
signal a process raises on *itself* is survived: neither SIGSEGV through
`faulthandler` nor SIGABRT through `os.abort` killed the child, so nothing was
reported because nothing had died. An uncatchable kill from outside behaves
identically on every leg. `Fail.CRASH` is therefore defined by its observable —
the process gone at once, no Python left running — which is all a caller ever
sees of a segfault. Worth remembering when the real DCAM adapter starts
crashing for real.

**Known gap.** One mutation survives: removing the wait that lets the link
deliver a child's last word before the death is reported. The guard is correct
and its absence is a race, but the race could not be made deterministic, and
its failure mode is a worse diagnostic rather than a wrong one.

## Milestone 5 — real adapters

*Done 2026-10-01. Staged 2026-09-19 at the user's request.*

**Milestone 5 is closed (user, 2026-10-01).** Every stage, 5a–5f, is done
on its hardware: the PCI-6733, the ORCA-Flash (with DCIMG recording) and
the uEye. 5f-5, DAQ-triggered, needed no work of its own: Z2–Z3 (the port)
and W2–W3 (the device) already ran on line 2's pulses. **Then, the same day: a recorder
that reads the buffer, for the uEye** (user), done over the fake and on the
camera; see "The buffer recorder" after 5f. The bullets below are the record
of how the milestone got here.

**Status, 2026-09-30:**
- **5f, the uEye, is decided and started (user, 2026-09-30).** See "5f —
  the uEye camera", after 5b. Its questions were answered: pyueye, no IDS
  file in the repo, one message per setting, RAW pixel formats, line 2
  wired, recording later. **5f-1, the survey, is done**
  (Y1–Y9, green on the camera). Device time is the raw stamp; RAW8 and
  RAW10 only; full-frame RAW10 triggered at 20 fps is required and holds
  (user). The shortest trigger period is its own query message, for the
  Flash too (user). **5f-2 is done**: `UEyeFrameCapture` over a fake
  pyueye (U1–U12). The camera counts triggers it ignores, so a missed
  trigger is now a lost frame (probe). **5f-3 is done on the camera**:
  K1–K9 and Z1–Z4. The driver zeroes memory it is given, so the port now
  says a slot is defined only once a frame is reported in it. **5f-4 is
  done**, over the fake and on the camera: `UEyeCamera`, its messages, and
  the shared `GetShortestTriggerPeriod` in core. **The Flash answers the
  same query** (O12 over the fake), from its own `TIMING MIN TRIGGER
  INTERVAL`. **5f is done**: 5f-5, DAQ-triggered, was covered by Z2, Z3,
  W2 and W3, and closed with the milestone (2026-10-01). The user kept `GetCameraSettings` (2026-10-01).
- **The Flash suite again, on the camera (2026-10-01, run by Claude with
  the user's permission).** `tests/hardware/dcam` with the DAQ trigger on
  `Dev1/port0/line0`: 170 passed, 6 skipped, in 605 s. V6 (the new
  query): 11.079 ms at 1 ms exposure, 20.083 ms at 10 ms, each DCAM's
  `TIMING MIN TRIGGER INTERVAL`. K1–K9 pass with the sentinels laid after
  attach; M1 is still depth 1 in all nine conditions; M2 4008 frames in
  5 s at 801.7 fps, none lost; V4–V5 as before.
- **Unknown messages are ignored, told or asked (user, 2026-09-30).** See
  "Actor core". `TimedDevice`, `TimingSource` and the Coordinator each
  published a Fault for one. Four tests went red first, one per actor, and
  D12's `a_clean_acquisition_raises_no_fault` for the `MainApplied` stray.
  Suites: default 559 on 3.14t and 3.14; DCAM 297 passed, 83 skipped.
  - **Still relayed:** the Coordinator republishes `SourcePhase` to
    participants. The user agrees a software-timed device should subscribe
    to the source directly. Whether the Coordinator gives the device the
    source's address, or gives the source the device's, is to be decided
    later. See "Open items → The communication model".
- **The frame count told to devices has been taken out (user, 2026-09-30).**
  The approach mixed waveform logic and camera logic too much. The user may
  revisit it later. A recording's length is now always indeterminate: the
  DCIMG limit is the free space less headroom (the larger of 100 GiB and
  10% of the disk), capped by `max_record_frames`, and a disk with no room
  for one frame refuses the arm.
  - Removed from the core (all of `9f1bfa5` but the Recording fix):
    - `StartDevice.frames`, `DeviceSpec.trigger`, and `CountPulses` /
      `PulseCounts`;
    - the Coordinator's `_pulse_counts`, `WaveformSource`'s edge counting,
      and `TimedDevice.expected_frames`;
    - `SimulatedTimingSource(pulses_per_cycle=)` and
      `SimulatedDevice.started_with`.
  - Tests removed with it: C6 (6), W14 (4), D13, the D12 count test, and
    the codec round trips of the two messages.
  - Removed from the Flash: the known-length branch of `_frame_limit` and
    `_frames_known`. O9 lost its four known-length tests. V6, the
    DAQ-pulsed camera test of a file sized to the count, is deleted.
  - **Kept:** `Recording.arm` names the file before anything arms, and
    its D7 test.
  - The 5e bullets below on the "known length" and on "how a device
    learns the length" are **superseded**. They are kept as the history of
    what was built and why.
  - Suites: default 555 on 3.14t and 3.14; DCAM 297 passed, 83 skipped
    (no SDK variables set); NI on `Dev1-sim` 99 passed. A mutant in
    `_frame_limit` was caught by O9.

**Status, 2026-09-25:**
- **5e, DCIMG recording: done, green over the fake and on the camera and
  DAQ (2026-09-25, fourth session; the user allowed Claude to run the
  camera and the NI hardware). Not committed.**
  - **The frame limit, decided after V4 (user, 2026-09-25), in order:** the
    acquisition's length when known; else the free space less headroom,
    the larger of 100 GiB (the legacy's constant, read from
    `HamamatsuFluor_v2` Start Acq: free − 100 × 2^30, over the frame size;
    a Frame Count ≥ 0 took precedence, and Max frames capped the rest) and
    10% of the disk; a config cap (`max_record_frames`) on either. A known
    length is used only when the Flash's TRIGGER SOURCE is EXTERNAL (on its
    own trigger it takes frames the line never pulsed; mine, flagged). A
    known length the disk cannot hold refuses the arm (mine, flagged).
  - **How a device learns the length (user's choice of three): in
    `StartDevice`.** Core changes, test-first:
    - `StartDevice(root_filename, cycles, frames=-1)`;
      `CountPulses(cycles)` answered `PulseCounts(lines, counts)`;
    - `DeviceSpec.trigger`: the DO line label that triggers the device
      (the plan's "registers against an already-configured trigger
      channel");
    - the Coordinator asks the source before `StartDevice`, bounded by the
      source's ready timeout; a `Rejected` or a timeout leaves every count
      -1 and the start goes on;
    - `WaveformSource` counts rising edges from low, over WARMUP, each
      queued MAIN for a cycle then the last one taken, COOLDOWN (a pulse
      held across a join is one); -1 until stopped or with no MAIN. A
      waveform changed mid-acquisition makes the count stale: a file then
      stopped short is the fatal fault at the stop;
    - `TimedDevice.expected_frames`, set before `arm` (an attribute, so no
      role's arm signature changed);
    - `SimulatedTimingSource(pulses_per_cycle=...)` counts;
      `SimulatedDevice.started_with` records each `StartDevice`.
    - Tests: D13, C6 (6), W14 (4), and a D12 end-to-end (the count told
      equals the frames the wire delivers). 13 mutants, all caught (two
      after strengthening W14's queue test).
  - Device: O9 rewritten (9 tests, a simulated disk throughout the module
    so no test depends on the disk it runs on); 12 mutants, all caught.
  - **V6 on the camera and DAQ:** EXTERNAL trigger, told 20 frames, 20
    pulses on `Dev1/port0/line0`: the file was 20 973 176 bytes at open
    (sized to 20 frames), held all 20, frame stamps 0–19, no fault. The
    uncapped run it replaced is gone: it would reserve most of E:.
  - **Decided (user, 2026-09-25):**
    - a file's frame limit comes from the free disk space (as the legacy
      and the SDK sample size it), and config can lower it
      (`max_record_frames`);
    - a short file is fatal, checked at the stop: fewer frames than were
      published, any the recorder missed, or a recorder still writing when
      the file closed;
    - the save directory comes from config and from a message,
      `SetSaveDirectory(path)`, answered `SaveDirectory(path)`;
    - files are read back through the DCIMG SDK (the user downloaded it,
      `Downloads/Hamamatsu_DCIMGSDK_v24026764`);
    - device test files go to `E:` (HSDATA) on this machine.
  - **Found on the camera: DCIMG preallocates its whole file at open.**
    V4, 256 rows, 50-frame limit: 52 431 416 bytes before the first frame
    (50 × (1 048 576 + 64) plus headers), 10 487 616 at close for 10
    frames. So a limit from the free space makes every recorded
    acquisition claim the whole disk at arm (2.7 TB on E:), and a process
    that dies before the close may leave a file that size. Arm took
    0.13 s for 50 MB; how long a 2.7 TB open takes is unmeasured. The
    legacy sized its files the same way, so it presumably did the same.
    Settled above.
  - **Also found:** `dcimg_copyframe` refuses a frame without its pixel
    type (`DCIMG_ERR_INVALIDVALUE`), though the header marks the field
    reserved; the SDK's sample sets it. The DCIMG SDK's licence has the
    DCAM SDK's terms (1.3, 1.4), so the same policy applies.
  - **Binding:** `dcamrec_openW`, `dcamrec_status`, `dcamrec_close`,
    `dcamcap_record`, `DCAMREC_OPENW`, `DCAMREC_STATUS`; conformance passes.
  - **Adapter (Q13, 17 tests):** `DcamFrameCapture.record(path,
    max_frames)` between attach and start, and `close_recording(timeout)`
    after stop, which waits for the recorder to finish and returns a
    `RecordingStatus`. Detach and close close an open file first. 15
    mutants, all caught.
  - **Device (O8–O12, 19 tests):** `OrcaFlash(ImageStreaming, Recording,
    TimedDevice)`. The arm is refused, arming nothing, when no directory
    is set, the file exists (never overwritten), or the disk has no room
    for a frame. A missing directory is refused at launch and by message.
    15 mutants, all caught.
  - **Core fix, found while writing the device:** `Recording.arm` named
    the file after the chain below had armed, outside its undo, so a
    `target` that raised left the device armed. It now names the file
    first. New D7 test, red first.
  - **The fake** gained a recorder (`recordings`, `recorder_lag`,
    `miss_recording`) and the `RECORD FIXED BYTES` properties, with the
    dev box Flash's sizes.
  - **The camera (`tests/hardware/dcam/test_recording.py`):**
    - V4: 10 frames recorded; the file holds every published frame pixel
      for pixel, with the same frame stamps and time stamps; arm 0.13 s,
      stop 0.06 s;
    - V5: a 3-frame limit and 5 frames: a fatal Fault "holds 3 of the 5
      frames published"; the file is readable, with frame stamps 0–2;
    - V6: see above.
    - The reader, `tests/adapters/dcam/dcimg_reader.py`, also read a
      legacy file on E: (1829 frames, 256 × 1536).
  - Suites: default 569 on 3.14t and 3.14; DCAM 382 passed, 2 skipped with
    both SDKs set (`STACSCOPE_DCIMG_SDK` for the reader's conformance);
    camera V1–V6 all passed.
- **The `OrcaFlash` device, green over the fake and on the camera
  (2026-09-25, third and fourth sessions).** Committed as `38be552`; the
  camera run and the fake's BUSY change after it.
  - **V1–V3 on the camera: 3 passed** (the user allowed Claude to run the
    camera, fourth session). Each launch took 2.2 s.
    - **DCAM opened in a worker thread works from the loop's thread.**
    - **A plain number has no label on the Flash** (EXPOSURE TIME's comes
      back `None`, as over the fake).
    - `SetProperty("EXPOSURE TIME", 0.0123456)` applied 0.0123461;
      `SetPropertyLabel("TRIGGER SOURCE", "SOFT")` is refused, naming
      INTERNAL, EXTERNAL, SOFTWARE, MASTER PULSE.
    - V2: 5 frames at 256 rows, frame stamps 0–4, in flight 2, no Fault.
    - **While capturing the Flash takes an exposure change** (0.002 applied
      as 0.0020073) **and refuses `SUBARRAY VSIZE` with `DCAMERR_BUSY`**,
      not the ACCESSDENY the fake gave. The acquisition went on (2 frames,
      no Fault). The fake now answers BUSY while capturing and ACCESSDENY
      while attached and stopped (untried on the Flash); O3 checks BUSY.
      The format guard never fired on the camera.
  - **Decided (user, 2026-09-25):**
    - settings by name;
    - settings may change while armed where DCAM takes them, and a change
      to the frame format is refused;
    - config settings are applied in order at launch;
    - `Recording` waits for 5e.
  - **The codec can't carry `float | str`**, so the value and the label
    travel in separate fields (user's choice among three). The messages,
    in `adapters/dcam/messages.py`:
    - requests: `SetProperty(name, value: float)`,
      `SetPropertyLabel(name, label: str)` and `GetProperty(name)`;
    - every answer: `PropertyValue(name, value, label | None)`, or
      `Rejected` with DCAM's text.
  - **The device** (`adapters/dcam/camera.py`): `OrcaFlash(ImageStreaming,
    TimedDevice)`, over `DcamFrameCapture`, with `in_flight=2` by default
    (`IN_FLIGHT`).
    - `settings=[(name, value), ...]` is applied in `setup`, in order,
      before the segment is created. A refusal fails the launch, with a
      note naming the setting, and the camera is closed.
  - **Found and fixed: adapter messages could not cross a process
    boundary.**
    - The child builds `Codec(MESSAGES)` before it unpickles the factory,
      and the factory's import is what registers the adapter's messages.
    - Fixed in the codec (user's choice): a type registered later gets the
      same check the first time it is encoded or decoded (`_hints_for`).
      Two new P1 tests, red first.
  - **Additions of mine, flagged for review:**
    - **The format guard.** While armed, the device compares the frame
      format before and after every change. The reference doesn't list
      which settings DCAM refuses while memory is attached, and a larger
      frame in slots laid out for a smaller one is a memory overrun. A
      moved or indescribable format is put back, answered `Rejected`, and
      a fatal `Fault` is published.
    - **DCAM is opened in a worker thread** (`asyncio.to_thread`). Opening
      takes seconds, and heartbeats are answered on the loop, so a camera
      opened on the loop in its own process would read as hung. If the
      setup is cancelled, it still waits for the thread, so teardown
      closes the camera.
    - **Untested on the camera: DCAM opened in one thread and used from
      another.** V1 checks it.
  - **The fake gained `SUBARRAY VSIZE`** (writable, reshapes the frame,
    `reshape(height)`). `SENSOR MODE` and `SUBARRAY VSIZE` are refused
    with `DCAMERR_ACCESSDENY` while frames are attached, as the header
    describes it: "cannot access during this DCAM STATUS".
    `refused_while_attached` can be emptied.
  - **Tests:**
    - `tests/adapters/dcam/test_orca_flash.py` (O1–O7) and
      `test_role_contract.py` (F1–F6 on the device, two formats).
    - 12 mutants, all caught. The first O6 heartbeat test was vacuous: the
      runtime answers a heartbeat on receipt, before `setup` runs. It now
      waits until the open is under way, and fails if the open's hold
      timed out.
    - Suites: default 554 on 3.14t and 3.14; DCAM 315 passed, 2 skipped
      with the SDK set.
  - **The camera suite: `tests/hardware/dcam/test_device.py` (V1–V3).**
    - V1: settings by message on a camera opened from a thread, including
      what the Flash says of a plain number's label (the fake:
      NOVALUETEXT).
    - V2: 5 software-triggered frames at 256 rows, published and stamped.
    - V3: while armed, whether the Flash takes an exposure change and
      refuses a subarray.
  - The earlier "`DcamCamera`" in "5b" below is this `OrcaFlash`.
- **5d: the adapter on the camera, done and green (2026-09-25, second
  session; the user allowed Claude to run the camera and the DAQ).
  Committed as `e489314`.** `tests/hardware/dcam/`, gated on `STACSCOPE_CAMERA` (the
  DCAM index), skipping without it; the DAQ condition also needs
  `STACSCOPE_CAMERA_TRIGGER=Dev1/port0/line0` and `--extra nidaqmx`.
  Every setting a test changes is put back (`dcam_rig.camera_with`), even
  after a test closes the capture; checked afterwards: AREA, INTERNAL,
  subarray off, 10 ms.
  - **S1–S6, the real DLL** (`test_open.py`): our bindings and structs work;
    a closed session opens again; a missing camera is refused and DCAM
    still opens; DCAM's error text comes through. C13440-20C S/N 300661,
    firmware 4.20.B, driver 8.13.30.7073. The camera reports both frame and
    time stamp capability.
  - **The adapter gained settings, test-first (Q11, Q12)**, as adapter
    methods, not port methods (settings are the device's own):
    `get(name)`, `label(name)`, `labels(name)`, `set(name, value)` (a
    number or the camera's label; returns what DCAM applied, rounded) and
    `fire_trigger()`. Bound: `dcamprop_setgetvalue`, `dcamprop_queryvalue`,
    `dcamcap_firetrigger`; conformance passes for all three. Labels are the
    camera's (walked PRIOR/NEXT from the current value, which ends with
    `DCAMERR_OUTOFRANGE` on the Flash; the reference says OUTOFVALUE, which
    no header defines), and the fake numbers modes unlike the header.
    The Flash's label is "DUAL LIGHT SHEET", the header's "DUAL LIGHTSHEET".
    10 mutants, all caught (one after strengthening the after-close test).
  - **K1–K9 on the camera** (`test_contract.py`): 144 passed, 6 skipped (lost
    frame, which cannot be forced; unaligned slots, no alignment asked), in
    full frame, 256 rows AREA and 256 rows PROGRESSIVE, software-triggered.
    About 8 minutes (a fresh DCAM open per test).
  - **In-flight depth (M1, `test_in_flight.py`): 1 in all nine
    conditions** — full frame, 256 rows, 256 rows lightsheet; each under
    software triggers, internal free-run (801.7/s at 256 rows AREA) and
    hardware-timed DAQ pulses on `Dev1/port0/line0` (wired to the camera's
    BNC input on the dev box). `partial` > 0 in every condition: the grabber
    writes each frame top to bottom, even in AREA readout. The declared
    `in_flight` is 2 (decided below).
  - **A bias in `measure_in_flight`, fixed test-first:** it measured written
    slots against the count read *before* its scan, so frames finishing
    during the scan counted as depth (red: 2 for a true 0, 3 for a true 1).
    It now re-reads the count after the scan. New Q10 test with a capture
    that lags. Survivor, accepted: dropping the `slot < delivered` skip
    changes only `partial`, in a race the fake cannot produce.
  - **`dcamcap_firetrigger` blocks until the camera can take the trigger**
    (0.05 ms when idle; up to ~5 ms at 256 rows and ~13 ms full frame when
    fired right after a frame; no trigger lost). A first software-trigger
    M1 run read depth 0 because the tool sat in that call; the test now
    fires from a worker thread. Not changed in the adapter: `fire_trigger`
    is synchronous and can hold an event loop for up to a frame time; it
    stays so (decided below).
  - **Outside a capture the Flash refuses a software trigger** with
    `0x80000107` (DCAMERR_NOTBUSY). The fake used to accept it; it now
    refuses, and Q12 pins it.
  - **M2, 800 frames a second** (`test_behaviour.py`): 256 rows AREA,
    free-running, read as the role reads it (one `wait_frames` per frame
    plus `stamps`), ring of 200 slots: 4008 frames in 5 s, no loss, no
    frame-stamp gap, 1 of 4006 returns with more than one frame (largest
    3), 0.40 cores. Frame interval from the time stamps 1247 µs median
    (1219–1275). No `FRAMESTAMP_MISMATCH`.
  - **Open 5d questions, answered on the Flash:**
    - frame stamps **restart at 0 for every capture**, within a session and
      across sessions (M3): D12 stands;
    - **FRAMEREADY latches** (M4): a wait begun after the frame landed
      returns at once (0.1–0.7 ms);
    - **the Flash supports attached stamp buffers** (the contract's stamp
      tests pass with both);
    - **DCAM time stamps are Unix time in µs** (M3's first stamps read
      1790351444… s, 2026-09-25 ~15:50 UTC): the host's wall clock, which
      can step; `TimeSource.DRIVER` as decided, but worth knowing;
    - `dcambuf_release` per kind (M5): **not observable** — DCAM refuses
      nothing, not a repeated release, not a stamp buffer attached over one
      attached. Releasing every kind is harmless; the adapter keeps doing it;
    - whether `nFrameCount` counts lost frames, and whether
      `DCAMERR_LOSTFRAME` comes from a wait: **not answerable** without a
      loss, which cannot be forced; none occurred.
  - **Lightsheet readout at 256 rows tops out at 387/s** (software/DAQ
    trigger; 278/s internal) at 1 ms exposure; 800/s is AREA readout only.
  - Suites: default 552 passed on 3.14t and 3.14; DCAM 210 passed (265 with
    `STACSCOPE_DCAM_SDK`); camera: S 7, K 144 + 6 skipped, M1 9, M2–M5 4.
  - **Decided (user, 2026-09-25):**
    - **The Flash declares `in_flight=2`**: the observed 1 plus a margin
      of 1. The adapter keeps no default; the `OrcaFlash` device passes 2.
    - **`fire_trigger` stays synchronous.** "A software trigger is
      unreliable for fast timing anyway": fast acquisitions are triggered
      by the DAQ.
  - **Next:** the `OrcaFlash(ImageStreaming, Recording, TimedDevice)` device
    actor over `DcamFrameCapture` (with `in_flight=2`), with its own
    settings messages, then 5e. (Built without `Recording`: see the entry
    above.) Committed as `e489314`; it, `07bc7fa`
    and `bd25b63` are not pushed.
- **5c committed as `07bc7fa`** (not pushed). Three questions from the user
  were investigated (findings below), and two were decided and done:
  - **Done: DCAM slots are packed at `BUFFER FRAMEBYTES`** (user). The
    adapter's `slot_alignment` defaults to 1; an override is still
    honoured, and the DCAM contract rig's `padded` format asks for 4096
    so the unaligned-slots refusal still runs against DCAM. New Q2 test.
  - **Done: fix A for the attach pin** (user). `DcamFrameCapture.attach`,
    `SimulatedFrameCapture.attach` and `measure_in_flight` each `del
    slots` before an error leaves them. New tests: K9 in the port
    contract (a refused attach, two refusals every adapter makes), a Q3
    test of DCAM's own refusal (of the frame memory, and of a stamp
    buffer after it), and a Q10 test (a refused start, a trigger that
    raises). The two Q3 tests' `del caught` workarounds are gone.
    Mutation checks: each of the three `del`s removed, each caught.
    Default suite 552 passed, DCAM suite 196 passed, on 3.14t and 3.14.
  - **Closed: the DCAM wait slice.** The user had misread what it
    governs; `WAIT_SLICE` stays 0.1 s. Committed as `bd25b63` (not
    pushed).
  - **Slot alignment 4096.** DCAM's reference states no alignment. It says
    only that the driver transfers "directly from the device to these
    buffers" and that the size needed is `BUFFER FRAMEBYTES`. Page
    alignment has one OS use: unbuffered or direct file I/O
    (`FILE_FLAG_NO_BUFFERING`, `O_DIRECT`) needs sector-aligned buffers.
    That matters only to a recorder of ours writing slots to disk, and
    DCIMG is DCAM's own. On the Flash it costs nothing either way: at
    full width `ROWBYTES` is 4096, so every frame is a whole number of
    pages, and 2048 × 256 × 2 is exactly 1 MiB. The user's proposal is to
    let the adapter decide through frame padding, i.e. slot stride =
    DCAM's `FRAMEBYTES`, with no extra alignment.
  - **DCAM wait at 800 fps (256-row ROI).** `WAIT_SLICE` does not pace
    frames. Each wait returns on the next FRAMEREADY, about 1.25 ms at
    800 fps. The slice only bounds a stop that lands between two waits.
    Measured on the dev box (3.14t, a thread signalling every 1.25 ms, one
    `to_thread` wait per frame, as `_wait` does): frame to loop median
    80 µs, p99 155 µs, max 430 µs; overhead about 0.15–0.2 core; almost
    always one frame per publish. A late loop publishes a batch, so a stall
    costs latency, not frames, if the ring covers it: n_slots ≥ 800·T +
    in_flight, at 1 MiB per frame (200 MiB for 0.25 s). **New 5d question:**
    whether FRAMEREADY latches when it fires between two waits. If it does
    not, a frame that lands in that window waits for the next frame or the
    end of the slice. At slow trigger rates that could leave the last
    frame up to one slice late.
  - **The attach pin.** Reproduced in scratch. The role is safe today: an
    attach failure is answered `Rejected`, and its exception is gone
    before `close`. Only a device that dies keeps its error, as
    `exit_error` through `teardown`, and neither death path passes through
    `attach`'s frame. **Scripts are not safe.** `try: attach(w.slots)
    finally: w.close()` raises `BufferError` in place of the driver's error,
    which survives only as the context. It is wider with
    `measure_in_flight`, which holds `slots` itself: any failure inside it
    pins the segment (start, wait, `FramesLost`), so the 5d in-flight
    script would hit this. Options put to the user: (A) each function
    taking slots drops them on failure (`del slots` before re-raising),
    plus a port contract test; (B) also clear the traceback frames of an
    attach error in `ImageStreaming.arm`; (C) document only.
- **5c, in simulation: Q1–Q10 green against the fake, plus the header
  conformance test (2026-09-24, seventh session). Not committed.** The DCAM
  suite (`uv run pytest tests/adapters/dcam`) gives 239 passed on 3.14t and
  on 3.14 with `STACSCOPE_DCAM_SDK` set, and the conformance test's 52 skip
  without it. The default suite gives 548 passed on both.
  - Files:
    - `adapters/dcam/_api.py`: structs, constants, and `load()`.
    - `adapters/dcam/capture.py`: `DcamFrameCapture` and `DcamError`.
    - `adapters/in_flight.py`: `measure_in_flight`. It needs only the
      `FrameCapture` port, polling with `wait_frames(seen, 0)`, so it will
      serve uEye too.
    - `tests/adapters/dcam/`: `fake_dcam.py`, `test_contract.py` (Q1),
      `test_capture.py` (Q2–Q9), `test_in_flight.py` (Q10) and
      `test_conformance.py`.

    Every DCAM file carries the 1.4 marking and no copyright line.
  - Mutation checks:
    - 21 on the adapter. Three survived at first:
      - "close does not join the waiter" survived because the fake's
        aborted wait left too fast. The fake now has `linger`.
      - "no wrap" survived until a test put a gap across the int32 sign.
      - One mutant changed nothing, and its redundant mask was removed.
    - 7 on the measurement.
    - 4 on the conformance test. A last field too wide hid in padding, so
      field sizes are now compared as well as offsets.

    All are caught now.
  - **A core change: `FramesLost.lost` may be `None`**, meaning DCAM
    reported a loss without a count (`DCAMERR_LOSTFRAME`). The message then
    says "an unknown number of frames".
  - Choices made in the code, for the user to review:
    - `in_flight` is a required argument with no default: it is measured.
    - `slot_alignment` defaulted to 4096; **now 1** (user, 2026-09-25):
      slots are packed at DCAM's own `BUFFER FRAMEBYTES`.
    - The camera is chosen by index.
    - A frame-stamp difference *smaller* than the frame count is not
      flagged, only a larger one.
    - DCAM's waits run in slices of `WAIT_SLICE` = 0.1 s, so a stop that
      lands between waits is seen within one slice.
  - **Found, not changed:** an exception raised from `attach` keeps the
    `slots` view alive through its traceback, because the failed call's
    frame holds its arguments. While that exception is alive, the segment
    cannot close (`BufferError`). Two Q3 tests held theirs and failed at
    teardown; they now drop it. The same can happen in the role, if an
    arm's exception outlives the arm into `close`. That is true of any
    capture adapter, not only DCAM.
  - **Open for 5d** (the fake takes the reading that is harder on the
    adapter; the camera decides):
    - whether frame stamps restart at 0 per capture (D12);
    - whether `nFrameCount` counts lost frames;
    - whether `DCAMERR_LOSTFRAME` comes from a wait, and whether once or
      repeatedly;
    - whether `dcambuf_release(kind)` must be called for each attach kind;
    - whether `DCAMCONST_FRAMESTAMP_MISMATCH` (0xFFFFFFFF) appears, which
      would read as a large gap;
    - whether FRAMEREADY latches when it fires between two waits (if not,
      a frame in that window waits for the next frame or the slice's end;
      see the 2026-09-25 wait finding above);
    - whether the Flash supports attached stamp buffers;
    - how deep in flight it really is.
  - **Not written yet:**
    - the real-DLL smoke test, which opens the driver, so the user runs it;
    - the 5d scripts: the contract on the camera, and the in-flight
      measurement with the sentinel the user chooses.
- **Sixth session (2026-09-24): the three open questions answered and done.
  Next is 5c.** 543 passed on 3.14t and on 3.14. Committed and pushed as
  `262ba38`; CI green on all four legs (run 36054359760).
  - **The start-failure gap is closed** (user: fix it; the start's error is
    the one raised). In `ImageStreaming.arm`, a `capture.start()` that fails
    now disarms everything after the role, then detaches in a `finally`:
    for the Flash, close file, disarm camera, detach. A failure of that
    disarm is a note on the start's error, as in `disarm` and `close`. A
    failure in the chain's own arm still only detaches, since the chain
    undoes its own part. Two tests in `test_composition.py`. Mutation
    checks: 5 mutants, all caught.
  - **D12: frame numbers restart at 0 per acquisition** (user), provisionally;
    5d checks what DCAM's framestamp does on the Flash, and D12 changes if
    the camera runs on.
  - **The base-class order is enforced** (user). `TimedDevice.__init_subclass__`
    raises `TypeError` if the roles are out of the order `SoftwareTimed,
    ImageStreaming, Recording`, or if a role is listed after the adapter's
    class. The second case is a role below `TimedDevice` in the MRO, which is
    never called. The check reads the MRO, so it also covers roles inherited
    through a device class or a role's subclass. The wrong-order test
    (`RecorderFirst`) became a refusal test. Four tests in all. Mutation checks:
    4 mutants, all caught. The package docstring and `Recording`'s class
    docstring now say the order is checked.
  - **A failed undo ends the device** (user, 2026-09-24, option A: "a device
    that fails in both directions like that is necessarily in a deeply
    unknown state"). Before, the device stayed alive and IDLE when a role's
    rollback disarm failed inside `arm`. The Coordinator's abort
    (`StopDevice`, a no-op when IDLE) then missed it, and the next arm landed
    on hardware in an unknown state. Now:
    - `TimedDevice._undo_arm(cause, super().disarm)` is used by both roles.
      On failure it notes the disarm's error on `cause` and marks the device.
    - `_start` still answers `Rejected` and publishes the fatal `Fault`, so
      the Coordinator aborts at once. Then it re-raises, and the device dies
      of the arm's error. `Relaunch` is the user's call, as for any loss.
    - The `Fault` reason uses `describe()` (repr plus notes), so both
      failures are reported.
    - `Recording` now raises the open's error, with the disarm's as a note,
      as `ImageStreaming` does. It used to raise the disarm's, with the
      open's chained.

    There are three tests in `test_composition.py`, replacing the probe
    test. Mutation checks: 6 mutants, all caught. 545 passed on both
    interpreters.
  - **Found, for the user (not changed):** after a participant is lost, the
    Coordinator drops it from the registry and the next `StartAcquisition`
    runs **without it**, even when it is `required`. `required` only decides
    whether a loss mid-run ends the run. This is from reading
    `Coordinator._start`, which asks only the registered devices and never
    checks the specs. **Correction:** X11 does pin it, in its last
    assertion ("ready to go again without the lost camera"), an extension
    of D6's "not latched" to a lost device, with no user decision recorded
    for it. With the change above,
    a camera that dies of a failed undo is left out of the next acquisition
    unless relaunched.
  - **Decided (user, 2026-09-24): no start without a required device.**
    Option A of two (the other: keep starting without it). While a
    `required` participant is lost, `StartAcquisition` is refused before
    anything arms, with `Rejected("required device lost: <names>; Relaunch
    it to start")`. Not latched: the Coordinator stays Idle. A lost optional
    device is left out, as before. Three tests in `test_participants.py`
    (D5); C1 and the relaunch-refused test now make their lost shutter
    optional, since they need a start to go ahead without it; X11's last
    assertion now expects the refusal. Mutation checks: 5 mutants, all
    caught. 548 passed on both interpreters.
  - Both committed together as `5c74b9e`; CI run 36056535777 green on all
    four legs. Next is 5c.
  - **5c: keep the fake DLL** (user, 2026-09-24, after asking whether it
    earns its keep when it cannot be distributed). It lives wherever the
    adapter lives, under the same policy, so distribution does not separate
    them. It buys CI coverage, tests Claude can run, and failure injection;
    it cannot vouch for argtypes, struct layouts or driver behaviour, which
    the header conformance test, a real-DLL smoke suite and 5d cover.
  - **The DCAM API differs between Windows and Linux** (user's note,
    2026-09-24: cross-platform may not be testable). What the SDK shows
    (`dcamapi4.h` and the sample `dcamapi4.py`, read locally; the API
    reference says nothing about platforms):
    - the library: `dcamapi.dll` on Windows, `/usr/local/lib/libdcamapi.so`
      on Linux;
    - `int32` is `long` on Windows and `int` elsewhere, 32 bits on both, and
      pointers are 8 bytes on both 64-bit platforms, so struct layouts
      should match; the conformance test is what would confirm it;
    - recording: `dcamrec_openA` / `dcamrec_openW` on Windows (the sample
      uses W), `dcamrec_open` with `char*` on Linux;
    - not in the SDK at all: which cameras, grabbers and features Linux
      DCAM supports (the Flash on a FireBird Camera Link grabber, say).
    There is no Linux machine with DCAM to test on. **Decided (user,
    2026-09-24): the adapter targets Windows only.** "Linux usage is only
    hypothetical, so as long as the streaming buffers are cross-compatible
    i'm satisfied a future user could write their own thin adapter." As
    proposed: the adapter claims nothing for Linux;
    the platform-specific parts (loading the library, opening a recording)
    sit in one place; the adapter's logic against the fake runs on both CI
    legs, since the fake is plain Python and tests no binding; the
    conformance test and anything that loads the real library run on
    Windows only. A Linux port later would need its own 5d.
  - **5c's shape and tests, approved (user, 2026-09-24).** Q1–Q10 as
    proposed:
    - Q1: K1–K8 over the fake, in real time.
    - Q2: the frame format from properties found by name.
    - Q3: `DcamError`, carrying the code, its name and DCAM's text.
    - Q4: close tries every step and reports every failure.
    - Q5: `attach` passes one address per slot, in slot order.
    - Q6: a `stop` before the wait starts still ends it.
    - Q7: lost frames.
    - Q8: stamps.
    - Q9: a library that cannot load fails clearly.
    - Q10: the in-flight measurement recovers the fake's depth.

    The binding is private (`adapters/dcam/_api.py`), and the port is
    `DcamFrameCapture` (`capture.py`), which takes the library as an
    argument. The fake (`tests/adapters/dcam/fake_dcam.py`) mimics the raw
    function table, numbers its properties differently from the header, and
    writes frames with `memmove`. Three decisions:
    1. **The fake tests stay out of CI and the default run**: they live in
       `tests/adapters/dcam/`, which the default run ignores, and are run by
       name. "Linux implementation is out of scope, and I have confirmed
       that the recording functionality this is built for is Windows only."
    2. **Stamps come from attached stamp buffers**, one per slot
       (`DCAMBUF_ATTACHKIND_TIMESTAMP` / `FRAMESTAMP`), not
       `dcambuf_copymetadata`, whose start index is a frame number with
       undocumented mapping onto the ring. A capability check at open; 5d
       confirms that the Flash supports it.
    3. **Lost frames are detected both ways**: `DCAMERR_LOSTFRAME` /
       `DCAMERR_MISSINGFRAME_TROUBLE` from DCAM, and a gap in the frame
       stamps. 5d checks what the Flash really reports.
- **Handover (2026-09-24, end of the fifth session).** Items 1 and the
  waiting questions below are done (sixth session, above). 5b is done: K1–K8, F1–F6 and the `Recording` change (`ec1951e`),
  then a comment review and this handover in CLAUDE.md (`5c8abf3`, CI green on
  all four legs). Next
  is 5c, DCAM against a fake DLL, with no camera. Before 5c, in this order:
  1. **Close the start-failure gap** (found in the comment review, not yet
     fixed; the user has not been asked). In `ImageStreaming.arm`, if
     `capture.start()` fails, the slots are detached, but everything listed
     after `ImageStreaming` has armed and is never disarmed. On the Flash
     that leaves the DCIMG file open and the camera armed. It is the same
     gap the user approved fixing in `Recording` ("Yes, fix it here"). The
     fix would be to call `super().disarm()` before the detach when the chain
     had armed. The code comment and the package docstring
     (`core/devices/__init__.py`) both say "not yet covered". Ask the user,
     then do it red/green and update both texts.
  2. **5c**, with the three decisions below (user, 2026-09-24).
  - **In-flight depth: measure it, with a test written in 5c and run in 5d.**
    See "In-flight depth, the measurement" in 5b.
  - **Time stamps: accept the driver's.** `TimeSource.DRIVER`, from
    `DCAM_TIMESTAMP` (seconds plus microseconds, so presumably
    `device_clock_hz` = 1e6; confirm against the header in 5c). Selecting the
    camera as the stamp source is not pursued.
  - **The SDK licence has a policy** (user, 2026-09-24): our own thin
    ctypes layer, no SDK file in the repository ever, the 1.4 marking in
    every DCAM file, **no copyright line or licence on them**, the adapter
    removable, property IDs looked up by name at runtime, and a local-only
    test that checks our structs against the installed headers. Sharing
    outside the licensee is not decided. Read "The DCAM SDK licence" in 5b
    before writing any DCAM file; it also records how Micro-Manager,
    areaDetector and the Python projects handle it.
  - Also waiting on the user: D12's frame numbering (restart at 0 per
    acquisition, or run on), and whether to enforce the base-class order.
- **The `Recording` change is written and green (2026-09-24, fifth session),
  committed and pushed as `ec1951e`; CI green on all four legs (run 36048627219).** 538 passed on 3.14t and on 3.14. Decided by the user:
  the device chooses where its file opens **by the order of its base
  classes**, and a file that fails to open disarms what armed below it.
  - Each role wraps every class listed after it. Arm runs down the bases,
    disarm back up. The order to list them is `SoftwareTimed,
    ImageStreaming, Recording`, then the adapter.
  - `ImageStreaming.disarm` is now `arm` mirrored: stop, final pass, the
    rest of the chain, then detach (in a `finally`). It used to detach
    before the chain disarmed.
  - So `OrcaFlash(ImageStreaming, Recording, TimedDevice)` arms as attach,
    arm camera, open file, start; and disarms as stop, publish last frames,
    close file, disarm camera, detach. That is Hamamatsu's recording sample
    (`samples/cpp/recording/recording.cpp`: alloc, rec_open, cap_record,
    cap_start, cap_stop, rec_close, buf_release) and the API reference's
    "Recording control". It also suits a buffer-reading recorder, which
    closes only after the stop's final pass.
  - `Recording.arm` now calls `super().disarm()` if `open_recording` raises,
    then re-raises. The arm is still refused with a fatal `Fault`.
  - The order is documented, not enforced: the package docstring
    (`core/devices/__init__.py`) states the rule with each role's
    before/after, and the class docstrings of all three roles point to it.
    Rationale is in comments. Not checked at class creation; the user may
    want that.
  - Tests: `tests/unit/devices/test_composition.py` (6) and one in
    `test_recording.py`. Mutation checks: 6 mutants, all caught. One (no
    `finally` on the detach) first survived, because the capture's close
    detaches anyway when the device dies; the test's capture now logs its
    close, so the two detaches are told apart.
  - **Counterexamples, found when the user asked for them.**
    - *A capture that will not stop* (fixed, user's go-ahead). With
      `ImageStreaming` first, the file's close sits behind `capture.stop()`.
      A failed stop used to skip `Recording.disarm` and the camera's disarm
      for good, because the device is idle when it dies and teardown does not
      disarm again. Now the rest of the chain disarms anyway. The slots stay
      attached, because the driver may still be writing, and `close`
      releases them. The stop's error is raised, and a later one is added as
      a note. Two tests; two more mutants, both caught.
    - *Only nested sequences can be expressed.* A role acts around
      everything listed after it, so two roles' steps cannot interleave, and
      `Recording` can never open before what is below it arms. That covers
      DCAM and a recorder that reads the buffer. An SDK that binds its
      recorder before the buffer is attached would need the device's own
      class to add the step, or a small class inserted between roles.
    - *A wrong order is silent*: documented, not checked.
  - Next: step 6 below.
- **F1–F6 are written and green in simulation (2026-09-24, fourth session),
  committed and pushed as `b2948ec`.** CI is green on all four legs (run
  36044320933), with 529 passed on each. The decisions made
  first are in 5b under "Settled before F1".
  - The role: `core/devices/image_streaming.py`. `ImageStreaming` takes an
    optional `capture`, plus `n_slots`, and then does the following:
    - lays each acquisition out from the capture's format (`layout_for`) and
      declares its stamps (`metadata()`);
    - attaches the slots, arms the rest of the chain, then starts the capture;
    - runs a publish loop as its own task, which stamps and then publishes
      every delivered frame;
    - on disarm: stops the capture, publishes what it delivered, then
      detaches;
    - closes in the agreed order.

    A producer without a capture works as before, through `layout()` and
    `writer`.
  - **A bug found while writing it:** the loop's stop flag was first named
    `_stopping`, which is the actor runtime's own flag, so disarm stopped
    the whole actor. It is now `_capture_stopping`. Mixins share the actor's
    namespace, so watch for this.
  - `SimulatedCamera` is now the role over `SimulatedFrameCapture`, built in
    its `setup` (the capture needs the actor's clock). It takes the same
    format arguments as the capture. `missed` and the trigger callback now
    belong to `camera.capture`. The simulated capture gained `reformat(...)`,
    a simulation control standing in for a camera setting. It is refused
    while memory is attached.
  - The tests:
    - the contract is `tests/contract/image_streaming_contract.py`, with 14
      tests;
    - the simulated camera's rig is `tests/contract/test_image_streaming_role.py`,
      in two formats (the padded one has `in_flight=2`), with 6 slots. The
      name avoids clashing with `tests/unit/devices/test_image_streaming.py`
      under pytest's default import mode;
    - five unit tests with purpose-built captures, in
      `tests/unit/devices/test_image_streaming.py`: the close order, a failed
      capture release, a leaked view, a refused arm, and a frame that lands
      after the last count.
  - **D12 changed** (user to confirm): `hw_frame` now restarts at 0 with
    each capture, because `SimulatedFrameCapture` counts from each `start`
    (K1–K8). D12's second-acquisition test used to assert that the numbers
    ran on across acquisitions. It now asserts the restart, which still shows
    that the stamps are the camera's own count and not the buffer's index.
    The DCAM reference does not say which a real camera does ("increased by
    the camera or DCAM on readout"); 5d can check.
  - Mutation checks: 13 mutants, all caught. One mutant, dropping the final
    pass after the stop, first survived: in simulation it is equivalent,
    because the simulated capture counts frames as the role resumes. It is
    now caught by a unit test whose capture reports a count before its last
    frame lands, as a threaded driver wait would. One property cannot be
    observed in simulation: that the layout is declared before capture
    starts, since no frame can arrive during `arm` there. The port enforces
    it anyway, because `attach` needs the new slots and `start` needs
    `attach`.
  - **Next: the `Recording` change (5b).** With
    `class OrcaFlash(Recording, ImageStreaming, TimedDevice)`, `Recording`'s
    disarm closes the file *before* the capture stops. DCIMG needs the
    opposite order, and needs the file opened between attach and start.
- **K1–K8 are written and green in simulation, committed as `eb1882a`** (CLAUDE.md
  in `f9495f9`; `main` is six commits ahead of the remote, not pushed). 100 new
  tests, 482 in all, on both interpreters. The files are those listed under
  "The port's shape, settled before K1" (5b). `layout_for` is in
  `core/devices/image_streaming.py`. Every test got a mutation check. The one
  mutant that survives is below. Two readings went beyond the text, and the
  user agreed to both (2026-09-24):
  - **K7:** "safe in every state" includes closed, so `detach` and `close`
    after `close` do nothing. The "every call except `stop` raises" rule
    applies to the other calls. The port's `close` docstring says so.
  - **K8:** besides attach-while-capturing, attaching over memory already
    attached, and `start` with nothing attached, both raise `RuntimeError`.
- **Pushed, and CI green again (2026-09-24), at `e4f45f1`.** 492 passed on all
  four legs. CI had been red on Linux since 2026-09-21 (`9ecd434`). V8 closed
  the producer twice, and a second `FrameWriter.close()` raised on POSIX.
  Worse, if a producer had been restarted under the same name, that second
  close removed the new producer's segment, because POSIX unlinks by name.
  Windows hid the problem. The user chose to make the close idempotent
  (option 1, over fixing only the tests). The change was done red/green on a
  branch: the red `a4fd780` failed only on the Linux leg, and the fix
  `e4f45f1` adds a `_removed` flag, set after the unlink so that a close
  retried after `BufferError` still removes the segment. A mutant that set the
  flag first failed the live-view tests. `main` is even with the remote.
  **Check CI after every push**: this failure went unnoticed for three days.
- **Buffer fix: steps 1 to 3 done (2026-09-24, third session), committed as
  `a866952`.** 491 passed on both interpreters.
  - The red test is `test_closing_under_a_live_view_raises_instead_of_unmapping`
    in `tests/unit/buffer/test_lifetime.py`: 9 cases, one per view a caller
    can hold (the writer's `frames`, a frame, `slots`, a slot and `meta`; the
    reader's `frames` and `meta`; a `Frame`; a `Group`). All 9 failed with
    "did not raise", then went green.
  - The fix is `_Mapped._bytes()`, which is `np.frombuffer(shm.buf, np.uint8)`.
    `_map` and `FrameWriter.slots` now pass it as `buffer=`.
    `np.ndarray(buffer=<ndarray>)` keeps the export, so no `as_strided` was
    needed. On Windows the error comes from `mmap.close()`, after the
    memoryview has already been released. A second close, once the views are
    dropped, goes through; the test proves it. CI must still confirm Linux.
  - The fix exposed leaks in tests:
    - 4 tests held `Frame`s past `reader.close()` (2 in
      `test_image_streaming.py`, 2 D12 tests in `test_triggered_rig.py`). They
      now `del` them first.
    - The contract's `buffers` fixture closed segments before the rig's
      teardown detached the capture (40 teardown errors). It now requests
      `rig` and awaits `rig.capture.detach()` before closing, which is the
      role's own order.
  - The `close` docstrings of `FrameWriter` and `FrameReader`, and the `Frame`
    docstring, now say that close raises `BufferError` while a view is alive.
    `_Mapped._release`'s docstring is now true and was left alone.
  - Step 2 re-check: K6 catches both kept-view mutants (`_frames` and
    `_slots`). V8 fails when the monitor keeps an uncopied frame. The
    benchmark runs clean.
  - Open, for the user: `ImageStreaming.close` calls `writer.close()` before
    `super().close()` with no `try/finally`. A leaked view in the producer
    process would now raise there and skip the rest of the device's close.
    This was not changed. It arises again in F1–F6, where the role detaches
    the capture before closing.
- **Next steps, in order** *(user agreed to the buffer fix, 2026-09-24)*:
  1. *(done, see above)* **The buffer fix, red/green in `core/buffer/`.** First a test that
     fails today: close a `FrameWriter` (and a `FrameReader`) while a
     `frames` / `slots` / `Frame` view is alive, and expect `BufferError`.
     Then build every view in `_Mapped._map` and `FrameWriter.slots` over
     `np.frombuffer(shm.buf, np.uint8)` (slice, `as_strided`, `.view`), not
     `np.ndarray(buffer=shm.buf)`. Run the whole suite on both interpreters:
     any test that now raises `BufferError` at close was holding a view, which
     is a leak to fix in that test, not something to suppress. Correct
     `_Mapped._release`'s docstring only if its wording is still wrong. CI must
     confirm Linux, where `mmap.close` with exports should also raise.
  2. *(done)* **Re-check K6 and V8.** Rerun the surviving mutant (`detach` keeps
     `self._frames` as `self._kept`) and confirm K6 now catches it. Mutate the
     monitor to hold a view and confirm V8 fails. `SimulatedFrameCapture`'s
     `_Pixels` holder builds its view from the slots it was given, so it pins
     the segment once the slots do.
  3. *(done, `a866952`)* **Commit the fix** on its own, after the user asks. The port work is
     already committed.
  4. *(done, `b2948ec`; see the F1–F6 entry above)* **F1–F6**, with
     `SimulatedCamera` rebuilt as the `ImageStreaming` role over
     `SimulatedFrameCapture`.
  5. *(done; see the `Recording` entry above)* **The `Recording` change** (5b, "Order").
  6. **The start-failure gap** in `ImageStreaming.arm` (see the handover entry at the top).
  7. **5c**: the DCAM ctypes layer against a fake DLL, and the in-flight
     measurement tested against the fake, under the licence policy (5b,
     "The DCAM SDK licence"). Then 5d on the camera (the user
     runs it), then 5e, DCIMG through `open_recording` / `close_recording`.
- **Found: numpy views do not pin the segment.**
  `np.ndarray(buffer=shm.buf)`, which `_map` and `FrameWriter.slots` use,
  keeps a reference to the `mmap` but no buffer export. So
  `SharedMemory.close()` unmaps under a live view, and touching the view then
  segfaults. This was reproduced on 3.14t with numpy 2.5.3; on standard 3.14
  the close also goes through. `np.frombuffer(shm.buf)` does hold the export,
  as do slices, reshapes, `as_strided` and `.view` over it, and then the
  close raises `BufferError`. Consequences:
  - `_Mapped._release`'s docstring promises the `BufferError`, and it does
    not happen;
  - the monitor's V8 (`test_closing_releases_the_segment`) is vacuous;
  - a consumer that keeps a `Frame` past close crashes instead of getting an
    error;
  - K6's check, that closing the segment after `detach` raises if the
    adapter kept a view, cannot fail. Its mutant, `detach` keeping the typed
    view, survives.

  The fix is to build every buffer view through `np.frombuffer`, as its own
  red/green change in `core/buffer/`, before K6 counts as done. The user
  agreed; it is step 1 above.
- **Where the first 2026-09-24 session stopped:** naming was settled before any 5b code, and
  nothing of 5b is written yet. `main` is at `132813a`, four commits ahead of
  the remote, **not pushed**:
  - `d9f9124`: image renames;
  - `baf8602`: segment → waveform;
  - `c2c6123` and `132813a`: CLAUDE.md.

  Both interpreters gave 382 passed. The NI adapter suite gave 99 passed and
  1 skipped. The `Dev1-sim` board rehearsal gave 32 passed and 18 skipped.
  Read "Vocabulary" before naming anything in 5b.
- **Starting K1–K8: confirmed by the user, 2026-09-24,** along with the two
  open questions below; see "The port's shape, settled before K1" under 5b.
  Follow the `SignalOutput` pattern:
  - the port as a `Protocol` in `core/ports/frame_capture.py`, with
    `FrameFormat` and `FramesLost` beside it;
  - the contract in `tests/contract/frame_capture_contract.py`, with a `Rig`
    of its own;
  - the simulated adapter's suite in `tests/contract/test_frame_capture.py`;
  - `SimulatedFrameCapture` in `adapters/simulated/`: triggered by
    `TriggerBus`, drawing the moving bar the way `TestImageGenerator` does, and
    touching only the slot memory it was given (never the counter, the header
    or the metadata).

  Open questions for the user when K1 is written:
  - what `attach(slots)` receives (the writer's slot views, or raw
    addresses);
  - how `FrameFormat` and `Layout` relate, since the port states the
    format and the role builds the `Layout` from it.
- **A flake seen once:** X7's `[segfault]` case
  (`tests/integration/test_process_failure.py`) failed once, in the first full 3.14t
  run after the image renames. That test uses none of the renamed code. It
  then passed three times in isolation with the renames, three times without
  them, and in every full run since. It is a real-clock test, so it is probably load-sensitive.
  Watch for it again; nothing has been changed.
- **5a (NI-DAQmx) is done and pushed** (`main` at `06dda5c`). That covers
  5a-1, 5a-2 and 5a-3 on the physical board.
  - The full board suite on `Dev1` gave 50 of 50.
  - The port contract suite passes on `Dev1-sim` and `Dev1`.
  - The records are under "Next steps" → Step 3 below, in date order.
- **5b's shape is decided** *(2026-09-24)*: the `ImageStreaming` role, which
  owns the buffer, and the `FrameCapture` port under it. Read "5b — the
  image-streaming role and its capture port" below, which also records the
  survey of the dev box camera.
  - **Next is its tests, red/green in simulation:** K1–K8 against a simulated
    capture port, then F1–F6 with `SimulatedCamera` rebuilt on the role, then
    the `Recording` change.
  - Names were settled first (2026-09-24, commits `d9f9124` and `baf8602`);
    see "Vocabulary".
- **What 5a leaves open** is listed under "Open items → NI-DAQmx, carried
  over from 5a". None of it blocks 5b.

"`nidaqmx` first, then DCAM" hid the fact that the two adapters are at entirely
different readiness. **`nidaqmx` is a drop-in**: the `SignalOutput` port exists
and so does its contract suite, written against NI-DAQmx continuous output with
regeneration disabled, underflow included. **DCAM has no port to implement** —
`core/ports/` holds only `clock.py` and `signal_output.py`; `FrameProducer`,
`FrameSink` and `Recorder` were named in the tree above but were never built. M2
built the *buffer*; M3's cameras are device actors writing to a `ImageBuffer`
directly. So the camera side needs a port invented before any DLL is touched,
and that is a design task, not an adapter task.

| Stage | Content | Needs |
|---|---|---|
| 5a | NI-DAQmx `SignalOutput` adapter | driver for 5a-2 only |
| 5b | The `ImageStreaming` role, the `FrameCapture` port, and both contract suites | **nothing** |
| 5c | DCAM ctypes layer, offline, against a fake DLL | SDK headers, read locally (never committed) |
| 5d | DCAM device actor, zero-copy into `ImageBuffer` | SDK |
| 5e | DCIMG recorder (`dcamrec_*`), Windows-only | SDK |
| 5f | uEye producer | pyueye (BSD-3); **done 2026-10-01**, see "5f — the uEye camera" |

5a splits again along the same seam: **5a-1** the adapter against a fake
`nidaqmx.Task` injected as a factory, needing nothing installed *(done
2026-09-21, N1–N9 green, commits a069935 to ce03480)*; **5a-2** the existing
contract suite against an NI MAX simulated PCI-6733; **5a-3** the instrument. 5b is the keystone — it has no external dependency at all, so
nothing can block it, and fixing the camera port's shape before the DLL is
involved is what de-risks DCAM.

**Decisions (user, 2026-09-19):**

1. **Underflow is not trusted until it is tested against real hardware.** 5a-1
   proves only that we map DAQmx's underflow codes onto our `Underflow`; 5a-2
   proves only that a simulated device *accepts* the configuration. A simulated
   device most likely cannot be made to genuinely run dry — there is no real
   sample clock consuming a real FIFO. Since regeneration-disabled output is the
   single constraint the whole refill-loop design exists to serve, the path is
   **written but unverified** until the instrument says otherwise, and no
   document or commit message may imply more.
2. **Device-adapter tests stay off CI.** The cross-OS matrix exists to keep the
   buffer model consistent, and a device adapter's OS differences are not
   knowable in advance, so a CI leg would not be evidence of much. Isolation is
   by directory: `tests/adapters/<vendor>/` and a future `tests/hardware/` are
   `--ignore`d by default and run only when named, by whoever is developing that
   adapter. Vendor packages stay optional extras, out of the dev group.
3. **No generalizability machinery.** A proposal to ship the contract suite as a
   pytest plugin, so third-party adapter distributions could reuse it, was
   rejected as premature. The user's principle: *"if we can recognize something
   as an implementation-specific requirement it should be kept out of abstract
   and general code, but we don't need to necessarily facilitate future
   development here."* The abstraction that matters is keeping NI-specific
   requirements **out of** `core/`, not building extension points for developers
   who do not exist. So: start order, `ao/SampleClock` routing, regeneration
   mode and error-code mapping all live in `adapters/nidaqmx/`, and the
   `SignalOutput` port does not change.
4. **The simulated device is a PCI-6733 named `Dev1`**, matching the channel map
   above. *Superseded 2026-09-23 (user):* it is named **`Dev1-sim`**, so it can
   sit beside a physical `Dev1`, and every dev setup is to use that name. Found
   on a machine with the real board installed, where `test_dev1.py`'s guard
   (device name only) would have driven real output; the suite now also
   requires `is_simulated`. The 10 `Dev1-sim` tests pass there, so the
   hyphenated name also works in the `/Dev1-sim/ao/SampleClock` route. N18's
   assertions against the MAX tasks need the same care: the real
   `SPIM_*_Task`s point at the physical `Dev1`.

**5a-1 acceptance tests**, against a fake `Task`:

- N1. `configure` builds an AO task and a DO task at the configured rate, one channel per `Channel.physical` in order, output buffer set to `buffer_samples`.
- N2. Regeneration is disabled and the DO task is clocked from `/Dev1/ao/SampleClock`, not its own — the two settings the refill design rests on.
- N3. `go()` starts the slave before the master, so no clock ticks before DO is waiting; with nothing written it raises `RuntimeError` and starts neither task.
- N4. `space_available()` reports the driver's `space_avail`; an oversized write raises `ValueError` before touching the driver.
- N5. `write` passes AO volts and DO bools through as channels×n in order; `label` never reaches the driver.
- N6. A `DaqError` carrying -200016 / -200621 / -200018 surfaces as our `Underflow` with `at` computed from samples generated ÷ rate; any other `DaqError` propagates unchanged and is never disguised as underflow.
- N7. `wait_for_space(n)` returns when the driver reports space, polling through the injected `Clock`; a full buffer never started raises `RuntimeError`.
- N8. `drain()` waits for written samples to finish, and an underflow code raised after it is the expected end, not an `Underflow`.
- N9. `stop()` is safe in any state and repeatable; teardown closes both tasks even if stop already ran — the "never wedge the driver" invariant.

**Closing a DAQmx task — settled 2026-09-22, see 5a-2 decision C below.**
`SignalOutput` had no `close()`. `WaveformSource` calls `output.stop()` from
`disarm`, from the end of `drain`, and from `teardown` — but `stop` cannot close
the tasks, because an `arm` for the next acquisition follows it and `configure`
runs only once, in `setup`. So for the NI adapter the two tasks lived until the
process exited. In-process that holds the board against anything else wanting
it; out-of-process the child's exit releases them, which is why M4's placement
made this less urgent than it looked. It bears on core invariant 3 ("`teardown`
always runs") all the same, and the DCAM adapter has the identical shape with
far worse consequences — an unclosed camera handle is the thing that wedges the
driver. N9 covers `stop` being safe and repeatable, not task closure.

**Found while building 5a-1.** The mutation check caught a real bug in `drain`:
both `wait_until_done` calls shared one translation guard, so an underflow
swallowed on the AO wait skipped the DO wait entirely and left the digital
lines undrained — the half-stopped rig that `stop` goes to lengths to avoid.
Each task now gets its own attempt. Twenty mutations, none surviving.

A second latent defect surfaced while isolating the device tests, unrelated to
M5 but fixed then (commit db1f105): seven test modules imported shared helpers
as `from conftest import ...`, and the suite has three `conftest.py` files.
That bare name resolves through `sys.path`, so only one could hold it; the
suite was green only because a single-root walk re-imports `conftest` as it
descends. Any multi-root invocation failed, in either order. Shared values now
live in uniquely named support modules.

### 5a-2 — the simulated PCI-6733 *(done 2026-09-22)*

`tests/adapters/nidaqmx/test_dev1.py`, ten tests against NI MAX's simulated
`Dev1`, skipped when the device is absent. They prove the *driver* accepts our
call sequence, that the board consumes at the configured rate, and that the DO
task keeps pace with the AO task. They cannot prove underflow: a simulated
device that runs dry stalls rather than erroring.

**Three real defects, each red before green** — none of which the 5a-1 fake
could have caught, and all of which would have failed identically on the
instrument:

1. `space_available()` raised −200983 before start, because `configure` never
   committed the tasks although the port says it reserves and commits. Fixed
   with `TaskMode.TASK_COMMIT` on both tasks at the end of `configure`.
2. `write` raised −200524 for a one-channel task: `nidaqmx.Task.write` infers
   the channel count from the array and refuses a 2-D array when the task holds
   one channel. The port always speaks channels×n, so the adapter squeezes.
3. `drain()` always timed out, because it called `wait_until_done` on a
   `CONTINUOUS` task. Rewritten to poll `total_samp_per_chan_generated`.

The fake was corrected on two counts so 5a-1 no longer passes on false
premises: it now models the channel-shape rule, and `_OutStream` grew its own
`fail` switch and a settable `played` counter.

Two findings established as evidence rather than assumption. **The board forces
the slaved-clock design**: a mutation unslaving the DO task is accepted by
`cfg_samp_clk_timing` but refused at *commit* with −200303, which is also why
committing at `configure` turns it into a setup-time failure rather than one
mid-acquisition. And **underflow stays unverifiable** in simulation, so
decision 1 above stands unchanged.

### 5a-2b — the LabVIEW printouts *(user, 2026-09-22)*

The user supplied a full VI-documentation export of the legacy hierarchy, block
diagrams included (see `spim-engine-walkthrough.md` source 1, and
`labview-vi-requests.md` for the VI-by-VI reading). Under the standing rule —
*in cases specifically involving PCI-6733 behaviour the LabVIEW code is a strict
reference* — it settles four decisions that had been held open.

It also found a **real defect**: `UNDERFLOW_CODES` was missing
**−200290 `GEN_STOPPED_TO_PREVENT_REGEN_OF_OLD_SAMPLES`**, the code a
regeneration-disabled task actually raises when it runs dry, and the one the
legacy cancels at shutdown. We carried only the FIFO/DAC-level codes, so on the
instrument every acquisition ending by running the buffer down would have been
reported as a fatal fault. *Fixed 2026-09-22, test-first.*

**Decisions (user, 2026-09-22):**

- **A. `drain()` keeps the poll.** The legacy uses `Wait Until Done(−1)` per
  task and lets it return by raising −200290. Ours polls
  `total_samp_per_chan_generated` through the injected clock, which reaches the
  same ending with no hardware-visible difference, runs in virtual time, and
  stays cancellable — a blocking driver call inside `teardown` is what makes a
  stuck shutdown unkillable. *Correction to the 5a-2 record:* the claim that a
  continuous task "is never done, so `wait_until_done` can only time out" is
  **wrong**. It times out against a simulated device because that device stalls
  instead of running dry. Both comment and test docstring have been fixed.
- **B. `wait_for_space` / `space_available` stay on the port.** The legacy never
  measures free space; it hands `DAQmx Write` a whole MAIN buffer with
  `timeout = −1`. This is the largest deliberate divergence from the reference,
  and it is deliberate: an infinite blocking write is unobservable and
  uninterruptible, which is why the original's only response to a late refill is
  to die. It is also what makes `SimulatedSignalOutput` and the port contract
  suite possible.
- **A & B carry a hardware obligation.** *Both* variants are to be tested
  against the instrument in 5a-3, in case the board deviates in ways neither the
  fake nor `Dev1` can show. Until that comparison exists, A and B are
  provisional. See the 5a-3 list below.
  *Confirmed, user 2026-09-23:* A and B are no longer provisional. The
  board did not deviate: `wait_until_done` timed out on driver 20.0 rather
  than returning, and polled and blocking refill both held at 0.8–2.4 % of
  a core. **If either misbehaves later, start from these records**: "First
  physical run 2026-09-23" (H2, H3) and the full run under "The last digital
  samples (H10)" below.
- **C. `close()` joins the `SignalOutput` port.** The legacy's `Stop Core.vi`
  clears both tasks by name, unconditionally, errors discarded, on every exit
  path including a fault — which is precisely `WaveformSource.teardown`. So the
  core owns the release; it is not the adapter builder's job. Per core invariant
  6, a failure to close is reported rather than propagated over whatever ended
  the acquisition.
- **D. The NI adapter imports its tasks from NI MAX.** `Pre Launch Init.vi` sets
  only `AO/DO.DataXferMech := DMA` and `Control Task := commit`; everything else
  — channels, 0–5 V RSE ranges, rate, `ao/SampleClock` routing, regeneration
  mode — lives in MAX, and the launch VI reads `SampClk.Rate` back out of the
  task to build waveforms against. The user's reasoning: **NI MAX must exist as
  part of any NI hardware configuration, and importing a config at system
  installation time is straightforward**, so this keeps hardware-specific
  configuration out of our scope entirely. It is also what gets us the
  transposed `Scan2→ao3` / `Scan3→ao2` for free.

The MAX task export the instrument uses is now in the repo as
`reference/legacy/DAQmxConfiguration.txt`, and both tasks are loaded on the
development box.

**What the `nidaqmx` package turns out to be** (checked 2026-09-19, v1.6.0):
pure Python (`py3-none-any`), so ABI-agnostic and free of the `cp314t` wheel
question. It imports with no driver present and raises `DaqNotFoundError` only
on use, which is a clean signal for skipping. `python -m nidaqmx installdriver`
exists but merely downloads NI's interactive installer and runs it — no silent
mode, and no OS check of its own on Windows. Simulation is **not** reachable
from Python: `Device.is_simulated` is read-only and there is no device-creation
API, so creating the simulated board is an NI MAX GUI task.

**The driver version is 20.0, matching the instrument** *(user, 2026-09-21)*.
The rig runs NI-DAQmx and NI MAX 20.0.0, so development targets that rather
than the latest: testing against a newer driver than the one the instrument
uses would prove the wrong thing.

This also disposed of an OS problem found on the way. `nidaqmx` 1.6.0's
`installdriver` offers only NI-DAQmx 26.5, whose supported-Windows list is
Windows 11, Server 2025, Server 2022 and Windows 10 IoT Enterprise 2021 (build
19044) — excluding the development box, which is Windows 10 Enterprise LTSC
2019, build 17763. Since 20.0 is a 2020 release with ordinary Windows 10
support, the mismatch disappears; the driver is simply downloaded manually,
and `installdriver` is not used. The `nidaqmx` package itself "supports all
versions of NI-DAQmx", with only newer functions unavailable on older drivers,
and every call the adapter makes long predates 2020.

### Next steps — the ordered work list *(written 2026-09-22)*

Everything below is approved and unblocked. Do it in this order: C is a `core/`
change that D's tests will rely on, and both must land before the instrument is
touched.

**Step 1 — `close()` on the port (decision C).** A `core/` change, so red/green
with a contract test, and a mutation check on anything that passes first try.
*Done 2026-09-23, N10–N13 green on 3.14t and 3.14.*

- **N12's mechanism:** `teardown` tries `stop` then `close`, gathers
  both failures, and raises an `ExceptionGroup`. The runtime already attaches a
  teardown failure to the error that ended the actor as a note (`_lifecycle`),
  so nothing new was needed there.
- **Found while writing N12, a real defect in the runtime:** the note never
  reached anyone. `ChildExited.error` and the process child's `ActorDied` were
  built with `repr(exit_error)`, and `repr` leaves notes out. So since M1 a
  failed teardown after a fault reached the supervisor as the fault alone,
  both in-process and across the boundary. Fixed with `describe()` in
  `core/actors/actor.py`, used at every point where an exit error becomes a
  string. Tested in-process (`test_supervision`) and across the process
  boundary. For the latter, `FaultyDevice` gained a `release_fails` option.
- **Mutation checks:** 16 mutations, none surviving. Two guards in
  `SimulatedSignalOutput` survived theirs, because `write` and `wait_for_space`
  already reach `space_available`'s check, and were removed rather than kept
  untested.
- **N13:** the `Dev1-sim` suite has a `WaveformSource` whose teardown alone
  frees the task names.

- N10. `SignalOutput.close()` exists on the port and in the contract suite:
  safe in any state, idempotent, and the adapter is unusable afterwards.
  `SimulatedSignalOutput.close()` marks itself closed and releases nothing.
- N11. `WaveformSource.teardown` calls `stop()` then `close()`, in that order,
  and calls `close()` **even if `stop()` raised** — teardown releases the
  hardware however the acquisition ended (core invariant 3).
- N12. A failure to close is reported, not propagated over the fault that
  started the shutdown (core invariant 6). Decide the mechanism when writing
  N12; the current `ExceptionGroup` out of `close()` is the adapter's contract
  and need not be what `teardown` does with it.
- N13. The NI adapter's existing `close()` loses its "not part of the port"
  docstring and the open-question comment, and `test_dev1.py`'s fixture stops
  being the only thing that closes a task.

**Step 2 — import the tasks from NI MAX (decision D).**
*Done 2026-09-23, N14–N18 green: 42 fake tests, 11 on `Dev1-sim` against the
imported `SPIM_*_Task_sim` pair; 19 mutations, none surviving.*

- **API as built:** `NiSignalOutput(clock, *, ao_task, do_task, system=None)`,
  with both names required (no default: on a dev box with a physical `Dev1`, a
  default pointing at the real `SPIM_*_Task` would be the wrong one to fall
  into), and `read_output_config(ao_task, do_task, *, buffer_samples,
  system=None)`. `system=` is the seam, `nidaqmx.system.System.local()` by
  default. Missing task → `LookupError` naming it, before either is loaded;
  mismatch → `ValueError` listing every difference; the loaded tasks stay
  held until `close()`, which teardown calls after a failed setup too.
- **Found on the driver (read-only probe of the `_sim` tasks):** a saved AO
  task loads with `ao_data_xfer_mech = POLLED` (the DO task loads DMA), so
  N15's DMA setting is a real change, and the simulated board accepts it —
  bearing on H4. Loading an already-loaded task is −200089; indexing
  `System.tasks` by an unknown name succeeds and only `load()` fails
  (−200088), which is why the adapter checks names first. Loaded channel
  names come back bare (`ETL`), physical names without a leading slash, the
  DO clock source fully qualified (`/Dev1-sim/ao/SampleClock`), the AO one
  as `/Dev1-sim/ao/SampleClockTimebase`.
- **N16 checks more than it lists**, by my call — flag if unwanted: sample
  mode continuous, regeneration disabled on both, and the DO task clocked
  from `/<device>/ao/SampleClock` (device taken from the AO task's channels).
  These were N2's settings; now that MAX owns them, a MAX edit is the only way
  they can go wrong, and the refill design rests on them. The AO clock source
  is not checked. Rates are compared exactly, which works because
  `read_output_config` reads the rate the driver reports.
- **`Dev1-sim` suite:** first written against the imported `SPIM_*_Task_sim`
  pair, then changed the same day **by the user's decision: device tests build
  their own tasks**, so a future change to the rig's config in MAX cannot break
  them. MAX import stays the deployment mechanism; only tests stopped depending
  on it. `tests/adapters/nidaqmx/saved_tasks.py` builds a pair from an
  `OutputConfig` (continuous, regeneration off, DO on `/<dev>/ao/SampleClock`),
  saves it with `Task.save` as `stacscope_test_*` (the only prefix it will
  overwrite or delete, which is tested), and deletes it with
  `PersistedTask.delete` whether the test passed or not (also tested). 5
  mutations of the helper, none surviving. The guard is back to "Dev1-sim
  exists and is simulated"; the tasks are on it by construction. The
  single-channel write shape is covered by the fake only. Buffer 40 000
  (0.4 s at 100 kS/s). `DAQmxConfiguration_sim.txt` stays for trying the rig's
  config by hand; nothing depends on it.

- N14. `NiSignalOutput(clock, *, ao_task=..., do_task=...)` loads both tasks
  from `System.local().tasks` by name; a missing task fails at `configure`
  with a message naming it, not at first use.
- N15. `configure` no longer adds channels or sets rate, clock source, sample
  mode or regeneration mode. It sets `DataXferMech := DMA`, sets
  `output_buf_size` from `OutputConfig.buffer_samples`, and commits.
- N16. `configure` **verifies** `OutputConfig` against what MAX reports —
  channel count, labels, physical names, rate — and raises on mismatch. A
  misconfigured rig is a setup-time failure, never a silent override, and never
  something discovered mid-acquisition.
- N17. A helper builds an `OutputConfig` from the loaded tasks, so a rig config
  need not restate the channel map. `buffer_samples` stays supplied from
  outside — it is the one thing MAX does not own.
- N18. `test_dev1.py` asserts against the real `SPIM_Analog_Task` /
  `SPIM_Digital_Task`: five AO channels in MAX's order with the transposed
  `Scan2→ao3` / `Scan3→ao2`, three DO lines, 100 kS/s, DO slaved to
  `ao/SampleClock`, regeneration disabled.
- The 5a-1 fake needs a persisted-task stand-in. Keep the injection seam:
  `tasks=` becomes a system stand-in rather than a task factory.
- The `Dev1` suite now hard-depends on those two MAX tasks existing. Skip with
  a message that says how to create them, not just that they are missing.

**Step 3 — 5a-3, the instrument.** *User runs everything here; Claude does not
execute anything that asserts output.* Read the hardware-safety section of
`claude-workflow-notes.md` first.

- H1. The underflow path, finally observable: confirm −200290 is what the board
  raises, and that `total_samp_per_chan_generated` is readable **while handling
  a failed write** — that read is how the adapter computes `Underflow.at`, and
  if it fails there, a `DaqError` surfaces where the port promises `Underflow`.
- H2. **Decision A's comparison.** Run `drain()` as the poll and as
  `wait_until_done(−1)` against the same acquisition and compare where output
  actually ends. Looking for: whether the poll can return before the last
  samples are clocked out, and whether the two differ in how they leave the
  scanners and laser parked.
- H3. **Decision B's comparison.** Run the refill as the polled
  `wait_for_space` and as a single blocking write per waveform. Looking for:
  margin to underflow at the real 100 kS/s, and whether polling `space_avail`
  every 1 ms measurably loads the driver.
- H4. Whether `DataXferMech := DMA` changes anything measurable, or is already
  the default for this board.

*Written and rehearsed 2026-09-23; run on `Dev1` the same day, first by the
user and then by Claude under the session's authorization. Done, except the
fix H10 calls for (see "The last digital samples (H10)").* The suite is
`tests/hardware/nidaqmx/test_board.py`. Its setup, decided by the user:
- It runs on this dev box's physical `Dev1`, with nothing wired.
- It outputs only 0 V and low lines.
- It builds its own tasks, `stacscope_test_hw_*`.

It runs only on the device named in `STACSCOPE_HARDWARE`. Pointed at
`Dev1-sim` it rehearses: 8 passed, and H1 and H2-waiting skipped, because a
simulated board stalls rather than running dry. Each test's measurements go
to a "hardware findings" summary at the end of the run.
- Because the output is all zeros, H2 cannot answer "how the scanners and laser
  are left parked". That needs non-zero values and a scope.
- **Already answered by the rehearsal (H4, simulated board):** a saved AO task
  loads with `POLLED` (programmed I/O) and an output buffer of 0. The driver
  **refuses programmed I/O for buffered AO** at commit, with −200767. So the
  adapter's DMA setting is required, not a tuning choice, at least in
  simulation. The physical run confirms or refutes this.
- Found while writing H4: a committed task refuses a change of transfer
  mechanism (−201288). Unreserve it first.

If H2 or H3 shows the instrument deviating, the strict-reference rule decides
and A or B is reverted to the legacy mechanism.

*First physical run 2026-09-23 (the user, on `Dev1`, nothing wired):*
- **Running dry, on the raw tasks (H1 survey):**
  - The AO task's `space_avail` reads **0**, with no error.
  - The AO task's `total_samp_per_chan_generated` reads **0**. The DO task's
    count is right (100 000).
  - The AO task's **`is_task_done()` raises −200290**. The DO task's returns
    False.
  - An AO write raises −200290.
  - The first AO `stop()` raises −200290 and the stop still takes; a second
    returns quietly. The DO stop is fine.
  - After a stop, both tasks accept writes and **start again without
    reloading**, so a second acquisition needs no reload.
- **H2:** the AO count stopped at **99 985 of 100 000** and never arrived.
  The DO count, on the same clock, reached 100 000.
  **`wait_until_done` timed out** rather than raising −200290 as it did
  for the legacy engine (driver 20.0 here). Whether the last ~15 analog
  samples physically leave the DAC needs non-zero output and a scope.
- **H3:** polled and blocking refill both held at 1 s and 100 ms buffers,
  at 0.8–2 % of one core. Its "lowest fill" was measured just *after* each
  refill, so it was full by construction and meant nothing. The measurement
  was removed; H5 replaces it.
- **H4 confirmed on the board:** POLLED is refused for buffered AO
  (−200767), so DMA is required.

*Changes made from it, 2026-09-23. The user said to experiment, so these are
Claude's calls; review them:*
- **Detecting running dry:** `space_available` calls `is_task_done()`
  whenever space reads 0. That is the only call that raises on this board.
  It is skipped when there is room, so a 1 ms poll does not double its
  driver calls. *(Narrowed later: see "Pin-level timing" below.)*
- **`Underflow.at`** is the number of samples written over the rate, not the
  count, which reads 0 after running dry.
- **`drain`** ends a task when its count reaches what was written, or when
  `is_task_done()` is True or raises a ran-dry code.
  - On the board, the AO task ends on the raise and the DO task on its count.
  - On `Dev1-sim`, both end on the count.
- **`stop` swallows the ran-dry codes only when the gap is already known**,
  after a drain or after an `Underflow` was raised. Otherwise it reports an
  `Underflow` in its group. This is the legacy scoping ("Unexpected stops
  still propagate the error"), and it avoids a false "not released cleanly"
  note in teardown after an underflow.
- **Chunks are made contiguous** in `_for_driver`. `WaveformSource` writes
  column slices, and nidaqmx's ctypes layer refuses a non-contiguous array.
  This was latent since W1 and found when H5 first drove the real source on
  `Dev1-sim`. The fake now refuses such arrays too.
- **Backpressure (the user, 2026-09-23):** the legacy engine kept the
  hardware buffer small on purpose, so that waiting for room in it paces the
  self-messaging refill. The requirements are:
  - writes are smooth;
  - no underrun except the expected one after cooldown;
  - no stale waveforms queued ahead of an update.

  `WaveformSource` already queued nothing beyond the hardware buffer. But when
  the buffer was full it waited for room for the *rest of the waveform*, and
  for a waveform longer than the buffer that meant waiting for it to empty.
  It now tops up a quarter of the buffer at a time (`REFILL_STEPS = 4`).
  - W7 pins the resulting margin: never below half the buffer at a refill.
  - W8 pins the latency: an update plays within one buffer of being
    accepted. W8 alone caught a "play last cycle's MAIN" mutation.
  - `buffer_samples` is therefore the latency knob.
- **H5 (new):** the real `WaveformSource` on the board at buffers of
  100/50/20/10 ms. It sends a MAIN update halfway, marked as −0.0, so the
  output is still 0 V. It reports:
  - whether the output ran dry;
  - the thinnest margin and the most queued;
  - the longest gap between refills;
  - the update's latency.

  `Dev1-sim` stalls rather than running dry, so its numbers only rehearse.
  They showed refills up to 30–50 ms apart even at small buffers: asyncio on
  Windows sleeps in steps of the system timer, not the 1 ms `POLL` asks for.
  That is probably what sets the floor for `buffer_samples`.
- Mutation checks: 10 on the adapter and 3 on the refill, none surviving.

*Second physical run, 2026-09-23 (Claude ran it on `Dev1`, with the user's
authorization for that session only): 15 of 15 passed, in 45 s.*

- H1 is unchanged. Both paths raise `Underflow` at t = 1.0000 s. The driver
  survey matches the first run exactly.
- H2: `drain` returned after 1.0185 s (1.0000 expected). DO 100000, AO
  98763. The AO shortfall varies from run to run.
- H3 and H4 are unchanged. Every refill is clean at 0% CPU, and POLLED is
  refused with −200767.
- H5 ran with no gap at any buffer depth:

  | buffer | refills | min margin | max queued | max refill gap | update latency | CPU |
  |---|---|---|---|---|---|---|
  | 100 ms | 163 | 86.5 ms | 119.8 ms | 33.9 ms | 119 ms | 0.4% |
  | 50 ms | 292 | 49.9 ms | 70.1 ms | 16.1 ms | 61 ms | 1.4% |
  | 20 ms | 300 | 19.9 ms | 40.3 ms | 16.3 ms | 39 ms | 1.4% |
  | 10 ms | 431 | 9.9 ms | 30.4 ms | 16.0 ms | 20 ms | 0.4% |

  - Reading: the most queued is always about 20 ms (≈2000 samples per
    channel) above the host buffer. That is probably the board's onboard
    FIFO, which the generated count does not include. So on the board, an
    update waits one host buffer plus the FIFO, and the margin at a refill
    never drops below the host buffer.
  - Refills were up to 16 ms apart, which is the Windows system-timer step.
    The 30–50 ms seen on Dev1-sim were the simulator.
  - Each depth ran for only a few seconds, so "no gap" does not measure
    tolerance to a long stall such as a GC pause or disk load. At 10 ms,
    about 30 ms is queued against a 16 ms timer step.
  - `buffer_samples` has not been chosen. That is the user's call.

*Refill experiments and the sequence feature, 2026-09-23 (Claude on `Dev1`,
same session authorization).* The user's framing:
- **First-class:** a predefined MAIN shape, with small user adjustments
  landing as fast as possible, between cycles, without interrupting the
  write. DMA stays, as legacy did.
- **Second-class:** a *sequence* of MAINs played in order, for a parameter
  sweep or closed-loop autofocus.
- A step at the cycle boundary is acceptable. What a waveform must satisfy is
  the generating code's responsibility; the source must not modify it.

Findings, all on the physical board, with exploratory scripts in the session
scratchpad:
- **Onboard FIFOs:** AO 3276 samples per channel (16384 shared by the 5
  channels), DO 2047. The transfer condition defaults to "less than full".
- **Which counts are true.** A counter on `ao/SampleClock` showed:
  - the AO generated count follows real clock edges to within 1–4 samples
    while running, so `written − AO generated` is the true lead;
  - the DO generated count runs up to its FIFO's depth ahead of the clock.
    It counts transfers, so never use it for timing.
- **Bug: the refill held the event loop.**
  - The adapter asked only the AO task for space. The DO buffer is usually
    the fuller one, so the DO write blocked inside the driver for 5–12 ms,
    and there was room again by the time it returned. The refill loop then
    never yielded.
  - A 10 ms sleep elsewhere took 150 ms with 50 ms volumes and 900 ms with
    300 ms volumes. A `SetWaveform` was handled only once the cycle had been
    written, a full volume late.
  - The earlier H5 "no gap at 10 ms" was an artefact of this pacing.
  - Fix: `space_available` is the smaller of the two tasks' free space.
    Tested by N4b; H5 now fails if the loop is held for more than 100 ms,
    and the mutation check confirmed it catches the bug.
- **Floor: a host buffer smaller than the AO FIFO is not safe.**
  - After the fix, at 1000 samples every run failed within seconds: −50410
    ("no more space in the buffer") with 15 ms still queued, or ran dry.
  - At 2000 samples, one run in four ran dry.
  - From 3000 up, every run was clean.
  - Free space comes back in blocks of about 2050 samples.
  - Decision: `configure` refuses `buffer_samples` below either task's
    `output_onbrd_buf_size` (N19). The floor is read from the driver, not
    assumed, because it depends on the channel count.
- **Transfer condition:** "when empty" is refused (−200077). "Half full" on
  both tasks is refused at start (−200946) at 20 and 50 ms buffers, and ran
  dry at 10 ms. "Half full" on AO alone saves about 4 ms of lead and costs
  about 3 ms of margin. Kept at the default, which is also legacy.
- **Stall tolerance = what was queued when the stall began.** No other
  factor showed up.
  - At the 3276 floor, 30 ms stalls survive and 40 ms stalls run dry.
  - At 50 ms, 50 ms stalls survive and 60 ms stalls run dry.
  - A 60 s soak at the floor had no gap, but the queue dipped to about
    16 ms on occasional late wakes, against about 30 ms in short runs.
- **Update latency (H6)** runs from one lead up to one volume plus one lead,
  as modelled. The lead is the host buffer plus about 20 ms of FIFO.
  - At the floor: 39–83 ms for 50 ms volumes, and 94–329 ms for 300 ms
    volumes.
  - For long volumes the volume dominates the latency, and only tearing a
    cycle could cut it.
- `buffer_samples` is still the user's call *(later chosen: 5000, see
  "`buffer_samples` = 5000" below)*. The floor is about 33 ms of host
  buffer plus about 20 ms of FIFO: a lead of 30–53 ms, covering stalls of
  roughly 16–30 ms. *(Still true through `WaveformSource`, per H7 after the
  change below: its thinnest queue is ~38 ms. A raw probe that keeps ~47 ms
  queued survives 40 ms.)*

*Pin-level timing and the cost of `is_task_done`, 2026-09-23 (same session
authorization). The user wired P0.1 to PFI9 and AO0 to PFI8, with AO0 bound
to 0–5 V. Counters latched the sample-clock count at each edge.*
- **AO and DO are sample-aligned at the pins.** Over about 2260 irregular
  pulses, both lines appeared 1 sample after the written index (the latch
  convention), with no slips, and AO pin − DO pin = 0 on every pulse.
  - Resolution is one sample (10 µs). Skew below that is not visible here.
  - **Decision (user):** AO feedback is disconnected. DO is trusted as the
    timing reference from now on.
- **Why the free-space readings differ.** Both outputs play the same
  samples.
  - AO `space_avail` runs one AO FIFO (≈3280) ahead of the AO generated
    count, because it counts only the host buffer.
  - The DO generated count runs one DO FIFO (≈2050) ahead of the AO count,
    because it counts transfers into the FIFO.
- **Regeneration rewrites can split AO and DO by a cycle.** With a rewrite at
  FIRST_SAMPLE offset 0, no line tore mid-cycle. But each task's write blocks
  until its own transfer wraps:
  - writing DO first, AO switched one cycle late 12 times in 12;
  - writing AO first, DO switched one cycle late in 2 of 12 (10k) and 2 of 8
    (30k), each time its call took a full cycle.
  - So there is no atomic AO+DO change under regeneration. Streaming with
    regeneration off, which production uses, has no such split.
- **`is_task_done` costs 16 ms at the median and 37 at worst** on Dev1, like
  `Dev1-sim`. The generated count costs 0.03 ms and `space_avail` 0.01. The
  earlier stall and 3276 runs had it in the idle loop, which is why they
  looked worse.
- **Stall tolerance, measured again without it:** about the queued time
  minus 5–10 ms.

  | AO / DO | buffer | queued | survives | runs dry |
  |---|---|---|---|---|
  | DMA / DMA | 3276 | ~47 ms | 40 ms | 50 ms |
  | DMA / DMA | 5000 | ~64–69 ms | 60 ms | 70 ms |
  | INTERRUPT / DMA | 3276 | ~47–52 ms | 30 ms | 40 ms |
  | INTERRUPT / DMA | 5000 | ~64–70 ms | 40 ms | 50 ms |

  Interrupt transfer on AO (as legacy) costs another 10–20 ms.
- **Running dry is declared once the host buffer is empty,** about 13 ms
  before the FIFO finishes. A buffer written once and never refilled lasted
  its full length on the pin.
- Before the error is raised, `space_avail` reads the whole buffer free, not
  0 as H1 recorded after it. *(Reconciled: both are true, at different
  times. See "Running dry, watched" below.)*
- The PCI-6733 has only 3 DMA channels: AO, DO and one counter.

**Change, decided by the user:** `space_available` asks `is_task_done` only
when space reads 0 while playing *and* the AO generated count has not moved
since the last such poll.
- The analog count reads 0 after running dry, so it changes once and then
  stands still. The gap is reported one poll late, about 1 ms.
- Tests: two new tests for no `is_task_done` while the count moves and none
  before `go`, plus one guard for a count that merely stops. Mutation
  checked: all three dry-detection tests catch a check that never asks.
- On Dev1 afterwards, 19 of 19 passed. H1 still reports `Underflow` at
  t = 1.0000 s, and H7 is unchanged: 30 ms survives at the floor, and 40 ms
  runs dry with 38.3 ms queued at the thinnest.
- **Alternatives, in case this misses a gap later:**
  - ask `is_task_done` from a worker thread, off the event loop (costs the
    loop nothing, but the answer arrives late and the thread needs a
    lifetime);
  - ask it at a fixed low rate, e.g. every 50 ms (one 16–37 ms hold per
    interval, and a gap reported up to that late).

**`buffer_samples` = 5000, decided by the user (2026-09-23).** It is
`BUFFER_SAMPLES` in `adapters/nidaqmx/signal_output.py`, and
`read_output_config` uses it when the caller names none. The rig config (in
`app/`, not built yet) may override it.
- On Dev1, through `WaveformSource`, 5000 survived 50 ms stalls and ran dry
  on 60 ms, with 58.3 ms queued at the thinnest. H5 measured the update lead
  at 61 ms. At the 3276 floor: 30 ms survives, lead about 39 ms.
- It trades about 17 ms of latency for 20 ms more stall tolerance. The floor
  leaves 30 ms against a 16 ms timer step, and the soak at the floor dipped
  to about 16 ms queued.
- **Conditions that would call for going higher (10000):**
  - `WaveformSource` shares its event loop with other actors, and H5 or H7
    run in that process report the loop held for more than about 40 ms (a
    long handler, a file write, a GC pause);
  - a long soak at 5000 has its thinnest queue below about 20 ms, or runs
    dry;
  - `is_task_done` goes back onto the loop at a fixed rate (it holds the
    loop for up to 37 ms), or AO goes back to interrupt transfer (10–20 ms
    less tolerance on the raw probe);
  - the rig drops to three AO channels or fewer. The FIFO is 16384 samples
    shared by the channels, so the floor rises to 5461 and `configure`
    refuses 5000;
  - the rate changes. The value is a sample count, so rescale it to keep
    50 ms.
- Test: `read_output_config` without `buffer_samples` gives 5000. Mutation
  checked: setting the default to the floor fails it. Suites green: 382 on
  3.14t and 3.14, 75 in the NI suite.

**Running dry, watched, and the long run at 5000 (2026-09-23, same
session authorization; P0.1 confirmed clear of CTR 0 OUT).** Two new board
tests, both run on Dev1, zeros only:
- **H1 survey 2** (`test_h1_how_free_space_reads_as_output_runs_dry`).
  It reads space and the generated counts every 0.5 ms, from 100 ms before
  output ends to 1 s after, and uses the cheap reads only. It runs with two
  fills (written once at 1 s, and kept fed at 5000 then left), each both
  polled and left alone. Times are from the predicted end, taken from the
  AO count at the last write.
- **The two readings are both true, at different times.** In the kept-fed
  run:
  - At −31 ms, AO space reaches 4993 (the whole host buffer, less 7) and
    holds. The AO host buffer is empty, but the FIFO (3276 samples,
    32.8 ms) is still playing, and the AO count keeps rising.
  - At −19.5 ms, DO space reaches 4936 and holds. The DO count stops
    rising, because it counts transfers into the FIFO.
  - At the end (DO at −0.5 ms, AO at 0 ms), both spaces drop to 0 and stay
    there. After that, `is_task_done` raises −200290 on both tasks.

  So the stall probe's "whole buffer free" was read in the last FIFO's
  worth of output, and H1's 0 was read after output stopped.
- **After running dry, the AO generated count is frozen at the last value
  read before the error.** It is not a live count.
  - Polled, it read 99 988 of 100 000 and 56 951 of 57 000.
  - Left alone, it read 8 and 50 893: the values last read, just after
    `go` and at the last write.
  - H1's "reads 0" and H2's "stopped at 99 985 of 100 000" are this same
    effect. H2's shortfall is therefore not evidence that the last analog
    samples failed to play. Only a scope with non-zero output can settle
    that.
- **The DO task ran dry 64 samples short in the kept-fed run.** Its space
  held at 4936 of 5000 and its count at 56 936 of 57 000, and it raised
  about 0.5 ms before AO. Written once at 100 000, it reached every sample,
  and its `is_task_done` returned False. Whether the last partial block of
  DO samples reaches the lines is open; it matters for the last trigger
  states of a COOLDOWN.
- **Earlier notes disagree on when running dry is detected.** They say it
  is declared "once the host buffer is empty, about 13 ms before the FIFO
  finishes". Watched with the cheap reads only, the task stops within
  0.5 ms of the predicted end, and the AO count keeps rising through the
  FIFO's drain. The 13 ms may be when `is_task_done` first raises, which
  this survey did not call during the window. Not settled.
- **Adapter: no change needed.**
  - In the window where the FIFO is still draining, `space_available`
    returns the buffer free, so the refill writes. That write either
    rescues the output or raises, and H1's late-write test covers the
    raise.
  - After the end, space is 0 and the count is frozen, so the next poll
    asks `is_task_done`, which raises.
  - `Underflow.at` from written ÷ rate was the right choice, since the
    count is frozen.
- **H9** (`test_h9_a_long_run_at_the_instruments_depth`): `WaveformSource`
  at `BUFFER_SAMPLES` for 60 s per volume. Both volumes had no gap and no
  refill below 20 ms.

  | volume | refills | thinnest | 1st percentile | median | loop held at worst |
  |---|---|---|---|---|---|
  | 50 ms | 4108 | 29.3 ms | 43.5 ms | 54.5 ms | 40 ms |
  | 300 ms | 3894 | 35.5 ms | 45.6 ms | 54.5 ms | 36 ms |

  The thinnest refills came after the loop had been held 31–36 ms, so they
  were late wakes, not the refill. The floor's 60 s soak dipped to 16 ms,
  so 5000 meets the "thinnest above ~20 ms" condition with ~9 ms to spare
  over a minute. A longer soak is `STACSCOPE_SOAK_SECONDS`.

**Timing feedback wiring.** Dev1's terminals are `PFI0`–`PFI9` plus the
counter pins. There is no `PFI12`. The pin some connector blocks label
"PFI12 / CTR 0 OUT" is the counter 0 output, which the board drives. For
pin-level timing, P0.1 goes to **PFI9** (ctr0's gate). The user connects it
when asked.

**The last digital samples (H10), 2026-09-23, Claude under the session
authorization; P0.1 wired to PFI9.** Only non-zero output can say whether
the last samples play. P0.1 pulsed through the last 256 samples (4 high,
4 low) and ended low, and ctr0 and ctr1 counted rising and falling edges on
PFI9. The output ended by `drain`.

| host buffer | written | edges (of 32) | DO generated |
|---|---|---|---|
| 100 000 | 100 000 (never wraps) | 32 | 100 000 |
| 100 000 | 57 000 (never wraps) | 32 | 57 000 |
| 5 000 | 5 000 (never wraps) | 32 | 5 000 |
| 5 000 | 57 000 (wraps, ends part-way) | **24** | 56 936 |
| 5 000 | 57 037 (wraps, ends part-way) | **24** | 56 972 |
| 5 000 | 60 000 (wraps, ends on a wrap) | 32 | 60 000 |
| 100 000 | 150 000 (wraps, ends part-way) | **24** | 149 936 |
| 100 000 | 200 000 (wraps, ends on a wrap) | 32 | 200 000 |

- **An output that has wrapped the host buffer and ends part-way through it
  loses its last digital samples.** They are never generated. The loss is
  64 samples plus the end's offset from a multiple of 4: generated =
  (written rounded down to 4) − 64. It does not depend on the buffer's
  depth.
- The DO count, which counts transfers into the FIFO, agrees with the pin
  every time.
- **Every acquisition on the instrument wraps.** Unless COOLDOWN happens to
  end exactly on a wrap, the last ~0.64 ms of digital output never plays,
  and each line is left at whatever it held ~64 samples before the end.
- **Padding fixes it.** Written padding after the real samples brought
  every edge back once it was at least the loss:
  - 32 samples of padding: 28 edges;
  - 64 of padding: 32 edges, though 57 037 + 64 still left 1 real sample
    unplayed;
  - 65 and 128: all 32, with every real sample generated.
- **The analog side is unknown.** Its count cannot be trusted after
  running dry, and AO feedback is retired. Padding both tasks covers it
  either way.
- **Decided: (a), user 2026-09-23. Implemented the same day.** The
  options were:
  - (a) `drain` writes ~128 samples repeating each channel's last value
    before it waits. This lives in the adapter, since it is this board's
    quirk, and a line ends where its last real sample left it.
  - (b) Require COOLDOWN to end with parked values held for at least
    ~128 samples. This lives in the config, and leaks the quirk into every
    rig.
  - (c) Pad to a whole number of buffers. That costs up to 50 ms, for no
    reason beyond (a).
- **The fix.** `TAIL_PAD = 128` is in `adapters/nidaqmx/signal_output.py`.
  - `drain` first writes 128 samples on both tasks, each channel repeating
    its last written value. It waits for room like any write, bounded by
    the drain's deadline.
  - It skips the padding when nothing was written, or when the output has
    already run dry.
  - The padding is not counted in `_written`, so the drain still ends when
    the caller's samples have played, and `Underflow.at` still dates a gap
    by them.
  - Tests, against the fake: the padding holds the last values on both
    tasks, waits for room, still gives up at the deadline, pads nothing
    after running dry or with nothing written, and is not counted.
    Mutation-checked seven ways, all caught.
  - On Dev1-sim, which drops nothing, the counts now end at
    written + `TAIL_PAD`, and three `test_dev1.py` tests were adjusted.
- **On Dev1 afterwards (full suite, P0.1→PFI9):** **50 of 50 passed, all six
  H10 cases included**, once H2 was adjusted.
  - The DO count now ends at written + 64 in every case, including output
    that never wraps: 128 of padding, less the 64 the board drops. The
    padded total no longer ends on a wrap.
  - H2 now allows the DO count up to written + `TAIL_PAD`.
  - H9 in this run: 39.1 ms and 42.1 ms at the thinnest, no refill below
    20 ms.
  - The contract suite gave 19 of 19.

**Full board suite on Dev1, 2026-09-23 (H1–H10, P0.1→PFI9 wired).** 28
passed, and the 3 failures are H10's wrapping cases above. Changes from
the earlier runs:
- **H9's 50 ms volume dipped to 12.5 ms queued**, with 2 refills below
  20 ms, 50 s in, just after the loop was held 57 ms. No gap. Its 300 ms
  volume held 39 ms at the thinnest. The standalone H9 run earlier the same
  day held 29.3 and 35.5 ms. H5 at the floor saw the loop held 47 ms in
  this run too, so the box was busier. One of the conditions for going to
  10 000 (thinnest below ~20 ms) was met once, by one dip. The user
  decides.
- **H7 through `WaveformSource`:** the floor survived 40 ms stalls and ran
  dry at 50; 5000 survived 60 and ran dry at 80. The earlier runs gave
  30/40 and 50/60. H7 has no 70 ms step.
- **H2:** drain returned after 1.0187 s. AO read 99 751, a frozen count as
  described above; DO read 100 000.
- **H3:** 0.0–2.4 % of one core.
- H1, H4, H6 and H8 are unchanged.

**The port contract suite, run against the NI adapter (2026-09-23).** The
5a-2 plan called for "the existing contract suite against an NI MAX simulated
PCI-6733", but what was built was `test_dev1.py`, a separate suite. So
`NiSignalOutput` had never passed the contract tests that `SimulatedSignalOutput`
passes.
- **The suite is now shared.** The tests moved to
  `tests/contract/signal_output_contract.py`, which pytest does not
  collect. Every count is a fraction of the rig's buffer depth, and every
  time a fraction of the buffer's length.
- **Each adapter's suite supplies a `rig`**: its own `OutputConfig`, an
  `elapse` (a virtual advance, or a real sleep), a `slack` in samples, and
  `runs_dry`.
- **The simulated suite is still exact**: slack 0, the same 19 tests and
  the same numbers as before.
- **The NI rigs** use the instrument's shape (5 AO, 3 DO), a 100 000-sample
  buffer and 8000 samples of slack. With no slack, the rate check reads
  −1033 samples on Dev1-sim and +2496 on Dev1, which is the FIFO running
  ahead. Each rig saves its own `stacscope_test_*contract_*` tasks.
  - `tests/adapters/nidaqmx/test_contract.py` runs on Dev1-sim: 18 passed,
    and the running-dry test skips.
  - `tests/hardware/nidaqmx/test_contract.py` runs on the physical board,
    user-run: **19 of 19 on Dev1, running dry included.**
- **They passed first time, so three mutations were checked**, each caught:
  - `wait_for_space` not refusing when nothing plays: the test hangs until
    pytest-timeout;
  - `go` not refusing an empty buffer: one failure;
  - `space_available` reading only the AO task: two failures.

**Sequence of MAINs (W9–W13).** Chosen over two alternatives:
- The legacy idea of a self-message that carries its own waveform would
  leave the queue unbounded and uninspectable. It also couldn't be cleared,
  and its ordering against outside senders would be mailbox-dependent.
- A pull model, where the source asks for each waveform, puts a lead-sized
  deadline on the sender.

What was built:
- **`QueueMain(tag, ao, do)`:**
  - Plays exactly once, in order, one per cycle, and the last one stays.
  - Its length may differ from the current MAIN's, and it is played as
    given.
  - It is validated like `SetWaveform` for channels, dtype and non-empty.
  - The queue is bounded by `queue_limit` (default 8). A full queue is
    `Rejected`, and a slot frees as each entry is taken.
- **`SetWaveform(MAIN)` keeps "this, now".** It empties the queue, so
  adjustments never wait behind a sweep.
- **`MainApplied(tag, cycle, sample)`:**
  - Published when a different MAIN is taken, and at cycle 0 of every
    acquisition. `tag` is `None` for `SetWaveform`.
  - `sample` is the cycle's first sample from the start of warmup, so its
    play time is `sample / rate` after output starts.
  - It is what a closed loop and the acquisition record use to match
    waveforms to volumes.
- **`MainDiscarded(tags, reason)`** is published whenever queued entries
  are dropped: by `SetWaveform`, at cooldown, or at disarm. Nothing queued
  for one acquisition plays in the next.
- On the board, H8 plays six queued steps in order with no gap. They are
  told apart by length, so the output stays at 0 V.
- Mutation checks: eight on the queue, none surviving; one on the board
  (H5 against the reverted space check).
- Not built: routing `QueueMain` through the Coordinator. Like `SetWaveform`,
  it goes straight to the source.

### 5b — the image-streaming role and its capture port *(decided 2026-09-24)*

Proposed after reading the DCAM SDK (`Hamamatsu_DCAMSDK4_v25056964`, in the
user's Downloads), the uEye SDK (`C:\Program Files\IDS\uEye\develop`), and the
legacy camera actor's block diagrams, then settled with the user.

**The shape (user, 2026-09-24):**

- **`ImageStreaming` is a device role, and its primary responsibility is
  management of the buffer** (user; first called `FrameProducer`, renamed the
  same day because a buffer writer need not produce the image, only stream
  it). Like `TimingSource`, it is an actor role
  in `core/`, not a port. Whatever the frames come from, it owns:
  - the segment, for the device's lifetime. The existing mixin, renamed from
    `ImageStreaming` to `ImageStreaming`, is the start of this role;
  - each acquisition's layout, taken from what the hardware says it will
    write, and declared before the first frame can arrive;
  - starting each acquisition at slot 0;
  - publishing: in order, only complete frames, each stamped before it is
    published, and never a frame whose slot the hardware may already have
    overwritten;
  - a final sweep on stop, so every frame delivered before the stop is
    published;
  - seeing that the hardware has let go of the frame memory before the
    segment is closed or re-laid-out, on every exit path. Once DCAM is
    attached it DMAs into our segment, so unmapping first means the driver
    writes into memory that no longer means what it thinks. The legacy
    releases in `Stop Acq` (UNPREP) and unmaps only after `dcamdev_close` in
    `Stop Core`.

  Its contract is written from a consumer's side of the buffer, and every
  producer must pass it.
- **A camera is a special case of `ImageStreaming`** (user). It adds settings
  such as ROI and exposure. A 1D PMT readout, or a computational producer such
  as an SLM or projector pattern, streams images without them. With mixins
  there is no camera base class: a camera is a composition, such as
  `class OrcaFlash(Recording, ImageStreaming, TimedDevice)` holding a
  `FrameCapture`, plus its own settings messages.
- **Hardware sits under the role behind a port, `FrameCapture`** *(name
  confirmed 2026-09-24: "frame", not "image", because the port is the frame
  grabber's side — see "Vocabulary")*. It is deliberately dumb, as
  `SignalOutput` is under `WaveformSource`: it writes frames into memory it is
  given and reports what arrived. It never touches the counter, the header or
  the metadata table. A computational producer has no capture port, and
  writes its slots itself through the same role.

  ```python
  frame_format() -> FrameFormat      # shape, dtype, strides, frame_bytes, slot alignment
  metadata() -> Metadata             # which stamps, their TimeSource, clock_hz
  attach(slots)                      # hand over the frame memory
  start()                            # ready for the first trigger on return
  wait_frames(seen, timeout) -> int  # frames delivered this acquisition; cancellable
  stamps(first, n)                   # hw_frame, device_ticks, host_time_ns
  stop()                             # stop capture; any state, repeatable
  detach()                           # afterwards no reference to any slot remains
  close()                            # release the device; any state, repeatable
  ```

  Attach and start are separate, as are stop and detach, for three reasons:
  both SDKs separate them; DCIMG has to be bound between attach and start;
  and a separate `detach` lets a test check that the frame memory is released
  before the segment closes.
- **Settings are each device's own messages, not port methods** (user: devices
  are controlled by message, so where there is no common logic an
  implementation establishes its own contract). Nothing in core reads ROI or
  exposure. The one consequence core needs is the frame format, which it asks
  the port for on the next arm. `DcamCamera`, and later a `UEyeCamera`, each
  define their own settings messages and answer `Rejected` to what they can't
  do. A shared camera message (ROI, say) is added when a second client needs
  to treat cameras alike, and not before (5a decision 3). The legacy's
  abstract camera class, with overridable Set ROI / Set Exposure, was an OO
  pattern; with actors, a camera is whatever answers camera messages.
- **Lost frames are fatal** (user): they break the contract with downstream
  consumers. `wait_frames` raises `FramesLost`, and the device publishes
  `Fault(fatal=True)`.
- **Recording: the device decides where in `arm` the file opens** (user). DCAM
  requires `dcamcap_record` in the READY state, after attach and before
  `dcamcap_start` (API reference, "Recording control"; the legacy `Start Acq`
  does ATTACH → DCIMG → START). The file closes after capture stops. The
  `Recording` mixin's fixed "open after the chain arms" order cannot express
  that, so it changes; see the correction under Milestone 3. uEye has no
  native recorder, so there the thesis's route applies: a recorder that
  consumes the buffer (§3.4.3).
- **Retired from the tree:** `FrameSink` and `Recorder` as ports. The buffer is
  already the seam between producer and consumers, and a driver-native
  recorder belongs to capture. A generic recorder is a consumer, built when a
  producer without a native one needs recording.
- **The legacy `Stop Core` sets the trigger source to Internal before
  closing.** User: probably resetting to an assumed default, so ignore it.

**Why the port has this shape.** The two SDKs on the dev box match almost call
for call on frame production, and not at all on settings:

| | DCAM | uEye |
|---|---|---|
| Hand over frame memory | `dcambuf_attach` (per-frame pointers) | `is_SetAllocatedImageMem` + `is_AddToSequence` |
| Driver-defined row pitch | `BUFFER_ROWBYTES` | `is_GetImageMemPitch` |
| Start / stop | `dcamcap_start` / `dcamcap_stop` | `is_CaptureVideo` / `is_StopLiveVideo` |
| Wait for frames | `dcamwait_start`, cancelled by `dcamwait_abort` | `is_WaitEvent(IS_SET_EVENT_FRAME)` |
| Per-frame stamps | frame stamp, timestamp (s + µs) | `u64FrameNumber`, `u64TimestampDevice` (0.1 µs) |
| Give the memory back | `dcambuf_release` | `is_ClearSequence` + `is_FreeImageMem` |
| Native recorder | DCIMG, bound between attach and start | none |
| Settings | property IDs; lightsheet mode ties exposure to line interval | `is_AOI`, `is_Exposure`; exposure bound by pixel clock and frame rate |

DCAM can also DMA stamps into user memory (`DCAMBUF_ATTACHKIND_TIMESTAMP` /
`FRAMESTAMP`), and `dcambuf_copymetadata` reads a block at once. How the
adapter gets them is its own business. The SDK also ships Hamamatsu's own
ctypes binding (`samples/python/dcamapi4.py`), a cross-check for struct
definitions in 5c. It is read locally and never copied in; "The DCAM SDK
licence" below has the policy, including the conformance test that does
the cross-check.

**Acceptance tests, in two tiers.**

The role, against every image stream: the simulated camera and a computational
producer now, and DCAM and uEye later, with fakes and on hardware.
- F1. Each arm declares the layout the hardware will write before capture
  starts. A settings change between acquisitions reaches a consumer holding
  the segment as a new `layout_generation`, which it follows.
- F2. Frames are published in order from slot 0, one per frame the hardware
  delivered, each only once complete.
- F3. A frame's metadata is stamped before it is published, and the header
  declares which fields are filled and where device time comes from.
- F4. Stop publishes every frame delivered before it and nothing after, and
  the buffer stays readable (D11).
- F5. The hardware lets go of the frame memory before the segment is closed or
  re-laid-out: on disarm, on the next arm's new layout, and on death
  mid-acquisition.
- F6. Lost frames are a fatal `Fault`, and no frame is published whose slot
  the hardware may have overwritten.

The port, against every capture adapter: a simulated one now, a fake DLL in
5c, and the camera in 5d.
- K1. `frame_format()` is self-consistent, and the slot alignment it asks for
  is honoured.
- K2. After attach and start, one frame per trigger, in slot `i mod n` from
  slot 0.
- K3. No trigger, no frame.
- K4. `wait_frames` returns on new frames and on timeout, and `stop()`
  cancels it promptly.
- K5. Stamps are consecutive, and a lost frame raises `FramesLost`.
- K6. After `detach`, no reference to any slot remains, and attaching again
  with a new layout works.
- K7. `stop`, `detach` and `close` are safe in every state and repeatable.
  After `close`, every call except `stop` raises `RuntimeError`.
- K8. `attach` while started is refused.
- K9. A refused `attach` keeps no hold on the slots it was offered, even
  through its error's traceback: the segment closes while the caller
  still holds the error *(user, 2026-09-25)*.

**The port's shape, settled before K1** *(user, 2026-09-24)*:
- **Files follow `SignalOutput`:** the port and `FrameFormat` / `FramesLost`
  in `core/ports/frame_capture.py`; the contract in
  `tests/contract/frame_capture_contract.py` with its own `Rig`; the
  simulated suite beside it; `SimulatedFrameCapture` in `adapters/simulated/`.
- **`attach(slots)` receives the writer's byte views**, an
  `(n_slots, slot_bytes)` `uint8` array (`FrameWriter.slots`), not raw
  addresses. A DCAM adapter takes pointers from them; a Python adapter
  writes through a typed view of its own. A view the adapter keeps holds the
  segment open, so the segment's close raises `BufferError`. That makes "no
  reference remains" (K6, F5) checkable, which raw addresses would not be.
- **`FrameFormat` is the per-frame half of `Layout`:** shape, dtype, strides,
  frame offset, `frame_bytes`, `slot_alignment` and `in_flight`, which are
  everything the hardware decides. `n_slots` is the role's, from config. One
  core function builds the `Layout`, with `slot_bytes` = `frame_bytes`
  rounded up to `slot_alignment`.
- **Async, like `SignalOutput`:** `attach`, `start`, `wait_frames`, `stop`,
  `detach` and `close` are coroutines. `frame_format`, `metadata` and
  `stamps` are cheap reads, so they are plain calls. A real adapter runs the
  SDK's blocking wait in a thread, and `stop` aborts it. The simulated
  adapter takes a `Clock`, so the contract runs in virtual time.

Order: K1–K8 against a simulated capture port that listens on `TriggerBus`
and writes the moving bar into the slots it is given, as DMA would. Then
F1–F6, with `SimulatedCamera` rebuilt as the role over that port. Then the
`Recording` change.

**Settled before F1** *(user, 2026-09-24)*:
- **Where F1–F6 live:** a role contract,
  `tests/contract/image_streaming_contract.py`, with a rig per stream, as
  K1–K8 do. For now it runs against `SimulatedCamera` over
  `SimulatedFrameCapture` only. A computational producer's rig comes when
  one is built for real.
- **A publisher that falls behind** loses frames: once the hardware may have
  overwritten a delivered frame's slot (more than `n_slots − in_flight`
  behind), none of those frames is published, the device publishes
  `Fault(fatal=True)`, and nothing more is published in that acquisition.
  The same applies to `FramesLost`.
- **`ImageStreaming.close`, in order** (the open item from the buffer fix):
  1. cancel the publish loop;
  2. `capture.close()`, which stops, detaches and releases the device;
  3. if that raised, do not close the segment. Leave it mapped on purpose,
     because the driver may still be writing into it;
  4. otherwise `writer.close()`, whose `BufferError` (a view leaked in the
     producer's own process) propagates only after step 5;
  5. `super().close()`, always.

  The first failure is raised, and any later one is attached to it as a note,
  as the runtime does for `teardown`. No `Fault` is published: a teardown
  error already reaches the supervisor as the death reason. Reasons:
  - only views of the producer's own writer can block its close, never a
    consumer's, since each consumer maps the segment separately;
  - an adapter that drops its view and then fails its release would let the
    close unmap memory a driver is still writing into, so the role does not
    rely on the `BufferError` alone;
  - on Linux a segment left behind is taken over by the next producer under
    that name, so nothing accumulates.

  F5 pins it with a test each: the order on a normal close, a failed
  capture release, and a leaked view.
- **Arm and disarm, with a capture:**
  - arm: configure the layout → start the acquisition → attach the slots →
    arm the rest of the chain → start the capture → start the publish loop
    as its own task;
  - disarm: stop the capture → the loop publishes every delivered frame and
    ends → detach → disarm the rest.

  The capture is optional on the role. Without one, a producer writes its own
  slots, as `SimulatedFreeRunningCamera` does.

**Still open for DCAM:**
- *In-flight depth.* DCAM reports only the newest index and the count, so how
  far ahead the grabber writes is unknown. The header carries `in_flight`, and
  the adapter declares a value. **To be measured** (user, 2026-09-24); see
  below.
- *What selects the imaging device as time source* (see the survey below).
  **Not pursued** (user, 2026-09-24): the driver's stamps are accepted, as
  `TimeSource.DRIVER`.

**In-flight depth, the measurement** *(planned 2026-09-24; nothing written
yet)*. The quantity is the largest k such that, when DCAM reports frame i as
the newest, the grabber may already be writing the slot of frame i + k. The
role then refuses any frame older than `n_slots − in_flight` (F6).
- *Method, a sentinel scan.*
  - Attach many more slots than the burst needs, say 64 slots for 32
    triggers, so nothing wraps.
  - Fill every slot with a sentinel pattern before start.
  - During the burst, in a tight loop, read `dcamcap_transferinfo` (newest
    index and count). Then scan the next few slots past the newest for any
    byte that is no longer the sentinel. Check the first and last rows
    separately, so a frame that is part-written shows up as one.
  - The greatest distance past the newest at which a written byte is found
    is the observed depth.
  - The sentinel must be a value the sensor cannot give at that exposure.
    The user should choose it; a dark, short-exposure frame sits near the
    offset.
- *Conditions.* Full frame, and a small subarray, which reads out faster,
  both at the instrument's trigger rate and at the camera's maximum. Also in
  PROGRESSIVE (lightsheet) readout. Triggered by DAQ pulses on `Dev1` as the
  rig does, and by the camera's internal trigger as a cross-check.
- *Test first, in 5c.* The fake DLL models a configurable depth, and the
  measuring code must recover the fake's depth exactly (red/green on the tool
  itself), including a depth of 0, 1 and 2.
- *On the camera, in 5d.* A script under `tests/hardware/dcam/`, gated as the
  NI suite is. **It triggers the camera, so the user runs it.** The result,
  per condition, is recorded here. The user chooses the declared
  `in_flight`, the observed maximum plus whatever margin they want.
- A depth larger than the camera ever shows cannot be ruled out by
  observation. The margin is there for that.
- *Result on the dev box Flash (2026-09-25, `tests/hardware/dcam/test_in_flight.py`,
  exposure 1 ms, dark, sentinel 0xA5):*

  | Format | software | internal | DAQ (P0.0) |
  |---|---|---|---|
  | full frame (2048 × 2048) | 1 (partial 271) | 1 (271) | 1 (258) |
  | 256 rows, AREA | 1 (12) | 1 (139), 801.7/s | 1 (108) |
  | 256 rows, PROGRESSIVE | 1 (13) | 1 (101), 278.1/s | 1 (79) |

  Minimum trigger interval: 11.079 ms full frame, 2.348 ms at 256 rows
  (2.495 internal), 3.693 ms lightsheet. The tool now re-reads the count
  after each scan (see the 5d status entry).

**The DCAM SDK licence** *(read 2026-09-24; `doc/LICENSE.pdf`, document
A6826000-01; re-read in full the same day, and the policy below decided by
the user. This is not legal advice.)*
- *Licensee (preamble):* "the registered corporate office of the LICENSEE
  representative installing or otherwise using this DCAM-SDK4". The licensee
  is the user's institution, not the user personally.
- *Grant (1.1):* free, non-exclusive, non-transferable, non-sublicensable
  and revocable, to use the SDK "to design and develop software to be used
  with Hamamatsu's camera products" on a separate list. It is "licensed for
  end use only". It does not grant the right to distribute or sell the SDK
  for commercial purposes. Any other use needs "a prior separate written
  authorization from Hamamatsu". That is the licence's own route to
  permission.
- *Restrictions (1.2):* no use except developing for those products. No
  reproducing it or making derivatives except in that development. No
  selling, lending or sublicensing. **No distributing the SDK in any form,
  including its source code.** So `dcamapi4.py`, the samples and the headers
  must never be copied into this repository. Reading them for reference
  while writing our own layer is the development use the grant covers.
- *Derivative works (1.3):* anything derived from studying the SDK counts as
  a derivative work. It may be kept and used for this development, but the
  licensee may not (i) "seek, claim or file for any patent, copyright or
  other Proprietary Right" in it, (ii) make it available to any third party,
  or (iii) use it except for developing for the listed cameras.
  "Derivative work" is defined as "device control software with general or
  similar functionality as provided by the SDK and DCAM-API". That is a
  category, not a size: a ctypes layer over `dcamapi.dll` falls under it
  however thin it is, and so does a fake of the DLL that encodes DCAM's
  behaviour. (i) matters on its own: an open-source licence rests on a
  copyright claim, so a DCAM file cannot carry one.
- *Marking (1.4):* "Any source code developed by LICENSEE using the SDK
  which discloses any part of the communication protocol of DCAM-API that is
  released on the public domain must contain the following commented
  statement: 'For end use and not for commercial purposes.'" 1.3 and 1.4 pull
  against each other: one forbids giving a derivative to third parties, the
  other describes marking one that is public. "That is released" can modify
  the source or the protocol; on the second reading, 1.4 covers code that
  discloses only what Hamamatsu itself has published. Either way the licence
  expects marked public source to exist.
- *Termination (3.1–3.3):* Hamamatsu may end the agreement at will (3.1). A
  breach ends it only if not cured within 30 days of notice (3.2). On
  termination "all of the licenses granted hereunder shall terminate" and
  the SDK is destroyed (3.3). That includes 1.3's right to keep using
  derivative works, so a strict reading loses the right to run our own DCAM
  adapter on the instrument. The driver (DCAM-API, which provides
  `dcamapi.dll`) is under a separate licence, not read yet.
- *No waiver (4.7):* Hamamatsu not enforcing an obligation does not waive
  it. Other projects publishing DCAM wrappers unchallenged gives us nothing
  to rely on.
- *Other terms:* Japanese law, Tokyo courts (4.2). Hamamatsu may revise the
  terms, and the latest version wins (4.6).

- *The API reference's own preamble* (`doc/api_reference/dcamapi4_en.html`,
  "Overview → SDK"; found 2026-09-24 in 5c, reported to the user): the SDK
  user may "giv[e] the host software or plugin module to everyone", but may
  not open "the resources that get from HAMAMATSU as SDK", nor make "a
  wrapper module that is almost the same as DCAM-API itself". This is looser
  than LICENSE.pdf on distributing host software, and adds a line on wrappers:
  our binding should stay private to the adapter and expose `FrameCapture`,
  never a general DCAM-API surface.

**How others handle it** *(surveyed 2026-09-24, reported to the user).*
- *Micro-Manager* keeps its Hamamatsu adapter out of public view. The
  `HamamatsuHam` adapter is written by Hamamatsu (HCRDG, HCRDG@hamamatsu.com)
  and lives in the private `micro-manager/SecretDeviceAdapters`, cloned only
  by team members and CI; its page's licence field reads "Source code cannot
  be made available". Only the compiled adapter ships, and users install
  DCAM-API themselves. The older UCSF adapter was LGPL, but "we have been
  asked not to distribute the source". The same wording covers Nikon and
  Zeiss adapters.
- *areaDetector/ADHamamatsuDCAM* (C++, EPICS) is the one sanctioned public
  release. It grew from a prototype written by Hamamatsu Photonics and is
  copyright Hamamatsu Corporation and the University of Chicago, under an
  open licence. `hamamatsu.h` carries the 1.4 marking (the only file on
  GitHub that does), the SDK is not in the repository, and issues go to
  HCRDG.
- *Python projects that write their own layer and ship no SDK files*:
  pyLabLib (GPL-3.0; definitions generated from `dcamapi4.h`),
  python-microscope (GPL-3.0; hand-written from the headers),
  tiagocoutinho/hamamatsu (GPL-3.0, on PyPI), pyDCAM (on PyPI), and the
  storm-control lineage (mesoSPIM-control, PYME, navigate). None carries the
  marking, none mentions Hamamatsu's terms, and all but pyDCAM claim a
  copyright, which 1.3(i) forbids.
- *Projects that commit Hamamatsu's `dcamapi4.py` itself*, "All right
  reserved" header and all: coPylot (BSD-3), Squid (BSD-3), microEye
  (GPL-3.0), voxel (MIT), openCLEM (MIT), and dozens of lab repositories.
  That is distribution of the SDK, which 1.2(d) forbids.
- No Hamamatsu notice appears in GitHub's DMCA archive. By 4.7, that
  proves nothing. Projects from before 2025 may have accepted older terms;
  not checked.

**Policy** *(user, 2026-09-24).* Follow the Python projects cautiously:
our own ctypes layer, as thin as possible, and never the SDK in the
repository. With these rules, which the user accepted:
- **No SDK file in the repository, ever**: no header, no `dcamapi4.py`, no
  sample, no library. Read them locally for development; that is the use
  the grant covers. At runtime the layer needs only `dcamapi.dll`, which the
  DCAM-API driver installs.
- **The 1.4 marking** in a comment in every DCAM file, adapter and fake
  alike, from the first commit.
- **No copyright line and no project licence on the DCAM files** (1.3(i)).
  When STACScope gets a licence, it must name `adapters/dcam/` and the fake
  as excluded.
- **Removable within the 30-day cure.** Nothing in `core` imports the
  adapter, and it installs as an optional extra, as NI-DAQmx does. If the
  project ever goes public, publish the core first and keep the adapter in
  a repository of its own: once a file is in public git history, removing
  it means rewriting that history.
- **Thin in two ways**: bind only the functions the adapter calls (expected
  to be about 20–25, including `dcamrec_*` for DCIMG files and
  `dcambuf_attach`; confirm in 5c), and hard-code as few constants as
  possible. Look up property IDs by name at runtime (`dcamprop_getnextid`,
  `dcamprop_getname`), value labels with `dcamprop_getvaluetext`, and error
  text with `dcamdev_getstring`. Storm-control looked up properties by name.
  Struct layouts still have to be hard-coded.
- **A local-only conformance test** reads the installed SDK headers from a
  path in an environment variable and checks our struct sizes, offsets and
  hard-coded constants against them. It is skipped when the variable is
  not set, like the hardware suites. It is how `dcamapi4.py` gets used as
  a cross-check without being brought in.
- **Sharing is not decided.** The repository is private (checked
  2026-09-24) with no project licence, so 1.3(ii) is not engaged. Before
  the adapter goes to anyone outside the licensee (a collaborator, another
  lab, a public release), the institution decides. The route is 1.1's
  written authorization, through HCRDG@hamamatsu.com, which both
  Micro-Manager and areaDetector deal with. Once asked, a "no" binds in a
  way that silence does not.

**Survey of the dev box camera (2026-09-24).** A read-only dump of every
property. Claude wrote the script; the auto-mode classifier blocked Claude
from running it, so the user ran it. It opens the camera, reads and closes, and
makes no set, attach or capture call.
- **Identity:** C13440-20C (ORCA-Flash4.0 V3), S/N 300661, firmware 4.20.B,
  on Camera Link through an Active Silicon FireBird (`AS-FBD-1XCLD-2PE4L`).
  DCAM module 25.12.4322.7073, driver 8.13.30.7073, DCAM-API 4.00. Whether
  the instrument runs the same versions is to be checked, as it was for
  NI-DAQmx 20.0.
- **Time stamps come from the DCAM module, not the camera.**
  `TIME STAMP PRODUCER` and `FRAME STAMP PRODUCER` both read DCAM MODULE.
  IMAGING DEVICE is a listed value, but both properties are read-only, and
  what would select it is unknown. So in this configuration:
  - device time is host software time, which maps to `TimeSource.DRIVER`;
  - `hw_frame` is DCAM's own count, which may not show a frame the camera
    exposed but never delivered.

  This answers the "needs checking on the instrument" question under
  "Open items → Consumer API", for the dev box.
- **Geometry, full frame, MONO16:** `BUFFER ROWBYTES` 4096,
  `BUFFER FRAMEBYTES` 8 388 608, top offset 0. There is no row padding, and a
  slot is 8 MiB, a whole number of pages. Subarray moves in steps of 4 px.
- **What may change when**, from the `ACCESSREADY` / `ACCESSBUSY`
  attributes:
  - *While capturing:* exposure time; trigger source, mode, polarity,
    connector, delay and times; output triggers; master pulse; defect
    correction.
  - *Only with no buffer attached:* sensor mode, binning, subarray, bit depth,
    pixel type, readout speed (the data-stream properties). So a ROI change
    needs `detach`, which stop already provides.
  - *With a buffer attached but not while capturing:* trigger active (edge,
    level, sync readout).
- **Sensor modes:** AREA, PROGRESSIVE (the legacy's mode 12, lightsheet
  readout), SPLIT VIEW, DUAL LIGHT SHEET. The camera sat in AREA with internal
  trigger, its defaults, since nothing had configured it.
- **Timing in AREA, full frame:** readout 10.0 ms; minimum trigger interval
  20.0 ms at 10 ms exposure; line interval 9.74 µs.
  `TIMING MIN TRIGGER INTERVAL` is what the legacy's
  `Get Minimum Trigger Interval.vi` computed by hand; see "Open items →
  Device-declared timing floors".
- **Recorder sizing:** 8 388 672 bytes per frame (frame + 64), 1016 per
  session, 256 per file. These are the inputs to sizing a DCIMG file from
  free disk space, as the legacy did.
- **For the camera-as-timing-source case:** a master pulse (continuous,
  start or burst, 10 µs–10 s) and three programmable output triggers.

### 5f — the uEye camera *(investigated and decided 2026-09-30; done 2026-10-01)*

Written by Claude while the user was away, so that a new session can pick it
up. Marks: *(user)* is what the user said; everything else is a finding or a
proposal, to be taken to the user before any of it is built.

**What the user said (2026-09-30):**
- The uEye on this machine is the instrument's camera: testing here is
  testing against the true instrument.
- It is the Thorlabs model (µManager's `ThorCam-uEye`, triggered on
  `ThorCamTrigger`, `Dev1/port0/line2`), but it runs on **IDS's** driver on
  purpose. Thorlabs' own documentation pointed to IDS's driver for a Python
  example, and switching fixed bugs in Thorlabs' drivers. So the adapter
  targets IDS's uEye API, not Thorlabs' uc480.
- Use pyueye, since IDS provides it.
- Look at IDS's newer SDK too, though the camera is old and may not be
  supported by it.

**What is on the machine** (read-only; the camera was opened, queried and
closed, and nothing captured):
- IDS Software Suite 4.97 (`C:\Program Files\IDS\uEye`). `ueye_api_64.dll`
  4.97.4111 is in System32. The SDK has headers (`develop\include\ueye.h`),
  the full API manual as HTML (`help\uEye_Manual`, 911 pages), and C++
  sample sources (`develop\sources`: `uEyeImageQueue`, `uEyeSequence`,
  `uEyeEvent` and `uEyeTimestamp` are the relevant ones). Nothing further is
  needed from the user.
- Windows lists the camera as "uEye UI-324x Series"
  (`USB\VID_1409&PID_3240`), driver OK. It is camera ID 1, model
  `UI324xCP-NIR` (a UI-3240CP-NIR), S/N 4103283431, board ID "IDS GmbH",
  dated 13.03.2018, board type 0x64 (USB 3 uEye CP).
- **Probably a CP Rev. 1.** Its feature bits (`is_DeviceFeature`,
  0x68001ef) lack the image-memory mode that only the CP Rev. 2 has (the
  manual's "is_DeviceFeature: USB 3 uEye CP Rev. 2 image memory" page). To
  be confirmed on the camera (Y1), because the timestamp's meaning depends
  on it.

  | | |
  |---|---|
  | Sensor | 1280 × 1024 mono, global shutter, 5.3 µm (e2v EV76C560) |
  | Link | USB 3, 4000 Mb/s reported |
  | Trigger modes | freerun, software, rising (`LO_HI`), falling (`HI_LO`); delay settable |
  | Pixel clock | 7–86 MHz (62 values); 24 MHz after init |
  | Exposure | 0.009–66.6 ms at 24 MHz; bound by pixel clock and frame rate |
  | Frame rate | 15 fps after init |
  | AOI | width step 4, height step 2, minimum 16 × 4; position step 2 |
  | Binning / subsampling | binning 2 modes; no subsampling |
  | I/O | 2 GPIOs; flash (strobe) output |
  | Timestamp configuration | not supported (no reset from a pin) |
  | Test images | supported, so a capture test can run with no light |

- ctypes from 3.14t loads the DLL and opens the camera, both directly and
  through pyueye.

**The SDK: pyueye over the uEye API (proposed).**
- *pyueye* 4.96.952 (PyPI, 2022-06-28) is IDS's own binding, under the
  **3-Clause BSD licence**. It is pure Python over ctypes
  (`py2.py3-none-any`, one dependency, `enum-compat`), so it installs on
  3.14t. It is a line-for-line mirror of the C API. It binds everything the
  port needs: `is_SetAllocatedImageMem`, `is_AddToSequence`,
  `is_ImageQueue`, `is_UnlockSeqBuf`, `is_GetImageInfo`, `is_CaptureStatus`,
  `is_CaptureVideo` / `is_StopLiveVideo`, `is_ClearSequence`,
  `is_FreeImageMem`, `is_Event`, and the settings functions. It also defines
  the structs and constants, so we need not transcribe `ueye.h`.
  - It **loads the DLL on import** and raises `ImportError` without it. So
    the adapter imports pyueye lazily, and a test passes a fake in its
    place, as `DcamFrameCapture(library=...)` does.
  - On 3.14 its docstrings raise `SyntaxWarning` (invalid escapes) the first
    time they compile. That is harmless: our pytest config does not make
    warnings errors.
  - It has not been released since 2022, against SDK 4.96, and the DLL here
    is 4.97. The C API it mirrors is stable, so that should not matter.
  - It opened this camera from 3.14t (`is_InitCamera`, `is_GetSensorInfo`,
    `is_ExitCamera`). `sizeof(UEYEIMAGEINFO)` is 96.
- *IDS peak* is what IDS now recommends for new projects. It calls the
  Software Suite "legacy, still supported". Older `UI-` cameras work under
  IDS peak through its "uEye transport layer", which needs Software Suite
  4.95 or later installed beside it. So the camera is supported, but:
  - the transport layer offers RAW pixel formats only, no trigger-controlled
    exposure, and no fine exposure or extended pixel clock. IDS's pages say
    nothing about timestamps or user buffers;
  - the Python binding, `ids_peak` 1.17.0 (2026-09-28), is under a
    **proprietary** licence, and ships only `cp310-abi3` wheels. **This
    interpreter accepts `abi3t` but not `abi3`**, checked with
    `packaging.tags`, so it cannot be installed into the engine's
    free-threaded environment. The only route would be a device process
    running standard 3.14, as the monitor does. The transport could place
    one there, but that is a new kind of deployment for one camera;
  - it adds a GenICam layer over the same uEye driver, which would still
    have to be installed.

  Proposal: pyueye over the uEye API now. Revisit IDS peak if IDS drops the
  Software Suite, or `ids_peak` ships free-threaded wheels.

**Licence** (proposed; not legal advice):
- pyueye is BSD-3. It is taken as a dependency, never copied in, so its
  notice travels with the package.
- The Software Suite has **no licence text on disk**. `legal/` holds only
  the third-party notices (Qt under LGPL-3), and none was found online. The
  headers carry "(c) 2004–2025 IDS". The installer presumably showed an
  end-user licence; ask the user whether they kept it.
- Until the terms are known, apply the DCAM policy's first rule: **no IDS
  file in the repository** (no header, sample, manual text or DLL). With
  pyueye supplying the structs and constants, the adapter should need none.
  The fake mimics pyueye's surface (its function names and call shapes),
  which is a BSD-licensed API. DCAM's other rules (the marking, no
  copyright line) come from Hamamatsu's licence, and nothing here calls for
  them.
- A conformance test like DCAM's would check pyueye's struct sizes against
  `ueye.h` when `STACSCOPE_UEYE_SDK` names the SDK folder. It is cheap
  insurance against the 4.96/4.97 gap.

**Findings that shape the design:**
1. **Our own memory.** `is_SetAllocatedImageMem` makes user memory an image
   buffer, then `is_AddToSequence` adds it to the ring, then
   `is_ClearSequence` + `is_FreeImageMem` take it back (which does not free
   it). The row pitch is `width × bytes per pixel`, with no padding, so
   `slot_bytes` is the frame's size. The manual says the memory "must
   always be locked globally". Whether a shared-memory segment (a mapped
   file) satisfies the driver is unknown, and is checked on the camera (Y2).
   If it does not, the fallbacks are `VirtualLock` on the segment's pages,
   or copying into the slot (a copying producer, `in_flight` 1).
2. **Queue mode fits the port** (proposed). `is_ImageQueue`:
   - it is first-in first-out, and each wait returns the memory ID of the
     oldest frame not yet taken;
   - a capture error enters the queue in order, as a return of
     `IS_CAPTURE_STATUS`, so a lost frame is in-band;
   - the wait can be cancelled (`IS_IMAGE_QUEUE_CMD_CANCEL_WAIT`), which is
     what `stop` needs;
   - a buffer locks itself when filled, and the driver reuses it only after
     `is_UnlockSeqBuf`. The manual says a frame's info matches its data
     only while it is locked, so the adapter reads `UEYEIMAGEINFO` before it
     unlocks. The unlock is the adapter's alone. Consumers are protected by
     the buffer's `in_flight` rule, not by locks.

   The alternative, ring mode with `is_GetActSeqBuf` on each frame event,
   can skip frames at high rates, by the manual's own warning. Rejected.
3. **Slot order must be checked.** The buffer assumes frame *k* lands in
   slot *k mod N*. Queue mode says which memory each frame went to, so the
   adapter checks it and raises if a frame is out of place, rather than
   trusting the driver's fill order. The camera test is Y3. How many
   buffers a sequence may hold is not documented; that is Y4.
4. **The colour mode defaults to 8-bit BGR packed**, even on this mono
   camera (`is_SetColorMode(IS_GET_COLOR_MODE)` read 1 after init). Opening
   must set a mono format, or every frame is three times the size. Also:
   - the MONO modes pass through the camera's LUT and gamma; the SENSOR_RAW
     modes do not. For science data RAW is probably right (*ask*);
   - 10- and 12-bit formats sit in 16 bits and some are filled from the
     MSB with fewer payload bits than named, depending on the camera
     model. As with DCAM, only formats numpy describes exactly are exposed
     until the bit placement is checked on the camera (Y5);
   - packed formats are unpacked by the API on the host, so a frame's final
     write into the slot is software, not DMA. The port does not care.
5. **Timestamps differ from the plan's assumption.** On "USB 3 uEye CP
   Rev. 1" (and USB 2, and USB 3 LE), `u64TimestampDevice` marks when the
   frame **finished transferring to the PC**, not the trigger (0.1 µs
   ticks). Only GigE and newer USB 3 cameras stamp the trigger. The manual
   gives a formula to estimate the exposure start from it. Which clock
   counts those ticks is not stated. The host timestamp (`UEYETIME`) is
   1 ms resolution, resynchronized to system time every 60 s.
   `u64FrameNumber` is "internal image number (not chronological)".
   Consequences, pending Y1 and Y6:
   - the `TimeSource` for device time is undecided (DEVICE or DRIVER);
   - lost frames are caught by the capture status, in the queue and from
     `is_CaptureStatus` counters (`DRV_OUT_OF_BUFFERS`,
     `USB_TRANSFER_FAILED`, `DEV_MISSED_IMAGES`, ...). Gaps in frame numbers
     count as well only if Y6 shows them sequential. The DCAM decision
     (both an error and gaps, user) is the model.
6. **Trigger.** On the instrument the DAQ triggers the uEye from
   `Dev1/port0/line2`. Whether that line is wired to this camera now is
   unknown (*ask*). Exposure is bounded by frame rate and pixel clock even
   under a trigger, so settings apply in order: pixel clock, then frame
   rate, then exposure.
7. **No native recorder.** Recording a uEye means the thesis's route: a
   recorder that consumes the buffer (§3.4.3, and "Retired from the tree"
   in 5b). That is new core work, not part of the adapter (*ask* whether it
   is wanted now).
8. **Opening.** `is_InitCamera` is opened in a worker thread, as DCAM is,
   so heartbeats are answered meanwhile. The camera gets a process of its
   own, where a vendor driver belongs, and the heartbeat is mandatory there
   (Milestone 4). Whether the driver frees the camera when its process is
   killed is checked on the camera (Y7), as the Flash's recovery was.

**Settings: the device's own messages (5b decision).** The uEye has no
property-by-name system like DCAM's: each setting is its own function
(`is_PixelClock`, `is_Exposure`, `is_SetFrameRate`, `is_AOI`,
`is_SetColorMode`, `is_SetExternalTrigger`, `is_SetTriggerDelay`,
`is_SetHardwareGain`, `is_SetBinning`). Two shapes, for the user to choose:
- (a) **One message per setting**: `SetExposure(ms)`, `SetAOI(x, y, w, h)`,
  `SetPixelFormat(name)`, `SetTrigger(mode, delay_us)`, `SetPixelClock(mhz)`,
  `SetFrameRate(fps)`. Each answers with the value the camera took, or
  `Rejected`. Explicit, and typed by the codec.
- (b) **The Flash's messages reused**: `SetProperty(name, value)` /
  `GetProperty(name)` answered `PropertyValue`, with a table of names in
  the adapter. A client could then treat both cameras alike, but 5b said a
  shared camera message waits for a second *client*, and this is a second
  camera.

Recommendation: (a). It needs no table of names, and it keeps a shared
message for when a client needs one.

**Proposed work, in order** (as 5c–5e went; tests first, and each passing
test gets its mutation check):
- **5f-1, survey on the camera** (Y). A script under `tests/hardware/ueye/`
  that puts back every setting it changes, and uses the test image where
  light does not matter:
  - Y1: revision, and what `u64TimestampDevice` measures: against host time
    under freerun, and against DAQ pulse times if line 2 is wired.
  - Y2: `is_SetAllocatedImageMem` over our shared-memory segment.
  - Y3: slot order under queue mode.
  - Y4: the largest sequence the driver takes.
  - Y5: bit placement of the 10/12/16-bit formats (test image, known
    values).
  - Y6: whether `u64FrameNumber` is sequential.
  - Y7: reopening after the process is killed.
- **5f-2, `UEyeFrameCapture` over a fake pyueye** (U), in
  `adapters/ueye/capture.py`, with the fake in `tests/adapters/ueye/fake_ueye.py`:
  - U-tests: importing the adapter needs no pyueye; open sets the pixel
    format and closes what it opened on any failure; `frame_format` from
    the AOI and format; attach registers every slot in order and checks the
    pitch; start inits the queue, then capture; `wait_frames` counts, checks
    each slot, and raises `FramesLost` on a capture-status return;
    stamps come from the info read before unlock; stop cancels a wait;
    detach exits the queue, clears the sequence and frees every memory ID;
    close is repeatable;
  - the port contract (K1–K9) over the fake.
- **5f-3, the port on the camera**: K1–K9, `measure_in_flight` (as M1),
  and a sustained-rate run with no loss (as M2).
- **5f-4, `UEyeCamera(ImageStreaming, TimedDevice)`** (E), in
  `adapters/ueye/camera.py`, with its messages in `adapters/ueye/messages.py`.
  The role contract (F1–F6) over the fake, then on the camera.
- **5f-5, DAQ-triggered**: frames on line 2's pulses, if it is wired.
  *Done without work of its own: Z2–Z3 and W2–W3 ran on line 2 (closed
  2026-10-01).*
- *Packaging:* an optional extra, `ueye = ["pyueye>=4.96"]`.
  `tests/adapters/ueye/` and `tests/hardware/ueye/` stay out of the default
  run, as the other vendors' tests do. The hardware suite is gated on
  `STACSCOPE_UEYE` (the camera ID), and the DAQ-triggered test also on
  `STACSCOPE_UEYE_TRIGGER`. Windows only, like DCAM, unless the user wants
  the Linux driver kept open.

**Decided (user, 2026-09-30)**, answering the proposal's seven questions:
1. **pyueye over the uEye API.** IDS peak is not used now. Revisit it if IDS
   drops the Software Suite, or `ids_peak` ships free-threaded wheels.
2. **No IDS file in the repository.** The Software Suite's end-user licence
   was not kept, so its terms are unknown. pyueye supplies the structs and
   constants.
3. **Settings are (a): one message per setting** (`SetExposure`, `SetAOI`,
   `SetPixelFormat`, `SetTrigger`, `SetPixelClock`, `SetFrameRate`). Each
   answers with the value the camera took, or `Rejected`.
4. **RAW pixel formats (no LUT or gamma), at 8, 10 and 12 bits.** Each depth
   is exposed only after Y5 confirms its bit placement on the camera.
   `SENSOR_RAW8` needs no such check. *(Narrowed after Y5, below: RAW8 and RAW10
   only.)*
5. **`Dev1/port0/line2` is wired to the uEye's trigger input** on this
   machine. 5f-5 and Y1's check against pulse times can run here.
6. **Recording for the uEye comes later.** 5f is capture and the device only.
   A buffer-consuming recorder is separate core work.
7. **Claude may run the survey's captures on the uEye in the session of
   2026-09-30, and may pulse the DAQ's digital lines.** No analog output may
   be set to anything but 0 V, even when the AO task runs only to supply
   `ao/SampleClock` to the digital lines.

**5f-1, the survey: done on the camera (2026-09-30), run by Claude.**
`tests/hardware/ueye/test_survey.py`, 13 tests, all green. It is gated on
`STACSCOPE_UEYE` (camera ID, 1 here), and its DAQ test on
`STACSCOPE_UEYE_TRIGGER` (`Dev1/port0/line2`). Run it with `--extra ueye
--extra nidaqmx`. The helpers are in `ueye_rig.py`. The DAQ's pulse train
moved from the DCAM rig to `tests/hardware/daq_pulses.py`, shared by both
camera suites. It drives one digital line clocked by counter 0, and creates
no analog task. pyueye is the `ueye` extra.
- **Y1: a CP Rev. 1.** Its feature bits (0x68001ef) lack
  `CAP_MEMORY_MODE`. **The device stamp marks the end of the transfer.**
  Under software trigger it moved 38.96 ms for 39 ms more exposure. It is
  in 0.1 µs ticks. On DAQ pulses (rising edge, `LO_HI`), 80 pulses gave
  exactly 80 frames and no more. The stamps were spaced 25 ms apart, spread
  26–100 µs across runs (the transfer's jitter). The device clock agrees
  with the DAQ's timebase to within the measurement, 13–33 ppm by run.
- **Y2: our shared-memory segment is accepted as image memory.**
  `is_SetAllocatedImageMem` took all eight slots, and the white test image
  landed in them as 255. No `VirtualLock` was needed. The pitch is the width
  (no padding).
- **Y3: queue mode fills slots in order and wraps** (three times round a
  ring of eight). **A slot still locked when its turn comes is skipped in
  silence.** The next frame lands in the following slot, and no capture
  error is counted. Only the adapter's slot-order check can catch it. The
  test asserts this, so a driver that changes it fails the test.
- **Y4:** a sequence of 8192 memories (the most tried) is accepted and
  captures.
- **Y5: RAW10 and RAW12 sit from the LSB** in 16 bits: white reads 1023 and
  4095, and RAW16 reads 65535. In all three, the sensor's gray wedge has
  exactly **1024 distinct values**, spread over the full range. So RAW12 and
  RAW16 carry the sensor's 10 bits rescaled, not more information (the
  EV76C560 has a 10-bit ADC).
- **Y6: `u64FrameNumber` steps by 1 within a capture.** It goes on counting
  sensor frames between captures and between sessions (gaps of 5–6 across
  a stop and start in freerun), so a gap means a loss only within one
  capture.
- **Y7: the camera opens after its capturing process is killed.**
  `is_InitCamera` answers `IS_DEVICE_IN_USE` (213) for about 1.5 s, then
  succeeds, and the camera captures.
- **Y8: `is_InitCamera` starts from the camera's defaults.** Colour mode,
  AOI, pixel clock and exposure changed in one session were back to
  BGR8, full frame, 24 MHz and 66.6 ms in the next.

- **Y9: the full sensor in RAW10, DAQ-triggered at 20 fps, loses
  nothing** (the user's requirement for testing, 2026-09-30). 200 pulses
  gave 200 frames, in order, with no gap and no capture error, at 40 MHz
  and 10 ms exposure. `STACSCOPE_SOAK_SECONDS` lengthens the run. **Under a
  trigger this sensor does not expose while it reads out**, and **a
  trigger that arrives while it is busy is ignored in silence**. The first
  try, at 31 MHz (22.2 fps freerun, about 45 ms of readout) plus 10 ms of
  exposure, got every other pulse. No `DEV_MISSED_IMAGES` or any other
  capture error counted the lost triggers. So the trigger period must
  cover the readout (one over the freerun maximum) plus the exposure. The
  camera cannot report a trigger it missed. *(Wrong: it can. See the
  probes below. Only the capture status counters were read here.)*

**Decided (user, 2026-09-30), from the survey:**
- **Device time is the raw stamp**: `TimeSource.DEVICE`, the camera's
  clock at transfer end, in 0.1 µs ticks. It is not corrected to the
  exposure start.
- **Pixel formats: RAW8 and RAW10 only.** RAW10 is LSB-aligned `<u2`.
  RAW12 and RAW16 are not exposed: they carry no more than RAW10.
- **Full-frame RAW10, DAQ-triggered at 20 fps, must be sustained** for
  testing. Y9 shows it is, at 40 MHz.

**What the survey means for 5f-2 (proposed):**
- *Settings.* A trigger the sensor is too busy for is lost without a
  trace. So `SetPixelClock`, `SetExposure` and `SetAOI` could answer with
  the shortest trigger period they now allow (readout plus exposure), and a
  client could check its waveform against it. *Ask* before building.
- *Loss.* A capture-status return in the queue, a slot out of place, or a
  gap in `u64FrameNumber` within one capture, counted from the capture's
  first frame.
- *Opening.* After a crash, `is_InitCamera` refuses with
  `IS_DEVICE_IN_USE` for a moment. Opening should retry that code for a few
  seconds before failing the launch.

**Decided (user, 2026-09-30):** the shortest trigger period is **a query
message of its own**, not an answer to the settings messages. The Flash
will probably want it too, so the message is shared (see 5f-4).

**Probes on the camera, before 5f-2 (2026-09-30), run by Claude.** Scripts
in the session's scratchpad, not kept: each question was one the fake had
to answer.
- **The camera counts triggers it ignores.** The manual ("Trigger mode")
  says an internal counter records them. `is_CameraStatus(IS_TRIGGER_MISSED,
  IS_GET_STATUS)` returns the count since the last read, and the read
  resets it. It counted every lost pulse exactly (20 of 40 at 0.97 of the
  shortest period). It counts nothing outside a capture: pulses before
  `is_CaptureVideo` were not counted. `IS_EXT_TRIGGER_EVENT_CNT` counts
  only the triggers taken. Y9's "cannot report a trigger it missed" read
  only the capture status counters.
- **The shortest trigger period is trigger delay + exposure + the minimum
  frame time** (`is_GetFrameTimeRange`, which is the readout), and the
  sum is exact. Full frame RAW10, 10 ms exposure: at 31 MHz (44.97 ms
  readout) and at 40 MHz (34.91 ms), 1.00× lost nothing and 0.97× lost
  every other pulse. At 40 MHz, 5 ms exposure: 1.00× clean, 0.98× lost
  half; with a 5000 µs delay, the sum without it lost half, the sum with
  it lost nothing. The manual's formula is an image; this is what it
  measures. No margin is built in: a client should leave its own.
- **With every memory locked, a frame is lost as a capture status.** Six
  pulses into four memories, none unlocked: four frames, then two
  `IS_CAPTURE_STATUS` returns (3) in order, then `IS_TIMED_OUT` (122). The
  counters said `API_NO_DEST_MEM` twice.
- **Frames stay in the queue across a stop**, and can be taken after it.
- **Each capture starts at the sequence's first memory**, whatever memory
  the last capture ended on.
- **A cancelled wait returns `IS_OPERATION_ABORTED` (157).**
- **`is_ForceTrigger` gives one frame in a hardware trigger mode**, so the
  capture can be driven without the DAQ.
- `IS_IMAGE_QUEUE_CMD_GET_PENDING` returns the number of queued entries.

**5f-2: done over the fake (2026-09-30).** `adapters/ueye/capture.py`, with
the fake in `tests/adapters/ueye/fake_ueye.py`. U1 is the port contract
(K1–K9, three formats); U2–U12 are the uEye's own, in `test_capture.py`.
Every mutation tried was caught. The two that at first survived were
gaps in the fake, not the adapter. Run: `uv run pytest tests/adapters/ueye`.
- **A consumer thread takes each frame as it lands.** The driver locks a
  memory it fills until it is handed back, and with every memory locked it
  loses the frame (probe). Taking frames only inside `wait_frames` lost
  them whenever the caller was slow, and made the grabber depend on being
  asked, which the port's does not. So `start` runs a thread that waits on
  the queue, reads each frame's info, unlocks it and counts it. The thread
  also reads the missed-trigger count after every wait. `wait_frames`
  waits on the count. `stop` cancels the thread's wait and joins it; the
  thread first takes whatever was queued before the stop.
- **Loss, four ways**: a capture status in the queue (consecutive ones are
  one gap), a gap in `u64FrameNumber`, a missed trigger, and a frame in a
  slot out of order (a plain `RuntimeError`: nothing was lost, but it was
  put in the wrong slot). A missed trigger's `after` is the number of
  frames taken when the count was read, so it may be early but never late.
- **Device time**: `TimeSource.CAMERA` (the enum has no `DEVICE`; the
  camera's own clock is `CAMERA`), at 1e7 Hz. Host time is `time.time_ns()`
  when the frame is taken.
- **Opening** retries `IS_DEVICE_IN_USE` for `open_timeout` (5 s), sets the
  pixel format (RAW8 by default) and the rising-edge trigger.
- **Settings** are methods on the capture, for 5f-4's messages:
  `pixel_format`, `aoi`, `pixel_clock(s)`, `frame_rate`, `exposure` (ms),
  `trigger` (edge and delay in µs), each with a `set_`. The pixel format and
  AOI are refused while memory is attached, because the driver would not
  refuse them. `shortest_trigger_period()` is in seconds. `fire_trigger()`
  is `is_ForceTrigger`.
- Each capture gets a fresh queue (exit and init), so a second capture on
  the same slots does not see the first one's entries.

**5f-3: done on the camera (2026-09-30), run by Claude.**
`tests/hardware/ueye/test_port.py`: K1–K9 in two formats (256 rows of
RAW8; full-frame RAW10), and Z1–Z4. It is gated like the survey, and Z1's
DAQ condition, Z2 and Z3 need `STACSCOPE_UEYE_TRIGGER`. About three
minutes. All green (101 passed, 4 skipped: the lost-frame contract test
and the alignment refusal, in each format).
- **The driver zeroes the image memory it is given.**
  `is_SetAllocatedImageMem` writes zeros over the whole image; nothing else
  writes into a memory but a frame, and frames survive a stop and a detach
  (probe). The contract laid its sentinel down before attach, so every
  slot looked written. **The port now says attaching may overwrite the
  slots**, and a slot is defined once a frame is reported in it. The
  contract and `measure_in_flight` lay down the sentinel after attach.
  This is harmless to the role: its arm configures the segment and starts
  a new acquisition before it attaches, so no consumer is still reading
  the old frames by then. The fake zeroes too.
- **Z1: in-flight depth 1**, triggered from software and from the DAQ (256
  rows of RAW8, 1 ms exposure, 1.5 times the shortest period). The device
  will declare 2, as the Flash does: measured, plus a margin.
- **Z2: the requirement holds through the adapter.** Full-frame RAW10 at 20
  fps from the DAQ: 200 pulses, 200 frames, frame numbers in steps of 1,
  device stamps 50.000 ms apart. The pixel clock was chosen with the
  adapter's own `shortest_trigger_period()`: 40 MHz, 44.911 ms at 10 ms
  exposure.
- **Z3: the shortest period, as the adapter reports it, is right.** At 31
  MHz and 10 ms (54.971 ms), pulses at 1.03 times it gave 40 of 40; at
  0.97 times, `FramesLost` after the first frame. With the missed-trigger
  check taken out, the 0.97 case failed (the mutation check, on the
  camera).
- **Z4:** the adapter opened the camera 1.2 s after its capturing process
  was killed, and captured.

**5f-4: done over the fake and on the camera (2026-09-30).**
`UEyeCamera(ImageStreaming, TimedDevice)` in `adapters/ueye/camera.py`, its
messages in `adapters/ueye/messages.py`. No `Recording` (decision 6).
- **Messages, one per setting** (decision 3): `SetPixelFormat` →
  `PixelFormat`, `SetAOI` → `AOI`, `SetPixelClock` → `PixelClock`,
  `SetFrameRate` → `FrameRate`, `SetExposure` (ms) → `Exposure`,
  `SetTrigger(edge, delay_us)` → `Trigger`, each with what the camera took,
  or `Rejected`. **Added by Claude, and kept (user, 2026-10-01):**
  `GetCameraSettings` → `CameraSettings`, which reads every setting and the
  pixel clocks on offer. Without it a client cannot read a setting without
  changing it, and a new pixel clock moves the exposure.
- **`GetShortestTriggerPeriod` → `ShortestTriggerPeriod(seconds)` is in
  `core/protocol/messages.py`**, as a client → camera message, because
  every triggered camera can answer it (user: the Flash too).
- While armed, `SetPixelFormat` and `SetAOI` are refused (the capture
  refuses them while memory is attached); the others apply at once.
- Config: `pixel_format`, `aoi`, `pixel_clock`, `frame_rate_fps`,
  `exposure_ms`, `trigger_edge`, `trigger_delay_us`, applied at launch in
  the camera's order (AOI, pixel clock, frame rate, exposure, trigger). A
  refusal fails the launch and exits the camera. `in_flight` defaults to 2
  (Z1 measured 1).
- The camera is opened in a worker thread, as DCAM is, so heartbeats are
  answered meanwhile.
- Tests: the role contract F1–F6 over the fake (`test_role_contract.py`;
  its `burst` holds the loop until the capture's thread has taken every
  frame, which is what a stalled publisher looks like with a thread
  taking frames), and E1–E6 (`test_ueye_camera.py`). Every mutation tried
  was caught. On the camera, `tests/hardware/ueye/test_device.py`:
  - **W1:** settings by message, and `SetAOI` refused while armed, with the
    acquisition going on.
  - **W2:** a client chooses the pixel clock by asking the shortest period
    (40 MHz, 44.911 ms), then 20 fps full-frame RAW10 from the DAQ: 200
    pulses, 200 frames published to a reader, `TimeSource.CAMERA`, in-flight
    depth 2.
  - **W3:** pulses at 0.97 of the shortest period end the acquisition with a
    fatal `Fault` ("FramesLost ... after frame 1").

### The buffer recorder *(proposed, decided and done 2026-10-01)*

**Status: done, over the fake and on the uEye (2026-10-01).** The proposal
below is kept as the history. Where it differs from this block, this block
holds: no writer port was built.

**Decided (user, 2026-10-01)**, answering the proposal's questions:
1. **TIFF only, through tifffile, a core dependency.** No raw format.
2. **Per-frame metadata goes in the TIFF**, if TIFF can hold it cleanly. It
   can: private tags on each page. The frame number may be redundant, so
   it is left out.
3. **An overrun is fatal.** This is one of the few cases, maybe the only
   one, where a consumer's overrun is a failure: the writer must handle
   every frame or fault. Setting up the experiment so that this does not
   happen is the user's responsibility. A benchmark measures a disk's
   limit, which depends on the hardware, not just on the writer.
4. **No headroom rule.** Nothing is preallocated, so the file system says
   when the disk is full.
5. **`SetSaveDirectory` is a core feature.**
6. **A thread in the parent's process.** Even GIL threading is acceptable,
   as long as the work is limited by moving data, not by computing.
7. **`TIFFRecorder`.** Another format means copying this class and
   adapting it, not one core class handling many formats. TIFF is the
   field's default for raw linear image sequences.

**Built:**
- **`SetSaveDirectory` / `SaveDirectory`** are in `core/protocol/messages.py`,
  answered by the `Recording` role. The role refuses a change while armed
  or to a directory that does not exist, and refuses a stem while no
  directory is set (`save_directory` defaults to `None`, not the working
  directory). `check_save_directory()` is the launch-time check each device
  calls before opening hardware. The Flash's own copy is gone; its O11
  tests pass through the role. These are D7's five new tests in
  `test_recording.py`.
- **`TIFFRecorder`** (`core/recording/tiff_recorder.py`), an actor:
  - It opens the reader and the file in `setup`, with `open(path, "xb")`,
    so a file is never written over. It refuses a buffer on another
    acquisition.
  - A thread of its own copies each frame, checks `still_valid` after the
    copy, then writes the copy. A torn frame never reaches the file.
  - Each page is a BigTIFF page of its own (`contiguous=False`,
    `metadata=None`), so it can carry tags: 65000 `device_ticks` and 65001
    `host_time_ns` (SLONG8), only for fields the producer fills.
  - Page 0's description is JSON naming the source, buffer, acquisition,
    time source, clock rate and tag map.
  - tifffile and Fiji read the pages as one stack.
  - `FinishRecording(frames)` → `RecordingFinished(frames)`, then it stops.
  - An overrun, a torn frame, a new acquisition, or a write error ends it
    with that error at once. The thread hands the error to the loop, and
    the file is closed first.
  - The thread clears its error's traceback frames: a view of the buffer in
    them stopped the reader from closing (K9 again, found by L8).
- **`BufferRecording(Recording)`** (`core/devices/buffer_recording.py`):
  - The recorder's class is the `recorder` attribute.
  - `open_recording` spawns it as the child `recorder-<acquisition id>`
    and waits for it to start or die.
  - `close_recording` tells it `FinishRecording(published)` and waits for
    its exit, rather than asking, then checks `frames_written` against the
    count.
  - A `ChildExited` with an error during the acquisition is published as
    a fatal `Fault` at once, and reported once only.
  - `close` stops a recorder still running.
  - Defining a device with the role but without `ImageStreaming` is a
    `TypeError`.
  - `SimulatedCamera` and `UEyeCamera` have the role, each with a
    `save_directory` option.
- **Tests:**
  - L1–L10, the recorder: `tests/unit/recording/`, on the real clock.
  - L11–L17, the role: `tests/unit/devices/test_buffer_recording.py`.
  - L13 and L14 end to end, through a Coordinator on the real clock:
    `tests/integration/test_recording_rig.py` (built after the user's
    confirmations, 2026-10-01).
  - E7 on the uEye fake.
  - W4 on the camera.
- **Mutation checks.** 21 mutants: the save directory 4, the recorder 9,
  the role 7, the uEye 1. Three survived:
  - The rewording of an overrun as a new acquisition was untested. It
    became a test: a new acquisition seen by a recorder that is behind.
  - `close` stopping a recorder was untested. It became a test: the camera
    cancelled mid-stop. That test found a real bug: `close_recording` let
    go of the recorder before it had stopped, so `close` could not stop it.
  - Skipping `file.close()` is equivalent: dropping the last reference
    closes the file in CPython.

  A recorder that loses count without an error has a test of its own,
  written before its mutant ran.
- **W4, on the camera:** full-frame RAW10, 20 fps from line 2, 10 s.
  - 200 pulses gave 200 frames published and 200 pages (524 MB) on E:.
  - The stop was answered in 0.01 s.
  - Page-to-page camera time: median 50.000 ms (49.957–50.035).
  - The file's last frames equal the buffer's, pixel for pixel and tick
    for tick. W1–W3 still pass.
- **`benchmarks/tiff_write.py DIRECTORY [--shape R C] [--seconds S]
  [--rate FPS]`**:
  - It records synthetic frames through the real recorder, never lapping
    it.
  - It reports the sustained rate, the longest hold-up, and the slots
    needed at `--rate`.
  - On the dev box's E:, uEye full frames for 30 s: 489 frames/s (1.28
    GB/s), longest hold-up 101 ms, so 6 slots at 20 fps.

**Findings, and the choices the user confirmed (2026-10-01):**
- **D12 runs unsaved now** (confirmed). Its Coordinator times the stop in
  virtual time, which expires before a real-thread recorder finishes
  ("stop not confirmed: camera"). It is an artifact of the test, with a
  real counterpart: **on the instrument, confirming a stop includes the
  recorder writing its backlog**. That is at most a ring's worth, at disk
  speed, so a recording camera's `DeviceSpec.stop_timeout` must allow for
  it.
- **An acquisition with no frames leaves a 16-byte TIFF with no pages**
  (confirmed). tifffile opens it as empty; a strict reader may refuse it.
  It is kept as the record that the acquisition ran (L5).
- **A camera closing while its recorder finishes waits for it**
  (confirmed). The recorder handles `FinishRecording` before it sees a
  stop, so every frame published still reaches the file (L16). A disk that
  hangs would hang the close.
- **The end-to-end tests are built** (2026-10-01),
  `tests/integration/test_recording_rig.py`. They use D12's rig, recording,
  with every actor on the real clock:
  - L13 end to end: 5 cycles, with a disk slower than the camera (80 ms a
    frame against 50 ms) so that it is behind when the triggers end. The
    file holds every frame triggered, and nothing faults.
  - L14 end to end: a disk that fills after two frames, during an
    acquisition of 100 cycles (10 s). The Coordinator aborts at once, the
    source stops early, and the file keeps its two frames. The abort
    takes about a second, because the queued output drains first.
  - The simulated output queues a second of samples. With 0.2 s queued,
    CI's Windows 3.14 runner failed L14: its event loop was held up long
    enough for the output to run dry, so the source was lost and the
    Coordinator went FAULTED. A 250 ms stall reproduces that at 0.2 s,
    and not at 1 s. On the real clock the output's buffer depth is the
    stall the rig survives, as it is on the board.
  - The camera's `stop_timeout` is set apart from its `ready_timeout`, as
    the instrument's must be.
  - Mutation checks, four mutants, all caught:
    - the Coordinator ignoring a fatal `Fault`;
    - the role not publishing a recorder's failure;
    - a stop timeout too short for the backlog;
    - `close_recording` not waiting for the recorder. This one first
      survived: with no backlog at the stop there was nothing to wait for,
      so L13 gained the slow disk.

**The user's brief (2026-10-01):** a lightweight recording actor that writes
TIFF or raw data to disk. The streaming actor launches it as a child, and
its life may simply match the acquisition's. It is for the uEye first.

**The thesis asks for exactly this** (§3.4.3, "Camera actors"): a camera
whose driver cannot write files "should launch a dedicated virtual device
actor which will watch for incoming images in the camera buffer and write
them to file", which "keeps buffered file writing separate from the thread
or process monitoring acquisition". §3.3.2 makes overruns the consumer's to
detect: "If this should stop acquisition ... that must be handled through
actor messages". §3.3.3 makes each camera's files intermediate, to be
consolidated later. Consolidation is not part of this work.

**What already fits, from the code:**
- `Recording`'s hooks are where the child is born and finished.
  `open_recording(path)` runs after the device's camera arms and before
  capture starts. `close_recording()` runs after capture stops and
  `ImageStreaming` has published the final frames. So the recorder is
  spawned before frame 0 exists, and it is told the final count once the
  count is final. The order check counts a subclass of `Recording` as
  `Recording`.
- `Recording.disarm` already turns an error from `close_recording` into a
  fatal `Fault` ("recording may be truncated"). `Recording.arm` already
  undoes the arm when `open_recording` raises.
- `Recording.target` names the file `<save_directory>/<stem>_<name><suffix>`,
  as the Flash's is named.
- The reader (`ImageBuffer.open`, `Cursor.next`, `Overrun`, `still_valid`)
  is everything a recorder reads with.

**Findings that shape it:**
- **A parent's exit does not stop its children.** `ChildWatch.close`
  stops watching, and the child runs on. A recorder whose camera died would
  keep its file open and its reader mapped. The role must stop its recorder
  in `close`, which runs on every exit, clean or not. Whether the runtime
  should stop a dead parent's children in general is a separate question.
- **`TimedDevice` ignores `ChildExited`** (unknown messages are ignored). A
  recorder that dies mid-acquisition would go unheard, so the role handles
  it.
- **Stall tolerance is the ring.** A recorder can fall behind by `n_slots −
  in_flight` frames before it is lapped: 18 frames, or 0.9 s at 20 fps,
  with W2's 20 slots. The ring is the queue, so a slow disk calls for more
  slots, not for a second queue in the recorder. RAW10 at full frame is 2.6
  MB a frame, so 100 slots is 262 MB and 4.9 s.
- **A frame copied from the ring is trusted only if `still_valid` holds
  after the copy.** Otherwise the producer may have overwritten it
  mid-write, and that is an overrun too.
- **`FrameWriter` is taken** (the buffer's producer side), so the file
  writer needs another name.
- **tifffile is not installed.** It is pure Python (BSD-3) over numpy.
  Whether it runs clean on 3.14t is to be checked on install.

**Proposed shape:**
- **`BufferRecorder`, an actor in core**, consuming one acquisition of one
  buffer. It is given the buffer's name, the acquisition's id and first
  frame, the path, and a file writer. Its `setup` opens the reader and the
  file: a refusal fails its launch, and so the arm. The writing runs on a
  thread of its own, because writes block and the actor's loop is the
  device's. On 3.14t that thread runs in parallel with capture; on 3.14,
  disk writes release the GIL. The thread reads every frame in order and
  writes it with its metadata record. `FinishRecording(frames)` is the
  device's word that the acquisition ended at that count. The recorder
  writes up to it, closes the file, answers `RecordingFinished(frames)`, and
  stops. An overrun, a torn frame, a changed acquisition id, or a writer
  error ends it with that error, after closing the file so the frames
  written stay readable.
- **`BufferRecording(Recording)`, a role**, for an `ImageStreaming` device
  with no recorder of its own: `UEyeCamera(ImageStreaming, BufferRecording,
  TimedDevice)`, and `SimulatedCamera` for the tests.
  - `open_recording` spawns the recorder as a child and waits for it to
    start or die.
  - `close_recording` asks `FinishRecording(published)` and raises if the
    file is short.
  - `ChildExited` with an error during an acquisition publishes a fatal
    `Fault`, so the Coordinator aborts.
  - `close` stops a recorder still running.
  - **One recorder per acquisition** (the user's suggestion). It has no
    state machine, a failure cannot carry into the next acquisition, and an
    in-process spawn costs nothing measurable. It matches the thesis's
    Record toggle, which is a `STOP` then a `START` with a stem.
- **A file-writer port**, so formats are adapters with a contract suite and
  a simulated (in-memory) writer for the tests. Each writer opens a path
  (refusing one that exists), writes frames and their records, and closes,
  returning the count.
  - *TIFF*: tifffile. BigTIFF, one page per frame, uncompressed and
    contiguous, so it writes at close to raw speed.
  - *Raw*: either `.npy`, whose header gives shape and dtype and is
    rewritten with the count at close, or a `.raw` beside a `.json`.
  - Either way, frames are written packed, without row padding, and the
    per-frame records (`sequence`, `hw_frame`, `device_ticks`,
    `host_time_ns`) go beside the frames with the header's time source and
    clock rate.
- **Where it runs: in the device's process**, the actor on the device's
  loop and the writes on its own thread. A process of its own would cost
  an interpreter start on every arm, and the transport has no way for a
  device to place a child yet. The reader works across processes, so
  moving it later changes nothing in the buffer.

**Tests, L (proposed), red/green:**
- *The writer contract* (each format, and the simulated writer):
  - L1: an existing path is refused.
  - L2: frames read back equal and in order, packed from padded strides.
  - L3: the records read back.
  - L4: close returns the count, and a file with no frames is still
    readable.
- *The recorder* (over a `SimulatedFrameCapture` buffer and the simulated
  writer):
  - L5: it writes every frame from the acquisition's first to the count
    it is told.
  - L6: Finish waits for frames published but not yet written.
  - L7: a lapped reader is an overrun: the recorder exits with an error,
    and what it wrote is readable.
  - L8: a frame overwritten during its copy is an overrun, not a frame.
  - L9: a writer error, or a changed acquisition id, ends it with that
    error.
  - L10: it closes its reader holding no views (K9).
- *The role* (on `SimulatedCamera`):
  - L11: no stem, no recorder.
  - L12: a recorder that cannot start refuses the arm and undoes it.
  - L13: at the stop, the file holds exactly the frames published.
  - L14: a recorder that dies mid-acquisition is a fatal `Fault`, and the
    Coordinator aborts (end to end).
  - L15: two acquisitions in a row give two files, from two recorders,
    under the same child name.
  - L16: a device stopped while recording stops its recorder, and the file
    is closed.
- *On the uEye* (W4): W2's 20 fps full-frame RAW10 from the DAQ, recorded
  to `E:/stacscope_tests`. Read back, the file holds every frame published,
  its `hw_frame`s are contiguous, and its last frames equal the buffer's.

**Questions for the user:**
1. Formats: TIFF and raw both at once, or one first? For raw, `.npy`, or
   `.raw` with a `.json`?
2. tifffile: a core dependency, or an optional extra like `nidaqmx`?
3. Per-frame records: in a file beside the frames, or inside the TIFF
   (per-page tags)?
4. An overrun: fatal, and the recorder stops writing (proposed, as for a
   lost frame)? Or does it keep writing and log the gap?
5. Disk space: the Flash's rule (refuse the arm below the larger of 100
   GiB and 10% of the disk, plus one frame), and a fatal `Fault` when a
   recording reaches that headroom?
6. `SetSaveDirectory` / `SaveDirectory` are the Flash's own messages. Move
   them to core, so every recording camera answers the same pair, as
   `GetShortestTriggerPeriod` was?
7. In the device's process on a thread (proposed), or a process of its
   own?
8. Names: `BufferRecorder` (the actor), `BufferRecording` (the role), and
   a name for the writer port.

### The rig config *(decided and built 2026-10-01; G13 green on the instrument)*

**Status: done (2026-10-01). Green in simulation and on `Dev1-sim`
(G1–G12, G14–G16), and G13, the instrument's run on both cameras, passes on
the hardware.** The composition root that "Open items → Deployment" said was
missing: `app/config.py` reads a TOML rig config, and `app/rig.py`'s
`Rig.start` turns it into a running Coordinator; `Rig.close` takes it down.

**The target (user, 2026-10-01).** With no session model yet, the rig is
tested against a run with digital timing signals only, AO held at 0 V: a
MAIN at 20 cycles a second, one uEye frame per cycle (full frame, RAW10),
and 31 Flash frames per cycle at 256 rows, packed at the start of the cycle
at the shortest trigger period that works. Both cameras record, and the
Coordinator runs the whole cycle. The final version also opens a buffer
monitor on each stream.

**Decided (user, 2026-10-01):**
- **Each device has its own save directory**; the file has no rig-wide one
  (refused if given). The Flash must record to E:, and the uEye may have to
  record to C: if one disk cannot take both streams; a device split onto
  another machine later has paths that mean nothing here. A directory is
  checked by its device at launch, on its own machine, never by the config.
- **Two devices may share a save directory**, as long as their files'
  names differ, and may even prefer to (user, 2026-10-01). They do: the
  `Recording` role names a file `<stem>_<device name>.<ext>`, and device
  names are unique. G16 pins it; with the device's name dropped from the
  file name, the second camera refuses its arm.
- **The Flash fits 31 frames in 50 ms by synchronous readout** (`TRIGGER
  ACTIVE` `SYNCREADOUT`, AREA, 256 rows). The 5d survey's minimum trigger
  interval at 256 rows, edge trigger, 1 ms exposure was 2.348 ms: 31 frames
  would take 72.8 ms. Synchronous readout puts the floor near the readout,
  ~1.25 ms (128 lines × 9.74 µs). Measured by G13: 1.296 ms.
- **COOLDOWN plays one more Flash pulse** (user, 2026-10-01). In
  synchronous readout each pulse ends one exposure and starts the next, so
  the run's first pulse ends nothing and its last opens an exposure only a
  further pulse reads out: N pulses give N − 1 frames (G13, measured). The
  closing pulse sits at COOLDOWN's start, where the next cycle's first would
  have been. A consequence for imaging: each cycle's first frame is exposed
  from the previous cycle's last pulse (~11 ms here), the other 30 for one
  period (1.3 ms).
- **The Flash's ring is a whole number of volumes**: `n_slots` a multiple
  of 31, as a volume readout will want (user, 2026-10-01). The example
  config has 403 (13 volumes); G13 refuses a config that is not.
- **"Minimum successful trigger timing" is the camera's own floor**
  (`GetShortestTriggerPeriod`), rounded up to a whole DAQ sample (10 µs).
  A lost frame is a fatal Fault, so a floor that is wrong ends the run.
- **The hardware run saves its own NI MAX tasks** (`stacscope_test_rig_*`)
  and overrides the config's task names, as the device suites do: the
  "never load `SPIM_*` from a test" rule stands.
- **Rig config logic stays inside `app/`** (user, 2026-10-01), because it
  may be refactored when a recording session config interface is made.
  Nothing outside `app/` imports `app.config` or `app.rig`, reads the file,
  or takes a `RigConfig`, `DeviceConfig` or `Construct`. Core and the
  adapters keep taking plain constructor arguments and `DeviceSpec`s; `app/`
  is where the file becomes those. Today nothing outside `app/` does.
- **The rig config is local to each machine**: `config/rig.toml` is
  gitignored; `config/rig.example.toml` is the tracked template (G12 keeps
  it loading).

**The file.** `[source]` is `nidaqmx` (two MAX task names, optional
`buffer_samples`; channels and rate are MAX's, read at `Rig.start`) or
`simulated` (rate, buffer depth, and `ao` / `do` tables of label = channel,
in row order). Each `[devices.<name>]` holds the rig's keys (`kind`,
`placement`, `trigger`, `ready_timeout`, `stop_timeout`, `heartbeat`,
`required`), and everything else is the device's constructor arguments,
arrays as tuples. Checked at load, naming table and key: unknown keys,
kinds and placements; options bound against the device class's signature,
so a misspelt setting fails there (the adapter modules import without their
vendor packages, so this needs none). `kind` is a short name (`orca_flash`,
`ueye`, `simulated_camera`, `simulated_free_running_camera`) or
`package.module:Class`, so a new adapter needs no registry change.
- **`trigger`** names the digital line, by label, that triggers a device.
  It is rig data: checked against a simulated source's lines at load and
  against MAX's at `Rig.start` (G15), used by a run to find a waveform's
  row (`rig.line(rig.triggers[name])`), and given to a device on the
  simulated trigger bus as its line. Such a device (one whose class takes
  `bus`) must run in-process with a simulated source.
- **Factories are `Construct(target, options)`**: a class name and plain
  data, so a process child is sent nothing live (G6, G11).

**What `Rig` does that a hand-built rig did not:**
- `start` waits for every in-process device's `setup`, which the
  Coordinator does not (it spawns them and moves on; a process launch
  already waits). A device that fails raises `LaunchFailed` naming it, and
  what had started is shut down (G9).
- `close` stops a running acquisition through the Coordinator first, so it
  ends through COOLDOWN (G8), then **stops the Coordinator before the
  devices**. Found by G8: closed the other way, the Coordinator published
  "participant … exited" and "timing source lost" Faults for a deliberate
  shutdown. Then the process host, then in-process devices and the source,
  since a parent's exit does not stop its children.

**G13, on the instrument** (`tests/hardware/rig/`, gated on
`STACSCOPE_RIG_CONFIG`, plus `STACSCOPE_CAMERA_TRIGGER` and
`STACSCOPE_UEYE_TRIGGER`; `--extra nidaqmx --extra ueye`; its docstring has
the commands). Run 2026-10-01 by Claude, with the user's permission, both
cameras saving to `E:/stacscope_tests`:
- Shortest trigger periods: Flash 1.296 ms at 256 rows in synchronous
  readout (played every 1.30 ms, so 31 frames take 40.3 ms of 50), with the
  camera's exposure set to 10 ms, which synchronous readout ignores; uEye
  44.9 ms at 10 ms exposure and 40 MHz, 5 ms inside the cycle.
- `SYNCREADOUT` and `POSITIVE` are the Flash's own labels.
- 5 s (100 cycles) and 20 s (400 cycles, both monitors open): every frame,
  3100 and 12400 Flash frames, 100 and 400 uEye pages, no Fault. Launch
  ~5 s, start 0.2 s, close 0.3 s: both cameras ran as process children, and
  their teardown is far inside `STOP_GRACE`. E: took both streams together
  (~700 MB/s) for 20 s.
- Before the closing pulse, the Flash's file was one frame short per run
  (3099, 12399), which is how the N − 1 above was found.
- One run, the first with `STACSCOPE_MONITOR=1`, failed after 208 s, never
  reaching idle within the deadline; its output was lost. The monitors'
  environment was being updated by both monitors at once in that run, and
  neither window appeared. Not reproduced in two later runs with monitors.
- A monitor left open holds its buffer's name, so the next `Rig.start`
  fails with `FileExistsError` naming it (no hang). Close monitors between
  runs.
- The Flash was found at its defaults after the first three runs
  (INTERNAL, EDGE, NEGATIVE, no subarray): the device's closed DCAM session
  left nothing to restore.

## Subsequent milestones

**Status, 2026-10-01: Milestones 1–5 are done, and so are the buffer
recorder, the buffer monitor and the rig config (G13 on the instrument).
Hardware testing is paused**, and development moves to a machine without
the instrument, to free it. `notes/instrument-dev-box.md` records that
machine and how to run each hardware suite when testing resumes.
- Next on this list: 6, the gateway, then 7, integration, which the user
  runs.
- Also open, with no milestone yet (see "Open items" and "Session model"):
  - the session model, which the user expects as a recording session
    config interface; the rig config stays inside `app/` until then;
  - logging and error reporting;
  - checking a waveform against the cameras' timing floors;
  - the communication model;
  - ETL waveforms.
- The user has not ordered these. The gateway carries the session's
  messages, so whether the session model comes first is worth deciding
  before either starts.

**2026-10-02: a core review comes first** (user). Its findings are in
"Open items → The core review". Bugs A–D are fixed (CR1–CR4, 2026-10-02);
next, the communication-model observations are discussed, before any item
above starts.

2. **Data flow** — generic ring buffer, producer/consumer, contract tests, synthetic producer. The contract suite runs on both CI legs, so POSIX unlink/persistence semantics and `track=False` are exercised rather than assumed.
3. **Device actors against simulation** — multi-device registry, per-device READY/timeout, shared root filename, software-timed participation. *(Done 2026-09-19; see "Milestone 3" above.)*
4. **Process transport** — run a simulated device out-of-process and prove fault isolation before the real driver arrives: kill the process, watch the heartbeat fire `ChildExited`, confirm the registry recovers and the engine survives. Doing this in simulation first means the DCAM adapter lands into a boundary already known to work. *(Done 2026-09-19; see "Milestone 4" above.)*
5. **Real adapters** — `nidaqmx` against NI MAX simulated devices first, then DCAM.
   *(Done 2026-10-01, in stages 5a–5f; see "Milestone 5" above.)*
   **Our own thin ctypes layer over the official DCAM SDK**, not `pyDCAM` (0.0.1,
   no `requires_python`, no classifiers — too thin for the highest-risk adapter).
   We wrap `dcamapi.dll` for init/open, ROI and exposure, `dcambuf_attach` for the
   zero-copy path, capture start/stop, and `dcamrec_*` for the DCIMG writer. Note
   the legacy `tm_*_40` names are LabVIEW subVIs from Hamamatsu's driver library,
   not DLL exports, so there is no custom wrapper to reverse-engineer.
   **Also planned (user, 2026-09-19): a STACScope producer for the uEye.** The
   user will decide later whether to use it instead of µManager control. It is
   secondary: sCMOS cameras like the Flash are the primary target, and the
   uEye's specifics (model, USB generation, metadata mapping) are deferred.
6. **ZeroMQ gateway** — the external surface, with the CLI as its first client, and Pycro-Manager hooks as the second.
7. **Integration on the instrument**, user-run.

Cross-machine distribution (timing and imaging on one machine, independent
tracking on another, coordinated by network control plus hardware sync signals)
is an additive `ZmqTransport` deployment after this, requiring no core changes.

**Constraint (user, 2026-09-18): the Coordinator lives on the same machine as
the timing source.** Other participants may be remote.

**Idea, not a requirement — the proxy actor** *(logged 2026-09-18 in case
remote devices run into complications)*: a remote device could be represented
by a local proxy actor, a child of the Coordinator, that relays the device
protocol over ZeroMQ, so the Coordinator treats it like any local participant.
Notes if it is ever taken up: the proxy must own liveness of the far side
(heartbeat over the link, then exit so `ChildExited` fires locally); the remote
side needs its own watchdog so it shuts down safely if the link drops; only
control crosses (timing by cable, frames stay in the remote machine's shared
memory). It coexists with `ZmqTransport`: a transport suits a remote end that
is itself STACScope, a proxy suits one needing translation, its own reconnect
policy, or state the Coordinator shouldn't hold.

## Open items

- **The core review (2026-10-02).** The user asked for a deep review of the
  core and the communication model before anything new is added. Claude read
  the runtime, protocol, Coordinator, timing source, device roles, transport
  and rig in full, and probed each suspected bug on the real clock with
  simulated parts.
  - *Bugs, each confirmed by a probe or by reading; the user chose to fix
    them first (CR1–CR4, below):*
    - **A.** A process child stopped its device 30 s after launch, cleanly
      (`ChildExited(error=None)`, measured at 30.1 s). `child.serve` waited for
      "link closed or device stopped" through `_either`, whose 30 s bound
      (`CONNECT_TIMEOUT`) is meant for the bootstrap. G13 never saw it: each
      run ended inside 30 s of launch. It may explain G13's unreproduced
      208 s failure (unconfirmed). Found beside it: a device setup slower
      than the host's `connect_timeout` (15 s) raised a bare `TimeoutError`
      from `ProcessHost.launch`, not `LaunchFailed`, so the rig reported
      "TimeoutError()"; and the child's own bound on setup, had it run out
      first, returned `None` where a reason was due.
    - **B.** A `Relaunch` whose factory raised, or whose process launch raised
      `LaunchFailed`, killed the Coordinator: the asker got no reply, and the
      source and devices ran on with no supervisor, since a parent's exit
      does not stop its children.
    - **C.** The Coordinator turned a device's refusal into "not ready within
      timeout": `_ready` reduced every reply to a bool. A `NotReady` refusal
      publishes no Fault, so its reason ("no MAIN waveform has been set", "no
      save directory is set") reached nobody.
    - **D.** A `SoftwareTimed` routine that raised failed in silence: the task's
      exception was collected only at disarm, by
      `gather(return_exceptions=True)`, and dropped.
  - *Fixed (2026-10-02), each test red first; 632 green on 3.14t and 3.14:*
    - **CR1** (`tests/integration/test_process_launch.py`): a child serves
      its device until the link closes, however long (run in-process
      against the host's half of the handshake, with the bound at 0.2 s);
      and a setup that outlasts the host's wait is `LaunchFailed` naming the
      device. The wait after "ready" is now unbounded: the OS closes the
      link when the engine's process ends, so it cannot strand an orphan.
      `child.serve` takes `bootstrap_timeout` for the test. The 40 s probe
      that found A now sees the child still running. `SimulatedDevice`
      gained `setup_time`.
    - **CR2** (`tests/unit/timing/test_coordinator.py`): a relaunch whose
      launch raises is answered `Rejected` with why; the Coordinator lives,
      the device stays lost and may be relaunched again; a failed source
      relaunch stays Faulted. `Factory` in `timing_support.py` now counts
      launches, so a maker that raises uses its turn.
    - **CR3** (same file): a failed start names each device not ready with
      its own reason, `not ready: camera (no save directory is set);
      laggard (no reply within 2.0 s)`, in the reply and the Fault.
    - **CR4** (`tests/unit/devices/test_software_timed.py`): a routine that
      raises is published as a fatal Fault when it does (Claude's choice of
      fatal: the device can no longer do its part); one cancelled by a stop
      or one that finishes is not. Mutation-checked.
  - *Communication-model observations, for discussion after the fixes (user,
    2026-10-02):*
    1. The Coordinator's mailbox is blocked while it starts or stops devices
       (`_start` / `_stop_everyone` await asks inside `handle`), so
       `ChildExited`, `Fault` and `SourcePhase` queue behind them. On an abort,
       a software-timed device gets `StopDevice` before the relayed COOLDOWN
       or IDLE. A device that dies during start costs its whole
       `ready_timeout`: an ask to a dead actor has no fast failure.
    2. A phase is published when it is committed, not when it plays: RUN is
       published once WARMUP is written, before output starts. The lead is
       WARMUP plus the buffer depth, plus mailbox latency. `SoftwareTimed`'s
       docstring ("a driven variable at RUN, with the first image")
       overstates it.
    3. Two ways to reach an actor: `BufferRecording` holds the recorder
       object (`started`, `stopped`, `frames_written`; `RecordingFinished` is
       never read), and `Rig` reads `coordinator.state` and `participants`
       directly. Fine in one process; the gateway cannot do it.
    4. Nothing a remote client needs yet: no message queries the
       Coordinator's state or registry, and no state transitions are
       published (only `DeviceLaunched`). Published messages (`Fault`,
       `SourcePhase`, `SourceFinished`) carry no acquisition identity; with
       today's ordering no misattribution was found.
    5. One subscriber list per actor: clients and devices share the
       Coordinator's, so every device, process children included, receives
       every Fault and `DeviceLaunched`. Dead subscribers are never pruned.
    6. The timing source shares the engine's event loop. Anything that holds
       it eats the ~37 ms refill margin (one run saw a 57 ms hold). Today the
       cameras are process children, so only the Coordinator shares it.
    7. Already open (below): process-placed devices reach only the
       Coordinator, which is why phases are relayed.
  - *Smaller:* the codec trusts a frame's fields on decode (a missing field
    raises `KeyError`, which closes the link, against `Connection.serve`'s
    intent of skipping one bad frame; field types are not checked), which
    matters once the gateway takes frames from outside. `tell` to a remote
    ref can raise `CodecError` in the sender; to a local one it cannot.
  - *Sound:* the runtime is small and consistent (exactly one `ChildExited`,
    heartbeats answered on receipt, guaranteed teardown); arm and disarm
    failure semantics are careful; role order is enforced; placement is
    transparent to the Coordinator; the buffer path never touches control.
    None of the problems is structural.
  - *Principles, decided while going through the actor model (user,
    2026-10-02), before the observations:*
    - **Long work does not block the handler.** The Coordinator's start and
      stop handshakes become states the Coordinator passes through, driven
      by the replies arriving in its mailbox, not a helper loop or task.
      A handler never awaits another actor. A wait on the actor's own
      hardware is unavoidable; how it is handled depends on what should
      happen if another message arrives meanwhile, decided per action.
      Waits have been used as backpressure, which made a stop wait behind
      them; one idea (user) is a wait during which only messages of higher
      priority than the current one are taken. Decided: see the
      priority-gated wait below.
    - **There are no special kinds of message.** A reply is an ordinary
      message told to the asker. `ask` is shorthand for a pattern: send,
      expect a reply from the recipient, act on the reply or on a timeout.
      Publish/subscribe is likewise a pattern, not a message type.
    - **A ref is the only handle on an actor.** Exceptions are kept to a
      minimum and reserved for the roots and maintenance processes that are
      not actors (the rig, a child process's entry point, tests). Any other
      exception is proposed and discussed on its own (user).
    - **An actor reaches another only by messaging**, at the addresses it
      knows: its parent, its children, the sender of the message it is
      handling, and addresses carried in messages.
    - **Subscriber lists are the actor's own business.** Several are fine
      when tied to a role or a temporary state (waiting for replies);
      whatever keeps the logic simplest. They stay inside the actor and need
      not be uniform.
    - **A parent's exit stops its children.** This reverses the rule that
      children outlive their parent. Children stop first, in reverse launch
      order, then the parent's own `teardown` runs. A child already finishing
      (a recorder told `FinishRecording`) is waited for, not cut short. A
      child stopped this way is not reported as a lost device.
    - **Deadlines are scheduled self-messages.** A timer set when the
      request goes out delivers a deadline message to the actor's own
      mailbox, and is ignored if the reply came first. It is an event-loop
      timer: no thread, no loop. The `Clock` port gains `call_later` so
      virtual time still drives it. Favour whatever is most robust in each
      case; internal threads are not ruled out.
    - **Hardware waits use a priority-gated wait** (Claude's recommendation,
      accepted by the user, 2026-10-02): at a marked wait, the actor takes
      only messages of higher priority than the one it is handling. Waits on
      other actors are states instead. Two needs settled it: a stop must get
      through the source's refill wait, and so must the timer that announces
      a phase as it starts playing.
    - **Every envelope carries its sender.** A reply is an ordinary tell
      back to the sender; `ask` remains a convenience for roots. The wire's
      reply field becomes the sender.
    - **An abort is not an emergency.** `StopDevice` to a running source
      stays Finish-like: the cycle completes, then COOLDOWN.
    - **An emergency halt is added.** Sent only from the root, normally by
      the user in response to an unsafe hardware state. Every actor moves to
      a safe state and passes the halt to its children. The DAQ source
      interrupts and writes the last sample of COOLDOWN. Its shape is to be
      worked out; the user is still thinking it over (2026-10-02). Claude's
      proposal, kept for then and not decided:
      - *Name and priority.* `Halt`, at a new priority level above URGENT,
        so an actor in a gated wait while handling an URGENT message still
        takes it.
      - *Root only.* An actor acts on `Halt` only when the envelope's
        sender is its parent, so a halt can only start at the root and
        travel down the tree.
      - *Spread.* Pass it to every child first, then go to its own safe
        state, without waiting for the children. Process children get it
        over their link like any other message.
      - *Safe state.* A `halt()` hook on each actor, defaulting to the
        device's `disarm`. An adapter overrides it where safe means more (a
        shutter closes, a pressure box vents); the source's override
        interrupts output and writes the last COOLDOWN sample.
      - *Confirmation.* Each actor answers its parent `Halted(name, safe,
        reason)`, and each parent passes a summary up, so the user learns
        which hardware is confirmed safe. The alternative is
        fire-and-forget. Open.
      - *Afterwards.* The actor stays halted and refuses work until the root
        clears it; the Coordinator gains a HALTED state that refuses
        `StartAcquisition`. The alternative is ending the halt in a restart.
        Open.
      - *Limits that cannot be designed away.* An actor inside a blocking
        driver call cannot act until the call returns. Killing a stuck
        process child does not make its hardware safe, so a device that
        does not answer `Halted` is reported as unconfirmed, not killed
        automatically. A sample-clocked NI task cannot do one on-demand
        write: it must be stopped and retimed, or a separate static write
        used. The `SignalOutput` port would gain `halt(sample)`, under the
        contract suite, checked on `Dev1-sim` and run by the user on `Dev1`.
      - *Open question.* What a halt writes while idle, or before any
        COOLDOWN is set: a safe sample named in the rig config, or the last
        COOLDOWN played.
- *Principles, decided while going through the Coordinator/TimingSource
  split (user, 2026-10-02):*
  - **The split stands.** The Coordinator knows who takes part and
    whether they are ready; the source knows when.
  - **FAULTED is folded into the rule for a lost required device.** The
    source counts as a device that is always required: while it is lost,
    every `StartAcquisition` is refused, naming it, until it is relaunched.
    No state is latched.
  - **Supervision stays in the Coordinator.** It is a consequence of being
    the top-level actor, and it is what makes coordinating an acquisition
    simple.
  - **`SourcePhase(RUN)` must be synced with the start of the first MAIN
    cycle**, for software-timed devices. Today it is not: the base enters
    RUN as soon as `warmup()` has committed WARMUP, which for the DAQ source
    is usually before the sample clock has started. RUN is announced early
    by the whole of WARMUP plus the start-up, or, when WARMUP is longer than
    the hardware buffer, by the buffer and the FIFO. COOLDOWN is early by up
    to the same.
  - **Every `SourcePhase` means "now playing"** (user): WARMUP when the
    clock starts, RUN at the first MAIN sample, COOLDOWN at its first
    sample, IDLE once output has drained. The source's own phase still
    changes when samples are queued, which keeps the MAIN → COOLDOWN handoff
    gapless; only the announcement moves. The mechanism (Claude's proposal,
    accepted):
    - The source knows the sample index where each phase starts, from its
      count of samples queued.
    - The `SignalOutput` port gains `samples_played()`: on NI,
      `total_samp_per_chan_generated`, which the adapter already reads
      (0.03 ms); in simulation, from the clock. Covered by the contract
      suite.
    - The source schedules a self-message for when the phase should start
      playing; when it arrives, it checks `samples_played()` and announces,
      or re-arms. It reaches the source through the priority-gated wait.
    - A source with no buffer announces at once: the base asks the subclass
      where a phase starts, and the default is now. That keeps a camera
      source possible.
    - The precision is the control flow's: about a millisecond in-process,
      a few across processes. On Windows, event-loop timers may be no finer
      than the system timer (often 15.6 ms); measure on `Dev1-sim` before
      relying on it.
  - **An acquisition stopped before the first MAIN plays never announces
    RUN** (user). A device waiting for RUN never starts its routine, and is
    stopped with everything else.
- *Principles, decided while going through the three information flows
  (user, 2026-10-02):* where the flows meet is a message or a stamp that
  converts between them.
  - **One host clock.** Frames are stamped on the monotonic clock the
    actors use (`time.monotonic_ns`, the performance counter on Windows
    from 3.13, about 1 µs), not the wall clock (`time.time_ns`), which can
    jump. Each file gets one wall-clock anchor for the date and time.
    Affects the metadata's `host_time_ns`, TIFF tag 65001, and the uEye and
    simulated captures. Today the two clocks cannot be compared, so a
    software-timed device's own record cannot be reconciled with frames.
  - **Mapping a frame to its cycle or waveform is the client's job.** The
    engine keeps the facts (`MainApplied`'s tag and sample, each frame's
    sequence, lost frames fatal so counting holds); the client that
    designed the waveform knows the pulses per MAIN on each camera's line
    and the camera's own rule (N − 1 in synchronous readout). Building it
    in would mix waveform and camera logic, as sizing a recording from
    pulses did.
  - **`MainApplied` stays at queue time**, the one exception to "now
    playing": it says where the MAIN plays (`sample`), not that it is
    playing, and a closed loop needs it early to queue the next. Its
    docstring is to say so.
  - The principles walk is done. Next: the communication-model
    observations 1–7, below.
  - The plan's "Coordinator and timing source" section is out of date (it
    stops only the devices that were ready, and calls FAULTED terminal); it
    is to be rewritten when these are built.
- *Decided on the communication-model observations 1–7 (user,
  2026-10-02), Claude's recommendations accepted:*
  - **1. A child lost mid-handshake.** In STARTING, a lost required device
    (or the source) fails the start at once with its reason, rather than
    costing its `ready_timeout`; a lost optional one is dropped from the
    wait and left out. In STOPPING, a lost device counts as done and is
    reported as lost, not "stop not confirmed".
  - **2.** Settled by "now playing"; `SoftwareTimed`'s docstring is
    corrected when that is built.
  - **3. The recorder stays a child actor** (option A; user). Recording is
    a kind of buffer consumer, and another format or consumer should be
    another actor. B, a worker thread owned by the role, was smaller but
    hand-rolls the supervision the runtime gives, and holds the camera's
    handler through a long flush.
    - Arm: "open file" spawns the recorder and returns; the device is
      ARMING and keeps the asker. `RecorderStarted` from the recorder →
      ARMED, answer `DeviceReady`. The recorder's `ChildExited` while
      ARMING → undo the arm, answer `Rejected` with its reason.
      `StopDevice` while ARMING stops the recorder and disarms; the
      Coordinator's `ready_timeout` is the only deadline. Capture starting
      before the file is confirmed is harmless: no trigger comes before
      `Go`, and the recorder reads from the acquisition's start in the
      ring.
    - Disarm: `FinishRecording(published)`, then DISARMING;
      `RecordingFinished(frames)` → check `frames` against `published`,
      answer `DeviceStopped`. A `ChildExited` with an error first fails
      the stop as a failed `close_recording` does today. While DISARMING
      the camera's handler is free (it refuses changes until disarmed).
    - Generally: a role may hold the device's answer until its own child
      reports; `TimedDevice` keeps the pending answer.
    - Names stay recording-specific until a second consumer exists (user:
      no specifics of other consumers for now).
    - `Rig` reads the Coordinator's state and devices through item 4's
      messages, keeping actor objects only for startup and shutdown.
  - **Every actor has its parent's address** (user), set by the runtime.
    Needed by the recorder, by a `SoftwareTimed` device subscribing to its
    parent, and by the parked halt. How it is set, and what goes to it, is
    in the parent/child rule below.
  - **A parent may wait on its children, never blocking** (user): as a
    state, like the Coordinator arming its devices. Such waits run only
    from parent to child; devices do not check on each other, which would
    risk circular dependencies.
  - **4. What a remote client needs.** `GetStatus` → `Status(state,
    acquisition, devices)` (each device's name, whether it is the source,
    required, present), also published to clients on every change. The
    Coordinator numbers acquisitions; `AcquisitionStarted`,
    `AcquisitionStopped` and `Status` carry the number. Faults and phases
    are not stamped: a test pins that the source's IDLE arrives before its
    `DeviceStopped` and `SourceFinished`.
  - **5. Subscribers.** The Coordinator no longer subscribes every device.
    A `SoftwareTimed` device subscribes to its parent in `setup`; a
    subscriber that is one of the Coordinator's devices hears phases only,
    any other hears everything. A device subscriber is dropped on its
    `ChildExited`; remote clients are the gateway's to fan out and prune.
  - **6. The shared event loop.** No structural change. A loop-lag probe
    (a periodic timer recording how late it fires) runs in the H9 soak on
    `Dev1-sim`, to find the cause of the 57 ms hold. Whether the gateway
    runs in the engine's process is an M6 decision; placing the source in
    a process of its own is the fallback, and works through the relay.
  - **7. The relay stays.** The direct path was wanted because relayed
    phases queued behind a blocked Coordinator, which item 1's states fix;
    across processes the relay needs no routing between links, and a
    relaunch of either end needs nothing. This closes "Software timing,
    direct" below.
- *The parent/child rule (user, 2026-10-02),* from a pattern the user saw
  in the decisions above: a child is synchronized with its parent, but it
  never waits on it; it advances its own state instead. Same strength as
  "a ref is the only handle": an exception is proposed and discussed on its
  own.
  - **Commands go down; answers and reports go up.** Only a parent sends a
    child a request that expects an answer. A child tells its parent
    answers and reports, and never waits on a reply from it. A child that
    needs its parent to decide something reports it and holds its state.
  - **An actor waits only on its children, as a state.** A parent's state
    holds the answers outstanding and a deadline (STARTING). A child "waits"
    on its parent by being in a state that names the command it takes next:
    IDLE takes `StartDevice`, ARMED takes `Go` or `StopDevice`.
  - **A child knows its parent only by address.** It speaks its own
    protocol and the parent adapts, as the recorder knows nothing of the
    camera but its buffer's name.
  - **Siblings never answer each other.** They coordinate through their
    common parent. That makes the system deadlock-free by construction:
    every wait follows a tree edge downward, and a tree has no cycles. It
    is also why the Coordinator relays the timing phases (user): the
    overhead is minimal and relaying is the Coordinator's role. This
    settles item 7 for good.
  - **Clients outside the tree** (the gateway, the rig, a CLI, tests) may
    send any actor a request, and no actor waits on them.
  - **A child's address is its lineage** (user). The runtime sets
    `parent: ActorRef` at spawn, `None` for a root, and its address is
    always the child's less its last segment (a test pins it). For a
    process child the link makes it with `ref_to`, as it already routes to
    the parent.
  - **Unprompted reports go to the parent by that address:** a device's
    `Fault`, the source's `SourcePhase` and `SourceFinished`, the recorder's
    `RecorderStarted`. The Coordinator then subscribes to nothing.
    Publishing is for listeners that opted in: clients outside the tree,
    and a child subscribing to its parent's phases (item 5).
  - **Every actor's use for a `Fault` is to pass it to its parent** (user);
    a root publishes it. This is not an exception to ignoring what an actor
    has no use for: every actor has this use. An actor that acts on a
    child's `Fault` (the Coordinator aborting on a fatal one) handles it in
    its own case, and a fault rises until something handles it or a root
    publishes it.
  - *Checked against the code (2026-10-02):* no child asks its parent, no
    actor asks a sibling, every report up is a tell, and the adapters send
    no messages. Departures, to fix when this is built:
    - Parents wait by blocking in their handlers (`Coordinator._start`,
      `_stop_everyone`, `BufferRecording`'s open and close). Items 1 and 3
      cover them. **Not yet covered:** `Coordinator._relaunch`, which for a
      process child waits inside the handler up to the host's
      `connect_timeout` (15 s); it becomes a state too.
    - Every device hears its siblings' refs today, in `DeviceLaunched` and
      in each relayed `Fault`'s `source`, because the Coordinator
      subscribes every device to itself. Item 5 ends it; a test is to pin
      that a device receives no other actor's ref.
    - `child.py` spawns a process child with `address=` and no parent;
      `spawn` gains the parent ref for that case.
- *Building it: acceptance tests CM1–CM51 (approved by the user,
  2026-10-02).* Seven steps, each leaving the suite green. Six details
  were settled with the list:
  1. A `Fault`'s default lives in `Actor.handle`: it passes the Fault to
     the parent and ignores the rest. Every `case _:` calls
     `super().handle(msg)`; an actor acting on a Fault has its own case.
  2. With a sender on every envelope, told and asked are the same: a
     refusal is `Rejected` to the sender; a `Fault` only when there is no
     sender (a tell from outside the tree).
  3. `Status` carries each device's ref, for clients; devices never get it.
  4. A process relaunch runs the host's launch as a task that tells the
     Coordinator the outcome: a launch is not another actor's reply.
  5. An acquisition takes its number when its start begins.
  6. `report(msg)`: to the parent, or published by a root. For `Fault`,
     `SourcePhase`, `SourceFinished`; the source still publishes
     `MainApplied` / `MainDiscarded`.

  **Step 1, addresses and reports (runtime).** CM1 `parent` is set at
  spawn, its address the child's less its last segment; `None` for a root.
  CM2 a process child's `parent` reaches the engine: a Fault it reports
  arrives at the Coordinator (integration). CM3 `report` tells the parent,
  or a root publishes. CM4 a Fault's default is the parent; a grandchild's
  climbs to the root, which publishes it. CM5 an actor handling a Fault in
  its own case stops it there. CM6 every envelope carries its sender: from
  inside an actor (or a task it started) its ref, from outside `None`, an
  `ask`'s its one-shot address; `reply` answers the sender, told or asked.
  CM7 the sender crosses a link (the wire's `r` field).

  **Step 2, time and lifetime (runtime).** CM8 `Clock.call_later`,
  cancellable, fired by `VirtualClock` at its deadline. CM9 `tell_later`
  delivers to the actor's own mailbox on its clock; cancellable; the
  actor's exit cancels it. CM10 a parent's exit stops its children first,
  in reverse launch order, then runs its `teardown`, also when it died of
  an error. CM11 a child mid-message finishes first, and is not reported
  lost. CM12 the same for a process child: the Coordinator's exit ends its
  process, and the device's `teardown` runs (integration). CM13 the
  priority-gated wait: messages above the handler's priority are handled
  as they arrive, the rest held until it returns, FIFO kept. CM14 an error
  in a message handled during the wait ends the actor as anywhere else.

  **Step 3, the Coordinator.** CM15 while STARTING it answers `GetStatus`
  and passes Faults on. CM16 each `ready_timeout` is a self-message; a late
  `DeviceReady` is ignored and that device stopped with the rest. CM17 a
  required device or the source lost while STARTING fails the start at
  once, naming it and why. CM18 an optional one is dropped and the start
  goes on. CM19 a device lost while STOPPING counts as done, reported lost.
  CM20 during an abort, a phase reaches phase subscribers before the stop
  handshake ends. CM21 a relaunch is a state: `GetStatus` answered during a
  slow launch, a second `Relaunch` of the same device `Rejected`,
  `StartAcquisition` refused while a required device relaunches. CM22
  FAULTED is gone: a lost source refuses starts, naming it, like any
  required device. CM23 the Coordinator subscribes to no device and no
  device to itself; device Faults reach it by address, a fatal one
  aborting. CM24 a `SoftwareTimed` device subscribes to its parent in
  `setup` and hears phases only; across a run with a fault and a relaunch,
  no device receives any ref but its own and its parent's. CM25 a client
  subscriber hears everything. CM26 a device subscriber is dropped on its
  exit. CM27 `GetStatus` → `Status(state, acquisition, devices)`, each
  device's name, source, required, present and ref. CM28 `Status` is
  published on every change. CM29 acquisitions are numbered in
  `AcquisitionStarted`, `AcquisitionStopped` and `Status`. CM30 the
  source's IDLE reaches the Coordinator before its `DeviceStopped` and
  `SourceFinished`. CM31 the new messages cross the codec.

  **Step 4, devices.** CM32 a role may hold the arm's answer: ARMING with
  the asker kept; completed → ARMED, `DeviceReady`; failed → undone,
  `Rejected`, IDLE. CM33 `BufferRecording` is ARMING until the recorder
  reports `RecorderStarted`. CM34 a recorder that fails to start undoes the
  arm, `Rejected` with its reason. CM35 `StopDevice` while ARMING stops the
  recorder and disarms. CM36 disarm sends `FinishRecording(published)`,
  DISARMING; `RecordingFinished(frames)` with the full count →
  `DeviceStopped`; short fails the stop as today. CM37 a recorder that
  exits with an error while DISARMING fails the stop as today. CM38 while
  DISARMING the handler answers queries and refuses `StartDevice`. CM39 the
  recorder answers `FinishRecording` without blocking its mailbox.

  **Step 5, "now playing".** CM40 `samples_played()` in the port contract
  suite: 0 before the clock, never above written, growing while playing,
  held after a stop. CM41 WARMUP announced when the clock starts. CM42 RUN
  once `samples_played()` reaches WARMUP's length, never before, within a
  timer interval after. CM43 COOLDOWN at its first sample, IDLE once
  drained. CM44 stopped before the first MAIN plays: WARMUP, COOLDOWN,
  IDLE, no RUN; a device waiting for RUN never starts its routine. CM45 a
  source with no buffer announces at once (the base default). CM46 RUN is
  on time through a long refill wait (the gated wait). CM47 announcements
  never overtake each other. Docstrings: `SoftwareTimed`, `MainApplied`.

  **Step 6, one host clock.** CM48 every capture that stamps host time
  uses `time.monotonic_ns`. CM49 each TIFF carries one wall-clock anchor.

  **Step 7, clients.** CM50 the rig works only through messages
  (`rig.device`, `rig.source`, `close` from `Status`); stopping the
  Coordinator stops every device. CM51 a loop-lag probe in the H9 soak (a
  measurement, on `Dev1-sim`). Then the plan's "Coordinator and timing
  source" section is rewritten and CLAUDE.md brought up to date.

- **The communication model (user, 2026-09-30): a deep dive, later.** For
  now the focus is a system that works.
  - *Software timing, direct.* **Closed (user, 2026-10-02): the relay
    stays** (the core review's observation 7). What follows is the
    question as it stood. A `SoftwareTimed` device should hear the
    source's phases from the source, not relayed by the Coordinator. Open:
    whether the Coordinator supplies the device with the source's address,
    or the source with the device's; and how that survives a relaunch of
    either (a relaunched source starts with no subscribers).
  - *Process placement.* On the engine side, each child's link knows only
    the Coordinator and the runtime's last-word address
    (`ProcessHost.launch`). A process-placed device therefore cannot reach
    the source, and a source in its own process cannot reach any other
    child. The relay works today only because every child can reach the
    Coordinator. Direct subscription across processes needs the engine to
    route between links.
  - *Ordering.* A device that subscribes before it answers `DeviceReady`
    is subscribed before `Go`, because each link is first-in, first-out and
    the engine forwards as frames arrive.

- **NI-DAQmx, carried over from 5a (2026-09-23).** None of these blocks
  anything.
  - *The analog tail.* Whether the last analog samples play, with and
    without `TAIL_PAD`, and where the scanners and laser are left parked
    (H2's unanswered half). This needs non-zero output and a scope, run by
    the user. `TAIL_PAD` pads both tasks, so the fix covers analog if it
    has the same quirk.
  - *When running dry is declared.* Earlier notes say about 13 ms before
    the FIFO finishes. H1's second survey saw the task stop at the
    predicted end. The 13 ms may be `is_task_done` raising early. The
    adapter does not depend on it.
  - *`buffer_samples`.* It stays at 5000. One full-suite run dipped to
    12.5 ms queued (one refill, after a 57 ms loop hold), and the next ran
    clean at 39 ms. Before going to 10 000, rerun H9 with
    `STACSCOPE_SOAK_SECONDS`, and check the conditions under
    "`buffer_samples` = 5000".
  - *Deployment.* Nothing has loaded the instrument's own `SPIM_*` tasks
    through `read_output_config` / `configure` on the control PC yet. That
    belongs to integration (milestone 7), and the user runs it. The rig
    config in `app/`, which names the tasks and may override
    `BUFFER_SAMPLES`, is built (2026-10-01; "The rig config").
  - *`QueueMain` through the Coordinator.* Not routed; it goes straight to
    the source, like `SetWaveform`.

- **Logging and error reporting (opened 2026-09-22).** Core invariant 6 sets
  the standard; nothing implements it yet. Needs its own milestone, after M5's
  adapters give it something real to report about. Scope to settle then: where
  diagnostics originate (the actor runtime, not each device), how a fault
  reaches the operator versus the supervisor, what a device may log without
  touching the timing flow, and whether the external control surface carries a
  log stream. Explicitly *not* a `Logger` actor of the kind the legacy
  abandoned.
- **Device-declared timing floors (opened 2026-09-22).** The legacy camera
  classes implement `Get Minimum Trigger Interval.vi` —
  `max(readout + 1 ms, internal frame interval)` — but nothing in the exported
  hierarchy consumes it, so whether the timing actor ever validated a cycle
  against its devices' limits is unrecoverable. Our `TimedDevice` has no
  equivalent. Worth deciding whether a participant should be able to constrain
  the `TimingSource`, which would be a real addition to the arm handshake, not
  a restoration.
  *Noted 2026-09-24:* DCAM reports the floor itself, as
  `TIMING MIN TRIGGER INTERVAL` (20.0 ms at full frame, 10 ms exposure, AREA
  mode on the dev box), so a camera device would not need to compute it.
  *2026-09-30:* each triggered camera now **answers the floor when asked**:
  `GetShortestTriggerPeriod` → `ShortestTriggerPeriod(seconds)`, a core
  message (user: a query of its own). The uEye works it out from the
  minimum frame time, exposure and trigger delay (exact on the camera,
  5f-3); the Flash reads it from DCAM. Nothing yet checks a waveform
  against it: whether the `TimingSource` or the arm handshake should is
  still this item's question.
- **Legacy napari viewer (decided 2026-09-19):** left alone; `lightsheet.py`
  cannot read the new header and gets no compatibility layer. It will be
  replaced later by a lightweight buffer viewer that is also usable for
  testing experimental adapters and processing pipelines. Options under
  investigation.
  **Decided 2026-09-19:** pyqtgraph + PySide6, as an optional `viewer` extra.
  It runs as its own consumer process and is never imported by the core.
  PySide6 has no free-threaded wheel (it is `abi3` only), so the viewer runs on
  standard 3.14; the buffer's layout does not depend on the interpreter build.
- **Consumer API.** Decided by the user 2026-09-19; the design details are
  still under discussion:
  - **No backpressure, as a core design decision.** A consumer that needs every
    frame is responsible for keeping up, or for handling the overrun error.
    Only recording to disk is expected to need every frame, and even that is
    not required. Most other consumers want only the most recent data.
  - **Views by default; the buffer makes no copies of its own.** Configuration
    (buffer length) is what keeps a processor from being overwritten. A copy
    is one line of numpy for the consumer.
  - **Per-frame metadata: yes.**
  - **The producer defines the buffer structure,** and the header describes it
    fully, strides and padding included, so that consumers outside Python can
    read it. Top-level acquisition config may choose the buffer length (e.g.
    to align with the stack size), which the producer does not otherwise know.
    Row padding must be representable: the Flash likely does not pad, but a 1D
    PMT readout with blanked corners would.
  - **Buffer lifetime = device lifetime.** The software cannot assume it knows
    every consumer, so consumers must detect a change or reset to a new
    acquisition from the buffer itself.
  - **Discovery and meaning** (what the frames mean) are out of scope for
    Milestone 2. "Most recent" can mean the most recent complete stack, which
    the buffer does not understand, so the consumer API must let a consumer
    express grouping it learned elsewhere.
  - The consumer interface, as the user sees it:
    - watch for new frames;
    - expose the whole buffer as one memory-mapped numpy array;
    - give quick access to the next or most recent frame from the consumer's
      local count;
    - respond to a buffer change or reset.
  - **Decided 2026-09-19:**
    - *Allocation:* the segment's capacity is set from config at device start,
      and each acquisition's layout (slots, shape, strides) fits inside it. A
      buffer smaller than its allocation is fine. A layout change bumps
      `layout_generation` in the header.
    - *Counting:* `frames_written` only ever counts up, for the device's whole
      lifetime. The header carries `acquisition_id` and `acquisition_start`,
      and each acquisition starts writing at slot 0.
    - *Waiting:* polling is fine, hidden behind `wait()`.
    - *Producer restart (decided 2026-09-19):* a restarted producer **takes
      over** the segment its predecessor left, if it is an image buffer of
      this version with enough capacity and slots. The counter and
      acquisition ids carry on, and readers still holding the segment follow
      the next `configure`, so a restart behaves the same on both OSes. A
      layout change the predecessor died in the middle of is completed, not
      trusted.
      Only a segment that does not fit still differs: POSIX replaces it (its
      readers keep an orphan), Windows refuses while a reader holds it.
      Caveat: two live producers configured with the same name would share
      the segment; keeping names unique is config's job. (commit d05f30c)
    - *In-flight depth:* unknown for DCAM, so the header carries it.
      **Corrected 2026-09-19 (found by B8):** `in_flight` is at least 1 for
      *every* producer, not 0 for copying ones. Any producer writes frame
      `published()` into its slot before publishing it. A DMA device may
      declare more.
  - **Per-frame metadata.** The investigation (2026-09-19) found camera
    timestamps are common but often not from the camera itself:
    - IDS uEye: `UEYEIMAGEINFO` has `u64FrameNumber`, `u64TimestampDevice`
      (camera clock, 0.1 µs, taken at the trigger or at readout start), and a
      host timestamp at 1 ms resolution.
    - DCAM: `DCAMBUF_FRAME` has `timestamp`, `framestamp` and `camerastamp`.
      `TIMESTAMP_PRODUCER` is NONE, DCAM module, kernel driver or capture
      device, with no imaging-device value (only `FRAMESTAMP_PRODUCER` has
      one). So the Flash's "camera time" may really be frame-grabber or driver
      time; this needs checking on the instrument.
      *Checked on the dev box 2026-09-24 (5b survey):* both producers read
      DCAM MODULE, so device time is host software time
      (`TimeSource.DRIVER`). *Correction:* `TIMESTAMP_PRODUCER` does have an
      IMAGING DEVICE value (5, in `dcamprop.h` of SDK 25.05), and this camera
      lists it, but the property is read-only and what selects it is unknown.
    - PVCAM: `FRAME_INFO` gives `FrameNr` plus beginning- and end-of-frame
      timestamps from the OS. For fast sCMOS it recommends embedded frame
      metadata instead.
    - Andor SDK3: metadata blocks appended to the frame hold ticks since
      camera start (plus `TimestampClockFrequency`) and FrameInfo, which
      includes a row **stride**, so real cameras do pad rows.
    - pco.edge: a BCD timestamp and image counter burned into the first 16
      pixels.
    - Thorlabs TSI: tagged metadata (`FCNT` frame count, pixel-clock ticks) and
      `timestamp_clock_frequency`.
    - GenICam/USB3 Vision: `ChunkFrameID` and `ChunkTimestamp`.
  - **Rule (user, 2026-09-19): the software producer never modifies what the
    hardware writes.** When the device writes its own format by DMA, timestamp
    included, those slot bytes are the hardware's. The software producer writes
    only outside the slots (the counters and the metadata table), and describes
    the slot layout in the header rather than changing it.
    As a consequence (pending the user's confirmation), device time is stored
    as raw ticks plus a header `device_clock_hz`, not converted to ns.
    The user confirmed (2026-09-19) that the table may hold copies of values
    read from the slot, as long as it is written separately. No test for the
    rule yet; it is too implementation-specific.
  - **How each SDK accepts frame memory** (investigated 2026-09-19). This
    decides where slots can live; the metadata table is separate from the
    slots, so it is unaffected either way.
    - DCAM: `dcambuf_attach` takes an array of per-frame pointers.
    - Andor SDK3: `AT_QueueBuffer`, one user buffer at a time (8-byte
      aligned), returned by `AT_WaitBuffer`.
    - uEye: `is_SetAllocatedImageMem` + `is_AddToSequence`, per-frame user
      memory in a ring.
    - GenTL (Euresys, GenICam frame grabbers): `DSAnnounceBuffer`, per-buffer
      user memory, with alignment reported by the stream.
    - Spinnaker: `SetUserBuffers` takes either one contiguous block or
      separate buffers. For USB3, sizes are rounded up to 1024 bytes.
    - PVCAM: `pl_exp_start_cont` takes **one contiguous circular buffer**,
      from the user or preallocated by the driver
      (`pl_exp_get_driver_buffer`).
    - pco.sdk: `PCO_AllocateBuffer` (SDK-owned), plus `PCO_AddBufferExtern`
      (external memory; details unverified).
    - Thorlabs TSI: I believe the SDK owns its buffers and hands out a pointer
      per frame, so a copy is needed (unverified).
    - Conclusions:
      - Every SDK surveyed notifies per frame, so software-written metadata is
        always possible.
      - A contiguous driver ring (PVCAM) only fixes `slot_bytes` and the slot
        order. Both match the "each acquisition starts at slot 0" rule.
      - SDK-owned memory (TSI, and PVCAM's driver buffer) means a copying
        producer.
      - A header-level mask of which metadata fields are present covers "no
        frame counter" and "no device time". An empty mask means no per-frame
        metadata.
  - The proposed metadata record is `sequence`, `hw_frame`, `device_time_ns`,
    `host_time_ns` and `flags`. *(Superseded in part by the rule above:
    `device_time_ns` would become raw `device_ticks`.)*
    - Host time is always recorded, when the producer sees the frame.
    - Device time is converted to ns by the producer, with a flag when it is
      unavailable.
    - The header records where the device time came from (none / camera /
      capture device / driver), because the two clocks are not comparable.
    - Metadata embedded in the pixels (pco, PVCAM, Andor, TSI) is decoded by
      the producer into the table. The raw bytes stay in the slot, skipped
      via `frame_offset` / `slot_bytes` / strides.
  - **Benchmark tiers:** USB 2 (about 0.04 GB/s); USB 3 (about 0.35–0.4 GB/s;
    the Flash V3 over USB 3 is 40 fps at full frame, 16-bit, about 0.34 GB/s);
    0.8 GB/s (Flash over Camera Link); 5 GB/s (CXP-12 on 4 links); 10 GB/s
    (CLHS, or CXP on 8 links).
    - USB producers are the likeliest to write frames into the buffer
      themselves; faster interfaces must use DMA.
    - The test producer writes only a small moving bar of pixels per frame
      (visible to a human, and runs far above any tier), plus the metadata.
      Automated tests check the metadata, not full frames.
    - Missing a tier is only a failure for a consumer designed to reach it;
      otherwise the benchmark just reports.
  - Reference consumer: the legacy napari viewer (`lightsheet.py` plus the
    notebook). It reads zero-copy `frombuffer` views, and copies only when
    moving data into the display.

- **Bring Chapter 2, the MATLAB ETL model file, and `etl_calibration.txt` into the repo.** They exist and are authoritative for the ETL MPC. Not needed for milestone 1, but required before waveform generation resumes — and since the legacy path ran MPC through a MATLAB script node (`etl-mpc_matlab.vi` + `matscript.dll`), that will be a reimplementation rather than a port.
- ~~Confirm `pydantic-core` free-threaded wheel availability and ctypes behaviour under `3.14t` at first install; fall back to standard 3.14 if either fails.~~ **Resolved 2026-09-18:** both work on `3.14t`, no fallback needed; pinned by `tests/unit/test_environment.py`.
- The recovered `NI-DAQmx.ini` records `SampQuant.SampPerChan = 100`, which at 100 kS/s is only 1 ms of buffer. That is almost certainly a stored default that LabVIEW overrode at runtime rather than the real buffer size — confirm the actual DAQmx output buffer depth before tuning the refill loop, since it sets the underflow margin.
