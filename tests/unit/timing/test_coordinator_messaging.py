"""CM15–CM31 -- the Coordinator under the parent/child rule.

Its devices are its children: it commands them, they answer and report to
it by its address, and it waits on them only as a state, never inside a
handler. Clients outside the tree -- the rig, the gateway, these tests --
subscribe to it and hear everything; a device subscribed to it hears the
timing phases and nothing else, so no device ever learns of a sibling.

Run against ``SimulatedTimingSource``, whose phases are no-ops.
"""

from timing_support import Rig

from stacscope.adapters.simulated import Fail, FaultyDevice, SimulatedDevice
from stacscope.core.actors import Fault, Subscribe
from stacscope.core.protocol import AcquisitionStarted


class Spy(SimulatedDevice):
    """A participant that keeps every envelope delivered to it, before its
    runtime sorts them -- a subscription included, which never reaches
    ``handle``."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.received = []

    def _receive(self, envelope, priority):
        self.received.append(envelope)
        super()._receive(envelope, priority)


# --- CM23: the Coordinator subscribes to nothing --------------------------------


async def test_cm23_the_coordinator_subscribes_to_no_device():
    """Devices report to their parent by its address, so there is nothing to
    subscribe to. A subscription would also put the Coordinator on a list a
    device publishes to, which the rule keeps for clients."""
    spy = Spy()
    rig = Rig(camera=spy)
    await rig.clock.advance(0)
    await rig.start()

    assert not [e for e in spy.received if isinstance(e.msg, Subscribe)]


async def test_cm23_a_devices_fault_reaches_the_coordinators_clients_once():
    """Reported up by address and published by the root, once. Had a device
    subscribed to the Coordinator heard its Faults, it would pass each back
    up, and the two would trade it forever."""
    rig = Rig(camera=FaultyDevice(Fail.REPORT, on="StartDevice"))
    await rig.clock.advance(0)

    assert await rig.start() == AcquisitionStarted()
    await rig.clock.advance(1)

    [fault] = rig.listener.faults()
    assert fault == Fault(rig.coordinator.participants["camera"], "the driver reported a warning", False)
