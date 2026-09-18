"""Simulated SignalOutput: a virtual sample clock that consumes what is written."""

import math

import numpy as np

from stacscope.core.ports import Clock, OutputConfig, Underflow

_EPS = 1e-9  # guards floor() against float error at exact sample boundaries


class SimulatedSignalOutput:
    """Implements ``SignalOutput`` on a ``Clock``. Samples play at ``rate``
    from ``go()``; what has played is computed from the clock, so it is exact
    in virtual time. Keeps everything written, for tests to inspect.
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self.config: OutputConfig | None = None
        self._reset()

    def _reset(self) -> None:
        self.timeline: list[tuple[str, int]] = []
        """``(label, first sample index)`` for each labelled write."""
        self.samples_written = 0
        self.underflow: float | None = None
        """Virtual time the output ran dry mid-stream, if it did."""
        self.stopped = False
        self._ao: list[np.ndarray] = []
        self._do: list[np.ndarray] = []
        self._started_at: float | None = None
        self._draining = False
        self._played_at_stop = 0
        self._stall = 0.0

    # --- SignalOutput --------------------------------------------------------

    async def configure(self, config: OutputConfig) -> None:
        self.config = config
        self._reset()

    async def arm(self) -> None:
        self._reset()

    def space_available(self) -> int:
        self._check()
        return self.config.buffer_samples - (self.samples_written - self.samples_played())

    async def write(self, ao: np.ndarray, do: np.ndarray, *, label: str | None = None) -> None:
        if self._stall:
            stall, self._stall = self._stall, 0.0
            await self.clock.sleep(stall)
        n = ao.shape[1]
        if ao.shape != (len(self.config.ao), n) or do.shape != (len(self.config.do), n):
            raise ValueError(f"shapes {ao.shape} / {do.shape} do not match the configured channels")
        if n > self.space_available():
            raise ValueError(f"{n} samples do not fit in {self.space_available()} free")
        if label is not None:
            self.timeline.append((label, self.samples_written))
        self._ao.append(ao.copy())
        self._do.append(do.copy())
        self.samples_written += n

    async def go(self) -> None:
        if self.samples_written == 0:
            raise RuntimeError("cannot start output with nothing written")
        self._started_at = self.clock.now()

    async def wait_for_space(self, n: int) -> None:
        if n > self.config.buffer_samples:
            raise ValueError(f"{n} exceeds the {self.config.buffer_samples}-sample buffer")
        if self.space_available() >= n:
            return
        if self._started_at is None:
            raise RuntimeError("buffer is full and output was never started")
        played_needed = self.samples_written - self.config.buffer_samples + n
        await self._sleep_until(self._started_at + played_needed / self.config.rate)
        self._check()

    async def drain(self) -> None:
        self._check()
        self._draining = True
        if self._started_at is not None:
            await self._sleep_until(self._started_at + self.samples_written / self.config.rate)

    async def stop(self) -> None:
        if not self.stopped:
            self._played_at_stop = self.samples_played()
            self.stopped = True

    # --- simulation controls and inspection -------------------------------------

    def stall_next_write(self, seconds: float) -> None:
        """Delay the next ``write`` — a refill thread that was not scheduled."""
        self._stall = seconds

    def samples_played(self) -> int:
        if self._started_at is None:
            return 0
        if self.stopped:
            return self._played_at_stop
        return min(self._elapsed_samples(), self.samples_written)

    def ao_played(self) -> np.ndarray:
        return self._concat(self._ao, len(self.config.ao), float)[:, : self.samples_played()]

    def do_played(self) -> np.ndarray:
        return self._concat(self._do, len(self.config.do), bool)[:, : self.samples_played()]

    # --- internals -------------------------------------------------------------

    def _elapsed_samples(self) -> int:
        return math.floor((self.clock.now() - self._started_at) * self.config.rate + _EPS)

    def _check(self) -> None:
        if self.underflow is not None:
            raise Underflow(self.underflow)
        if self._started_at is None or self.stopped or self._draining:
            return
        if self._elapsed_samples() > self.samples_written:
            self.underflow = self._started_at + self.samples_written / self.config.rate
            raise Underflow(self.underflow)

    async def _sleep_until(self, t: float) -> None:
        delay = t - self.clock.now()
        if delay > 0:
            await self.clock.sleep(delay)

    @staticmethod
    def _concat(chunks: list[np.ndarray], rows: int, dtype: type) -> np.ndarray:
        return np.concatenate(chunks, axis=1) if chunks else np.zeros((rows, 0), dtype=dtype)
