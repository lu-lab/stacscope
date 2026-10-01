"""The composition root: a rig built from its config file.

load_config reads the file that names the timing source and every device;
Rig.start builds and launches the rig it describes, and Rig.close shuts it
down. See stacscope.app.config for the file's format.
"""

from stacscope.app.config import (
    KINDS,
    ConfigError,
    Construct,
    DeviceConfig,
    RigConfig,
    SourceConfig,
    load_config,
    parse_config,
)

__all__ = [
    "KINDS",
    "ConfigError",
    "Construct",
    "DeviceConfig",
    "RigConfig",
    "SourceConfig",
    "load_config",
    "parse_config",
]
