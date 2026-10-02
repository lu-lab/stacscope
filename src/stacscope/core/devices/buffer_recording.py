"""BufferRecording: a streaming device records through a recorder of its own.

A camera whose driver writes files, like the ORCA-Flash, records with
Recording alone. A camera whose driver cannot lists BufferRecording in its
place:

    class UEyeCamera(ImageStreaming, BufferRecording, TimedDevice)

For each acquisition with a stem, the device starts a recorder, an actor of
its own, which reads the device's image buffer and writes every frame to
<save directory>/<stem>_<device>.tif. The writing happens on the recorder's
own thread, so a slow disk never holds up the camera. At the stop the device
tells the recorder how many frames were published, and the stop is answered
once the file holds them all.

A recorder that cannot keep up, or whose disk fills, is a fatal fault as
soon as it happens, and the acquisition stops. A file that cannot be created
(it exists, or its directory does not) refuses the acquisition.
"""

import asyncio
from pathlib import Path

from stacscope.core.actors import Actor, ChildExited, Fault, describe
from stacscope.core.devices.recording import Recording
from stacscope.core.protocol import FinishRecording
from stacscope.core.recording import TIFFRecorder


class BufferRecording(Recording):
    """Mixin for an ``ImageStreaming`` device that records by reading its own
    buffer. List it after ``ImageStreaming``, where ``Recording`` would go;
    listing it without ``ImageStreaming`` raises ``TypeError``.

    ``recorder`` is the recorder's class, ``TIFFRecorder`` unless a device
    replaces it. It is built as ``recorder(buffer_name, acquisition_id,
    path, source=name)`` and must answer ``FinishRecording`` as
    ``TIFFRecorder`` does, and count ``frames_written``.

    Everything ``Recording`` does still applies: an empty stem records
    nothing, a stem with no save directory refuses the arm, and
    ``SetSaveDirectory`` moves the files.

    - The arm is refused if the recorder does not start.
    - A recorder that ends with an error during an acquisition is published
      as a fatal ``Fault`` at once.
    - At the stop, a file short of the frames published is a fatal
      ``Fault``, unless that failure was already reported.
    - A device that closes while recording stops its recorder, which closes
      the file with what it has written. A recorder already told to finish
      writes every frame published first, so the close waits for it.
    """

    recorder: type[Actor] = TIFFRecorder
    file_suffix: str = ".tif"

    _recorder: Actor | None = None
    _recorder_reported: bool = False

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # Imported here: image_streaming imports this package.
        from stacscope.core.devices.device import TimedDevice
        from stacscope.core.devices.image_streaming import ImageStreaming

        # Only a device is checked: a class mixing roles together for later
        # use names no TimedDevice yet.
        if issubclass(cls, TimedDevice) and not issubclass(cls, ImageStreaming):
            raise TypeError(
                f"{cls.__name__} lists BufferRecording without ImageStreaming: "
                "it records the device's image buffer, so the device needs one"
            )

    async def open_recording(self, path: Path) -> None:
        acquisition = self.writer.acquisition_id
        recorder = self.recorder(self.buffer_name, acquisition, path, source=self.name)
        # Named by acquisition: the last recorder's name is released only once
        # its exit has been reported, which may be after the next arm.
        self.spawn_child(recorder, name=f"recorder-{acquisition}")
        waits = [asyncio.ensure_future(recorder.started.wait()), asyncio.ensure_future(recorder.stopped.wait())]
        try:
            await asyncio.wait(waits, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for wait in waits:
                wait.cancel()
        if not recorder.started.is_set():
            raise RuntimeError(f"the recorder for {path} did not start: {describe(recorder.exit_error)}")
        self._recorder = recorder
        self._recorder_reported = False

    async def close_recording(self) -> None:
        recorder = self._recorder
        if not recorder.stopped.is_set():
            # Every frame this acquisition will have is published by now:
            # ImageStreaming's last pass ran before this.
            published = self.writer.published() - self.writer.acquisition_start
            recorder.ref.tell(FinishRecording(published))
            # Waited on its exit, not asked: a recorder that dies while
            # finishing would leave an ask unanswered.
            await recorder.stopped.wait()
        else:
            published = None
        # Let go only once it has stopped: a camera cancelled during the wait
        # (a shutdown) leaves it to close, which stops it.
        self._recorder = None
        if recorder.exit_error is not None:
            if self._recorder_reported:
                return  # published when it happened; the stop adds nothing
            self._recorder_reported = True
            raise RuntimeError(f"the recorder for {recorder.path} failed: {describe(recorder.exit_error)}")
        if published is not None and recorder.frames_written != published:
            raise RuntimeError(
                f"{recorder.path} holds {recorder.frames_written} of the {published} frames published"
            )

    async def handle(self, msg: object) -> None:
        match msg:
            case ChildExited(child, error) if self._recorder is not None and child == self._recorder.ref:
                # Mid-acquisition: the frames it missed are gone from the ring,
                # so the acquisition cannot be completed. Published now, so the
                # Coordinator aborts rather than running on unrecorded.
                if error is not None and not self._recorder_reported:
                    self._recorder_reported = True
                    reason = f"recording to {self._recorder.path} failed: {error}"
                    self.report(Fault(self.ref, reason, fatal=True))
            case _:
                await super().handle(msg)

    async def close(self) -> None:
        # The recorder is already gone: a parent's exit stops its children
        # before its teardown, waiting for one told to finish (CM10).
        self._recorder = None
        await super().close()
