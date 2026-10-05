"""Strict message codec: registered dataclass ↔ msgpack bytes.

The enforcement point for invariant 4. A message is a map of its fields plus a
``type`` key holding its registered name — plain msgpack, readable by any
language. Only these may cross, and anything else raises ``CodecError``:

- ``None``, ``bool``, ``int``, ``float``, ``str``, ``bytes``
- ``Enum`` members, as their value
- ``numpy.ndarray`` without object dtype, as ``{dtype, shape, data}``
- ``ActorRef``, as its address string
- ``tuple[T, ...]`` of the above; ``T | None``
- other registered messages, nested

Decoding is driven by the field annotations, so the wire carries no Python
type information beyond the message name.
"""

import dataclasses
import types
from collections.abc import Callable
from enum import Enum
from typing import Any, Union, get_args, get_origin, get_type_hints

import msgpack
import numpy as np

from stacscope.core.actors import ActorRef
from stacscope.core.protocol.registry import MessageRegistry

_TYPE = "type"
_SCALARS = (bool, int, float, str, bytes)

type Resolve = Callable[[str], ActorRef | None]


class CodecError(Exception):
    """A message cannot cross a boundary."""


class Codec:
    def __init__(self, registry: MessageRegistry) -> None:
        """Checks every registered type up front: a field the codec cannot
        carry is a definition error, not a runtime surprise. A type
        registered later, as an adapter's messages are when its module is
        imported, is checked the same way the first time it is encoded or
        decoded."""
        self._registry = registry
        self._hints: dict[type, dict[str, Any]] = {}
        for cls in registry.types():
            self._learn(cls)

    def _learn(self, cls: type) -> dict[str, Any]:
        """Check a registered type's fields, and keep its annotations."""
        if not dataclasses.is_dataclass(cls):
            raise CodecError(f"{cls.__name__} is not a dataclass")
        hints = get_type_hints(cls)
        for field in dataclasses.fields(cls):
            self._check(hints[field.name], f"{cls.__name__}.{field.name}")
        self._hints[cls] = hints
        return hints

    def _hints_for(self, cls: type) -> dict[str, Any] | None:
        """The annotations of ``cls``, or ``None`` if it is not registered."""
        hints = self._hints.get(cls)
        if hints is None and self._registry.lookup(cls.__name__) is cls:
            # Registered after this codec was built: a process child builds
            # its codec before unpickling the device, whose module registers
            # the device's own messages.
            hints = self._learn(cls)
        return hints

    def encode(self, msg: object) -> bytes:
        return msgpack.packb(self._encode_message(msg), use_bin_type=True)

    def decode(self, data: bytes, *, resolve: Resolve | None = None) -> object:
        """``resolve`` turns an address back into a ref; required only if the
        message carries one. Anything that is not exactly one well-formed
        message -- a field missing or extra, a value of the wrong type --
        raises ``CodecError``."""
        try:
            raw = msgpack.unpackb(data, raw=False)
        except Exception as exc:  # msgpack's errors share no base worth naming
            raise CodecError(f"not msgpack: {exc!r}") from None
        return self._decode_message(raw, None, resolve)

    # --- annotation check --------------------------------------------------

    def _check(self, tp: Any, where: str) -> None:
        origin = get_origin(tp)
        if origin in (Union, types.UnionType):
            arms = [a for a in get_args(tp) if a is not type(None)]
            if len(arms) != 1:
                raise CodecError(f"{where}: only 'T | None' unions are supported, not {tp}")
            self._check(arms[0], where)
        elif origin is tuple:
            args = get_args(tp)
            if len(args) != 2 or args[1] is not Ellipsis:
                raise CodecError(f"{where}: only tuple[T, ...] is supported, not {tp}")
            self._check(args[0], where)
        elif tp in _SCALARS or tp is np.ndarray or tp is ActorRef:
            pass
        elif isinstance(tp, type) and issubclass(tp, Enum):
            pass
        elif tp not in self._hints and tp not in self._registry.types():
            raise CodecError(f"{where}: {tp!r} cannot cross a boundary")

    # --- encode (driven by values) -----------------------------------------

    def _encode_message(self, msg: object) -> dict[str, Any]:
        cls = type(msg)
        if self._hints_for(cls) is None:
            raise CodecError(f"{cls.__name__} is not a registered message")
        encoded = {_TYPE: cls.__name__}
        for field in dataclasses.fields(cls):
            try:
                encoded[field.name] = self._encode_value(getattr(msg, field.name))
            except CodecError as exc:
                raise CodecError(f"{cls.__name__}.{field.name}: {exc}") from None
        return encoded

    def _encode_value(self, value: object) -> object:
        if value is None or isinstance(value, _SCALARS):
            return value
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, np.ndarray):
            if value.dtype.hasobject:
                raise CodecError("object arrays cannot cross a boundary")
            return {
                "dtype": value.dtype.str,
                "shape": list(value.shape),
                "data": np.ascontiguousarray(value).tobytes(),
            }
        if isinstance(value, ActorRef):
            return value.address
        if isinstance(value, tuple):
            return [self._encode_value(v) for v in value]
        if self._hints_for(type(value)) is not None:
            return self._encode_message(value)
        raise CodecError(f"{type(value).__name__} cannot cross a boundary")

    # --- decode (driven by annotations) ------------------------------------

    # Nothing decoded is trusted: frames reach the gateway from any client in
    # any language. Every check raises CodecError, the one error a link skips
    # and the gateway answers, so a bad message never closes a connection and
    # never reaches an actor holding a value its type does not allow.

    def _decode_message(self, raw: object, expected: type | None, resolve: Resolve | None) -> object:
        if not isinstance(raw, dict) or not isinstance(raw.get(_TYPE), str):
            raise CodecError("not an encoded message")
        cls = self._registry.lookup(raw[_TYPE])
        if cls is None:
            raise CodecError(f"unknown message type {raw[_TYPE]!r}")
        if expected is not None and cls is not expected:
            raise CodecError(f"expected {expected.__name__}, got {cls.__name__}")
        hints = self._hints_for(cls)
        names = [f.name for f in dataclasses.fields(cls)]
        # An extra field is refused, not dropped: a client written against
        # another version of a message would believe it had set something.
        if extra := sorted(map(str, raw.keys() - {_TYPE, *names})):
            raise CodecError(f"{cls.__name__} has no field {', '.join(extra)}")
        fields = {}
        for name in names:
            if name not in raw:
                raise CodecError(f"{cls.__name__}.{name} is missing")
            try:
                fields[name] = self._decode_value(raw[name], hints[name], resolve)
            except CodecError as exc:
                raise CodecError(f"{cls.__name__}.{name}: {exc}") from None
        return cls(**fields)

    def _decode_value(self, raw: object, tp: Any, resolve: Resolve | None) -> object:
        origin = get_origin(tp)
        if origin in (Union, types.UnionType):
            if raw is None:
                return None
            [arm] = [a for a in get_args(tp) if a is not type(None)]
            return self._decode_value(raw, arm, resolve)
        if origin is tuple:
            if not isinstance(raw, list):
                raise CodecError(f"expected a list, got {_kind(raw)}")
            item = get_args(tp)[0]
            return tuple(self._decode_value(r, item, resolve) for r in raw)
        # bool is a subclass of int to Python, but never one on the wire:
        # True for a cycle count is a client's mistake, not a 1.
        if tp is float:
            if isinstance(raw, bool) or not isinstance(raw, (int, float)):
                raise CodecError(f"expected a number, got {_kind(raw)}")
            return float(raw)  # an int is a float: many languages write 2.0 as 2
        if tp in _SCALARS:
            if type(raw) is not tp:
                raise CodecError(f"expected {tp.__name__}, got {_kind(raw)}")
            return raw
        if isinstance(tp, type) and issubclass(tp, Enum):
            try:
                return tp(raw)
            except (ValueError, TypeError):
                values = ", ".join(repr(m.value) for m in tp)
                raise CodecError(f"{raw!r:.40} is not one of {values}") from None
        if tp is np.ndarray:
            return _decode_array(raw)
        if tp is ActorRef:
            if not isinstance(raw, str):
                raise CodecError(f"expected an actor address, got {_kind(raw)}")
            ref = resolve(raw) if resolve is not None else None
            if ref is None:
                raise CodecError(f"cannot resolve actor address {raw!r}")
            return ref
        return self._decode_message(raw, tp, resolve)


