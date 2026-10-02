"""SignalOutput port contract. Every adapter must pass this suite.

Modelled on NI-DAQmx continuous output with regeneration disabled: samples
written are played once, in order, at the configured rate; the software must
keep the buffer fed, and running dry mid-stream is an error (W3 at the port
level), not a silent gap. Only ``drain`` makes running dry the expected end.

The tests live here, in a module pytest does not collect, and each adapter's
suite imports them beside a ``rig`` fixture of its own (see ``Rig``):

    from signal_output_contract import *  # the tests, and Rig

    @pytest.fixture
    async def rig():
        ...

The simulated adapter's suite is ``test_signal_output.py``, beside this file,
in virtual time and exact. The NI adapter's are in ``tests/adapters/nidaqmx``
(a simulated board) and ``tests/hardware/nidaqmx`` (the physical one).

Every duration is a fraction of the buffer's own length, and every count a
fraction of its depth, so one test fits any rig.
"""

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import numpy as np
import pytest

from stacscope.core.ports import OutputConfig, SignalOutput, Underflow

__all__ = [
    "Rig",
    "test_a_closed_output_refuses_to_be_used",
    "test_close_is_safe_before_anything_was_configured",
    "test_close_is_safe_mid_stream_and_after_stop",
    "test_close_may_be_repeated",
    "test_drain_makes_running_dry_the_expected_end",
    "test_output_consumes_samples_at_the_configured_rate",
    "test_running_dry_mid_stream_surfaces_as_underflow_on_the_next_call",
    "test_samples_played_counts_what_has_played_and_never_what_has_not",
    "test_samples_played_holds_once_output_stops",
    "test_samples_played_is_zero_until_output_starts",
    "test_space_is_the_whole_buffer_once_configured",
    "test_starting_with_nothing_written_is_an_error",
    "test_stop_is_safe_whatever_the_state",
    "test_stop_stays_safe_after_close",
    "test_wait_for_space_returns_exactly_when_it_frees",
    "test_waiting_on_a_full_buffer_that_was_never_started_is_an_error",
    "test_writes_consume_space_and_may_not_exceed_it",
]


@dataclass
class Rig:
    """An adapter under test, not yet configured, and what the suite needs to
    know about the thing behind it.

    ``elapse`` lets time pass: advancing a virtual clock, or sleeping on a real
    one. ``slack`` is how many samples a free-space reading may be off from
    what the elapsed time predicts: 0 in virtual time; on hardware, the
    onboard FIFO takes samples ahead of the clock, the driver moves them in
    blocks, and the process wakes late. ``runs_dry`` is False for a
    simulated board that stalls when it runs out instead of reporting it,
    which skips the one test that needs it to."""

    output: SignalOutput
    config: OutputConfig
    elapse: Callable[[float], Awaitable[None]]
    slack: int = 0
    runs_dry: bool = True

    @property
    def depth(self) -> int:
        return self.config.buffer_samples

    @property
    def length(self) -> float:
        """How long the full buffer takes to play, in seconds."""
        return self.depth / self.config.rate

    def block(self, n: int):
        """``n`` samples at 0 V and low, on every channel: safe on any rig."""
        return np.zeros((len(self.config.ao), n)), np.zeros((len(self.config.do), n), dtype=bool)

    def near(self, actual: int, expected: float) -> None:
        assert abs(actual - expected) <= self.slack, (
            f"{actual} free, expected {expected:.0f} (within {self.slack})"
        )


async def _configured(rig: Rig) -> SignalOutput:
    await rig.output.configure(rig.config)
    await rig.output.arm()
    return rig.output


async def test_space_is_the_whole_buffer_once_configured(rig):
    output = await _configured(rig)

    assert output.space_available() == rig.depth


