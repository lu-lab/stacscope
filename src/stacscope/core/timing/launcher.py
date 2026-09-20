"""Where a device runs -- decided by config, once, at the composition root.

Placement is not an architectural choice. A device in another process speaks
the same protocol through the same kind of ``ActorRef``, so the Coordinator
asks a launcher for a device and never learns which it got. That is what keeps
the fault isolation a vendor driver needs from leaking into the control flow.
"""

from enum import Enum
from typing import TYPE_CHECKING, Protocol

from stacscope.core.actors import Actor, ActorRef, HeartbeatPolicy
from stacscope.core.transport import LaunchFailed

if TYPE_CHECKING:
    from collections.abc import Callable

    from stacscope.core.timing.coordinator import DeviceSpec


class Placement(Enum):
    IN_PROCESS = "in_process"
    """With the engine. Fastest, and a segfault takes everything with it."""
    PROCESS = "process"
    """In a process of its own, for a driver that cannot be trusted not to."""


class Host(Protocol):
    """Whatever can start an actor somewhere else. ``ProcessHost`` now; a
    machine away later, with nothing here to change."""

    async def launch(
        self,
        parent: Actor,
        *,
        name: str,
        factory: "Callable[[], Actor]",
        heartbeat: HeartbeatPolicy | None = None,
    ) -> ActorRef: ...


class Launcher:
    """Starts each device where its spec says."""

    def __init__(self, host: Host | None = None) -> None:
        self._host = host

    async def launch(self, parent: Actor, spec: "DeviceSpec") -> ActorRef:
        if spec.placement is Placement.IN_PROCESS:
            return parent.spawn_child(spec.factory(), name=spec.name, heartbeat=spec.heartbeat)
        if self._host is None:
            raise LaunchFailed(
                f"{spec.name} is configured to run in its own process, "
                "but this engine was built without a host to run it in"
            )
        return await self._host.launch(
            parent, name=spec.name, factory=spec.factory, heartbeat=spec.heartbeat
        )
