"""The participant side of the device protocol. No hardware.

A device is a TimedDevice with roles mixed in. There are three:

    SoftwareTimed   runs a routine on the clock during the acquisition
    ImageStreaming  fills a shared-memory image buffer, from a frame capture
    Recording       writes the device's own file for each acquisition

BufferRecording is a kind of Recording, for a camera whose driver cannot
write files: for each acquisition it starts a recorder that reads the
camera's image buffer and writes a TIFF file. It goes where Recording goes.

The device's own class, the adapter, is a TimedDevice subclass that says what
arming and disarming its hardware means.


Composing a device: the order of the base classes is the order of the calls

Each role wraps every class listed after it. Arming runs down the list of
bases and disarming runs back up it, so a role listed earlier acts before a
later one on the way in and after it on the way out. What each role does
around the classes after it:

    SoftwareTimed   arm:     (nothing; its routine starts on a phase)
                    disarm:  cancel the routine, then the rest

    ImageStreaming  arm:     lay out the buffer, attach the frame memory,
                             then the rest, then start capture
                    disarm:  stop capture, publish the last frames,
                             then the rest, then detach the frame memory

    Recording       arm:     the rest, then open the file
                    disarm:  close the file, then the rest

List them in this order, then the adapter's own class last:

    class Device(SoftwareTimed, ImageStreaming, Recording, Adapter)

leaving out any role the device does not have. For a camera whose driver
writes the file, such as a DCAM camera and its DCIMG recorder, that gives:

    class DcamCamera(ImageStreaming, Recording, Adapter)

    arm:     lay out buffer, attach frame memory, arm camera,
             open file, start capture
    disarm:  stop capture, publish last frames, close file,
             disarm camera, detach frame memory

The file opens once the driver has the frame memory and before any frame
can be captured, and closes once capture has stopped and before the memory
is taken back. That is what DCAM requires, and what a recorder that reads
the buffer needs too: it sees every frame, including the last ones
published at the stop.

The order is checked when the device's class is defined: roles out of this
order, or listed after the adapter's class, raise TypeError. A camera
listing Recording before ImageStreaming would otherwise open its file after
capture starts and close it before capture stops, and a role listed after
the adapter would never be called at all.

If an arm fails, the roles undo what they can before the error goes on,
because a failed arm is never followed by a disarm: Recording, when its file
will not open, disarms what armed after it; ImageStreaming detaches the frame
memory, and when capture fails to start, first disarms what armed after it.
If that undoing disarm fails too, the device has failed in both directions
and nothing is known about its hardware: the arm is refused, and then the
device dies of the arm's error, with the disarm's added as a note.

If a disarm fails, everything listed after the failing step still disarms,
so a file is still closed and the hardware still reaches its safe state. A
capture that will not stop keeps its frame memory until the device closes,
since its driver may still be writing into it. A file that will not close is
reported as a fatal fault and does not stop the disarm. Any other failure
ends the device, which is how a stop that was not achieved is reported.
"""

from stacscope.core.devices.buffer_recording import BufferRecording
from stacscope.core.devices.device import DeviceState, NotReady, PropertyRefused, TimedDevice
from stacscope.core.devices.image_streaming import ImageStreaming
from stacscope.core.devices.recording import Recording
from stacscope.core.devices.software_timed import SoftwareTimed

__all__ = [
    "BufferRecording",
    "DeviceState",
    "ImageStreaming",
    "NotReady",
    "PropertyRefused",
    "Recording",
    "SoftwareTimed",
    "TimedDevice",
]
