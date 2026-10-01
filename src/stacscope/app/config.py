"""The rig config: one TOML file that says what the instrument is made of.

It names the timing source and every device, where each device runs, how
long each may take to answer, and each device's own settings. The rig is
built from it (stacscope.app.rig). Everything in the file is checked when it
is read, before any hardware is opened, and a mistake is reported with the
table and key it is in.

    [source]
    kind = "nidaqmx"                  # or "simulated"
    ao_task = "SPIM_Analog_Task"      # the tasks saved in NI MAX
    do_task = "SPIM_Digital_Task"
    buffer_samples = 5000             # optional
    ready_timeout = 5.0               # seconds; stop_timeout is optional

    [devices.flash]                   # the device's name
    kind = "orca_flash"               # or "ueye", or "package.module:Class"
    placement = "process"             # or "in_process" (the default)
    trigger = "HamamatsuCamTrigger"   # the digital line that triggers it
    ready_timeout = 30.0
    stop_timeout = 60.0               # optional; the ready timeout if not
    heartbeat = { interval = 1.0, timeout = 10.0 }   # optional
    required = true                   # optional; true if not given
    save_directory = "E:/data"        # from here on, the device's own
    buffer_name = "stacscope_flash"   # settings, as its documentation
    n_slots = 200                     # names them
    capacity_bytes = 209715200
    max_slots = 200
    settings = [["SENSOR MODE", "AREA"]]

Each device that records has its own save_directory; there is no rig-wide
one, so two cameras can write to two disks. Two devices may also give the
same directory: each file's name ends with its device's name, so their
files differ. A directory is checked by its device when it launches, on the
machine it runs on.

A simulated source states its own lines, in the order of a waveform's rows:

    [source]
    kind = "simulated"
    rate = 100000.0
    buffer_samples = 5000
    ready_timeout = 2.0
    ao = { etl = "Dev1/ao0" }                        # label = channel
    do = { flash_trigger = "Dev1/port0/line0" }

and a simulated camera ("simulated_camera") is triggered by the pulses it
plays on the line named by its trigger.
"""

# Keep the rig config inside app/ (user, 2026-10-01). It may be refactored
# when a recording session config interface is made, so nothing outside
# app/ may import this module or rig.py, read the file, or take a
# RigConfig, DeviceConfig or Construct. Core and the adapters keep taking
# plain constructor arguments and DeviceSpecs; app/ is where the file is
# turned into those.

import importlib
import inspect
import os
import tomllib
from dataclasses import dataclass, field
from typing import Any

from stacscope.core.actors import HeartbeatPolicy
from stacscope.core.devices import TimedDevice
from stacscope.core.ports import Channel, OutputConfig
from stacscope.core.timing import Placement

KINDS = {
    "orca_flash": "stacscope.adapters.dcam:OrcaFlash",
    "ueye": "stacscope.adapters.ueye:UEyeCamera",
    "simulated_camera": "stacscope.adapters.simulated:SimulatedCamera",
    "simulated_free_running_camera": "stacscope.adapters.simulated:SimulatedFreeRunningCamera",
}
"""Short names for the device classes STACScope ships. Any other is named in
full, as ``package.module:Class``."""

SOURCE_KINDS = ("nidaqmx", "simulated")

_SOURCE_KEYS = {
    "nidaqmx": ({"kind", "ao_task", "do_task", "ready_timeout"}, {"buffer_samples", "stop_timeout"}),
    "simulated": ({"kind", "rate", "buffer_samples", "ao", "do", "ready_timeout"}, {"stop_timeout"}),
}
"""Per source kind: the keys it must have, and the keys it may have."""

_DEVICE_KEYS = {"kind", "placement", "ready_timeout", "stop_timeout", "heartbeat", "required", "trigger"}
"""A device table's keys that belong to the rig. The rest are the device's."""

# A device class that takes `bus` is triggered through the simulated trigger
# bus, a live object the rig builds and hands to both sides. It can only be
# given to a device in the engine's own process, and only exists when the
# source is simulated, so both are checked here rather than discovered as a
# camera that never sees a pulse.
_BUS = "bus"


class ConfigError(ValueError):
    """The rig config cannot be used. The message names where the mistake is."""


@dataclass(frozen=True)
class SourceConfig:
    """The ``[source]`` table."""

    kind: str
    ready_timeout: float
    stop_timeout: float | None = None
    ao_task: str | None = None
    """The analog output task saved in NI MAX (``nidaqmx`` only)."""
    do_task: str | None = None
    """The digital output task saved in NI MAX (``nidaqmx`` only)."""
    buffer_samples: int | None = None
    """``nidaqmx``: overrides the adapter's default buffer depth."""
    output: OutputConfig | None = None
    """``simulated`` only: the lines the source plays, as the file states them."""


