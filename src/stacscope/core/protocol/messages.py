"""Control-flow messages between clients, the Coordinator and devices.

Plain frozen dataclasses of primitives and arrays, never live references
(invariant 4). Direction is part of the name: client → Coordinator messages
are ``*Acquisition``; Coordinator → device messages are ``*Device``.
"""

from dataclasses import dataclass
from enum import Enum

import numpy as np

from stacscope.core.actors.ref import ActorRef
from stacscope.core.protocol.registry import message

# --- client → Coordinator ----------------------------------------------------


@message(client=True)
@dataclass(frozen=True, slots=True)
class StartAcquisition:
    """Begin acquiring. ``cycles < 0`` runs until ``StopAcquisition``; an empty
    ``root_filename`` means do not record."""

    cycles: int
    root_filename: str


@message(client=True)
@dataclass(frozen=True, slots=True)
class StopAcquisition:
    pass


@message
@dataclass(frozen=True, slots=True)
class AcquisitionStarted:
    """Reply to ``StartAcquisition``: every device was ready and the source has
    been told to go. ``acquisition`` is its number."""

    acquisition: int


@message
@dataclass(frozen=True, slots=True)
class AcquisitionStopped:
    """Reply to ``StopAcquisition``: output has ceased and every device
    stopped. ``acquisition`` is the number of the one that stopped."""

    acquisition: int


@message(client=True)
@dataclass(frozen=True, slots=True)
class GetStatus:
    """Ask the Coordinator for its ``Status``."""


@message(client=True)
@dataclass(frozen=True, slots=True)
class Relaunch:
    """Spawn a fresh instance of a lost device from its config. Replied
    ``Accepted`` once spawned (a failed setup then arrives as a ``Fault``) or
    ``Rejected``. Never sent by STACScope itself: when to relaunch is the
    user's decision."""

    name: str


# --- Coordinator → its clients -------------------------------------------------


class CoordinatorState(Enum):
    IDLE = "idle"
    STARTING = "starting"
    RUNNING = "running"
    STOPPING = "stopping"


@message
@dataclass(frozen=True, slots=True)
class DeviceStatus:
    """One device, as the Coordinator sees it: whether it is the timing
    source, whether acquisitions need it, and whether it is running now.
    ``ref`` reaches it while it is present."""

    name: str
    source: bool
    required: bool
    present: bool
    ref: ActorRef | None


@message
@dataclass(frozen=True, slots=True)
class Status:
    """The answer to ``GetStatus``, and published to clients on every
    change. ``acquisition`` is the number of the latest acquisition begun,
    0 before the first; ``devices`` lists the source first, then the
    participants in their configured order."""

    state: CoordinatorState
    acquisition: int
    devices: tuple[DeviceStatus, ...]



@message
@dataclass(frozen=True, slots=True)
class DeviceLaunched:
    """A device was (re)launched; ``device`` is its current ref. Refs to a
    previous incarnation reach nothing, so clients holding one refresh it."""

    name: str
    device: ActorRef


# --- Coordinator ↔ every participant, the timing source included -------------


@message
@dataclass(frozen=True, slots=True)
class StartDevice:
    """Prepare for acquisition, then reply ``DeviceReady``. ``cycles`` matters
    only to the timing source; other participants ignore it."""

    root_filename: str
    cycles: int


@message
@dataclass(frozen=True, slots=True)
class DeviceReady:
    pass


@message
@dataclass(frozen=True, slots=True)
class StopDevice:
    """Finalize acquisition and return to Idle, then reply ``DeviceStopped``."""


@message
@dataclass(frozen=True, slots=True)
class DeviceStopped:
    pass


# --- Coordinator ↔ timing source ---------------------------------------------


class Phase(Enum):
    """Timing-source phases. The progression is required of every source;
    what each phase does is up to the implementation."""

    IDLE = "idle"
    ARMED = "armed"
    WARMUP = "warmup"
    RUN = "run"
    COOLDOWN = "cooldown"


@message
@dataclass(frozen=True, slots=True)
class Go:
    """Every participant is ready: begin hardware output."""


@message
@dataclass(frozen=True, slots=True)
class Finish:
    """Leave Run for Cooldown at the source's next safe boundary."""


@message
@dataclass(frozen=True, slots=True)
class SourcePhase:
    """Reported by the timing source to its parent as each phase starts
    playing -- "now playing", not "now queued" -- and relayed by the
    Coordinator to whoever subscribed to it. Good to the control flow's
    precision: milliseconds, never samples."""

    phase: Phase


@message
@dataclass(frozen=True, slots=True)
class SourceFinished:
    """Hardware output has ceased."""


