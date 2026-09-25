"""W1–W13 — WaveformSource, the AO timing source, over a simulated SignalOutput.

The simulated output records what it played, sample by sample, on a virtual
clock. A "gap" is an underflow: the output running dry before the next
waveform was committed.
"""

import asyncio

import numpy as np
import pytest

from stacscope.adapters.simulated import SimulatedSignalOutput, VirtualClock
from stacscope.core.actors import (
    Accepted,
    Actor,
    Priority,
    Rejected,
    Subscribe,
    ask,
    describe,
    spawn,
)
from stacscope.core.ports import Channel, OutputConfig, Underflow
from stacscope.core.protocol import (
    CountPulses,
    DeviceReady,
    Finish,
    Go,
    MainApplied,
    MainDiscarded,
    Phase,
    QueueMain,
    SetWaveform,
    SourceFinished,
    SourcePhase,
    StartDevice,
    StopDevice,
    Waveform,
)
from stacscope.core.timing import WaveformSource

RATE = 8.0  # samples/s, so 8-sample MAIN = one 1 s cycle; binary-exact times
CONFIG = OutputConfig(
    rate=RATE,
    ao=(Channel("Dev1/ao0", "etl"), Channel("Dev1/ao1", "scan1")),
    do=(
        Channel("Dev1/port0/line0", "camera_trigger"),
        Channel("Dev1/port0/line1", "laser_enable"),
        Channel("Dev1/port0/line2", "ueye_trigger"),  # no actor will ever claim it
    ),
    buffer_samples=12,  # 1.5 cycles of headroom
)


def waveform(n, value, ueye=False):
    """AO rows held at ``value``; DO rows a recognizable per-line pattern."""
    ao = np.full((2, n), float(value))
    do = np.zeros((3, n), dtype=bool)
    do[0, ::4] = True  # camera trigger
    do[1, :] = True  # laser on throughout
    if ueye:
        do[2, ::8] = True  # uEye: one pulse per MAIN cycle
    return ao, do


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)


class Boom(Exception):
    pass


class RefusingOutput(SimulatedSignalOutput):
    """Logs ``stop`` and ``close``, and fails whichever is named in
    ``refuse`` -- a driver that will not let go. Also records how much was
    still queued each time a write arrived once output had started: the
    margin the refill left itself."""

    def __init__(self, clock):
        super().__init__(clock)
        self.calls = []
        self.refuse = set()
        self.queued_at_write = []

    async def write(self, ao, do, *, label=None):
        if self._started_at is not None and not self._draining:
            self.queued_at_write.append(self.samples_written - self.samples_played())
        await super().write(ao, do, label=label)

    async def stop(self):
        self.calls.append("stop")
        await super().stop()
        if "stop" in self.refuse:
            raise Boom("the driver refused to stop")

    async def close(self):
        self.calls.append("close")
        await super().close()
        if "close" in self.refuse:
            raise Boom("the driver refused to close")


class Rig:
    def __init__(self, warmup=4, main=8, cooldown=4, buffer=None, queue_limit=None):
        self.clock = VirtualClock()
        self.output = RefusingOutput(self.clock)
        config = CONFIG if buffer is None else OutputConfig(CONFIG.rate, CONFIG.ao, CONFIG.do, buffer)
        limits = {} if queue_limit is None else {"queue_limit": queue_limit}
        self.source = WaveformSource(self.output, config, **limits)
        self.ref = spawn(self.source, clock=self.clock, name="source")
        self.listener = Listener()
        self.ref.tell(Subscribe(spawn(self.listener)))
        self.lengths = {"warmup": warmup, "main": main, "cooldown": cooldown}

    async def ask(self, msg, timeout=1.0):
        return await ask(self.ref, msg, timeout=timeout, clock=self.clock)

    async def load(self, main_value=1.0):
        for which, value in [(Waveform.WARMUP, 0.5), (Waveform.MAIN, main_value), (Waveform.COOLDOWN, 0.0)]:
            ao, do = waveform(self.lengths[which.value], value, ueye=which is Waveform.MAIN)
            assert await self.ask(SetWaveform(which, ao, do)) == Accepted()

    async def go(self, cycles):
        await self.load()
        assert await self.ask(StartDevice("run-01", cycles)) == DeviceReady()
        self.ref.tell(Go())
        await self.clock.advance(0)

    def played(self):
        """[(label, start, length)] of what the output played, in order."""
        timeline = self.output.timeline
        ends = [start for _, start in timeline[1:]] + [self.output.samples_written]
        return [(label, start, end - start) for (label, start), end in zip(timeline, ends)]


