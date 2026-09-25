"""The DCAM-API calls the DCAM adapter makes, bound through ctypes.

Private to the adapter: nothing outside ``stacscope.adapters.dcam`` should
import it. It loads ``dcamapi.dll``, which the DCAM-API driver installs, and
runs on Windows only.
"""
# For end use and not for commercial purposes.
#
# Thin on purpose (the plan's "The DCAM SDK licence"). Only the functions
# DcamFrameCapture calls are bound, and only the constants it acts on are
# written here; property IDs and value labels are looked up by name at run
# time. Struct layouts have to be written out, and the local-only conformance
# test checks them against the installed headers. The SDK's reference also
# forbids a wrapper "almost the same as DCAM-API itself", so this module
# exposes nothing beyond what the adapter needs.

import ctypes
import sys
from ctypes import POINTER, Structure, c_double, c_int32, c_uint32, c_void_p


def _signed(code: int) -> int:
    """A DCAMERR as the DLL returns it: a 32-bit signed int, negative on failure."""
    return code - (1 << 32) if code & 0x80000000 else code


# --- errors (DCAMERR) --------------------------------------------------------------

SUCCESS = 1
ERR_BUSY = _signed(0x80000101)
ERR_ABORT = _signed(0x80000102)
ERR_NOTREADY = _signed(0x80000103)
ERR_TIMEOUT = _signed(0x80000106)
ERR_LOSTFRAME = _signed(0x80000301)
ERR_MISSINGFRAME_TROUBLE = _signed(0x80000F06)
ERR_NOCAMERA = _signed(0x80000206)
ERR_OUTOFRANGE = _signed(0x80000822)
ERR_NOPROPERTY = _signed(0x80000828)
ERR_NOVALUETEXT = _signed(0x8000082D)
ERR_NOTSUPPORT = _signed(0x80000F03)

ERROR_NAMES = {
    ERR_BUSY: "DCAMERR_BUSY",
    ERR_ABORT: "DCAMERR_ABORT",
    ERR_NOTREADY: "DCAMERR_NOTREADY",
    ERR_TIMEOUT: "DCAMERR_TIMEOUT",
    ERR_LOSTFRAME: "DCAMERR_LOSTFRAME",
    ERR_MISSINGFRAME_TROUBLE: "DCAMERR_MISSINGFRAME_TROUBLE",
    ERR_NOCAMERA: "DCAMERR_NOCAMERA",
    ERR_OUTOFRANGE: "DCAMERR_OUTOFRANGE",
    ERR_NOPROPERTY: "DCAMERR_NOPROPERTY",
    ERR_NOVALUETEXT: "DCAMERR_NOVALUETEXT",
    ERR_NOTSUPPORT: "DCAMERR_NOTSUPPORT",
}
"""Names for the codes the adapter acts on. Any other code is reported by
number, with DCAM's own text for it."""

# --- other constants ---------------------------------------------------------------

INITOPTION_APIVER_LATEST = 0x00000001
INITOPTION_ENDMARK = 0x00000000

CAPDOMAIN_FUNCTION = 0x00000000
CAPFLAG_FRAMESTAMP = 0x00000001
CAPFLAG_TIMESTAMP = 0x00000002

ATTACHKIND_FRAME = 0
ATTACHKIND_TIMESTAMP = 1
ATTACHKIND_FRAMESTAMP = 2

CAPSTART_SEQUENCE = -1

CAPEVENT_FRAMEREADY = 0x0002
CAPEVENT_STOPPED = 0x0010

PROPOPTION_SUPPORT = 0x00000000
PROPOPTION_NEXT = 0x01000000
PROPOPTION_PRIOR = _signed(0xFF000000)


# --- structures ----------------------------------------------------------------------
# Each begins with its own size, which DCAM reads to tell versions apart; new()
# fills it in. Pointers to caller-owned text are c_void_p rather than c_char_p,
# so that the address of a buffer can be set and read back; the layout is the
# same. The headers ask for #pragma pack(8), which changes nothing here: no
# member is aligned wider than 8 bytes on 64-bit Windows.


class _Sized(Structure):
    @classmethod
    def new(cls, **fields):
        struct = cls(**fields)
        setattr(struct, cls._fields_[0][0], ctypes.sizeof(cls))
        return struct


class DCAMAPI_INIT(_Sized):
    _fields_ = [
        ("size", c_int32),
        ("iDeviceCount", c_int32),
        ("reserved", c_int32),
        ("initoptionbytes", c_int32),
        ("initoption", POINTER(c_int32)),
        ("guid", c_void_p),
    ]


