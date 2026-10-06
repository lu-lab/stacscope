"""A read-only map, for messages that carry a table."""


def _refuse(self, *args, **kwargs):
    raise TypeError(f"a {type(self).__name__} cannot be changed")


class ReadOnlyMap(dict):
    """A dict that refuses every change after it is built.

    Built from any mapping or iterable of pairs, as a copy, so the sender may
    go on changing its own. Reads, compares, pickles and packs as a dict;
    ``|`` returns a new, plain dict. A message holding a map holds one of
    these, because in one process every subscriber is handed the same
    message object.
    """

    # dict's own __init__ fills it without calling the methods below.
    __setitem__ = __delitem__ = __ior__ = _refuse
    clear = pop = popitem = setdefault = update = _refuse

    def __reduce__(self):
        # The default rebuilds a dict subclass item by item through
        # __setitem__, which refuses.
        return (type(self), (dict(self),))

    def __repr__(self) -> str:
        return f"{type(self).__name__}({dict.__repr__(self)})"
