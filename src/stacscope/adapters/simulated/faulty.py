"""A device that fails on purpose, in the ways a real driver does.

Fault isolation cannot be shown with a device that never faults, and it should
not have to wait for a vendor SDK to prove itself. Each mode here is a failure
seen in practice from C libraries behind a ctypes layer, and the same harness
is what the DCAM adapter will be put through when it arrives.
"""

import faulthandler
import os
import pathlib
import sys
import time
from enum import Enum

from stacscope.core.actors import Actor
from stacscope.core.protocol import DeviceReady, DeviceStopped, StartDevice, StopDevice


class Fail(Enum):
    NOTHING = "nothing"
    """Behaves. The control for every other mode."""
    CRASH = "crash"
    """A real segmentation fault -- thesis 3.6's reason for all of this. No
    Python runs afterwards, so nothing in the process can report it, and the
    parent has only the exit status to go on. Note that ``ctypes.string_at(0)``
    would *not* do: ctypes traps the access violation and raises instead."""
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


class FaultyDevice(Actor):
    """Speaks the device protocol until the named message, then misbehaves."""

    def __init__(
        self, fail: Fail = Fail.NOTHING, *, on: str = "StartDevice", note: str = ""
    ) -> None:
        super().__init__()
        self.fail = fail
        self.on = on
        self.note = note
        """A path to write in ``teardown``, standing in for releasing hardware.
        A file is the only evidence of an orderly shutdown that survives the
        process it happened in."""

    async def teardown(self) -> None:
        if self.note:
            pathlib.Path(self.note).write_text("released", encoding="utf-8")

    async def handle(self, msg: object) -> None:
        if type(msg).__name__ == self.on:
            await self._misbehave()
        match msg:
            case StartDevice():
                self.reply(DeviceReady())
            case StopDevice():
                self.reply(DeviceStopped())

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
            case Fail.NOTHING:
                pass


def _crash() -> None:
    """Die the way a bad pointer dereference dies.

    Windows would otherwise put up a crash dialog and wait for someone to
    click it, which in a test suite is indistinguishable from the hang this
    milestone exists to rule out.
    """
    if sys.platform == "win32":
        import ctypes

        SEM_FAILCRITICALERRORS, SEM_NOGPFAULTERRORBOX = 0x0001, 0x0002
        ctypes.windll.kernel32.SetErrorMode(SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX)
    faulthandler._sigsegv()
