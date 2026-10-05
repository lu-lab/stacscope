"""WS19, WS24, WS25 -- ``stacscope serve`` and the command line.

``stacscope serve`` is how the engine runs on the instrument: the rig its
config describes, and the gateway in front of it, until Ctrl-C. The other
commands are clients of it. On the real clock, each wait with a deadline.

Ctrl-C reaches ``serve`` as the cancellation of its task, so these tests
cancel the task, in process. ``test_ctrl_c.py`` presses a real one, on a
real ``stacscope serve``, for what only a real one shows: that it reaches
device processes too, and that it can be pressed twice.
"""

import asyncio
import contextlib
import errno
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import pytest

from stacscope.app import parse_config
from stacscope.app.cli import main
from stacscope.app.serve import Engine
from stacscope.client import Client, Disconnected
from stacscope.core.protocol import CoordinatorState, Status

from gateway_support import DEADLINE, EngineThread, rig_text, waveforms

pytestmark = pytest.mark.timeout(60)


def config_text(tag: str, port: int = 0) -> str:
    return rig_text(tag) + f"\n[gateway]\nport = {port}\n"


async def in_thread(fn, *args, **kwargs):
    """Runs blocking client code off the engine's loop."""
    return await asyncio.to_thread(fn, *args, **kwargs)


async def until_url(engine: Engine) -> str:
    await asyncio.wait_for(engine.ready.wait(), DEADLINE)
    return engine.url


@contextlib.asynccontextmanager
async def serving(text: str):
    said: list[str] = []
    engine = Engine(parse_config(text), say=said.append)
    task = asyncio.create_task(engine.run())
    try:
        await until_url(engine)
        yield engine, task, said
    finally:
        if not task.done() and not task.cancelling():  # once: a second cancel would cut the cleanup short
            task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, DEADLINE)


def run_until_running(url: str) -> int:
    with Client(url) as client:
        for msg in waveforms():
            client.ask(client.source(), msg)
        return client.start(cycles=-1)


# --- WS24: serve ----------------------------------------------------------------


async def test_ws24_serve_starts_the_rig_and_the_endpoint_from_its_config():
    async with serving(config_text(uuid.uuid4().hex[:12])) as (engine, _, said):
        status = await in_thread(lambda: Client(engine.url).status())

    assert status.state is CoordinatorState.IDLE
    assert [d.name for d in status.devices] == ["source", "cam"]
    assert any(engine.url in line for line in said)


async def test_ws24_ctrl_c_closes_the_rig_then_the_endpoint():
    """A running acquisition is stopped first, while clients are still
    connected: a page watching sees the instrument stop before its
    connection goes, rather than losing the connection and not knowing
    whether output is still playing."""
    async with serving(config_text(uuid.uuid4().hex[:12])) as (engine, task, _):
        await in_thread(run_until_running, engine.url)
        client = await in_thread(Client, engine.url)
        events = await in_thread(client.subscribe)
        assert (await in_thread(events.get, DEADLINE)).state is CoordinatorState.RUNNING

        task.cancel()  # Ctrl-C
        heard = []
        with pytest.raises(Disconnected):
            while True:
                heard.append(await in_thread(events.get, DEADLINE))

    states = [m.state for m in heard if isinstance(m, Status)]
    assert states[-1] is CoordinatorState.IDLE
    assert engine.rig_closed and engine.gateway.stopped.is_set()


async def test_ws24_an_endpoint_that_cannot_listen_fails_serve_and_closes_the_rig():
    """A port already taken is a mistake to report, not to retry: the
    operator must know nobody can reach the engine."""
    blocker = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
    port = blocker.sockets[0].getsockname()[1]
    engine = Engine(parse_config(config_text(uuid.uuid4().hex[:12], port)), say=lambda _: None)
    try:
        with pytest.raises(OSError) as raised:
            await asyncio.wait_for(engine.run(), DEADLINE)
        # Not a TimeoutError, which is an OSError too: retrying until the
        # deadline would pass a bare pytest.raises(OSError).
        assert raised.value.errno == errno.EADDRINUSE, raised.value
    finally:
        blocker.close()
        await blocker.wait_closed()

    assert engine.rig_closed


