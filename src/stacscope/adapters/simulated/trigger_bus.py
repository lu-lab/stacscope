"""TriggerBus: the wire, in simulation.

Real hardware has no object here. A DO line goes high, a camera's trigger input
sees it, and a frame happens — there is nothing in between to model. In
simulation something has to stand in for the wire, and this is it: the signal
output hands it every DO sample it is given, and it turns rising edges into
callbacks at the virtual time those samples play.

That is what makes an end-to-end test mean something. A frame count that comes
from a fake told how many frames to make proves nothing about the timing; a
frame count caused by the pulses the timing source actually emitted proves the
two flows line up.

Devices subscribe **by line label**, never by physical channel, so the wiring
can change in config without touching a device. A line nobody subscribes to
still plays — the uEye is triggered by the DAQ and controlled entirely by
µManager — and a line two devices subscribe to reaches both.
"""

import asyncio
import heapq
from collections.abc import Callable

import numpy as np

from stacscope.core.ports import Clock, OutputConfig


class TriggerBus:
    """Rising edges on the output's DO lines, delivered as they play.

    Edges are worked out when a waveform is written, which is before it plays,
    and become callbacks only once output has started — the pre-roll is written
    while the board is still idle. A stop drops whatever has not played yet.

    A callback runs outside any actor's mailbox, which is deliberate: a real
    camera DMAs a frame in whenever its trigger fires, with no actor involved.
    """

    def __init__(self, clock: Clock) -> None:
        self.clock = clock
        self._subscribers: dict[str, list[Callable[[], None]]] = {}
        self._pending: list[tuple[float, int, str]] = []
        """A heap of (time, sequence, label): the edges not yet delivered."""
        self._edges: list[tuple[int, str]] = []
        """(sample index, label) worked out at write time, before output starts."""
        self._last: dict[str, bool] = {}
        self._labels: tuple[str, ...] = ()
        self._rate = 0.0
        self._started_at: float | None = None
        self._seq = 0
        self._arrived = asyncio.Event()
        self._pump: asyncio.Task | None = None

    def subscribe(self, label: str, callback: Callable[[], None]) -> None:
        """Watch a line by its configured label. Several devices may watch one
        line, and a line with no watchers is emitted just the same."""
        self._subscribers.setdefault(label, []).append(callback)

    def unsubscribe(self, label: str, callback: Callable[[], None]) -> None:
        watchers = self._subscribers.get(label, [])
        if callback in watchers:
            watchers.remove(callback)

    # --- what the signal output tells it ------------------------------------

    def configured(self, config: OutputConfig) -> None:
        self._labels = tuple(channel.label for channel in config.do)
        self._rate = config.rate
        self.reset()

    def reset(self) -> None:
        """Forget every line's state and every edge not yet delivered. The
        output calls this when it is armed as well as when it is configured:
        a second acquisition starts from lines low, not from wherever the last
        one left them."""
        self._last = dict.fromkeys(self._labels, False)
        self._edges.clear()
        self._pending.clear()

    def wrote(self, do: np.ndarray, first_sample: int) -> None:
        """Find the rising edges in a chunk of DO samples about to be queued."""
        for row, label in enumerate(self._labels):
            line = do[row]
            previous = np.concatenate(([self._last[label]], line[:-1]))
            for offset in np.flatnonzero(line & ~previous):
                self._edges.append((first_sample + int(offset), label))
            if len(line):
                self._last[label] = bool(line[-1])
        self._schedule()

    def started(self, at: float) -> None:
        self._started_at = at
        self._schedule()

    def stopped(self) -> None:
        """Output has ceased: an edge that never played never fires."""
        self._started_at = None
        self.reset()
        if self._pump is not None:
            self._pump.cancel()
            self._pump = None

    # --- delivery ------------------------------------------------------------

    def _schedule(self) -> None:
        if self._started_at is None:
            return  # written during the pre-roll; it will be scheduled at go()
        for sample, label in self._edges:
            heapq.heappush(
                self._pending, (self._started_at + sample / self._rate, self._seq, label)
            )
            self._seq += 1
        self._edges.clear()
        self._arrived.set()
        if self._pump is None:
            self._pump = asyncio.create_task(self._deliver())

    async def _deliver(self) -> None:
        while True:
            if not self._pending:
                self._arrived.clear()
                await self._arrived.wait()
                continue
            due = self._pending[0][0]
            delay = due - self.clock.now()
            if delay > 0:
                await self.clock.sleep(delay)
                continue  # an earlier edge may have been queued while asleep
            _, _, label = heapq.heappop(self._pending)
            for callback in self._subscribers.get(label, ()):
                callback()
