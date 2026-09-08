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


class TestWholeListPriorities:
    """Priorities on a whole list definition.

    None of this is the type's own behaviour, and that is what these pin. The
    module system runs `filterOverrides` over the definitions and only then
    calls `namedListOf.merge`, so a priority decides which definitions the type
    ever sees. A Kubernetes list has to obey the same rules as any other
    option.
    """

    async def test_mk_force_replaces_a_plain_list(self) -> None:
        # The plain case. Every other force in this file is applied to a
        # *marked* definition instead.
        result = await evaluate_file(NIX_TEST_FILE, "mkForcePlainListOverPlain")
        assert result == {"args": ["--b"]}

    async def test_mk_default_loses_to_an_ordinary_definition(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "mkDefaultPlainListLoses")
        assert result == {"args": ["--chosen"]}

    async def test_mk_default_applies_when_it_is_the_only_definition(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "mkDefaultPlainListAloneApplies")
        assert result == {"args": ["--fallback"]}

    async def test_mk_force_of_an_empty_list_empties_the_field(self) -> None:
        # `mkForce []` is how a module empties a list. It must stay a real
        # empty list rather than becoming an absent field.
        result = await evaluate_file(NIX_TEST_FILE, "mkForceEmptiesList")
        assert result == {"args": []}

    async def test_two_forces_at_one_priority_concatenate(self) -> None:
        # `mkForce` means "beats every lower priority", not "the last word".
        # Two survivors then concatenate, the way any two ordinary list
        # definitions do. The order is the definition-collection order, which
        # `TestBehavesLikeListOf` shows is plain `listOf`'s and not this
        # type's.
        result = await evaluate_file(NIX_TEST_FILE, "twoMkForcesConcatenate")
        assert result == {"args": ["--b", "--a"]}

    async def test_a_lower_numeric_priority_wins(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "mkOverridePriorityOrder")
        assert result == {"args": ["--winner"]}

    async def test_forcing_a_list_silently_discards_a_named_patch(self) -> None:
        """The precedence rule that ties whole-list and per-entry priorities together.

        A priority resolves BEFORE the type looks for a marker.
        `filterOverrides` drops the ordinary `mkNamedList` definition, so
        `anyNamed` is false and the plain list branch runs. The patch is
        discarded without a word.

        That is `mkForce`'s meaning rather than a defect, but it is invisible:
        a module that forces a list also silences every named patch of it.
        """
        result = await evaluate_file(NIX_TEST_FILE, "mkForceDiscardsLaterNamedPatch")
        assert result == {"containers": [{"name": "replacement", "image": "v9"}]}


class TestBehavesLikeListOf:
    """An unmarked list must behave exactly like `types.listOf`.

    Each case gives the same definitions to `kubeValueType` and to a control
    option typed `types.listOf types.raw`, then asserts the two agree. `raw`
    because `raw.merge` is `mergeOneOption` and `listOf` hands each element
    exactly one definition, so the control is identity on plain data while
    still running `dischargeProperties` and `filterOverrides` per element.

    Asserting equality rather than a literal is deliberate. It states the
    property the user actually cares about -- "this is a normal NixOS list"
    -- and a divergence prints as a diff.
    """

    @staticmethod
    async def _case(name: str) -> tuple[object, object]:
        result = await evaluate_file(NIX_TEST_FILE, "sameAsListOf")
        assert isinstance(result, dict)
        case = result[name]
        assert isinstance(case, dict)
        return case["kube"], case["control"]

    @pytest.mark.parametrize(
        "case",
        [
            "concatenates",
            "forced",
            "defaulted",
            "ordered",
            "oneDefinitionSwitchedOff",
            "everyDefinitionSwitchedOff",
            "emptyDefinition",
            "elementSwitchedOff",
            "elementForced",
        ],
    )
    async def test_matches_plain_list_of(self, case: str) -> None:
        kube, control = await self._case(case)
        assert kube == control

    async def test_definitions_concatenate_in_collection_order(self) -> None:
        # Pinning the actual value once, so "they agree" cannot pass by both
        # sides being wrong in the same way. The reversal is nixpkgs' own:
        # definitions from separate modules arrive in this order.
        kube, control = await self._case("concatenates")
        assert kube == ["--b", "--a"]
        assert control == ["--b", "--a"]

    async def test_order_properties_sort_by_priority(self) -> None:
        kube, _ = await self._case("ordered")
        # mkOrder 400, mkBefore (500), plain (1000), mkAfter (1500).
        assert kube == ["--late", "--first", "--middle", "--last"]

    async def test_a_property_on_one_element_is_discharged(self) -> None:
        # `listOf` merges each element on its own, so `dischargeProperties`
        # runs there too: `mkIf false` drops that one element rather than the
        # whole definition.
        kube, _ = await self._case("elementSwitchedOff")
        assert kube == ["--kept"]


