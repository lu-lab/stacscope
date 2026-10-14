# STACScope

**S**ynchronous **T**iming, **A**synchronous **C**ontrol & **S**treaming for
microscopy — an actor-based hardware control engine meant to coordinate multi-component 
instruments with high data throughput and mixed analog/digital signaling. It began as a Python
reimplementation of the control software for a custom light-sheet
microscope[^1] and is built to generalize to other instruments. 
The original architecture is described in detail in Alex Calhoun's PhD thesis[^2].

Currently supports coordinating device actors across threads, processes, and WebSockets.
Streaming buffers are tested in Windows and Linux.


[^1]:Calhoun, W.A., Moon, S., Peng, L., Lu, H. _High-speed functional imaging with a 
microfluidics-compatible open-top light-sheet microscope enabled by model predictive 
control of a tunable lens_, preprint at <https://www.biorxiv.org/content/10.1101/2025.07.23.666439v1> (2025)
[^2]:Calhoun, W.A. _Light sheet microscopy and software tools for functional imaging 
of the C. elegans nervous system_, Georgia Tech and Emory University, 2026


