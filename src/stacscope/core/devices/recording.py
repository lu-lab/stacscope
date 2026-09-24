"""Recording: a device writes its own files, under the acquisition's stem.

The Coordinator issues one root filename for the whole acquisition and knows
nothing else about recording. Each device joins that stem to its own directory,
its own name and its own format — DCIMG through the camera's driver here, TIFF
or CSV elsewhere — which is why the engine cannot own the writer.

Sharing the stem is what makes one acquisition's files identifiable as one
acquisition's files, and the device's own name is what keeps them apart.

Where the file opens and closes, relative to the rest of the device, is set by
where Recording is listed among the device's base classes. A camera that
streams images lists it after ImageStreaming:

    class OrcaFlash(ImageStreaming, Recording, TimedDevice)

so the file opens after the frame memory is attached and before capture
starts, and closes after capture stops and before the memory is given back.
The package's own description (stacscope.core.devices) explains the rule.
"""

from pathlib import Path

from stacscope.core.actors import Fault


class Recording:
    """Mixin for a ``TimedDevice`` that records.

    **Where it goes in the bases decides when the file opens.** Like every
    role, it wraps whatever is listed after it:

    - ``arm``: everything after it arms, *then* the file opens.
    - ``disarm``: the file closes, *then* everything after it disarms.

    So the file is open for as long as everything listed after it is armed.
    With ``class OrcaFlash(ImageStreaming, Recording, TimedDevice)`` a
    camera arms and disarms in this order::

        arm:     attach frame memory, arm camera, OPEN FILE, start capture
        disarm:  stop capture, publish last frames, CLOSE FILE,
                 disarm camera, detach frame memory

    That is the order DCAM's DCIMG recorder needs, and a recorder that reads
    the buffer. Listed before ``ImageStreaming``, the file would open after
    capture starts and close before it stops. See ``stacscope.core.devices``
    for all the roles together.

    It owns exactly one rule — an empty stem means do not record — so that no
    adapter has to remember it. A file that will not open fails the arm, after
    disarming everything that armed after it. A file that will not close is
    reported as a fatal ``Fault``, never raised, and death closes the file
    too, because an unclosed file may be an unreadable one.
    """

    save_directory: Path = Path()
    file_suffix: str = ""

    _recording: bool = False

    def target(self, root_filename: str) -> Path | None:
        """This acquisition's file for this device, or ``None`` for an
        acquisition that is not being saved. Override to name it differently —
        returning ``None`` still means record nothing."""
        if not root_filename:
            return None
        return self.save_directory / f"{root_filename}_{self.name}{self.file_suffix}"

    @property
    def name(self) -> str:
        """The device's registered name, which config has already made unique."""
        return self.ref.address.rsplit("/", 1)[-1]

    async def open_recording(self, path: Path) -> None:
        """Start writing to ``path``. The file is the device's to create.
        Called once everything listed after ``Recording`` in the device's
        bases has armed; raise to fail the arm."""

    async def close_recording(self) -> None:
        """Finish the file. Called once per recording that was opened, before
        anything listed after ``Recording`` disarms."""

    async def arm(self, root_filename: str, cycles: int) -> None:
        # Everything after this class in the MRO arms first, so the file opens
        # at a point the device picked by where it listed Recording. The mixin
        # once fixed that point for every device; DCAM needs it between attach
        # and start, inside ImageStreaming's arm, which a fixed point could not
        # express (user, 2026-09-24).
        await super().arm(root_filename, cycles)
        path = self.target(root_filename)
        if path is None:
            return
        try:
            await self.open_recording(path)
        except BaseException:
            # A failed arm is never followed by a disarm, and what armed below
            # may have opened a shutter or started a pump. If this disarm fails
            # too, that error is raised, with the open's chained to it.
            await super().disarm()
            raise
        self._recording = True

    async def disarm(self) -> None:
        """Closes the file, then disarms everything listed after it. A file
        that will not close is published as a fatal ``Fault``, not raised."""
        # Not raised: letting it propagate would skip whatever the adapter does
        # after its own super().disarm() — the shutter it closes, the pressure
        # it vents — so a full disk could leave hardware running. The data may
        # be truncated, which is the user's to deal with, while the hardware
        # still reaches its safe state and the stop is still confirmed.
        try:
            if self._recording:
                self._recording = False  # the file is no longer ours, closed or not
                await self.close_recording()
        except Exception as exc:
            self.publish(Fault(self.ref, f"recording may be truncated: {exc!r}", fatal=True))
        await super().disarm()
