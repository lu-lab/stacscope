"""DP5–DP14 -- a device's properties, through the core messages.

Every ``TimedDevice`` answers ``GetProperties``, ``GetProperty`` and
``SetProperty`` from a table its adapter declares, so that a new adapter
writes a table rather than message classes, and no client needs to import an
adapter to configure one (user, 2026-10-05). The adapter has two hooks: read
the table as it stands, and apply one value. Everything else is core's, and
is what these tests pin: the checks made before the adapter is called, the
answer of everything that moved, and publishing it.

The device here is a fake whose table exercises each rule: a number bounded
by another (exposure by the frame rate), a number of listed choices, a label,
a flag, a read-only value, and properties changeable only while idle. The
contract every real adapter's table must also meet is
``tests/contract/property_contract.py``.
"""

import uuid

import pytest

from stacscope.adapters.simulated import SimulatedCamera, VirtualClock
from stacscope.core.actors import Actor, ChildExited, Fault, Rejected, Subscribe, ask, spawn
from stacscope.core.buffer import ImageBuffer
from stacscope.core.devices import DeviceState, TimedDevice
from stacscope.core.protocol import (
    DeviceReady,
    DeviceStopped,
    GetProperties,
    GetProperty,
    Properties,
    PropertiesChanged,
    Property,
    PropertyAccess,
    PropertyKind,
    SetProperty,
    StartDevice,
    StopDevice,
)

NUMBER, LABEL, FLAG = PropertyKind.NUMBER, PropertyKind.LABEL, PropertyKind.FLAG
IDLE, ALWAYS, READ_ONLY = PropertyAccess.IDLE, PropertyAccess.ALWAYS, PropertyAccess.READ_ONLY


class Boom(RuntimeError):
    """A driver refusing a value."""


class Camera(TimedDevice):
    """A table over plain values, recording every read and write."""

    def __init__(self, *, settings=(), fail=None, hold_arm=False, hold_disarm=False):
        super().__init__()
        self.launch_settings = tuple(settings)
        self.values = {
            "frame_rate": 10.0,
            "exposure": 0.05,
            "binning": 1.0,
            "mode": "LINEAR",
            "cooler": False,
        }
        self.reads = 0
        self.writes = []
        self.fail = fail or {}
        self.hold_arm = hold_arm
        self.hold_disarm = hold_disarm

    async def read_properties(self):
        self.reads += 1
        v = self.values
        return [
            Property("frame_rate", NUMBER, v["frame_rate"], "Hz", 1.0, 100.0, access=IDLE),
            Property("exposure", NUMBER, v["exposure"], "s", 0.001, 1 / v["frame_rate"], 0.001),
            Property("binning", NUMBER, v["binning"], choices=(1.0, 2.0, 4.0), access=IDLE),
            Property("mode", LABEL, v["mode"], labels=("LINEAR", "LOG"), access=IDLE),
            Property("cooler", FLAG, v["cooler"]),
            Property("temperature", NUMBER, -10.0, "°C", access=READ_ONLY),
        ]

    async def write_property(self, name, value):
        self.writes.append((name, value))
        if name in self.fail:
            raise Boom(self.fail[name])
        if name == "exposure":
            value = round(value, 3)  # the hardware's step
        self.values[name] = value
        if name == "frame_rate":  # a faster frame leaves less time to expose
            self.values["exposure"] = min(self.values["exposure"], 1 / value)

    async def arm(self, root_filename, cycles):
        if self.hold_arm:
            self.hold()

    async def disarm(self):
        if self.hold_disarm:
            self.hold()


class NoTable(TimedDevice):
    """A device whose adapter declares no properties: the default."""


class Listener(Actor):
    def __init__(self):
        super().__init__()
        self.heard = []

    async def handle(self, msg):
        self.heard.append(msg)
        await super().handle(msg)

    def of(self, cls):
        return [m for m in self.heard if isinstance(m, cls)]


