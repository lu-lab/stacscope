"""A stand-in for dcamapi.dll: the raw DCAM-API function table, in Python, with
one simulated camera behind it.

What this can and cannot prove, stated plainly because it is easy to forget:
it pins *our* call sequence, and our use of the structs, against DCAM-API as
its reference documents it. It cannot prove the argument types or the struct
layouts -- it uses the adapter's own structs -- which is the conformance
test's job against the installed headers, and it cannot prove what the camera
does, which only 5d can. Where the documentation is silent, the fake takes
the reading that is harder on the adapter, and says so beside the code:

- ``dcamwait_start`` sees only events that happen while it waits, and
  ``dcamwait_abort`` with nobody waiting does nothing (documented).
- Frame stamps run on across captures from an arbitrary number, rather than
  restarting at 0 (D12 is still open for the Flash).
- A lost frame is reported by a gap in the frame stamps, by
  ``DCAMERR_LOSTFRAME`` from the next wait, or both (``loss_report``).
- Property IDs are numbered unlike dcamprop.h, and pixel types unlike
  dcamapi4.h, so an adapter that hard-coded either fails here.

Every call is logged in ``calls``, and any use the reference forbids -- a
call after ``dcamapi_uninit``, closing a wait handle while a wait is running
on it, a null frame address -- is recorded in ``misuse``, which every test
expects to be empty.
"""
# For end use and not for commercial purposes.

import ctypes
import threading
import time
from dataclasses import dataclass, field

import numpy as np

from stacscope.adapters.dcam import _api

ERR_INVALIDHANDLE = _api._signed(0x80000807)
ERR_INVALIDPARAM = _api._signed(0x80000808)
ERR_INVALIDPROPERTYID = _api._signed(0x80000825)
ERR_NOVALUETEXT = _api._signed(0x8000082D)
ERR_INVALIDWAITHANDLE = _api._signed(0x84002001)

PIXEL_TYPES = {"MONO8": 21, "MONO16": 22, "MONO12": 23, "MONO12P": 25}
"""Value numbers for BUFFER PIXEL TYPE, deliberately not dcamapi4.h's."""

PIXEL_BYTES = {"MONO8": 1, "MONO16": 2, "MONO12": 2, "MONO12P": 1.5}

_ID_BASE = 0x0FA00010
"""Property IDs start here, stepping by 0x10: nowhere near dcamprop.h's."""

TIMESTAMP_START_US = 1_700_000_000_000_000
FRAME_PERIOD_US = 10_000
"""The frame stamped ``1000 + n`` is time-stamped ``TIMESTAMP_START_US + n *
FRAME_PERIOD_US`` microseconds, as seconds and microseconds."""


def error_text(code: int) -> str:
    """What the fake's ``dcamdev_getstring`` says about an error code."""
    return f"fake DCAM text for {code & 0xFFFFFFFF:#010x}"


@dataclass
class _Property:
    id: int
    value: float
    texts: dict[float, str] = field(default_factory=dict)


