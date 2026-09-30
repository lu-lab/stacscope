"""Messages for changing a uEye camera's settings.

Each setting has a message of its own, answered with the setting as the
camera took it, which may differ from what was asked: the camera rounds
the exposure to its own step, and bounds the frame rate by the pixel clock
and area of interest. A setting the camera refuses is answered with
Rejected, carrying the reason. GetCameraSettings reads them all.

The shortest trigger period the settings allow is asked for with
GetShortestTriggerPeriod, which every triggered camera answers.
"""

from dataclasses import dataclass

from stacscope.core.protocol import message


@message
@dataclass(frozen=True, slots=True)
class SetPixelFormat:
    """Set the pixel format, "RAW8" or "RAW10". Replied ``PixelFormat``, or
    ``Rejected`` during an acquisition."""

    name: str


@message
@dataclass(frozen=True, slots=True)
class PixelFormat:
    name: str


@message
@dataclass(frozen=True, slots=True)
class SetAOI:
    """Set the area of interest, in pixels from the sensor's top left. On
    the UI-3240, x and y must be even, the width a multiple of 4 and the
    height of 2, at least 16 x 4. Replied ``AOI``, or ``Rejected`` during
    an acquisition."""

    x: int
    y: int
    width: int
    height: int


@message
@dataclass(frozen=True, slots=True)
class AOI:
    x: int
    y: int
    width: int
    height: int


@message
@dataclass(frozen=True, slots=True)
class SetPixelClock:
    """Set the pixel clock, in MHz, to one the camera offers (see
    ``CameraSettings``). A faster clock reads out faster. The frame rate
    and exposure may change with it. Replied ``PixelClock``."""

    mhz: int


@message
@dataclass(frozen=True, slots=True)
class PixelClock:
    mhz: int


@message
@dataclass(frozen=True, slots=True)
class SetFrameRate:
    """Set the frame rate, in frames a second. Under a trigger it is not the
    rate frames come at, but it bounds the exposure: at most one frame
    period. Replied ``FrameRate`` with the rate the camera took."""

    fps: float


@message
@dataclass(frozen=True, slots=True)
class FrameRate:
    fps: float


@message
@dataclass(frozen=True, slots=True)
class SetExposure:
    """Set the exposure time, in milliseconds. It changes at once, even during
    an acquisition. Replied ``Exposure`` with the time the camera took."""

    ms: float


@message
@dataclass(frozen=True, slots=True)
class Exposure:
    ms: float


@message
@dataclass(frozen=True, slots=True)
class SetTrigger:
    """Take a frame on the ``edge`` ("rising" or "falling") of the trigger
    input, and begin exposing ``delay_us`` microseconds after it. Replied
    ``Trigger``."""

    edge: str
    delay_us: int


@message
@dataclass(frozen=True, slots=True)
class Trigger:
    edge: str
    delay_us: int


@message
@dataclass(frozen=True, slots=True)
class GetCameraSettings:
    """Read every setting. Replied ``CameraSettings``."""


@message
@dataclass(frozen=True, slots=True)
class CameraSettings:
    """Every setting as the camera holds it. ``aoi`` is (x, y, width,
    height), and ``pixel_clocks_mhz`` the pixel clocks the camera offers."""

    pixel_format: str
    aoi: tuple[int, ...]
    pixel_clock_mhz: int
    pixel_clocks_mhz: tuple[int, ...]
    frame_rate_fps: float
    exposure_ms: float
    trigger_edge: str
    trigger_delay_us: int
