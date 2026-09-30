"""An IDS uEye camera as a frame grabber, through the uEye API.

UEyeFrameCapture gives the camera's driver the slots of an image buffer as
its image memory, and the driver writes each frame into the next slot. It
also collects each frame's frame number and time stamp. It is the
FrameCapture port under the image-streaming role, which publishes what
arrives.

It also reads and changes the camera's settings: pixel format (RAW8 or
RAW10), area of interest, pixel clock, frame rate, exposure time and
trigger edge and delay. It says the shortest trigger period the current
settings allow: a trigger that comes sooner is ignored by the camera, and
reported here as a lost frame. It can also send a trigger from software.

Frames are taken on the rising edge of the camera's trigger input by
default. The IDS Software Suite must be installed, with pyueye (the "ueye"
extra). Opening the camera waits a few seconds for one that a crashed
process still holds.
"""

import asyncio
import ctypes
import threading
import time
import warnings

import numpy as np

from stacscope.core.buffer import MetaField, Metadata, TimeSource
from stacscope.core.ports import STAMP_DTYPE, FrameFormat, FramesLost

PIXEL_FORMATS = {"RAW8": ("IS_CM_SENSOR_RAW8", 8, "u1"), "RAW10": ("IS_CM_SENSOR_RAW10", 16, "<u2")}
"""Each pixel format by name: the colour mode, the bits a pixel takes in
memory, and its numpy dtype."""
# RAW, not MONO: MONO passes through the camera's LUT and gamma (user,
# decision 4). RAW10 sits in the low 10 bits of 16 (Y5); RAW12 and RAW16
# only rescale the sensor's 10 bits (Y5), so they are not offered (user).

TRIGGER_EDGES = {"rising": "IS_SET_TRIGGER_LO_HI", "falling": "IS_SET_TRIGGER_HI_LO"}

OPEN_TIMEOUT = 5.0
"""Seconds opening goes on retrying a camera that says it is in use."""
# Y7: a camera whose process was killed answers IS_DEVICE_IN_USE for about
# 1.5 s, then opens.

WAIT_SLICE = 0.1
"""The longest single queue wait, in seconds. A stop that lands just before a
wait begins is not seen by that wait, so this bounds how long it can go
unnoticed."""

DEVICE_CLOCK_HZ = 1e7
"""u64TimestampDevice counts 0.1 us ticks."""

_ERROR_NAMES = {
    -1: "IS_NO_SUCCESS",
    1: "IS_INVALID_CAMERA_HANDLE",
    3: "IS_CANT_OPEN_DEVICE",
    49: "IS_INVALID_MEMORY_POINTER",
    122: "IS_TIMED_OUT",
    125: "IS_INVALID_PARAMETER",
    157: "IS_OPERATION_ABORTED",
    213: "IS_DEVICE_IN_USE",
}
# From ueye.h through pyueye. Only codes this adapter has met or expects:
# pyueye's constants share numbers between unrelated names, so a reverse
# lookup over all of them would often name the wrong one.

_IN_USE = 213
_OPEN_RETRY = 0.25  # seconds between opens while the camera is in use


class UEyeUnavailable(RuntimeError):
    """The uEye API cannot be used here: pyueye, or the IDS driver it loads,
    is not installed."""


class UEyeError(RuntimeError):
    """A uEye API call failed. ``call`` is the function, and ``code`` what it
    returned."""

    def __init__(self, call: str, code: int) -> None:
        self.call = call
        self.code = code
        name = _ERROR_NAMES.get(code)
        super().__init__(f"{call} failed: {name} ({code})" if name else f"{call} failed: {code}")


def load():
    """pyueye's ``ueye`` module. Raises ``UEyeUnavailable`` if pyueye or the
    uEye driver is not installed."""
    try:
        with warnings.catch_warnings():
            # Its docstrings hold invalid escapes, which 3.14 warns about once.
            warnings.simplefilter("ignore", SyntaxWarning)
            from pyueye import ueye
    except ImportError as exc:
        raise UEyeUnavailable(
            "pyueye could not be loaded: install the IDS Software Suite, and the 'ueye' extra"
        ) from exc
    return ueye


