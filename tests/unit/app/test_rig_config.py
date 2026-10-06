"""G1-G6 -- the rig config: one TOML file says what the instrument is made of.

The composition root reads a file naming the timing source and every device,
where each runs, how long each may take, and each device's own settings, and
turns it into a Coordinator (``test_rig.py``). This suite is the file's half:
what it may say, and what it must refuse. A rig config is read once, before
anything opens hardware, so every mistake in it is refused there, naming the
table and the key -- not found when the third device fails to launch, or
when an acquisition starts with a setting silently dropped.

Each device has its own save directory, and the file has no rig-wide one
(user, 2026-10-01). Two streams written at once may need two disks (the
Flash to E:, the uEye to C: if E: cannot take both), and a device on another
machine has paths that mean nothing on this one. A directory is not checked
here for the same reason: the device checks its own at launch.
"""

import pickle

import pytest

from stacscope.adapters.simulated import SimulatedCamera, SimulatedFreeRunningCamera
from stacscope.app import ConfigError, parse_config
from stacscope.core.actors import HeartbeatPolicy
from stacscope.core.ports import Channel, OutputConfig
from stacscope.core.timing import Placement

INSTRUMENT = """
[source]
kind = "nidaqmx"
ao_task = "SPIM_Analog_Task"
do_task = "SPIM_Digital_Task"
buffer_samples = 5000
ready_timeout = 5.0
stop_timeout = 10.0

[devices.flash]
kind = "dcam"
placement = "process"
trigger = "HamamatsuCamTrigger"
ready_timeout = 30.0
stop_timeout = 60.0
heartbeat = { interval = 1.0, timeout = 10.0 }
buffer_name = "stacscope_flash"
n_slots = 200
capacity_bytes = 209715200
max_slots = 200
save_directory = "E:/data"
settings = [["SENSOR MODE", "AREA"], ["TRIGGER ACTIVE", "SYNCREADOUT"]]

[devices.ueye]
kind = "ueye"
placement = "process"
trigger = "ThorCamTrigger"
ready_timeout = 30.0
required = false
buffer_name = "stacscope_ueye"
n_slots = 20
capacity_bytes = 52428800
max_slots = 20
pixel_format = "RAW10"
settings = [["pixel_clock", 40e6], ["exposure", 0.01]]
save_directory = "C:/data"
"""

SIMULATED = """
[source]
kind = "simulated"
rate = 1000.0
buffer_samples = 100
ready_timeout = 2.0
ao = { etl = "Dev1/ao0" }
do = { flash_trigger = "Dev1/port0/line0", ueye_trigger = "Dev1/port0/line2" }

[devices.flash]
kind = "simulated_camera"
trigger = "flash_trigger"
ready_timeout = 2.0
buffer_name = "sim_flash"
shape = [4, 8]
"""


def test_g1_a_rig_file_names_the_source_and_every_device_in_order():
    """What the Coordinator is built from. The rig's own keys (kind, where it
    runs, timeouts, heartbeat, whether it is required, its trigger line) are
    taken out, and everything else is the device's own: passed to its
    constructor as written, arrays as tuples, since that is what the
    devices take."""
    config = parse_config(INSTRUMENT)

    source = config.source
    assert source.kind == "nidaqmx"
    assert (source.ao_task, source.do_task, source.buffer_samples) == (
        "SPIM_Analog_Task",
        "SPIM_Digital_Task",
        5000,
    )
    assert (source.ready_timeout, source.stop_timeout) == (5.0, 10.0)

    assert [d.name for d in config.devices] == ["flash", "ueye"], "the file's order"
    flash, ueye = config.devices
    assert flash.placement is Placement.PROCESS
    assert flash.trigger == "HamamatsuCamTrigger"
    assert (flash.ready_timeout, flash.stop_timeout) == (30.0, 60.0)
    assert flash.heartbeat == HeartbeatPolicy(interval=1.0, timeout=10.0)
    assert flash.required, "required unless the file says otherwise"
    assert not ueye.required
    assert ueye.heartbeat is None, "the host's own policy"
    assert ueye.stop_timeout is None, "the ready timeout, as DeviceSpec has it"
    assert flash.options == {
        "buffer_name": "stacscope_flash",
        "n_slots": 200,
        "capacity_bytes": 209715200,
        "max_slots": 200,
        "save_directory": "E:/data",
        "settings": (("SENSOR MODE", "AREA"), ("TRIGGER ACTIVE", "SYNCREADOUT")),
    }
    assert ueye.options["settings"] == (("pixel_clock", 40e6), ("exposure", 0.01))


