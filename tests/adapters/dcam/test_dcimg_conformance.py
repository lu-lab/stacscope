"""The DCIMG reader (dcimg_reader.py) against the installed DCIMG SDK header:
every struct's size and field offsets, every constant, and every function's
name and argument count.

The reader checks what the camera recorded, so a layout wrong in it would
report a good file as bad, or a bad one as good. The header may not be copied
into this repository (the SDK licence), so this reads it where it is
installed, from the SDK folder named in STACSCOPE_DCIMG_SDK (the one holding
inc/dcimgapi.h), and is skipped when that is not set:

    STACSCOPE_DCIMG_SDK=<path to dcimgsdk> uv run pytest tests/adapters/dcam
"""
# For end use and not for commercial purposes.

import ctypes
import os
import pathlib
import re

import dcimg_reader
import pytest
from test_conformance import layout, strip_comments, structs_in

SDK = os.environ.get("STACSCOPE_DCIMG_SDK")
pytestmark = pytest.mark.skipif(not SDK, reason="STACSCOPE_DCIMG_SDK is not set")


def _header() -> str:
    return (pathlib.Path(SDK) / "inc" / "dcimgapi.h").read_text(encoding="latin-1")


@pytest.mark.parametrize(
    "struct",
    [
        dcimg_reader.DCIMG_INIT,
        dcimg_reader.DCIMG_OPENA,
        dcimg_reader.DCIMG_TIMESTAMP,
        dcimg_reader.DCIMG_FRAME,
    ],
    ids=lambda s: s.__name__,
)
def test_each_struct_matches_the_header(struct):
    structs = structs_in(_header(), "DCIMG")
    size, _, fields = layout(struct.__name__, structs)

    assert ctypes.sizeof(struct) == size
    assert [name for name, _ in struct._fields_] == [field for _, field, _ in structs[struct.__name__]]
    for name, _ in struct._fields_:
        assert (getattr(struct, name).offset, getattr(struct, name).size) == fields[name], name


CONSTANTS = {
    "SUCCESS": "DCIMG_ERR_SUCCESS",
    "CODEPAGE_UTF8": "DCIMG_CODEPAGE__UTF8",
    "IDPARAML_NUMBEROF_TOTALFRAME": "DCIMG_IDPARAML_NUMBEROF_TOTALFRAME",
    "IDPARAML_NUMBEROF_SESSION": "DCIMG_IDPARAML_NUMBEROF_SESSION",
    "IDPARAML_IMAGE_WIDTH": "DCIMG_IDPARAML_IMAGE_WIDTH",
    "IDPARAML_IMAGE_HEIGHT": "DCIMG_IDPARAML_IMAGE_HEIGHT",
    "IDPARAML_IMAGE_ROWBYTES": "DCIMG_IDPARAML_IMAGE_ROWBYTES",
    "IDPARAML_IMAGE_PIXELTYPE": "DCIMG_IDPARAML_IMAGE_PIXELTYPE",
    "PIXELTYPE_MONO8": "DCIMG_PIXELTYPE_MONO8",
    "PIXELTYPE_MONO16": "DCIMG_PIXELTYPE_MONO16",
}


@pytest.mark.parametrize("ours, theirs", CONSTANTS.items(), ids=CONSTANTS.keys())
def test_each_constant_matches_the_header(ours, theirs):
    values = {
        name: int(value, 0)
        for name, value in re.findall(
            r"^\s*(DCIMG\w+)\s*=\s*(0x[0-9A-Fa-f]+|\d+)\s*,?", strip_comments(_header()), re.M
        )
    }
    assert getattr(dcimg_reader, ours) == values[theirs]


@pytest.mark.parametrize("function", dcimg_reader.FUNCTIONS, ids=list(dcimg_reader.FUNCTIONS))
def test_each_function_is_declared_with_as_many_arguments(function):
    declared = {
        name: 0 if arguments.strip() in ("", "void") else arguments.count(",") + 1
        for name, arguments in re.findall(
            r"DCIMG_ERR\s+DCIMGAPI\s+(\w+)\s*\(([^)]*)\)", strip_comments(_header())
        )
    }
    assert len(dcimg_reader.FUNCTIONS[function]) == declared[function]