def assert_contiguous(played, lengths):
    position = 0
    for label, start, length in played:
        assert start == position, f"{label} at {start}, expected {position}: a gap"
        assert length == lengths[label]
        position += length


# --- W1 ------------------------------------------------------------------------


async def test_w1_warmup_main_cooldown_play_contiguously():
    rig = Rig()
    await rig.go(cycles=3)
    await rig.clock.advance(10.0)

    played = rig.played()
    assert [label for label, _, _ in played] == ["warmup", "main", "main", "main", "cooldown"]
    assert_contiguous(played, rig.lengths)
    assert rig.output.underflow is None
    assert rig.output.samples_played() == 4 + 3 * 8 + 4
    assert rig.listener.heard[-1] == SourceFinished()


async def test_w1_nothing_is_emitted_between_arm_and_go():
    rig = Rig()
    await rig.load()
    assert await rig.ask(StartDevice("run-01", 2)) == DeviceReady()

    await rig.clock.advance(5.0)  # the Coordinator waits on a slow participant
    assert rig.output.samples_played() == 0

    rig.ref.tell(Go())
    await rig.clock.advance(10.0)
    assert rig.output.underflow is None
    assert rig.listener.heard[-1] == SourceFinished()


async def test_w1_drain_waits_for_the_cooldown_to_actually_play():
    rig = Rig()
    await rig.go(cycles=3)
    await rig.clock.advance(3.75)  # 32 samples at 8/s end at t = 4.0

    assert rig.source.phase is Phase.COOLDOWN
    await rig.clock.advance(0.25)
    assert rig.source.phase is Phase.IDLE


# --- W2 ------------------------------------------------------------------------


async def test_w2_n_cycles_emit_exactly_n_main_waveforms():
    for cycles in (1, 2, 5):
        rig = Rig()
        await rig.go(cycles=cycles)
        await rig.clock.advance(20.0)

        labels = [label for label, _, _ in rig.played()]
        assert labels.count("main") == cycles


async def test_w2_negative_cycles_run_until_finish_then_cool_down_seamlessly():
    rig = Rig()
    await rig.go(cycles=-1)
    await rig.clock.advance(5.5)
    rig.ref.tell(Finish(), Priority.URGENT)
    await rig.clock.advance(10.0)

    played = rig.played()
    labels = [label for label, _, _ in played]
    assert labels[0] == "warmup" and labels[-1] == "cooldown"
    assert set(labels[1:-1]) == {"main"} and len(labels[1:-1]) >= 5
    assert_contiguous(played, rig.lengths)
    assert rig.output.underflow is None


# --- W3 ------------------------------------------------------------------------


async def test_w3_a_stalled_refill_surfaces_as_underflow_not_a_silent_gap():
    rig = Rig()
    await rig.go(cycles=-1)
    await rig.clock.advance(2.0)

    rig.output.stall_next_write(3.0)  # e.g. the process was not scheduled
    await rig.clock.advance(5.0)

    assert isinstance(rig.source.exit_error, Underflow)
    assert rig.output.stopped, "teardown must stop the output"


# --- W4 ------------------------------------------------------------------------


async def test_w4_configured_trigger_lines_are_emitted_though_no_actor_claims_them():
    rig = Rig()
    await rig.go(cycles=2)
    await rig.clock.advance(10.0)

    ueye = rig.output.do_played()[2]
    main_starts = [start for label, start, _ in rig.played() if label == "main"]
    assert [int(i) for i in np.flatnonzero(ueye)] == main_starts


# --- W5 ------------------------------------------------------------------------


