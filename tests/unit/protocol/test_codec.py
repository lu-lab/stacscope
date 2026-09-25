"""P1 — every message type round-trips through the serializer unchanged.

The test that keeps the process and network transports adoptable (invariant 4).
Samples are generated from each message's field annotations, so a newly
registered type is covered automatically — and a field of a type the codec
cannot carry fails here rather than at the first process boundary.
"""

import asyncio
import dataclasses
import inspect
import types
from enum import Enum
from typing import Union, get_args, get_origin, get_type_hints

import msgpack
import numpy as np
import pytest

import stacscope.core.actors.messages as runtime_messages
import stacscope.core.protocol.messages as protocol_messages
from stacscope.core.actors import ActorRef, ChildExited
from stacscope.core.protocol import MESSAGES, MessageRegistry, StartAcquisition
from stacscope.core.transport import Codec, CodecError

REF = ActorRef(transport=None, address="coordinator/camera0")
RESOLVE = {REF.address: REF}.get


def sample(tp):
    """An instance of annotation ``tp`` with non-default, non-trivial content."""
    origin = get_origin(tp)
    if origin in (Union, types.UnionType):
        [arm] = [a for a in get_args(tp) if a is not type(None)]
        return sample(arm)
    if origin is tuple:
        item, _ = get_args(tp)
        return (sample(item), sample(item))
    if isinstance(tp, type) and issubclass(tp, Enum):
        return list(tp)[-1]
    if isinstance(tp, type) and dataclasses.is_dataclass(tp):
        hints = get_type_hints(tp)
        return tp(**{f.name: sample(hints[f.name]) for f in dataclasses.fields(tp)})
    samples = {
        bool: True,
        int: -3,
        float: 2.5,
        str: "run-01",
        bytes: b"\x00\xff",
        np.ndarray: np.arange(12.0).reshape(3, 4),
        ActorRef: REF,
    }
    if tp not in samples:
        pytest.fail(f"no sample for field type {tp!r}: can the codec carry it?")
    return samples[tp]


def same(a, b):
    """Deep equality that understands arrays (dataclass ``==`` does not)."""
    if isinstance(a, np.ndarray):
        return (
            isinstance(b, np.ndarray)
            and a.dtype == b.dtype
            and a.shape == b.shape
            and np.array_equal(a, b)
        )
    if dataclasses.is_dataclass(a) and not isinstance(a, type):
        return type(a) is type(b) and all(
            same(getattr(a, f.name), getattr(b, f.name)) for f in dataclasses.fields(a)
        )
    if isinstance(a, tuple):
        return isinstance(b, tuple) and len(a) == len(b) and all(map(same, a, b))
    return type(a) is type(b) and a == b


@pytest.fixture
def codec():
    return Codec(MESSAGES)


# --- every registered type ---------------------------------------------------


def test_the_registry_holds_the_runtime_and_protocol_messages():
    expected = {
        "ChildExited", "Heartbeat", "HeartbeatReply",
        "StartAcquisition", "StopAcquisition",
        "StartDevice", "DeviceReady", "StopDevice", "DeviceStopped",
        "Go", "Finish", "SourcePhase", "SourceFinished",
    }  # fmt: skip
    assert expected <= set(MESSAGES.names())


@pytest.mark.parametrize("cls", MESSAGES.types(), ids=lambda c: c.__name__)
def test_every_message_type_round_trips_unchanged(codec, cls):
    msg = sample(cls)

    assert same(codec.decode(codec.encode(msg), resolve=RESOLVE), msg)


@pytest.mark.parametrize("module", [runtime_messages, protocol_messages], ids=lambda m: m.__name__)
def test_every_message_class_is_registered_and_frozen(module):
    classes = [
        c
        for _, c in inspect.getmembers(module, inspect.isclass)
        if c.__module__ == module.__name__ and dataclasses.is_dataclass(c)
    ]
    assert classes
    for cls in classes:
        assert cls in MESSAGES.types(), f"{cls.__name__} is not registered"
        assert cls.__dataclass_params__.frozen, f"{cls.__name__} is not frozen"


def test_optional_fields_round_trip_none(codec):
    msg = ChildExited(REF, None)

    assert codec.decode(codec.encode(msg), resolve=RESOLVE) == msg


# --- arrays ------------------------------------------------------------------

arrays = MessageRegistry()


