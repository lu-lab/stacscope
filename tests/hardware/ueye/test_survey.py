"""Y1-Y8 -- what the uEye does where its manual is silent or model-dependent,
and what the adapter's design rests on (the plan's "5f -- the uEye camera").

RUN ON THE CAMERA'S MACHINE. This captures, in freerun, under software
trigger and (with STACSCOPE_UEYE_TRIGGER) on DAQ pulses; the DAQ drives only
that digital line, clocked by a counter, never an analog output. Frames may
be dark: where the values matter, the sensor's test image is used. Nothing
is written to the camera's EEPROM, and is_InitCamera starts from defaults
(Y8), so there is nothing to put back.

  Y1  Which camera this is (CP Rev. 1 or 2), and what u64TimestampDevice
      marks: under software trigger, whether the stamp moves with the
      exposure time (it marks the end of the transfer) or not (it marks the
      trigger). With the trigger line, whether each DAQ pulse gives one
      frame, and the device clock's rate against the DAQ's.
  Y2  Image memory in our own shared-memory segment: accepted, and filled.
  Y3  Slot order under queue mode, and what the driver does when a slot is
      still locked when its turn comes round.
  Y4  The largest sequence the driver accepts.
  Y5  Where the 10- and 12-bit RAW formats put their bits.
  Y6  Whether u64FrameNumber counts every frame, across captures and
      sessions.
  Y7  Whether the camera opens again after a process capturing from it is
      killed.
  Y8  Whether settings survive is_ExitCamera and is_InitCamera.
  Y9  The full sensor in RAW10, triggered from the DAQ at 20 frames a
      second, with no frame lost (the user's requirement). Ten seconds;
      set STACSCOPE_SOAK_SECONDS to run it for longer.

Each adds its result to the "uEye findings" section at the end of the run;
paste that section back.

    PowerShell:  $env:STACSCOPE_UEYE = "1"
                 $env:STACSCOPE_UEYE_TRIGGER = "Dev1/port0/line2"
                 uv run --extra ueye --extra nidaqmx pytest tests/hardware/ueye
                 Remove-Item Env:STACSCOPE_UEYE, Env:STACSCOPE_UEYE_TRIGGER

    Git Bash:    STACSCOPE_UEYE=1 STACSCOPE_UEYE_TRIGGER=Dev1/port0/line2 \\
                     uv run --extra ueye --extra nidaqmx pytest tests/hardware/ueye

Takes about a minute and a half.
"""

import asyncio
import os
import pathlib
import subprocess
import sys
import time

import numpy as np
import pytest
import ueye_rig as rig
from ueye_rig import Sequence, opened, ueye

pytestmark = pytest.mark.timeout(300)

SOAK = float(os.environ.get("STACSCOPE_SOAK_SECONDS", "10"))
"""Seconds Y9 triggers for."""

TICK = 1e-7
"""Seconds per count of u64TimestampDevice (0.1 us, the manual)."""

ROWS_256 = (0, 384, 1280, 256)
"""256 rows at the centre of the sensor, (x, y, width, height): short enough
a readout for the DAQ-triggered test to run fast."""


def stamps(taken) -> np.ndarray:
    return np.array([info.u64TimestampDevice.value for _, _, info, _ in taken], dtype=np.int64)


def failures(taken) -> list[int]:
    return [ret for ret, _, _, _ in taken if ret != ueye().IS_SUCCESS]


def test_y1_the_camera_is_a_cp_rev_1(camera, report):
    """The manual gives the stamp a different meaning on a CP Rev. 2 (the
    trigger) from a Rev. 1 (the end of the transfer). Only the Rev. 2 has
    internal image memory, so the feature bit tells them apart."""
    u = ueye()
    with opened(camera) as h:
        info = u.CAMINFO()
        rig.ok(u.is_GetCameraInfo(h, info), "is_GetCameraInfo")
        sensor = u.SENSORINFO()
        rig.ok(u.is_GetSensorInfo(h, sensor), "is_GetSensorInfo")
        bits = rig.features(h)
    rev2 = bool(bits & u.IS_DEVICE_FEATURE_CAP_MEMORY_MODE)
    report(
        f"{sensor.strSensorName.decode()} S/N {info.SerNo.decode()}, type {info.Type.value:#x}, "
        f"features {bits:#x}: {'CP Rev. 2 (image memory)' if rev2 else 'CP Rev. 1 (no image memory)'}"
    )
    assert not rev2, "a CP Rev. 2 stamps the trigger; the rest of Y1 assumes a Rev. 1"


