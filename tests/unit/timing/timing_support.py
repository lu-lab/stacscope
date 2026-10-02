"""Shared rig for the Coordinator tests (C1–C5 and D4–D6).

Run against ``SimulatedTimingSource``, whose phases are no-ops: nothing here
may depend on the source being an AO board.

This holds no fixtures, so it is a plain module rather than a ``conftest.py``.
A bare ``from conftest import ...`` binds to whichever of this suite's several
``conftest.py`` files was imported first, which depends on how pytest was
invoked; see the note in ``tests/integration/process_support.py``.
"""

import asyncio

from stacscope.adapters.simulated import SimulatedTimingSource, VirtualClock
from stacscope.core.actors import Actor, Fault, Subscribe, ask, spawn
from stacscope.core.protocol import StartAcquisition
from stacscope.core.timing import Coordinator, DeviceSpec

READY_TIMEOUT = 2.0
PERIOD = 1.0


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)

    def faults(self):
        return [m for m in self.heard if isinstance(m, Fault)]


class Factory:
    """Makes a fresh instance per launch: the n-th launch uses the n-th maker
    (the last one repeats). Keeps every instance made. Launches are counted,
    not instances, so a maker that raises still uses up its turn."""

    def __init__(self, *makers):
        self.makers = makers
        self.made = []
        self.launches = 0

    def __call__(self):
        maker = self.makers[min(self.launches, len(self.makers) - 1)]
        self.launches += 1
        self.made.append(maker())
        return self.made[-1]


class Dev:
    """A participant whose ``DeviceSpec`` is not the rig's default — a longer
    timeout of its own, or ``required=False``."""

    def __init__(self, instance, **spec):
        self.instance = instance
        self.spec = spec


class Rig:
    """A Coordinator with a simulated source and named simulated participants."""

    def __init__(self, source=None, **participants):
        self.clock = VirtualClock()
        self.source = source or SimulatedTimingSource(cycle_period=PERIOD)
        self.devices = {
            name: dev.instance if isinstance(dev, Dev) else dev
            for name, dev in participants.items()
        }
        self.coordinator = Coordinator(
            source=self._spec("source", self.source),
            participants=[self._spec(name, dev) for name, dev in participants.items()],
        )
        self.ref = spawn(self.coordinator, clock=self.clock, name="coordinator")
        self.listener = Listener()
        self.ref.tell(Subscribe(spawn(self.listener)))

    @staticmethod
    def _spec(name, dev):
        overrides = dev.spec if isinstance(dev, Dev) else {}
        instance = dev.instance if isinstance(dev, Dev) else dev
        factory = instance if isinstance(instance, Factory) else (lambda: instance)
        return DeviceSpec(
            name=name, factory=factory, **{"ready_timeout": READY_TIMEOUT, **overrides}
        )

    def request(self, msg):
        """Ask from a client, as a task: replies may need virtual time to pass."""
        return asyncio.create_task(ask(self.ref, msg, timeout=100.0, clock=self.clock))

    async def call(self, msg):
        """Ask and let the reply arrive, when no virtual time needs to pass."""
        replying = self.request(msg)
        await self.clock.advance(0)
        return await replying

    async def start(self, cycles=-1):
        return await self.call(StartAcquisition(cycles, "run-01"))
