"""WS26-WS28 -- a real Ctrl-C on ``stacscope serve``, with a device in a
process of its own.

Ctrl-C is how an operator stops the engine. It must stop it in order: the
acquisition through the Coordinator, each device disarmed and then closed
by the engine. Two ways to get that wrong were found by pressing it:

- The console's Ctrl-C reaches every process attached to it (on POSIX, the
  terminal's whole foreground group), device processes included. A device
  process then tears its device down by itself, at once, while the engine
  may still be playing output to it -- on the instrument, a camera shut down
  that way can wedge its driver.
- asyncio turns a second Ctrl-C into a ``KeyboardInterrupt`` wherever the
  engine is, which cuts its orderly close short.

So these press the key for real, in a console of the engine's own (see
``ctrl_c_support``), and the device writes down how it was stopped (see
``ctrl_c_device``). On the real clock; each wait has a deadline.
"""

import time
import uuid
from pathlib import Path

import pytest

from stacscope.client import Client, Disconnected
from stacscope.core.actors import Fault
from stacscope.core.protocol import CoordinatorState, Status

from ctrl_c_device import read_log
from ctrl_c_support import Serve
from gateway_support import DEADLINE, waveforms

pytestmark = pytest.mark.timeout(90)

STOPPED_IN_ORDER = ["setup", "ready", "disarm", "close cancelled=False"]


def rig(tmp_path: Path, log: Path, *, setup_delay: float = 0.0, disarm_delay: float = 0.0) -> Path:
    """A simulated source, and the logging camera in a process of its own."""
    path = tmp_path / "rig.toml"
    path.write_text(
        f"""
[source]
kind = "simulated"
rate = 1000.0
buffer_samples = 1000
ready_timeout = 2.0
stop_timeout = {DEADLINE}
ao = {{ etl = "Dev1/ao0" }}
do = {{ cam_trigger = "Dev1/port0/line0" }}

[devices.cam]
kind = "ctrl_c_device:LoggingCamera"
placement = "process"
ready_timeout = {DEADLINE}
stop_timeout = {DEADLINE}
buffer_name = "cc{uuid.uuid4().hex[:12]}"
shape = [4, 8]
n_slots = 16
log = '{log.as_posix()}'
setup_delay = {setup_delay}
disarm_delay = {disarm_delay}

[gateway]
port = 0
""",
        encoding="utf-8",
    )
    return path


def until(condition, what: str, timeout: float = DEADLINE) -> None:
    deadline = time.monotonic() + timeout
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.02)


def start_acquisition(client: Client) -> None:
    for msg in waveforms():
        client.ask(client.source(), msg)
    client.start(cycles=-1)


def test_ws26_ctrl_c_during_an_acquisition_stops_it_in_order(tmp_path):
    """The acquisition stops through the Coordinator, so a client watching
    sees it end with no Fault; the device is disarmed, then closed, by the
    engine. A device process that heard the Ctrl-C itself would close at
    once, cancelled, and the Coordinator would see a device lost
    mid-acquisition."""
    log = tmp_path / "cam.log"
    with Serve(rig(tmp_path, log)) as serve:
        with Client(serve.url(DEADLINE)) as client:
            start_acquisition(client)
            with client.subscribe() as events:
                assert events.get(DEADLINE).state is CoordinatorState.RUNNING
                serve.press()
                heard = []
                with pytest.raises(Disconnected):
                    while True:
                        heard.append(events.get(DEADLINE))
        code = serve.wait(DEADLINE)

    output = "\n".join(serve.output)
    assert code == 0, output
    assert not [m for m in heard if isinstance(m, Fault)], heard
    assert [m.state for m in heard if isinstance(m, Status)][-1] is CoordinatorState.IDLE
    assert read_log(log) == STOPPED_IN_ORDER, output


def test_ws27_a_second_ctrl_c_while_closing_cuts_nothing_short(tmp_path):
    """An operator who presses again while the rig is closing must not turn
    an orderly close into an abandoned one. The camera takes two seconds to
    disarm, so the second press lands while it does.

    The camera's log alone cannot tell: a device process whose link drops
    stops its device in order too. What an abandoned close loses is the
    engine's side -- the Coordinator never finishes stopping the
    acquisition -- so a client watching must see it reach IDLE."""
    log = tmp_path / "cam.log"
    with Serve(rig(tmp_path, log, disarm_delay=2.0)) as serve:
        with Client(serve.url(DEADLINE)) as client:
            start_acquisition(client)
            with client.subscribe() as events:
                assert events.get(DEADLINE).state is CoordinatorState.RUNNING
                serve.press()
                serve.expect("closing the rig", DEADLINE)
                until(lambda: "disarm" in read_log(log), "the camera to start disarming")
                serve.press()
                serve.expect("still closing", DEADLINE)
                heard = []
                with pytest.raises(Disconnected):
                    while True:
                        heard.append(events.get(DEADLINE))
        code = serve.wait(DEADLINE)

    output = "\n".join(serve.output)
    assert code == 0, output
    assert not [m for m in heard if isinstance(m, Fault)], heard
    assert [m.state for m in heard if isinstance(m, Status)][-1] is CoordinatorState.IDLE, heard
    assert read_log(log) == STOPPED_IN_ORDER, output


def test_ws28_ctrl_c_while_the_rig_starts_closes_the_device_first(tmp_path):
    """A camera still opening when Ctrl-C comes is let finish opening and
    then closed, by the engine, before the engine exits. Killing it mid-open
    is what wedges a driver; leaving it to close after the engine has gone
    leaves it holding the camera when the operator starts the engine again."""
    log = tmp_path / "cam.log"
    with Serve(rig(tmp_path, log, setup_delay=3.0)) as serve:
        until(lambda: "setup" in read_log(log), "the camera to start opening", timeout=30)
        serve.press()
        code = serve.wait(DEADLINE + 3.0)
        at_exit = read_log(log)

    output = "\n".join(serve.output)
    assert code == 0, output
    assert "listening" not in output, output
    assert at_exit == ["setup", "ready", "close cancelled=False"], output
