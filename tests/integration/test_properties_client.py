"""DP16–DP20 -- a device's properties from outside the engine: through the
gateway, the Python client and the command line.

The point of a table of properties is that a client configures any device
knowing nothing of its adapter: no adapter's messages to import, the same
three requests for every camera. These run a simulated rig behind its
gateway (``gateway_support``): a triggered camera in the engine's process,
and a free-running one in a process of its own, as the instrument's cameras
are. On the real clock, each wait with a deadline.
"""

import subprocess
import sys
import time
import uuid

import pytest

from stacscope.app.cli import main
from stacscope.client import Client, Refused
from stacscope.core.actors import Rejected
from stacscope.core.protocol import (
    GetProperties,
    GetProperty,
    Properties,
    PropertiesChanged,
    Property,
    PropertyKind,
    SetProperty,
)

from gateway_support import DEADLINE, EngineThread, rig_text

pytestmark = pytest.mark.timeout(60)


def text(tag: str) -> str:
    return rig_text(tag) + f"""
[devices.free]
kind = "simulated_free_running_camera"
placement = "process"
heartbeat = {{ interval = 1.0, timeout = 3.0 }}
ready_timeout = {DEADLINE}
stop_timeout = {DEADLINE}
buffer_name = "gf{tag}"
shape = [4, 8]
"""


@pytest.fixture(scope="module")
def engine():
    engine = EngineThread(text(uuid.uuid4().hex[:12]))
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


# --- DP16: the three requests, end to end -------------------------------------------


def test_dp16_a_client_lists_reads_and_sets_a_cameras_property_end_to_end(connect):
    client = connect()
    cam = client.device("cam")

    table = client.ask(cam, GetProperties())
    one = client.ask(cam, GetProperty("exposure"))
    changed = client.ask(cam, SetProperty("exposure", 0.02))

    assert isinstance(table, Properties) and "exposure" in table.properties
    assert one == table.properties["exposure"]
    assert isinstance(changed, PropertiesChanged) and changed.properties["exposure"].value == 0.02
    assert client.ask(cam, GetProperty("exposure")).value == 0.02


def test_dp16_a_refused_set_is_answered_rejected_through_the_gateway(connect):
    client = connect()

    reply = client.ask(client.device("cam"), SetProperty("exposure", "fast"))

    assert isinstance(reply, Rejected) and "exposure" in reply.reason


# --- DP17: subscribing to a device ----------------------------------------------------


@pytest.mark.parametrize("device", ["cam", "free"], ids=["in the engine", "in a process of its own"])
def test_dp17_a_subscriber_hears_a_change_another_client_made(connect, device):
    """A page showing a camera must see a change a script made. The
    instrument's cameras run in processes of their own, so the camera's
    publication crosses a link to reach the gateway."""
    watcher, script = connect(), connect()
    name = {"cam": "exposure", "free": "frame_interval"}[device]
    with watcher.subscribe(watcher.device(device)) as changes:
        time.sleep(0.2)  # the gateway's subscription reaches the device
        value = script.properties(device)[name].value * 2

        script.set(device, name, value)

        heard = changes.get(timeout=DEADLINE)
    assert isinstance(heard, PropertiesChanged) and heard.properties[name].value == value


# --- DP18: no adapter in a client ----------------------------------------------------------


def test_dp18_importing_the_client_loads_no_adapter():
    """A client configures any camera through the core's messages. One that
    needed an adapter's would need that adapter installed, and its vendor's
    driver beside it, on every machine with a script."""
    code = "import sys, stacscope.client; print([m for m in sys.modules if m.startswith('stacscope.adapters')])"

    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=True).stdout

    assert out.strip() == "[]"


# --- DP19: the client's methods ----------------------------------------------------------


def test_dp19_properties_returns_the_table_by_name(connect):
    client = connect()

    table = client.properties("cam")

    assert isinstance(table["exposure"], Property) and table["exposure"].unit == "s"
    assert table["pixel_format"].kind is PropertyKind.LABEL


def test_dp19_set_returns_what_moved(connect):
    client = connect()
    client.set("cam", "aoi_x", 0.0)

    moved = client.set("cam", "aoi_width", 4.0)

    assert set(moved) == {"aoi_width", "aoi_x"} and moved["aoi_width"].value == 4.0
    client.set("cam", "aoi_width", 8.0)


def test_dp19_set_raises_refused_with_the_reason(connect):
    client = connect()

    with pytest.raises(Refused, match="read-only"):
        client.set("cam", "sensor_temperature", 1.0)


def test_dp19_an_unknown_device_is_a_lookup_error(connect):
    with pytest.raises(LookupError, match="ghost"):
        connect().properties("ghost")


# --- DP20: the command line -------------------------------------------------------------------


def test_dp20_properties_prints_the_table(engine, capsys):
    assert main(["properties", "--url", engine.url, "cam"]) == 0

    out = capsys.readouterr().out
    assert "exposure" in out and "s" in out and "read-only" in out and "MONO8" in out


@pytest.mark.parametrize(
    ("name", "typed", "value"),
    [("exposure", "0.03", 0.03), ("test_pattern", "true", True), ("pixel_format", "MONO8", "MONO8")],
    ids=["number", "flag", "label"],
)
def test_dp20_set_parses_the_value_by_the_propertys_kind_and_prints_what_moved(
    engine, connect, capsys, name, typed, value
):
    assert main(["set", "--url", engine.url, "cam", name, typed]) == 0

    assert name in capsys.readouterr().out
    assert connect().properties("cam")[name].value == value


def test_dp20_a_refused_set_exits_nonzero_with_the_reason(engine, capsys):
    assert main(["set", "--url", engine.url, "cam", "exposure", "99"]) == 1

    assert "exposure" in capsys.readouterr().err


@pytest.mark.parametrize(("name", "typed"), [("exposure", "fast"), ("test_pattern", "maybe")])
def test_dp20_a_value_that_does_not_parse_is_a_mistake_in_the_command(engine, capsys, name, typed):
    assert main(["set", "--url", engine.url, "cam", name, typed]) == 2

    assert name in capsys.readouterr().err