def test_y1_what_the_device_stamp_marks(camera, report):
    """Under software trigger, the stamp is compared with the host's time at
    the trigger call, at a short and a long exposure. The two clocks'
    offset is unknown but fixed, so it cancels in the difference: if the
    stamp moves by the change in exposure, it is taken after the exposure
    (the end of the transfer, as the manual says of a Rev. 1); if it does
    not move, it is taken at the trigger. Which one decides what device
    time means in the buffer's metadata."""
    u = ueye()
    short, long = 1.0, 40.0
    offsets: dict[float, np.ndarray] = {}
    with opened(camera) as h:
        rig.set_color_mode(h, u.IS_CM_SENSOR_RAW8)
        rig.set_trigger(h, u.IS_SET_TRIGGER_SOFTWARE)
        with Sequence(h, 4, 8) as sequence:
            for ms in (short, long):
                actual = rig.set_exposure(h, ms)
                d = []
                for _ in range(10):
                    t = time.perf_counter()
                    rig.ok(u.is_FreezeVideo(h, u.IS_DONT_WAIT), "is_FreezeVideo")
                    ret, mem_id, info = sequence.wait()
                    assert ret == u.IS_SUCCESS, f"software trigger gave {ret}"
                    d.append(info.u64TimestampDevice.value * TICK - t)
                    sequence.unlock(mem_id)
                    time.sleep(0.02)
                report(f"exposure {actual:.3f} ms set; stamp minus trigger call, spread {np.ptp(d) * 1e3:.3f} ms")
                offsets[ms] = np.array(d)
    moved = (np.median(offsets[long]) - np.median(offsets[short])) * 1e3
    report(f"the stamp moved {moved:.2f} ms for {long - short:.1f} ms more exposure")
    assert abs(moved - (long - short)) < 5 or abs(moved) < 5, "the stamp moved by neither 0 nor the exposure change"
    report("stamp marks: " + ("after the exposure (transfer end)" if abs(moved) >= 5 else "the trigger"))


async def test_y1_each_daq_pulse_gives_one_frame(camera, trigger_line, report):
    """The instrument triggers the uEye from the DAQ, rising edge. Every
    pulse must give exactly one frame, and the frames' stamps must be spaced
    as the pulses were, which also measures the device clock against the
    DAQ's timebase."""
    from daq_pulses import daq_pulses

    u = ueye()
    n, period = 80, 0.025
    with opened(camera) as h:
        rig.set_color_mode(h, u.IS_CM_SENSOR_RAW8)
        rig.set_aoi(h, *ROWS_256)
        rig.set_exposure(h, 1.0)
        rig.set_trigger(h, u.IS_SET_TRIGGER_LO_HI)
        with Sequence(h, 16, 8) as sequence:
            rig.reset_capture_errors(h)
            sequence.live()
            with daq_pulses(trigger_line, period, n) as fire:
                await fire()
                taken = await asyncio.to_thread(sequence.take, n, 1000)
                extra = sequence.take(1, 300)
            errors = rig.capture_errors(h)
    assert failures(taken) == [], f"waits failed: {failures(taken)}; capture errors {errors}"
    assert failures(extra) == [u.IS_TIMED_OUT], "more frames than pulses"
    intervals = np.diff(stamps(taken)) * TICK
    # The mean interval is the span over n - 1, so it is only as good as the
    # two end stamps, each of which carries the transfer's jitter.
    rate_ppm = (np.mean(intervals) / period - 1) * 1e6
    resolution_ppm = np.ptp(intervals) / ((n - 1) * period) * 1e6
    report(
        f"{n} pulses, {len(taken)} frames; interval median {np.median(intervals) * 1e3:.4f} ms, "
        f"spread {np.ptp(intervals) * 1e6:.1f} us; device clock {rate_ppm:+.1f} ppm against the DAQ, "
        f"resolved to about {resolution_ppm:.0f} ppm"
    )
    assert np.all(np.abs(intervals - period) < 0.001), "a frame does not follow its own pulse"


