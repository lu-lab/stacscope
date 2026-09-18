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
        carry is a definition error, not a runtime surprise."""
        self._registry = registry
        self._hints: dict[type, dict[str, Any]] = {}
        for cls in registry.types():
            if not dataclasses.is_dataclass(cls):
                raise CodecError(f"{cls.__name__} is not a dataclass")
            hints = get_type_hints(cls)
            for field in dataclasses.fields(cls):
                self._check(hints[field.name], f"{cls.__name__}.{field.name}")
            self._hints[cls] = hints

    def encode(self, msg: object) -> bytes:
        return msgpack.packb(self._encode_message(msg), use_bin_type=True)

    def decode(self, data: bytes, *, resolve: Resolve | None = None) -> object:
        """``resolve`` turns an address back into a ref; required only if the
        message carries one."""
        return self._decode_message(msgpack.unpackb(data, raw=False), None, resolve)

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
        if cls not in self._hints:
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
        if type(value) in self._hints:
            return self._encode_message(value)
        raise CodecError(f"{type(value).__name__} cannot cross a boundary")

    # --- decode (driven by annotations) ------------------------------------

    def _decode_message(self, raw: object, expected: type | None, resolve: Resolve | None) -> object:
        if not isinstance(raw, dict) or not isinstance(raw.get(_TYPE), str):
            raise CodecError("not an encoded message")
        cls = self._registry.lookup(raw[_TYPE])
        if cls is None:
            raise CodecError(f"unknown message type {raw[_TYPE]!r}")
        if expected is not None and cls is not expected:
            raise CodecError(f"expected {expected.__name__}, got {cls.__name__}")
        hints = self._hints[cls]
        return cls(
            **{
                f.name: self._decode_value(raw[f.name], hints[f.name], resolve)
                for f in dataclasses.fields(cls)
            }
        )

    def _decode_value(self, raw: object, tp: Any, resolve: Resolve | None) -> object:
        origin = get_origin(tp)
        if origin in (Union, types.UnionType):
            if raw is None:
                return None
            [arm] = [a for a in get_args(tp) if a is not type(None)]
            return self._decode_value(raw, arm, resolve)
        if origin is tuple:
            item = get_args(tp)[0]
            return tuple(self._decode_value(r, item, resolve) for r in raw)
        if tp is float:
            return float(raw)
        if tp in _SCALARS:
            return raw
        if isinstance(tp, type) and issubclass(tp, Enum):
            return tp(raw)
        if tp is np.ndarray:
            dtype = np.dtype(raw["dtype"])
            return np.frombuffer(raw["data"], dtype=dtype).reshape(raw["shape"]).copy()
        if tp is ActorRef:
            ref = resolve(raw) if resolve is not None else None
            if ref is None:
                raise CodecError(f"cannot resolve actor address {raw!r}")
            return ref
        return self._decode_message(raw, tp, resolve)