class DCAMDEV_OPEN(_Sized):
    _fields_ = [("size", c_int32), ("index", c_int32), ("hdcam", c_void_p)]


class DCAMDEV_CAPABILITY(_Sized):
    _fields_ = [("size", c_int32), ("domain", c_int32), ("capflag", c_int32), ("kind", c_int32)]


class DCAMDEV_STRING(_Sized):
    _fields_ = [("size", c_int32), ("iString", c_int32), ("text", c_void_p), ("textbytes", c_int32)]


class DCAMPROP_VALUETEXT(_Sized):
    _fields_ = [
        ("cbSize", c_int32),
        ("iProp", c_int32),
        ("value", c_double),
        ("text", c_void_p),
        ("textbytes", c_int32),
    ]


class DCAMBUF_ATTACH(_Sized):
    _fields_ = [
        ("size", c_int32),
        ("iKind", c_int32),
        ("buffer", POINTER(c_void_p)),
        ("buffercount", c_int32),
    ]


class DCAM_TIMESTAMP(Structure):
    _fields_ = [("sec", c_uint32), ("microsec", c_int32)]


class DCAMCAP_TRANSFERINFO(_Sized):
    _fields_ = [
        ("size", c_int32),
        ("iKind", c_int32),
        ("nNewestFrameIndex", c_int32),
        ("nFrameCount", c_int32),
    ]


class DCAMWAIT_OPEN(_Sized):
    _fields_ = [
        ("size", c_int32),
        ("supportevent", c_int32),
        ("hwait", c_void_p),
        ("hdcam", c_void_p),
    ]


class DCAMWAIT_START(_Sized):
    _fields_ = [
        ("size", c_int32),
        ("eventhappened", c_int32),
        ("eventmask", c_int32),
        ("timeout", c_int32),
    ]


# --- functions ---------------------------------------------------------------------

FUNCTIONS = {
    "dcamapi_init": [POINTER(DCAMAPI_INIT)],
    "dcamapi_uninit": [],
    "dcamdev_open": [POINTER(DCAMDEV_OPEN)],
    "dcamdev_close": [c_void_p],
    "dcamdev_getcapability": [c_void_p, POINTER(DCAMDEV_CAPABILITY)],
    "dcamdev_getstring": [c_void_p, POINTER(DCAMDEV_STRING)],
    "dcamprop_getnextid": [c_void_p, POINTER(c_int32), c_int32],
    "dcamprop_getname": [c_void_p, c_int32, ctypes.c_char_p, c_int32],
    "dcamprop_getvalue": [c_void_p, c_int32, POINTER(c_double)],
    "dcamprop_getvaluetext": [c_void_p, POINTER(DCAMPROP_VALUETEXT)],
    "dcamprop_setgetvalue": [c_void_p, c_int32, POINTER(c_double), c_int32],
    "dcamprop_queryvalue": [c_void_p, c_int32, POINTER(c_double), c_int32],
    "dcambuf_attach": [c_void_p, POINTER(DCAMBUF_ATTACH)],
    "dcambuf_release": [c_void_p, c_int32],
    "dcamcap_start": [c_void_p, c_int32],
    "dcamcap_stop": [c_void_p],
    "dcamcap_transferinfo": [c_void_p, POINTER(DCAMCAP_TRANSFERINFO)],
    "dcamcap_firetrigger": [c_void_p, c_int32],
    "dcamwait_open": [POINTER(DCAMWAIT_OPEN)],
    "dcamwait_close": [c_void_p],
    "dcamwait_start": [c_void_p, POINTER(DCAMWAIT_START)],
    "dcamwait_abort": [c_void_p],
}
"""Every function the adapter calls, with its argument types. Each returns a
DCAMERR as a signed 32-bit int."""


class DcamUnavailable(RuntimeError):
    """DCAM-API cannot be used here: not Windows, or not installed."""


def load():
    """``dcamapi.dll``, with every function in ``FUNCTIONS`` typed. Raises
    ``DcamUnavailable`` off Windows, or if DCAM-API is not installed."""
    if sys.platform != "win32":
        raise DcamUnavailable("the DCAM adapter runs on Windows only")
    try:
        library = ctypes.WinDLL("dcamapi.dll")
    except OSError as exc:
        raise DcamUnavailable("dcamapi.dll could not be loaded: is DCAM-API installed?") from exc
    for name, argtypes in FUNCTIONS.items():
        try:
            function = getattr(library, name)
        except AttributeError:
            raise DcamUnavailable(
                f"dcamapi.dll has no {name}: DCAM-API 4 or later is required"
            ) from None
        function.argtypes = argtypes
        function.restype = c_int32
    return library
