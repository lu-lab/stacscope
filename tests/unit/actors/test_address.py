"""Actor addresses: hierarchical paths, which is what lets a ref cross a boundary."""

import asyncio

import pytest

from stacscope.core.actors import Actor, spawn


class Leaf(Actor):
    pass


class Named(Actor):
    def __init__(self, child_names=()):
        super().__init__()
        self.child_names = child_names
        self.child_refs = []

    async def setup(self):
        for name in self.child_names:
            self.child_refs.append(self.spawn_child(Leaf(), name=name))


async def test_top_level_address_is_the_given_name():
    assert spawn(Leaf(), name="coordinator").address == "coordinator"


async def test_child_address_extends_the_parents_path():
    parent = Named(child_names=["camera0"])
    spawn(parent, name="coordinator")
    await asyncio.sleep(0)  # let setup run

    [camera] = parent.child_refs
    assert camera.address == "coordinator/camera0"


async def test_default_names_are_unique_and_readable():
    a, b = spawn(Leaf()), spawn(Leaf())

    assert a.address != b.address
    assert a.address.startswith("Leaf")


async def test_duplicate_sibling_name_is_rejected():
    parent = Named(child_names=["camera0", "camera0"])
    spawn(parent, name="coordinator")
    await parent.stopped.wait()

    assert isinstance(parent.exit_error, ValueError)


@pytest.mark.parametrize("bad", ["", "a/b", "has space", "ünï"])
async def test_names_are_restricted_to_a_portable_charset(bad):
    with pytest.raises(ValueError):
        spawn(Leaf(), name=bad)


async def test_refs_compare_and_hash_by_address():
    ref = spawn(Leaf(), name="solo")

    assert ref == ref
    assert ref != spawn(Leaf(), name="other")
    assert {ref: 1}[ref] == 1
