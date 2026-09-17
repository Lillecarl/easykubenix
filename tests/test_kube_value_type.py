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
        with pytest.raises(nanopynix.NixError, match="mkNamedList, mkNumberedList"):
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


class TestTheBranchOrderIsLoadBearing:
    """Two orderings in `baseType` are correctness, not performance.

    The list is otherwise sorted by measured node frequency -- strings
    58.8%, attribute sets 32.1%, `null` 0.02% -- because `oneOf` is a left
    fold and a value matching at position k pays k checks. These two must
    survive the next person who re-counts and re-sorts. Issue
    Lillecarl/easykubenix#31.
    """

    async def test_an_absolute_path_string_stays_a_string(self) -> None:
        """`types.str` must precede `types.path`.

        `path.check` accepts any string beginning with "/", and manifests
        are full of absolute-path-looking strings -- every `mountPath` and
        every `command`. Swap the two and each merges as a path, which
        either copies a file into the store or fails because none exists.
        """
        result = await evaluate_file(NIX_TEST_FILE, "absolutePathStringsStayStrings")

        assert result == {
            "mountPath": "/var/lib/grafana",
            "command": "/bin/sh",
            "notAPath": "/nix/store/0000000000000000000000000000000-nothing-here",
        }
        assert all(isinstance(value, str) for value in result.values())

    async def test_a_lone_marked_list_still_becomes_a_list(self) -> None:
        """`namedListOf` must precede `objectType`, which the file already
        documents. A plain object fails the former's check, so ordinary
        objects still fall through; a marked one must not be taken by the
        attribute map and keep its `_type` in the output."""
        result = await evaluate_file(NIX_TEST_FILE, "loneMkNamedListBecomesList")

        assert result == {"containers": [{"name": "main", "image": "v1"}]}


class TestUntypedValues:
    """A value carried whole, never walked.

    For something large and opaque that nobody merges field by field -- a
    CRD's OpenAPI schema, a Grafana dashboard. Typed, 39 dashboards cost
    0.740s; carried, 0.158s. Issue Lillecarl/easykubenix#32.
    """

    async def test_one_definition_unwraps_to_its_content(self) -> None:
        """Nothing of the marker survives: no `_type`, no `content`."""
        result = await evaluate_file(NIX_TEST_FILE, "untypedCarriesTheValueWhole")

        assert result == {"panels": [{"id": 1}], "nested": {"deep": "x"}}

    async def test_the_object_map_does_not_eat_the_marker(self) -> None:
        """The guard on `objectType`, and the reason it is needed.

        That type's check is `isAttrs` plus a guard against the list markers,
        so without naming this one too it accepts an untyped marker, merges
        it as a plain object, and leaves `_type` and `content` in the
        rendered manifest. It surfaced as a differing leaf count -- 37,455
        against 37,416 -- rather than by reading the code.

        The content here deliberately *contains* the words `_type` and
        `content`, so a value that merely looks like a marker still comes
        through untouched.
        """
        result = await evaluate_file(NIX_TEST_FILE, "untypedNestedInsideAnObject")

        assert result == {
            "metadata": {"name": "cm"},
            "data": {"dashboard": {"_type": "not-a-marker", "content": "a field genuinely called content"}},
        }

    async def test_two_definitions_are_an_error_not_a_merge(self) -> None:
        """The difference from `types.anything`, which measures identically
        and deep-merges the second definition silently -- invisible until the
        day somebody sets the value twice. The message names the option."""
        with pytest.raises(nanopynix.NixError, match="is untyped and has 2 definitions"):
            await evaluate_file(NIX_TEST_FILE, "untypedTwiceThrows")


