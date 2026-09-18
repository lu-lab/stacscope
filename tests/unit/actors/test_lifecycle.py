"""R3 — ``teardown`` runs even when ``run`` raises.

Invariant 3: an unreleased DCAM handle can wedge the driver until reboot, so
teardown must survive every failure mode — including a failed ``setup``.
"""

import asyncio

import pytest

from stacscope.core.actors import Actor, spawn


class Boom(Exception):
    pass


class Recorder(Actor):
    def __init__(self, fail_in: str | None = None):
        super().__init__()
        self.fail_in = fail_in
        self.calls = []

    async def setup(self):
        self.calls.append("setup")
        if self.fail_in == "setup":
            raise Boom

    async def handle(self, msg):
        self.calls.append("handle")
        if self.fail_in == "handle":
            raise Boom
        self.stop()

    async def teardown(self):
        self.calls.append("teardown")


async def _run(actor):
    spawn(actor).tell("go")
    await asyncio.wait_for(actor.stopped.wait(), timeout=1)


async def test_clean_stop_runs_teardown_and_records_no_error():
    actor = Recorder()
    await _run(actor)

    assert actor.calls == ["setup", "handle", "teardown"]
    assert actor.exit_error is None


@pytest.mark.parametrize(
    ("fail_in", "expected"),
    [
        ("setup", ["setup", "teardown"]),
        ("handle", ["setup", "handle", "teardown"]),
    ],
)
async def test_teardown_runs_and_error_is_recorded(fail_in, expected):
    actor = Recorder(fail_in)
    await _run(actor)

    assert actor.calls == expected
    assert isinstance(actor.exit_error, Boom)


async def test_teardown_runs_when_an_overridden_run_raises():
    class Crasher(Recorder):
        async def run(self):
            raise Boom

    actor = Crasher()
    spawn(actor)
    await asyncio.wait_for(actor.stopped.wait(), timeout=1)

    assert actor.calls == ["setup", "teardown"]
    assert isinstance(actor.exit_error, Boom)


async def test_error_in_teardown_is_recorded_not_lost():
    class BadTeardown(Recorder):
        async def teardown(self):
            await super().teardown()
            raise Boom

    actor = BadTeardown()
    await _run(actor)

    assert isinstance(actor.exit_error, Boom)
    assert actor.stopped.is_set()