class Rig:
    """The device under a parent, with a subscriber to the device itself."""

    def __init__(self, device=None, **kwargs):
        self.clock = VirtualClock()
        self.device = device if device is not None else Camera(**kwargs)
        self.parent = Listener()
        spawn(self.parent, clock=self.clock, name="parent")
        self.ref = self.parent.spawn_child(self.device, name="device")
        self.subscriber = Listener()
        self.ref.tell(Subscribe(spawn(self.subscriber, clock=self.clock, name="subscriber")))

    async def ask(self, msg):
        return await ask(self.ref, msg, timeout=1.0, clock=self.clock)

    async def settle(self):
        for _ in range(5):
            await self.clock.advance(0)

    async def table(self):
        return (await self.ask(GetProperties())).properties


# --- DP5: a device with no table ---------------------------------------------


async def test_dp5_a_device_with_no_table_answers_an_empty_map():
    rig = Rig(NoTable())

    assert await rig.ask(GetProperties()) == Properties({})


@pytest.mark.parametrize("msg", [GetProperty("exposure"), SetProperty("exposure", 0.01)])
async def test_dp5_a_device_with_no_table_refuses_every_name_naming_it(msg):
    rig = Rig(NoTable())

    reply = await rig.ask(msg)

    assert isinstance(reply, Rejected) and "exposure" in reply.reason


# --- DP6: reading ------------------------------------------------------------


async def test_dp6_get_properties_answers_the_whole_table_by_name_in_its_order():
    rig = Rig()

    table = await rig.table()

    assert list(table) == ["frame_rate", "exposure", "binning", "mode", "cooler", "temperature"]
    assert table["exposure"] == Property("exposure", NUMBER, 0.05, "s", 0.001, 0.1, 0.001)
    assert table["mode"].labels == ("LINEAR", "LOG") and table["mode"].access is IDLE


async def test_dp6_get_property_answers_one():
    rig = Rig()

    assert await rig.ask(GetProperty("binning")) == (await rig.table())["binning"]


# --- DP7: a set answers everything that moved ----------------------------------


async def test_dp7_a_set_answers_what_it_moved_and_nothing_else():
    """The frame rate bounds the exposure: setting it moves the exposure's
    maximum, and here its value too. A client holding the old limits would
    otherwise offer values the device now refuses."""
    rig = Rig()
    before = await rig.table()

    reply = await rig.ask(SetProperty("frame_rate", 50.0))

    assert isinstance(reply, PropertiesChanged)
    assert list(reply.properties) == ["frame_rate", "exposure"]
    assert reply.properties["frame_rate"].value == 50.0
    assert reply.properties["exposure"] == Property("exposure", NUMBER, 0.02, "s", 0.001, 0.02, 0.001)
    assert reply.properties["exposure"] != before["exposure"]


async def test_dp7_the_answer_holds_the_value_as_the_device_applied_it():
    rig = Rig()

    reply = await rig.ask(SetProperty("exposure", 0.0123))

    assert list(reply.properties) == ["exposure"]
    assert reply.properties["exposure"].value == 0.012  # rounded to its step


async def test_dp7_the_property_set_is_answered_even_if_nothing_moved():
    rig = Rig()

    reply = await rig.ask(SetProperty("cooler", False))

    assert reply == PropertiesChanged({"cooler": Property("cooler", FLAG, False)})


async def test_dp7_a_number_may_arrive_as_an_int():
    """In process, a caller may write 2 for 2.0; the adapter is handed a
    float either way, as it is from the wire."""
    rig = Rig()

    await rig.ask(SetProperty("binning", 2))

    assert rig.device.writes == [("binning", 2.0)] and type(rig.device.writes[0][1]) is float


# --- DP8: refused before the adapter is called ---------------------------------


@pytest.mark.parametrize(
    ("name", "value", "says"),
    [
        ("gain", 1.0, "gain"),  # no such property
        ("temperature", -20.0, "read-only"),
        ("exposure", "fast", "number"),  # a string for a number
        ("exposure", True, "number"),  # a flag is never a number
        ("cooler", 1.0, "flag"),
        ("cooler", "on", "flag"),
        ("mode", 2.0, "label"),
        ("mode", "CUBIC", "LINEAR"),  # names the labels it has
        ("exposure", 0.5, "0.1"),  # above the maximum, which it names
        ("exposure", 0.0001, "0.001"),  # below the minimum
        ("binning", 3.0, "4"),  # not among the choices, which it names
    ],
)
async def test_dp8_a_bad_set_is_refused_before_the_adapter_is_called(name, value, says):
    rig = Rig()
    before = await rig.table()

    reply = await rig.ask(SetProperty(name, value))

    assert isinstance(reply, Rejected)
    assert name in reply.reason and says in reply.reason
    assert rig.device.writes == []
    assert await rig.table() == before
    await rig.settle()
    assert rig.subscriber.of(PropertiesChanged) == []