_ARRAY_KEYS = {"dtype", "shape", "data"}


def _decode_array(raw: object) -> np.ndarray:
    if not isinstance(raw, dict) or raw.keys() != _ARRAY_KEYS:
        raise CodecError(f"expected an array as a map of dtype, shape and data, got {_kind(raw)}")
    if not isinstance(raw["dtype"], str):
        raise CodecError(f"an array's dtype is a string, not {_kind(raw['dtype'])}")
    try:
        dtype = np.dtype(raw["dtype"])
    except (TypeError, ValueError):
        raise CodecError(f"unknown dtype {raw['dtype']!r:.40}") from None
    # hasobject, not kind: an object can hide in a field of a record dtype.
    if dtype.hasobject:
        raise CodecError("object arrays cannot cross a boundary")
    shape = raw["shape"]
    if not isinstance(shape, list) or not all(type(n) is int and n >= 0 for n in shape):
        raise CodecError(f"an array's shape is a list of extents, not {shape!r:.40}")
    data = raw["data"]
    if not isinstance(data, bytes):
        raise CodecError(f"an array's data is bytes, not {_kind(data)}")
    size = dtype.itemsize
    for n in shape:
        size *= n
    if len(data) != size:
        raise CodecError(f"{len(data)} bytes of data for {size} bytes of {dtype} in shape {shape}")
    try:
        return np.frombuffer(data, dtype=dtype).reshape(shape).copy()
    except ValueError as exc:  # a zero-size dtype, for one
        raise CodecError(f"not an array: {exc}") from None


def _kind(raw: object) -> str:
    return f"{type(raw).__name__} {raw!r:.40}"