@dataclass(frozen=True)
class Construct:
    """Builds one device from plain data: the class's ``module:Class`` name
    and its keyword arguments. Picklable whenever the arguments are, so it
    can be sent to a process of its own and called there."""

    target: str
    options: dict[str, Any] = field(default_factory=dict)

    def __call__(self) -> TimedDevice:
        return resolve(self.target)(**self.options)


@dataclass(frozen=True)
class DeviceConfig:
    """One ``[devices.<name>]`` table."""

    name: str
    target: str
    """The device class, as ``module:Class``."""
    cls: type
    ready_timeout: float
    stop_timeout: float | None = None
    heartbeat: HeartbeatPolicy | None = None
    placement: Placement = Placement.IN_PROCESS
    required: bool = True
    trigger: str | None = None
    """The label of the digital line that triggers this device, if any."""
    options: dict[str, Any] = field(default_factory=dict)
    """The device's own settings, passed to its constructor."""

    @property
    def simulated_trigger(self) -> bool:
        """Whether this device is triggered through the simulated trigger bus."""
        return _BUS in inspect.signature(self.cls).parameters

    def factory(self, **injected: Any) -> Construct:
        """A factory for this device, with ``injected`` added to its settings."""
        return Construct(self.target, {**self.options, **injected})


@dataclass(frozen=True)
class RigConfig:
    source: SourceConfig
    devices: tuple[DeviceConfig, ...]
    """In the order the file lists them."""


def load_config(path: str | os.PathLike) -> RigConfig:
    """Read and check the rig config at ``path``. Raises ``ConfigError``."""
    try:
        with open(path, "rb") as file:
            raw = tomllib.load(file)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{os.fspath(path)}: {exc}") from None
    return _parse(raw)


def parse_config(text: str) -> RigConfig:
    """Check a rig config given as TOML text. Raises ``ConfigError``."""
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(str(exc)) from None
    return _parse(raw)


def resolve(target: str) -> type:
    """The class named ``module:Class``. Raises ``ConfigError``."""
    module_name, _, class_name = target.partition(":")
    if not module_name or not class_name:
        raise ConfigError(f"{target!r} is neither a known kind ({', '.join(KINDS)}) nor package.module:Class")
    try:
        module = importlib.import_module(module_name)
    except ImportError as exc:
        raise ConfigError(f"{target!r}: cannot import {module_name}: {exc}") from None
    try:
        return getattr(module, class_name)
    except AttributeError:
        raise ConfigError(f"{target!r}: {module_name} has no {class_name}") from None


def _parse(raw: dict[str, Any]) -> RigConfig:
    _keys("the rig config", raw, required={"source"}, allowed={"devices"})
    source = _source(_table("source", raw["source"]))
    devices = _table("devices", raw.get("devices", {}))
    config = RigConfig(
        source=source,
        devices=tuple(_device(name, _table(f"devices.{name}", table), source) for name, table in devices.items()),
    )
    if source.output is not None:
        check_triggers(config.devices, source.output)
    return config


def check_triggers(devices: "tuple[DeviceConfig, ...]", output: OutputConfig) -> None:
    """Raises ``ConfigError`` for a device whose trigger is not one of the
    digital lines in ``output``. A simulated source's lines are in the file,
    and are checked when it is read; the NI source's are in NI MAX, and are
    checked when the rig starts."""
    lines = [channel.label for channel in output.do]
    for device in devices:
        if device.trigger is not None and device.trigger not in lines:
            raise ConfigError(
                f"devices.{device.name}.trigger: the source plays no line {device.trigger!r} "
                f"({', '.join(lines)})"
            )


def _source(table: dict[str, Any]) -> SourceConfig:
    kind = table.get("kind")
    if kind not in SOURCE_KINDS:
        raise ConfigError(f"source: kind {kind!r} is not one of {', '.join(SOURCE_KINDS)}")
    required, optional = _SOURCE_KEYS[kind]
    _keys(f"source ({kind})", table, required=required, allowed=optional)
    common = dict(
        kind=kind,
        ready_timeout=_seconds("source.ready_timeout", table["ready_timeout"]),
        stop_timeout=_seconds("source.stop_timeout", table.get("stop_timeout")),
    )
    if kind == "nidaqmx":
        return SourceConfig(
            **common,
            ao_task=_text("source.ao_task", table["ao_task"]),
            do_task=_text("source.do_task", table["do_task"]),
            buffer_samples=_count("source.buffer_samples", table.get("buffer_samples")),
        )
    output = OutputConfig(
        rate=_positive("source.rate", table["rate"]),
        ao=_channels("source.ao", table["ao"]),
        do=_channels("source.do", table["do"]),
        buffer_samples=_count("source.buffer_samples", table["buffer_samples"]),
    )
    return SourceConfig(**common, output=output)


