"""How fast this computer's disk takes a TIFF recording. Run on demand:

    uv run python benchmarks/tiff_write.py DIRECTORY [--shape ROWS COLUMNS]
        [--seconds 30] [--rate FPS] [--keep]

Records synthetic frames through the same TIFF recorder a camera uses, into
a new file in DIRECTORY, for as long as asked, and reports:

- the sustained rate, in frames and megabytes a second;
- the longest the recorder was held up by the disk;
- for --rate, how many frames the camera's buffer must hold so that a hold-up
  that long does not lose a frame.

Not a pass/fail test. The answer depends on the disk, its free space, what
else is writing to it and the size of the frames, so measure on the
instrument's own computer, onto the disk the data will go to, at the
instrument's frame size. Run it for longer than the operating system's write
cache takes to fill (tens of seconds), or the rate will be the cache's, not
the disk's. It writes as fast as the disk takes it, which on a fast disk is
tens of gigabytes in 30 seconds, so check there is room. The file is deleted
afterwards unless --keep is given.

The defaults are the uEye's full frame in RAW10 (1024 x 1280, 16 bits).
"""

import argparse
import asyncio
import math
import sys
import threading
import time
import uuid
from pathlib import Path

import numpy as np

from stacscope.core.actors import ask, spawn
from stacscope.core.buffer import ImageBuffer, Layout, MetaField, Metadata, TimeSource
from stacscope.core.protocol import FinishRecording
from stacscope.core.recording import TIFFRecorder

N_SLOTS = 64
IN_FLIGHT = 2


class TimedRecorder(TIFFRecorder):
    """The camera's recorder, noting when each frame was written."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.started_at = 0.0
        self.times: list[float] = []

    def write_frame(self, pixels, meta, description):
        super().write_frame(pixels, meta, description)
        self.times.append(time.perf_counter())


def produce(writer, recorder, shape, until, done):
    """Publish frames as fast as the recorder takes them, never lapping it:
    the benchmark measures the disk, not a race."""
    room = N_SLOTS - IN_FLIGHT - 1
    pixels = np.zeros(shape, "<u2")
    while time.perf_counter() < until:
        index = writer.published()
        k = index - writer.acquisition_start
        if k - recorder.frames_written >= room:
            time.sleep(0.0002)
            continue
        pixels[0, 0] = k & 0xFFFF
        writer.frames[writer.slot_of(index)][...] = pixels
        writer.stamp(index, device_ticks=k, host_time_ns=time.time_ns())
        writer.publish(1)
    done.append(writer.published() - writer.acquisition_start)


async def run(directory: Path, shape: tuple[int, int], seconds: float) -> tuple[TimedRecorder, Path]:
    name = "bench" + uuid.uuid4().hex[:12]
    writer = ImageBuffer.create(
        name, capacity_bytes=N_SLOTS * shape[0] * shape[1] * 2 + (1 << 20), max_slots=N_SLOTS
    )
    try:
        writer.configure(
            Layout(shape, "<u2", N_SLOTS, in_flight=IN_FLIGHT),
            Metadata(MetaField.DEVICE_TIME | MetaField.HOST_TIME, TimeSource.CAMERA, 1e7),
        )
        acquisition = writer.start_acquisition()
        path = directory / f"tiff_write_{name}.tif"
        recorder = TimedRecorder(name, acquisition, path, source="benchmark")
        ref = spawn(recorder, name="recorder")
        await asyncio.wait(
            [asyncio.ensure_future(recorder.started.wait()), asyncio.ensure_future(recorder.stopped.wait())],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if not recorder.started.is_set():
            raise SystemExit(f"the recorder did not start: {recorder.exit_error!r}")
        recorder.started_at = time.perf_counter()
        done: list[int] = []
        producer = threading.Thread(
            target=produce, args=(writer, recorder, shape, recorder.started_at + seconds, done)
        )
        producer.start()
        await asyncio.to_thread(producer.join)
        await ask(ref, FinishRecording(done[0]), timeout=None)
        await recorder.stopped.wait()
        if recorder.exit_error is not None:
            raise SystemExit(f"the recording failed: {recorder.exit_error!r}")
        return recorder, path
    finally:
        writer.close()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("directory", type=Path, help="an existing directory on the disk to measure")
    parser.add_argument("--shape", type=int, nargs=2, default=(1024, 1280), metavar=("ROWS", "COLUMNS"))
    parser.add_argument("--seconds", type=float, default=30.0, help="how long to record (default 30)")
    parser.add_argument("--rate", type=float, help="the camera's frame rate, to size its buffer for")
    parser.add_argument("--keep", action="store_true", help="keep the file")
    args = parser.parse_args(argv)
    if not args.directory.is_dir():
        parser.error(f"{args.directory} is not an existing directory")

    shape = tuple(args.shape)
    frame_mb = shape[0] * shape[1] * 2 / 1e6
    print(f"Recording {shape[0]} x {shape[1]} frames ({frame_mb:.2f} MB) to {args.directory} for {args.seconds:g} s")
    recorder, path = asyncio.run(run(args.directory, shape, args.seconds))
    try:
        times = np.array(recorder.times)
        n = len(times)
        elapsed = times[-1] - recorder.started_at
        gaps = np.diff(np.concatenate(([recorder.started_at], times)))
        longest = float(gaps.max())
        rate = n / elapsed
        print(f"  {n} frames, {n * frame_mb / 1e3:.1f} GB, in {elapsed:.1f} s")
        print(f"  sustained: {rate:.0f} frames/s, {rate * frame_mb:.0f} MB/s")
        print(f"  frame to frame: median {np.median(gaps) * 1e3:.2f} ms, longest {longest * 1e3:.1f} ms")
        if args.rate:
            if args.rate >= rate:
                print(f"  at {args.rate:g} frames/s: faster than this disk sustains; the recorder would fall behind")
            else:
                # The longest hold-up, at the camera's rate, plus the frames the
                # camera may be writing, plus one for luck.
                slots = math.ceil(longest * args.rate) + IN_FLIGHT + 1
                print(
                    f"  at {args.rate:g} frames/s: give the camera at least {slots} slots ({slots * frame_mb:.0f} MB)"
                )
    finally:
        if not args.keep:
            path.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