# --- client → WaveformSource, directly (never through the Coordinator) ---------


class Waveform(Enum):
    """The three waveforms an AO source holds: WARMUP plays once, MAIN once
    per cycle, COOLDOWN once. Not called "phases": ``Phase`` is the
    acquisition's state, and RUN may play several MAINs."""

    WARMUP = "warmup"
    MAIN = "main"
    COOLDOWN = "cooldown"


@message(client=True)
@dataclass(frozen=True, slots=True, eq=False)
class SetWaveform:
    """Replace a waveform. ``ao`` is AO channels × samples in volts; ``do`` is
    DO lines × samples, boolean; both in the configured channel order and the
    same length. A MAIN update mid-run takes effect at the next cycle boundary.
    Replied ``Accepted`` or ``Rejected``."""

    waveform: Waveform
    ao: np.ndarray
    do: np.ndarray


@message(client=True)
@dataclass(frozen=True, slots=True, eq=False)
class QueueMain:
    """Queue a MAIN to play for exactly one cycle, after any already queued.
    When the queue runs out, the last MAIN taken keeps playing. ``tag`` is
    the caller's name for it, echoed in ``MainApplied`` and ``MainDiscarded``.
    ``ao`` and ``do`` are as for ``SetWaveform``; the length need not match
    the current MAIN's, and the samples are played exactly as given.

    A ``SetWaveform`` of MAIN empties the queue. So does the end of an
    acquisition: nothing queued for one acquisition plays in the next. Either
    way the dropped tags are published in ``MainDiscarded``.

    Replied ``Accepted``, or ``Rejected`` if the waveform is malformed or the
    queue is full. A slot frees each time a queued MAIN is taken."""

    tag: str
    ao: np.ndarray
    do: np.ndarray


@message
@dataclass(frozen=True, slots=True)
class MainApplied:
    """Published by a waveform source when a different MAIN is taken for a
    cycle, and for the first cycle of every acquisition. ``tag`` is the
    ``QueueMain`` tag, or ``None`` for a MAIN set by ``SetWaveform``.
    ``cycle`` counts MAIN cycles from 0 in this acquisition; ``sample`` is
    the index of the cycle's first sample in the acquisition's output,
    counting from the first sample of warmup, so it plays ``sample / rate``
    seconds after output starts. Published when the cycle is queued, a
    buffer's depth ahead of when it plays -- unlike ``SourcePhase``, which
    means "now playing". It says *where* the MAIN plays, and a closed loop
    needs that early, to queue the next one in time."""

    tag: str | None
    cycle: int
    sample: int


@message
@dataclass(frozen=True, slots=True)
class MainDiscarded:
    """Published by a waveform source when queued MAINs are dropped unplayed,
    with their tags in queue order and why."""

    tags: tuple[str, ...]
    reason: str


# --- client → a triggered camera, directly ------------------------------------


@message(client=True)
@dataclass(frozen=True, slots=True)
class GetShortestTriggerPeriod:
    """Ask a camera for the shortest time from one trigger to the next that it
    will not ignore, at its current settings. Replied
    ``ShortestTriggerPeriod``, or ``Rejected``."""


@message
@dataclass(frozen=True, slots=True)
class ShortestTriggerPeriod:
    """The shortest trigger period a camera takes at its current settings, in
    seconds. A trigger sooner than this is ignored, and the frame is lost.
    It changes with the settings, so ask again after changing any."""

    seconds: float


# --- client → a recording device, directly --------------------------------------


@message(client=True)
@dataclass(frozen=True, slots=True)
class SetSaveDirectory:
    """Record later acquisitions into the existing directory ``path``.
    Replied ``SaveDirectory``, or ``Rejected`` during an acquisition or for a
    directory that does not exist."""

    path: str


@message
@dataclass(frozen=True, slots=True)
class SaveDirectory:
    """The directory a device records acquisitions into."""

    path: str


# --- a streaming device → the recorder it started -------------------------------


@message
@dataclass(frozen=True, slots=True)
class RecorderStarted:
    """From a recorder to its parent: its file is open and it is reading the
    buffer, so the parent's arm can complete."""


@message
@dataclass(frozen=True, slots=True)
class FinishRecording:
    """The acquisition being recorded ended at ``frames`` frames: write up to
    that many, close the file, and stop. Replied ``RecordingFinished``; a
    recorder that cannot ends with an error instead."""

    frames: int


@message
@dataclass(frozen=True, slots=True)
class RecordingFinished:
    """The recording is closed, holding ``frames`` frames."""

    frames: int
