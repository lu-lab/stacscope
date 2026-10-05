"""WS8, the marks -- which messages a client outside the engine may send.

The gateway forwards a client's message only if its type is marked
client-sendable where it is defined (user, 2026-10-05: decision 1). The set
is the user's to change, so it is pinned here exactly: a type marked by
accident would let any client on the socket drive the engine's internal
protocol -- arm a camera with ``StartDevice``, end a recording early with
``FinishRecording`` -- and one unmarked by accident would lock every client
out of a request it needs.
"""

import dataclasses

import pytest

import stacscope.adapters.dcam.messages  # noqa: F401 - registers the cameras' messages
import stacscope.adapters.ueye.messages  # noqa: F401
from stacscope.core.protocol import MESSAGES, MessageRegistry

CLIENT_SENDABLE = {
    # the Coordinator
    "StartAcquisition",
    "StopAcquisition",
    "GetStatus",
    "Relaunch",
    # the waveform source
    "SetWaveform",
    "QueueMain",
    # any recording or triggered device
    "SetSaveDirectory",
    "GetShortestTriggerPeriod",
    # the Flash
    "SetProperty",
    "SetPropertyLabel",
    "GetProperty",
    # the uEye
    "SetPixelFormat",
    "SetAOI",
    "SetPixelClock",
    "SetFrameRate",
    "SetExposure",
    "SetTrigger",
    "GetCameraSettings",
}


def test_ws8_exactly_the_decided_messages_are_client_sendable():
    marked = {cls.__name__ for cls in MESSAGES.types() if MESSAGES.client_sendable(cls)}

    assert marked == CLIENT_SENDABLE


@pytest.mark.parametrize("name", ["StartDevice", "Go", "Finish", "StopDevice", "FinishRecording", "Heartbeat", "Fault"])
def test_ws8_the_engines_own_protocol_is_not_client_sendable(name):
    assert not MESSAGES.client_sendable(MESSAGES.lookup(name))


def test_a_registry_takes_the_mark_with_or_without_arguments():
    registry = MessageRegistry()

    @registry.register
    @dataclasses.dataclass(frozen=True)
    class Plain:
        pass

    @registry.register(client=True)
    @dataclasses.dataclass(frozen=True)
    class Marked:
        pass

    assert registry.lookup("Plain") is Plain and registry.lookup("Marked") is Marked
    assert not registry.client_sendable(Plain)
    assert registry.client_sendable(Marked)


def test_a_type_another_registry_holds_is_not_client_sendable_here():
    other = MessageRegistry()

    @other.register(client=True)
    @dataclasses.dataclass(frozen=True)
    class GetStatus:  # the name of a marked type, but not that type
        pass

    assert not MESSAGES.client_sendable(GetStatus)
