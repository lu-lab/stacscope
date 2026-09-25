"""Messages for changing a DCAM camera's settings.

A setting is named as the camera names it ("EXPOSURE TIME", "SENSOR MODE"),
and set either to a number or to one of the camera's own labels for its
values ("PROGRESSIVE"). Every request is answered with a PropertyValue
holding what the camera applied, which may be rounded from what was asked,
or with Rejected, carrying the camera's reason.
"""
# For end use and not for commercial purposes.

from dataclasses import dataclass

from stacscope.core.protocol import message


@message
@dataclass(frozen=True, slots=True)
class SetProperty:
    """Set the camera's setting ``name`` to the number ``value``. Replied
    ``PropertyValue`` with what the camera applied, or ``Rejected``."""

    name: str
    value: float


@message
@dataclass(frozen=True, slots=True)
class SetPropertyLabel:
    """Set the camera's setting ``name`` to the value it labels ``label``,
    such as "PROGRESSIVE" for "SENSOR MODE". Replied ``PropertyValue`` or
    ``Rejected``, which names the labels the camera has."""

    name: str
    label: str


@message
@dataclass(frozen=True, slots=True)
class GetProperty:
    """Read the camera's setting ``name``. Replied ``PropertyValue`` or
    ``Rejected``."""

    name: str


@message
@dataclass(frozen=True, slots=True)
class PropertyValue:
    """A setting as the camera holds it: its number, and its label if the
    camera labels its values (``None`` for a plain number, such as an
    exposure time)."""

    name: str
    value: float
    label: str | None