# --- WS19: a gateway that dies --------------------------------------------------


async def test_ws19_a_gateway_that_dies_is_restarted_and_the_acquisition_goes_on():
    """The gateway is how anyone reaches the engine, but the engine must
    never depend on it (decision 4): an acquisition running when it dies
    keeps running, and clients reconnect at the same address."""
    async with serving(config_text(uuid.uuid4().hex[:12])) as (engine, _, said):
        url = engine.url
        number = await in_thread(run_until_running, url)
        first = engine.gateway

        first.stop()  # dies: nobody asked serve to stop it
        await asyncio.wait_for(first.stopped.wait(), DEADLINE)
        deadline = time.monotonic() + DEADLINE
        while engine.gateway is first or not engine.gateway.started.is_set():
            assert time.monotonic() < deadline, "the gateway was not restarted"
            await asyncio.sleep(0.01)

        status = await in_thread(lambda: Client(url).status())

    assert engine.url == url
    assert status.state is CoordinatorState.RUNNING and status.acquisition == number
    assert any("restart" in line for line in said)


# --- WS25: the command line -----------------------------------------------------


@pytest.fixture
def engine():
    engine = EngineThread(rig_text(uuid.uuid4().hex[:12]))
    yield engine
    engine.close()


def test_ws25_status_prints_the_coordinator_and_every_device(engine, capsys):
    assert main(["status", "--url", engine.url]) == 0

    out = capsys.readouterr().out
    assert "idle" in out and "source" in out and "cam" in out


def test_ws25_start_and_stop_run_an_acquisition(engine, capsys):
    with Client(engine.url) as client:
        for msg in waveforms():
            client.ask(client.source(), msg)

    assert main(["start", "--url", engine.url, "--cycles", "-1"]) == 0
    assert main(["stop", "--url", engine.url]) == 0

    out = capsys.readouterr().out
    assert "acquisition 1 started" in out and "acquisition 1 stopped" in out


def test_ws25_a_refusal_exits_nonzero_with_the_engines_reason(engine, capsys):
    assert main(["stop", "--url", engine.url]) == 1

    assert "refused" in capsys.readouterr().err


def test_ws25_no_engine_to_reach_exits_nonzero(capsys):
    assert main(["status", "--url", "ws://127.0.0.1:9", "--timeout", "2"]) == 3

    assert "cannot reach" in capsys.readouterr().err


def test_ws25_relaunch_names_the_device(engine, capsys):
    assert main(["relaunch", "--url", engine.url, "cam"]) == 1  # it is running

    assert "cam" in capsys.readouterr().err


def test_ws25_the_stacscope_command_is_installed_and_serves_a_rig(tmp_path):
    """The entry point itself, as an operator runs it: the installed
    command starts a rig from a file and says where it listens."""
    command = Path(sys.executable).parent / ("stacscope.exe" if os.name == "nt" else "stacscope")
    rig = tmp_path / "rig.toml"
    # No devices: the process is killed at the end, and a camera's shared
    # memory would outlive it on POSIX.
    rig.write_text(config_text("unused").split("[devices.cam]")[0] + "\n[gateway]\nport = 0\n", encoding="utf-8")
    serve = subprocess.Popen(
        [str(command), "serve", "--config", str(rig)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        line = serve.stdout.readline()
        assert "ws://" in line, line
        url = line[line.index("ws://") :].split()[0]
        status = subprocess.run(
            [str(command), "status", "--url", url], capture_output=True, text=True, timeout=DEADLINE
        )
    finally:
        serve.kill()
        serve.wait(DEADLINE)

    assert status.returncode == 0, status.stderr
    assert "idle" in status.stdout
