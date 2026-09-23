"""N14-N17, N19 -- the tasks come from NI MAX; `configure` loads, checks and commits.

Since step 2 the channel map, rate, clock routing and regeneration mode are
the instrument's, owned by NI MAX -- which is where the legacy engine kept
them too. `configure` sets only the two things MAX does not own, and refuses a
rig that disagrees with what the caller believes it has.

These run only when asked for by name: the directory is ignored by the default
suite, because it needs the `nidaqmx` extra installed. See pyproject.

    uv run --extra nidaqmx pytest tests/adapters/nidaqmx
"""

import dataclasses

import pytest
from fake_daqmx import FakeDaq
from nidaqmx.constants import AcquisitionType, DataTransferActiveTransferMode, RegenerationMode

from stacscope.adapters.nidaqmx import NiSignalOutput, read_output_config
from stacscope.adapters.simulated import VirtualClock
from stacscope.core.ports import Channel, OutputConfig

RATE = 100_000.0
BUFFER = 8192
AO_TASK = "test_ao"
DO_TASK = "test_do"

CONFIG = OutputConfig(
    rate=RATE,
    ao=(Channel("Dev1/ao0", "etl"), Channel("Dev1/ao1", "scan1")),
    do=(Channel("Dev1/port0/line0", "camera"), Channel("Dev1/port0/line1", "laser")),
    buffer_samples=BUFFER,
)


def rig(space: int = 0) -> FakeDaq:
    """A system whose NI MAX holds the two tasks exactly as CONFIG says."""
    return FakeDaq(space=space).saving(CONFIG, ao=AO_TASK, do=DO_TASK)


def adapter(clock, daq: FakeDaq) -> NiSignalOutput:
    return NiSignalOutput(clock, ao_task=AO_TASK, do_task=DO_TASK, system=daq)


@pytest.fixture
def daq():
    return rig()


@pytest.fixture
async def configured(daq):
    output = adapter(VirtualClock(), daq)
    await output.configure(CONFIG)
    return output


async def test_both_tasks_are_loaded_from_max_by_name_at_configure(daq):
    """N14. The tasks are the instrument's, saved in NI MAX, and loaded rather
    than built -- so the channel map lives in one place, the place the rig's
    owner already edits. Loading waits for ``configure``: building an adapter
    must not reserve the board."""
    output = adapter(VirtualClock(), daq)
    assert daq.loaded == []

    await output.configure(CONFIG)

    assert [t.name for t in daq.loaded] == [AO_TASK, DO_TASK]


@pytest.mark.parametrize("missing", [AO_TASK, DO_TASK])
async def test_a_missing_task_fails_configure_and_names_the_task(daq, missing):
    """N14. A task that was never saved (or saved on another machine) is a
    setup error, reported at ``configure`` by name. The driver itself would
    not say which one: indexing the saved tasks by an unknown name succeeds,
    and only a later load fails, with a code. Nothing is loaded either --
    half a rig reserved is worse than none."""
    del daq.saved[missing]
    output = adapter(VirtualClock(), daq)

    with pytest.raises(LookupError, match=missing):
        await output.configure(CONFIG)

    assert daq.loaded == []


async def test_configure_changes_only_the_transfer_and_the_buffer(configured, daq):
    """N15. MAX owns everything about the tasks except two things: the output
    buffer's depth, which is the refill loop's business, and DMA transfer,
    which NI MAX leaves at POLLED for an analog output task (read off the
    instrument's own saved task on Dev1-sim). Anything else set here would
    silently override the rig's owner."""
    ao, do = daq.task(AO_TASK), daq.task(DO_TASK)

    assert ao.changed == {
        "ao_data_xfer_mech": DataTransferActiveTransferMode.DMA,
        "output_buf_size": BUFFER,
    }
    assert do.changed == {
        "do_data_xfer_mech": DataTransferActiveTransferMode.DMA,
        "output_buf_size": BUFFER,
    }
    assert ao.committed
    assert do.committed


def _swap_ao_physical(daq):
    (a, pa), (b, pb) = daq.saved[AO_TASK].channels
    daq.saved[AO_TASK].channels = [(a, pb), (b, pa)]


def _relabel_do(daq):
    daq.saved[DO_TASK].channels[1] = ("shutter", "Dev1/port0/line1")


def _extra_ao(daq):
    daq.saved[AO_TASK].channels.append(("scan2", "Dev1/ao2"))


def _ao_rate(daq):
    daq.saved[AO_TASK].rate = RATE / 2


def _do_rate(daq):
    daq.saved[DO_TASK].rate = RATE / 2


def _do_own_clock(daq):
    daq.saved[DO_TASK].source = "/Dev1/do/SampleClockTimebase"


def _regenerates(daq):
    daq.saved[AO_TASK].regen = RegenerationMode.ALLOW_REGENERATION


def _finite(daq):
    daq.saved[DO_TASK].mode = AcquisitionType.FINITE


