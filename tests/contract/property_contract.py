"""Property contract (DP15). Every device that declares a table of properties
must pass this suite.

A client configures any device through the same three messages, knowing
nothing of its adapter, so every adapter's table must keep the same promises:
each property is well formed and reads back as listed (DP6); a set answers
everything it moved and nothing else (DP7); a value of the wrong kind, or
outside what the table lists, is refused and changes nothing (DP8); an IDLE
property is refused while armed, and an ALWAYS one is applied without
stopping the acquisition (DP9); and the names never change (DP12).

The core's own tests (``tests/unit/devices/test_properties.py``) prove the
checks are made before the adapter is called. These prove each adapter's
table is one the checks can be trusted with: limits that are true, labels
the hardware takes, kinds that match the values.

The tests live here, in a module pytest does not collect, and each device's
suite imports them beside a ``rig`` fixture of its own (see ``Rig``):

    from property_contract import *  # the tests and Rig

    @pytest.fixture
    async def rig():
        ...
"""

import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest

from stacscope.core.actors import Rejected
from stacscope.core.protocol import (
    DeviceReady,
    DeviceStopped,
    GetProperties,
    GetProperty,
    PropertiesChanged,
    Property,
    PropertyAccess,
    PropertyKind,
    SetProperty,
    StartDevice,
    StopDevice,
)

__all__ = [
    "Rig",
    "test_dp6_every_property_is_well_formed",
    "test_dp6_get_property_answers_each_as_listed",
    "test_dp7_a_set_answers_what_it_moved_and_nothing_else",
    "test_dp8_a_label_not_listed_is_refused_and_changes_nothing",
    "test_dp8_a_number_outside_its_limits_is_refused_and_changes_nothing",
    "test_dp8_a_read_only_property_is_refused_and_changes_nothing",
    "test_dp8_a_value_of_the_wrong_kind_is_refused_and_changes_nothing",
    "test_dp8_an_unknown_name_is_refused_and_changes_nothing",
    "test_dp9_an_always_property_is_applied_while_armed_and_the_acquisition_carries_on",
    "test_dp9_an_idle_property_is_refused_while_armed",
    "test_dp12_the_names_never_change",
]

type Value = bool | float | str


@dataclass
class Rig:
    """A device under test, spawned and past its setup, idle.

    ``ask`` sends it a message and returns the answer. ``moves`` is a set
    that changes another property's value or limits, as ``(name, value,
    moved)``; ``idle`` a valid change to an IDLE property, and ``always`` a
    valid change to an ALWAYS one, each ``(name, value)``. Each value must
    differ from the property's value at launch.
    """

    ask: Callable[[object], Awaitable[object]]
    moves: tuple[str, Value, str]
    idle: tuple[str, Value]
    always: tuple[str, Value]


async def _table(rig) -> dict[str, Property]:
    return dict((await rig.ask(GetProperties())).properties)


async def _refused(rig, name: str, value: Value, *, says: str = "") -> None:
    """Asserts the set is refused, naming the property and saying ``says``,
    and moves nothing. ``says`` is what the core's check says, not the
    hardware's: a driver that refuses on its own proves nothing about the
    table."""
    before = await _table(rig)

    reply = await rig.ask(SetProperty(name, value))

    assert isinstance(reply, Rejected), f"{name} = {value!r} was not refused: {reply}"
    assert name in reply.reason and says in reply.reason, reply.reason
    assert await _table(rig) == before


def _of_kind(table, kind) -> list[Property]:
    return [p for p in table.values() if p.kind is kind and p.access is not PropertyAccess.READ_ONLY]


# --- DP6 ---------------------------------------------------------------------


async def test_dp6_every_property_is_well_formed(rig):
    """What a page draws a control from: a number's limits contain its
    value, a label is one of its labels, and nothing is listed that does not
    apply to its kind."""
    table = await _table(rig)

    assert table
    for name, p in table.items():
        assert p.name == name
        assert isinstance(p.unit, str)
        match p.kind:
            case PropertyKind.NUMBER:
                assert type(p.value) is float and math.isfinite(p.value), p
                assert p.minimum is None or p.minimum <= p.value, p
                assert p.maximum is None or p.value <= p.maximum, p
                assert not p.choices or p.value in p.choices, p
                assert p.step is None or p.step > 0, p
                assert p.labels == (), p
            case PropertyKind.LABEL:
                assert type(p.value) is str and p.value in p.labels, p
                assert p.choices == () and p.minimum is p.maximum is p.step is None, p
            case PropertyKind.FLAG:
                assert type(p.value) is bool, p
                assert p.labels == p.choices == () and p.minimum is p.maximum is None, p


