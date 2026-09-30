"""A stand-in for pyueye's ``ueye`` module: the uEye API's functions, structs
and constants, in Python, with one simulated camera behind them.

What this can and cannot prove, stated plainly because it is easy to forget:
it pins *our* call sequence against the uEye API as its manual documents it
and as the 5f survey found the camera (a UI-3240CP-NIR, a USB 3 uEye CP Rev.
1) behaving. It cannot prove pyueye's argument handling or struct layouts,
and it cannot prove what the camera does: only the camera suite can. Where
the manual and the survey are silent, the fake takes the reading that is
harder on the adapter, and says so beside the code.

What the survey found, and the fake copies:

- ``is_InitCamera`` answers ``IS_DEVICE_IN_USE`` for a while after the
  process that had the camera was killed (Y7): ``in_use`` calls here.
- The camera starts from its defaults, 8-bit BGR on a mono sensor (Y8).
- Frames fill the sequence's memories in order and wrap (Y3), and each
  capture begins at the sequence's first memory (probe, 2026-09-30). A
  memory still locked when its turn comes is skipped in silence (Y3). With
  every memory locked, the frame is lost and a capture status
  (``IS_CAPTURE_STATUS``) enters the queue in its place.
- ``u64FrameNumber`` steps by 1 within a capture and runs on across captures
  (Y6); here it starts from an arbitrary number.
- ``u64TimestampDevice`` is in 0.1 us ticks.
- A trigger that arrives while the sensor is busy is ignored, and counted by
  ``is_CameraStatus(IS_TRIGGER_MISSED)``, which resets on each read (probe,
  2026-09-30). Pulses outside a capture are not counted.
- Frames still queued at a stop can be taken after it.
- A wait cancelled by ``IS_IMAGE_QUEUE_CMD_CANCEL_WAIT`` returns
  ``IS_OPERATION_ABORTED``. A cancel with nobody waiting does nothing here;
  the camera was not asked, and this is the harder reading.
- The shortest trigger period is the minimum frame time plus the exposure
  plus the trigger delay (probe, 2026-09-30: at 0.97 of it, every other
  trigger was missed). The fake does not time its pulses; ``miss`` stands in.

Every call is logged in ``calls``, and any use the manual forbids -- a call
on a handle that is not open, reading a frame's info after unlocking it,
exiting the camera while a wait runs -- is recorded in ``misuse``, which
every test expects to be empty.
"""

import ctypes
import threading
import time

import numpy as np

# pyueye's ctypes types are subclasses of ctypes', so a struct's field comes
# back as an object with a ``.value``, not as a Python number. These do the
# same, so an adapter that forgot ``.value`` fails here as it would there.


class c_int(ctypes.c_int):
    pass


class c_uint(ctypes.c_uint):
    pass


class c_double(ctypes.c_double):
    pass


class c_longlong(ctypes.c_longlong):
    pass


class c_char_p(ctypes.c_char_p):
    pass


class c_mem_p(ctypes.c_void_p):
    pass


HIDS = c_uint


class IS_RECT(ctypes.Structure):
    _fields_ = [("s32X", c_int), ("s32Y", c_int), ("s32Width", c_int), ("s32Height", c_int)]


class IMAGEQUEUEWAITBUFFER(ctypes.Structure):
    _fields_ = [("timeout", c_uint), ("ppcMem", ctypes.POINTER(c_char_p)), ("pnMemId", ctypes.POINTER(c_int))]


class UEYEIMAGEINFO(ctypes.Structure):
    # Only the fields the adapter reads, and a size unlike the real one (96):
    # the adapter must pass sizeof of whatever struct it was given.
    _fields_ = [
        ("dwFlags", c_uint),
        ("u64TimestampDevice", c_longlong),
        ("u64FrameNumber", c_longlong),
    ]