async def test_writes_consume_space_and_may_not_exceed_it(rig):
    output = await _configured(rig)
    await output.write(*rig.block(rig.depth * 5 // 8))

    rig.near(output.space_available(), rig.depth * 3 / 8)
    with pytest.raises(ValueError):
        await output.write(*rig.block(output.space_available() + 1))


async def test_output_consumes_samples_at_the_configured_rate(rig):
    output = await _configured(rig)
    await output.write(*rig.block(rig.depth))
    await output.go()

    await rig.elapse(rig.length / 4)

    rig.near(output.space_available(), rig.depth / 4)


async def test_samples_played_is_zero_until_output_starts(rig):
    """CM40. The timing source dates each phase by the sample it starts at,
    and announces it once that sample has played: nothing has before the
    clock starts, however much is written."""
    output = await _configured(rig)
    await output.write(*rig.block(rig.depth // 2))
    await rig.elapse(rig.length / 4)

    assert output.samples_played() == 0


async def test_samples_played_counts_what_has_played_and_never_what_has_not(rig):
    """CM40. At the configured rate from ``go``, within the rig's slack, and
    never more than was written -- a phase announced early is the bug this
    exists to fix."""
    output = await _configured(rig)
    await output.write(*rig.block(rig.depth))
    await output.go()

    await rig.elapse(rig.length / 4)
    quarter = output.samples_played()
    await rig.elapse(rig.length / 2)
    three_quarters = output.samples_played()

    assert abs(quarter - rig.depth / 4) <= rig.slack, quarter
    assert abs(three_quarters - rig.depth * 3 / 4) <= rig.slack, three_quarters
    assert three_quarters <= rig.depth


async def test_samples_played_holds_once_output_stops(rig):
    """CM40. Nothing plays after a stop, so the count stops with it."""
    output = await _configured(rig)
    await output.write(*rig.block(rig.depth))
    await output.go()
    await rig.elapse(rig.length / 4)

    await output.stop()
    stopped = output.samples_played()
    await rig.elapse(rig.length / 4)

    assert output.samples_played() == stopped
    assert abs(stopped - rig.depth / 4) <= rig.slack, stopped


async def test_wait_for_space_returns_exactly_when_it_frees(rig):
    output = await _configured(rig)
    await output.write(*rig.block(rig.depth))
    await output.go()
    wanted = rig.depth // 2

    waiting = asyncio.create_task(output.wait_for_space(wanted))
    await rig.elapse(rig.length * 3 / 8)
    assert not waiting.done()

    await rig.elapse(rig.length / 8 + rig.slack / rig.config.rate)
    assert waiting.done()
    assert wanted <= output.space_available() <= wanted + 2 * rig.slack


async def test_starting_with_nothing_written_is_an_error(rig):
    output = await _configured(rig)

    with pytest.raises(RuntimeError):
        await output.go()


async def test_waiting_on_a_full_buffer_that_was_never_started_is_an_error(rig):
    output = await _configured(rig)
    await output.write(*rig.block(rig.depth))

    with pytest.raises(RuntimeError):
        await output.wait_for_space(1)


async def test_running_dry_mid_stream_surfaces_as_underflow_on_the_next_call(rig):
    if not rig.runs_dry:
        pytest.skip("this rig stalls when it runs out, instead of reporting it")
    output = await _configured(rig)
    await output.write(*rig.block(rig.depth // 2))
    await output.go()

    await rig.elapse(rig.length * 3 / 4)  # ran dry at half; nothing refilled it

    with pytest.raises(Underflow) as caught:
        await output.write(*rig.block(1))
    assert caught.value.at == (rig.depth // 2) / rig.config.rate


async def test_drain_makes_running_dry_the_expected_end(rig):
    output = await _configured(rig)
    await output.write(*rig.block(rig.depth * 3 // 4))
    await output.go()

    draining = asyncio.create_task(output.drain())
    await rig.elapse(rig.length * 5 / 8)
    assert not draining.done()

    await rig.elapse(rig.length / 4)  # well past the last sample at 3/4: no underflow
    assert draining.done()
    await draining
    await output.stop()


async def test_stop_is_safe_whatever_the_state(rig):
    output = await _configured(rig)
    await output.stop()
    await output.stop()


# --- N10: close ------------------------------------------------------------------


async def test_close_is_safe_before_anything_was_configured(rig):
    """``teardown`` runs even when ``setup`` failed, so the release it performs
    must cope with an output that never reserved anything."""
    await rig.output.close()


async def test_close_is_safe_mid_stream_and_after_stop(rig):
    """The two ways an acquisition reaches ``teardown``: cut off while
    playing, or stopped first. Neither may make the release itself fail."""
    output = await _configured(rig)
    await output.write(*rig.block(rig.depth // 2))
    await output.go()
    await rig.elapse(rig.length / 8)

    await output.stop()
    await output.close()


async def test_close_may_be_repeated(rig):
    """Both an owner and a fixture may release the same output; the second
    must not fail over the first."""
    output = await _configured(rig)
    await output.close()
    await output.close()


@pytest.mark.parametrize(
    "use",
    [
        lambda output, rig: output.configure(rig.config),
        lambda output, rig: output.arm(),
        lambda output, rig: output.write(*rig.block(1)),
        lambda output, rig: output.go(),
        lambda output, rig: output.wait_for_space(1),
        lambda output, rig: output.drain(),
    ],
    ids=["configure", "arm", "write", "go", "wait_for_space", "drain"],
)
async def test_a_closed_output_refuses_to_be_used(rig, use):
    """Closing gives the hardware back. An output that quietly went on working
    afterwards -- or reserved the board again -- would be holding hardware
    that its owner believes it released."""
    output = await _configured(rig)
    await output.write(*rig.block(4))
    await output.close()

    with pytest.raises(RuntimeError):
        await use(output, rig)
    with pytest.raises(RuntimeError):
        output.space_available()


async def test_stop_stays_safe_after_close(rig):
    """``stop`` is safe in any state, and closed is a state."""
    output = await _configured(rig)
    await output.close()
    await output.stop()
