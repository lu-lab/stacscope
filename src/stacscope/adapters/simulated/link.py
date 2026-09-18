"""A transport wrapper that can be severed."""

from stacscope.core.actors import Envelope, Priority, Transport


class SimulatedLink:
    """Delivers through ``inner`` until severed, then silently drops everything.

    From outside, a frozen process, a killed process and a broken network link
    all look like this: silence. It is what heartbeat liveness must detect.
    """

    def __init__(self, inner: Transport) -> None:
        self._inner = inner
        self.severed = False

    def sever(self) -> None:
        self.severed = True

    def deliver(self, envelope: Envelope, priority: Priority) -> None:
        if not self.severed:
            self._inner.deliver(envelope, priority)