def test_y2_image_memory_in_our_segment_is_filled(camera, report):
    """The manual asks for memory that is "locked globally". A shared-memory
    segment is a mapped file; the driver must accept its pages and write
    into them. The sensor's white test image makes every pixel of a filled
    frame the same known value, and the slots start zeroed."""
    u = ueye()
    with opened(camera) as h:
        rig.set_color_mode(h, u.IS_CM_SENSOR_RAW8)
        rig.set_test_image(h, u.IS_TEST_IMAGE_WHITE)
        with Sequence(h, 4, 8) as sequence:
            pitch = sequence.pitch()
            sequence.live()
            ret, mem_id, _ = sequence.wait()
            assert ret == u.IS_SUCCESS
            image = sequence.frame(mem_id).copy()
            sequence.unlock(mem_id)
            width = sequence.format.shape[1]
    values = np.unique(image)
    report(f"pitch {pitch} for width {width}; white test image reads {values.tolist()}")
    assert pitch == width, "rows are padded"
    assert len(values) == 1 and values[0] > 0, "the frame did not land in the segment as the test image"


def test_y3_frames_fill_the_slots_in_order(camera, report):
    """The buffer assumes frame k lands in slot k mod N. Queue mode says which
    memory each frame went to; three times round a ring of eight must visit
    the slots in order."""
    u = ueye()
    with opened(camera) as h:
        rig.set_color_mode(h, u.IS_CM_SENSOR_RAW8)
        rig.set_aoi(h, *ROWS_256)
        with Sequence(h, 8, 8) as sequence:
            sequence.live()
            taken = sequence.take(24)
            ids = sequence.ids
    order = [mem_id for _, mem_id, _, _ in taken]
    report(f"memory IDs {ids}; filled {order}")
    assert failures(taken) == []
    assert order == [ids[k % 8] for k in range(24)]


def test_y3_a_locked_slot_when_its_turn_comes(camera, report):
    """The adapter unlocks each slot as soon as its info is read, so a slot
    still locked a whole ring later means the host fell behind. The driver
    may then skip it (and the next frame lands out of place, which the
    adapter must catch), or refuse the frame (an error in the queue). The
    first frame's slot is held. On this camera the driver skips it, in
    silence."""
    u = ueye()
    with opened(camera) as h:
        rig.set_color_mode(h, u.IS_CM_SENSOR_RAW8)
        rig.set_aoi(h, *ROWS_256)
        with Sequence(h, 4, 8) as sequence:
            rig.reset_capture_errors(h)
            sequence.live()
            ret, held, _ = sequence.wait()
            assert ret == u.IS_SUCCESS
            later = []
            for _ in range(10):
                ret, mem_id, _ = sequence.wait(1000)
                later.append(mem_id if mem_id is not None else f"ret {ret}")
                if mem_id is not None:
                    sequence.unlock(mem_id)
            sequence.stop()
            errors = rig.capture_errors(h)
            ids = sequence.ids
    report(f"held {held} of {ids}; then {later}; capture errors {errors}")
    # Found on the camera (2026-09-30): the driver skips the held slot and
    # says nothing, so the next frame lands out of place. Only the adapter's
    # slot-order check catches it.
    others = [i for i in ids if i != held]
    assert later == [others[k % len(others)] for k in range(10)], "the driver no longer skips a locked slot"
    assert errors == {}, "the driver now reports a skipped slot"


def test_y4_the_largest_sequence(camera, report):
    """How many image memories one sequence takes is not documented. Tried
    at the smallest AOI, doubling, up to 8192; the largest accepted then
    captures a few frames."""
    u = ueye()
    largest = None
    refused = None
    with opened(camera) as h:
        rig.set_color_mode(h, u.IS_CM_SENSOR_RAW8)
        rig.set_aoi(h, 0, 0, 64, 16)
        n = 64
        while n <= 8192:
            try:
                with Sequence(h, n, 8):
                    pass
            except rig.UEyeError as error:
                refused = (n, str(error))
                break
            largest = n
            n *= 2
        assert largest is not None
        with Sequence(h, largest, 8) as sequence:
            sequence.live()
            taken = sequence.take(5)
    report(f"largest accepted {largest}; refused {refused}")
    assert failures(taken) == []