# Return codes (ueye.h, through pyueye).
IS_NO_SUCCESS = -1
IS_SUCCESS = 0
IS_INVALID_CAMERA_HANDLE = 1
IS_CANT_OPEN_DEVICE = 3
IS_CAPTURE_STATUS = 3  # an image queue entry; the same number as the above
IS_INVALID_MEMORY_POINTER = 49
IS_TIMED_OUT = 122
IS_INVALID_PARAMETER = 125
IS_OPERATION_ABORTED = 157
IS_DEVICE_IN_USE = 213

IS_GET_STATUS = 0x8000
IS_TRIGGER_MISSED = 20

IS_DONT_WAIT = 0x0000
IS_FORCE_VIDEO_STOP = 0x4000

IS_GET_COLOR_MODE = 0x8000
IS_CM_BGR8_PACKED = 1
IS_CM_MONO8 = 6
IS_CM_SENSOR_RAW8 = 11
IS_CM_SENSOR_RAW12 = 27
IS_CM_SENSOR_RAW10 = 33
COLOR_MODE_BITS = {IS_CM_BGR8_PACKED: 24, IS_CM_MONO8: 8, IS_CM_SENSOR_RAW8: 8, IS_CM_SENSOR_RAW10: 16, IS_CM_SENSOR_RAW12: 16}
"""Bits a pixel takes in image memory, for each colour mode the fake has."""

IS_AOI_IMAGE_SET_AOI = 0x0001
IS_AOI_IMAGE_GET_AOI = 0x0002

IS_PIXELCLOCK_CMD_GET_NUMBER = 1
IS_PIXELCLOCK_CMD_GET_LIST = 2
IS_PIXELCLOCK_CMD_GET = 5
IS_PIXELCLOCK_CMD_SET = 6

IS_GET_FRAMERATE = 0x8000

IS_EXPOSURE_CMD_GET_EXPOSURE = 7
IS_EXPOSURE_CMD_SET_EXPOSURE = 12

IS_GET_EXTERNALTRIGGER = 0x8000
IS_SET_TRIGGER_OFF = 0x0000
IS_SET_TRIGGER_CONTINUOUS = 0x1000
IS_SET_TRIGGER_HI_LO = IS_SET_TRIGGER_CONTINUOUS | 0x0001
IS_SET_TRIGGER_LO_HI = IS_SET_TRIGGER_CONTINUOUS | 0x0002
IS_SET_TRIGGER_SOFTWARE = IS_SET_TRIGGER_CONTINUOUS | 0x0008
HARDWARE_TRIGGERS = {IS_SET_TRIGGER_HI_LO, IS_SET_TRIGGER_LO_HI}

IS_GET_TRIGGER_DELAY = 0x8000

IS_IMAGE_QUEUE_CMD_INIT = 0
IS_IMAGE_QUEUE_CMD_EXIT = 1
IS_IMAGE_QUEUE_CMD_WAIT = 2
IS_IMAGE_QUEUE_CMD_CANCEL_WAIT = 3
IS_IMAGE_QUEUE_CMD_GET_PENDING = 4

SENSOR = (1280, 1024)
"""The UI-3240's sensor, width by height."""

PIXEL_CLOCKS = (7, 10, 20, 24, 31, 40, 86)
"""MHz. A few of the camera's 62."""

ROW_PIXELS = 1400
"""Pixel clocks a row takes to read, blanking included: the fake's model of
readout, so the minimum frame time moves with the clock and the AOI."""

EXPOSURE_STEP_MS = 0.01
"""The fake rounds exposure to whole multiples of this, as the camera
rounds it to its line time."""

TICKS_START = 5_000_000_000
FRAME_TICKS = 500_000
"""The frame numbered ``n`` is stamped ``TICKS_START + n * FRAME_TICKS``, in
0.1 us ticks (a 50 ms period)."""

PACE_LIMIT = 1.0
"""Seconds a paced pulse waits for a memory to be handed back."""

MEM_ID_BASE = 7
MEM_ID_STEP = 3
"""Memory IDs are handed out from 7 in steps of 3, not 1, 2, 3 as the camera
numbers them, so an adapter that assumed the camera's numbering fails."""

HANDLE = 0x4001
"""The handle ``is_InitCamera`` writes back. The camera writes back its ID;
this differs, so an adapter that kept the ID it opened with fails."""


