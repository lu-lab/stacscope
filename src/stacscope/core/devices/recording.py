"""Recording: a device writes its own files, under the acquisition's stem.

The Coordinator issues one root filename for the whole acquisition and knows
nothing else about recording. Each device joins that stem to its own directory,
its own name and its own format — DCIMG through the camera's driver here, TIFF
or CSV elsewhere — which is why the engine cannot own the writer.

Sharing the stem is what makes one acquisition's files identifiable as one
acquisition's files, and the device's own name is what keeps them apart.
"""

from pathlib import Path

from stacscope.core.actors import Fault


class Recording:
    """Mixin for a ``TimedDevice`` that records. Put it first:
    ``class Camera(Recording, TimedDevice)``.

    It owns exactly one rule — an empty stem means do not record — so that no
    adapter has to remember it. ``arm`` starts the recording after the device
    itself is prepared but before any trigger can arrive; ``disarm`` closes it
    before the device reaches its safe state, and death closes it too, because
    an unclosed file may be an unreadable one.
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
        """Start writing to ``path``. The file is the device's to create."""

    async def close_recording(self) -> None:
        """Finish the file. Called once per recording that was opened."""

    async def arm(self, root_filename: str, cycles: int) -> None:
        await super().arm(root_filename, cycles)
        path = self.target(root_filename)
        if path is not None:
            await self.open_recording(path)
            self._recording = True

    async def disarm(self) -> None:
        """A file that will not close is reported, never raised.

        Letting it propagate would skip whatever the adapter does after its own
        ``super().disarm()`` — the shutter it closes, the pressure it vents —
        so a full disk could leave hardware running. It is published as a fatal
        fault instead: the data may be truncated, which is the user's to deal
        with, while the hardware still reaches its safe state and the stop is
        still confirmed.
        """
        try:
            if self._recording:
                self._recording = False  # the file is no longer ours, closed or not
                await self.close_recording()
        except Exception as exc:
            self.publish(Fault(self.ref, f"recording may be truncated: {exc!r}", fatal=True))
        await super().disarm()
