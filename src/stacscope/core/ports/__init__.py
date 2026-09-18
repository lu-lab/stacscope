"""Abstract ports the core depends on. Adapters implement them."""

from stacscope.core.ports.clock import Clock, MonotonicClock

__all__ = ["Clock", "MonotonicClock"]
