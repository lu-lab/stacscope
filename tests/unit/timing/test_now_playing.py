"""CM41–CM47 -- every ``SourcePhase`` means "now playing".

A buffered source commits each phase ahead of when it plays: WARMUP is
written before the clock starts, RUN as soon as WARMUP is queued, COOLDOWN a
buffer's depth before its first sample. Its own phase still changes when the
samples are queued -- that is what keeps the MAIN → COOLDOWN handoff gapless
-- but the announcement waits until the phase's first sample has played:
WARMUP when the clock starts, RUN at the first MAIN sample, COOLDOWN at its
first sample, IDLE once output has drained (user, 2026-10-02). A
software-timed device that starts at RUN then starts with the first image,
not a WARMUP and a buffer early.

Each test notes, at the moment the source reports a phase, whether its
output had started and how many samples had played -- at the report, not
when a listener gets round to it, which in virtual time can be after the
clock has started. The source's check lands on the sample exactly.
"""

from test_waveform_source import Rig

from stacscope.adapters.simulated import SimulatedTimingSource, VirtualClock
from stacscope.core.actors import Actor, Priority, Subscribe, spawn
from stacscope.core.protocol import DeviceReady, Finish, Go, Phase, SourcePhase, StartDevice


class Watch:
    """Wraps the source's ``report``: notes what had played as each phase
    was reported."""

    def __init__(self, source, output):
        self.output = output
        self.heard = []
        report = source.report

        def noting(msg, *args, **kwargs):
            if isinstance(msg, SourcePhase):
                started = output._started_at is not None
                self.heard.append((msg.phase, started, output.samples_played()))
            report(msg, *args, **kwargs)

        source.report = noting

    def phases(self):
        return [phase for phase, _, _ in self.heard]

    def at(self, phase):
        """(whether output had started, samples played) when ``phase`` was heard."""
        [(started, played)] = [(s, p) for ph, s, p in self.heard if ph is phase]
        return started, played


def watched(**lengths):
    rig = Rig(**lengths)
    return rig, Watch(rig.source, rig.output)


async def test_cm41_warmup_is_announced_when_the_clock_starts():
    """The buffer is deep enough that the clock starts only once the first
    MAIN is queued: WARMUP has long been committed by then."""
    rig, watch = watched(buffer=64)
    await rig.go(cycles=2)
    await rig.clock.advance(10.0)

    assert watch.at(Phase.WARMUP) == (True, 0)


async def test_cm42_run_is_announced_as_the_first_main_sample_plays():
    """Before, RUN went out the moment WARMUP was committed -- a WARMUP and a
    buffer before the first image."""
    rig, watch = watched(warmup=4)
    await rig.go(cycles=2)
    await rig.clock.advance(10.0)

    assert watch.at(Phase.RUN) == (True, 4)


async def test_cm43_cooldown_at_its_first_sample_and_idle_once_drained():
    rig, watch = watched(warmup=4, main=8, cooldown=4)
    await rig.go(cycles=2)
    await rig.clock.advance(10.0)

    assert watch.at(Phase.COOLDOWN) == (True, 4 + 2 * 8)
    assert watch.at(Phase.IDLE) == (True, 4 + 2 * 8 + 4)
    assert watch.phases() == [Phase.ARMED, Phase.WARMUP, Phase.RUN, Phase.COOLDOWN, Phase.IDLE]


async def test_cm44_stopped_before_the_first_main_plays_run_is_never_announced():
    """Told to finish while WARMUP is still being written, the source queues
    no MAIN at all: RUN never plays, so it is never announced, and a device
    waiting for RUN never starts its routine (user)."""
    rig, watch = watched(warmup=32, buffer=12)
    await rig.go(cycles=-1)
    await rig.clock.advance(0.5)  # WARMUP is three buffers long: still writing it

    rig.ref.tell(Finish(), Priority.URGENT)
    await rig.clock.advance(10.0)

    assert watch.phases() == [Phase.ARMED, Phase.WARMUP, Phase.COOLDOWN, Phase.IDLE]
    assert [label for label, _, _ in rig.played()] == ["warmup", "cooldown"]


async def test_cm45_a_source_with_no_buffer_announces_each_phase_as_it_enters_it():
    clock = VirtualClock()
    source = SimulatedTimingSource(cycle_period=1.0)
    ref = spawn(source, clock=clock, name="source")
    heard = []

    class Ears(Actor):
        async def handle(self, msg):
            if isinstance(msg, SourcePhase):
                heard.append((msg.phase, clock.now()))

    ref.tell(Subscribe(spawn(Ears(), clock=clock)))
    await clock.advance(0)
    ref.tell(StartDevice("", 2), sender=None)
    ref.tell(Go())
    await clock.advance(5.0)

    assert heard == [
        (Phase.ARMED, 0.0),
        (Phase.WARMUP, 0.0),
        (Phase.RUN, 0.0),
        (Phase.COOLDOWN, 2.0),
        (Phase.IDLE, 2.0),
    ]


async def test_cm46_a_phase_is_announced_on_time_through_a_long_refill_wait():
    """The check is a message the source sends itself, URGENT, and it gets in
    while a cycle's handler waits for room in the buffer. Here each MAIN is
    four buffers long: the first cycle's commit is still waiting when RUN
    starts playing, and a check that queued behind it would announce RUN a
    cycle late."""
    rig, watch = watched(warmup=4, main=48, buffer=12)
    await rig.go(cycles=2)
    await rig.clock.advance(20.0)

    assert watch.at(Phase.RUN) == (True, 4)
    assert watch.at(Phase.COOLDOWN) == (True, 4 + 2 * 48)


async def test_cm46_warmup_longer_than_the_buffer_is_announced_as_the_clock_starts():
    rig, watch = watched(warmup=32, buffer=12)
    await rig.go(cycles=1)
    await rig.clock.advance(20.0)

    started, played = watch.at(Phase.WARMUP)
    assert started and played == 0
    assert watch.at(Phase.RUN) == (True, 32)


async def test_cm47_phases_that_start_together_are_announced_in_order():
    """An empty WARMUP starts where RUN does: both at the first sample, and
    WARMUP first."""
    rig, watch = watched(warmup=0)
    await rig.go(cycles=1)
    await rig.clock.advance(10.0)

    assert watch.phases() == [Phase.ARMED, Phase.WARMUP, Phase.RUN, Phase.COOLDOWN, Phase.IDLE]
    assert watch.at(Phase.WARMUP) == (True, 0)
    assert watch.at(Phase.RUN) == (True, 0)


async def test_cm47_a_source_that_never_starts_its_clock_still_announces_its_end():
    """Zero cycles with empty WARMUP and COOLDOWN: nothing is ever written,
    so the clock never starts (W5). Its phases are empty, and IDLE still
    comes, so the Coordinator is not left waiting."""
    rig, watch = watched(warmup=0, cooldown=0)
    await rig.load()
    assert await rig.ask(StartDevice("run-01", 0)) == DeviceReady()
    rig.ref.tell(Go())
    await rig.clock.advance(5.0)

    assert watch.phases()[-1] is Phase.IDLE
    assert Phase.RUN not in watch.phases()
