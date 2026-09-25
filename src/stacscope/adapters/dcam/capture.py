"""A Hamamatsu camera as a frame grabber, through DCAM-API.

DcamFrameCapture hands the camera's driver the slots of an image buffer, and
the driver writes each frame straight into the next slot, with no copy. It
also collects each frame's frame number and time stamp from the driver. It is
the FrameCapture port under the image-streaming role, which publishes what
arrives.

Windows only, and DCAM-API must be installed. DCAM-API allows one session per
process, so open at most one DcamFrameCapture in a process: give the camera a
process of its own, which is where a vendor driver belongs anyway.
"""
# For end use and not for commercial purposes.

import asyncio
import ctypes
import time
from ctypes import POINTER, c_double, c_int32, c_void_p

import numpy as np

from stacscope.adapters.dcam import _api
from stacscope.adapters.dcam._api import DcamUnavailable
from stacscope.core.buffer import MetaField, Metadata, TimeSource
from stacscope.core.ports import STAMP_DTYPE, FrameFormat, FramesLost

_FORMAT_PROPERTIES = (
    "IMAGE WIDTH",
    "IMAGE HEIGHT",
    "BUFFER ROWBYTES",
    "BUFFER FRAMEBYTES",
    "BUFFER TOP OFFSET BYTES",
    "BUFFER PIXEL TYPE",
)
# Names as the camera reports them (the dev box survey), which is not always
# as dcamprop.h's comments spell them: "BUFFER FRAMEBYTES", not "BUFFER FRAME
# BYTES". The BUFFER ones describe attached memory, which the grabber may
# pad; the IMAGE ones describe the sensor's output.

_DTYPES = {"MONO8": "u1", "MONO16": "<u2"}
# Only what numpy can describe exactly. MONO12P is packed; MONO12's placement
# within its 16 bits is not documented.

_TIMESTAMP = np.dtype([("sec", "<u4"), ("microsec", "<i4")])  # DCAM_TIMESTAMP

WAIT_SLICE = 0.1
"""The longest single DCAM wait, in seconds. A stop that lands just before a
wait begins is not seen by that wait, so this bounds how long it can go
unnoticed."""


class DcamError(RuntimeError):
    """A DCAM-API call failed. ``call`` is the function, ``code`` the DCAMERR
    as a signed 32-bit int, ``name`` its name where this adapter knows it
    (else ``None``), and ``text`` DCAM's own description, if it gave one."""

    def __init__(self, call: str, code: int, text: str = "") -> None:
        self.call = call
        self.code = code
        self.name = _api.ERROR_NAMES.get(code)
        self.text = text
        number = f"{code & 0xFFFFFFFF:#010x}"
        which = f"{self.name} ({number})" if self.name else number
        super().__init__(f"{call} failed: {which}" + (f": {text}" if text else ""))


