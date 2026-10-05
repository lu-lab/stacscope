"""WS24, the config -- the rig file's [gateway] table.

Where ``stacscope serve`` listens, and which web pages may connect, are the
instrument's own settings, so they live in its rig file beside everything
else, and are checked when it is read: a typo in an origin found when the
file loads costs nothing, found when a page cannot connect costs a session.
"""

import pytest

from stacscope.app import ConfigError, parse_config
from stacscope.app.config import GatewayConfig

SOURCE = """
[source]
kind = "simulated"
rate = 1000.0
buffer_samples = 1000
ready_timeout = 2.0
ao = { etl = "Dev1/ao0" }
do = { line = "Dev1/port0/line0" }
"""


def gateway(table: str) -> GatewayConfig:
    return parse_config(SOURCE + "\n[gateway]\n" + table).gateway


def test_without_a_gateway_table_clients_connect_on_this_computer_at_the_default_port():
    assert parse_config(SOURCE).gateway == GatewayConfig("127.0.0.1", 8765, ())


def test_the_gateway_table_is_read():
    read = gateway('interface = "::1"\nport = 9000\norigins = ["https://lab.example", "http://localhost:5173"]')

    assert read == GatewayConfig("::1", 9000, ("https://lab.example", "http://localhost:5173"))


@pytest.mark.parametrize(
    ("table", "where"),
    [
        ('interface = "0.0.0.0"', "gateway.interface"),  # WS17, at read time
        ('interface = "192.168.1.5"', "gateway.interface"),
        ("port = 70000", "gateway.port"),
        ("port = -1", "gateway.port"),
        ('port = "8765"', "gateway.port"),
        ('origins = "https://lab.example"', "gateway.origins"),  # one, not a list
        ('origins = ["lab.example"]', "gateway.origins"),  # no scheme
        ('origins = ["https://lab.example/tools/"]', "gateway.origins"),  # a page, not an origin
        ('origins = ["ftp://lab.example"]', "gateway.origins"),
        ("bind = 1", "gateway"),
    ],
)
def test_a_mistake_in_the_gateway_table_is_found_when_the_file_is_read(table, where):
    with pytest.raises(ConfigError, match=where):
        gateway(table)
