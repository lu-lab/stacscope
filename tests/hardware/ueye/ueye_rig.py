"""Helpers for the uEye suite: the camera opened for one test and always
closed, its settings, and a queue-mode sequence of image memories over one
of our own shared-memory segments, as the adapter will build it.

Every test opens the camera itself and exits it afterwards. The suite
changes nothing it must put back: is_InitCamera starts from the camera's
defaults (Y8), and nothing here writes the camera's EEPROM.

Run as a script (``python ueye_rig.py <camera>``), it captures freerun until
it is killed, for Y7."""

import contextlib
import ctypes
import sys
import time
import uuid
import warnings

import numpy as np

from stacscope.core.buffer import ImageBuffer
from stacscope.core.devices.image_streaming import layout_for
from stacscope.core.ports.frame_capture import FrameFormat


def ueye():
    """pyueye's ueye module. Its docstrings hold invalid escapes, which 3.14
    warns about once, when they are first compiled."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", SyntaxWarning)
        from pyueye import ueye as u
    return u


class UEyeError(RuntimeError):
    def __init__(self, what: str, code: int) -> None:
        super().__init__(f"{what} returned {code}")
        self.code = code


def ok(ret, what: str) -> None:
    if ret != ueye().IS_SUCCESS:
        raise UEyeError(what, int(ret))


@contextlib.contextmanager
def opened(camera: int):
    """The camera's handle, initialised; afterwards capture is stopped and the
    camera exited, whatever happened."""
    u = ueye()
    h = u.HIDS(camera)
    ok(u.is_InitCamera(h, None), "is_InitCamera")
    try:
        yield h
    finally:
        u.is_StopLiveVideo(h, u.IS_FORCE_VIDEO_STOP)
        u.is_ExitCamera(h)


# Settings. Each is read back from the camera, since it may round.


def color_mode(h) -> int:
    u = ueye()
    return int(u.is_SetColorMode(h, u.IS_GET_COLOR_MODE))


def set_color_mode(h, mode: int) -> None:
    ok(ueye().is_SetColorMode(h, mode), f"is_SetColorMode({mode})")


def aoi(h) -> tuple[int, int, int, int]:
    """(x, y, width, height)."""
    u = ueye()
    rect = u.IS_RECT()
    ok(u.is_AOI(h, u.IS_AOI_IMAGE_GET_AOI, rect, ctypes.sizeof(rect)), "is_AOI get")
    return rect.s32X.value, rect.s32Y.value, rect.s32Width.value, rect.s32Height.value


def set_aoi(h, x: int, y: int, width: int, height: int) -> None:
    u = ueye()
    rect = u.IS_RECT()
    rect.s32X, rect.s32Y, rect.s32Width, rect.s32Height = x, y, width, height
    ok(u.is_AOI(h, u.IS_AOI_IMAGE_SET_AOI, rect, ctypes.sizeof(rect)), "is_AOI set")


def pixel_clock(h) -> int:
    u = ueye()
    mhz = ctypes.c_uint()
    ok(u.is_PixelClock(h, u.IS_PIXELCLOCK_CMD_GET, mhz, ctypes.sizeof(mhz)), "is_PixelClock get")
    return mhz.value


def set_pixel_clock(h, mhz: int) -> None:
    u = ueye()
    value = ctypes.c_uint(mhz)
    ok(u.is_PixelClock(h, u.IS_PIXELCLOCK_CMD_SET, value, ctypes.sizeof(value)), "is_PixelClock set")


def pixel_clocks(h) -> list[int]:
    """Every pixel clock the camera offers, in MHz, ascending."""
    u = ueye()
    count = ctypes.c_uint()
    ok(u.is_PixelClock(h, u.IS_PIXELCLOCK_CMD_GET_NUMBER, count, ctypes.sizeof(count)), "is_PixelClock number")
    values = (ctypes.c_uint * count.value)()
    ok(u.is_PixelClock(h, u.IS_PIXELCLOCK_CMD_GET_LIST, values, ctypes.sizeof(values)), "is_PixelClock list")
    return sorted(values)


def frame_rate(h) -> float:
    u = ueye()
    fps = ctypes.c_double()
    ok(u.is_SetFrameRate(h, u.IS_GET_FRAMERATE, fps), "is_SetFrameRate get")
    return fps.value


def set_frame_rate(h, fps: float) -> float:
    u = ueye()
    actual = ctypes.c_double()
    ok(u.is_SetFrameRate(h, ctypes.c_double(fps), actual), "is_SetFrameRate")
    return actual.value


def exposure(h) -> float:
    """Milliseconds."""
    u = ueye()
    ms = ctypes.c_double()
    ok(u.is_Exposure(h, u.IS_EXPOSURE_CMD_GET_EXPOSURE, ms, ctypes.sizeof(ms)), "is_Exposure get")
    return ms.value


def set_exposure(h, ms: float) -> float:
    u = ueye()
    value = ctypes.c_double(ms)
    ok(u.is_Exposure(h, u.IS_EXPOSURE_CMD_SET_EXPOSURE, value, ctypes.sizeof(value)), "is_Exposure set")
    return value.value  # the driver writes back what it set


def set_trigger(h, mode: int) -> None:
    ok(ueye().is_SetExternalTrigger(h, mode), f"is_SetExternalTrigger({mode})")


def set_test_image(h, image: int, param: int = 0) -> None:
    ok(ueye().is_SetSensorTestImage(h, image, param), f"is_SetSensorTestImage({image:#x})")


def features(h) -> int:
    u = ueye()
    bits = ctypes.c_uint()
    ok(
        u.is_DeviceFeature(h, u.IS_DEVICE_FEATURE_CMD_GET_SUPPORTED_FEATURES, bits, ctypes.sizeof(bits)),
        "is_DeviceFeature",
    )
    return bits.value


def capture_errors(h) -> dict[str, int]:
    """The capture status counters that are not zero, by name."""
    u = ueye()
    status = u.UEYE_CAPTURE_STATUS_INFO()
    ok(u.is_CaptureStatus(h, u.IS_CAPTURE_STATUS_INFO_CMD_GET, status, ctypes.sizeof(status)), "is_CaptureStatus")
    detail = status.adwCapStatusCnt_Detail
    names = {int(v): n for n, v in vars(u.UEYE_CAPTURE_STATUS).items() if n.startswith("IS_CAP_STATUS_")}
    return {names.get(i, str(i)): int(detail[i]) for i in range(256) if detail[i]}


def reset_capture_errors(h) -> None:
    u = ueye()
    ok(u.is_CaptureStatus(h, u.IS_CAPTURE_STATUS_INFO_CMD_RESET, None, 0), "is_CaptureStatus reset")


class Sequence:
    """``n`` image memories of the camera's current AOI at ``bits`` a pixel,
    each one slot of a shared-memory segment, in a sequence in queue mode.

    ``slots`` is the segment's (n, slot_bytes) view, ``ids`` the memory ID of
    each slot in order. ``wait`` returns (return code, memory ID, image info)
    for the oldest frame not yet taken; ``unlock`` gives its memory back to
    the driver. On exit, capture is stopped and everything is taken back
    from the driver before the segment is closed."""

    def __init__(self, h, n: int, bits: int) -> None:
        u = ueye()
        self.h = h
        _, _, width, height = aoi(h)
        size = (bits + 7) // 8
        fmt = FrameFormat(
            shape=(height, width),
            dtype=f"<u{size}",
            strides=(width * size, size),
            frame_bytes=width * height * size,
        )
        self.format = fmt
        layout = layout_for(fmt, n)
        self._writer = ImageBuffer.create(
            "b" + uuid.uuid4().hex[:20], capacity_bytes=layout.resolve().total_bytes, max_slots=n
        )
        self._memories: list[tuple[object, int]] = []
        self._queued = False
        try:
            self._writer.configure(layout)
            self.slots = self._writer.slots
            self.slots[:] = 0
            for i in range(n):
                pointer = u.c_mem_p(self.slots.ctypes.data + i * self.slots.strides[0])
                mem_id = ctypes.c_int()
                ok(u.is_SetAllocatedImageMem(h, width, height, bits, pointer, mem_id), f"is_SetAllocatedImageMem {i}")
                self._memories.append((pointer, mem_id.value))
                ok(u.is_AddToSequence(h, pointer, mem_id), f"is_AddToSequence {i}")
            self.ids = [m for _, m in self._memories]
            ok(u.is_ImageQueue(h, u.IS_IMAGE_QUEUE_CMD_INIT, None, 0), "is_ImageQueue init")
            self._queued = True
        except BaseException:
            self.close()
            raise

    def __enter__(self) -> "Sequence":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def frame(self, mem_id: int) -> np.ndarray:
        """The slot ``mem_id`` fills, as an image."""
        slot = self.slots[self.ids.index(mem_id)]
        return slot[: self.format.frame_bytes].view(self.format.dtype).reshape(self.format.shape)

    def pitch(self) -> int:
        u = ueye()
        x, y, bits, pitch = (ctypes.c_int() for _ in range(4))
        pointer, mem_id = self._memories[0]
        ok(u.is_InquireImageMem(self.h, pointer, mem_id, x, y, bits, pitch), "is_InquireImageMem")
        return pitch.value

    def wait(self, timeout_ms: int = 2000):
        u = ueye()
        mem_id = ctypes.c_int()
        memory = ctypes.c_char_p()
        wait = u.IMAGEQUEUEWAITBUFFER()
        wait.timeout = timeout_ms
        # pyueye's fields are pointers to its own ctypes subclasses.
        wait.ppcMem = ctypes.cast(ctypes.pointer(memory), type(wait.ppcMem))
        wait.pnMemId = ctypes.cast(ctypes.pointer(mem_id), type(wait.pnMemId))
        ret = int(u.is_ImageQueue(self.h, u.IS_IMAGE_QUEUE_CMD_WAIT, wait, ctypes.sizeof(wait)))
        if ret != u.IS_SUCCESS:
            return ret, None, None
        info = u.UEYEIMAGEINFO()
        ok(u.is_GetImageInfo(self.h, mem_id, info, ctypes.sizeof(info)), "is_GetImageInfo")
        return ret, mem_id.value, info

    def unlock(self, mem_id: int) -> None:
        u = ueye()
        pointer = self._memories[self.ids.index(mem_id)][0]
        ok(u.is_UnlockSeqBuf(self.h, mem_id, pointer), "is_UnlockSeqBuf")

    def take(self, n: int, timeout_ms: int = 2000, *, until_failure: bool = False):
        """Wait for ``n`` frames, unlocking each after its info is read.
        Returns [(return code, memory ID, info, host time)], one per wait.
        With ``until_failure``, stops after the first wait that fails."""
        taken = []
        for _ in range(n):
            ret, mem_id, info = self.wait(timeout_ms)
            taken.append((ret, mem_id, info, time.perf_counter()))
            if mem_id is not None:
                self.unlock(mem_id)
            elif until_failure:
                break
        return taken

    def live(self) -> None:
        u = ueye()
        ok(u.is_CaptureVideo(self.h, u.IS_DONT_WAIT), "is_CaptureVideo")

    def stop(self) -> None:
        u = ueye()
        u.is_StopLiveVideo(self.h, u.IS_FORCE_VIDEO_STOP)

    def close(self) -> None:
        u = ueye()
        self.stop()
        if self._queued:
            u.is_ImageQueue(self.h, u.IS_IMAGE_QUEUE_CMD_EXIT, None, 0)
            self._queued = False
        u.is_ClearSequence(self.h)
        for pointer, mem_id in self._memories:
            u.is_FreeImageMem(self.h, pointer, mem_id)
        self._memories = []
        # The segment will not close while a view of it is alive.
        self.slots = None
        self._writer.close()


def capture_until_killed(camera: int) -> None:
    """Freerun into a sequence and print "capturing" once frames arrive, then
    go on taking frames until the process is killed. For Y7."""
    with opened(camera) as h:
        set_color_mode(h, ueye().IS_CM_SENSOR_RAW8)
        with Sequence(h, 4, 8) as sequence:
            sequence.live()
            sequence.take(2)
            print("capturing", flush=True)
            while True:
                sequence.take(1)


if __name__ == "__main__":
    capture_until_killed(int(sys.argv[1]))
