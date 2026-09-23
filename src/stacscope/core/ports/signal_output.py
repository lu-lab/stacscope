"""SignalOutput port: hardware-clocked analog and digital output.

Modelled on continuous output with regeneration disabled: each sample written
plays exactly once, in order, at ``rate``. The caller must keep the buffer fed;
running dry mid-stream is an ``Underflow``, never a silent gap.
"""

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True, slots=True)
class Channel:
    """A physical channel and what it means on this rig. Wiring can change,
    so code refers to neither directly — both come from config."""

    physical: str
    label: str


@dataclass(frozen=True, slots=True)
class OutputConfig:
    rate: float
    """Samples per second, shared by every AO channel and DO line."""
    ao: tuple[Channel, ...]
    do: tuple[Channel, ...]
    """Every configured line is emitted, whether or not any actor claims it."""
    buffer_samples: int
    """Hardware buffer depth, per channel."""


class Underflow(RuntimeError):
    """The output ran dry mid-stream: hardware emitted a gap."""

    def __init__(self, at: float) -> None:
        super().__init__(f"output ran dry at t = {at} s")
        self.at = at


# Deliberately dumb: chunking, pre-roll and when to start belong to the
# WaveformSource using it, so each hardware adapter stays thin.
class SignalOutput(Protocol):
    async def configure(self, config: OutputConfig) -> None:
        """Reserve and commit hardware. Once, at setup."""
        ...

    async def arm(self) -> None:
        """Prepare a fresh stream: nothing written, nothing playing."""
        ...

    def space_available(self) -> int:
        """Samples that may be written now. Raises ``Underflow`` if it occurred."""
        ...

    async def write(self, ao: np.ndarray, do: np.ndarray, *, label: str | None = None) -> None:
        """Queue ``ao`` (channels × n, volts) and ``do`` (lines × n, bool).
        ``n`` must not exceed ``space_available()``. ``label`` marks the start
        of a segment, for diagnostics. Raises ``Underflow`` if it occurred."""
        ...

    async def go(self) -> None:
        """Start the sample clock. ``RuntimeError`` if nothing has been written."""
        ...

    async def wait_for_space(self, n: int) -> None:
        """Return once ``n`` samples may be written. Raises ``Underflow``, or
        ``RuntimeError`` if the buffer is full and output was never started."""
        ...

    async def drain(self) -> None:
        """No more samples are coming: return once everything written has
        played. Running dry after this is the expected end, not an underflow."""
        ...

    async def stop(self) -> None:
        """Stop output. Safe in any state, any number of times."""
        ...

    async def close(self) -> None:
        """Release the hardware. Safe in any state, including before
        ``configure``, and any number of times. Afterwards every other call
        except ``stop`` raises ``RuntimeError``. Call ``stop`` first: closing
        is not guaranteed to stop output cleanly."""
        ...
