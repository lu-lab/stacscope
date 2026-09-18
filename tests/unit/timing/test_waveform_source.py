"""W1–W6 — WaveformSource, the AO timing source, over a simulated SignalOutput.

The simulated output records what it played, sample by sample, on a virtual
clock. A "gap" is an underflow: the output running dry before the next
segment was committed.
"""

import asyncio

import numpy as np

from stacscope.adapters.simulated import SimulatedSignalOutput, VirtualClock
from stacscope.core.actors import Accepted, Actor, Priority, Rejected, Subscribe, ask, spawn
from stacscope.core.ports import Channel, OutputConfig, Underflow
from stacscope.core.protocol import (
    DeviceReady,
    Finish,
    Go,
    Phase,
    Segment,
    SetSegment,
    SourceFinished,
    SourcePhase,
    StartDevice,
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


class Rig:
    def __init__(self, warmup=4, main=8, cooldown=4, buffer=None):
        self.clock = VirtualClock()
        self.output = SimulatedSignalOutput(self.clock)
        config = CONFIG if buffer is None else OutputConfig(CONFIG.rate, CONFIG.ao, CONFIG.do, buffer)
        self.source = WaveformSource(self.output, config)
        self.ref = spawn(self.source, clock=self.clock, name="source")
        self.listener = Listener()
        self.ref.tell(Subscribe(spawn(self.listener)))
        self.lengths = {"warmup": warmup, "main": main, "cooldown": cooldown}

    async def ask(self, msg, timeout=1.0):
        return await ask(self.ref, msg, timeout=timeout, clock=self.clock)

    async def load(self, main_value=1.0):
        for segment, value in [(Segment.WARMUP, 0.5), (Segment.MAIN, main_value), (Segment.COOLDOWN, 0.0)]:
            ao, do = waveform(self.lengths[segment.value], value, ueye=segment is Segment.MAIN)
            assert await self.ask(SetSegment(segment, ao, do)) == Accepted()

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


async def test_w2_n_cycles_emit_exactly_n_main_segments():
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
    updating = asyncio.create_task(rig.ask(SetSegment(Segment.MAIN, ao, do), timeout=10.0))
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


# --- validation --------------------------------------------------------------------


async def test_segments_must_match_the_configured_channels():
    rig = Rig()
    ao, do = waveform(8, 1.0)

    assert isinstance(await rig.ask(SetSegment(Segment.MAIN, ao[:1], do)), Rejected)
    assert isinstance(await rig.ask(SetSegment(Segment.MAIN, ao, do[:, :7])), Rejected)
    assert isinstance(await rig.ask(SetSegment(Segment.MAIN, *waveform(0, 1.0))), Rejected)


async def test_arming_without_a_main_segment_is_rejected():
    rig = Rig()

    assert isinstance(await rig.ask(StartDevice("run-01", 1)), Rejected)
    assert rig.source.phase is Phase.IDLE


async def test_a_segment_longer_than_the_buffer_is_written_in_chunks():
    rig = Rig(main=20, buffer=12)
    await rig.go(cycles=2)
    await rig.clock.advance(20.0)

    assert [label for label, _, _ in rig.played()] == ["warmup", "main", "main", "cooldown"]
    assert_contiguous(rig.played(), rig.lengths)
    assert rig.output.underflow is None