async def test_w5_empty_warmup_and_cooldown_are_valid_no_op_phases():
    rig = Rig(warmup=0, cooldown=0)
    await rig.go(cycles=2)
    await rig.clock.advance(10.0)

    assert [label for label, _, _ in rig.played()] == ["main", "main"]
    assert rig.output.underflow is None
    phases = [m.phase for m in rig.listener.heard if isinstance(m, SourcePhase)]
    assert phases == [Phase.ARMED, Phase.WARMUP, Phase.RUN, Phase.COOLDOWN, Phase.IDLE]


async def test_w5_zero_cycles_with_empty_warmup_and_cooldown_never_starts_output():
    rig = Rig(warmup=0, cooldown=0)
    await rig.go(cycles=0)

    assert rig.output.samples_written == 0
    assert rig.source.phase is Phase.IDLE
    assert rig.listener.heard[-1] == SourceFinished()


# --- W6 ------------------------------------------------------------------------


async def test_w6_a_main_update_mid_run_lands_on_a_cycle_boundary():
    rig = Rig()
    await rig.go(cycles=-1)
    await rig.clock.advance(1.25)  # inside a cycle

    ao, do = waveform(8, 2.0, ueye=True)
    updating = asyncio.create_task(rig.ask(SetWaveform(Waveform.MAIN, ao, do), timeout=10.0))
    await rig.clock.advance(1.0)  # the source is mid-cycle until t = 2.0
    assert await updating == Accepted()
    await rig.clock.advance(2.0)
    rig.ref.tell(Finish(), Priority.URGENT)
    await rig.clock.advance(10.0)

    etl = rig.output.ao_played()[0]
    mains = [etl[s : s + n] for label, s, n in rig.played() if label == "main"]
    values = [set(np.unique(m)) for m in mains]
    assert all(len(v) == 1 for v in values), "a MAIN cycle mixed old and new samples"
    sequence = [v.pop() for v in values]
    switch = sequence.index(2.0)
    assert set(sequence[:switch]) == {1.0} and set(sequence[switch:]) == {2.0}


# --- W7-W8: backpressure ----------------------------------------------------------


@pytest.mark.parametrize(("main", "buffer"), [(8, 12), (20, 12)], ids=["main-fits", "main-longer"])
async def test_w7_the_refill_keeps_the_buffer_topped_up(main, buffer):
    """The hardware buffer is small on purpose: it is what holds the refill
    loop back, and it bounds how stale the queued output can get (W8). The
    price is a thin margin, so the loop must top it up in small steps as it
    plays, never let it run down and then refill in one go. Waiting for room
    for a whole waveform -- or, for a waveform longer than the buffer, for the
    buffer to empty -- spends the margin on every cycle, and a refill that is
    then late by a little is an underflow.

    The legacy engine got this from the driver: one blocking write per
    waveform, which DAQmx feeds in as room appears."""
    rig = Rig(main=main, buffer=buffer)
    await rig.go(cycles=4)
    await rig.clock.advance(30.0)

    assert rig.output.underflow is None
    assert rig.output.queued_at_write, "nothing was written after output started"
    assert min(rig.output.queued_at_write) >= buffer // 2


async def test_w8_an_update_plays_within_one_buffer_of_being_accepted():
    """Responsiveness. A MAIN update is taken at the next cycle boundary
    (W6), and then waits behind only what the hardware buffer already holds:
    nothing is queued in software ahead of it. So the buffer's depth is the
    whole of the latency, and a small buffer buys a responsive instrument --
    the stale-output bound the user relies on when retuning live."""
    rig = Rig()
    await rig.go(cycles=-1)
    await rig.clock.advance(1.25)

    accepted = []
    updating = asyncio.create_task(rig.ask(SetWaveform(Waveform.MAIN, *waveform(8, 2.0, ueye=True)), timeout=10.0))
    updating.add_done_callback(lambda _: accepted.append(rig.clock.now()))
    await rig.clock.advance(6.0)
    rig.ref.tell(Finish(), Priority.URGENT)
    await rig.clock.advance(10.0)

    first_new = int(np.flatnonzero(rig.output.ao_played()[0] == 2.0)[0])
    plays_at = first_new / RATE
    assert plays_at - accepted[0] <= CONFIG.buffer_samples / RATE


