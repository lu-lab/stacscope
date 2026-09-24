"""B9 — the buffer refuses to run where its memory-ordering argument fails.

Plain stores and loads of ``frames_written`` are correct only on x86-64: TSO
orders them, and an aligned 8-byte access is atomic. 32-bit x86 tears 8-byte
accesses; ARM reorders them. Refusing is explicit where a torn frame would be
silent.
"""

import platform
import sys

import pytest

from stacscope.core.buffer import ImageBuffer, UnsupportedPlatform


@pytest.mark.parametrize("machine", ["AMD64", "x86_64", "amd64"])
def test_x86_64_is_accepted(monkeypatch, name, machine):
    monkeypatch.setattr(platform, "machine", lambda: machine)
    writer = ImageBuffer.create(name, capacity_bytes=4096, max_slots=4)
    ImageBuffer.open(name).close()
    writer.close()


@pytest.mark.parametrize("machine", ["arm64", "aarch64", "armv7l", "i686", "x86", "riscv64", ""])
def test_other_architectures_are_refused_before_anything_is_created(monkeypatch, name, machine):
    monkeypatch.setattr(platform, "machine", lambda: machine)
    with pytest.raises(UnsupportedPlatform):
        ImageBuffer.create(name, capacity_bytes=4096, max_slots=4)
    with pytest.raises(UnsupportedPlatform):
        ImageBuffer.open(name)

    monkeypatch.undo()
    with pytest.raises(FileNotFoundError):
        ImageBuffer.open(name)  # the refused create left nothing behind


def test_a_32_bit_interpreter_on_a_64_bit_machine_is_refused(monkeypatch, name):
    # 32-bit Python on 64-bit Windows reports AMD64, but its 8-byte stores
    # may be two 4-byte stores.
    monkeypatch.setattr(platform, "machine", lambda: "AMD64")
    monkeypatch.setattr(sys, "maxsize", 2**31 - 1)
    with pytest.raises(UnsupportedPlatform):
        ImageBuffer.create(name, capacity_bytes=4096, max_slots=4)
