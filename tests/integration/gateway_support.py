"""A simulated rig behind its gateway, on an event loop in a thread of its own.

That is how the engine runs beside the scripts, hooks and command-line calls
that drive it: they block, and must never share its loop. The Python client
is synchronous, so a test using it runs in the test's own thread while the
engine serves it from this one.
"""

import asyncio
import threading
from concurrent.futures import Future

import numpy as np

from stacscope.app import parse_config
from stacscope.app.rig import Rig
from stacscope.core.actors import spawn
from stacscope.core.gateway import Gateway
from stacscope.core.protocol import SetWaveform, Waveform

DEADLINE = 10.0
RATE = 1000.0  # samples/s
CYCLE = 50  # samples: the instrument's 20 cycles a second


def rig_text(tag: str, *, save_directory=None) -> str:
    """A source and one camera, triggered once a cycle; buffer names of the
    test's own."""
    save = f"save_directory = '{save_directory}'" if save_directory is not None else ""
    return f"""
[source]
kind = "simulated"
rate = {RATE}
buffer_samples = 1000
ready_timeout = 2.0
stop_timeout = {DEADLINE}
ao = {{ etl = "Dev1/ao0" }}
do = {{ cam_trigger = "Dev1/port0/line0" }}

[devices.cam]
kind = "simulated_camera"
trigger = "cam_trigger"
ready_timeout = 2.0
stop_timeout = {DEADLINE}
buffer_name = "gw{tag}"
shape = [4, 8]
n_slots = 16
{save}
"""


def waveforms():
    """WARMUP, MAIN and COOLDOWN for ``rig_text``: the camera pulsed at the
    start of every cycle, 0 V throughout."""
    quiet = np.zeros((1, 4)), np.zeros((1, 4), dtype=bool)
    do = np.zeros((1, CYCLE), dtype=bool)
    do[0, 0] = True
    main = np.zeros((1, CYCLE)), do
    return [
        SetWaveform(Waveform.WARMUP, *quiet),
        SetWaveform(Waveform.MAIN, *main),
        SetWaveform(Waveform.COOLDOWN, *quiet),
    ]


class EngineThread:
    """Starts the rig ``text`` describes and a gateway in front of it, on a
    loop in a thread of its own; ``url`` is where clients connect."""

    def __init__(self, text: str, **gateway_options) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, name="engine", daemon=True)
        self._thread.start()
        self.rig: Rig | None = None
        self.gateway: Gateway | None = None
        try:
            self.run(self._start(text, gateway_options))
        except BaseException:
            self.close()
            raise

    @property
    def url(self) -> str:
        return f"ws://127.0.0.1:{self.gateway.port}"

    def run(self, coro):
        """Runs ``coro`` on the engine's loop and returns what it does."""
        future: Future = asyncio.run_coroutine_threadsafe(coro, self.loop)
        return future.result(DEADLINE)

    def call(self, fn):
        """Calls ``fn()`` on the engine's loop and returns what it does."""

        async def call():
            return fn()

        return self.run(call())

    async def _start(self, text: str, options: dict) -> None:
        self.rig = await Rig.start(parse_config(text))
        self.gateway = Gateway(self.rig.ref, port=0, **options)
        spawn(self.gateway, name="gateway")
        await asyncio.wait_for(self.gateway.started.wait(), DEADLINE)

    async def _close(self) -> None:
        if self.gateway is not None:
            self.gateway.stop()
            await asyncio.wait_for(self.gateway.stopped.wait(), DEADLINE)
        if self.rig is not None:
            await self.rig.close()

    def close(self) -> None:
        try:
            self.run(self._close())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(DEADLINE)
            self.loop.close()