class FakeDcam:
    """The library, one camera, and the controls a test drives it with.

    The camera's format is set here as its properties would set it: ``width``
    and ``height`` of the image, ``pixel_type`` by its DCAM name, ``row_bytes``
    and ``top_offset`` for what the grabber adds, and ``frame_bytes`` for the
    whole of what it writes per frame. ``stamps`` says whether the camera can
    attach time and frame stamp buffers. ``in_flight`` is how many frames past
    the newest reported one the grabber has begun writing (see ``pulse``).
    """

    def __init__(
        self,
        *,
        width: int = 40,
        height: int = 24,
        pixel_type: str = "MONO16",
        row_bytes: int | None = None,
        top_offset: int = 0,
        frame_bytes: int | None = None,
        cameras: int = 1,
        stamps: bool = True,
        in_flight: int = 0,
        loss_report: str = "both",
        framestamp_start: int = 1000,
    ) -> None:
        pixel = PIXEL_BYTES[pixel_type]
        row_bytes = row_bytes or int(width * pixel)
        frame_bytes = frame_bytes or top_offset + height * row_bytes
        self.pixel_type = pixel_type
        self.cameras = cameras
        self.stamps = stamps
        self.in_flight = in_flight
        self.loss_report = loss_report
        self.calls: list[str] = []
        self.misuse: list[str] = []
        self.missed = 0
        """Pulses that arrived when no capture was running."""
        self.wait_gate: threading.Event | None = None
        """If set, every ``dcamwait_start`` stops at its entrance, before it
        begins to wait, until the gate opens. ``at_gate`` is set when one
        arrives there. A test uses the two to act in that gap."""
        self.at_gate = threading.Event()
        self.linger = 0.0
        """Seconds a wait stays inside ``dcamwait_start`` after it has its
        answer, as a driver's thread may before it returns."""

        # IMAGE ROWBYTES and friends describe the image as the camera sends it;
        # BUFFER ROWBYTES and friends describe it in memory the adapter
        # attaches. They differ here, so reading the wrong ones shows.
        types = {number: name for name, number in PIXEL_TYPES.items()}
        self._properties: dict[str, _Property] = {}
        for name, value, texts in [
            ("SENSOR MODE", 1, {1: "AREA", 12: "PROGRESSIVE"}),
            ("TRIGGER SOURCE", 2, {1: "INTERNAL", 2: "EXTERNAL", 3: "SOFTWARE"}),
            ("IMAGE WIDTH", width, {}),
            ("IMAGE HEIGHT", height, {}),
            ("IMAGE ROWBYTES", int(width * pixel), {}),
            ("IMAGE FRAMEBYTES", int(width * pixel) * height, {}),
            ("IMAGE TOP OFFSET BYTES", 0, {}),
            ("IMAGE PIXEL TYPE", PIXEL_TYPES[pixel_type], types),
            ("BUFFER ROWBYTES", row_bytes, {}),
            ("BUFFER FRAMEBYTES", frame_bytes, {}),
            ("BUFFER TOP OFFSET BYTES", top_offset, {}),
            ("BUFFER PIXEL TYPE", PIXEL_TYPES[pixel_type], types),
        ]:
            self._properties[name] = _Property(_ID_BASE + 0x10 * len(self._properties), value, texts)

        self._lock = threading.Condition()
        self._scripted: dict[str, list[tuple[str, int]]] = {}
        self._initialized = False
        self._hdcam: int | None = None
        self._next_handle = 0x5000
        self._waits: dict[int, int] = {}  # hwait -> number of calls waiting on it
        self._events: list[tuple[str, int | None]] = []  # (kind, hwait for an abort)
        self._lost_pending = False
        self._busy = False
        self._frames: list[int] | None = None
        self._timestamps: list[int] | None = None
        self._framestamps: list[int] | None = None
        self._count = 0
        self._newest = -1
        self._framestamp = framestamp_start
        self._begun: list[int] = []  # frames the grabber has begun but not finished

    # --- controls ------------------------------------------------------------------

    def fail(self, name: str, code: int, *, times: int = 1, after: int = 0) -> None:
        """Let the next ``after`` calls to ``name`` work, then make ``times``
        calls do nothing and return ``code``."""
        script = self._scripted.setdefault(name, [])
        script.extend([("pass", 0)] * after + [("fail", code)] * times)

    def succeed_with(self, name: str, code: int) -> None:
        """Make the next call to ``name`` work, and return ``code``: DCAM allows
        a success that is any positive number."""
        self._scripted.setdefault(name, []).append(("succeed", code))

    def set_property(self, name: str, value: float) -> None:
        """Change a property, as a camera setting would."""
        self._properties[name].value = value

    def remove_property(self, name: str) -> None:
        """Make the camera one that lacks ``name``."""
        del self._properties[name]

    def pulse(self, n: int = 1) -> None:
        """``n`` trigger pulses on the camera's input. Each begins a frame in
        the next slot; a frame is finished, stamped and reported once
        ``in_flight`` more have begun, or at ``settle``. Pulses outside a
        capture are counted in ``missed``."""
        with self._lock:
            for _ in range(n):
                if not self._busy:
                    self.missed += 1
                    continue
                frame = self._count + len(self._begun)
                self._begin(frame)
                self._begun.append(frame)
                while len(self._begun) > self.in_flight:
                    self._finish(self._begun.pop(0))
            self._lock.notify_all()

    def settle(self) -> None:
        """Let the grabber finish every frame it has begun, as it does once
        the triggers stop."""
        with self._lock:
            while self._begun:
                self._finish(self._begun.pop(0))
            self._lock.notify_all()

    def lose(self, n: int = 1) -> None:
        """The camera exposes ``n`` frames the grabber never delivers."""
        with self._lock:
            if self.loss_report in ("gap", "both"):
                self._framestamp += n  # the camera counted them; nobody saw them
            if self.loss_report in ("error", "both"):
                self._lost_pending = True
            self._lock.notify_all()

    @property
    def attached(self) -> bool:
        return self._frames is not None

    @property
    def capturing(self) -> bool:
        return self._busy

    def waiting(self) -> int:
        """How many calls are inside ``dcamwait_start`` now."""
        with self._lock:
            return sum(self._waits.values())

    # --- the grabber -------------------------------------------------------------------

    def _begin(self, frame: int) -> None:
        """Write the frame's first row, as the grabber does before the rest."""
        slot = frame % len(self._frames)
        image = self._image(frame)
        top = int(self._value("BUFFER TOP OFFSET BYTES"))
        row = int(self._value("IMAGE WIDTH") * PIXEL_BYTES[self.pixel_type])
        ctypes.memmove(self._frames[slot] + top, image[top : top + row].ctypes.data, row)

    def _finish(self, frame: int) -> None:
        slot = frame % len(self._frames)
        image = self._image(frame)
        ctypes.memmove(self._frames[slot], image.ctypes.data, image.nbytes)
        if self._timestamps is not None:
            micros = TIMESTAMP_START_US + (self._framestamp - 1000) * FRAME_PERIOD_US
            stamp = _api.DCAM_TIMESTAMP.from_address(self._timestamps[slot])
            stamp.sec, stamp.microsec = divmod(micros, 1_000_000)
        if self._framestamps is not None:
            ctypes.c_int32.from_address(self._framestamps[slot]).value = _as_int32(self._framestamp)
        self._framestamp += 1
        self._count += 1
        self._newest = slot
        self._events.append(("frameready", None))

    def _image(self, frame: int) -> np.ndarray:
        """Everything the grabber writes for a frame: pixels of one value, never
        the contract's sentinel, and zeros in any padding."""
        image = np.zeros(int(self._value("BUFFER FRAMEBYTES")), np.uint8)
        top = int(self._value("BUFFER TOP OFFSET BYTES"))
        pitch = int(self._value("BUFFER ROWBYTES"))
        row = int(self._value("IMAGE WIDTH") * PIXEL_BYTES[self.pixel_type])
        value = frame % 200 + 1
        for r in range(int(self._value("IMAGE HEIGHT"))):
            image[top + r * pitch : top + r * pitch + row] = value
        return image

    def _value(self, name: str) -> float:
        return self._properties[name].value

    # --- the function table --------------------------------------------------------------

    def _call(self, name: str) -> tuple[str, int] | None:
        self.calls.append(name)
        scripted = self._scripted.get(name)
        if scripted:
            step = scripted.pop(0)
            if step[0] != "pass":
                return step
        if not self._initialized and name != "dcamapi_init":
            self.misuse.append(f"{name} before dcamapi_init or after dcamapi_uninit")
            return ("fail", ERR_INVALIDHANDLE)
        return None

    def _device(self, name: str, hdcam) -> int | None:
        if hdcam is None or hdcam != self._hdcam:
            self.misuse.append(f"{name} with a handle that is not open: {hdcam!r}")
            return ERR_INVALIDHANDLE
        return None

    def dcamapi_init(self, param):
        with self._lock:
            if (scripted := self._call("dcamapi_init")) and scripted[0] == "fail":
                return scripted[1]
            p = param.contents
            if p.size != ctypes.sizeof(_api.DCAMAPI_INIT):
                self.misuse.append("DCAMAPI_INIT.size not set")
            if self._initialized:
                self.misuse.append("dcamapi_init while initialized")
            self._initialized = True
            p.iDeviceCount = self.cameras
            if self.cameras == 0:
                return _api.ERR_NOCAMERA  # documented: an error when none is found
            return scripted[1] if scripted else _api.SUCCESS

    def dcamapi_uninit(self):
        with self._lock:
            if (scripted := self._call("dcamapi_uninit")) and scripted[0] == "fail":
                return scripted[1]
            if self._hdcam is not None:
                self._close_device()
            self._initialized = False
            return _api.SUCCESS

    def dcamdev_open(self, param):
        with self._lock:
            if scripted := self._call("dcamdev_open"):
                if scripted[0] == "fail":
                    return scripted[1]
            p = param.contents
            if not 0 <= p.index < self.cameras:
                return ERR_INVALIDPARAM
            self._hdcam = self._next_handle
            self._next_handle += 1
            p.hdcam = self._hdcam
            return _api.SUCCESS

    def dcamdev_close(self, hdcam):
        with self._lock:
            if (scripted := self._call("dcamdev_close")) and scripted[0] == "fail":
                return scripted[1]
            if error := self._device("dcamdev_close", hdcam):
                return error
            self._close_device()
            return _api.SUCCESS

    def _close_device(self) -> None:
        # Documented: "forcibly terminates active capturing and waiting
        # sessions, and releases attached or allocated buffers".
        self._busy = False
        for hwait, waiting in self._waits.items():
            if waiting:
                self._events.append(("abort", hwait))
        self._waits.clear()
        self._frames = self._timestamps = self._framestamps = None
        self._hdcam = None
        self._lock.notify_all()

    def dcamdev_getcapability(self, hdcam, param):
        with self._lock:
            if (scripted := self._call("dcamdev_getcapability")) and scripted[0] == "fail":
                return scripted[1]
            if error := self._device("dcamdev_getcapability", hdcam):
                return error
            p = param.contents
            if p.domain == _api.CAPDOMAIN_FUNCTION and self.stamps:
                p.capflag = _api.CAPFLAG_TIMESTAMP | _api.CAPFLAG_FRAMESTAMP
            else:
                p.capflag = 0
            return _api.SUCCESS

    def dcamdev_getstring(self, hdcam, param):
        with self._lock:
            if (scripted := self._call("dcamdev_getstring")) and scripted[0] == "fail":
                return scripted[1]
            p = param.contents
            if p.iString < 0:
                text = error_text(p.iString)
            elif p.iString == 0x04000104:
                text = "C13440-20C"  # DCAM_IDSTR_MODEL
            else:
                return ERR_INVALIDPARAM
            data = text.encode() + b"\0"
            if len(data) > p.textbytes:
                data = data[: p.textbytes - 1] + b"\0"
            ctypes.memmove(p.text, data, len(data))
            return _api.SUCCESS

    def dcamprop_getnextid(self, hdcam, pointer, option):
        with self._lock:
            if (scripted := self._call("dcamprop_getnextid")) and scripted[0] == "fail":
                return scripted[1]
            if error := self._device("dcamprop_getnextid", hdcam):
                return error
            current = pointer.contents.value
            later = sorted(p.id for p in self._properties.values() if p.id > current)
            if not later:
                return _api.ERR_NOPROPERTY
            pointer.contents.value = later[0]
            return _api.SUCCESS

    def dcamprop_getname(self, hdcam, iprop, text, textbytes):
        with self._lock:
            if (scripted := self._call("dcamprop_getname")) and scripted[0] == "fail":
                return scripted[1]
            if error := self._device("dcamprop_getname", hdcam):
                return error
            name = self._name(iprop)
            if name is None:
                return ERR_INVALIDPROPERTYID
            text.value = name.encode()[: textbytes - 1]
            return _api.SUCCESS

    def dcamprop_getvalue(self, hdcam, iprop, pointer):
        with self._lock:
            if (scripted := self._call("dcamprop_getvalue")) and scripted[0] == "fail":
                return scripted[1]
            if error := self._device("dcamprop_getvalue", hdcam):
                return error
            name = self._name(iprop)
            if name is None:
                return ERR_INVALIDPROPERTYID
            pointer.contents.value = self._properties[name].value
            return _api.SUCCESS

    def dcamprop_getvaluetext(self, hdcam, param):
        with self._lock:
            if (scripted := self._call("dcamprop_getvaluetext")) and scripted[0] == "fail":
                return scripted[1]
            if error := self._device("dcamprop_getvaluetext", hdcam):
                return error
            p = param.contents
            name = self._name(p.iProp)
            if name is None:
                return ERR_INVALIDPROPERTYID
            text = self._properties[name].texts.get(p.value)
            if text is None:
                return ERR_NOVALUETEXT
            data = text.encode()[: p.textbytes - 1] + b"\0"
            ctypes.memmove(p.text, data, len(data))
            return _api.SUCCESS

    def _name(self, iprop: int) -> str | None:
        for name, prop in self._properties.items():
            if prop.id == iprop:
                return name
        return None

    def dcambuf_attach(self, hdcam, param):
        with self._lock:
            scripted = self._call("dcambuf_attach")
            if scripted and scripted[0] == "fail":
                return scripted[1]
            if error := self._device("dcambuf_attach", hdcam):
                return error
            if self._busy:
                return _api.ERR_BUSY
            p = param.contents
            if p.size != ctypes.sizeof(_api.DCAMBUF_ATTACH):
                self.misuse.append("DCAMBUF_ATTACH.size not set")
            addresses = [p.buffer[i] for i in range(p.buffercount)]
            if not addresses or not all(addresses):
                # Documented: DCAM does not check, and "the system may hang up".
                self.misuse.append(f"dcambuf_attach with a null address: {addresses}")
                return ERR_INVALIDPARAM
            if p.iKind == _api.ATTACHKIND_FRAME:
                if self._frames is not None:
                    self.misuse.append("frames attached twice without a release")
                    return ERR_INVALIDPARAM
                self._frames = addresses
            elif p.iKind in (_api.ATTACHKIND_TIMESTAMP, _api.ATTACHKIND_FRAMESTAMP):
                if not self.stamps:
                    return _api.ERR_NOTSUPPORT
                if p.iKind == _api.ATTACHKIND_TIMESTAMP:
                    self._timestamps = addresses
                else:
                    self._framestamps = addresses
            else:
                return ERR_INVALIDPARAM
            return scripted[1] if scripted else _api.SUCCESS

    def dcambuf_release(self, hdcam, kind):
        with self._lock:
            if (scripted := self._call("dcambuf_release")) and scripted[0] == "fail":
                return scripted[1]
            if error := self._device("dcambuf_release", hdcam):
                return error
            if self._busy:
                return _api.ERR_BUSY  # documented
            if kind == _api.ATTACHKIND_FRAME:
                self._frames = None
            elif kind == _api.ATTACHKIND_TIMESTAMP:
                self._timestamps = None
            elif kind == _api.ATTACHKIND_FRAMESTAMP:
                self._framestamps = None
            else:
                return ERR_INVALIDPARAM
            return _api.SUCCESS

    def dcamcap_start(self, hdcam, mode):
        with self._lock:
            if (scripted := self._call("dcamcap_start")) and scripted[0] == "fail":
                return scripted[1]
            if error := self._device("dcamcap_start", hdcam):
                return error
            if self._frames is None:
                return _api.ERR_NOTREADY  # documented: no buffer prepared
            if self._busy:
                return _api.ERR_BUSY
            for stamps in (self._timestamps, self._framestamps):
                if stamps is not None and len(stamps) != len(self._frames):
                    self.misuse.append("stamp buffers and frames differ in count")
                    return ERR_INVALIDPARAM
            if mode != _api.CAPSTART_SEQUENCE:
                self.misuse.append(f"dcamcap_start in mode {mode}, not SEQUENCE")
            self._busy = True
            self._count = 0
            self._newest = -1
            self._begun = []
            self._lost_pending = False
            return _api.SUCCESS

    def dcamcap_stop(self, hdcam):
        with self._lock:
            if (scripted := self._call("dcamcap_stop")) and scripted[0] == "fail":
                return scripted[1]
            if error := self._device("dcamcap_stop", hdcam):
                return error
            if self._busy:
                self._busy = False
                self._begun = []  # frames cut short by the stop are never delivered
                self._events.append(("stopped", None))
                self._lock.notify_all()
            return _api.SUCCESS

    def dcamcap_transferinfo(self, hdcam, param):
        with self._lock:
            if (scripted := self._call("dcamcap_transferinfo")) and scripted[0] == "fail":
                return scripted[1]
            if error := self._device("dcamcap_transferinfo", hdcam):
                return error
            p = param.contents
            p.nNewestFrameIndex = self._newest
            p.nFrameCount = self._count
            return _api.SUCCESS

    def dcamwait_open(self, param):
        with self._lock:
            if (scripted := self._call("dcamwait_open")) and scripted[0] == "fail":
                return scripted[1]
            p = param.contents
            if error := self._device("dcamwait_open", p.hdcam):
                return error
            hwait = self._next_handle
            self._next_handle += 1
            self._waits[hwait] = 0
            p.hwait = hwait
            p.supportevent = _api.CAPEVENT_FRAMEREADY | _api.CAPEVENT_STOPPED
            return _api.SUCCESS

    def dcamwait_close(self, hwait):
        with self._lock:
            if (scripted := self._call("dcamwait_close")) and scripted[0] == "fail":
                return scripted[1]
            if hwait not in self._waits:
                self.misuse.append(f"dcamwait_close with a handle that is not open: {hwait!r}")
                return ERR_INVALIDWAITHANDLE
            if self._waits[hwait]:
                self.misuse.append("dcamwait_close while a wait is running on it")
            del self._waits[hwait]
            return _api.SUCCESS

    def dcamwait_abort(self, hwait):
        with self._lock:
            if (scripted := self._call("dcamwait_abort")) and scripted[0] == "fail":
                return scripted[1]
            if hwait not in self._waits:
                self.misuse.append(f"dcamwait_abort with a handle that is not open: {hwait!r}")
                return ERR_INVALIDWAITHANDLE
            if self._waits[hwait]:  # documented: with nobody waiting, nothing happens
                self._events.append(("abort", hwait))
                self._lock.notify_all()
            return _api.SUCCESS

    def dcamwait_start(self, hwait, param):
        if self.wait_gate is not None:
            self.at_gate.set()
            self.wait_gate.wait()
        with self._lock:
            if (scripted := self._call("dcamwait_start")) and scripted[0] == "fail":
                return scripted[1]
            if hwait not in self._waits:
                self.misuse.append(f"dcamwait_start with a handle that is not open: {hwait!r}")
                return ERR_INVALIDWAITHANDLE
            p = param.contents
            self._waits[hwait] += 1
            code = self._wait(hwait, p)
        # Still inside the call, and still counted as waiting, while it lingers.
        time.sleep(self.linger)
        with self._lock:
            if hwait in self._waits:
                self._waits[hwait] -= 1
        return code

    def _wait(self, hwait, p) -> int:
        deadline = time.monotonic() + p.timeout / 1000
        seen = len(self._events)  # only what happens from now on is seen
        while True:
            if self._lost_pending:
                self._lost_pending = False
                return _api.ERR_LOSTFRAME
            for kind, target in self._events[seen:]:
                if kind == "abort" and target == hwait:
                    return _api.ERR_ABORT
                if kind == "frameready" and p.eventmask & _api.CAPEVENT_FRAMEREADY:
                    p.eventhappened = _api.CAPEVENT_FRAMEREADY
                    return _api.SUCCESS
                if kind == "stopped" and p.eventmask & _api.CAPEVENT_STOPPED:
                    p.eventhappened = _api.CAPEVENT_STOPPED
                    return _api.SUCCESS
            seen = len(self._events)
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _api.ERR_TIMEOUT
            self._lock.wait(remaining)


def _as_int32(n: int) -> int:
    """A frame stamp as DCAM's int32 field holds it, wrapping as it does."""
    n &= 0xFFFFFFFF
    return n - (1 << 32) if n & 0x80000000 else n
