"""DP30–DP33 -- bulk writes: several properties in one message.

Added to be measured rather than guessed at, and removable if they add too
much complexity (user, 2026-10-05). A batch is a map: a client's language
may not keep its order, so the device applies it in its own, its table's,
which the adapter declares in the order its writes depend on each other.
What can be checked for the whole map is checked first, and refuses the
whole message. Limits cannot be: earlier writes move them. So the table is
read again after each write, and each write is checked as it comes (user,
2026-10-06), and the first refused ends the batch. Nothing is put back.

An adapter whose hardware takes several settings in one call (the uEye's
area of interest) may take the batch whole; its driver then judges the
limits, and the table is read only before and after.
"""

import pytest

from stacscope.core.actors import Rejected
from stacscope.core.devices import PropertyRefused
from stacscope.core.protocol import (
    DeviceReady,
    PropertiesChanged,
    PropertiesRefused,
    SetProperties,
    StartDevice,
)

from test_properties import Camera, Rig


class Whole(Camera):
    """Takes a batch whole, as hardware with a multi-setting call would."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.batches = []

    async def write_properties(self, values):
        self.batches.append(list(values))
        for name, value in values:
            try:
                await self.write_property(name, value)
            except Exception as exc:
                raise PropertyRefused(str(exc), name=name) from exc


class Unnamed(Whole):
    """Takes a batch whole, and fails it without saying which property."""

    async def write_properties(self, values):
        raise OSError("the serial line dropped")


async def reads_during(rig, msg):
    before = rig.device.reads
    reply = await rig.ask(msg)
    return reply, rig.device.reads - before


# --- DP30: one message, the device's order ---------------------------------------------------


async def test_dp30_a_batch_is_applied_in_the_devices_order_whatever_the_maps():
    """The exposure of 0.5 s is allowed only once the frame rate has come
    down to 2 Hz. In the map's order it would be refused; in the table's,
    which puts the frame rate first, it is not."""
    rig = Rig()

    reply = await rig.ask(SetProperties({"exposure": 0.5, "mode": "LOG", "frame_rate": 2.0}))

    assert isinstance(reply, PropertiesChanged), reply
    assert rig.device.writes == [("frame_rate", 2.0), ("exposure", 0.5), ("mode", "LOG")]


async def test_dp30_a_batch_answers_everything_that_moved_once_and_publishes_it_once():
    rig = Rig()
    before = await rig.table()

    reply = await rig.ask(SetProperties({"frame_rate": 50.0, "cooler": True}))
    await rig.settle()

    after = await rig.table()
    assert set(reply.properties) == {"frame_rate", "exposure", "cooler"}
    assert all(reply.properties[n] == after[n] != before[n] for n in reply.properties)
    assert rig.subscriber.of(PropertiesChanged) == [reply]


async def test_dp30_every_property_set_is_answered_even_if_it_did_not_move():
    """As for a single set: the answer says what each property now is."""
    rig = Rig()

    reply = await rig.ask(SetProperties({"cooler": False, "frame_rate": 50.0}))

    assert set(reply.properties) == {"cooler", "frame_rate", "exposure"}


async def test_dp30_a_batch_of_n_reads_the_table_n_plus_one_times():
    """Once before, and once after each write: the read after one write is
    the one the next is checked against, and the last is the answer. N
    single writes read it 2N times."""
    rig = Rig()

    _, reads = await reads_during(rig, SetProperties({"frame_rate": 50.0, "exposure": 0.01, "cooler": True}))

    assert reads == 4


async def test_dp30_an_empty_batch_changes_nothing_and_publishes_nothing():
    rig = Rig()

    reply = await rig.ask(SetProperties({}))
    await rig.settle()

    assert reply == PropertiesChanged({})
    assert rig.subscriber.heard == []


# --- DP31: what can be checked for the whole map, is ------------------------------------------


@pytest.mark.parametrize(
    ("values", "says"),
    [
        ({"frame_rate": 50.0, "gain": 1.0}, "gain"),
        ({"frame_rate": 50.0, "temperature": 1.0}, "read-only"),
        ({"frame_rate": 50.0, "exposure": "fast"}, "number"),
        ({"frame_rate": 50.0, "cooler": 1.0}, "flag"),
        ({"frame_rate": 50.0, "mode": "CUBIC"}, "LINEAR"),
    ],
    ids=["unknown", "read-only", "kind", "flag", "label"],
)
async def test_dp31_a_bad_entry_anywhere_refuses_the_whole_map_and_nothing_is_applied(values, says):
    rig = Rig()
    before = await rig.table()

    reply = await rig.ask(SetProperties(values))

    assert isinstance(reply, Rejected) and says in reply.reason
    assert rig.device.writes == []
    assert await rig.table() == before


async def test_dp31_an_idle_property_while_armed_refuses_the_whole_map():
    rig = Rig()
    assert await rig.ask(StartDevice("", -1)) == DeviceReady()

    reply = await rig.ask(SetProperties({"exposure": 0.02, "mode": "LOG"}))

    assert isinstance(reply, Rejected) and "mode" in reply.reason and "idle" in reply.reason
    assert rig.device.writes == []


# --- DP32: a refusal partway ------------------------------------------------------------------


@pytest.mark.parametrize(
    ("fail", "exposure", "says"),
    [(None, 0.05, "at most 0.02"), ({"exposure": "the shutter is stuck"}, 0.01, "the shutter is stuck")],
    ids=["a limit an earlier write moved", "the adapter raising"],
)
async def test_dp32_a_refusal_partway_stops_there_and_puts_nothing_back(fail, exposure, says):
    """The frame rate is applied, and moves the exposure's maximum to 0.02 s.
    The exposure is then refused, and the mode, after it in the table, is
    never tried. The frame rate stays: putting it back may not even be
    possible from where the device now is. The answer says which was
    refused and why, and holds what moved before it, which is published."""
    rig = Rig(fail=fail)

    reply = await rig.ask(SetProperties({"mode": "LOG", "exposure": exposure, "frame_rate": 50.0}))
    await rig.settle()

    assert isinstance(reply, PropertiesRefused), reply
    assert reply.name == "exposure" and says in reply.reason
    assert set(reply.changed) == {"frame_rate", "exposure"}  # the exposure's limits moved
    assert ("mode", "LOG") not in rig.device.writes
    table = await rig.table()
    assert table["frame_rate"].value == 50.0 and table["mode"].value == "LINEAR"
    assert rig.subscriber.of(PropertiesChanged) == [PropertiesChanged(reply.changed)]


async def test_dp32_a_refusal_of_the_first_write_publishes_nothing():
    rig = Rig()

    reply = await rig.ask(SetProperties({"frame_rate": 500.0, "cooler": True}))
    await rig.settle()

    assert reply == PropertiesRefused("frame_rate", reply.reason, {})
    assert rig.subscriber.heard == []


# --- DP33: an adapter that takes the batch whole ------------------------------------------------


async def test_dp33_an_adapter_that_takes_the_batch_whole_receives_it_whole_in_the_devices_order():
    rig = Rig(Whole())

    reply, reads = await reads_during(rig, SetProperties({"cooler": True, "exposure": 0.01, "frame_rate": 50.0}))

    assert isinstance(reply, PropertiesChanged)
    assert rig.device.batches == [[("frame_rate", 50.0), ("exposure", 0.01), ("cooler", True)]]
    assert reads == 2


async def test_dp33_a_whole_batch_is_checked_up_front_but_its_limits_are_the_adapters():
    """The core cannot see limits between writes it does not make; the
    driver judges them. What the whole map can be checked for, it is."""
    rig = Rig(Whole())

    refused = await rig.ask(SetProperties({"frame_rate": 50.0, "mode": "CUBIC"}))
    # 0.5 s is past the exposure's maximum as read before the batch, and
    # within it once the frame rate is 2 Hz: the core cannot tell which.
    taken = await rig.ask(SetProperties({"frame_rate": 2.0, "exposure": 0.5}))

    assert isinstance(refused, Rejected)
    assert rig.device.batches == [[("frame_rate", 2.0), ("exposure", 0.5)]]
    assert isinstance(taken, PropertiesChanged)


async def test_dp33_a_whole_batch_the_adapter_refuses_answers_what_moved():
    rig = Rig(Whole(fail={"cooler": "the cooler is not fitted"}))

    reply = await rig.ask(SetProperties({"frame_rate": 50.0, "cooler": True}))

    assert isinstance(reply, PropertiesRefused)
    assert reply.name == "cooler" and "the cooler is not fitted" in reply.reason
    assert set(reply.changed) == {"frame_rate", "exposure"}


async def test_dp33_a_whole_batch_refused_without_a_name_names_none():
    """Any exception from the adapter is a refusal (decision 5). Which
    property failed is the adapter's to say; without it, the answer names
    none rather than guess."""
    rig = Rig(Unnamed())

    reply = await rig.ask(SetProperties({"frame_rate": 50.0}))

    assert reply == PropertiesRefused("", reply.reason, {}) and "the serial line dropped" in reply.reason
