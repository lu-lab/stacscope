"""WS5, WS18, WS20–WS22 -- the Python client, against a simulated rig.

Scripts, notebooks and Pycro-Manager hooks drive the engine through
``stacscope.client``, over the gateway's WebSocket endpoint. They are
synchronous code, so the client is too; the engine runs on its own loop in a
thread of its own (``gateway_support``), as it will beside them.

On the real clock, each wait with a deadline.
"""

import time
import uuid

import pytest

from stacscope.client import Client, Disconnected, Refused
from stacscope.core.actors import AskTimeout, Rejected
from stacscope.core.protocol import (
    AcquisitionStopped,
    CoordinatorState,
    GetShortestTriggerPeriod,
    MainApplied,
    SaveDirectory,
    SetSaveDirectory,
    Status,
    StopAcquisition,
)

from gateway_support import DEADLINE, EngineThread, rig_text, waveforms

pytestmark = pytest.mark.timeout(60)


@pytest.fixture
def engine(tmp_path):
    engine = EngineThread(rig_text(uuid.uuid4().hex[:12], save_directory=tmp_path))
    yield engine
    engine.close()


@pytest.fixture
def connect(engine):
    opened: list[Client] = []

    def connect(**options) -> Client:
        client = Client(engine.url, **options)
        opened.append(client)
        return client

    yield connect
    for client in opened:
        client.close()


def load(client: Client) -> None:
    for msg in waveforms():
        client.ask(client.source(), msg)


def until_state(client: Client, state: CoordinatorState) -> Status:
    deadline = time.monotonic() + DEADLINE
    while (status := client.status()).state is not state:
        assert time.monotonic() < deadline, f"still {status.state}"
        time.sleep(0.01)
    return status


# --- WS5 ------------------------------------------------------------------------


def test_ws5_a_client_asks_the_coordinator_for_its_status_end_to_end(connect):
    """The whole path once: a client's request through the socket and the
    gateway to the Coordinator of a running rig, and its answer back."""
    client = connect()

    status = client.status()

    assert status.state is CoordinatorState.IDLE
    assert [(d.name, d.source, d.present) for d in status.devices] == [("source", True, True), ("cam", False, True)]
    assert status.devices[1].ref.address == "coordinator/cam"


# --- WS20: ask, tell, and the Coordinator's requests --------------------------


def test_ws20_ask_returns_the_reply_rejected_included(connect):
    """``ask`` is the general request: whatever the actor answers comes back
    as is, so a script can tell a refusal from an answer itself."""
    client = connect()

    reply = client.ask(client.coordinator, StopAcquisition())

    assert isinstance(reply, Rejected)


def test_ws20_an_ask_nobody_answers_raises_ask_timeout(connect):
    """An actor ignores a request it has no use for (user, 2026-09-30), so a
    client waiting on one must give up, and say so."""
    client = connect()

    started = time.monotonic()
    with pytest.raises(AskTimeout):
        client.ask(client.source(), GetShortestTriggerPeriod(), timeout=0.3)
    assert time.monotonic() - started < DEADLINE


def test_ws20_start_and_stop_run_an_acquisition_and_a_refusal_raises(connect):
    """The Coordinator's own requests, as a script calls them. A refusal is
    an exception here, not a value: a script that starts an acquisition and
    carries on as though it had would go on to act on nothing."""
    client = connect()
    with pytest.raises(Refused, match="MAIN"):
        client.start(cycles=-1)  # no waveforms yet

    load(client)
    number = client.start(cycles=-1)
    running = until_state(client, CoordinatorState.RUNNING)
    stopped = client.stop()
    idle = until_state(client, CoordinatorState.IDLE)

    # The refused start was numbered too: every StartAcquisition is.
    assert number == running.acquisition == stopped == idle.acquisition == 2


