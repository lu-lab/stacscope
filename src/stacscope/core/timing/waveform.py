"""WaveformSource: a timing source that clocks buffered AO/DO waveforms.

The NI PCI-6733 today, via a ``SignalOutput`` adapter. This is the one
designated seam between the control flow (messages in) and the timing flow
(samples out); it knows nothing of what the waveforms mean.
"""

from collections import deque

import numpy as np

from stacscope.core.actors import Accepted
from stacscope.core.ports import OutputConfig, SignalOutput
from stacscope.core.protocol import (
    CountPulses,
    MainApplied,
    MainDiscarded,
    PulseCounts,
    QueueMain,
    SetWaveform,
    Waveform,
)
from stacscope.core.timing.source import NotReady, TimingSource

type Samples = tuple[np.ndarray, np.ndarray]  # (ao, do)

REFILL_STEPS = 4
"""Once output is playing, a refill waits for room for a quarter of the
hardware buffer (or the rest of the waveform, if less), so the buffer never
falls below three quarters full at a refill."""

QUEUE_LIMIT = 8
"""How many ``QueueMain`` waveforms may wait at once, by default."""


class WaveformSource(TimingSource):
    """Holds the WARMUP, MAIN and COOLDOWN waveforms and keeps the output fed.

    - Warmup commits WARMUP; each cycle commits one copy of MAIN, captured at
      the start of the cycle, so an update never tears one; Cooldown commits
      COOLDOWN directly behind the last MAIN — no gap.
    - Output starts once the pre-roll is queued (WARMUP plus the first MAIN),
      or earlier if the buffer fills first. It never starts with nothing
      queued, so empty WARMUP/COOLDOWN are valid no-op phases.
    - Waveforms longer than the hardware buffer are written in chunks.
    - Once output is playing, the buffer is topped up a quarter at a time,
      so it never falls below three quarters full at a refill. Nothing is
      queued beyond it, so its depth (``OutputConfig.buffer_samples``) is
      the most an update to MAIN waits behind once it is taken.
    - ``QueueMain`` queues a MAIN for exactly one cycle, up to
      ``queue_limit`` at a time; the last one taken keeps playing. A
      ``SetWaveform`` of MAIN empties the queue, and so does the end of an
      acquisition. Each MAIN taken that differs from the last is published as
      ``MainApplied``, and every dropped one in a ``MainDiscarded``.
    - ``CountPulses(cycles)`` is answered with each DO line's rising edges
      over an acquisition of that many cycles, as the waveforms stand:
      WARMUP, each queued MAIN for a cycle and then the last one taken,
      COOLDOWN. -1 for every line if the acquisition runs until stopped or
      no MAIN is set.
    - An ``Underflow`` is not caught: the source dies, and the Coordinator
      treats it as losing the source.
    """

    def __init__(self, output: SignalOutput, config: OutputConfig, *, queue_limit: int = QUEUE_LIMIT) -> None:
        super().__init__()
        self.output = output
        self.config = config
        self.queue_limit = queue_limit
        empty = (np.zeros((len(config.ao), 0)), np.zeros((len(config.do), 0), dtype=bool))
        self._waveforms: dict[Waveform, Samples | None] = {
            Waveform.WARMUP: empty,
            Waveform.MAIN: None,
            Waveform.COOLDOWN: empty,
        }
        self._started = False
        self._queued = 0
        # The queue holds only what was asked for by QueueMain; the steady MAIN
        # stays in _waveforms. Each MAIN gets a new version when it is set or
        # taken, so a change is told from a repeat by version, never by
        # comparing arrays.
        self._pending: deque[tuple[str, Samples]] = deque()
        self._main_tag: str | None = None
        self._main_version = 0
        self._reported_version: int | None = None
        self._cycle = 0

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
            case SetWaveform(waveform, ao, do):
                problem = self._validate(waveform, ao, do)
                if problem is not None:
                    self._reject(problem)
                    return
                self._waveforms[waveform] = (np.asarray(ao, dtype=float), np.asarray(do, dtype=bool))
                if waveform is Waveform.MAIN:
                    # "This, now": an adjustment never waits behind a sequence.
                    self._main_tag = None
                    self._main_version += 1
                    self._discard("replaced by SetWaveform")
                self.reply(Accepted())
            case QueueMain(tag, ao, do):
                problem = self._validate(Waveform.MAIN, ao, do)
                if problem is None and len(self._pending) >= self.queue_limit:
                    problem = f"the queue is full ({self.queue_limit} waiting)"
                if problem is not None:
                    self._reject(problem)
                    return
                self._pending.append((tag, (np.asarray(ao, dtype=float), np.asarray(do, dtype=bool))))
                self.reply(Accepted())
            case CountPulses(cycles):
                self.reply(PulseCounts(tuple(line.label for line in self.config.do), self._pulses(cycles)))
            case _:
                await super().handle(msg)

    # --- phase hooks ----------------------------------------------------------

    async def arm(self, root_filename: str, cycles: int) -> None:
        if self._waveforms[Waveform.MAIN] is None and not self._pending:
            raise NotReady("no MAIN waveform has been set")
        await self.output.arm()
        self._started = False
        self._queued = 0
        self._cycle = 0
        self._reported_version = None  # every acquisition's record starts with what plays first

    async def disarm(self) -> None:
        self._discard("the acquisition was stopped before it started")
        await self.output.stop()

    async def warmup(self) -> None:
        await self._commit("warmup", self._waveforms[Waveform.WARMUP])

    async def run_cycle(self) -> None:
        if self._pending:
            self._main_tag, self._waveforms[Waveform.MAIN] = self._pending.popleft()
            self._main_version += 1
        if self._main_version != self._reported_version:
            self.publish(MainApplied(self._main_tag, self._cycle, self._queued))
            self._reported_version = self._main_version
        await self._commit("main", self._waveforms[Waveform.MAIN])
        self._cycle += 1
        await self._start_if_queued()

    async def cooldown(self) -> None:
        self._discard("the acquisition ended")
        await self._commit("cooldown", self._waveforms[Waveform.COOLDOWN])
        await self._start_if_queued()

    async def drain(self) -> None:
        if self._started:
            await self.output.drain()
        await self.output.stop()

    # --- internals ---------------------------------------------------------------

    async def _commit(self, label: str, samples: Samples) -> None:
        ao, do = samples  # captured once: a SetWaveform cannot change it mid-write
        n = ao.shape[1]
        # The hardware buffer is kept small on purpose: waiting for room in it
        # is the backpressure that paces this loop, and its depth is all that
        # can queue ahead of a MAIN update. So the margin is thin, and is
        # topped up a step at a time as it plays. Waiting for room for the
        # rest of the waveform instead -- for a waveform longer than the buffer,
        # for the buffer to empty -- spends the margin on every cycle.
        step = max(1, self.config.buffer_samples // REFILL_STEPS)
        written = 0
        while written < n:
            space = self.output.space_available()
            if not self._started:
                if space == 0:
                    await self._go()  # pre-roll already fills the buffer
                    continue
            elif space < min(n - written, step):
                await self.output.wait_for_space(min(n - written, step))
                continue
            k = min(space, n - written)
            chunk = slice(written, written + k)
            await self.output.write(ao[:, chunk], do[:, chunk], label=label if written == 0 else None)
            written += k
            self._queued += k

    def _pulses(self, cycles: int) -> tuple[int, ...]:
        """Each DO line's rising edges over an acquisition of ``cycles``."""
        main = self._waveforms[Waveform.MAIN]
        if cycles < 0 or (main is None and not self._pending):
            return (-1,) * len(self.config.do)
        # What run_cycle will take: the queue in order, one cycle each, then
        # the last MAIN taken for every cycle left.
        queued = [samples for _, samples in self._pending][:cycles]
        steady = queued[-1] if queued else main
        segments = [(self._waveforms[Waveform.WARMUP][1], 1)]
        segments += [(do, 1) for _, do in queued]
        segments += [(steady[1], cycles - len(queued)), (self._waveforms[Waveform.COOLDOWN][1], 1)]
        counts = np.zeros(len(self.config.do), dtype=np.int64)
        last = np.zeros(len(self.config.do), dtype=bool)  # every line is low before output
        for do, times in segments:
            if times == 0 or do.shape[1] == 0:
                continue
            within = np.count_nonzero(do[:, 1:] & ~do[:, :-1], axis=1)
            # The first copy rises from whatever came before it; each repeat
            # rises from the copy before it.
            counts += within * times + (do[:, 0] & ~last) + (do[:, 0] & ~do[:, -1]) * (times - 1)
            last = do[:, -1]
        return tuple(int(n) for n in counts)

    def _discard(self, reason: str) -> None:
        if self._pending:
            self.publish(MainDiscarded(tuple(tag for tag, _ in self._pending), reason))
            self._pending.clear()

    async def _start_if_queued(self) -> None:
        if not self._started and self._queued:
            await self._go()

    async def _go(self) -> None:
        await self.output.go()
        self._started = True

    def _validate(self, waveform: Waveform, ao: np.ndarray, do: np.ndarray) -> str | None:
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
        if waveform is Waveform.MAIN and ao.shape[1] == 0:
            return "MAIN must not be empty: it defines the cycle"
        return None
