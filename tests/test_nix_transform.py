from __future__ import annotations

from pathlib import Path

import nanopynix
import pytest

from ekn.eval import evaluate_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NIX_TEST_FILE = PROJECT_ROOT / "tests/test_nix_transform.nix"


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class TestNixTransform:
    async def test_the_transform_walks_its_input(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "walksTheInput")
        assert result == {
            "title": "patched: Cilium Metrics",
            "panels": [{"id": 1}, {"id": 2}, {"id": 3}],
            "panelCount": 3,
        }

    async def test_easykubenixs_own_lib_is_in_scope_in_the_sandbox(self) -> None:
        # The sandboxed evaluator reads bare `<nixpkgs/lib>`, so the overlay
        # has to be shipped in for `lib.hashAttrs` to resolve at all.
        result = await evaluate_file(NIX_TEST_FILE, "reachesTheEknOverlay")
        assert isinstance(result, dict)
        assert len(result["hash"]) == 32

    async def test_a_named_list_comes_back_as_a_list(self) -> None:
        # The key becomes `name`, and no `_type` survives -- the result lands
        # in an untyped option, which walks nothing and would ship the marker.
        result = await evaluate_file(NIX_TEST_FILE, "convertsANamedList")
        assert result == {
            "containers": [
                {"name": "app", "image": "v1"},
                {"name": "sidecar", "image": "s1"},
            ]
        }

    async def test_a_numbered_list_comes_back_in_index_order(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "convertsANumberedList")
        assert result == {"args": ["--first", "--second"]}

    async def test_a_marker_that_cannot_be_converted_fails_the_build(self) -> None:
        with pytest.raises(nanopynix.NixError, match="still holds a marker"):
            await evaluate_file(NIX_TEST_FILE, "refusesAnUnconvertibleMarker")

    async def test_the_render_side_is_one_read_file(self) -> None:
        assert await evaluate_file(NIX_TEST_FILE, "isJustAReadFileAfterwards") is True

    async def test_args_reach_the_transform_as_values(self) -> None:
        # A list stays a list. Interpolated as `builtins.toJSON` it would
        # have been `["a","b"]`, which is a syntax error in Nix.
        result = await evaluate_file(NIX_TEST_FILE, "readsItsArgs")
        assert result == {"title": "Cilium Metrics", "panels": [{"id": 1}]}

    async def test_args_may_name_a_store_path(self) -> None:
        # `pkgs.writeText` and not `builtins.toFile`, which refuses a string
        # carrying store context. A real configuration carries chart paths.
        result = await evaluate_file(NIX_TEST_FILE, "carriesAStorePathInArgs")
        assert result == {"isStorePath": True}

    async def test_a_list_of_transforms_applies_left_to_right(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "appliesAListInOrder")
        assert isinstance(result, dict)
        assert result["title"] == "first: Cilium Metrics (prod)"

    async def test_a_transform_that_declares_no_args_is_called_without_them(self) -> None:
        # `{ lib, value }` predates `args`, and Nix rejects a call that
        # passes an attribute the pattern does not declare.
        result = await evaluate_file(NIX_TEST_FILE, "aTransformThatIgnoresArgsStillRuns")
        assert result == {"seen": ["panels", "title"]}
