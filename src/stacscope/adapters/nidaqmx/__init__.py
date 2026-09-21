"""NI-DAQmx adapters. Requires the ``nidaqmx`` extra; nothing in ``core``
imports this package."""

from stacscope.adapters.nidaqmx.signal_output import NiSignalOutput

__all__ = ["NiSignalOutput"]
