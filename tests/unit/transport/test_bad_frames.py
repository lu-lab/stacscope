"""WS4 -- a link skips a bad frame and keeps going.

``Connection.serve`` always meant to skip one undeliverable frame: one
malformed message from a device is no reason to cut it off, and the link is
how anything else about it would be heard. But a frame whose message lacked a
field raised ``KeyError``, which ``serve`` does not catch, so it ended the
link -- and a device process whose link ends exits. With decoding hardened,
the bad frame is reported and the next one delivered.
"""

import asyncio

import msgpack

from stacscope.core.actors import Actor, spawn
from stacscope.core.protocol import MESSAGES, StartDevice
from stacscope.core.transport import Codec, CodecError, Connection, Wire
from stacscope.core.transport.stream import send_frame

DEADLINE = 5.0


class Keeper(Actor):
    def __init__(self) -> None:
        super().__init__()
        self.got = asyncio.Queue()

    async def handle(self, msg):
        await self.got.put(msg)


def frame(message: dict) -> bytes:
    return msgpack.packb(
        {"t": "keeper", "p": 1, "s": None, "m": msgpack.packb(message, use_bin_type=True)},
        use_bin_type=True,
    )


async def test_ws4_a_bad_frame_is_skipped_and_the_good_one_after_it_is_delivered():
    keeper = Keeper()
    ref = spawn(keeper, name="keeper")
    errors: list[Exception] = []
    accepted: asyncio.Future = asyncio.get_running_loop().create_future()

    async def on_client(reader, writer):
        connection = Connection(reader, writer, Wire(Codec(MESSAGES)), on_error=errors.append)
        connection.peer.register(ref)
        accepted.set_result(connection)

    server = await asyncio.start_server(on_client, "127.0.0.1", 0)
    host, port = server.sockets[0].getsockname()[:2]
    _, writer = await asyncio.open_connection(host, port)
    connection = await asyncio.wait_for(accepted, DEADLINE)
    serving = asyncio.create_task(connection.serve())
    try:
        send_frame(writer, frame({"type": "StartDevice", "root_filename": "fish"}))  # no cycles
        send_frame(writer, frame({"type": "StartDevice", "root_filename": "fish", "cycles": 3}))

        assert await asyncio.wait_for(keeper.got.get(), DEADLINE) == StartDevice("fish", 3)
        assert not connection.closed.is_set()
        [error] = errors
        assert isinstance(error, CodecError) and "StartDevice.cycles" in str(error)
    finally:
        writer.close()
        await asyncio.wait_for(serving, DEADLINE)
        server.close()
        await server.wait_closed()
        keeper.stop()
