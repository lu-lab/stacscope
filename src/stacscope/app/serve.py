"""stacscope serve: run the instrument, and let clients drive it.

Starts the rig the config file describes and the gateway in front of it,
says where clients connect, and runs until Ctrl-C. Clients -- the stacscope
command, scripts using stacscope.client, pages in a browser -- connect to
that address.

On Ctrl-C an acquisition still running is stopped first, while clients are
still connected and can see it stop; then the rig is shut down, then the
gateway. Closing can take a while, when a camera has a backlog to write;
pressing Ctrl-C again does not hurry it, and only says it is still closing.
If the gateway fails, it is started again at the same address, and the rig,
and any acquisition, carry on.
"""

import asyncio
import ipaddress
import signal
import sys
from collections.abc import Callable

from stacscope.app.config import RigConfig
from stacscope.app.rig import Rig
from stacscope.core.actors import describe, spawn
from stacscope.core.gateway import Gateway

RESTART_DELAY = 1.0
"""Seconds between attempts to restart a gateway that failed."""

CLOSE_TIMEOUT = 10.0
"""Seconds the gateway is given to close its connections at the end."""


def _say(line: str) -> None:
    print(line, flush=True)


def run(config: RigConfig, *, say: Callable[[str], None] = _say) -> None:
    """Run the engine ``config`` describes until Ctrl-C, then close it. A
    Ctrl-C while it closes does not cut the close short. Raises as
    ``Engine.run`` does."""
    asyncio.run(_until_interrupted(Engine(config, say=say)))


async def _until_interrupted(engine: "Engine") -> None:
    # asyncio's own handler cancels the main task on the first Ctrl-C and
    # raises KeyboardInterrupt, wherever the engine is, on the next: an
    # impatient second press would abandon the rig's close half done, with a
    # camera still open. This one cancels once and then only answers.
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    pressed = 0

    def on_ctrl_c(signum, frame) -> None:
        nonlocal pressed
        pressed += 1
        if pressed == 1:
            loop.call_soon_threadsafe(task.cancel)
        else:
            loop.call_soon_threadsafe(engine.say, "still closing the rig; wait for it")

    previous = signal.signal(signal.SIGINT, on_ctrl_c)
    try:
        await engine.run()
    except asyncio.CancelledError:
        if not pressed:
            raise
        # Closed, in order: what Ctrl-C asked for.
    finally:
        signal.signal(signal.SIGINT, previous)


class Engine:
    """The rig ``config`` describes, and its gateway, as ``stacscope serve``
    runs them. ``say`` is given a line of text for each thing worth telling
    the operator; by default it is printed.

    ``run`` starts both and runs until cancelled. ``ready`` is set once
    clients can connect, at ``url``."""

    def __init__(self, config: RigConfig, *, say: Callable[[str], None] = _say) -> None:
        self.config = config
        self.say = say
        self.rig: Rig | None = None
        self.gateway: Gateway | None = None
        self.ready = asyncio.Event()
        self.rig_closed = False
        """Whether ``run`` has shut the rig down. Diagnostic."""
        self._port = config.gateway.port

    @property
    def url(self) -> str:
        """Where clients connect."""
        host = self.config.gateway.interface
        if ":" in host:
            host = f"[{ipaddress.ip_address(host)}]"
        return f"ws://{host}:{self._port}"

    async def run(self) -> None:
        """Start the rig and the gateway, and run until cancelled -- which is
        what Ctrl-C does to it under ``asyncio.run``. Raises if the rig cannot
        start, or the gateway cannot listen when first started; the rig is
        shut down first."""
        self.rig = await Rig.start(self.config)
        try:
            await self._start_gateway()
            self.ready.set()
            self.say(f"listening on {self.url}")
            while True:
                await self.gateway.stopped.wait()
                # The engine never depends on the gateway (decision 4,
                # 2026-10-05): it is how clients reach the rig, not part of it.
                why = describe(self.gateway.exit_error) or "it was stopped"
                self.say(f"the gateway stopped ({why}); restarting it")
                await self._restart()
        finally:
            await self._close()

    async def _start_gateway(self) -> None:
        gateway = self.gateway = Gateway(
            self.rig.ref,
            interface=self.config.gateway.interface,
            port=self._port,
            origins=self.config.gateway.origins,
        )
        spawn(gateway, name="gateway")
        await _either(gateway.started, gateway.stopped)
        if not gateway.started.is_set():
            raise gateway.exit_error or RuntimeError("the gateway stopped as it started")
        # A port chosen by the system (0) is kept, so a restarted gateway is
        # where clients last found it.
        self._port = gateway.port

    async def _restart(self) -> None:
        while True:
            try:
                await self._start_gateway()
            except Exception as exc:  # noqa: BLE001 - whatever stops it listening, try again
                self.say(f"the gateway could not restart ({exc}); trying again in {RESTART_DELAY} s")
                await asyncio.sleep(RESTART_DELAY)
            else:
                self.say(f"listening again on {self.url}")
                return

    async def _close(self) -> None:
        # The rig first: an acquisition is stopped while clients can still
        # see it stop, rather than losing their connection not knowing
        # whether output is playing.
        try:
            if self.rig is not None:
                self.say("closing the rig")
                await self.rig.close()
                self.rig_closed = True
        finally:
            gateway = self.gateway
            if gateway is not None and not gateway.stopped.is_set():
                gateway.stop()
                try:
                    await asyncio.wait_for(gateway.stopped.wait(), CLOSE_TIMEOUT)
                except TimeoutError:
                    print("the gateway did not close in time", file=sys.stderr)


async def _either(*events: asyncio.Event) -> None:
    waits = [asyncio.create_task(event.wait()) for event in events]
    try:
        await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for wait in waits:
            wait.cancel()
