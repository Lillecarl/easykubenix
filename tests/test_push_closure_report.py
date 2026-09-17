"""`push_closure_to_store` says how much it is about to copy.

The cache push runs before the git commit that triggers GitOps sync, and it is
the only step that talks to another machine. It reported one line after the
fact, with no path count and no byte count, so a slow link and a hung deploy
looked the same and nothing afterwards said whether 3 paths moved or 300.
easykubenix issue #26.

These test `_closure_size`, which is where the counting is. The copy itself
belongs to nanopynix.
"""

from __future__ import annotations

from typing import Any

from ekn.eval import _closure_size


class _FakeInfo:
    def __init__(self, nar_size: int) -> None:
        self.nar_size = nar_size


class _FakeSource:
    """A store that answers the two read calls `_closure_size` makes."""

    def __init__(self, closures: dict[str, list[str]], sizes: dict[str, int]) -> None:
        self.closures = closures
        self.sizes = sizes
        self.info_calls: list[str] = []

    async def compute_fs_closure(self, path: str) -> list[str]:
        return self.closures[path]

    async def query_path_info(self, path: str) -> Any:
        self.info_calls.append(path)
        return _FakeInfo(self.sizes[path])


async def test_it_counts_the_closure_and_not_the_named_paths() -> None:
    source = _FakeSource(
        closures={"/nix/store/aaa-top": ["/nix/store/aaa-top", "/nix/store/bbb-dep"]},
        sizes={"/nix/store/aaa-top": 1000, "/nix/store/bbb-dep": 2000},
    )

    count, nar_bytes = await _closure_size(source, ["/nix/store/aaa-top"])

    assert count == 2, "one named path whose closure is two"
    assert nar_bytes == 3000


async def test_a_shared_dependency_is_counted_once() -> None:
    """Two roots over one library is the ordinary shape of a deploy closure."""
    shared = "/nix/store/ccc-shared"
    source = _FakeSource(
        closures={
            "/nix/store/aaa-one": ["/nix/store/aaa-one", shared],
            "/nix/store/bbb-two": ["/nix/store/bbb-two", shared],
        },
        sizes={"/nix/store/aaa-one": 10, "/nix/store/bbb-two": 20, shared: 500},
    )

    count, nar_bytes = await _closure_size(source, ["/nix/store/aaa-one", "/nix/store/bbb-two"])

    assert count == 3, "three distinct paths, not four"
    assert nar_bytes == 530, "the shared path is counted once, not twice"
    assert source.info_calls.count(shared) == 1, "and it is asked about once"


async def test_no_paths_is_no_copy_and_no_crash() -> None:
    count, nar_bytes = await _closure_size(_FakeSource({}, {}), [])
    assert (count, nar_bytes) == (0, 0)
