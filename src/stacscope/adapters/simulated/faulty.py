"""A device that fails on purpose, in the ways a real driver does.

Fault isolation cannot be shown with a device that never faults, and it should
not have to wait for a vendor SDK to prove itself. Each mode here is a failure
seen in practice from C libraries behind a ctypes layer, and the same harness
is what the DCAM adapter will be put through when it arrives.
"""

import os
import pathlib
import signal
import sys
import time
from enum import Enum

from stacscope.core.actors import Actor, Fault
from stacscope.core.protocol import DeviceReady, DeviceStopped, StartDevice, StopDevice


class Fail(Enum):
    NOTHING = "nothing"
    """Behaves. The control for every other mode."""
    CRASH = "crash"
    """A real segmentation fault -- the reason a device gets a process of its
    own in the first place. No Python runs afterwards, so nothing in the
    process can report it, and the parent has only the exit status to go on."""
    RAISE = "raise"
    """An ordinary exception in a handler. The device dies, the process lives
    long enough to say why."""
    EXIT = "exit"
    """A library that calls ``exit()`` on an error it does not like, taking the
    process with it and running no cleanup."""
    BLOCK = "block"
    """A blocking call into a driver that never returns. The process is alive
    and its event loop is not: nothing answers, including heartbeats. This is
    the failure a closed connection cannot detect and only a heartbeat can."""
    QUIT = "quit"
    """An orderly stop, from the device's own side."""
    REPORT = "report"
    """Reports a non-fatal ``Fault`` to its parent and carries on: a driver
    warning, the kind of news a device has that nobody asked for."""


class FaultyDevice(Actor):
    """Speaks the device protocol until the named message, then misbehaves.
    ``on="setup"`` misbehaves while starting instead."""

    def __init__(
        self,
        fail: Fail = Fail.NOTHING,
        *,
        on: str = "StartDevice",
        note: str = "",
        release_fails: bool = False,
    ) -> None:
        super().__init__()
        self.fail = fail
        self.on = on
        self.note = note
        """A path to write in ``teardown``, standing in for releasing hardware.
        A file is the only evidence of an orderly shutdown that survives the
        process it happened in."""
        self.release_fails = release_fails
        """Whether ``teardown`` raises, as a driver that refuses to close does."""

    async def setup(self) -> None:
        if self.on == "setup":
            await self._misbehave()

    async def teardown(self) -> None:
        if self.note:
            pathlib.Path(self.note).write_text("released", encoding="utf-8")
        if self.release_fails:
            raise OSError("the driver would not release the device")

    async def handle(self, msg: object) -> None:
        if type(msg).__name__ == self.on:
            await self._misbehave()
        match msg:
            case StartDevice():
                self.reply(DeviceReady())
            case StopDevice():
                self.reply(DeviceStopped())
            case _:
                await super().handle(msg)

    async def _misbehave(self) -> None:
        match self.fail:
            case Fail.CRASH:
                _crash()
            case Fail.RAISE:
                raise RuntimeError("the driver reported an unrecoverable error")
            case Fail.EXIT:
                os._exit(9)
            case Fail.BLOCK:
                time.sleep(3600)  # deliberately not awaited: the loop stops too
            case Fail.QUIT:
                self.stop()
            case Fail.REPORT:
                self.report(Fault(self.ref, "the driver reported a warning", fatal=False))
            case Fail.NOTHING:
                pass


def _crash() -> None:
    """Die the way a driver crash kills a process: at once, no Python left.

    The mode is defined by that observable, not by a particular signal, and
    deliberately so. Raising a fatal signal from inside the process -- SIGSEGV
    via faulthandler, or SIGABRT via ``os.abort`` -- proved unreliable on the
    free-threaded build, where the process carried on and its "death" was
    never reported. An uncatchable kill behaves the same everywhere, and is
    what a caller sees of a segfault anyway: the process is simply gone.

    ``ctypes.string_at(0)`` is not an option either: ctypes traps the access
    violation and raises instead.
    """
    if sys.platform == "win32":
        import ctypes

        SEM_FAILCRITICALERRORS, SEM_NOGPFAULTERRORBOX = 0x0001, 0x0002
        ctypes.windll.kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)
        os.abort()  # no SIGKILL here; this is the decisive one Windows has
    os.kill(os.getpid(), signal.SIGKILL)