@pytest.mark.parametrize("name, bits", [("SENSOR_RAW10", 10), ("SENSOR_RAW12", 12), ("SENSOR_RAW16", 16)])
def test_y5_where_the_raw_bits_sit(camera, report, name, bits):
    """10- and 12-bit data sit in 16 bits, from the LSB or the MSB depending
    on the model. White is full scale: 2**bits - 1 from the LSB, or that
    shifted to the top. The sensor's gray wedge shows which low bits are
    ever set. Until this is known, only RAW8 is exposed."""
    u = ueye()
    mode = getattr(u, f"IS_CM_{name}")
    seen = {}
    with opened(camera) as h:
        try:
            rig.set_color_mode(h, mode)
        except rig.UEyeError as error:
            report(f"{name} refused: {error}")
            pytest.skip(f"{name} refused")
        for label, image in (("white", u.IS_TEST_IMAGE_WHITE), ("wedge", u.IS_TEST_IMAGE_WEDGE_GRAY_SENSOR)):
            rig.set_test_image(h, image)
            with Sequence(h, 2, 16) as sequence:
                sequence.live()
                ret, mem_id, _ = sequence.wait()
                assert ret == u.IS_SUCCESS
                seen[label] = sequence.frame(mem_id).copy()
                sequence.unlock(mem_id)
    white = np.unique(seen["white"])
    wedge = seen["wedge"]
    low = int(np.bitwise_or.reduce(wedge.ravel()) & 0xFFFF)
    report(
        f"{name}: white {white.tolist()}, wedge {wedge.min()}..{wedge.max()} "
        f"({len(np.unique(wedge))} values), bits ever set {low:#06x}"
    )


def test_y6_frame_numbers_count_every_frame(camera, report):
    """u64FrameNumber is "internal image number (not chronological)". If it
    counts every frame the sensor makes, a gap in it is a lost frame, as the
    Flash's frame stamps are. Read within a capture, across a stop and
    start, and across a close and open."""
    u = ueye()
    runs = []
    for _session in range(2):
        with opened(camera) as h:
            rig.set_color_mode(h, u.IS_CM_SENSOR_RAW8)
            rig.set_aoi(h, *ROWS_256)
            with Sequence(h, 8, 8) as sequence:
                for _capture in range(2):
                    sequence.live()
                    taken = sequence.take(20)
                    sequence.stop()
                    assert failures(taken) == []
                    runs.append([info.u64FrameNumber.value for _, _, info, _ in taken])
    within = [set(np.diff(r).tolist()) for r in runs]
    report(f"first numbers {[r[0] for r in runs]}, last {[r[-1] for r in runs]}; steps within {within}")
    assert all(steps == {1} for steps in within)


def test_y7_the_camera_opens_after_its_process_is_killed(camera, report):
    """A device's process can die with the camera capturing. The driver must
    let the next process open it, or a crash would need a replug or a
    reboot, as a wedged DCAM driver can."""
    u = ueye()
    rig_path = pathlib.Path(rig.__file__)
    child = subprocess.Popen(
        [sys.executable, str(rig_path), str(camera)],
        stdout=subprocess.PIPE,
        text=True,
        env={**os.environ, "PYTHONWARNINGS": "ignore"},
    )
    try:
        line = child.stdout.readline()
        assert line.strip() == "capturing", f"the child never captured: {line!r}"
        child.kill()
        child.wait(10)
    finally:
        if child.poll() is None:
            child.kill()
    start = time.monotonic()
    last = None
    while time.monotonic() - start < 20:
        try:
            with opened(camera) as h:
                opened_after = time.monotonic() - start
                rig.set_color_mode(h, u.IS_CM_SENSOR_RAW8)
                with Sequence(h, 4, 8) as sequence:
                    sequence.live()
                    taken = sequence.take(3)
            break
        except rig.UEyeError as error:
            last = error
            time.sleep(0.5)
    else:
        pytest.fail(f"the camera would not open for 20 s after the kill: {last}")
    report(f"opened {opened_after:.2f} s after the kill (last refusal: {last})")
    assert failures(taken) == []


