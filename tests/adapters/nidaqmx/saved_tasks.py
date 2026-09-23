"""Output tasks saved to NI MAX by a test, and deleted again after it.

The adapter loads the instrument's tasks from NI MAX by name, so a suite that
drives a real driver needs saved tasks to load. It builds its own rather than
borrowing the instrument's: the rig's tasks are the rig owner's to change, and
a test that depends on them breaks -- or, worse, silently tests something else
-- the day the wiring does. These are built from an ``OutputConfig`` the test
owns, set up the way the adapter requires of any saved pair (continuous,
regeneration disabled, digital lines clocked from ``ao/SampleClock``), and
saved under names no instrument would use.
"""

import contextlib

import nidaqmx
import nidaqmx.system
from nidaqmx.constants import AcquisitionType, LineGrouping, RegenerationMode

from stacscope.core.ports import OutputConfig

PREFIX = "stacscope_test_"
"""Every task saved here is named with this, and nothing else is ever
overwritten or deleted: a typo in a test must not be able to replace the
instrument's own tasks."""


@contextlib.contextmanager
def saved(config: OutputConfig, *, ao: str, do: str):
    """Save an analog task named ``ao`` and a digital task named ``do`` to
    NI MAX, matching ``config``, and delete both on the way out -- whether
    the test passed or not. A pair left over from a run that was killed is
    overwritten. Both names must start with ``PREFIX``."""
    for name in (ao, do):
        assert name.startswith(PREFIX), f"{name!r}: test tasks must be named {PREFIX}*"
    try:
        _save_ao(ao, config)
        _save_do(do, config)
        yield
    finally:
        tasks = nidaqmx.system.System.local().tasks
        for name in (ao, do):
            if name in tasks:
                tasks[name].delete()


def _save_ao(name: str, config: OutputConfig) -> None:
    with nidaqmx.Task() as task:
        for channel in config.ao:
            task.ao_channels.add_ao_voltage_chan(
                channel.physical, name_to_assign_to_channel=channel.label
            )
        task.timing.cfg_samp_clk_timing(config.rate, sample_mode=AcquisitionType.CONTINUOUS)
        task.out_stream.regen_mode = RegenerationMode.DONT_ALLOW_REGENERATION
        task.save(save_as=name, overwrite_existing_task=True)


def _save_do(name: str, config: OutputConfig) -> None:
    device = config.ao[0].physical.split("/")[0]
    with nidaqmx.Task() as task:
        for channel in config.do:
            task.do_channels.add_do_chan(
                channel.physical,
                name_to_assign_to_lines=channel.label,
                line_grouping=LineGrouping.CHAN_PER_LINE,
            )
        task.timing.cfg_samp_clk_timing(
            config.rate,
            source=f"/{device}/ao/SampleClock",
            sample_mode=AcquisitionType.CONTINUOUS,
        )
        task.out_stream.regen_mode = RegenerationMode.DONT_ALLOW_REGENERATION
        task.save(save_as=name, overwrite_existing_task=True)
