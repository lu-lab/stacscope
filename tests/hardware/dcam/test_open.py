"""S1-S6 -- DcamFrameCapture over the real dcamapi.dll: open, describe,
change a setting, close.

RUN ON THE CAMERA'S MACHINE. This opens the camera's driver, but attaches no
memory and starts no capture. The settings it changes, the trigger source
and the exposure time, are put back. It reads what the fake DLL cannot
vouch for: that our argument types and structs work against the real
library, and that a session closes cleanly enough to open again.

  S1  The library loads, with every function the adapter calls.
  S2  The camera opens, describes its frames and its stamps, and closes;
      a closed session opens again.
  S3  A camera that is not there is refused, and DCAM can still be opened.
  S4  An error from the real driver comes back as a DcamError with its text.
  S5  Settings by name and by label: the camera's labels, a change applied
      and read back, a rounded exposure, and a refused label.
  S6  A software trigger with no capture running.

Every test adds what it read to a "camera findings" section at the end of
the run; paste that section back.

It runs only against the camera whose DCAM index is in STACSCOPE_CAMERA:

    PowerShell:  $env:STACSCOPE_CAMERA = "0"
                 uv run pytest tests/hardware/dcam/test_open.py
                 Remove-Item Env:STACSCOPE_CAMERA

    Git Bash:    STACSCOPE_CAMERA=0 uv run pytest tests/hardware/dcam/test_open.py

Takes about ten seconds, most of it DCAM starting up.
"""
# For end use and not for commercial purposes.

import ctypes

import pytest

from stacscope.adapters.dcam import DcamError, DcamFrameCapture, _api

pytestmark = pytest.mark.timeout(60)

IDSTR = {"model": 0x04000104, "serial": 0x04000102, "firmware": 0x04000105, "driver": 0x04000106}
"""DCAM_IDSTR codes for the identity strings reported (dcamapi4.h)."""


def identity(capture: DcamFrameCapture) -> str:
    parts = []
    for label, code in IDSTR.items():
        text = ctypes.create_string_buffer(256)
        request = _api.DCAMDEV_STRING.new(iString=code, text=ctypes.addressof(text), textbytes=len(text))
        capture._call("dcamdev_getstring", capture._hdcam, ctypes.pointer(request))
        parts.append(f"{label} {text.value.decode(errors='replace')}")
    return ", ".join(parts)


@pytest.fixture
async def opened(camera):
    """The camera, closed after the test whatever it did: a session left
    open makes every later open in this process fail."""
    capture = DcamFrameCapture(camera, in_flight=0)
    yield capture
    await capture.close()


def test_s1_the_library_loads_with_every_function_the_adapter_calls(report):
    """The binding names every function the adapter calls; a DLL older than
    DCAM-API 4 lacks some, and load() must say so rather than fail later,
    mid-acquisition, on the first call to one."""
    library = _api.load()
    report(f"{len(_api.FUNCTIONS)} functions bound")
    assert all(getattr(library, name).restype is ctypes.c_int32 for name in _api.FUNCTIONS)


async def test_s2_the_camera_opens_describes_its_frames_and_closes(opened, report):
    """The frame format and stamp capabilities read through the real
    structs. A struct laid out wrong reads garbage here, or crashes, before
    any memory is handed to the driver; the consistency checks are what a
    layout computed from garbage would fail."""
    fmt = opened.frame_format()
    meta = opened.metadata()
    report(identity(opened))
    report(
        f"shape {fmt.shape}, dtype {fmt.dtype}, strides {fmt.strides}, "
        f"frame_bytes {fmt.frame_bytes}, offset {fmt.frame_offset}"
    )
    report(
        f"stamps: framestamp {opened._has_framestamps}, timestamp {opened._has_timestamps}; "
        f"metadata fields {meta.fields!r}, source {meta.device_time_source!r}"
    )
    height, width = fmt.shape
    assert height > 0 and width > 0
    assert fmt.strides[0] >= width * fmt.strides[1]
    assert fmt.frame_offset + (height - 1) * fmt.strides[0] + width * fmt.strides[1] <= fmt.frame_bytes


async def test_s2_a_closed_session_opens_again(camera):
    """Close uninitializes DCAM. If it did not, DCAM's one session per process
    would refuse the next open, and a device actor could never be relaunched
    in the same process."""
    for _ in range(2):
        capture = DcamFrameCapture(camera, in_flight=0)
        await capture.close()


async def test_s3_a_camera_that_is_not_there_is_refused_and_dcam_still_opens(camera, report):
    """The refusal comes after dcamapi_init has succeeded, so it proves the
    unwinding path closes what it opened: a leak here would leave the next
    open refused."""
    with pytest.raises(ValueError, match="there is no camera 99") as caught:
        DcamFrameCapture(99, in_flight=0)
    report(str(caught.value))
    capture = DcamFrameCapture(camera, in_flight=0)
    await capture.close()


async def test_s4_an_error_from_the_driver_carries_its_code_and_text(opened, report):
    """Reading a property ID that does not exist is refused by DCAM itself,
    harmlessly. The error must name the call, and carry DCAM's own text
    through dcamdev_getstring's real struct, or a user reading a fault from
    the camera has only a hex code to go on."""
    with pytest.raises(DcamError) as caught:
        opened._call("dcamprop_getvalue", opened._hdcam, 0, ctypes.pointer(ctypes.c_double()))
    report(str(caught.value))
    assert caught.value.call == "dcamprop_getvalue"
    assert caught.value.code < 0
    assert caught.value.text


async def test_s5_settings_by_name_and_label_on_the_camera(opened, report):
    """The settings calls through the real bindings (setgetvalue and
    queryvalue are typed by hand), against labels the fake could only
    guess. Everything changed is put back, since the camera keeps its
    settings between sessions."""
    for name in ("SENSOR MODE", "TRIGGER SOURCE", "TRIGGER ACTIVE", "TRIGGER POLARITY", "READOUT DIRECTION"):
        report(f"{name}: {opened.label(name)} of {list(opened.labels(name))}")
    source = opened.label("TRIGGER SOURCE")
    exposure = opened.get("EXPOSURE TIME")
    try:
        assert opened.set("TRIGGER SOURCE", "SOFTWARE") == "SOFTWARE"
        assert opened.label("TRIGGER SOURCE") == "SOFTWARE"
        applied = opened.set("EXPOSURE TIME", 0.0123456)
        report(f"EXPOSURE TIME 0.0123456 s applied as {applied!r}")
        assert applied == pytest.approx(0.0123456, abs=1e-4)
        assert opened.get("EXPOSURE TIME") == applied
        with pytest.raises(ValueError, match="SOFTWARE"):
            opened.set("TRIGGER SOURCE", "SOFT")
    finally:
        opened.set("TRIGGER SOURCE", source)
        opened.set("EXPOSURE TIME", exposure)
    assert opened.label("TRIGGER SOURCE") == source


async def test_s6_a_software_trigger_with_no_capture_running(opened, report):
    """The reference says a software trigger is "only effective while
    capturing in SOFTWARE trigger mode", not whether it is refused
    otherwise. The adapter passes DCAM's answer on either way; this records
    which it is, with the trigger source left as it was found. The Flash
    refuses it with DCAMERR_NOTBUSY (2026-09-25), which the fake and the
    rig's software trigger both rely on."""
    try:
        opened.fire_trigger()
    except DcamError as error:
        report(f"refused, trigger source {opened.label('TRIGGER SOURCE')}: {error}")
    else:
        report(f"accepted, trigger source {opened.label('TRIGGER SOURCE')}")