@pytest.mark.parametrize(
    ("break_rig", "named"),
    [
        (_swap_ao_physical, AO_TASK),
        (_relabel_do, DO_TASK),
        (_extra_ao, AO_TASK),
        (_ao_rate, AO_TASK),
        (_do_rate, DO_TASK),
        (_do_own_clock, DO_TASK),
        (_regenerates, AO_TASK),
        (_finite, DO_TASK),
    ],
    ids=["physical", "label", "count", "ao-rate", "do-rate", "do-clock", "regeneration",
         "sample-mode"],
)
async def test_a_rig_that_disagrees_with_the_configuration_fails_configure(
    daq, break_rig, named
):
    """N16. The configuration is checked against MAX, never imposed on it. A
    mismatch -- a transposed pair of scanners, a relabelled line, a rate typed
    into one task and not the other -- is a setup-time failure naming the task,
    not an override and not something found mid-acquisition.

    Clock routing, regeneration and sample mode are checked too though no
    caller states them: they are what the refill design rests on (see the
    module docstring of the adapter), and since MAX owns them, a rig edited in
    MAX is the only way they can now go wrong."""
    break_rig(daq)
    output = adapter(VirtualClock(), daq)

    with pytest.raises(ValueError, match=named):
        await output.configure(CONFIG)

    assert not any(t.committed for t in daq.loaded)


async def test_a_mismatch_leaves_the_tasks_for_close_to_release(daq):
    """N16. A refused ``configure`` still holds what it loaded, and ``close``
    gives it back -- the source's teardown calls it whether or not setup
    succeeded, so a misconfigured rig does not keep the board reserved."""
    _ao_rate(daq)
    output = adapter(VirtualClock(), daq)
    with pytest.raises(ValueError):
        await output.configure(CONFIG)

    await output.close()

    assert daq.loaded and all(t.closed for t in daq.loaded)


@pytest.mark.parametrize("buffer", [3275, 1000])
async def test_a_buffer_smaller_than_the_board_holds_fails_configure(daq, buffer):
    """N19. On the physical PCI-6733 a host buffer smaller than the analog
    task's onboard FIFO (3276 samples with five channels) is not safe, with
    nothing on our side falling behind. At 1000 samples every run failed within
    a few seconds -- the driver raised -50410 ("no more space in the buffer")
    with 15 ms still queued, or ran dry; at 2000 one run in four ran dry. From
    3000 up, 20 runs of 8 s were clean. The driver refills the board in blocks
    of about 2050 samples, and a host buffer that cannot hold one is where it
    breaks. Refused at setup, naming the floor, rather than found as a gap
    mid-acquisition."""
    config = dataclasses.replace(CONFIG, buffer_samples=buffer)
    output = adapter(VirtualClock(), daq)

    with pytest.raises(ValueError, match="3276"):
        await output.configure(config)

    assert not any(t.committed for t in daq.loaded)


async def test_a_buffer_as_large_as_the_board_holds_is_accepted(daq):
    """N19, the boundary: the floor is the onboard FIFO itself."""
    output = adapter(VirtualClock(), daq)

    await output.configure(dataclasses.replace(CONFIG, buffer_samples=3276))

    assert all(t.committed for t in daq.loaded)


async def test_the_configuration_can_be_read_from_max(daq):
    """N17. A rig config need not restate the channel map: it is read from the
    same tasks ``configure`` will check against, so the two cannot disagree.
    ``buffer_samples`` is the caller's -- MAX does not own it. The tasks are
    released afterwards, or the adapter's own load would be refused."""
    config = read_output_config(AO_TASK, DO_TASK, buffer_samples=BUFFER, system=daq)

    assert config == CONFIG
    assert all(t.closed for t in daq.loaded)

    await adapter(VirtualClock(), daq).configure(config)


def test_a_configuration_read_from_max_defaults_to_the_chosen_buffer(daq):
    """The instrument's ``buffer_samples``, chosen by the user on 2026-09-23:
    5000 (50 ms at 100 kS/s). On Dev1 it rode out 50 ms stalls of the event
    loop and ran dry on a 60 ms one, where the 3276 floor survived only 30 ms;
    an update reaches the pins about 17 ms later than at the floor. A caller
    that passes nothing gets this, not the floor -- the floor is the least the
    board accepts, not a safe depth."""
    config = read_output_config(AO_TASK, DO_TASK, system=daq)

    assert config.buffer_samples == 5000


async def test_a_configuration_read_from_max_keeps_max_order(daq):
    """N17. MAX's order is the order of the caller's array rows, and it is
    not the order of the physical lines -- on the instrument Scan2 is ao3 and
    Scan3 is ao2. Sorting anywhere would swap two scanners' waveforms."""
    daq.saved[AO_TASK].channels = [("scan2", "Dev1/ao3"), ("scan3", "Dev1/ao2")]

    config = read_output_config(AO_TASK, DO_TASK, buffer_samples=BUFFER, system=daq)

    assert config.ao == (Channel("Dev1/ao3", "scan2"), Channel("Dev1/ao2", "scan3"))
    assert dataclasses.replace(config, ao=CONFIG.ao) == CONFIG