class TestBranchSelection:
    """Which branch of `oneOf` takes a definition.

    Every other class here tests how definitions merge. This tests where they
    land first, which is a separate mechanism and the one that decides whether
    a value keeps its Nix type on the way to JSON. `types.oneOf` is a left fold
    of `either`, and `either` takes the first branch whose `check` accepts every
    definition -- so the order in `baseType` is load-bearing, and a branch whose
    check is too wide silently swallows a value meant for a later one.
    """

    async def test_each_scalar_keeps_its_own_type(self) -> None:
        # An int that arrives as a float, or a bool that arrives as a string,
        # is a manifest the API server rejects.
        result = await evaluate_file(NIX_TEST_FILE, "scalarBranches")
        assert result == {"anInt": 3, "aFloat": 3.0, "aString": "3", "aTrue": True, "aFalse": False}
        assert isinstance(result, dict)
        # `3 == 3.0` in Python, so equality above does not separate the two.
        assert isinstance(result["anInt"], int)
        assert isinstance(result["aFloat"], float)
        assert isinstance(result["aTrue"], bool)

    async def test_a_path_renders_as_a_store_path(self) -> None:
        # `types.path` is a branch of its own and nothing exercised it.
        assert await evaluate_file(NIX_TEST_FILE, "pathBranch") == "test_kube_value_type.nix"

    async def test_an_empty_attribute_set_stays_an_object(self) -> None:
        # `{}` and `[]` are not interchangeable in a Kubernetes manifest.
        assert await evaluate_file(NIX_TEST_FILE, "emptyObjectStaysAnObject") == {"spec": {}}

    async def test_an_empty_list_stays_a_list(self) -> None:
        # `namedListOf` comes before `objectType`, so an empty list has to fail
        # `objectType`'s check rather than be taken by it.
        assert await evaluate_file(NIX_TEST_FILE, "emptyListStaysAList") == {"args": []}

    async def test_a_marked_list_nested_inside_an_object(self) -> None:
        # `objectType`'s `addCheck` is what stops the object branch taking a
        # marker: its own check is only `isAttrs`, so without the guard the
        # marker would merge as an ordinary object and leave `_type` in the
        # rendered manifest. `loneMkNamedListBecomesList` covers the option
        # root; this covers a marker further down a value tree.
        result = await evaluate_file(NIX_TEST_FILE, "markedListInsideAnObject")
        assert result == {"spec": {"template": {"containers": [{"name": "a", "image": "x"}]}}}

    async def test_one_field_cannot_be_an_int_and_a_string(self) -> None:
        # No branch accepts both, so `oneOf` rejects the pair. A Kubernetes
        # field has one fixed type, the same argument that rejects an unmarked
        # attribute set against a list.
        with pytest.raises(nanopynix.NixError, match=r"value\.replicas"):
            await evaluate_file(NIX_TEST_FILE, "intAndStringThrows")

    async def test_a_function_matches_no_branch(self) -> None:
        # Without a rejection here it would reach `builtins.toJSON` and fail
        # there instead, naming neither the option nor the module that wrote it.
        with pytest.raises(nanopynix.NixError, match=r"value\.callback"):
            await evaluate_file(NIX_TEST_FILE, "aFunctionThrows")


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

    async def test_an_order_property_on_a_numbered_entry_is_rejected(self) -> None:
        # A numbered list takes its order from its index keys, exactly as a
        # named list takes its order from its name keys. So an order property
        # on an entry does nothing, and both branches now say so. This used to
        # be discharged in silence: `orderedEntryKeys` inspected only
        # `isNamedList` definitions.
        with pytest.raises(nanopynix.NixError, match="mkBefore/mkAfter/mkOrder"):
            await evaluate_file(NIX_TEST_FILE, "mkOrderOnNumberedEntryThrows")

    async def test_the_numbered_order_error_names_the_index_keys(self) -> None:
        with pytest.raises(nanopynix.NixError, match=r"mkNumberedList entries: 0"):
            await evaluate_file(NIX_TEST_FILE, "mkOrderOnNumberedEntryThrows")

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

    async def test_replace_patches_a_scalar_list_by_content(self) -> None:
        # The case the marker exists for: a chart's `args`, where an element
        # has no `name` and its index moves with the next chart version.
        result = await evaluate_file(NIX_TEST_FILE, "replaceOverrideOfScalarList")
        assert result == {
            "args": [
                "--metrics-addr=:8443",
                "--enable-leader-election",
                "--health-probe-addr=:8081",
            ]
        }

    async def test_an_exact_key_replaces_too(self) -> None:
        # A whole element is a prefix of itself, so prefix matching covers
        # the exact case and needs no second match mode.
        result = await evaluate_file(NIX_TEST_FILE, "replaceWithAnExactKey")
        assert result == {"args": ["--a", "--B"]}

    async def test_two_modules_replace_two_elements(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "replaceFromTwoModules")
        assert result == {"args": ["--a=2", "--b=1", "--c=2"]}

    async def test_replace_works_inside_a_named_list_entry(self) -> None:
        # The real call site: the container by name, its args by content.
        result = await evaluate_file(NIX_TEST_FILE, "replaceNestedInANamedList")
        assert result == {"containers": [{"name": "manager", "args": ["--metrics-addr=:8443", "--leader-elect"]}]}

    async def test_a_switched_off_replace_marker_leaves_the_list(self) -> None:
        # `mkIf false` drops the marker before the merge, so a key that would
        # match nothing is not an error when the patch is switched off.
        result = await evaluate_file(NIX_TEST_FILE, "replaceSwitchedOffLeavesTheList")
        assert result == {"args": ["--a", "--b"]}

    async def test_a_replace_key_that_matches_nothing_is_an_error(self) -> None:
        # The whole point of the marker. `mkNumberedList` cannot do this: an
        # index always matches something, just not the element you meant.
        with pytest.raises(nanopynix.NixError, match="keys that match\n *no element"):
            await evaluate_file(NIX_TEST_FILE, "replaceKeyMatchesNothingThrows")

    async def test_the_no_match_error_shows_the_list(self) -> None:
        with pytest.raises(nanopynix.NixError, match=r'The list holds:\n *"--a"'):
            await evaluate_file(NIX_TEST_FILE, "replaceKeyMatchesNothingThrows")

    async def test_a_replace_key_that_matches_twice_is_an_error(self) -> None:
        with pytest.raises(nanopynix.NixError, match="more than one element"):
            await evaluate_file(NIX_TEST_FILE, "replaceKeyMatchesTwoThrows")

    async def test_two_replace_keys_on_one_element_is_an_error(self) -> None:
        with pytest.raises(nanopynix.NixError, match="match the same element"):
            await evaluate_file(NIX_TEST_FILE, "replaceTwoKeysOnOneElementThrows")

    async def test_a_replace_marker_alone_has_nothing_to_replace(self) -> None:
        with pytest.raises(nanopynix.NixError, match="no list to replace in"):
            await evaluate_file(NIX_TEST_FILE, "replaceWithNoListThrows")

    async def test_mk_force_drops_the_list_the_marker_needs(self) -> None:
        # `mkForce` removes the plain definitions before the type sees them,
        # so it turns the marker into the case above. The error says so,
        # because `mkForce` is the habit this marker replaces.
        with pytest.raises(nanopynix.NixError, match="needs no mkForce"):
            await evaluate_file(NIX_TEST_FILE, "replaceUnderMkForceThrows")

    async def test_replace_refuses_a_list_of_objects(self) -> None:
        # Matching by content reads a string. Point at `mkNamedList` instead
        # of reporting that the key matched nothing.
        with pytest.raises(nanopynix.NixError, match="not a string"):
            await evaluate_file(NIX_TEST_FILE, "replaceOnAListOfObjectsThrows")

    async def test_mixed_numbered_and_replace_markers_rejected(self) -> None:
        with pytest.raises(nanopynix.NixError, match="mkNumberedList, mkReplaceList"):
            await evaluate_file(NIX_TEST_FILE, "mixedNumberedAndReplaceThrows")

    async def test_an_order_property_on_a_replace_entry_is_rejected(self) -> None:
        # A replacement takes the position of the element it replaces, so an
        # order property on it does nothing.
        with pytest.raises(nanopynix.NixError, match=r"mkReplaceList entries: --a"):
            await evaluate_file(NIX_TEST_FILE, "mkOrderOnReplaceEntryThrows")

    async def test_two_replacements_of_one_element_conflict(self) -> None:
        # The entries merge as an attribute set, so the module system reports
        # the conflict itself and names the key.
        with pytest.raises(nanopynix.NixError, match=r'value\.args\."--a="'):
            await evaluate_file(NIX_TEST_FILE, "replaceConflictingValuesThrows")

    async def test_mk_replace_list_rejects_non_attrs_input(self) -> None:
        with pytest.raises(nanopynix.NixError, match="Input must be an attribute set"):
            await evaluate_file(NIX_TEST_FILE, "mkReplaceListRejectsNonAttrsInput")

    async def test_mk_replace_list_rejects_the_empty_key(self) -> None:
        # An empty key is a prefix of every element, so it can never resolve
        # to one. Refuse it where it is written, not at the merge.
        with pytest.raises(nanopynix.NixError, match="empty key matches every element"):
            await evaluate_file(NIX_TEST_FILE, "mkReplaceListRejectsTheEmptyKey")

    async def test_replace_metadata_is_one_entry_per_element(self) -> None:
        # The invariant `metadataTracksTheValue` keeps on the named branch.
        result = await evaluate_file(NIX_TEST_FILE, "replaceMetadataTracksTheValue")
        assert result["value"] == ["--a=1", "--b=2"]
        assert result["metaLength"] == 2

    async def test_replace_markers_compose_with_mk_merge(self) -> None:
        # `mkMerge` is the list form. The module system expands it into one
        # definition per element, so the marker needs no list shape of its
        # own -- which would merge by concatenation instead of by key and
        # lose the conflict report below.
        result = await evaluate_file(NIX_TEST_FILE, "replaceMarkersComposeWithMkMerge")
        assert result == {"args": ["--a=2", "--b=1", "--c=2"]}

    async def test_one_module_can_hold_the_list_and_its_patch(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "replaceMergedWithItsOwnList")
        assert result == {"args": ["--a=1", "--b=2"]}

    async def test_a_member_of_an_mk_merge_can_be_switched_off(self) -> None:
        # `mkIf` resolves before the merge, so a switched-off member never
        # has to match an element.
        result = await evaluate_file(NIX_TEST_FILE, "replaceInsideMkMergeCanBeSwitchedOff")
        assert result == {"args": ["--a=2", "--b=1"]}

    async def test_mk_merge_does_not_allow_one_element_twice(self) -> None:
        # Composing with `mkMerge` is not a way around the conflict: the
        # entries still merge as an attribute set, keyed by the match.
        with pytest.raises(nanopynix.NixError, match=r'value\.args\."--a="'):
            await evaluate_file(NIX_TEST_FILE, "replaceMkMergeSameKeyThrows")

    async def test_the_no_match_error_names_the_closest_element(self) -> None:
        # The list can be twenty flags long. Naming the nearest one turns a
        # scan of the list into a read of one line.
        with pytest.raises(
            nanopynix.NixError,
            match=r'the closest element is "--metrics-addr=0\.0\.0\.0:8443"',
        ):
            await evaluate_file(NIX_TEST_FILE, "replaceNoMatchNamesTheClosestElement")

    async def test_no_suggestion_when_nothing_resembles_the_key(self) -> None:
        # Every long option starts with `--`, so a suggestion built on that
        # alone names a neighbour at random. Say nothing instead.
        with pytest.raises(nanopynix.NixError, match="nothing in the list resembles it"):
            await evaluate_file(NIX_TEST_FILE, "replaceNoMatchWithNothingSimilar")
