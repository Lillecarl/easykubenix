"""`push_closure_to_store` says how much it is about to copy.

The cache push runs before the git commit that triggers GitOps sync, and it is
the only step that talks to another machine. It reported one line after the
fact, with no path count and no byte count, so a slow link and a hung deploy
looked the same and nothing afterwards said whether 3 paths moved or 300.
easykubenix issue #26.

These test `_closure_size`, which is where the counting is. The copy itself
belongs to huggorm.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from ekn.eval import UnrealisedPathsError, _closure_size, _refuse_unrealised
from ekn.nix import store_path

if TYPE_CHECKING:
    from huggorm import StorePath


def _p(letter: str, name: str) -> str:
    """A store path that parses: a 32-character hash, then the name."""
    return f"/nix/store/{letter * 32}-{name}"


def _full(path: StorePath) -> str:
    return f"/nix/store/{path.to_string()}"


class _FakeInfo:
    def __init__(self, nar_size: int) -> None:
        self._nar_size = nar_size

    def nar_size(self) -> int:
        return self._nar_size


class _FakeSource:
    """A store that answers the two read calls `_closure_size` makes."""

    def __init__(self, closures: dict[str, list[str]], sizes: dict[str, int]) -> None:
        self.closures = closures
        self.sizes = sizes
        self.info_calls: list[str] = []

    async def compute_fs_closure(self, paths: list[StorePath]) -> list[StorePath]:
        # One closure for the whole set, each member once, as libstore has it.
        members = dict.fromkeys(member for path in paths for member in self.closures[_full(path)])
        return [store_path(member) for member in members]

    async def query_path_info(self, path: StorePath) -> Any:
        self.info_calls.append(_full(path))
        return _FakeInfo(self.sizes[_full(path)])


async def test_it_counts_the_closure_and_not_the_named_paths() -> None:
    source = _FakeSource(
        closures={_p("a", "top"): [_p("a", "top"), _p("b", "dep")]},
        sizes={_p("a", "top"): 1000, _p("b", "dep"): 2000},
    )

    count, nar_bytes = await _closure_size(source, [_p("a", "top")])

    assert count == 2, "one named path whose closure is two"
    assert nar_bytes == 3000


async def test_a_shared_dependency_is_counted_once() -> None:
    """Two roots over one library is the ordinary shape of a deploy closure."""
    shared = _p("c", "shared")
    source = _FakeSource(
        closures={
            _p("a", "one"): [_p("a", "one"), shared],
            _p("b", "two"): [_p("b", "two"), shared],
        },
        sizes={_p("a", "one"): 10, _p("b", "two"): 20, shared: 500},
    )

    count, nar_bytes = await _closure_size(source, [_p("a", "one"), _p("b", "two")])

    assert count == 3, "three distinct paths, not four"
    assert nar_bytes == 530, "the shared path is counted once, not twice"
    assert source.info_calls.count(shared) == 1, "and it is asked about once"


async def test_no_paths_is_no_copy_and_no_crash() -> None:
    count, nar_bytes = await _closure_size(_FakeSource({}, {}), [])
    assert (count, nar_bytes) == (0, 0)


class TestRefusingAnUnrealisedPush:
    """A path the manifest names as text is not always on this machine.

    nixkube's `discardStringContext` strips Nix string context from every
    `nixkube/discard` resource, so building the manifest does not realise
    what those resources name -- and the node and pynixd environments are
    exactly those resources. Pushing the rest and reporting success is the
    failure this refuses.
    """

    class _Store:
        def __init__(self, valid: set[str]) -> None:
            self.valid = valid

        async def is_valid_path(self, path: StorePath) -> bool:
            return _full(path) in self.valid

    async def test_every_path_present_is_no_refusal(self) -> None:
        paths = [_p("a", "one"), _p("b", "two")]
        await _refuse_unrealised(self._Store(set(paths)), paths)

    async def test_a_missing_path_names_itself_and_the_option(self) -> None:
        paths = [_p("a", "one"), _p("b", "nodeEnv")]

        with pytest.raises(UnrealisedPathsError) as exc:
            await _refuse_unrealised(self._Store({_p("a", "one")}), paths)

        message = str(exc.value)
        assert _p("b", "nodeEnv") in message
        assert _p("a", "one") not in message, "a path that is here is not the reader's problem"
        assert "nixkube.discardStringContext" in message
