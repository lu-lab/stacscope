"""BufferRecording: a streaming device records through a recorder of its own.

A camera whose driver writes files, like a DCAM camera, records with
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

from pathlib import Path

from stacscope.core.actors import Actor, ActorRef, ChildExited, Fault
from stacscope.core.devices.device import DeviceState
from stacscope.core.devices.recording import Recording
from stacscope.core.protocol import FinishRecording, RecorderStarted, RecordingFinished
from stacscope.core.recording import TIFFRecorder


class BufferRecording(Recording):
    """Mixin for an ``ImageStreaming`` device that records by reading its own
    buffer. List it after ``ImageStreaming``, where ``Recording`` would go;
    listing it without ``ImageStreaming`` raises ``TypeError``.

    ``recorder`` is the recorder's class, ``TIFFRecorder`` unless a device
    replaces it. It is built as ``recorder(buffer_name, acquisition_id,
    path, source=name)`` and started as the device's child. Like
    ``TIFFRecorder``, it must report ``RecorderStarted`` to its parent once
    its file is open, and answer ``FinishRecording(frames)`` with
    ``RecordingFinished(frames written)`` before it stops.

    Everything ``Recording`` does still applies: an empty stem records
    nothing, a stem with no save directory refuses the arm, and
    ``SetSaveDirectory`` moves the files.

    - The arm is answered once the recorder reports it has started (the
      device is ARMING until then), and refused if it does not start.
    - A recorder that ends with an error during an acquisition is reported
      as a fatal ``Fault`` at once.
    - The stop is answered once the recorder reports how many frames it
      wrote (the device is DISARMING until then). A file short of the
      frames published is a fatal ``Fault``, unless that failure was
      already reported.
    - A device that closes while recording stops its recorder, which closes
      the file with what it has written. A recorder already told to finish
      writes every frame published first, and the close waits for it.
    """

    recorder: type[Actor] = TIFFRecorder
    file_suffix: str = ".tif"

    _recorder: ActorRef | None = None
    """The recorder, while it is this device's business: from its launch
    until it reports its last frame, or its failure is reported."""
    _recorder_path: Path | None = None
    _told: int | None = None
    """The frame count the recorder was told to finish with."""

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
        self._recorder = self.spawn_child(recorder, name=f"recorder-{acquisition}")
        self._recorder_path = path
        self._told = None
        # The arm completes when the recorder says its file is open. Capture
        # starting before then is harmless: no trigger comes before Go, and
        # the recorder reads from the acquisition's start in the ring.
        self.hold()

    async def close_recording(self) -> None:
        if self._recorder is None:
            return  # gone, and its failure reported when it happened
        # Every frame this acquisition will have is published by now:
        # ImageStreaming's last pass ran before this.
        self._told = self.writer.published() - self.writer.acquisition_start
        self._recorder.tell(FinishRecording(self._told))
        self.hold()  # answered when the recorder says the file holds them

    async def handle(self, msg: object) -> None:
        match msg:
            case RecorderStarted() if self._from_recorder():
                self.complete_arm()
            case RecordingFinished(frames) if self._from_recorder():
                self._recorder = None  # it stops after answering: its exit is no news
                problem = None
                if frames != self._told:
                    # Checked here rather than trusting the recorder's word, as
                    # DcamCamera checks its DCIMG file: a recorder may be any class.
                    problem = (
                        f"recording may be truncated: {self._recorder_path} holds {frames} "
                        f"of the {self._told} frames published"
                    )
                self.complete_disarm(problem)
            case ChildExited(child, error) if self._recorder is not None and child == self._recorder:
                await self._recorder_exited(error)
            case _:
                await super().handle(msg)

    def _from_recorder(self) -> bool:
        return self._recorder is not None and self.sender == self._recorder

    async def _recorder_exited(self, error: str | None) -> None:
        self._recorder = None
        path, why = self._recorder_path, error or "it stopped"
        match self.state:
            case DeviceState.ARMING:
                await self.fail_arm(f"the recorder for {path} did not start: {why}")
            case DeviceState.DISARMING:
                self.complete_disarm(f"recording may be truncated: the recorder for {path} failed: {why}")
            case DeviceState.ARMED if error is not None:
                # Mid-acquisition: the frames it missed are gone from the ring,
                # so the acquisition cannot be completed. Reported now, so the
                # Coordinator aborts rather than running on unrecorded.
                self.report(Fault(self.ref, f"recording to {path} failed: {error}", fatal=True))

    async def close(self) -> None:
        # The recorder is already gone: a parent's exit stops its children
        # before its teardown, and waits for one told to finish (CM10).
        self._recorder = None
        await super().close()