@arrays.register
@dataclasses.dataclass(frozen=True, slots=True, eq=False)
class Waveform:
    data: np.ndarray


@pytest.mark.parametrize(
    "data",
    [
        np.linspace(-5.0, 5.0, 2000).reshape(5, 400),  # AO: channels x samples
        np.array([[0, 1, 1, 0]] * 3, dtype=np.uint8),  # digital lines
        np.array([True, False, True]),
        np.arange(12, dtype=">i4").reshape(3, 4),  # non-native byte order
        np.arange(12.0).reshape(3, 4).T,  # non-contiguous
        np.zeros((5, 0)),  # an empty waveform
    ],
    ids=["ao", "digital", "bool", "big-endian", "transposed", "empty"],
)
def test_arrays_round_trip_with_dtype_and_shape(data):
    codec = Codec(arrays)

    decoded = codec.decode(codec.encode(Waveform(data)))

    assert same(decoded.data, data)


def test_object_arrays_are_rejected():
    with pytest.raises(CodecError):
        Codec(arrays).encode(Waveform(np.array([object()])))


# --- invariant 4: nothing live crosses ---------------------------------------


def test_live_objects_are_rejected_at_encode(codec):
    smuggled = StartAcquisition(cycles=asyncio.Event(), root_filename="run-01")

    with pytest.raises(CodecError):
        codec.encode(smuggled)


def test_unregistered_messages_are_rejected(codec):
    @dataclasses.dataclass(frozen=True)
    class Stray:
        n: int

    with pytest.raises(CodecError):
        codec.encode(Stray(1))


def test_a_message_type_with_an_uncarriable_field_is_rejected_by_the_codec():
    bad = MessageRegistry()

    @bad.register
    @dataclasses.dataclass(frozen=True)
    class HoldsAnything:
        payload: object

    with pytest.raises(CodecError):
        Codec(bad)


def test_a_type_registered_after_the_codec_was_built_still_crosses():
    """An adapter's own messages are registered when its module is imported,
    and a process child imports the device's module only when it unpickles
    the factory, after its codec is built. A codec that knew only the types
    registered before it would refuse every settings reply the device sends,
    and every request it is sent."""
    registry = MessageRegistry()
    codec = Codec(registry)

    @registry.register
    @dataclasses.dataclass(frozen=True)
    class Late:
        name: str
        value: float
        label: str | None

    assert codec.decode(codec.encode(Late("EXPOSURE TIME", 0.01, None))) == Late(
        "EXPOSURE TIME", 0.01, None
    )


def test_a_late_type_with_an_uncarriable_field_is_rejected_when_first_met():
    """A type registered late is checked as strictly as one registered
    early, only later: at its first encode or decode, not at its first
    field that happens to hold something odd."""
    registry = MessageRegistry()
    codec = Codec(registry)

    @registry.register
    @dataclasses.dataclass(frozen=True)
    class HoldsAnything:
        payload: object

    with pytest.raises(CodecError, match="HoldsAnything.payload"):
        codec.encode(HoldsAnything(1))
    with pytest.raises(CodecError, match="HoldsAnything.payload"):
        codec.decode(msgpack.packb({"type": "HoldsAnything", "payload": 1}))


def test_duplicate_message_names_are_rejected():
    registry = MessageRegistry()

    @registry.register
    @dataclasses.dataclass(frozen=True)
    class Twin:
        pass

    with pytest.raises(ValueError):

        @registry.register
        @dataclasses.dataclass(frozen=True)
        class Twin:  # noqa: F811
            pass


# --- the wire ----------------------------------------------------------------


def test_the_wire_is_plain_msgpack_readable_without_stacscope(codec):
    wire = msgpack.unpackb(codec.encode(StartAcquisition(cycles=-1, root_filename="run-01")))

    assert wire == {"type": "StartAcquisition", "cycles": -1, "root_filename": "run-01"}


def test_a_ref_travels_as_its_address(codec):
    data = codec.encode(ChildExited(REF, "heartbeat timeout"))

    assert msgpack.unpackb(data)["child"] == "coordinator/camera0"
    assert codec.decode(data, resolve=RESOLVE).child == REF


def test_an_unresolvable_address_is_an_error(codec):
    data = codec.encode(ChildExited(REF, None))

    with pytest.raises(CodecError):
        codec.decode(data, resolve=lambda address: None)