# --- W9-W13: a sequence of MAINs ----------------------------------------------------


def mains_played(rig):
    """The ETL value of each MAIN cycle that played, in order. Each cycle
    must be one value throughout -- a mixed cycle is a torn update."""
    etl = rig.output.ao_played()[0]
    cycles = [etl[s : s + n] for label, s, n in rig.played() if label == "main"]
    assert all(len(np.unique(c)) == 1 for c in cycles), "a MAIN cycle mixed two waveforms"
    return [float(c[0]) for c in cycles]


def applied(rig):
    return [m for m in rig.listener.heard if isinstance(m, MainApplied)]


def discarded(rig):
    return [m for m in rig.listener.heard if isinstance(m, MainDiscarded)]


async def test_w9_queued_mains_play_once_each_in_order_then_the_last_repeats():
    """The second-class use: a scan-parameter sweep, or a closed loop stepping
    a parameter. The legacy engine could only take the most recent MAIN, so a
    sweep had to be sent as one long waveform, known in full beforehand. A
    queued MAIN plays for exactly one cycle, in the order sent, and the last
    one stays -- the queue running dry holds the instrument at the final step
    rather than snapping back to an older waveform. Each plays exactly as
    sent: shape and values are the sender's business."""
    rig = Rig()
    await rig.load(main_value=1.0)
    for step in (2.0, 3.0, 4.0):
        assert await rig.ask(QueueMain(f"step{step}", *waveform(8, step, ueye=True))) == Accepted()
    assert await rig.ask(StartDevice("run-01", 6)) == DeviceReady()
    rig.ref.tell(Go())
    await rig.clock.advance(20.0)

    assert mains_played(rig) == [2.0, 3.0, 4.0, 4.0, 4.0, 4.0]
    assert rig.output.underflow is None


async def test_w9_a_queued_main_may_differ_in_length():
    """Nothing about a queued MAIN is imposed beyond the channels: its length
    and content are the generating code's responsibility, not the source's."""
    rig = Rig()
    await rig.load()
    assert await rig.ask(QueueMain("long", *waveform(12, 2.0))) == Accepted()
    assert await rig.ask(StartDevice("run-01", 2)) == DeviceReady()
    rig.ref.tell(Go())
    await rig.clock.advance(20.0)

    lengths = [n for label, _, n in rig.played() if label == "main"]
    assert lengths == [12, 12]
    assert rig.output.underflow is None


async def test_w10_set_waveform_during_a_sequence_clears_it_and_lands_next():
    """The first-class use keeps priority. A ``SetWaveform`` means "this, now":
    the user's adjustment must not wait behind a sweep, so it empties the
    queue and plays at the next cycle boundary, within one buffer (W8). What
    was dropped is reported by tag, so the sender knows those steps never
    played -- silently losing them would corrupt a sweep's record."""
    rig = Rig()
    await rig.load(main_value=1.0)
    for step in range(2, 8):
        assert await rig.ask(QueueMain(f"s{step}", *waveform(8, float(step), ueye=True))) == Accepted()
    assert await rig.ask(StartDevice("run-01", -1)) == DeviceReady()
    rig.ref.tell(Go())
    await rig.clock.advance(2.25)  # inside the second cycle

    accepted = []
    updating = asyncio.create_task(rig.ask(SetWaveform(Waveform.MAIN, *waveform(8, 9.0, ueye=True)), timeout=10.0))
    updating.add_done_callback(lambda _: accepted.append(rig.clock.now()))
    await rig.clock.advance(4.0)
    rig.ref.tell(Finish(), Priority.URGENT)
    await rig.clock.advance(10.0)

    played = mains_played(rig)
    switch = played.index(9.0)
    assert played[:switch] == [float(v) for v in range(2, 2 + switch)]
    assert set(played[switch:]) == {9.0}
    [dropped] = discarded(rig)
    assert dropped.tags == tuple(f"s{v}" for v in range(2 + switch, 8))
    first_new = int(np.flatnonzero(rig.output.ao_played()[0] == 9.0)[0])
    assert first_new / RATE - accepted[0] <= CONFIG.buffer_samples / RATE