def test_g2_each_device_has_its_own_save_directory_and_the_rig_has_none():
    """The user's requirement: the Flash records to E:, and the uEye may
    have to record to C: if one disk cannot take both streams. So the
    directory is the device's, reaches that device only, and a rig-wide
    one is refused rather than quietly applied to both."""
    flash, ueye = parse_config(INSTRUMENT).devices
    assert flash.options["save_directory"] == "E:/data"
    assert ueye.options["save_directory"] == "C:/data"

    with pytest.raises(ConfigError, match="save_directory"):
        parse_config('save_directory = "E:/data"\n' + INSTRUMENT)


def test_g3_a_simulated_source_carries_its_channels_in_the_files_order():
    """With no NI MAX to read them from, a simulated source states its own
    lines. Their order is the order of the rows of every waveform."""
    config = parse_config(SIMULATED)

    assert config.source.output == OutputConfig(
        rate=1000.0,
        ao=(Channel("Dev1/ao0", "etl"),),
        do=(Channel("Dev1/port0/line0", "flash_trigger"), Channel("Dev1/port0/line2", "ueye_trigger")),
        buffer_samples=100,
    )


def _with(text: str, old: str, new: str) -> str:
    assert old in text, old
    return text.replace(old, new, 1)


REFUSED = {
    "no source": ("[devices.flash]" + INSTRUMENT.split("[devices.flash]", 1)[1], "source"),
    "an unknown source kind": (_with(INSTRUMENT, 'kind = "nidaqmx"', 'kind = "labview"'), "labview"),
    "a saved task not named": (_with(INSTRUMENT, 'do_task = "SPIM_Digital_Task"\n', ""), "do_task"),
    "a rate for the NI source, whose rate is MAX's": (
        _with(INSTRUMENT, "buffer_samples = 5000", "buffer_samples = 5000\nrate = 100000.0"),
        "rate",
    ),
    "an unknown device kind": (_with(INSTRUMENT, 'kind = "dcam"', 'kind = "orca"'), "orca"),
    "a kind that names no class": (
        _with(INSTRUMENT, 'kind = "dcam"', 'kind = "stacscope.nowhere:Camera"'),
        "stacscope.nowhere",
    ),
    "a kind that is not a device": (
        _with(INSTRUMENT, 'kind = "dcam"', 'kind = "builtins:dict"'),
        "not a device",
    ),
    "a misspelt device setting": (_with(INSTRUMENT, "n_slots = 200", "n_slot = 200"), "n_slot"),
    "a device setting left out that the device needs": (
        _with(INSTRUMENT, 'buffer_name = "stacscope_flash"\n', ""),
        "buffer_name",
    ),
    "an unknown placement": (_with(INSTRUMENT, 'placement = "process"', 'placement = "thread"'), "thread"),
    "a heartbeat with no timeout": (
        _with(INSTRUMENT, "heartbeat = { interval = 1.0, timeout = 10.0 }", "heartbeat = { interval = 1.0 }"),
        "heartbeat",
    ),
    "a device with no ready timeout": (
        _with(SIMULATED, "ready_timeout = 2.0\nbuffer_name", "buffer_name"),
        "ready_timeout",
    ),
    "a simulated camera in a process of its own": (
        _with(SIMULATED, 'kind = "simulated_camera"', 'kind = "simulated_camera"\nplacement = "process"'),
        "process",
    ),
    "a simulated camera with nothing simulated to trigger it": (
        _with(INSTRUMENT, 'kind = "dcam"', 'kind = "simulated_camera"'),
        "simulated source",
    ),
    "a device named as the source is": (_with(SIMULATED, "[devices.flash]", "[devices.source]"), "timing source"),
    "a trigger the simulated source does not play": (
        _with(SIMULATED, 'trigger = "flash_trigger"', 'trigger = "laser_enable"'),
        "laser_enable",
    ),
}