class TestPrioritiesInsideAnEntry:
    """A marked list merges its entries through `types.attrsOf elemType`.

    An entry therefore gets the full module merge any other option value gets.
    "The override mechanism reaches inside a list element" is the whole reason
    the two markers exist, so it is worth pinning rather than assuming.
    """

    async def test_mk_default_loses_to_the_plain_list(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "namedEntryMkDefaultLoses")
        assert result == {"containers": [{"name": "app", "image": "from-chart"}]}

    async def test_mk_default_fills_a_field_the_plain_list_omits(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "namedEntryMkDefaultFillsAGap")
        assert result == {"containers": [{"name": "app", "image": "from-chart", "imagePullPolicy": "IfNotPresent"}]}

    async def test_mk_merge_inside_an_entry(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "namedEntryMkMerge")
        assert result == {"containers": [{"name": "app", "image": "v1", "imagePullPolicy": "Always"}]}

    async def test_switching_a_patch_off_leaves_the_plain_entry(self) -> None:
        # `mkIf false` on an entry the plain list also defines discharges to
        # nothing, and the plain definition is untouched. So this is a no-op,
        # not a deletion: a module cannot remove an entry by switching its own
        # patch off.
        result = await evaluate_file(NIX_TEST_FILE, "namedEntrySwitchedOffLeavesThePlainEntry")
        assert result == {"containers": [{"name": "app", "image": "v1"}]}

    async def test_a_switched_off_new_entry_is_not_added(self) -> None:
        # No definition survives for that key, so `attrsOf` drops it and the
        # entry never reaches the list.
        result = await evaluate_file(NIX_TEST_FILE, "namedEntrySwitchedOffIsNotAdded")
        assert result == {"containers": [{"name": "app", "image": "v1"}]}

    async def test_a_switched_on_new_entry_is_added(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "namedEntrySwitchedOnIsAdded")
        assert result == {"containers": [{"name": "app", "image": "v1"}, {"name": "sidecar", "image": "s1"}]}

    async def test_metadata_keeps_one_entry_per_element(self) -> None:
        # The invariant that keeps `valueMeta` usable, after a force, a drop
        # and an append have all happened to the same list.
        result = await evaluate_file(NIX_TEST_FILE, "metadataTracksTheValue")
        assert isinstance(result, dict)
        value = result["value"]
        assert isinstance(value, list)
        assert value == [
            {"name": "app", "image": "v2"},
            {"name": "sidecar", "image": "s1"},
            {"name": "added", "image": "a"},
        ]
        assert result["metaLength"] == len(value)

    async def test_numbered_mk_default_loses_to_the_plain_list(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "numberedEntryMkDefaultLoses")
        assert result == {"initContainers": [{"name": "migrate", "image": "m1"}]}

    async def test_a_switched_off_numbered_entry_is_not_appended(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "numberedEntrySwitchedOffIsNotAdded")
        assert result == {"initContainers": [{"name": "migrate"}]}

    async def test_an_order_property_on_a_numbered_entry_is_tolerated(self) -> None:
        """Recorded, not endorsed -- the two branches disagree here.

        The named branch refuses `mkBefore`/`mkAfter`/`mkOrder` on an entry,
        because a named list takes its order from the keys and the property
        would be lost in silence. A numbered list takes its order from the keys
        too, but `orderedNamedKeys` only inspects `isNamedList` definitions, so
        the numbered branch never runs that check. The property is discharged
        and the entry merges as if it had not been written.

        This test states what happens today. Whether it should throw instead is
        a decision about the type, not about this test.
        """
        result = await evaluate_file(NIX_TEST_FILE, "numberedEntryWithAnOrderProperty")
        assert result == {"initContainers": [{"name": "migrate", "image": "m2"}]}

    async def test_two_definitions_of_one_field_inside_an_entry_conflict(self) -> None:
        # This is why every other test here writes `mkForce`: without one, a
        # patch of a field the plain list already sets is a conflict rather
        # than an override. The error names the entry by key.
        with pytest.raises(nanopynix.NixError, match=r"value\.containers\.app\.image"):
            await evaluate_file(NIX_TEST_FILE, "namedEntryConflictThrows")

    async def test_two_definitions_of_one_numbered_field_conflict(self) -> None:
        # `showOption` quotes an index, since `0` is not an identifier.
        with pytest.raises(nanopynix.NixError, match=r'value\.initContainers\."0"\.image'):
            await evaluate_file(NIX_TEST_FILE, "numberedEntryConflictThrows")
