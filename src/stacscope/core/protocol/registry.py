"""Registry of message types by stable wire name."""

from collections.abc import Callable
from typing import overload


class MessageRegistry:
    """Maps a wire name to a message class. A class must be registered to
    cross any boundary; its name is its class name, unique per registry."""

    def __init__(self) -> None:
        self._by_name: dict[str, type] = {}
        self._client: set[type] = set()

    @overload
    def register[T: type](self, cls: T, /) -> T: ...
    @overload
    def register[T: type](self, *, client: bool = False) -> Callable[[T], T]: ...

    def register(self, cls=None, /, *, client=False):
        """Class decorator, used bare or as ``register(client=True)``.
        ``client`` marks a type that clients outside the engine may send
        through the gateway; the gateway refuses every other type."""

        def add(cls):
            name = cls.__name__
            if name in self._by_name:
                raise ValueError(f"message name {name!r} is already registered")
            self._by_name[name] = cls
            if client:
                self._client.add(cls)
            return cls

        return add if cls is None else add(cls)

    def names(self) -> list[str]:
        return list(self._by_name)

    def types(self) -> list[type]:
        return list(self._by_name.values())

    def lookup(self, name: str) -> type | None:
        return self._by_name.get(name)

    def client_sendable(self, cls: type) -> bool:
        """Whether ``cls`` was registered here with ``client=True``."""
        return cls in self._client


MESSAGES = MessageRegistry()
"""Every message type in STACScope."""

message = MESSAGES.register
"""Registers a message type in ``MESSAGES``: ``@message``, or
``@message(client=True)`` for one clients may send."""
