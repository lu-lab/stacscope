"""Control-flow messages between clients, the Coordinator and devices.

Plain frozen dataclasses of primitives and arrays, never live references
(invariant 4). Direction is part of the name: client → Coordinator messages
are ``*Acquisition``; Coordinator → device messages are ``*Device``.
"""

from dataclasses import dataclass
from enum import Enum

from stacscope.core.protocol.registry import message

# --- client → Coordinator ----------------------------------------------------


@message
@dataclass(frozen=True, slots=True)
class StartAcquisition:
    """Begin acquiring. ``cycles < 0`` runs until ``StopAcquisition``; an empty
    ``root_filename`` means do not record (§3.4.1)."""

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