class _Memory:
    def __init__(self, address: int, width: int, height: int, bits: int) -> None:
        self.address = address
        self.width = width
        self.height = height
        self.bits = bits
        self.locked = False
        self.frame_number: int | None = None


class FakeUEye:
    """The module, one camera, and the controls a test drives it with.

    ``camera`` is the camera's ID; opening any other fails. ``in_flight`` is
    how many frames past the newest queued one the driver has begun writing
    (see ``pulse``). ``loss_report`` says how a frame ``lose`` makes is
    seen: by a capture status in the queue, by a gap in the frame numbers,
    or ``"both"``. ``in_use`` is how many opens are refused with
    ``IS_DEVICE_IN_USE`` before one succeeds.
    """

    c_int = c_int
    c_uint = c_uint
    c_double = c_double
    c_mem_p = c_mem_p
    HIDS = HIDS
    IS_RECT = IS_RECT
    IMAGEQUEUEWAITBUFFER = IMAGEQUEUEWAITBUFFER
    UEYEIMAGEINFO = UEYEIMAGEINFO

    def __init__(
        self,
        *,
        camera: int = 1,
        in_flight: int = 0,
        loss_report: str = "both",
        in_use: int = 0,
        frame_number_start: int = 369_000,
    ) -> None:
        self.camera = camera
        self.in_flight = in_flight
        self.loss_report = loss_report
        self.in_use = in_use
        self.calls: list[str] = []
        self.misuse: list[str] = []
        self.linger = 0.0
        """Seconds a wait stays inside the call after it has its answer."""
        self.wait_gate: threading.Event | None = None
        """If set, every queue wait stops at its entrance, before it begins
        to wait, until the gate opens. ``at_gate`` is set when one arrives
        there. A test uses the two to let frames queue up untaken."""
        self.at_gate = threading.Event()
        self.skip = 0
        """Memories the driver skips at the next frame, as it skips one still
        locked (Y3). A test sets it to put a frame out of place."""
        self.paced = True
        """Whether ``pulse`` waits, up to ``PACE_LIMIT``, for the next memory
        to be unlocked before each frame, as triggers a frame time apart
        would give the adapter time to. Off, pulses come all at once, and
        frames beyond the free memories are lost."""
        self.row_padding = 0
        """Bytes the driver says it adds to each row of image memory. The
        camera adds none (Y2); a test sets it to make one that does."""

        self._lock = threading.Condition()
        self._scripted: dict[str, list[tuple[str, int]]] = {}
        self._open = False
        self.color_mode = IS_CM_BGR8_PACKED
        self.aoi_rect = (0, 0, *SENSOR)
        self.pixel_clock = 24
        self.fps = 15.0
        self.exposure_ms = 66.6
        self.trigger_mode = IS_SET_TRIGGER_OFF
        self.trigger_delay_us = 0
        self.missed_triggers = 0
        self._memories: dict[int, _Memory] = {}
        self._next_id = MEM_ID_BASE
        self.sequence: list[int] = []
        self._queue_active = False
        self._queue: list[tuple[str, int | None]] = []
        self._waiting = 0
        self._cancel = 0
        self._capturing = False
        self._position = 0  # index into the sequence of the next frame
        self._begun: list[int] = []  # memory IDs begun but not finished
        self._frame_number = frame_number_start

    def __getattr__(self, name: str):
        # The constants, as pyueye's module has them.
        if name.startswith("IS_") and name in globals():
            return globals()[name]
        raise AttributeError(name)

    # --- controls ------------------------------------------------------------------

    def fail(self, name: str, code: int, *, times: int = 1, after: int = 0) -> None:
        """Let the next ``after`` calls to ``name`` work, then make ``times``
        calls do nothing and return ``code``."""
        script = self._scripted.setdefault(name, [])
        script.extend([("pass", 0)] * after + [("fail", code)] * times)

    def pulse(self, n: int = 1) -> None:
        """``n`` edges on the trigger input, of the edge the trigger mode
        takes. In a capture each begins a frame in the next memory; a frame
        is finished, locked and queued once ``in_flight`` more have begun,
        or at ``settle``. Outside a capture, or in any other mode, nothing."""
        with self._lock:
            for _ in range(n):
                if not self._capturing or self.trigger_mode not in HARDWARE_TRIGGERS:
                    continue
                if self.paced:
                    # Real triggers are a frame time apart, and in that time
                    # whoever takes frames has handed the last one back.
                    deadline = time.monotonic() + PACE_LIMIT
                    while self._next_is_locked() and time.monotonic() < deadline:
                        self._lock.wait(deadline - time.monotonic())
                self._begin()
                self._lock.notify_all()

    def _next_is_locked(self) -> bool:
        if not self.sequence:
            return False
        mem_id = self.sequence[self._position % len(self.sequence)]
        return self._memories[mem_id].locked or mem_id in self._begun

    def settle(self) -> None:
        """Let the driver finish every frame it has begun, as it does once
        the triggers stop."""
        with self._lock:
            while self._begun:
                self._finish(self._begun.pop(0))
            self._lock.notify_all()

    def lose(self, n: int = 1) -> None:
        """The camera exposes ``n`` frames that never reach a memory. With
        ``loss_report`` "status", they leave no gap in the frame numbers:
        the camera may number frames as they are sent, not as exposed."""
        with self._lock:
            if self.loss_report in ("gap", "both"):
                self._frame_number += n  # the sensor counted them
            if self.loss_report in ("status", "both"):
                self._queue.extend([("status", None)] * n)
            self._lock.notify_all()

    def miss(self, n: int = 1) -> None:
        """``n`` triggers arrive while the sensor is busy, and are ignored."""
        with self._lock:
            if self._capturing:
                self.missed_triggers += n

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def capturing(self) -> bool:
        return self._capturing

    @property
    def memories(self) -> dict[int, int]:
        """Every memory the driver holds, as ID to address."""
        return {i: m.address for i, m in self._memories.items()}

    @property
    def queue_active(self) -> bool:
        return self._queue_active

    def locked(self) -> list[int]:
        return [i for i, m in self._memories.items() if m.locked]

    def waiting(self) -> int:
        with self._lock:
            return self._waiting

    def min_frame_time(self) -> float:
        """Seconds: the readout at the current pixel clock and AOI."""
        return self.aoi_rect[3] * ROW_PIXELS / (self.pixel_clock * 1e6)

    # --- the driver ----------------------------------------------------------------------

    def _begin(self) -> None:
        n = len(self.sequence)
        # The memories free to take, in the order the driver tries them.
        free = [
            (self._position + k) % n
            for k in range(n)
            if not self._memories[self.sequence[(self._position + k) % n]].locked
            and self.sequence[(self._position + k) % n] not in self._begun
        ]
        if not free:
            self._frame_number += 1
            self._queue.append(("status", None))  # IS_CAP_STATUS_API_NO_DEST_MEM
            return
        position = free[min(self.skip, len(free) - 1)]
        self.skip = 0
        self._position = (position + 1) % n
        mem_id = self.sequence[position]
        memory = self._memories[mem_id]
        mode_bits = COLOR_MODE_BITS[self.color_mode]
        _, _, width, height = self.aoi_rect
        if (memory.width, memory.height, memory.bits) != (width, height, mode_bits):
            self.misuse.append(
                f"capture into memory {mem_id} of {memory.width}x{memory.height}x{memory.bits}, "
                f"with the camera at {width}x{height}x{mode_bits}"
            )
        row = width * mode_bits // 8
        value = self._frame_number % 200 + 1
        ctypes.memmove(memory.address, bytes([value]) * row, row)  # the first row first
        memory.frame_number = self._frame_number
        self._frame_number += 1
        self._begun.append(mem_id)
        while len(self._begun) > self.in_flight:
            self._finish(self._begun.pop(0))

    def _finish(self, mem_id: int) -> None:
        memory = self._memories[mem_id]
        size = memory.width * memory.height * memory.bits // 8
        image = np.full(size, memory.frame_number % 200 + 1, np.uint8)  # never the contract's sentinel
        ctypes.memmove(memory.address, image.ctypes.data, size)
        memory.locked = True
        self._queue.append(("frame", mem_id))

    def _call(self, name: str, h=None) -> int | None:
        """Logs the call and returns a code to return in its place, if any."""
        self.calls.append(name)
        scripted = self._scripted.get(name)
        if scripted:
            step = scripted.pop(0)
            if step[0] == "fail":
                return step[1]
        if h is not None and (not self._open or _value(h) != HANDLE):
            self.misuse.append(f"{name} on a handle that is not open: {_value(h)!r}")
            return IS_INVALID_CAMERA_HANDLE
        return None

    # --- opening -------------------------------------------------------------------------

    def is_InitCamera(self, phCam, hWnd):
        with self._lock:
            if (code := self._call("is_InitCamera")) is not None:
                return code
            if self._open:
                self.misuse.append("is_InitCamera on a camera already open")
            if self.in_use:
                self.in_use -= 1
                return IS_DEVICE_IN_USE
            if phCam.value not in (0, self.camera):
                return IS_CANT_OPEN_DEVICE
            phCam.value = HANDLE
            self._open = True
            # Y8: every setting back to the camera's defaults.
            self.color_mode = IS_CM_BGR8_PACKED
            self.aoi_rect = (0, 0, *SENSOR)
            self.pixel_clock = 24
            self.fps = 15.0
            self.exposure_ms = 66.6
            self.trigger_mode = IS_SET_TRIGGER_OFF
            self.trigger_delay_us = 0
            return IS_SUCCESS

    def is_ExitCamera(self, hCam):
        with self._lock:
            if (code := self._call("is_ExitCamera", hCam)) is not None:
                return code
            if self._waiting:
                self.misuse.append("is_ExitCamera while a wait is running")
            # Exiting frees everything the driver holds (the manual).
            self._capturing = False
            self._queue_active = False
            self._queue.clear()
            self.sequence.clear()
            self._memories.clear()
            self._open = False
            self._lock.notify_all()
            return IS_SUCCESS

    # --- settings ------------------------------------------------------------------------

    def is_SetColorMode(self, hCam, Mode):
        with self._lock:
            if (code := self._call("is_SetColorMode", hCam)) is not None:
                return code
            if _value(Mode) == IS_GET_COLOR_MODE:
                return self.color_mode
            if _value(Mode) not in COLOR_MODE_BITS:
                return IS_INVALID_PARAMETER
            self.color_mode = _value(Mode)
            return IS_SUCCESS

    def is_AOI(self, hCam, nCommand, pParam, cbSizeOfParam):
        with self._lock:
            if (code := self._call("is_AOI", hCam)) is not None:
                return code
            if _value(nCommand) == IS_AOI_IMAGE_GET_AOI:
                pParam.s32X, pParam.s32Y, pParam.s32Width, pParam.s32Height = self.aoi_rect
                return IS_SUCCESS
            if _value(nCommand) != IS_AOI_IMAGE_SET_AOI:
                return IS_INVALID_PARAMETER
            x, y, w, h = (pParam.s32X.value, pParam.s32Y.value, pParam.s32Width.value, pParam.s32Height.value)
            # The survey's steps: width 4, height 2, position 2; at least 16 x 4.
            if (
                w % 4 or h % 2 or x % 2 or y % 2 or w < 16 or h < 4
                or x < 0 or y < 0 or x + w > SENSOR[0] or y + h > SENSOR[1]
            ):
                return IS_INVALID_PARAMETER
            self.aoi_rect = (x, y, w, h)
            self._bound_exposure()
            return IS_SUCCESS

    def is_PixelClock(self, hCam, nCommand, pParam, cbSizeOfParam):
        with self._lock:
            if (code := self._call("is_PixelClock", hCam)) is not None:
                return code
            command = _value(nCommand)
            if command == IS_PIXELCLOCK_CMD_GET:
                pParam.value = self.pixel_clock
            elif command == IS_PIXELCLOCK_CMD_GET_NUMBER:
                pParam.value = len(PIXEL_CLOCKS)
            elif command == IS_PIXELCLOCK_CMD_GET_LIST:
                if cbSizeOfParam < len(PIXEL_CLOCKS) * 4:
                    return IS_INVALID_PARAMETER
                # Not in order: the manual does not promise any.
                for i, mhz in enumerate(reversed(PIXEL_CLOCKS)):
                    pParam[i] = mhz
            elif command == IS_PIXELCLOCK_CMD_SET:
                if pParam.value not in PIXEL_CLOCKS:
                    return IS_INVALID_PARAMETER
                self.pixel_clock = pParam.value
                self.fps = min(self.fps, 1 / self.min_frame_time())
                self._bound_exposure()
            else:
                return IS_INVALID_PARAMETER
            return IS_SUCCESS

    def is_GetFrameTimeRange(self, hCam, min, max, intervall):
        with self._lock:
            if (code := self._call("is_GetFrameTimeRange", hCam)) is not None:
                return code
            min.value = self.min_frame_time()
            max.value = 2.0
            intervall.value = 1e-5
            return IS_SUCCESS

    def is_SetFrameRate(self, hCam, FPS, newFPS):
        with self._lock:
            if (code := self._call("is_SetFrameRate", hCam)) is not None:
                return code
            fps = _value(FPS)
            if fps == IS_GET_FRAMERATE:
                newFPS.value = self.fps
                return IS_SUCCESS
            # Clamped to what the pixel clock and AOI allow, as the camera does.
            self.fps = float(np.clip(fps, 0.5, 1 / self.min_frame_time()))
            self._bound_exposure()
            newFPS.value = self.fps
            return IS_SUCCESS

    def is_Exposure(self, hCam, nCommand, pParam, cbSizeOfParam):
        with self._lock:
            if (code := self._call("is_Exposure", hCam)) is not None:
                return code
            command = _value(nCommand)
            if command == IS_EXPOSURE_CMD_GET_EXPOSURE:
                pParam.value = self.exposure_ms
            elif command == IS_EXPOSURE_CMD_SET_EXPOSURE:
                ms = round(pParam.value / EXPOSURE_STEP_MS) * EXPOSURE_STEP_MS
                if not 0.009 <= pParam.value <= 1000 / self.fps:
                    return IS_INVALID_PARAMETER
                self.exposure_ms = max(ms, 0.01)
                pParam.value = self.exposure_ms  # written back, as the camera does
            else:
                return IS_INVALID_PARAMETER
            return IS_SUCCESS

    def _bound_exposure(self) -> None:
        self.exposure_ms = min(self.exposure_ms, 1000 / self.fps)

    def is_SetExternalTrigger(self, hCam, nTriggerMode):
        with self._lock:
            if (code := self._call("is_SetExternalTrigger", hCam)) is not None:
                return code
            mode = _value(nTriggerMode)
            if mode == IS_GET_EXTERNALTRIGGER:
                return self.trigger_mode
            if mode not in (IS_SET_TRIGGER_OFF, IS_SET_TRIGGER_SOFTWARE, *HARDWARE_TRIGGERS):
                return IS_INVALID_PARAMETER
            self.trigger_mode = mode
            return IS_SUCCESS

    def is_SetTriggerDelay(self, hCam, nTriggerDelay):
        with self._lock:
            if (code := self._call("is_SetTriggerDelay", hCam)) is not None:
                return code
            delay = _value(nTriggerDelay)
            if delay == IS_GET_TRIGGER_DELAY:
                return self.trigger_delay_us
            if not 0 <= delay <= 4_000_000:
                return IS_INVALID_PARAMETER
            self.trigger_delay_us = delay
            return IS_SUCCESS

    def is_ForceTrigger(self, hCam):
        with self._lock:
            if (code := self._call("is_ForceTrigger", hCam)) is not None:
                return code
            # A frame now, in a capture in a hardware trigger mode (probe).
            # Outside one the camera was not asked; refused here.
            if not self._capturing or self.trigger_mode not in HARDWARE_TRIGGERS:
                return IS_NO_SUCCESS
            self._begin()
            self._lock.notify_all()
            return IS_SUCCESS

    def is_CameraStatus(self, hCam, nInfo, ulValue):
        with self._lock:
            if (code := self._call("is_CameraStatus", hCam)) is not None:
                return code
            if _value(nInfo) != IS_TRIGGER_MISSED or _value(ulValue) != IS_GET_STATUS:
                return IS_NO_SUCCESS
            missed, self.missed_triggers = self.missed_triggers, 0
            return missed

    # --- image memory --------------------------------------------------------------------

    def is_SetAllocatedImageMem(self, hCam, width, height, bitspixel, pcImgMem, pid):
        with self._lock:
            if (code := self._call("is_SetAllocatedImageMem", hCam)) is not None:
                return code
            address = _value(pcImgMem)
            if not address:
                return IS_INVALID_MEMORY_POINTER
            if any(m.address == address for m in self._memories.values()):
                self.misuse.append(f"memory at {address:#x} given to the driver twice")
            mem_id = self._next_id
            self._next_id += MEM_ID_STEP
            self._memories[mem_id] = _Memory(address, _value(width), _value(height), _value(bitspixel))
            pid.value = mem_id
            return IS_SUCCESS

    def _memory(self, name: str, pointer, mem_id) -> _Memory | None:
        memory = self._memories.get(_value(mem_id))
        if memory is None or memory.address != _value(pointer):
            self.misuse.append(f"{name} with memory {_value(mem_id)} at {_value(pointer)!r}, which the driver does not hold")
            return None
        return memory

    def is_AddToSequence(self, hCam, pcMem, nID):
        with self._lock:
            if (code := self._call("is_AddToSequence", hCam)) is not None:
                return code
            if self._memory("is_AddToSequence", pcMem, nID) is None:
                return IS_INVALID_MEMORY_POINTER
            self.sequence.append(_value(nID))
            return IS_SUCCESS

    def is_ClearSequence(self, hCam):
        with self._lock:
            if (code := self._call("is_ClearSequence", hCam)) is not None:
                return code
            if self._capturing:
                self.misuse.append("is_ClearSequence during a capture")
            self.sequence.clear()
            return IS_SUCCESS

    def is_FreeImageMem(self, hCam, pcMem, nID):
        with self._lock:
            if (code := self._call("is_FreeImageMem", hCam)) is not None:
                return code
            if self._memory("is_FreeImageMem", pcMem, nID) is None:
                return IS_INVALID_MEMORY_POINTER
            if _value(nID) in self.sequence:
                self.misuse.append(f"is_FreeImageMem of memory {_value(nID)} still in the sequence")
            del self._memories[_value(nID)]
            return IS_SUCCESS

    def is_InquireImageMem(self, hCam, pcMem, nID, pnX, pnY, pnBits, pnPitch):
        with self._lock:
            if (code := self._call("is_InquireImageMem", hCam)) is not None:
                return code
            memory = self._memory("is_InquireImageMem", pcMem, nID)
            if memory is None:
                return IS_INVALID_MEMORY_POINTER
            pnX.value, pnY.value, pnBits.value = memory.width, memory.height, memory.bits
            pnPitch.value = memory.width * ((memory.bits + 7) // 8) + self.row_padding
            return IS_SUCCESS

    def is_UnlockSeqBuf(self, hCam, nNum, pcMem):
        with self._lock:
            if (code := self._call("is_UnlockSeqBuf", hCam)) is not None:
                return code
            memory = self._memory("is_UnlockSeqBuf", pcMem, nNum)
            if memory is None:
                return IS_INVALID_MEMORY_POINTER
            if not memory.locked:
                self.misuse.append(f"is_UnlockSeqBuf of memory {_value(nNum)}, which is not locked")
            memory.locked = False
            self._lock.notify_all()
            return IS_SUCCESS

    def is_GetImageInfo(self, hCam, nImageBufferID, pImageInfo, imageInfoSize):
        with self._lock:
            if (code := self._call("is_GetImageInfo", hCam)) is not None:
                return code
            memory = self._memories.get(_value(nImageBufferID))
            if memory is None:
                return IS_INVALID_MEMORY_POINTER
            if imageInfoSize != ctypes.sizeof(UEYEIMAGEINFO):
                return IS_INVALID_PARAMETER
            if not memory.locked:
                # The manual: the info matches the image only while it is locked.
                self.misuse.append(f"is_GetImageInfo of memory {_value(nImageBufferID)} after it was unlocked")
            pImageInfo.u64FrameNumber = memory.frame_number
            pImageInfo.u64TimestampDevice = TICKS_START + memory.frame_number * FRAME_TICKS
            return IS_SUCCESS

    # --- capture and the queue -----------------------------------------------------------

    def is_CaptureVideo(self, hCam, Wait):
        with self._lock:
            if (code := self._call("is_CaptureVideo", hCam)) is not None:
                return code
            if not self.sequence:
                return IS_NO_SUCCESS
            if not self._queue_active:
                self.misuse.append("is_CaptureVideo with no image queue")
            self._capturing = True
            self._position = 0  # each capture begins at the first memory (probe)
            return IS_SUCCESS

    def is_StopLiveVideo(self, hCam, Wait):
        with self._lock:
            if (code := self._call("is_StopLiveVideo", hCam)) is not None:
                return code
            self._capturing = False
            self._begun.clear()  # a frame half-written at the stop never arrives
            return IS_SUCCESS

    def is_ImageQueue(self, hCam, nCommand, pParam, cbSizeOfParams):
        command = _value(nCommand)
        with self._lock:
            if (code := self._call("is_ImageQueue", hCam)) is not None:
                return code
            if command == IS_IMAGE_QUEUE_CMD_INIT:
                if self._queue_active:
                    self.misuse.append("image queue initialised twice")
                if not self.sequence:
                    return IS_NO_SUCCESS
                self._queue_active = True
                self._queue.clear()
                return IS_SUCCESS
            if not self._queue_active:
                self.misuse.append(f"is_ImageQueue command {command} with no image queue")
                return IS_NO_SUCCESS
            if command == IS_IMAGE_QUEUE_CMD_EXIT:
                if self._waiting:
                    self.misuse.append("image queue exited while a wait is running")
                self._queue_active = False
                self._queue.clear()
                return IS_SUCCESS
            if command == IS_IMAGE_QUEUE_CMD_GET_PENDING:
                pParam.value = len(self._queue)
                return IS_SUCCESS
            if command == IS_IMAGE_QUEUE_CMD_CANCEL_WAIT:
                if self._waiting:
                    self._cancel += 1
                    self._lock.notify_all()
                return IS_SUCCESS
            if command != IS_IMAGE_QUEUE_CMD_WAIT:
                return IS_INVALID_PARAMETER
            if cbSizeOfParams != ctypes.sizeof(IMAGEQUEUEWAITBUFFER):
                return IS_INVALID_PARAMETER
            gate = self.wait_gate
            if gate is not None and not gate.is_set():
                self.at_gate.set()
                self._lock.release()
                try:
                    gate.wait()
                finally:
                    self._lock.acquire()
            self._waiting += 1
            code = self._wait(pParam)
        time.sleep(self.linger)
        with self._lock:
            self._waiting -= 1
        return code

    def _wait(self, p) -> int:
        deadline = time.monotonic() + p.timeout.value / 1000
        while True:
            if self._cancel:
                self._cancel -= 1
                return IS_OPERATION_ABORTED
            if self._queue:
                kind, mem_id = self._queue.pop(0)
                if kind == "status":
                    return IS_CAPTURE_STATUS
                p.pnMemId.contents.value = mem_id
                return IS_SUCCESS
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return IS_TIMED_OUT
            self._lock.wait(remaining)


def _value(x):
    """A ctypes object's value, or the plain number itself."""
    return x.value if hasattr(x, "value") else x