@pytest.mark.parametrize("case", REFUSED)
def test_g4_a_mistake_is_refused_at_load_naming_where_it_is(case):
    """Every one of these would otherwise surface later and worse: at a
    launch, as an acquisition that will not arm, or not at all (a misspelt
    setting the device never sees, a simulated camera no pulse can reach).
    The message names the key, so the user can find it in the file."""
    text, named = REFUSED[case]
    with pytest.raises(ConfigError) as refused:
        parse_config(text)
    assert named in str(refused.value)


def test_g5_a_device_class_can_be_named_without_registering_it():
    """Adding hardware should need no change to the core (working
    agreement): an adapter the registry does not know is named as
    ``module:Class``."""
    text = _with(
        SIMULATED, 'kind = "simulated_camera"', 'kind = "stacscope.adapters.simulated:SimulatedCamera"'
    )
    (flash,) = parse_config(text).devices
    assert flash.cls is SimulatedCamera


def test_g6_a_devices_factory_crosses_a_process_boundary_as_plain_config():
    """A process child is sent its device as a pickled factory, and builds
    it there. So the factory holds the class's name and the options, never
    a class object from this process or a live device."""
    text = """
[source]
kind = "simulated"
rate = 1000.0
buffer_samples = 100
ready_timeout = 2.0
ao = { etl = "Dev1/ao0" }
do = { trigger = "Dev1/port0/line0" }

[devices.camera]
kind = "simulated_free_running_camera"
placement = "process"
ready_timeout = 2.0
buffer_name = "sim_camera"
interval = 0.01
shape = [4, 8]
"""
    (camera,) = parse_config(text).devices
    factory = pickle.loads(pickle.dumps(camera.factory()))

    device = factory()
    assert isinstance(device, SimulatedFreeRunningCamera)
    assert (device.buffer_name, device.interval, device.shape) == ("sim_camera", 0.01, (4, 8))


def test_dp29_a_devices_settings_reach_the_device_in_order():
    """Every device takes settings, applied in order at launch; the file's
    array is the order, which a client's map would not keep."""
    config = parse_config(SIMULATED + 'settings = [["exposure", 0.02], ["pixel_format", "MONO8"]]\n')

    device = config.devices[0].factory(bus=None)()

    assert device.launch_settings == (("exposure", 0.02), ("pixel_format", "MONO8"))


def test_dp29_the_tracked_example_gives_each_camera_settings():
    """The example is where a new machine's config starts: it shows the
    form for each camera, by each camera's own property names."""
    from pathlib import Path

    from stacscope.app import load_config

    example = Path(__file__).parents[3] / "config" / "rig.example.toml"
    flash, ueye = load_config(example).devices

    assert ("SENSOR MODE", "AREA") in flash.options["settings"]
    assert dict(ueye.options["settings"]).keys() >= {"pixel_clock", "exposure", "trigger_edge"}
    assert not {"exposure_ms", "pixel_clock", "trigger_edge", "aoi"} & ueye.options.keys()


def test_g12_the_tracked_example_is_a_rig_config_that_loads():
    """config/rig.example.toml is what each machine's own config is copied
    from. If it stops loading, every new copy starts broken; reading it
    opens nothing, so it is checked here, with no hardware."""
    from pathlib import Path

    from stacscope.app import load_config

    example = Path(__file__).parents[3] / "config" / "rig.example.toml"
    config = load_config(example)

    assert [d.name for d in config.devices] == ["flash", "ueye"]
    assert all(d.placement is Placement.PROCESS for d in config.devices)
    assert {d.options["save_directory"] for d in config.devices} == {"E:/data"}
