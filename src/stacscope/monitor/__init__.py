"""A buffer monitor: a devtool for watching and debugging an image buffer.

It runs in a process of its own so it cannot interfere with what it watches,
and it is a pure consumer -- a segment name is the only thing it is told.
"""

from stacscope.monitor.follower import BufferMonitor, State, View

__all__ = ["BufferMonitor", "State", "View"]