async def test_w11_each_new_main_is_reported_with_its_cycle_and_first_sample():
    """What a closed loop, and the record of an acquisition, both need: which
    waveform was live on which volume. Published as the source takes it, with
    the cycle's index in this acquisition and the index of its first sample in
    the output, from the first sample of warmup -- which, over the rate, is
    when it plays. A ``SetWaveform`` change is reported too, untagged; a
    repeated MAIN is not reported again."""
    rig = Rig()
    await rig.load(main_value=1.0)
    assert await rig.ask(QueueMain("a", *waveform(8, 2.0, ueye=True))) == Accepted()
    assert await rig.ask(QueueMain("b", *waveform(8, 3.0, ueye=True))) == Accepted()
    assert await rig.ask(StartDevice("run-01", 4)) == DeviceReady()
    rig.ref.tell(Go())
    await rig.clock.advance(1.5)
    updating = asyncio.create_task(rig.ask(SetWaveform(Waveform.MAIN, *waveform(8, 5.0, ueye=True)), timeout=10.0))
    await rig.clock.advance(20.0)
    assert await updating == Accepted()

    starts = [s for label, s, _ in rig.played() if label == "main"]
    values = mains_played(rig)
    reports = applied(rig)
    assert [m.tag for m in reports] == ["a", "b", None]
    assert [m.cycle for m in reports] == sorted({m.cycle for m in reports})
    for m in reports:
        assert m.sample == starts[m.cycle]
        assert values[m.cycle] == {"a": 2.0, "b": 3.0, None: 5.0}[m.tag]
    assert values[reports[-1].cycle :] == [5.0] * (4 - reports[-1].cycle)


async def test_w11_the_first_cycle_is_reported_even_without_a_change():
    """Every acquisition's record starts from a known waveform, so cycle 0 is
    always reported, whatever was playing in the last acquisition."""
    rig = Rig()
    await rig.go(cycles=2)
    await rig.clock.advance(20.0)
    assert await rig.ask(StartDevice("run-02", 1)) == DeviceReady()
    rig.ref.tell(Go())
    await rig.clock.advance(20.0)

    assert [(m.tag, m.cycle, m.sample) for m in applied(rig)] == [(None, 0, 4), (None, 0, 4)]


async def test_w12_the_queue_is_bounded_and_a_slot_frees_as_each_plays():
    """Flow control. An unbounded queue is the stale-output problem again,
    and unbounded memory: a 300 ms MAIN on the instrument is about 1 MB. Past
    the limit a ``QueueMain`` is refused, not held; a slot frees as each queued
    MAIN is taken, and ``MainApplied`` is the sender's cue to send the next."""
    rig = Rig(queue_limit=2)
    await rig.load()
    assert await rig.ask(QueueMain("a", *waveform(8, 2.0))) == Accepted()
    assert await rig.ask(QueueMain("b", *waveform(8, 3.0))) == Accepted()
    assert isinstance(await rig.ask(QueueMain("c", *waveform(8, 4.0))), Rejected)

    assert await rig.ask(StartDevice("run-01", -1)) == DeviceReady()
    rig.ref.tell(Go())
    await rig.clock.advance(0.5)  # "a" has been taken
    queueing = asyncio.create_task(rig.ask(QueueMain("c", *waveform(8, 4.0)), timeout=10.0))
    await rig.clock.advance(5.0)
    assert await queueing == Accepted()
    rig.ref.tell(Finish(), Priority.URGENT)
    await rig.clock.advance(10.0)

    assert mains_played(rig)[:3] == [2.0, 3.0, 4.0]


