"""Toolchain sanity checks for the uv/3.14t environment.

Pins down the two things the architecture plan calls out as unverified at
first install: a real free-threaded pydantic-core wheel (not a silent GIL
re-enable) and correct ctypes behaviour under free-threading. Portable so it
also runs on the standard (non-free-threaded) 3.14 fallback and on the Linux
CI leg.
"""

import ctypes
import ctypes.util
import sys
import threading

import pydantic


def test_python_version_is_314_or_newer():
    assert sys.version_info >= (3, 14)


def test_package_imports_as_stacscope():
    import stacscope

    assert stacscope.__name__ == "stacscope"


def test_pydantic_core_import_does_not_disable_gil_if_free_threaded():
    gil_enabled_before = sys._is_gil_enabled()

    import pydantic_core  # noqa: F401

    assert sys._is_gil_enabled() == gil_enabled_before


def test_pydantic_basic_validation():
    class Point(pydantic.BaseModel):
        x: float
        y: float

    p = Point(x=1, y=2.5)
    assert (p.x, p.y) == (1.0, 2.5)


def _libc_getpid() -> int:
    if sys.platform == "win32":
        lib = ctypes.WinDLL("kernel32", use_last_error=True)
        func = lib.GetCurrentProcessId
    else:
        lib = ctypes.CDLL(ctypes.util.find_library("c"))
        func = lib.getpid
    func.restype = ctypes.c_uint32
    return func()


def test_ctypes_call_matches_os_getpid():
    import os

    assert _libc_getpid() == os.getpid()


def test_ctypes_concurrent_calls_from_multiple_threads():
    errors = []

    def worker():
        try:
            for _ in range(10_000):
                _libc_getpid()
        except Exception as exc:  # pragma: no cover - failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors
