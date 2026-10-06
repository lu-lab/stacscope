"""DP1–DP4 -- the codec carries a property's value and a table of them.

A device's properties are numbers, labels and flags, so one field must hold
any of the three: ``bool | float | str``. A table is a map from name to
property: ``dict[str, T]``. Both cross from any client in any language, so
decoding trusts neither, and a union is allowed only where the wire alone
says which arm a value is. msgpack's int and float are distinct types, but
not every language keeps them distinct: JavaScript writes 2.0 as 2. So a
union holding both ``int`` and ``float`` cannot be decoded faithfully, and is
refused when the codec is built rather than guessed at per message.
"""

import dataclasses
import pickle

import msgpack
import pytest

from stacscope.core.actors import ActorRef
from stacscope.core.protocol import MessageRegistry, ReadOnlyMap
from stacscope.core.transport import Codec, CodecError

registry = MessageRegistry()


@registry.register
@dataclasses.dataclass(frozen=True)
class Entry:
    unit: str
    value: float


@registry.register
@dataclasses.dataclass(frozen=True)
class Holds:
    value: bool | float | str
    maybe: bool | float | str | None
    numbers: dict[str, float]
    entries: dict[str, Entry]


@pytest.fixture
def codec():
    return Codec(registry)


def message(**changes) -> Holds:
    fields = dict(
        value=2.5,
        maybe=None,
        numbers={"b": 1.0, "a": 2.0},
        entries={"exposure": Entry("s", 0.01)},
    )
    return Holds(**(fields | changes))


def decode(codec, spoiled: dict):
    return codec.decode(msgpack.packb(spoiled, use_bin_type=True))


def raw(codec, msg: Holds) -> dict:
    return msgpack.unpackb(codec.encode(msg))


# --- DP1: the union is decided by the wire type -------------------------------


@pytest.mark.parametrize("value", [True, False, 2.5, -0.0, "LINEAR", ""])
def test_dp1_each_kind_round_trips_as_itself(codec, value):
    decoded = codec.decode(codec.encode(message(value=value)))

    assert decoded.value == value and type(decoded.value) is type(value)


def test_dp1_an_int_on_the_wire_arrives_as_a_float(codec):
    """The union has no int arm: a property's number is a float, however the
    client's language wrote it."""
    spoiled = raw(codec, message())
    spoiled["value"] = 2

    decoded = decode(codec, spoiled)

    assert decoded.value == 2.0 and type(decoded.value) is float


def test_dp1_true_arrives_as_a_bool_never_as_a_number(codec):
    """Python calls a bool an int, so a decoder that asked ``isinstance(raw,
    int)`` first would turn a flag into 1.0, and a device would then refuse
    the flag as the wrong kind, or worse, accept it as a number."""
    spoiled = raw(codec, message())
    spoiled["value"] = True

    decoded = decode(codec, spoiled)

    assert decoded.value is True


@pytest.mark.parametrize(
    "value",
    [None, b"\x00", [1.0], {"x": 1.0}],
    ids=["nil", "bytes", "list", "map"],
)
def test_dp1_a_value_of_no_arm_is_refused_naming_the_field(codec, value):
    spoiled = raw(codec, message())
    spoiled["value"] = value

    with pytest.raises(CodecError, match="Holds.value"):
        decode(codec, spoiled)


def test_dp1_an_optional_union_also_takes_nil(codec):
    for value in (None, True, 3.0, "x"):
        decoded = codec.decode(codec.encode(message(maybe=value)))

        assert decoded.maybe == value and type(decoded.maybe) is type(value)


# --- DP2: a union the wire cannot decide is refused at build ------------------


@pytest.mark.parametrize(
    "annotation",
    [
        int | float,  # JavaScript writes 2.0 as 2
        int | float | None,
        bool | int | float | str,
        float | tuple[float, ...],  # a non-scalar arm
        str | Entry,
        str | ActorRef,  # an address is a string on the wire
        bool | dict[str, float],
    ],
    ids=str,
)
def test_dp2_an_undecidable_union_is_refused_naming_the_field(annotation):
    bad = MessageRegistry()

    @bad.register
    @dataclasses.dataclass(frozen=True)
    class Ambiguous:
        value: annotation

    with pytest.raises(CodecError, match="Ambiguous.value"):
        Codec(bad)


@pytest.mark.parametrize("annotation", [bool | float | str, int | str, bool | int, str | bytes])
def test_dp2_a_union_of_distinct_wire_types_is_accepted(annotation):
    """The rule is the wire's, not a list of blessed unions."""
    good = MessageRegistry()

    @good.register
    @dataclasses.dataclass(frozen=True)
    class Decidable:
        value: annotation

    Codec(good)


@pytest.mark.parametrize("annotation", [dict[int, float], dict[str, object], dict])
def test_dp2_a_map_with_keys_other_than_strings_or_values_it_cannot_carry_is_refused(annotation):
    bad = MessageRegistry()

    @bad.register
    @dataclasses.dataclass(frozen=True)
    class BadMap:
        values: annotation

    with pytest.raises(CodecError, match="BadMap.values"):
        Codec(bad)


