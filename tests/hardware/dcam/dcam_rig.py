"""Helpers for the camera suite: the frame formats it runs in, settings
changed for a test and put back, a software trigger that returns once its
frame has arrived, and a hardware-timed pulse train from the DAQ."""
# For end use and not for commercial purposes.

import asyncio
import contextlib
import ctypes
import time

from stacscope.adapters.dcam import DcamError, DcamFrameCapture, _api

ERR_NOTBUSY = _api._signed(0x80000107)
"""What the Flash answers a software trigger with no capture running (S6)."""

ARRIVAL = 2.0
"""Seconds a software trigger's frame may take to arrive. Far longer than
any exposure and readout the suite sets."""

GRACE = 0.25
"""Seconds allowed for a DCAM wait's thread to return and its caller to run."""

SOFTWARE = [("TRIGGER SOURCE", "SOFTWARE"), ("EXPOSURE TIME", 0.001)]
"""The settings every triggered test starts from: frames come only from
fire_trigger, and each is over quickly."""

ROWS_256 = [("SUBARRAY VSIZE", 256), ("SUBARRAY VPOS", 896), ("SUBARRAY MODE", "ON")]
"""The instrument's light-sheet ROI: 256 rows at the centre of the sensor.
The size is set before the position, so neither ever lies off the sensor."""

FORMATS = {
    "full frame": [],
    "256 rows": ROWS_256,
    "256 rows lightsheet": [("SENSOR MODE", "PROGRESSIVE"), *ROWS_256],
}
"""The frame formats the suite runs in, as settings on top of the camera's
defaults. The sensor mode is set first: it may change what the others allow."""


def delivered(capture: DcamFrameCapture) -> int:
    """How many frames DCAM says this capture has delivered, read directly,
    so that a rig can know a frame arrived without asking the adapter under
    test."""
    info = _api.DCAMCAP_TRANSFERINFO.new()
    capture._call("dcamcap_transferinfo", capture._hdcam, ctypes.pointer(info))
    return info.nFrameCount


def software_trigger(capture: DcamFrameCapture):
    """``trigger(n)`` for a Rig: ``n`` software triggers, one at a time, each
    waited for until its frame has arrived (or ``ARRIVAL`` has passed), then
    ``GRACE`` for a waiting caller to see it, as the contract expects. A
    trigger with no capture running is refused by the Flash; that is a pulse
    that lands nowhere, as the contract expects."""

    async def trigger(n: int) -> None:
        for _ in range(n):
            if not capture._capturing:
                try:
                    capture.fire_trigger()
                except DcamError as error:
                    if error.code != ERR_NOTBUSY:
                        raise
                    continue
                raise AssertionError("the camera accepted a software trigger with no capture running")
            before = delivered(capture)
            capture.fire_trigger()
            deadline = time.monotonic() + ARRIVAL
            while delivered(capture) <= before and time.monotonic() < deadline:
                await asyncio.sleep(0.001)
        await asyncio.sleep(GRACE)

    return trigger


def apply(capture: DcamFrameCapture, changes) -> list[tuple[str, float]]:
    """Make ``changes``, a list of (property, value), in order. Returns what
    each property held before, for ``restore``. If one is refused, those
    already made are put back before the error is raised."""
    saved: list[tuple[str, float]] = []
    try:
        for name, value in changes:
            before = capture.get(name)
            capture.set(name, value)
            saved.append((name, before))
    except BaseException as exc:
        for error in restore(capture, saved, raising=False):
            exc.add_note(f"putting the settings back also failed: {error}")
        raise
    return saved


def restore(capture: DcamFrameCapture, saved, *, raising: bool = True) -> list[Exception]:
    """Put back what ``apply`` saved, last change first, trying every one.
    Raises the first failure, or with ``raising=False`` returns them all."""
    errors: list[Exception] = []
    for name, value in reversed(saved):
        try:
            capture.set(name, value)
        except (DcamError, ValueError) as error:
            errors.append(error)
    if errors and raising:
        raise errors[0]
    return errors


@contextlib.asynccontextmanager
async def camera_with(camera: int, changes, *, in_flight: int = 1):
    """The camera opened, with ``changes`` made; afterwards it is stopped,
    detached, set back as it was found and closed, even if the test closed
    it first (the camera keeps its settings between sessions)."""
    capture = DcamFrameCapture(camera, in_flight=in_flight)
    try:
        saved = apply(capture, changes)
    except BaseException:
        await capture.close()
        raise
    try:
        yield capture
    finally:
        if capture._closed:
            capture = DcamFrameCapture(camera, in_flight=in_flight)
        try:
            await capture.stop()
            await capture.detach()
            restore(capture, saved)
        finally:
            await capture.close()


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
    import nidaqmx  # the nidaqmx extra; only this condition needs it
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
