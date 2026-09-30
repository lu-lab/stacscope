"""A hardware-timed pulse train from the DAQ, for the camera suites that
trigger a camera the way the instrument does. Shared by tests/hardware/dcam
and tests/hardware/ueye, whose conftests put this folder on the path.

Only a digital line is driven. It is clocked by the device's counter 0, not
by ao/SampleClock, so no analog output task is ever created."""

import asyncio
import contextlib

DAQ_RATE = 100_000
"""Samples a second of the DAQ's pulse train: the instrument's output rate."""


@contextlib.contextmanager
def daq_pulses(line: str, period: float, n: int, width: float = 0.0005):
    """``n`` pulses on the DAQ's digital ``line`` (such as Dev1/port0/line0),
    ``period`` seconds apart and ``width`` high, hardware-timed, as the
    instrument triggers the camera. The line is clocked by the device's
    counter 0, so no analog output is touched. Yields an async ``fire`` for
    measure_in_flight: the first call starts the train, and every call
    yields. The line is left low."""
    import nidaqmx  # the nidaqmx extra; only the triggered conditions need it
    import numpy as np
    from nidaqmx.constants import AcquisitionType, LineGrouping

    device = line.split("/")[0]
    step = round(period * DAQ_RATE)
    high = max(1, round(width * DAQ_RATE))
    pattern = np.zeros(n * step + 1, bool)  # ends low
    for i in range(n):
        pattern[i * step + 1 : i * step + 1 + high] = True
    with nidaqmx.Task() as clock, nidaqmx.Task() as pulses:
        clock.co_channels.add_co_pulse_chan_freq(f"{device}/ctr0", freq=DAQ_RATE, duty_cycle=0.5)
        clock.timing.cfg_implicit_timing(sample_mode=AcquisitionType.CONTINUOUS)
        pulses.do_channels.add_do_chan(line, line_grouping=LineGrouping.CHAN_PER_LINE)
        pulses.timing.cfg_samp_clk_timing(
            DAQ_RATE,
            source=f"/{device}/Ctr0InternalOutput",
            sample_mode=AcquisitionType.FINITE,
            samps_per_chan=len(pattern),
        )
        pulses.write(pattern.tolist(), auto_start=False)
        started = False

        async def fire() -> None:
            nonlocal started
            if not started:
                pulses.start()
                clock.start()
                started = True
            await asyncio.sleep(0)

        yield fire
