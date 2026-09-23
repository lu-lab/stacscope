"""NI-DAQmx adapters. Requires the ``nidaqmx`` extra; nothing in ``core``
imports this package."""

from stacscope.adapters.nidaqmx.signal_output import NiSignalOutput, read_output_config

__all__ = ["NiSignalOutput", "read_output_config"]
