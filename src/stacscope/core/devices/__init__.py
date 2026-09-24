"""The participant side of the device protocol. No hardware.

A device is a TimedDevice with roles mixed in. There are three:

    SoftwareTimed   runs a routine on the clock during the acquisition
    ImageStreaming  fills a shared-memory image buffer, from a frame capture
    Recording       writes the device's own file for each acquisition

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
writes the file, such as the ORCA-Flash and its DCIMG recorder, that gives:

    class OrcaFlash(ImageStreaming, Recording, Adapter)

    arm:     lay out buffer, attach frame memory, arm camera,
             open file, start capture
    disarm:  stop capture, publish last frames, close file,
             disarm camera, detach frame memory

The file opens once the driver has the frame memory and before any frame
can be captured, and closes once capture has stopped and before the memory
is taken back. That is what DCAM requires, and what a recorder that reads
the buffer needs too: it sees every frame, including the last ones
published at the stop.

The order is not checked. Python will accept the roles in any order and each
will do its part where it is listed, so a camera listing Recording before
ImageStreaming would open its file after capture starts and close it before
capture stops. Choosing the order is choosing when each part happens.

If an arm fails, each role undoes its own part before the error goes on:
Recording disarms what armed after it, ImageStreaming detaches the frame
memory. A failed arm is never followed by a disarm, so nothing else will.

If a disarm fails, everything listed after the failing step still disarms,
so a file is still closed and the hardware still reaches its safe state. A
capture that will not stop keeps its frame memory until the device closes,
since its driver may still be writing into it. A file that will not close is
reported as a fatal fault and does not stop the disarm. Any other failure
ends the device, which is how a stop that was not achieved is reported.
"""

from stacscope.core.devices.device import DeviceState, NotReady, TimedDevice
from stacscope.core.devices.image_streaming import ImageStreaming
from stacscope.core.devices.recording import Recording
from stacscope.core.devices.software_timed import SoftwareTimed

__all__ = [
    "DeviceState",
    "ImageStreaming",
    "NotReady",
    "Recording",
    "SoftwareTimed",
    "TimedDevice",
]
