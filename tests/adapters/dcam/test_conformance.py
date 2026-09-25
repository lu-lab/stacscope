"""The DCAM binding against the installed SDK headers: every struct's size and
field offsets, every constant, and every function's name and argument count.

The fake uses the binding's own structs, so it cannot catch a layout wrong in
both; only the headers can. The headers may not be copied into this
repository (the SDK licence), so this test reads them where they are
installed, from the SDK folder named in STACSCOPE_DCAM_SDK (the one holding
inc/dcamapi4.h), and is skipped when that is not set:

    STACSCOPE_DCAM_SDK=<path to dcamsdk4> uv run pytest tests/adapters/dcam

Layouts are computed for 64-bit Windows, the only target: int32 is a C long,
4 bytes, and pointers are 8.
"""
# For end use and not for commercial purposes.

import ctypes
import os
import pathlib
import re

import pytest

from stacscope.adapters.dcam import _api

SDK = os.environ.get("STACSCOPE_DCAM_SDK")
pytestmark = pytest.mark.skipif(not SDK, reason="STACSCOPE_DCAM_SDK is not set")

SIZES = {
    "int32": 4,
    "_ui32": 4,
    "long": 4,
    "int": 4,
    "short": 2,
    "unsigned short": 2,
    "unsigned char": 1,
    "char": 1,
    "double": 8,
    "DCAM_PIXELTYPE": 4,  # an enum
}
HANDLES = {"HDCAM", "HDCAMWAIT", "HDCAMREC"}


def _header(name: str) -> str:
    return (pathlib.Path(SDK) / "inc" / name).read_text(encoding="latin-1")


def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _structs() -> dict[str, list[tuple[str, str, int]]]:
    """Every struct in dcamapi4.h, as (type, field, array length) in order."""
    text = _strip_comments(_header("dcamapi4.h"))
    structs = {}
    for name, body in re.findall(
        r"DCAM_DECLARE_BEGIN\(\s*struct\s*,\s*(\w+)\s*\)\s*\{(.*?)\}\s*DCAM_DECLARE_END", text, re.S
    ):
        fields = []
        for declaration in body.split(";"):
            declaration = " ".join(declaration.split())
            if not declaration:
                continue
            match = re.fullmatch(r"(?:const )?([\w ]+?)\s*(\**)\s*(\w+)(?:\[\s*(\d+)\s*\])?", declaration)
            assert match, f"cannot read {declaration!r} in {name}"
            base, stars, field, length = match.groups()
            fields.append((base + stars, field, int(length or 1)))
        structs[name] = fields
    return structs