def test_ws20_tell_sends_and_expects_nothing(connect):
    client = connect()
    load(client)
    client.start(cycles=-1)
    until_state(client, CoordinatorState.RUNNING)

    client.tell(client.coordinator, StopAcquisition())

    assert until_state(client, CoordinatorState.IDLE).acquisition == 1


def test_ws20_relaunch_brings_a_lost_device_back(engine, connect):
    client = connect()
    with pytest.raises(Refused):
        client.relaunch("cam")  # it is running
    with pytest.raises(Refused):
        client.relaunch("nobody")

    engine.call(lambda: engine.rig._made["cam"].stop())
    deadline = time.monotonic() + DEADLINE
    while client.status().devices[1].present:
        assert time.monotonic() < deadline
        time.sleep(0.01)
    client.relaunch("cam")

    deadline = time.monotonic() + DEADLINE
    while not client.status().devices[1].present:
        assert time.monotonic() < deadline
        time.sleep(0.01)


# --- WS21: subscribing --------------------------------------------------------


def test_ws21_a_subscription_yields_the_status_then_what_the_coordinator_publishes(connect):
    client = connect()
    load(client)

    with client.subscribe() as events:
        first = events.get(DEADLINE)
        client.start(cycles=-1)
        states = []
        while CoordinatorState.RUNNING not in states:
            msg = events.get(DEADLINE)
            if isinstance(msg, Status):
                states.append(msg.state)
        client.stop()

    assert isinstance(first, Status) and first.state is CoordinatorState.IDLE
    assert states == [CoordinatorState.STARTING, CoordinatorState.RUNNING]


def test_ws21_a_subscription_to_the_source_hears_which_main_plays(connect):
    client = connect()
    load(client)

    with client.subscribe(client.source()) as mains:
        client.start(cycles=-1)
        applied = mains.get(DEADLINE)
        client.stop()

    assert isinstance(applied, MainApplied) and applied.cycle == 0


def test_ws21_a_subscription_raises_when_the_connection_drops(engine, connect):
    """A script waiting on a subscription must hear that nothing more will
    come, not wait for ever on a socket that is gone."""
    client = connect()
    events = client.subscribe()
    events.get(DEADLINE)

    engine.run(stop_gateway(engine))

    with pytest.raises(Disconnected):
        events.get(DEADLINE)
    with pytest.raises(Disconnected):
        client.status()


async def stop_gateway(engine):
    engine.gateway.stop()
    await engine.gateway.stopped.wait()


# --- WS22: a device, through its ref --------------------------------------------


def test_ws22_a_devices_ref_from_the_status_takes_its_settings(connect, tmp_path):
    """Refs in a Status work through the client, so a script reaches a
    device the way an actor would: told a setting, it takes it; asked, it
    answers; and each recording lands where the device was last told."""
    client = connect()
    load(client)
    first, second = tmp_path / "first", tmp_path / "second"
    first.mkdir()
    second.mkdir()

    cam = client.device("cam")
    cam.tell(SetSaveDirectory(str(first)))
    client.start(cycles=3, root="run-01")
    until_state(client, CoordinatorState.IDLE)
    answer = client.ask(cam, SetSaveDirectory(str(second)))
    client.start(cycles=3, root="run-02")
    until_state(client, CoordinatorState.IDLE)

    assert answer == SaveDirectory(str(second))
    assert [p.name for p in first.iterdir()] == ["run-01_cam.tif"]
    assert [p.name for p in second.iterdir()] == ["run-02_cam.tif"]


# --- WS18: a client that leaves ----------------------------------------------


def test_ws18_a_client_leaving_mid_acquisition_changes_nothing(connect):
    """A refreshed browser tab or a crashed hook must not stop an acquisition
    (decision 5). Any other client sees it running, and can stop it."""
    starter, other = connect(), connect()
    load(starter)
    number = starter.start(cycles=-1)

    starter.close()
    time.sleep(0.3)

    assert other.status().state is CoordinatorState.RUNNING
    assert other.ask(other.coordinator, StopAcquisition()) == AcquisitionStopped(number)
