from __future__ import annotations

from pathlib import Path

import nanopynix
import pytest

from ekn.eval import evaluate_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NIX_TEST_FILE = PROJECT_ROOT / "tests/test_kube_value_type.nix"


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class TestKubeValueType:
    async def test_named_list_override_via_mk_named_list(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "namedListOverrideViaMkNamedList")
        assert isinstance(result, dict)
        containers = result["template"]["spec"]["containers"]
        assert containers == [
            {"name": "app", "image": "v2"},
            {"name": "sidecar", "image": "s1"},
        ]

    async def test_plain_list_of_named_things_never_auto_converted(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "plainListOfNamedThingsNeverAutoConverted")
        assert result == {"containers": [{"name": "app", "image": "v1"}]}

    async def test_owner_references_with_duplicate_names_preserved(self) -> None:
        # Regression: an earlier heuristic ("list of attrs with a unique
        # `name` field" auto-detection) silently dropped one of two
        # ownerReferences sharing a `name` across different `kind`s, via
        # listToAttrs. Since nothing here uses mkNamedList, this must stay
        # a plain, untouched list.
        result = await evaluate_file(NIX_TEST_FILE, "ownerReferencesWithDuplicateNamesPreserved")
        assert isinstance(result, dict)
        refs = result["metadata"]["ownerReferences"]
        assert len(refs) == 2
        assert {r["kind"] for r in refs} == {"ConfigMap", "Deployment"}

    async def test_init_containers_override_via_mk_numbered_list(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "initContainersOverrideViaMkNumberedList")
        assert isinstance(result, dict)
        containers = result["initContainers"]
        assert [c["name"] for c in containers] == ["migrate", "wait-for-db"]
        assert containers[0]["image"] == "m2"

    async def test_unmarked_attrs_rejected_against_list(self) -> None:
        # Kubernetes fields have one fixed shape (list XOR map, never
        # interchangeable) -- an ordinary, unmarked attrset colliding with
        # a real list definition must be a hard error, not a silent
        # reinterpretation as "the attrs form of a list".
        with pytest.raises(nanopynix.NixError, match="defined multiple times"):
            await evaluate_file(NIX_TEST_FILE, "unmarkedAttrsRejectedAgainstListThrows")

    async def test_plain_list_passes_through_untouched(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "plainListPassthrough")
        assert result == {"args": ["--foo", "--bar"]}

    async def test_nested_attrs_merge_across_modules_still_works(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "nestedAttrsMergeAcrossModules")
        assert result == {"spec": {"foo": "a", "bar": "b"}}

    async def test_scalars_pass_through(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "scalarsPassthrough")
        assert result == {
            "aBool": True,
            "anInt": 3,
            "aFloat": 1.5,
            "aString": "hello",
            "aNull": None,
        }

    async def test_multi_def_plain_list_concatenates_like_vanilla_listof(self) -> None:
        # Matches plain `types.listOf`'s own (definition-order-dependent,
        # not append-order) merge behavior -- not a namedListOf artifact.
        result = await evaluate_file(NIX_TEST_FILE, "multiDefPlainListConcatenates")
        assert isinstance(result, dict)
        assert sorted(result["args"]) == ["--bar", "--foo"]