async def test_w13_what_is_still_queued_when_an_acquisition_ends_is_dropped_and_reported():
    """Steps meant for one acquisition must not play at the start of the
    next, maybe minutes later: that is exactly the stale waveform the design
    exists to prevent. They are dropped when cooldown is committed, and
    reported by tag."""
    rig = Rig()
    await rig.load(main_value=1.0)
    for tag, value in (("a", 2.0), ("b", 3.0), ("c", 4.0)):
        assert await rig.ask(QueueMain(tag, *waveform(8, value, ueye=True))) == Accepted()
    assert await rig.ask(StartDevice("run-01", 1)) == DeviceReady()
    rig.ref.tell(Go())
    await rig.clock.advance(20.0)
    assert mains_played(rig) == [2.0]
    assert [d.tags for d in discarded(rig)] == [("b", "c")]

    assert await rig.ask(StartDevice("run-02", 1)) == DeviceReady()
    rig.ref.tell(Go())
    await rig.clock.advance(20.0)
    assert mains_played(rig) == [2.0]  # the last MAIN taken, not a leftover step


async def test_w13_an_armed_acquisition_that_is_stopped_drops_the_queue_too():
    rig = Rig()
    await rig.load()
    assert await rig.ask(QueueMain("a", *waveform(8, 2.0))) == Accepted()
    assert await rig.ask(StartDevice("run-01", 1)) == DeviceReady()

    await rig.ask(StopDevice())

    assert [d.tags for d in discarded(rig)] == [("a",)]


async def test_a_queued_main_is_checked_like_a_set_one():
    rig = Rig()
    ao, do = waveform(8, 1.0)

    assert isinstance(await rig.ask(QueueMain("x", ao[:1], do)), Rejected)
    assert isinstance(await rig.ask(QueueMain("x", ao[:, :0], do[:, :0])), Rejected)


async def test_a_queued_main_alone_is_enough_to_arm():
    rig = Rig()
    assert await rig.ask(QueueMain("x", *waveform(8, 2.0))) == Accepted()

    assert await rig.ask(StartDevice("run-01", 1)) == DeviceReady()


# --- validation --------------------------------------------------------------------


async def test_waveforms_must_match_the_configured_channels():
    rig = Rig()
    ao, do = waveform(8, 1.0)

    assert isinstance(await rig.ask(SetWaveform(Waveform.MAIN, ao[:1], do)), Rejected)
    assert isinstance(await rig.ask(SetWaveform(Waveform.MAIN, ao, do[:, :7])), Rejected)
    assert isinstance(await rig.ask(SetWaveform(Waveform.MAIN, *waveform(0, 1.0))), Rejected)


async def test_arming_without_a_main_waveform_is_rejected():
    rig = Rig()

    assert isinstance(await rig.ask(StartDevice("run-01", 1)), Rejected)
    assert rig.source.phase is Phase.IDLE


async def test_a_waveform_longer_than_the_buffer_is_written_in_chunks():
    rig = Rig(main=20, buffer=12)
    await rig.go(cycles=2)
    await rig.clock.advance(20.0)

    assert [label for label, _, _ in rig.played()] == ["warmup", "main", "main", "cooldown"]
    assert_contiguous(rig.played(), rig.lengths)
    assert rig.output.underflow is None


# --- N11-N12: teardown releases the output --------------------------------------


async def ended(rig):
    """Stop the source from outside, as its supervisor would, and let it end."""
    rig.source.stop()
    await rig.clock.advance(0)
    assert rig.source.stopped.is_set()


async def test_n11_teardown_stops_then_closes_the_output():
    """Invariant 3: teardown gives the hardware back. Stop comes first, because
    closing a task that is still generating is not guaranteed to leave the
    lines where a stop would."""
    rig = Rig()
    await rig.go(cycles=1)
    await rig.clock.advance(5.0)

    await ended(rig)

    assert rig.output.calls[-2:] == ["stop", "close"]
    assert rig.output.closed
    assert rig.source.exit_error is None


async def test_n11_an_acquisition_that_died_still_closes_the_output():
    """The case the invariant exists for: an underflow kills the source, and
    the board must still be released rather than held until the process
    exits."""
    rig = Rig()
    await rig.go(cycles=-1)
    await rig.clock.advance(2.0)
    rig.output.stall_next_write(3.0)
    await rig.clock.advance(5.0)

    assert isinstance(rig.source.exit_error, Underflow)
    assert rig.output.closed


