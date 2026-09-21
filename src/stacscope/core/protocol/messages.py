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


@message
@dataclass(frozen=True, slots=True)
class StartAcquisition:
    """Begin acquiring. ``cycles < 0`` runs until ``StopAcquisition``; an empty
    ``root_filename`` means do not record."""

    cycles: int
    root_filename: str


@message
@dataclass(frozen=True, slots=True)
class StopAcquisition:
    pass


@message
@dataclass(frozen=True, slots=True)
class AcquisitionStarted:
    """Reply to ``StartAcquisition``: every device was ready and the source has
    been told to go."""


@message
@dataclass(frozen=True, slots=True)
class AcquisitionStopped:
    """Reply to ``StopAcquisition``: output has ceased and every device stopped."""


@message
@dataclass(frozen=True, slots=True)
class Relaunch:
    """Spawn a fresh instance of a lost device from its config. Replied
    ``Accepted`` once spawned (a failed setup then arrives as a ``Fault``) or
    ``Rejected``. Never sent by STACScope itself: when to relaunch is the
    user's decision."""

    name: str


# --- Coordinator → its subscribers ---------------------------------------------


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
    """Published by the timing source on every phase transition."""

    phase: Phase


@message
@dataclass(frozen=True, slots=True)
class SourceFinished:
    """Hardware output has ceased."""


# --- client → WaveformSource, directly (never through the Coordinator) ---------


class Segment(Enum):
    """The three waveform segments an AO source holds. Not called "buffers":
    that word is reserved for the data flow."""

    WARMUP = "warmup"
    MAIN = "main"
    COOLDOWN = "cooldown"


@message
@dataclass(frozen=True, slots=True, eq=False)
class SetSegment:
    """Replace a segment. ``ao`` is AO channels × samples in volts; ``do`` is
    DO lines × samples, boolean; both in the configured channel order and the
    same length. A MAIN update mid-run takes effect at the next cycle boundary.
    Replied ``Accepted`` or ``Rejected``."""

    segment: Segment
    ao: np.ndarray
    do: np.ndarray
