"""CM58 -- the exit waits for the child's mail, across a process boundary.

In-process, the rule is enough on its own (CM52). Across a boundary it also
depends on the order things reach the parent's mailbox: the device's last
answer crosses the link, while its death is noticed by the process host. The
host waits for the link to close before reporting, and the link closes only
once its last frame is delivered. So the answer is queued by the time the exit
arrives, and the rule puts the exit behind it.
"""

import asyncio
from functools import partial

import pytest
from process_support import PATIENT_HEARTBEAT, until

from stacscope.adapters.simulated import Fail, FaultyDevice
from stacscope.core.actors import Actor, ChildExited, spawn
from stacscope.core.protocol import DeviceReady, StartDevice

pytestmark = pytest.mark.timeout(30)


class Busy:
    """To the parent: stay in this handler until ``release`` is set."""


class Parent(Actor):
    def __init__(self) -> None:
        super().__init__()
        self.heard: list[object] = []
        self.release = asyncio.Event()

    async def handle(self, msg):
        self.heard.append(msg)
        if isinstance(msg, Busy):
            # Bounded, as every wait in this suite is.
            await asyncio.wait_for(self.release.wait(), 10.0)


async def test_cm58_a_process_childs_last_answer_is_handled_before_its_exit(host):
    """The device answers ``StartDevice`` and stops (``Fail.QUIT``): the answer
    and the death leave the child moments apart. The parent is busy until
    both have arrived, so both are queued, and the exit, sent URGENT, would
    overtake the answer without the rule."""
    parent = Parent()
    spawn(parent, name="engine")
    await asyncio.sleep(0)
    device = await host.launch(
        parent,
        name="camera",
        factory=partial(FaultyDevice, Fail.QUIT),
        heartbeat=PATIENT_HEARTBEAT,
    )
    parent.ref.tell(Busy())

    device.tell(StartDevice("", 1), sender=parent.ref)
    # The host releases the name just before it sends the exit.
    assert await until(lambda: host.running() == 0, deadline=5.0)
    parent.release.set()
    assert await until(lambda: any(isinstance(m, ChildExited) for m in parent.heard))

    _, answer, exited = parent.heard
    assert isinstance(answer, DeviceReady)
    assert isinstance(exited, ChildExited) and exited.error is None