class DcamFrameCapture:
    """Implements ``FrameCapture`` on DCAM camera number ``camera``.

    ``in_flight`` is how many frames past the newest one reported the grabber
    may already be writing; it is measured on each camera, not known in
    advance, so there is no default. ``slot_alignment`` is the boundary each
    slot starts on. ``library`` replaces ``dcamapi.dll``, for tests.

    Opening initializes DCAM-API, opens the camera and checks it describes
    its frames. Raises ``DcamUnavailable`` if DCAM-API or a property the
    format needs is missing, ``ValueError`` if there is no camera ``camera``,
    and ``DcamError`` if DCAM refuses; anything opened is closed again first.
    """

    def __init__(
        self,
        camera: int = 0,
        *,
        in_flight: int,
        slot_alignment: int = 4096,
        library=None,
    ) -> None:
        self._lib = library if library is not None else _api.load()
        self._in_flight = in_flight
        self._slot_alignment = slot_alignment
        self._initialized = False
        self._hdcam = None
        self._hwait = None
        self._closed = False
        self._slots: np.ndarray | None = None
        self._timestamps: np.ndarray | None = None
        self._framestamps: np.ndarray | None = None
        self._pointers: dict[int, ctypes.Array] = {}
        """The address arrays DCAM was given, by attach kind, kept alive while
        attached; a kind is here until it has been released."""
        self._capturing = False
        self._started = False
        self._delivered = 0
        self._checked: tuple[int, int] | None = None
        """The last frame whose frame stamp was checked, and that stamp."""
        self._lost: FramesLost | None = None
        self._dcam_reported_loss = False
        self._waiter: asyncio.Future | None = None
        self._has_timestamps = False
        self._has_framestamps = False
        self._ids: dict[str, int] = {}
        try:
            self._open(camera)
        except BaseException as exc:
            errors: list[DcamError] = []
            self._teardown(errors)
            for error in errors:
                exc.add_note(f"closing again also failed: {error}")
            raise

    # --- FrameCapture ----------------------------------------------------------

    def frame_format(self) -> FrameFormat:
        self._require_open()
        width = int(self._value("IMAGE WIDTH"))
        height = int(self._value("IMAGE HEIGHT"))
        pixel_type = self._value_text("BUFFER PIXEL TYPE")
        dtype = _DTYPES.get(pixel_type)
        if dtype is None:
            raise ValueError(
                f"BUFFER PIXEL TYPE is {pixel_type}, which STACScope cannot describe; "
                f"use {' or '.join(_DTYPES)}"
            )
        return FrameFormat(
            shape=(height, width),
            dtype=dtype,
            strides=(int(self._value("BUFFER ROWBYTES")), np.dtype(dtype).itemsize),
            frame_bytes=int(self._value("BUFFER FRAMEBYTES")),
            frame_offset=int(self._value("BUFFER TOP OFFSET BYTES")),
            slot_alignment=self._slot_alignment,
            in_flight=self._in_flight,
        )

    def metadata(self) -> Metadata:
        self._require_open()
        fields = MetaField(0)
        if self._has_framestamps:
            fields |= MetaField.HW_FRAME
        if not self._has_timestamps:
            return Metadata(fields=fields)
        # The Flash's stamps come from the DCAM module, a host clock (dev box
        # survey), and are accepted as such (user, 2026-09-24).
        return Metadata(
            fields=fields | MetaField.DEVICE_TIME,
            device_time_source=TimeSource.DRIVER,
            device_clock_hz=1e6,
        )

    async def attach(self, slots: np.ndarray) -> None:
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
        # DCAM checks none of these addresses, and writes wherever they point
        # ("the system may hang up"), which is why the checks above come first.
        n = len(slots)
        self._slots = slots
        memory = [(_api.ATTACHKIND_FRAME, slots.ctypes.data, slots.strides[0])]
        if self._has_timestamps:
            self._timestamps = np.zeros(n, _TIMESTAMP)
            memory.append((_api.ATTACHKIND_TIMESTAMP, self._timestamps.ctypes.data, _TIMESTAMP.itemsize))
        if self._has_framestamps:
            self._framestamps = np.zeros(n, "<i4")
            memory.append((_api.ATTACHKIND_FRAMESTAMP, self._framestamps.ctypes.data, 4))
        try:
            for kind, base, step in memory:
                pointers = (c_void_p * n)(*(base + i * step for i in range(n)))
                attach = _api.DCAMBUF_ATTACH.new(
                    iKind=kind, buffer=ctypes.cast(pointers, POINTER(c_void_p)), buffercount=n
                )
                self._pointers[kind] = pointers
                try:
                    self._call("dcambuf_attach", self._hdcam, ctypes.pointer(attach))
                except DcamError:
                    del self._pointers[kind]  # refused, so nothing to release
                    raise
        except BaseException as exc:
            # Half-attached memory is memory DCAM writes into and the role
            # believes free: release whatever went in.
            errors: list[DcamError] = []
            self._release(errors)
            for kind, _, _ in memory:
                if kind not in self._pointers:  # never attached, or released
                    self._forget(kind)
            for error in errors:
                exc.add_note(f"releasing it again also failed: {error}")
            raise

    async def start(self) -> None:
        self._require_open()
        if self._slots is None:
            raise RuntimeError("attach frame memory before starting")
        if self._capturing:
            raise RuntimeError("already capturing")
        self._delivered = 0
        self._checked = None
        self._lost = None
        self._dcam_reported_loss = False
        self._call("dcamcap_start", self._hdcam, _api.CAPSTART_SEQUENCE)
        self._capturing = True
        self._started = True

    async def wait_frames(self, seen: int, timeout: float) -> int:
        self._require_open()
        deadline = time.monotonic() + timeout
        while True:
            self._refresh()
            if self._lost is not None:
                raise self._lost
            if self._delivered > seen or not self._capturing:
                return self._delivered
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return self._delivered
            code = await self._wait(min(remaining, WAIT_SLICE))
            if code in (_api.ERR_LOSTFRAME, _api.ERR_MISSINGFRAME_TROUBLE):
                self._dcam_reported_loss = True
            elif code < 0 and code not in (_api.ERR_TIMEOUT, _api.ERR_ABORT):
                raise DcamError("dcamwait_start", code, self._error_text(code))

    def stamps(self, first: int, n: int) -> np.ndarray:
        self._require_open()
        if self._slots is None:
            raise RuntimeError("no frame memory is attached")
        self._refresh()
        held = len(self._slots)
        oldest = max(0, self._delivered - held)
        if n < 0 or first < oldest or first + n > self._delivered:
            raise ValueError(
                f"frames {first} to {first + n - 1} are not all in the slots, "
                f"which hold {oldest} to {self._delivered - 1}"
            )
        slots = np.arange(first, first + n) % held
        stamps = np.zeros(n, STAMP_DTYPE)
        if self._framestamps is not None:
            stamps["hw_frame"] = self._framestamps[slots].view("<u4")
        if self._timestamps is not None:
            times = self._timestamps[slots]
            stamps["device_ticks"] = times["sec"].astype(np.int64) * 1_000_000 + times["microsec"]
        return stamps

    async def stop(self) -> None:
        if self._closed:
            return
        errors: list[DcamError] = []
        self._stop(errors)
        _raise_first(errors)

    async def detach(self) -> None:
        if self._closed:
            return
        errors: list[DcamError] = []
        self._stop(errors)
        await self._join_waiter()
        self._release(errors)
        _raise_first(errors)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        errors: list[DcamError] = []
        self._stop(errors)
        # The wait handle is closed below, and closing it under a running wait
        # is undefined: the aborted wait comes out first.
        await self._join_waiter()
        self._teardown(errors)
        _raise_first(errors)

    # --- opening and closing --------------------------------------------------------

    def _open(self, camera: int) -> None:
        options = (c_int32 * 2)(_api.INITOPTION_APIVER_LATEST, _api.INITOPTION_ENDMARK)
        init = _api.DCAMAPI_INIT.new(
            initoptionbytes=ctypes.sizeof(options),
            initoption=ctypes.cast(options, POINTER(c_int32)),
        )
        # Uninitialized on the way out even if init fails: whether a failed
        # init (DCAMERR_NOCAMERA, say) leaves anything initialized is not
        # documented, and uninit is harmless if not.
        self._initialized = True
        self._call("dcamapi_init", ctypes.pointer(init))
        if not 0 <= camera < init.iDeviceCount:
            raise ValueError(f"DCAM found {init.iDeviceCount} camera(s); there is no camera {camera}")
        device = _api.DCAMDEV_OPEN.new(index=camera)
        self._call("dcamdev_open", ctypes.pointer(device))
        self._hdcam = device.hdcam
        wait = _api.DCAMWAIT_OPEN.new(hdcam=self._hdcam)
        self._call("dcamwait_open", ctypes.pointer(wait))
        self._hwait = wait.hwait
        capability = _api.DCAMDEV_CAPABILITY.new(domain=_api.CAPDOMAIN_FUNCTION)
        self._call("dcamdev_getcapability", self._hdcam, ctypes.pointer(capability))
        self._has_timestamps = bool(capability.capflag & _api.CAPFLAG_TIMESTAMP)
        self._has_framestamps = bool(capability.capflag & _api.CAPFLAG_FRAMESTAMP)
        self._ids = self._property_ids()
        missing = [name for name in _FORMAT_PROPERTIES if name not in self._ids]
        if missing:
            raise DcamUnavailable(f"the camera has no {', '.join(missing)} property")

    def _property_ids(self) -> dict[str, int]:
        # By name at run time, not from dcamprop.h (the licence policy).
        ids: dict[str, int] = {}
        current = c_int32(0)
        name = ctypes.create_string_buffer(64)
        while True:
            code = self._lib.dcamprop_getnextid(
                self._hdcam, ctypes.pointer(current), _api.PROPOPTION_SUPPORT
            )
            if code == _api.ERR_NOPROPERTY:  # the end of the list
                return ids
            self._check("dcamprop_getnextid", code)
            self._call("dcamprop_getname", self._hdcam, current.value, name, len(name))
            ids[name.value.decode()] = current.value

    def _stop(self, errors: list) -> None:
        """Stop capture and abort any wait, attempting both."""
        if self._capturing and self._attempt(errors, "dcamcap_stop", self._hdcam):
            self._capturing = False
        if self._hwait is not None:
            self._attempt(errors, "dcamwait_abort", self._hwait)

    def _release(self, errors: list) -> None:
        """Give back every kind of attached memory, stamps first; drop our
        references to each only once DCAM has let go of it."""
        for kind in sorted(self._pointers, reverse=True):
            if self._attempt(errors, "dcambuf_release", self._hdcam, kind):
                self._forget(kind)

    def _forget(self, kind: int | None = None) -> None:
        """Drop the references for ``kind`` of memory, or for every kind."""
        for each in [kind] if kind is not None else list(self._pointers):
            self._pointers.pop(each, None)
            if each == _api.ATTACHKIND_FRAME:
                self._slots = None
            elif each == _api.ATTACHKIND_TIMESTAMP:
                self._timestamps = None
            elif each == _api.ATTACHKIND_FRAMESTAMP:
                self._framestamps = None

    def _teardown(self, errors: list) -> None:
        """Release, close the wait handle and the camera, and uninitialize,
        attempting every step whatever fails before it."""
        if self._pointers:
            self._release(errors)
        if self._hwait is not None:
            self._attempt(errors, "dcamwait_close", self._hwait)
            self._hwait = None
        # Closing the camera, or uninitializing, releases its memory by force
        # (documented), so only then can a failed release's references go.
        if self._hdcam is not None:
            if self._attempt(errors, "dcamdev_close", self._hdcam):
                self._forget()
            self._hdcam = None
        if self._initialized:
            if self._attempt(errors, "dcamapi_uninit"):
                self._forget()
            self._initialized = False

    # --- waiting and counting ---------------------------------------------------------

    async def _wait(self, seconds: float) -> int:
        """One DCAM wait for a frame or the stop, in a thread. Returns its code."""
        await self._join_waiter()
        start = _api.DCAMWAIT_START.new(
            eventmask=_api.CAPEVENT_FRAMEREADY | _api.CAPEVENT_STOPPED,
            timeout=max(1, round(seconds * 1000)),
        )
        # Shielded and kept: if the caller is cancelled, the thread is still in
        # DCAM, and close must wait for it before closing the wait handle.
        self._waiter = asyncio.ensure_future(
            asyncio.to_thread(self._lib.dcamwait_start, self._hwait, ctypes.pointer(start))
        )
        return await asyncio.shield(self._waiter)

    async def _join_waiter(self) -> None:
        if self._waiter is not None:
            await asyncio.wait({self._waiter})
            self._waiter = None

    def _refresh(self) -> None:
        """Read how many frames DCAM has delivered, and check them for loss."""
        if not self._started or self._slots is None:
            return
        info = _api.DCAMCAP_TRANSFERINFO.new()
        self._call("dcamcap_transferinfo", self._hdcam, ctypes.pointer(info))
        self._delivered = info.nFrameCount
        if self._lost is None and self._framestamps is not None:
            self._check_framestamps()
        # DCAM's own report counts nothing, so a gap in the stamps, which
        # does, is preferred when both are seen.
        if self._lost is None and self._dcam_reported_loss:
            self._lost = FramesLost(self._delivered, None)

    def _check_framestamps(self) -> None:
        # Frame stamps count every frame the camera exposed, so a frame
        # ``k`` frames after the last one checked must be stamped ``k`` later.
        # Only frames whose slots the grabber cannot be rewriting are read,
        # which may skip some after a caller fell behind; the difference
        # still shows a gap anywhere in between.
        held = len(self._slots)
        first = max(0, self._delivered - held + self._in_flight)
        if self._checked is not None:
            first = max(first, self._checked[0] + 1)
        for frame in range(first, self._delivered):
            stamp = int(self._framestamps[frame % held])
            if self._checked is not None:
                last_frame, last_stamp = self._checked
                counted = (stamp - last_stamp) & 0xFFFFFFFF  # an int32 that wraps
                if counted > frame - last_frame:
                    self._lost = FramesLost(last_frame + 1, counted - (frame - last_frame))
                    return
            self._checked = (frame, stamp)

    # --- properties and calls -----------------------------------------------------------

    def _value(self, name: str) -> float:
        value = c_double()
        self._call("dcamprop_getvalue", self._hdcam, self._ids[name], ctypes.pointer(value))
        return value.value

    def _value_text(self, name: str) -> str:
        """The label DCAM gives the property's current value, such as MONO16."""
        text = ctypes.create_string_buffer(64)
        request = _api.DCAMPROP_VALUETEXT.new(
            iProp=self._ids[name],
            value=self._value(name),
            text=ctypes.addressof(text),
            textbytes=len(text),
        )
        self._call("dcamprop_getvaluetext", self._hdcam, ctypes.pointer(request))
        return text.value.decode()

    def _call(self, name: str, *args) -> int:
        return self._check(name, getattr(self._lib, name)(*args))

    def _check(self, name: str, code: int) -> int:
        # Success is any positive value (the reference), not only SUCCESS.
        if code < 0:
            raise DcamError(name, code, self._error_text(code))
        return code

    def _attempt(self, errors: list, name: str, *args) -> bool:
        try:
            self._call(name, *args)
        except DcamError as error:
            errors.append(error)
            return False
        return True

    def _error_text(self, code: int) -> str:
        text = ctypes.create_string_buffer(256)
        request = _api.DCAMDEV_STRING.new(
            iString=code, text=ctypes.addressof(text), textbytes=len(text)
        )
        try:
            if self._lib.dcamdev_getstring(self._hdcam, ctypes.pointer(request)) < 0:
                return ""
        except Exception:
            # The error being described matters more than why its text
            # could not be had.
            return ""
        return text.value.decode(errors="replace")

    def _require_open(self) -> None:
        if self._closed:
            raise RuntimeError("the capture has been closed")


def _raise_first(errors: list) -> None:
    if not errors:
        return
    first, *later = errors
    for error in later:
        first.add_note(f"then: {error}")
    raise first