async def test_dp8_a_get_of_an_unknown_name_is_refused_naming_it():
    rig = Rig()

    reply = await rig.ask(GetProperty("gain"))

    assert isinstance(reply, Rejected) and "gain" in reply.reason


# --- DP9: IDLE properties, and sets during an acquisition ---------------------------------


async def test_dp9_an_idle_property_is_refused_while_armed_saying_why():
    rig = Rig()
    assert await rig.ask(StartDevice("", -1)) == DeviceReady()

    reply = await rig.ask(SetProperty("frame_rate", 50.0))

    assert isinstance(reply, Rejected)
    assert "frame_rate" in reply.reason and "idle" in reply.reason and "ARMED" in reply.reason
    assert rig.device.writes == []


@pytest.mark.parametrize("held", ["hold_arm", "hold_disarm"])
async def test_dp9_an_idle_property_is_refused_while_arming_or_disarming(held):
    """A role holding the arm open for its recorder, or the disarm for its
    file, is no more idle than an armed device."""
    rig = Rig(**{held: True})
    rig.ref.tell(StartDevice("", -1))
    await rig.settle()
    if held == "hold_disarm":
        rig.device.complete_arm()
        rig.ref.tell(StopDevice())
        await rig.settle()
    state = rig.device.state
    assert state in (DeviceState.ARMING, DeviceState.DISARMING)

    reply = await rig.ask(SetProperty("mode", "LOG"))

    assert isinstance(reply, Rejected) and state.name in reply.reason
    assert rig.device.writes == []


async def test_dp9_an_always_property_is_applied_during_an_acquisition_which_carries_on():
    rig = Rig()
    assert await rig.ask(StartDevice("", -1)) == DeviceReady()

    reply = await rig.ask(SetProperty("exposure", 0.02))

    assert reply.properties["exposure"].value == 0.02
    assert rig.device.state is DeviceState.ARMED
    assert await rig.ask(StopDevice()) == DeviceStopped()


async def test_dp9_an_idle_property_is_accepted_again_once_stopped():
    rig = Rig()
    await rig.ask(StartDevice("", -1))
    await rig.ask(StopDevice())

    reply = await rig.ask(SetProperty("frame_rate", 50.0))

    assert isinstance(reply, PropertiesChanged)


# --- DP10: the adapter refusing ----------------------------------------------------


async def test_dp10_an_adapter_that_raises_is_answered_rejected_and_the_device_lives_on():
    """Decision 5: any exception from the adapter's set is a refusal, not a
    fault. A value the driver will not take is a person's mistake to
    correct, not a reason to lose the camera."""
    rig = Rig(fail={"cooler": "the cooler is not fitted"})

    reply = await rig.ask(SetProperty("cooler", True))

    assert isinstance(reply, Rejected)
    assert "cooler" in reply.reason and "the cooler is not fitted" in reply.reason
    await rig.settle()
    assert rig.subscriber.of(PropertiesChanged) == []
    assert rig.parent.of(Fault) == [] and rig.parent.of(ChildExited) == []
    assert isinstance(await rig.ask(SetProperty("exposure", 0.02)), PropertiesChanged)


# --- DP11: a change is published ------------------------------------------------------


async def test_dp11_a_change_is_published_as_it_was_answered():
    """Decision 2: a page showing the device sees a change a script made."""
    rig = Rig()

    reply = await rig.ask(SetProperty("frame_rate", 50.0))
    await rig.settle()

    assert rig.subscriber.of(PropertiesChanged) == [reply]


