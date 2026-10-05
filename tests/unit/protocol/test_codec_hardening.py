"""WS1–WS3 -- decoding trusts nothing it is given.

Until the gateway, every frame came from a STACScope process built from the
same code, so the codec decoded by the field annotations and trusted the
values. The gateway takes frames from anyone who can open a socket: a script
with a typo, a browser page written in another language, an older client.
A malformed message must then fail as ``CodecError`` -- the one error a link
knows to skip and a gateway knows to answer ``Rejected`` -- never as a
``KeyError`` or ``TypeError`` that closes the link, and never as a message
that reaches an actor holding a value of the wrong type.
"""

import dataclasses
from enum import Enum

import msgpack
import numpy as np
import pytest

from stacscope.core.actors import ActorRef
from stacscope.core.protocol import MessageRegistry
from stacscope.core.transport import Codec, CodecError

registry = MessageRegistry()


class Colour(Enum):
    RED = "red"
    BLUE = "blue"


@registry.register
@dataclasses.dataclass(frozen=True)
class Inner:
    n: int


@registry.register
@dataclasses.dataclass(frozen=True, eq=False)
class Kinds:
    """One field of every kind the codec carries."""

    n: int
    x: float
    s: str
    b: bytes
    flag: bool
    colour: Colour
    maybe: int | None
    items: tuple[int, ...]
    inner: Inner
    ref: ActorRef
    data: np.ndarray


@registry.register
@dataclasses.dataclass(frozen=True)
class Other:
    n: int


REF = ActorRef(transport=None, address="coordinator")
RESOLVE = {REF.address: REF}.get

GOOD = Kinds(
    n=3,
    x=2.5,
    s="run-01",
    b=b"\x00",
    flag=True,
    colour=Colour.BLUE,
    maybe=None,
    items=(1, 2),
    inner=Inner(4),
    ref=REF,
    data=np.arange(6, dtype="<u2").reshape(2, 3),
)


@pytest.fixture
def codec():
    return Codec(registry)


def raw(codec) -> dict:
    """``GOOD`` as the plain map that travels, ready to be spoiled."""
    return msgpack.unpackb(codec.encode(GOOD))


def decode(codec, spoiled: dict):
    return codec.decode(msgpack.packb(spoiled, use_bin_type=True), resolve=RESOLVE)


def test_the_unspoiled_message_decodes(codec):
    """The baseline every case below spoils one thing of."""
    decoded = decode(codec, raw(codec))

    assert decoded.n == 3 and decoded.colour is Colour.BLUE and decoded.inner == Inner(4)
    assert np.array_equal(decoded.data, GOOD.data)


# --- WS1: the fields present are the fields declared --------------------------


def test_ws1_a_missing_field_is_a_codec_error_naming_it(codec):
    """It was a ``KeyError``, which a link does not skip: one message missing
    a field closed the whole connection."""
    spoiled = raw(codec)
    del spoiled["s"]

    with pytest.raises(CodecError, match="Kinds.s"):
        decode(codec, spoiled)


def test_ws1_an_extra_field_is_a_codec_error_naming_it(codec):
    """Dropping it in silence would hide a client written against another
    version of the message: it would believe it set something it did not."""
    spoiled = raw(codec)
    spoiled["exposure"] = 0.01

    with pytest.raises(CodecError, match="exposure"):
        decode(codec, spoiled)


def test_ws1_a_missing_field_in_a_nested_message_is_a_codec_error(codec):
    spoiled = raw(codec)
    del spoiled["inner"]["n"]

    with pytest.raises(CodecError, match="Inner.n"):
        decode(codec, spoiled)