def _device(name: str, table: dict[str, Any], source: SourceConfig) -> DeviceConfig:
    where = f"devices.{name}"
    if name == "source":
        raise ConfigError(f"{where}: 'source' is the timing source's name; call the device something else")
    # Only the rig's keys are checked here; the device's own are checked
    # against its constructor below, once its class is known.
    missing = sorted({"kind", "ready_timeout"} - set(table))
    if missing:
        raise ConfigError(f"{where}: missing {', '.join(missing)}")
    kind = table["kind"]
    if not isinstance(kind, str):
        raise ConfigError(f"{where}.kind must be a string, not {kind!r}")
    target = KINDS.get(kind, kind)
    try:
        cls = resolve(target)
    except ConfigError as exc:
        raise ConfigError(f"{where}.kind: {exc}") from None
    if not (isinstance(cls, type) and issubclass(cls, TimedDevice)):
        raise ConfigError(f"{where}.kind: {target} is not a device (a TimedDevice)")

    placement_name = table.get("placement", Placement.IN_PROCESS.value)
    try:
        placement = Placement(placement_name)
    except ValueError:
        choices = ", ".join(p.value for p in Placement)
        raise ConfigError(f"{where}.placement: {placement_name!r} is not one of {choices}") from None
    trigger = table.get("trigger")
    if trigger is not None:
        trigger = _text(f"{where}.trigger", trigger)
    required = table.get("required", True)
    if not isinstance(required, bool):
        raise ConfigError(f"{where}.required must be true or false, not {required!r}")

    options = {key: _frozen(value) for key, value in table.items() if key not in _DEVICE_KEYS}
    device = DeviceConfig(
        name=name,
        target=target,
        cls=cls,
        ready_timeout=_seconds(f"{where}.ready_timeout", table["ready_timeout"]),
        stop_timeout=_seconds(f"{where}.stop_timeout", table.get("stop_timeout")),
        heartbeat=_heartbeat(f"{where}.heartbeat", table.get("heartbeat")),
        placement=placement,
        required=required,
        trigger=trigger,
        options=options,
    )
    injected = {}
    if device.simulated_trigger:
        _check_simulated_trigger(where, device, source)
        injected = {_BUS: None, "trigger": trigger}
    try:
        inspect.signature(cls).bind(**{**options, **injected})
    except TypeError as exc:
        raise ConfigError(f"{where}: {kind}: {exc}") from None
    return device


def _check_simulated_trigger(where: str, device: DeviceConfig, source: SourceConfig) -> None:
    if source.output is None:
        raise ConfigError(
            f"{where}: a {device.cls.__name__} is triggered by a simulated source's pulses, "
            f"and this rig's source is {source.kind}"
        )
    if device.placement is not Placement.IN_PROCESS:
        raise ConfigError(
            f"{where}.placement: a {device.cls.__name__} is triggered through the simulated "
            "trigger bus, which cannot reach another process; it must run in_process"
        )
    for key in (_BUS, "trigger"):
        if key in device.options:
            raise ConfigError(f"{where}.{key}: the rig supplies this; leave it out")
    if device.trigger is None:
        lines = ", ".join(channel.label for channel in source.output.do)
        raise ConfigError(f"{where}.trigger: name the digital line that triggers it ({lines})")


# --- checking values ---------------------------------------------------------


def _keys(where: str, table: dict[str, Any], *, required: set[str], allowed: set[str]) -> None:
    missing = sorted(required - set(table))
    if missing:
        raise ConfigError(f"{where}: missing {', '.join(missing)}")
    unknown = sorted(set(table) - required - allowed)
    if unknown:
        raise ConfigError(f"{where}: unknown key {', '.join(unknown)}")


def _table(where: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ConfigError(f"{where} must be a table, not {value!r}")
    return value


def _text(where: str, value: Any) -> str:
    if not isinstance(value, str) or not value:
        raise ConfigError(f"{where} must be a non-empty string, not {value!r}")
    return value


def _positive(where: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
        raise ConfigError(f"{where} must be a positive number, not {value!r}")
    return float(value)


def _seconds(where: str, value: Any) -> float | None:
    return None if value is None else _positive(where, value)


def _count(where: str, value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ConfigError(f"{where} must be a whole number of at least 1, not {value!r}")
    return value


def _channels(where: str, value: Any) -> tuple[Channel, ...]:
    table = _table(where, value)
    return tuple(Channel(_text(f"{where}.{label}", physical), label) for label, physical in table.items())


def _heartbeat(where: str, value: Any) -> HeartbeatPolicy | None:
    if value is None:
        return None
    table = _table(where, value)
    _keys(where, table, required={"interval", "timeout"}, allowed=set())
    return HeartbeatPolicy(
        interval=_positive(f"{where}.interval", table["interval"]),
        timeout=_positive(f"{where}.timeout", table["timeout"]),
    )


def _frozen(value: Any) -> Any:
    """TOML arrays as tuples, which is what the devices take."""
    if isinstance(value, list):
        return tuple(_frozen(item) for item in value)
    return value