async def test_dp6_get_property_answers_each_as_listed(rig):
    table = await _table(rig)

    for name, p in table.items():
        assert await rig.ask(GetProperty(name)) == p


# --- DP7 ---------------------------------------------------------------------


async def test_dp7_a_set_answers_what_it_moved_and_nothing_else(rig):
    name, value, moved = rig.moves
    before = await _table(rig)

    reply = await rig.ask(SetProperty(name, value))

    assert isinstance(reply, PropertiesChanged), reply
    assert name in reply.properties and moved in reply.properties
    after = await _table(rig)
    for n, p in after.items():
        if n in reply.properties:
            assert reply.properties[n] == p
            assert n == name or before[n] != p, f"{n} did not move, but was answered"
        else:
            assert before[n] == p, f"{n} moved, but was not answered"


# --- DP8 ---------------------------------------------------------------------


async def test_dp8_an_unknown_name_is_refused_and_changes_nothing(rig):
    await _refused(rig, "no such property", 1.0)


async def test_dp8_a_read_only_property_is_refused_and_changes_nothing(rig):
    table = await _table(rig)
    read_only = [p for p in table.values() if p.access is PropertyAccess.READ_ONLY]
    if not read_only:
        pytest.skip("this device has no read-only property")

    for p in read_only:
        await _refused(rig, p.name, p.value)


async def test_dp8_a_value_of_the_wrong_kind_is_refused_and_changes_nothing(rig):
    table = await _table(rig)
    wrong = {
        PropertyKind.NUMBER: ["1.0", True],
        PropertyKind.LABEL: [1.0, True],
        PropertyKind.FLAG: [1.0, "true"],
    }

    for kind, values in wrong.items():
        for p in _of_kind(table, kind)[:1]:
            for value in values:
                await _refused(rig, p.name, value)


async def test_dp8_a_label_not_listed_is_refused_and_changes_nothing(rig):
    table = await _table(rig)
    labels = _of_kind(table, PropertyKind.LABEL)
    if not labels:
        pytest.skip("this device has no label")

    for p in labels:
        await _refused(rig, p.name, "NOT A LABEL", says=p.labels[0])


async def test_dp8_a_number_outside_its_limits_is_refused_and_changes_nothing(rig):
    table = await _table(rig)
    tried = 0

    for p in _of_kind(table, PropertyKind.NUMBER):
        span = 1.0 if p.minimum is None or p.maximum is None else max(p.maximum - p.minimum, 1.0)
        if p.maximum is not None:
            await _refused(rig, p.name, p.maximum + span)
            tried += 1
        if p.minimum is not None:
            await _refused(rig, p.name, p.minimum - span)
            tried += 1
        if p.choices:
            await _refused(rig, p.name, max(p.choices) + span)
            tried += 1
    if not tried:
        pytest.skip("this device has no bounded number")


# --- DP9 ---------------------------------------------------------------------


async def test_dp9_an_idle_property_is_refused_while_armed(rig):
    name, value = rig.idle
    assert (await _table(rig))[name].access is PropertyAccess.IDLE
    assert await rig.ask(StartDevice("", -1)) == DeviceReady()

    try:
        await _refused(rig, name, value, says="idle")
    finally:
        assert await rig.ask(StopDevice()) == DeviceStopped()
    assert isinstance(await rig.ask(SetProperty(name, value)), PropertiesChanged)


async def test_dp9_an_always_property_is_applied_while_armed_and_the_acquisition_carries_on(rig):
    name, value = rig.always
    assert (await _table(rig))[name].access is PropertyAccess.ALWAYS
    assert await rig.ask(StartDevice("", -1)) == DeviceReady()

    reply = await rig.ask(SetProperty(name, value))

    assert isinstance(reply, PropertiesChanged), reply
    assert reply.properties[name].value == pytest.approx(value) if isinstance(value, float) else value
    assert await rig.ask(StopDevice()) == DeviceStopped()


# --- DP12 --------------------------------------------------------------------


async def test_dp12_the_names_never_change(rig):
    """A client builds its view of a device once, from the names; a name
    that came or went would leave it showing a control for nothing."""
    names = list(await _table(rig))

    await rig.ask(SetProperty(*rig.moves[:2]))
    await rig.ask(SetProperty(*rig.always))
    assert await rig.ask(StartDevice("", -1)) == DeviceReady()
    armed = list(await _table(rig))
    assert await rig.ask(StopDevice()) == DeviceStopped()

    assert armed == names
    assert list(await _table(rig)) == names
