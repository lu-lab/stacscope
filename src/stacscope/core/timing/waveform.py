"""WaveformSource: a timing source that clocks buffered AO/DO waveforms.

The NI PCI-6733 today, via a ``SignalOutput`` adapter. This is the one
designated seam between the control flow (messages in) and the timing flow
(samples out); it knows nothing of what the waveforms mean.
"""

import numpy as np

from stacscope.core.actors import Accepted
from stacscope.core.ports import OutputConfig, SignalOutput
from stacscope.core.protocol import Segment, SetSegment
from stacscope.core.timing.source import NotReady, TimingSource

type Waveform = tuple[np.ndarray, np.ndarray]  # (ao, do)


class WaveformSource(TimingSource):
    """Holds the WARMUP, MAIN and COOLDOWN segments and keeps the output fed.

    - Warmup commits WARMUP; each cycle commits one copy of MAIN, captured at
      the start of the cycle, so an update never tears one; Cooldown commits
      COOLDOWN directly behind the last MAIN — no gap.
    - Output starts once the pre-roll is queued (WARMUP plus the first MAIN),
      or earlier if the buffer fills first. It never starts with nothing
      queued, so empty WARMUP/COOLDOWN are valid no-op phases.
    - Segments longer than the hardware buffer are written in chunks.
    - An ``Underflow`` is not caught: the source dies, and the Coordinator
      treats it as losing the source.
    """

    def __init__(self, output: SignalOutput, config: OutputConfig) -> None:
        super().__init__()
        self.output = output
        self.config = config
        empty = (np.zeros((len(config.ao), 0)), np.zeros((len(config.do), 0), dtype=bool))
        self._segments: dict[Segment, Waveform | None] = {
            Segment.WARMUP: empty,
            Segment.MAIN: None,
            Segment.COOLDOWN: empty,
        }
        self._started = False
        self._queued = 0

    async def setup(self) -> None:
        await self.output.configure(self.config)

    async def teardown(self) -> None:
        # Close is attempted whatever stop did: a driver that will not stop is
        # when holding the board matters most. Failures are gathered rather
        # than raised in turn, because raising close's out of a `finally` would
        # hide stop's -- and a failed stop is the one saying lines may still be
        # asserted. The runtime reports what this raises beside whatever ended
        # the acquisition, never in place of it.
        failures = []
        for release in (self.output.stop, self.output.close):
            try:
                await release()
            except Exception as exc:  # noqa: PERF203 - each step gets its own attempt
                failures.append(exc)
        if failures:
            raise ExceptionGroup("the output was not released cleanly", failures)

    async def handle(self, msg: object) -> None:
        match msg:
            case SetSegment(segment, ao, do):
                problem = self._validate(segment, ao, do)
                if problem is not None:
                    self._reject(problem)
                    return
                self._segments[segment] = (np.asarray(ao, dtype=float), np.asarray(do, dtype=bool))
                self.reply(Accepted())
            case _:
                await super().handle(msg)

    # --- phase hooks ----------------------------------------------------------

    async def arm(self, root_filename: str, cycles: int) -> None:
        if self._segments[Segment.MAIN] is None:
            raise NotReady("no MAIN segment has been set")
        await self.output.arm()
        self._started = False
        self._queued = 0

    async def disarm(self) -> None:
        await self.output.stop()

    async def warmup(self) -> None:
        await self._commit("warmup", self._segments[Segment.WARMUP])

    async def run_cycle(self) -> None:
        await self._commit("main", self._segments[Segment.MAIN])
        await self._start_if_queued()

    async def cooldown(self) -> None:
        await self._commit("cooldown", self._segments[Segment.COOLDOWN])
        await self._start_if_queued()

    async def drain(self) -> None:
        if self._started:
            await self.output.drain()
        await self.output.stop()

    # --- internals ---------------------------------------------------------------

    async def _commit(self, label: str, waveform: Waveform) -> None:
        ao, do = waveform  # captured once: a SetSegment cannot change it mid-write
        n = ao.shape[1]
        written = 0
        while written < n:
            space = self.output.space_available()
            if space == 0:
                if not self._started:
                    await self._go()  # pre-roll already fills the buffer
                await self.output.wait_for_space(min(n - written, self.config.buffer_samples))
                continue
            k = min(space, n - written)
            chunk = slice(written, written + k)
            await self.output.write(ao[:, chunk], do[:, chunk], label=label if written == 0 else None)
            written += k
            self._queued += k

    async def _start_if_queued(self) -> None:
        if not self._started and self._queued:
            await self._go()

    async def _go(self) -> None:
        await self.output.go()
        self._started = True

    def _validate(self, segment: Segment, ao: np.ndarray, do: np.ndarray) -> str | None:
        n_ao, n_do = len(self.config.ao), len(self.config.do)
        if ao.ndim != 2 or ao.shape[0] != n_ao:
            return f"AO must be {n_ao} channels × samples, got shape {ao.shape}"
        if do.ndim != 2 or do.shape[0] != n_do:
            return f"DO must be {n_do} lines × samples, got shape {do.shape}"
        if ao.shape[1] != do.shape[1]:
            return f"AO has {ao.shape[1]} samples but DO has {do.shape[1]}"
        if not np.issubdtype(ao.dtype, np.number) or np.iscomplexobj(ao):
            return f"AO must be real-valued, got {ao.dtype}"
        if do.dtype != bool and not (np.issubdtype(do.dtype, np.integer) and np.isin(do, (0, 1)).all()):
            return f"DO must be boolean or 0/1, got {do.dtype}"
        if segment is Segment.MAIN and ao.shape[1] == 0:
            return "MAIN must not be empty: it defines the cycle"
        return None