# --- WS2: each value is of its field's type -----------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("n", "3"),  # a string for an int
        ("n", True),  # a bool is an int to Python, never to a message
        ("n", 2.5),  # a float for an int
        ("x", "2.5"),  # a string for a float
        ("x", False),
        ("s", 7),  # an int for a string
        ("s", b"run-01"),  # bytes for a string
        ("b", "\x00"),  # a string for bytes
        ("flag", 1),  # an int for a bool
        ("colour", "green"),  # not one of the enum's values
        ("colour", None),
        ("maybe", "4"),  # an optional is only None or its type
        ("items", 5),  # a scalar for a tuple
        ("items", [1, "2"]),  # an item of the wrong type
        ("items", {"type": "Inner", "n": 1}),  # a map for a tuple
        ("inner", [4]),  # a list where a message is expected
        ("inner", 4),
        ("inner", {"type": "Other", "n": 4}),  # the wrong message
        ("inner", {"n": 4}),  # a message without its type
        ("ref", 12),  # an address is a string
        ("s", None),  # None only where the field allows it
    ],
)
def test_ws2_a_value_of_the_wrong_type_is_a_codec_error_naming_its_field(codec, field, value):
    spoiled = raw(codec)
    spoiled[field] = value

    with pytest.raises(CodecError, match=f"Kinds.{field}"):
        decode(codec, spoiled)


def test_ws2_an_int_is_a_float(codec):
    """msgpack writes 2.0 as an int from many languages; the field is a float
    and the value is exactly one."""
    spoiled = raw(codec)
    spoiled["x"] = 2

    decoded = decode(codec, spoiled)

    assert decoded.x == 2.0 and type(decoded.x) is float


@pytest.mark.parametrize("data", [b"\xc1", b"\x93\x01", b"", b"\x81\xa4type\xa5Inner\x00"])
def test_ws2_bytes_that_are_not_one_msgpack_message_are_a_codec_error(codec, data):
    """Not msgpack at all, truncated, empty, or a message followed by more."""
    with pytest.raises(CodecError):
        codec.decode(data)


# --- WS3: arrays --------------------------------------------------------------


@pytest.mark.parametrize(
    "array",
    [
        {"dtype": "|O", "shape": [1], "data": b"\x00" * 8},  # object, by its string
        {"dtype": "<f8", "shape": [2, 3], "data": b"\x00" * 8},  # too few bytes
        {"dtype": "<f8", "shape": [1], "data": b"\x00" * 16},  # too many
        {"dtype": "<f8", "shape": [-1], "data": b"\x00" * 8},  # a negative extent
        {"dtype": "<f8", "shape": 1, "data": b"\x00" * 8},  # a shape that is not a list
        {"dtype": "<f8", "shape": ["1"], "data": b"\x00" * 8},
        {"dtype": "not a dtype", "shape": [1], "data": b"\x00" * 8},
        {"dtype": 8, "shape": [1], "data": b"\x00" * 8},
        {"dtype": "<f8", "shape": [1], "data": "\x00" * 8},  # data as a string
        {"dtype": "<f8", "shape": [1]},  # no data
        {"dtype": "<f8", "shape": [1], "data": b"\x00" * 8, "order": "F"},
        [1.0, 2.0],  # not a map
        b"\x00" * 8,
    ],
    ids=[
        "object-dtype",
        "short",
        "long",
        "negative",
        "scalar-shape",
        "string-extent",
        "bad-dtype",
        "numeric-dtype",
        "string-data",
        "no-data",
        "extra-key",
        "list",
        "bytes",
    ],
)
def test_ws3_a_malformed_array_is_a_codec_error_naming_its_field(codec, array):
    spoiled = raw(codec)
    spoiled["data"] = array

    with pytest.raises(CodecError, match="Kinds.data"):
        decode(codec, spoiled)


def test_ws3_a_structured_dtype_holding_objects_is_refused(codec):
    """The object hides inside a field of the record, past a check of the
    dtype's kind ("V", not "O")."""
    assert np.dtype("O,i4").kind == "V" and np.dtype("O,i4").hasobject
    spoiled = raw(codec)
    spoiled["data"] = {"dtype": "O,i4", "shape": [1], "data": b"\x00" * 12}

    with pytest.raises(CodecError, match="Kinds.data"):
        decode(codec, spoiled)
