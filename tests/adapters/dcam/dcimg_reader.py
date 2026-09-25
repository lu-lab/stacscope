"""Reads DCIMG files through Hamamatsu's dcimgapi.dll, which DCAM-API
installs, so a test can check what the camera's recorder wrote.

Test support, not part of the adapter: nothing in STACScope reads DCIMG yet.
Windows only. Its structs are checked against the DCIMG SDK's header by
test_conformance.py, when STACSCOPE_DCIMG_SDK names the SDK folder.
"""
# For end use and not for commercial purposes.
#
# Thin, like the DCAM binding: only the five calls a read-back needs. The
# file is opened with dcimg_openA and a UTF-8 path, as the SDK's own Python
# sample does; what dcimg_openW expects of its codepage is not documented.

import ctypes
import os
import sys
from ctypes import POINTER, Structure, c_char_p, c_double, c_int32, c_uint32, c_void_p

import numpy as np

SUCCESS = 1
CODEPAGE_UTF8 = 65001

IDPARAML_NUMBEROF_TOTALFRAME = 0
IDPARAML_NUMBEROF_SESSION = 1
IDPARAML_IMAGE_WIDTH = 9
IDPARAML_IMAGE_HEIGHT = 10
IDPARAML_IMAGE_ROWBYTES = 11
IDPARAML_IMAGE_PIXELTYPE = 12

PIXELTYPE_MONO8 = 0x00000001
PIXELTYPE_MONO16 = 0x00000002

_DTYPES = {PIXELTYPE_MONO8: np.dtype("u1"), PIXELTYPE_MONO16: np.dtype("<u2")}


class DCIMG_INIT(Structure):
    _fields_ = [("size", c_int32), ("reserved", c_int32), ("guid", c_void_p)]


class DCIMG_OPENA(Structure):
    _fields_ = [("size", c_int32), ("codepage", c_int32), ("hdcimg", c_void_p), ("path", c_char_p)]


class DCIMG_TIMESTAMP(Structure):
    _fields_ = [("sec", c_uint32), ("microsec", c_int32)]


class DCIMG_FRAME(Structure):
    _fields_ = [
        ("size", c_int32),
        ("iKind", c_int32),
        ("option", c_int32),
        ("iFrame", c_int32),
        ("buf", c_void_p),
        ("rowbytes", c_int32),
        ("type", c_int32),
        ("width", c_int32),
        ("height", c_int32),
        ("left", c_int32),
        ("top", c_int32),
        ("timestamp", DCIMG_TIMESTAMP),
        ("framestamp", c_int32),
        ("camerastamp", c_int32),
        ("conversionfactor_coeff", c_double),
        ("conversionfactor_offset", c_double),
    ]


FUNCTIONS = {
    "dcimg_init": [POINTER(DCIMG_INIT)],
    "dcimg_openA": [POINTER(DCIMG_OPENA)],
    "dcimg_close": [c_void_p],
    "dcimg_getparaml": [c_void_p, c_int32, POINTER(c_int32)],
    "dcimg_copyframe": [c_void_p, POINTER(DCIMG_FRAME)],
}

_library = None


def _load():
    global _library
    if _library is None:
        if sys.platform != "win32":
            raise OSError("dcimgapi.dll is Windows only")
        library = ctypes.WinDLL("dcimgapi.dll")
        for name, argtypes in FUNCTIONS.items():
            function = getattr(library, name)
            function.argtypes = argtypes
            function.restype = c_int32
        init = DCIMG_INIT(size=ctypes.sizeof(DCIMG_INIT))
        _check("dcimg_init", library.dcimg_init(ctypes.byref(init)))
        _library = library
    return _library


def _check(call: str, code: int) -> None:
    if code < 0:
        raise OSError(f"{call} failed: {code & 0xFFFFFFFF:#010x}")


class DcimgFile:
    """A DCIMG file open for reading: ``frames`` in it, their ``shape`` and
    ``dtype``, and ``frame(i)`` for one frame with its stamps."""

    def __init__(self, path: str | os.PathLike) -> None:
        self._lib = _load()
        request = DCIMG_OPENA(
            size=ctypes.sizeof(DCIMG_OPENA),
            codepage=CODEPAGE_UTF8,
            path=os.fspath(path).encode("utf-8"),
        )
        _check("dcimg_openA", self._lib.dcimg_openA(ctypes.byref(request)))
        self._handle = request.hdcimg
        self.frames = self._param(IDPARAML_NUMBEROF_TOTALFRAME)
        self.sessions = self._param(IDPARAML_NUMBEROF_SESSION)
        self.shape = (self._param(IDPARAML_IMAGE_HEIGHT), self._param(IDPARAML_IMAGE_WIDTH))
        self.row_bytes = self._param(IDPARAML_IMAGE_ROWBYTES)
        pixel_type = self._param(IDPARAML_IMAGE_PIXELTYPE)
        if pixel_type not in _DTYPES:
            raise ValueError(f"pixel type {pixel_type:#x} is not MONO8 or MONO16")
        self.dtype = _DTYPES[pixel_type]
        self._pixel_type = pixel_type

    def frame(self, index: int) -> tuple[np.ndarray, int, int]:
        """Frame ``index``: its pixels, its frame stamp, and its time stamp
        in microseconds."""
        image = np.zeros(self.shape, self.dtype)
        request = DCIMG_FRAME(
            size=ctypes.sizeof(DCIMG_FRAME),
            iFrame=index,
            buf=image.ctypes.data,
            rowbytes=image.strides[0],
            # The header marks it reserved, but copyframe refuses a frame
            # without it (DCIMG_ERR_INVALIDVALUE); the SDK's sample sets it.
            type=self._pixel_type,
            width=self.shape[1],
            height=self.shape[0],
        )
        _check("dcimg_copyframe", self._lib.dcimg_copyframe(self._handle, ctypes.byref(request)))
        stamp = request.timestamp
        return image, request.framestamp, stamp.sec * 1_000_000 + stamp.microsec

    def close(self) -> None:
        if self._handle is not None:
            _check("dcimg_close", self._lib.dcimg_close(self._handle))
            self._handle = None

    def __enter__(self):
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _param(self, which: int) -> int:
        value = c_int32()
        _check("dcimg_getparaml", self._lib.dcimg_getparaml(self._handle, which, ctypes.byref(value)))
        return value.value