def test_y8_settings_do_not_survive_exit(camera, report):
    """The suite changes settings without putting them back, relying on
    is_InitCamera to start from the camera's defaults. If a setting
    survived, a crash in a test would leave the instrument's camera
    altered for the next program (µManager on the instrument)."""
    u = ueye()
    with opened(camera) as h:
        before = (rig.color_mode(h), rig.aoi(h), rig.pixel_clock(h), round(rig.exposure(h), 3))
        rig.set_color_mode(h, u.IS_CM_SENSOR_RAW8)
        rig.set_aoi(h, *ROWS_256)
        rig.set_pixel_clock(h, 40 if before[2] != 40 else 30)
        rig.set_exposure(h, 5.0)
        changed = (rig.color_mode(h), rig.aoi(h), rig.pixel_clock(h), round(rig.exposure(h), 3))
    with opened(camera) as h:
        after = (rig.color_mode(h), rig.aoi(h), rig.pixel_clock(h), round(rig.exposure(h), 3))
    report(f"(colour mode, AOI, pixel clock, exposure) at open {before}, changed {changed}, reopened {after}")
    assert changed != before
    assert after == before


async def test_y9_raw10_full_frame_triggered_at_20_fps(camera, trigger_line, report):
    """The user's requirement for testing (2026-09-30): the full sensor in
    RAW10, triggered from the DAQ at 20 frames a second, sustained. Every
    pulse must give one frame, in its slot, with no gap in the frame
    numbers and no capture error.

    Found on the camera (2026-09-30): under a trigger this sensor does not
    expose while it reads out. At 31 MHz (22 fps freerun, so about 45 ms of
    readout) with 10 ms of exposure, every other pulse was ignored, and no
    capture error counted the missed triggers. So the pixel clock chosen is
    the lowest whose readout (one over the freerun maximum) plus the
    exposure fits in 90% of the pulse period."""
    from daq_pulses import daq_pulses

    u = ueye()
    rate, seconds = 20.0, SOAK
    n = int(rate * seconds)
    with opened(camera) as h:
        rig.set_color_mode(h, u.IS_CM_SENSOR_RAW10)
        exposure_ms = 10.0
        chosen = None
        for mhz in rig.pixel_clocks(h):
            rig.set_pixel_clock(h, mhz)
            fastest = rig.set_frame_rate(h, 1000.0)  # the driver sets its maximum
            if 1 / fastest + exposure_ms / 1e3 <= 0.9 / rate:
                chosen = mhz
                break
        assert chosen is not None, f"no pixel clock reads out in time for {rate} fps in RAW10 at full frame"
        exposure = rig.set_exposure(h, exposure_ms)
        rig.set_trigger(h, u.IS_SET_TRIGGER_LO_HI)
        with Sequence(h, 20, 16) as sequence:
            ids = sequence.ids
            rig.reset_capture_errors(h)
            sequence.live()
            with daq_pulses(trigger_line, 1 / rate, n) as fire:
                await fire()
                taken = await asyncio.to_thread(sequence.take, n, 1000, until_failure=True)
                extra = sequence.take(1, 300)
            errors = rig.capture_errors(h)
            shape = sequence.format.shape
    order = [mem_id for _, mem_id, _, _ in taken]
    numbers = np.diff([info.u64FrameNumber.value for _, _, info, _ in taken if info is not None])
    intervals = np.diff(stamps([t for t in taken if t[2] is not None])) * TICK
    report(
        f"{shape[1]}x{shape[0]} RAW10 at {chosen} MHz (freerun max {fastest:.1f} fps), "
        f"exposure {exposure:.3f} ms; {n} pulses at {rate:.0f}/s, {len(taken) - len(failures(taken))} frames; "
        f"interval {np.median(intervals) * 1e3:.3f} ms, spread {np.ptp(intervals) * 1e6:.0f} us; "
        f"capture errors {errors}"
    )
    assert failures(taken) == [], f"waits failed: {failures(taken)}; capture errors {errors}"
    assert failures(extra) == [u.IS_TIMED_OUT], "more frames than pulses"
    assert order == [ids[k % len(ids)] for k in range(n)], "a frame landed out of place"
    assert set(numbers.tolist()) == {1}, "a gap in the frame numbers"
    assert errors == {}