async def test_n11_the_output_is_closed_even_when_stop_fails():
    """A driver that will not stop is exactly when the release matters most.
    Giving up at the first refusal would leave the board reserved."""
    rig = Rig()
    rig.output.refuse.add("stop")

    await ended(rig)

    assert rig.output.closed


async def test_n12_a_failed_close_is_reported_not_raised_over_the_cause():
    """Invariant 6. The underflow is why the acquisition ended and must stay
    the reported cause; the close that failed afterwards is reported beside
    it, not in place of it and not instead of nothing."""
    rig = Rig()
    rig.output.refuse.add("close")
    await rig.go(cycles=-1)
    await rig.clock.advance(2.0)
    rig.output.stall_next_write(3.0)
    await rig.clock.advance(5.0)

    assert isinstance(rig.source.exit_error, Underflow)
    assert "refused to close" in describe(rig.source.exit_error)


async def test_n12_when_stop_and_close_both_fail_neither_is_lost():
    """Two refusals, one report. Raising the second out of a ``finally`` would
    hide the first behind it -- and a stop that failed is the one that says
    the lines may still be asserted."""
    rig = Rig()
    rig.output.refuse.update({"stop", "close"})

    await ended(rig)

    report = describe(rig.source.exit_error)
    assert "refused to stop" in report
    assert "refused to close" in report


# --- W14: each line's pulses over an acquisition ----------------------------------------


def _counts(reply):
    return dict(zip(reply.lines, reply.counts, strict=True))


async def test_w14_each_lines_pulses_are_counted_over_the_whole_acquisition():
    """A device on a trigger line learns from this how many frames an
    acquisition will give it (user, 2026-09-25). A pulse is a rising edge,
    from the line's low before output: WARMUP once, MAIN once per cycle,
    COOLDOWN once. A line held high across the joins is one pulse."""
    rig = Rig()
    await rig.load()

    reply = await rig.ask(CountPulses(3))

    # camera_trigger: a pulse every 4 samples; laser_enable: high throughout;
    # ueye_trigger: one pulse per MAIN.
    assert _counts(reply) == {"camera_trigger": 1 + 3 * 2 + 1, "laser_enable": 1, "ueye_trigger": 3}


async def test_w14_a_pulse_across_the_join_between_cycles_is_counted_once():
    rig = Rig()
    await rig.load()
    ao, do = waveform(8, 1.0)
    do[0] = [True, False, False, False, False, False, False, True]  # high from sample 7 to the next 0
    assert await rig.ask(SetWaveform(Waveform.MAIN, ao, do)) == Accepted()

    reply = await rig.ask(CountPulses(3))

    # WARMUP's pulse; MAIN's first sample rises once, and each sample 7
    # rises and runs on into the next cycle's sample 0; the last one runs on
    # into COOLDOWN's first sample, so COOLDOWN adds none.
    assert _counts(reply)["camera_trigger"] == 1 + 1 + 3 + 0


async def test_w14_queued_mains_are_counted_in_order_then_the_last_one_repeats():
    """As they will play: each queued MAIN for one cycle, then the last one
    taken for every cycle after; MAINs queued beyond the acquisition's
    cycles never play."""
    rig = Rig()
    await rig.load()
    for tag, pulses in [("a", 1), ("b", 4), ("c", 3)]:
        ao, do = waveform(8, 1.0)
        do[0] = False
        do[0, : 2 * pulses : 2] = True
        assert await rig.ask(QueueMain(tag, ao, do)) == Accepted()

    assert _counts(await rig.ask(CountPulses(5)))["camera_trigger"] == 1 + (1 + 4 + 3 + 3 + 3) + 1
    assert _counts(await rig.ask(CountPulses(1)))["camera_trigger"] == 1 + 1 + 1


async def test_w14_an_acquisition_until_stopped_or_without_a_main_has_no_count():
    rig = Rig()

    assert set(_counts(await rig.ask(CountPulses(3))).values()) == {-1}, "no MAIN yet"
    await rig.load()
    assert set(_counts(await rig.ask(CountPulses(-1))).values()) == {-1}