# --- DP3: maps ---------------------------------------------------------------


def test_dp3_a_map_round_trips_keeping_its_order(codec):
    """The order is the device's table order, which a page shows as it is;
    a client in another language may not keep it, so nothing depends on it
    but display."""
    msg = message(numbers={"z": 1.0, "a": 2.0, "m": 3.0})

    decoded = codec.decode(codec.encode(msg))

    assert list(decoded.numbers.items()) == [("z", 1.0), ("a", 2.0), ("m", 3.0)]
    assert decoded.entries == {"exposure": Entry("s", 0.01)}


def test_dp3_a_map_travels_as_a_plain_msgpack_map(codec):
    wire = raw(codec, message(numbers={"a": 2.0}))

    assert wire["numbers"] == {"a": 2.0}
    assert wire["entries"] == {"exposure": {"type": "Entry", "unit": "s", "value": 0.01}}


def test_dp3_an_empty_map_round_trips(codec):
    decoded = codec.decode(codec.encode(message(numbers={}, entries={})))

    assert decoded.numbers == {} and decoded.entries == {}


@pytest.mark.parametrize(
    "value",
    [{1: 2.0}, {b"a": 2.0}, [["a", 2.0]], "a", None],
    ids=["int-key", "bytes-key", "list", "string", "nil"],
)
def test_dp3_a_malformed_map_is_refused_naming_the_field(codec, value):
    spoiled = raw(codec, message())
    spoiled["numbers"] = value

    with pytest.raises(CodecError, match="Holds.numbers"):
        decode(codec, spoiled)


def test_dp3_a_bad_value_is_refused_naming_its_key(codec):
    spoiled = raw(codec, message())
    spoiled["numbers"] = {"good": 1.0, "exposure": "fast"}

    with pytest.raises(CodecError, match=r"Holds\.numbers\[.exposure.\]"):
        decode(codec, spoiled)


def test_dp3_a_bad_nested_message_is_refused_naming_its_key(codec):
    spoiled = raw(codec, message())
    del spoiled["entries"]["exposure"]["unit"]

    with pytest.raises(CodecError, match=r"entries\[.exposure.\].*Entry.unit"):
        decode(codec, spoiled)


def test_dp3_a_map_with_a_key_that_is_not_a_string_is_refused_at_encode(codec):
    with pytest.raises(CodecError, match="Holds.numbers"):
        codec.encode(message(numbers={1: 2.0}))


# --- DP4: a map in a message is read-only -------------------------------------


def test_dp4_a_decoded_map_is_read_only(codec):
    """In one process every subscriber is handed the same message object, so
    a map one of them could change would change under all the others."""
    decoded = codec.decode(codec.encode(message()))

    with pytest.raises(TypeError):
        decoded.numbers["a"] = 5.0
    assert isinstance(decoded.numbers, ReadOnlyMap)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.__setitem__("a", 5.0),
        lambda m: m.__delitem__("a"),
        lambda m: m.clear(),
        lambda m: m.pop("a"),
        lambda m: m.popitem(),
        lambda m: m.setdefault("c", 1.0),
        lambda m: m.update(c=1.0),
        lambda m: m.__ior__({"c": 1.0}),
    ],
    ids=["set", "del", "clear", "pop", "popitem", "setdefault", "update", "ior"],
)
def test_dp4_a_read_only_map_refuses_every_change(mutate):
    m = ReadOnlyMap({"a": 1.0, "b": 2.0})

    with pytest.raises(TypeError):
        mutate(m)
    assert m == {"a": 1.0, "b": 2.0}


def test_dp4_a_read_only_map_is_a_copy_of_what_it_was_built_from():
    """The sender keeps its own dict, and may go on changing it."""
    source = {"a": 1.0}
    m = ReadOnlyMap(source)
    source["a"] = 9.0

    assert m == {"a": 1.0}


def test_dp4_a_read_only_map_reads_like_a_dict():
    m = ReadOnlyMap({"z": 1.0, "a": 2.0})

    assert list(m) == ["z", "a"] and m["a"] == 2.0 and m.get("q") is None
    assert m == {"z": 1.0, "a": 2.0} and len(m) == 2
    merged = m | {"q": 3.0}
    assert merged == {"z": 1.0, "a": 2.0, "q": 3.0} and type(merged) is dict


def test_dp4_a_read_only_map_pickles_and_packs():
    """A device's launch arguments are pickled to a process child, and a map
    is packed by msgpack as it stands."""
    m = ReadOnlyMap({"a": 1.0})

    again = pickle.loads(pickle.dumps(m))
    assert again == m and type(again) is ReadOnlyMap
    assert msgpack.unpackb(msgpack.packb(m)) == {"a": 1.0}
