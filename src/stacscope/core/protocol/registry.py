"""Registry of message types by stable wire name."""


class MessageRegistry:
    """Maps a wire name to a message class. A class must be registered to
    cross any boundary; its name is its class name, unique per registry."""

    def __init__(self) -> None:
        self._by_name: dict[str, type] = {}

    def register[T: type](self, cls: T) -> T:
        """Class decorator."""
        name = cls.__name__
        if name in self._by_name:
            raise ValueError(f"message name {name!r} is already registered")
        self._by_name[name] = cls
        return cls

    def names(self) -> list[str]:
        return list(self._by_name)

    def types(self) -> list[type]:
        return list(self._by_name.values())

    def lookup(self, name: str) -> type | None:
        return self._by_name.get(name)


MESSAGES = MessageRegistry()
"""Every message type in STACScope."""

message = MESSAGES.register