def _layout(name: str, structs) -> tuple[int, int, dict[str, tuple[int, int]]]:
    """Size, alignment, and each field's (offset, size), of ``name`` as MSVC
    lays it out."""
    offset, alignment, fields = 0, 1, {}
    for kind, field, length in structs[name]:
        if kind.endswith("*") or kind in HANDLES:
            size = align = 8
        elif kind in SIZES:
            size = align = SIZES[kind]
        else:
            size, align, _ = _layout(kind, structs)
        offset = -(-offset // align) * align
        fields[field] = (offset, size * length)
        offset += size * length
        alignment = max(alignment, align)
    return -(-offset // alignment) * alignment, alignment, fields


BOUND = [
    _api.DCAMAPI_INIT,
    _api.DCAMDEV_OPEN,
    _api.DCAMDEV_CAPABILITY,
    _api.DCAMDEV_STRING,
    _api.DCAMPROP_VALUETEXT,
    _api.DCAMBUF_ATTACH,
    _api.DCAM_TIMESTAMP,
    _api.DCAMCAP_TRANSFERINFO,
    _api.DCAMWAIT_OPEN,
    _api.DCAMWAIT_START,
]


@pytest.mark.parametrize("struct", BOUND, ids=lambda s: s.__name__)
def test_each_struct_matches_the_header(struct):
    """A field at the wrong offset is a value DCAM reads from the wrong
    bytes, or writes over the next field: silently, until a camera behaves
    oddly or the process crashes."""
    structs = _structs()
    size, _, fields = _layout(struct.__name__, structs)

    assert ctypes.sizeof(struct) == size
    assert [name for name, _ in struct._fields_] == [field for _, field, _ in structs[struct.__name__]]
    for name, _ in struct._fields_:
        # Size as well as offset: a last field too wide can hide in padding.
        assert (getattr(struct, name).offset, getattr(struct, name).size) == fields[name], name


CONSTANTS = {
    "SUCCESS": "DCAMERR_SUCCESS",
    "ERR_BUSY": "DCAMERR_BUSY",
    "ERR_ABORT": "DCAMERR_ABORT",
    "ERR_NOTREADY": "DCAMERR_NOTREADY",
    "ERR_TIMEOUT": "DCAMERR_TIMEOUT",
    "ERR_LOSTFRAME": "DCAMERR_LOSTFRAME",
    "ERR_MISSINGFRAME_TROUBLE": "DCAMERR_MISSINGFRAME_TROUBLE",
    "ERR_NOCAMERA": "DCAMERR_NOCAMERA",
    "ERR_NOPROPERTY": "DCAMERR_NOPROPERTY",
    "ERR_NOTSUPPORT": "DCAMERR_NOTSUPPORT",
    "INITOPTION_APIVER_LATEST": "DCAMAPI_INITOPTION_APIVER__LATEST",
    "INITOPTION_ENDMARK": "DCAMAPI_INITOPTION_ENDMARK",
    "CAPDOMAIN_FUNCTION": "DCAMDEV_CAPDOMAIN__FUNCTION",
    "CAPFLAG_FRAMESTAMP": "DCAMDEV_CAPFLAG_FRAMESTAMP",
    "CAPFLAG_TIMESTAMP": "DCAMDEV_CAPFLAG_TIMESTAMP",
    "ATTACHKIND_FRAME": "DCAMBUF_ATTACHKIND_FRAME",
    "ATTACHKIND_TIMESTAMP": "DCAMBUF_ATTACHKIND_TIMESTAMP",
    "ATTACHKIND_FRAMESTAMP": "DCAMBUF_ATTACHKIND_FRAMESTAMP",
    "CAPSTART_SEQUENCE": "DCAMCAP_START_SEQUENCE",
    "CAPEVENT_FRAMEREADY": "DCAMWAIT_CAPEVENT_FRAMEREADY",
    "CAPEVENT_STOPPED": "DCAMWAIT_CAPEVENT_STOPPED",
    "PROPOPTION_SUPPORT": "DCAMPROP_OPTION_SUPPORT",
}


def _enum_values() -> dict[str, int]:
    values = {}
    for header in ("dcamapi4.h", "dcamprop.h"):
        for name, value in re.findall(
            r"^\s*(DCAM\w+)\s*=\s*(-?0x[0-9A-Fa-f]+|-?\d+)\s*,?", _strip_comments(_header(header)), re.M
        ):
            values[name] = _api._signed(int(value, 0) & 0xFFFFFFFF)
    return values


@pytest.mark.parametrize("ours, theirs", CONSTANTS.items(), ids=CONSTANTS.keys())
def test_each_constant_matches_the_header(ours, theirs):
    """Written by hand as signed 32-bit values; one wrong digit, and an
    abort reads as a failure or a lost frame goes unnoticed."""
    assert getattr(_api, ours) == _enum_values()[theirs]


def test_every_error_name_is_the_headers_name_for_its_code():
    values = _enum_values()
    for code, name in _api.ERROR_NAMES.items():
        assert values[name] == code, name


def _declarations() -> dict[str, int]:
    """Each function in dcamapi4.h, with its number of arguments."""
    text = _strip_comments(_header("dcamapi4.h"))
    found = {}
    for name, arguments in re.findall(r"DCAMERR\s+DCAMAPI\s+(\w+)\s*\(([^)]*)\)", text):
        arguments = arguments.strip()
        found[name] = 0 if arguments in ("", "void") else arguments.count(",") + 1
    return found


@pytest.mark.parametrize("function", _api.FUNCTIONS, ids=list(_api.FUNCTIONS))
def test_each_function_is_declared_with_as_many_arguments(function):
    """A function bound with too few arguments passes garbage for the rest."""
    assert len(_api.FUNCTIONS[function]) == _declarations()[function]
