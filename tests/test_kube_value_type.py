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
    async def test_the_type_uses_the_v2_merge_protocol(self) -> None:
        # `merge.v2` plus a coherent `check`. The flag lets `oneOf` pick a
        # branch without running the module system's coherence check over
        # every leaf, which is the dominant cost in this value tree.
        assert await evaluate_file(NIX_TEST_FILE, "usesV2Merge") is True

    async def test_object_metadata_survives_the_merge(self) -> None:
        assert await evaluate_file(NIX_TEST_FILE, "exposesObjectMetadata") is True

    async def test_named_list_metadata_is_one_entry_per_element(self) -> None:
        # The value is a list, so its metadata is a list too, in the same
        # order. Two plain entries and a marker patching one of them give two.
        assert await evaluate_file(NIX_TEST_FILE, "exposesNamedListMetadata") is True

    async def test_numbered_list_metadata_is_one_entry_per_element(self) -> None:
        assert await evaluate_file(NIX_TEST_FILE, "exposesNumberedListMetadata") is True

    async def test_a_whole_value_can_be_null(self) -> None:
        # This type carries its own null branch, because `types.nullOr` is
        # still legacy and would drop the metadata of everything below it.
        assert await evaluate_file(NIX_TEST_FILE, "topLevelNull") is None

    async def test_mk_if_exists_works_inside_an_object(self) -> None:
        # An object's fields are a `conditionalAttrsOf`, so a patch can
        # condition on a field the same way it conditions on an object.
        result = await evaluate_file(NIX_TEST_FILE, "conditionalFieldInsideObject")
        assert result == {"spec": {"replicas": 3}}

    async def test_null_and_a_value_cannot_both_define_one_field(self) -> None:
        with pytest.raises(nanopynix.NixError, match="is neither a value of type"):
            await evaluate_file(NIX_TEST_FILE, "mixedNullAndValueThrows")

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
        #
        # Under the v2 merge protocol `oneOf` reports this itself, as "no one
        # branch accepts every definition". Before v2 it fell through to the
        # module system's own "defined multiple times". Same rejection, and
        # the newer message names both candidate types.
        with pytest.raises(nanopynix.NixError, match="is neither a value of type"):
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

    async def test_lone_mk_named_list_becomes_a_list(self) -> None:
        # A marked attrset with no plain list at the same path must still
        # become a real list. `types.oneOf` is a left fold of `either`, so an
        # `attrsOf` branch that accepts any attrset would swallow this
        # definition and leave `_type` in the output.
        result = await evaluate_file(NIX_TEST_FILE, "loneMkNamedListBecomesList")
        assert result == {"containers": [{"name": "main", "image": "v1"}]}

    async def test_lone_mk_numbered_list_becomes_a_list(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "loneMkNumberedListBecomesList")
        assert result == {"initContainers": [{"image": "first"}, {"image": "second"}]}

    async def test_name_injected_for_marker_only_entry(self) -> None:
        # An entry that only the marker introduces must still carry its
        # `name`; the key is the only source of it. Without this a container
        # added purely by override reaches the cluster with no name.
        result = await evaluate_file(NIX_TEST_FILE, "nameInjectedForNewEntry")
        assert result == {
            "containers": [
                {"name": "main", "image": "base"},
                {"name": "sidecar", "image": "s"},
            ]
        }

    async def test_key_wins_over_inner_name(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "keyWinsOverInnerName")
        assert result == {
            "containers": [
                {"name": "a", "image": "A"},
                {"name": "b", "image": "B"},
            ]
        }

    async def test_order_preserved_under_named_override(self) -> None:
        # Patching one entry must not reorder the list. An alphabetical sort
        # would put "alpha" first; env vars resolve `$(VAR)` positionally.
        result = await evaluate_file(NIX_TEST_FILE, "orderPreservedUnderNamedOverride")
        assert isinstance(result, dict)
        assert [c["name"] for c in result["containers"]] == ["zeta", "alpha"]
        assert result["containers"][0]["image"] == "1"

    async def test_new_names_append_after_plain_entries(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "newNamesAppendAfterPlainEntries")
        assert isinstance(result, dict)
        assert [c["name"] for c in result["containers"]] == ["zeta", "alpha", "beta"]

    async def test_mk_merge_of_two_marked_lists(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "mkMergeOfTwoMarkedLists")
        assert result == {
            "containers": [
                {"name": "a", "image": "x"},
                {"name": "b", "image": "y"},
            ]
        }

    async def test_mk_merge_of_plain_list_and_marked(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "mkMergeOfPlainListAndMarked")
        assert result == {
            "containers": [
                {"name": "a", "image": "x"},
                {"name": "b", "image": "y"},
            ]
        }

    async def test_mk_force_on_whole_marked_list(self) -> None:
        # mkForce drops the plain list definition, leaving only the marked
        # one -- which must still resolve to a real list.
        result = await evaluate_file(NIX_TEST_FILE, "mkForceWholeMarkedList")
        assert result == {"containers": [{"name": "b", "image": "y"}]}

    async def test_mk_if_false_marked_list_is_dropped(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "mkIfFalseMarkedListIsDropped")
        assert result == {"containers": [{"name": "a", "image": "x"}]}

    async def test_mk_if_true_marked_list_applies(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "mkIfTrueMarkedListApplies")
        assert result == {"containers": [{"name": "a", "image": "y"}]}

    async def test_mk_order_on_plain_lists(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "mkOrderOnPlainLists")
        assert isinstance(result, dict)
        assert [c["name"] for c in result["containers"]] == ["aaa", "mmm", "zzz"]

    async def test_numbered_override_of_scalar_list(self) -> None:
        # Index addressing is well-defined for scalars, so `args`/`command`
        # are overridable by index.
        result = await evaluate_file(NIX_TEST_FILE, "numberedOverrideOfScalarList")
        assert result == {"args": ["--a", "--B"]}

    async def test_numbered_sparse_index_appends(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "numberedSparseIndexAppends")
        assert result == {"containers": [{"name": "a"}, {"name": "f"}]}

    async def test_nested_named_list_override(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "nestedNamedListOverride")
        assert result == {"containers": [{"name": "a", "env": [{"name": "V", "value": "2"}]}]}

    async def test_mixed_named_and_numbered_markers_rejected(self) -> None:
        # Previously the named branch won in silence and left the other
        # marker's literal `true` behind as a list element.
        with pytest.raises(nanopynix.NixError, match="both an mkNamedList"):
            await evaluate_file(NIX_TEST_FILE, "mixedNamedAndNumberedThrows")

    async def test_mk_order_on_named_entry_rejected(self) -> None:
        # A named list takes its order from the keys, so mkBefore on an entry
        # cannot work. Refuse it rather than discard it silently.
        with pytest.raises(nanopynix.NixError, match="mkBefore/mkAfter/mkOrder"):
            await evaluate_file(NIX_TEST_FILE, "mkOrderOnNamedEntryThrows")

    async def test_mk_named_list_rejects_non_attrs_input(self) -> None:
        with pytest.raises(nanopynix.NixError, match="Input must be an attribute set"):
            await evaluate_file(NIX_TEST_FILE, "mkNamedListRejectsNonAttrsInput")

    async def test_mk_named_list_rejects_non_attrs_values(self) -> None:
        with pytest.raises(nanopynix.NixError, match="must themselves be attribute sets"):
            await evaluate_file(NIX_TEST_FILE, "mkNamedListRejectsNonAttrsValues")

    async def test_mk_numbered_list_rejects_non_int_keys(self) -> None:
        with pytest.raises(nanopynix.NixError, match="must be integer strings"):
            await evaluate_file(NIX_TEST_FILE, "mkNumberedListRejectsNonIntKeys")
