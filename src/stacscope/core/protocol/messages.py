"""Control-flow messages between clients, the Coordinator and devices.

Plain frozen dataclasses of primitives and arrays, never live references
(invariant 4). Direction is part of the name: client → Coordinator messages
are ``*Acquisition``; Coordinator → device messages are ``*Device``.
"""

from dataclasses import dataclass
from enum import Enum

import numpy as np

from stacscope.core.actors.ref import ActorRef
from stacscope.core.protocol.maps import ReadOnlyMap
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


# --- anyone → a device: its properties ----------------------------------------
#
# Every device answers these, from the table its adapter declares. A device
# with no table answers an empty map, and refuses any name.


class PropertyKind(Enum):
    NUMBER = "number"
    """A float, within ``minimum`` and ``maximum``, or one of ``choices``."""
    LABEL = "label"
    """One of ``labels``."""
    FLAG = "flag"
    """True or false."""


class PropertyAccess(Enum):
    READ_ONLY = "read_only"
    IDLE = "idle"
    """Changeable only while the device is not armed for an acquisition."""
    ALWAYS = "always"
    """Changeable at any time, during an acquisition too."""


@message
@dataclass(frozen=True, slots=True)
class Property:
    """One of a device's properties, as it stands.

    ``value`` is a float for a NUMBER, a string for a LABEL and a bool for a
    FLAG. ``unit`` is an SI symbol, such as "s" or "Hz", or "" for none.
    ``minimum``, ``maximum`` and ``step`` bound a NUMBER where they are not
    ``None``; ``choices``, if not empty, are the only numbers it takes.
    ``labels`` are a LABEL's values. Every field but ``name`` and ``kind``
    may change as other properties are set.
    """

    name: str
    kind: PropertyKind
    value: bool | float | str
    unit: str = ""
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: tuple[float, ...] = ()
    labels: tuple[str, ...] = ()
    access: PropertyAccess = PropertyAccess.ALWAYS


def _read_only(msg: object, field: str) -> None:
    # A message is one object shared by every subscriber in a process.
    object.__setattr__(msg, field, ReadOnlyMap(getattr(msg, field)))


@message(client=True)
@dataclass(frozen=True, slots=True)
class GetProperties:
    """Read every property of a device. Replied ``Properties``."""


@message
@dataclass(frozen=True, slots=True)
class Properties:
    """A device's whole table, by name, in the device's own order."""

    properties: dict[str, Property]

    def __post_init__(self) -> None:
        _read_only(self, "properties")


@message(client=True)
@dataclass(frozen=True, slots=True)
class GetProperty:
    """Read one property. Replied ``Property``, or ``Rejected`` for a name
    the device does not have."""

    name: str


@message(client=True)
@dataclass(frozen=True, slots=True)
class SetProperty:
    """Set one property. Replied ``PropertiesChanged``, or ``Rejected`` with
    the reason, in which case nothing changed.

    Sets sent without waiting for their answers are applied in the order
    sent, and a refused one does not stop those after it.
    """

    name: str
    value: bool | float | str


@message
@dataclass(frozen=True, slots=True)
class PropertiesChanged:
    """Every property whose value or limits a set moved, as it now stands,
    the one set always among them. Answers the set, and is published to the
    device's subscribers."""

    properties: dict[str, Property]

    def __post_init__(self) -> None:
        _read_only(self, "properties")


@message(client=True)
@dataclass(frozen=True, slots=True)
class SetProperties:
    """Set several properties in one message. The device applies them in its
    own order, whatever the map's, and checks each as it comes.

    Replied ``PropertiesChanged``; or ``Rejected``, with nothing applied,
    for an unknown name, a read-only property, a value of the wrong kind, a
    label not listed, or a property changeable only while idle; or
    ``PropertiesRefused``, if one was refused partway. Nothing applied
    before a refusal is put back.
    """

    values: dict[str, bool | float | str]

    def __post_init__(self) -> None:
        _read_only(self, "values")


@message
@dataclass(frozen=True, slots=True)
class PropertiesRefused:
    """A ``SetProperties`` stopped at the property ``name``, for ``reason``:
    ``changed`` holds every property that moved before it, as it now stands,
    and is published. Those after it were not tried. ``name`` is empty if
    the device could not say which property it refused."""

    name: str
    reason: str
    changed: dict[str, Property]

    def __post_init__(self) -> None:
        _read_only(self, "changed")


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