class UEyeFrameCapture:
    """Implements ``FrameCapture`` on the uEye camera with ID ``camera`` (0
    for the first one free).

    ``in_flight`` is how many frames past the newest one reported the driver
    may already be writing; it is measured on each camera, so there is no
    default. ``pixel_format`` is "RAW8" or "RAW10". ``slot_alignment`` is a
    boundary each slot must start on; by default there is none.
    ``open_timeout`` is how long, in seconds, to go on trying a camera that
    another process holds or held. ``ueye`` replaces pyueye's module, for
    tests.

    Opening initialises the camera, which resets every setting to the
    camera's defaults, then sets the pixel format and the rising-edge
    trigger. Raises ``UEyeUnavailable`` without pyueye, ``ValueError`` for
    an unknown pixel format, and ``UEyeError`` if the camera refuses; a
    camera opened is closed again first.
    """

    def __init__(
        self,
        camera: int = 0,
        *,
        in_flight: int,
        pixel_format: str = "RAW8",
        slot_alignment: int = 1,
        open_timeout: float = OPEN_TIMEOUT,
        ueye=None,
    ) -> None:
        _format(pixel_format)
        self._u = ueye if ueye is not None else load()
        self._in_flight = in_flight
        self._slot_alignment = slot_alignment
        self._h = None
        self._closed = False
        self._slots: np.ndarray | None = None
        self._memories: list[tuple[object, int]] = []
        """(pointer, memory ID) for each slot the driver holds, in slot order."""
        self._in_sequence = False
        self._queued = False
        self._capturing = False
        self._consumer: threading.Thread | None = None
        self._stopping = threading.Event()
        self._loop: asyncio.AbstractEventLoop | None = None
        self._changed: asyncio.Event | None = None
        # Written by the consumer thread, read by callers: under _state.
        self._state = threading.Lock()
        self._delivered = 0
        self._last_frame_number: int | None = None
        self._failure: Exception | None = None
        self._hw_frame: np.ndarray | None = None
        self._ticks: np.ndarray | None = None
        self._host: np.ndarray | None = None
        try:
            self._open(camera, open_timeout)
            self._set_color_mode(pixel_format)
            self._call("is_SetExternalTrigger", self._h, getattr(self._u, TRIGGER_EDGES["rising"]))
        except BaseException as exc:
            errors: list[UEyeError] = []
            self._teardown(errors)
            for error in errors:
                exc.add_note(f"closing again also failed: {error}")
            raise

    # --- FrameCapture ----------------------------------------------------------------

    def frame_format(self) -> FrameFormat:
        self._require_open()
        _, bits, dtype = _format(self.pixel_format())
        _, _, width, height = self.aoi()
        size = bits // 8
        return FrameFormat(
            shape=(height, width),
            dtype=dtype,
            strides=(width * size, size),
            frame_bytes=width * height * size,
            slot_alignment=self._slot_alignment,
            in_flight=self._in_flight,
        )

    def metadata(self) -> Metadata:
        self._require_open()
        # The raw stamp: the camera's clock when the frame finished reaching
        # the PC, not corrected to the exposure (user, 2026-09-30, from Y1).
        return Metadata(
            fields=MetaField.HW_FRAME | MetaField.DEVICE_TIME | MetaField.HOST_TIME,
            device_time_source=TimeSource.CAMERA,
            device_clock_hz=DEVICE_CLOCK_HZ,
        )

    async def attach(self, slots: np.ndarray) -> None:
        try:
            self._require_open()
            if self._slots is not None:
                raise RuntimeError("frame memory is already attached; detach it first")
            fmt = self.frame_format()
            if slots.dtype != np.uint8 or slots.ndim != 2 or slots.strides[1] != 1:
                raise ValueError("slots must be an (n_slots, slot_bytes) array of contiguous bytes")
            if slots.shape[1] < fmt.frame_bytes:
                raise ValueError(f"slots of {slots.shape[1]} bytes cannot hold {fmt.frame_bytes}")
            if slots.ctypes.data % fmt.slot_alignment or slots.strides[0] % fmt.slot_alignment:
                raise ValueError(f"slots must start on {fmt.slot_alignment}-byte boundaries")
            if not slots.flags.writeable:
                raise ValueError("slots must be writable")
            height, width = fmt.shape
            bits = np.dtype(fmt.dtype).itemsize * 8
            self._slots = slots
            try:
                for i in range(len(slots)):
                    pointer = self._u.c_mem_p(slots.ctypes.data + i * slots.strides[0])
                    mem_id = self._u.c_int()
                    self._call("is_SetAllocatedImageMem", self._h, width, height, bits, pointer, mem_id)
                    self._memories.append((pointer, mem_id.value))
                pitch = self._pitch()
                if pitch != fmt.strides[0]:
                    raise ValueError(f"the driver's row pitch is {pitch} bytes, not the format's {fmt.strides[0]}")
                self._in_sequence = True
                for pointer, mem_id in self._memories:
                    self._call("is_AddToSequence", self._h, pointer, mem_id)
            except BaseException as exc:
                # Memory the driver holds is memory it may write into after
                # the role believes it free: give back whatever went in.
                errors: list[UEyeError] = []
                self._release(errors)
                for error in errors:
                    exc.add_note(f"giving it back also failed: {error}")
                raise
            n = len(slots)
            self._hw_frame = np.zeros(n, "<u8")
            self._ticks = np.zeros(n, "<i8")
            self._host = np.zeros(n, "<i8")
        except BaseException:
            # This frame, and so the slots, stay alive for as long as anyone
            # holds the error, through its traceback, and the segment cannot
            # close while a view of it is alive (K9). Nothing else in this
            # frame refers to the slots' memory: keep it that way, or delete
            # that too.
            del slots
            raise

    async def start(self) -> None:
        self._require_open()
        if self._slots is None:
            raise RuntimeError("attach frame memory before starting")
        if self._capturing:
            raise RuntimeError("already capturing")
        with self._state:
            self._delivered = 0
            self._last_frame_number = None
            self._failure = None
        errors: list[UEyeError] = []
        # A fresh queue for each capture: one left from the last would hand
        # this capture its frames and capture statuses.
        if self._queued and self._attempt(errors, "is_ImageQueue", self._h, self._u.IS_IMAGE_QUEUE_CMD_EXIT, None, 0):
            self._queued = False
        _raise_first(errors)
        # Read to reset it: triggers missed before this capture are not its.
        self._missed_triggers()
        self._call("is_ImageQueue", self._h, self._u.IS_IMAGE_QUEUE_CMD_INIT, None, 0)
        self._queued = True
        try:
            self._call("is_CaptureVideo", self._h, self._u.IS_DONT_WAIT)
        except BaseException as exc:
            if self._attempt(errors, "is_ImageQueue", self._h, self._u.IS_IMAGE_QUEUE_CMD_EXIT, None, 0):
                self._queued = False
            for error in errors:
                exc.add_note(f"closing the queue also failed: {error}")
            raise
        self._capturing = True
        # The driver locks each memory it fills until it is handed back, and
        # with every memory locked it loses the frame (probe, 2026-09-30). So
        # frames are taken from the queue as they land, by a thread of their
        # own, not when a caller happens to ask: the port's grabber writes on
        # whether or not anyone is waiting.
        self._loop = asyncio.get_running_loop()
        self._changed = asyncio.Event()
        self._stopping.clear()
        self._consumer = threading.Thread(target=self._consume, name="ueye-frames", daemon=True)
        self._consumer.start()

    async def wait_frames(self, seen: int, timeout: float) -> int:
        self._require_open()
        deadline = time.monotonic() + timeout
        while True:
            if self._changed is not None:
                self._changed.clear()  # before looking, so no change is missed
            with self._state:
                failure, delivered = self._failure, self._delivered
            if failure is not None:
                raise failure
            consumer = self._consumer
            if delivered > seen or consumer is None or not consumer.is_alive():
                return delivered
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return delivered
            try:
                await asyncio.wait_for(self._changed.wait(), remaining)
            except TimeoutError:
                pass

    def stamps(self, first: int, n: int) -> np.ndarray:
        self._require_open()
        if self._slots is None:
            raise RuntimeError("no frame memory is attached")
        with self._state:
            held = len(self._slots)
            oldest = max(0, self._delivered - held)
            if n < 0 or first < oldest or first + n > self._delivered:
                raise ValueError(
                    f"frames {first} to {first + n - 1} are not all in the slots, "
                    f"which hold {oldest} to {self._delivered - 1}"
                )
            slots = np.arange(first, first + n) % held
            stamps = np.zeros(n, STAMP_DTYPE)
            stamps["hw_frame"] = self._hw_frame[slots]
            stamps["device_ticks"] = self._ticks[slots]
            stamps["host_time_ns"] = self._host[slots]
        return stamps

    async def stop(self) -> None:
        if self._closed:
            return
        errors: list[UEyeError] = []
        await self._stop(errors)
        _raise_first(errors)

    async def detach(self) -> None:
        if self._closed:
            return
        errors: list[UEyeError] = []
        await self._stop(errors)
        self._release(errors)
        _raise_first(errors)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[UEyeError] = []
        await self._stop(errors)
        self._teardown(errors)
        _raise_first(errors)

    # --- settings: the camera device's own, not the port's -----------------------------------

    def pixel_format(self) -> str:
        """The current pixel format, "RAW8" or "RAW10". Raises ``ValueError``
        if the camera is in any other colour mode."""
        self._require_open()
        mode = int(self._u.is_SetColorMode(self._h, self._u.IS_GET_COLOR_MODE))
        for name, (constant, _, _) in PIXEL_FORMATS.items():
            if mode == getattr(self._u, constant):
                return name
        raise ValueError(f"the camera's colour mode is {mode}, which STACScope cannot describe; use RAW8 or RAW10")

    def set_pixel_format(self, name: str) -> str:
        """Set the pixel format to "RAW8" or "RAW10", and return it. Raises
        ``ValueError`` for any other, and ``RuntimeError`` while memory is
        attached."""
        self._require_open()
        _format(name)
        self._refuse_while_attached("the pixel format")
        self._set_color_mode(name)
        return self.pixel_format()

    def aoi(self) -> tuple[int, int, int, int]:
        """The area of interest, as (x, y, width, height) in pixels."""
        self._require_open()
        rect = self._u.IS_RECT()
        self._call("is_AOI", self._h, self._u.IS_AOI_IMAGE_GET_AOI, rect, ctypes.sizeof(rect))
        return rect.s32X.value, rect.s32Y.value, rect.s32Width.value, rect.s32Height.value

    def set_aoi(self, x: int, y: int, width: int, height: int) -> tuple[int, int, int, int]:
        """Set the area of interest and return it as the camera took it. The
        camera refuses one off its grid (on the UI-3240: x and y even, width
        a multiple of 4, height of 2, at least 16 x 4). Raises
        ``RuntimeError`` while memory is attached. The exposure and frame
        rate may change with it."""
        self._require_open()
        self._refuse_while_attached("the AOI")
        rect = self._u.IS_RECT()
        rect.s32X, rect.s32Y, rect.s32Width, rect.s32Height = x, y, width, height
        self._call("is_AOI", self._h, self._u.IS_AOI_IMAGE_SET_AOI, rect, ctypes.sizeof(rect))
        return self.aoi()

    def pixel_clock(self) -> int:
        """The pixel clock, in MHz."""
        self._require_open()
        mhz = self._u.c_uint()
        self._call("is_PixelClock", self._h, self._u.IS_PIXELCLOCK_CMD_GET, mhz, ctypes.sizeof(mhz))
        return mhz.value

    def pixel_clocks(self) -> list[int]:
        """Every pixel clock the camera offers, in MHz, lowest first."""
        self._require_open()
        count = self._u.c_uint()
        self._call("is_PixelClock", self._h, self._u.IS_PIXELCLOCK_CMD_GET_NUMBER, count, ctypes.sizeof(count))
        values = (self._u.c_uint * count.value)()
        self._call("is_PixelClock", self._h, self._u.IS_PIXELCLOCK_CMD_GET_LIST, values, ctypes.sizeof(values))
        return sorted(v.value if hasattr(v, "value") else int(v) for v in values)

    def set_pixel_clock(self, mhz: int) -> int:
        """Set the pixel clock to one of ``pixel_clocks()`` and return it. A
        faster clock reads out faster, so shortens the trigger period; the
        frame rate and exposure may change with it."""
        self._require_open()
        value = self._u.c_uint(mhz)
        self._call("is_PixelClock", self._h, self._u.IS_PIXELCLOCK_CMD_SET, value, ctypes.sizeof(value))
        return self.pixel_clock()

    def frame_rate(self) -> float:
        """The frame rate setting, in frames a second. Under a trigger it is
        not the rate frames come at, but it bounds the exposure: at most
        one frame period."""
        self._require_open()
        fps = self._u.c_double()
        self._call("is_SetFrameRate", self._h, self._u.IS_GET_FRAMERATE, fps)
        return fps.value

    def set_frame_rate(self, fps: float) -> float:
        """Set the frame rate and return what the camera applied, which is
        bounded by the pixel clock and AOI."""
        self._require_open()
        actual = self._u.c_double()
        self._call("is_SetFrameRate", self._h, self._u.c_double(fps), actual)
        return actual.value

    def exposure(self) -> float:
        """The exposure time, in milliseconds."""
        self._require_open()
        ms = self._u.c_double()
        self._call("is_Exposure", self._h, self._u.IS_EXPOSURE_CMD_GET_EXPOSURE, ms, ctypes.sizeof(ms))
        return ms.value

    def set_exposure(self, ms: float) -> float:
        """Set the exposure time, in milliseconds, and return what the camera
        applied: it rounds to its own step, and refuses a time beyond one
        frame period at the current frame rate."""
        self._require_open()
        value = self._u.c_double(ms)
        self._call("is_Exposure", self._h, self._u.IS_EXPOSURE_CMD_SET_EXPOSURE, value, ctypes.sizeof(value))
        return value.value  # the driver writes back what it set

    def trigger(self) -> tuple[str, int]:
        """The trigger, as its edge ("rising" or "falling") and its delay in
        microseconds. Raises ``ValueError`` if the camera is not in a
        hardware trigger mode."""
        self._require_open()
        mode = int(self._u.is_SetExternalTrigger(self._h, self._u.IS_GET_EXTERNALTRIGGER))
        for edge, constant in TRIGGER_EDGES.items():
            if mode == getattr(self._u, constant):
                return edge, self._trigger_delay()
        raise ValueError(f"the camera's trigger mode is {mode:#x}, not a rising or falling edge")

    def set_trigger(self, edge: str, delay_us: int = 0) -> tuple[str, int]:
        """Trigger on the ``edge`` ("rising" or "falling") of the trigger
        input, and begin exposing ``delay_us`` microseconds after it. Returns
        the trigger as the camera took it."""
        self._require_open()
        if edge not in TRIGGER_EDGES:
            raise ValueError(f"a trigger edge is rising or falling, not {edge!r}")
        self._call("is_SetExternalTrigger", self._h, getattr(self._u, TRIGGER_EDGES[edge]))
        self._call("is_SetTriggerDelay", self._h, delay_us)
        return self.trigger()

    def shortest_trigger_period(self) -> float:
        """The shortest time, in seconds, from one trigger to the next that
        the camera will not ignore at its current settings: the trigger
        delay, the exposure and the readout. It changes with the pixel
        clock, AOI, exposure and delay. The camera ignores a trigger that
        comes sooner, and the capture reports it as a lost frame."""
        self._require_open()
        # The sensor does not expose while it reads out under a trigger (Y9),
        # and is ready again once the frame has reached the PC (the manual,
        # "Trigger mode"). The minimum frame time is the readout; the sum was
        # exact on the camera, with and without a delay: at 0.97 of it every
        # other trigger was missed, at 1.0 none (probe, 2026-09-30).
        shortest, longest, step = self._u.c_double(), self._u.c_double(), self._u.c_double()
        self._call("is_GetFrameTimeRange", self._h, shortest, longest, step)
        return self._trigger_delay() * 1e-6 + self.exposure() * 1e-3 + shortest.value

    def fire_trigger(self) -> None:
        """Expose one frame now, as a trigger on the input would. Raises
        ``UEyeError`` if no capture is running."""
        self._require_open()
        self._call("is_ForceTrigger", self._h)

    # --- opening and closing ------------------------------------------------------------------

    def _open(self, camera: int, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while True:
            # A new handle each try: the call writes the handle over the ID.
            h = self._u.HIDS(camera)
            code = int(self._u.is_InitCamera(h, None))
            if code == self._u.IS_SUCCESS:
                self._h = h
                return
            if code != _IN_USE or time.monotonic() >= deadline:
                raise UEyeError("is_InitCamera", code)
            time.sleep(_OPEN_RETRY)

    def _set_color_mode(self, name: str) -> None:
        constant, _, _ = _format(name)
        self._call("is_SetColorMode", self._h, getattr(self._u, constant))

    def _trigger_delay(self) -> int:
        return int(self._u.is_SetTriggerDelay(self._h, self._u.IS_GET_TRIGGER_DELAY))

    def _pitch(self) -> int:
        x, y, bits, pitch = (self._u.c_int() for _ in range(4))
        pointer, mem_id = self._memories[0]
        self._call("is_InquireImageMem", self._h, pointer, mem_id, x, y, bits, pitch)
        return pitch.value

    async def _stop(self, errors: list) -> None:
        """Stop capture, then end the consumer once it has taken what was
        queued before the stop; attempts every step."""
        if self._capturing and self._attempt(errors, "is_StopLiveVideo", self._h, self._u.IS_FORCE_VIDEO_STOP):
            self._capturing = False
        consumer = self._consumer
        if consumer is None:
            return
        self._stopping.set()
        if self._queued:
            # Ends the wait the consumer is in. One that lands between its
            # waits is not seen by the next, which then lasts WAIT_SLICE.
            self._attempt(errors, "is_ImageQueue", self._h, self._u.IS_IMAGE_QUEUE_CMD_CANCEL_WAIT, None, 0)
        # Joined before anything is released or the camera exited: both are
        # undefined under a running wait.
        await asyncio.to_thread(consumer.join)
        self._consumer = None
        self._changed.set()

    def _release(self, errors: list) -> None:
        """Exit the queue, clear the sequence and free every memory; drop our
        references to the slots only once the driver has let go of all."""
        if self._queued and self._attempt(errors, "is_ImageQueue", self._h, self._u.IS_IMAGE_QUEUE_CMD_EXIT, None, 0):
            self._queued = False
        if self._in_sequence and self._attempt(errors, "is_ClearSequence", self._h):
            self._in_sequence = False
        kept = []
        for pointer, mem_id in self._memories:
            if not self._attempt(errors, "is_FreeImageMem", self._h, pointer, mem_id):
                kept.append((pointer, mem_id))
        self._memories = kept
        if not self._memories and not self._in_sequence:
            self._forget()

    def _forget(self) -> None:
        self._memories = []
        self._slots = None
        self._hw_frame = self._ticks = self._host = None

    def _teardown(self, errors: list) -> None:
        """Release, then exit the camera, attempting both whatever fails."""
        if self._h is None:
            return
        if self._slots is not None or self._queued:
            self._release(errors)
        # Exiting frees whatever the driver still holds (the manual), so only
        # then can a failed release's references go.
        if self._attempt(errors, "is_ExitCamera", self._h):
            self._forget()
            self._queued = self._in_sequence = False
        self._h = None

    # --- the consumer: taking frames from the queue ------------------------------------------

    def _consume(self) -> None:
        """The consumer thread: takes each queue entry as it lands, until a
        stop, then takes what was queued before the stop."""
        try:
            while not self._stopping.is_set():
                self._take(*self._wait_once(round(WAIT_SLICE * 1000)))
                self._check_triggers()
                self._notify()
            # Frames stay in the queue across a stop (probe, 2026-09-30), and
            # the role's last pass publishes whatever arrived before it.
            pending = self._u.c_uint()
            self._call(
                "is_ImageQueue", self._h, self._u.IS_IMAGE_QUEUE_CMD_GET_PENDING, pending, ctypes.sizeof(pending)
            )
            for _ in range(pending.value):
                self._take(*self._wait_once(1))
            self._check_triggers()
        except BaseException as exc:
            with self._state:
                if self._failure is None:
                    self._failure = exc
        finally:
            self._notify()

    def _notify(self) -> None:
        """Wake ``wait_frames``, from the consumer thread."""
        try:
            self._loop.call_soon_threadsafe(self._changed.set)
        except RuntimeError:
            pass  # the loop has closed, so nobody is waiting

    def _wait_once(self, timeout_ms: int) -> tuple[int, int | None]:
        """One queue wait. Returns what it returned, and the memory ID of the
        frame, if it gave one."""
        wait = self._u.IMAGEQUEUEWAITBUFFER()
        mem_id = ctypes.c_int()
        memory = ctypes.c_char_p()
        wait.timeout = timeout_ms
        # pyueye's fields are pointers to its own ctypes subclasses.
        wait.ppcMem = ctypes.cast(ctypes.pointer(memory), type(wait.ppcMem))
        wait.pnMemId = ctypes.cast(ctypes.pointer(mem_id), type(wait.pnMemId))
        ret = int(self._u.is_ImageQueue(self._h, self._u.IS_IMAGE_QUEUE_CMD_WAIT, wait, ctypes.sizeof(wait)))
        return ret, (mem_id.value if ret == self._u.IS_SUCCESS else None)

    def _take(self, ret: int, mem_id: int | None) -> None:
        """Account for one queue entry: a frame, a capture status or nothing."""
        u = self._u
        if ret in (u.IS_TIMED_OUT, _ABORTED):
            return
        if ret == u.IS_CAPTURE_STATUS:
            # A frame lost on its way, in its place in the queue: consecutive
            # statuses are one gap.
            with self._state:
                failure = self._failure
                if failure is None:
                    self._failure = FramesLost(self._delivered, 1)
                elif isinstance(failure, FramesLost) and failure.after == self._delivered:
                    self._failure = FramesLost(self._delivered, (failure.lost or 0) + 1)
            return
        if ret != u.IS_SUCCESS:
            raise UEyeError("is_ImageQueue", ret)
        info = u.UEYEIMAGEINFO()
        index = next(i for i, (_, m) in enumerate(self._memories) if m == mem_id)
        try:
            # Read before the unlock: the manual says the info matches the
            # image only while it is locked.
            self._call("is_GetImageInfo", self._h, mem_id, info, ctypes.sizeof(info))
            host = time.time_ns()
        finally:
            self._call("is_UnlockSeqBuf", self._h, mem_id, self._memories[index][0])
        with self._state:
            if self._failure is not None:
                return  # nothing after a loss is counted
            slot = self._delivered % len(self._memories)
            if index != slot:
                # The driver skips a memory still locked, in silence (Y3).
                self._failure = RuntimeError(f"frame {self._delivered} landed in slot {index}, not slot {slot}")
                return
            number = info.u64FrameNumber.value
            if self._last_frame_number is not None and number != self._last_frame_number + 1:
                # Frame numbers step by one within a capture (Y6).
                self._failure = FramesLost(self._delivered, number - self._last_frame_number - 1)
                return
            self._last_frame_number = number
            self._hw_frame[slot] = number
            self._ticks[slot] = info.u64TimestampDevice.value
            self._host[slot] = host
            self._delivered += 1

    def _check_triggers(self) -> None:
        # A trigger that comes while the sensor is busy is ignored, and only
        # this count shows it (probe, 2026-09-30). It was missed during the
        # busy spell of a frame no earlier than the last one taken, so the
        # gap is after at least as many frames as have been counted; it can
        # be later, if the queue was behind.
        missed = self._missed_triggers()
        with self._state:
            if missed and self._failure is None:
                self._failure = FramesLost(self._delivered, missed)

    def _missed_triggers(self) -> int:
        """Triggers the camera ignored since the last read; the read resets it."""
        return int(self._u.is_CameraStatus(self._h, self._u.IS_TRIGGER_MISSED, self._u.IS_GET_STATUS))

    # --- calls ----------------------------------------------------------------------------------

    def _refuse_while_attached(self, what: str) -> None:
        # The uEye does not refuse it itself: the memories keep the size they
        # were given at attach, and the driver would write the new frame
        # into them regardless.
        if self._slots is not None:
            raise RuntimeError(f"{what} cannot change while frame memory is attached")

    def _call(self, name: str, *args) -> int:
        code = int(getattr(self._u, name)(*args))
        if code != self._u.IS_SUCCESS:
            raise UEyeError(name, code)
        return code

    def _attempt(self, errors: list, name: str, *args) -> bool:
        try:
            self._call(name, *args)
        except UEyeError as error:
            errors.append(error)
            return False
        return True

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("the capture has been closed")


_ABORTED = 157  # IS_OPERATION_ABORTED: a wait ended by a cancel


def _format(name: str) -> tuple[str, int, str]:
    try:
        return PIXEL_FORMATS[name]
    except KeyError:
        raise ValueError(f"the pixel format is RAW8 or RAW10, not {name!r}") from None


def _raise_first(errors: list) -> None:
    if not errors:
        return
    first, *later = errors
    for error in later:
        first.add_note(f"then: {error}")
    raise first