async def test_dp11_a_refusal_and_a_read_publish_nothing():
    rig = Rig()

    await rig.ask(SetProperty("frame_rate", 500.0))
    await rig.ask(GetProperties())
    await rig.ask(GetProperty("exposure"))
    await rig.settle()

    assert rig.subscriber.heard == []


# --- DP13: launch settings -----------------------------------------------------------


async def test_dp13_launch_settings_are_applied_in_order_and_publish_nothing():
    """In order: the frame rate first lowers the exposure's maximum, so the
    exposure set after it is checked against the new one."""
    rig = Rig(settings=[("frame_rate", 50.0), ("exposure", 0.015), ("mode", "LOG")])
    await rig.settle()

    assert rig.device.writes == [("frame_rate", 50.0), ("exposure", 0.015), ("mode", "LOG")]
    assert rig.subscriber.of(PropertiesChanged) == []


async def test_dp13_a_refused_launch_setting_fails_the_launch_naming_it():
    rig = Rig(settings=[("frame_rate", 50.0), ("exposure", 0.05), ("mode", "LOG")])
    await rig.device.stopped.wait()
    await rig.settle()

    [exited] = rig.parent.of(ChildExited)
    assert "exposure" in exited.error and "0.02" in exited.error  # the maximum it broke
    assert rig.device.writes == [("frame_rate", 50.0)]


async def test_dp13_a_launch_setting_the_adapter_refuses_fails_the_launch_with_its_reason():
    rig = Rig(settings=[("cooler", True)], fail={"cooler": "the cooler is not fitted"})
    await rig.device.stopped.wait()
    await rig.settle()

    [exited] = rig.parent.of(ChildExited)
    assert "launch setting cooler" in exited.error and "the cooler is not fitted" in exited.error


async def test_dp13_launch_settings_come_before_the_buffer_and_a_refusal_leaves_none():
    """A consumer finds a segment by name alone: one created by a device that
    then failed to launch would be found, and read, for nothing."""
    clock = VirtualClock()
    parent = Listener()
    spawn(parent, clock=clock, name="parent")
    name = "p" + uuid.uuid4().hex[:20]
    camera = SimulatedCamera(name, settings=[("no such setting", 1.0)])
    parent.spawn_child(camera, name="camera")
    await camera.stopped.wait()
    for _ in range(5):
        await clock.advance(0)

    [exited] = parent.of(ChildExited)
    assert "no such setting" in exited.error
    with pytest.raises(FileNotFoundError):
        ImageBuffer.open(name)


async def test_dp13_no_segment_exists_while_launch_settings_are_applied():
    """Removed again when the launch fails is not enough: a consumer polling
    for the name could open it in between."""

    class Watched(SimulatedCamera):
        seen = []

        async def write_property(self, name, value):
            try:
                ImageBuffer.open(self.buffer_name).close()
                self.seen.append("segment")
            except FileNotFoundError:
                self.seen.append("none")
            await super().write_property(name, value)

    clock = VirtualClock()
    parent = Listener()
    spawn(parent, clock=clock, name="parent")
    camera = Watched("p" + uuid.uuid4().hex[:20], settings=[("exposure", 0.02)])
    parent.spawn_child(camera, name="camera")
    await clock.advance(0)

    assert camera.seen == ["none"]
    camera.stop()
    await camera.stopped.wait()


# --- DP14: sets told without waiting --------------------------------------------------


async def test_dp14_sets_told_without_waiting_are_applied_in_order_past_a_refusal():
    """Decision 6: no promise that one does not depend on another, but the
    order holds, and a refusal stops only itself. A client that depends on an
    earlier set waits for its answer."""
    rig = Rig()

    for msg in [
        SetProperty("frame_rate", 50.0),
        SetProperty("exposure", 0.05),  # refused: the maximum is now 0.02
        SetProperty("exposure", 0.015),
        SetProperty("mode", "LOG"),
    ]:
        rig.ref.tell(msg)
    table = await rig.table()

    assert rig.device.writes == [("frame_rate", 50.0), ("exposure", 0.015), ("mode", "LOG")]
    assert table["exposure"].value == 0.015 and table["mode"].value == "LOG"
