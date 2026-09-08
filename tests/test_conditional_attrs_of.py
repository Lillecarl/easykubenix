from __future__ import annotations

from pathlib import Path

import nanopynix
import pytest

from ekn.eval import evaluate_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NIX_TEST_FILE = PROJECT_ROOT / "tests/test_conditional_attrs_of.nix"


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class TestConditionalAttrsOf:
    async def test_the_type_uses_the_v2_merge_protocol(self) -> None:
        # `merge.v2` and a coherent `check` are what let this type sit under
        # `either`/`oneOf` without the module system falling back to the
        # expensive coherence check, and what let it publish `valueMeta`.
        assert await evaluate_file(NIX_TEST_FILE, "usesV2Merge") is True

    async def test_a_key_with_no_ordinary_definition_is_omitted(self) -> None:
        assert await evaluate_file(NIX_TEST_FILE, "omitsKeysWithoutAnOrdinaryDefinition") is True

    async def test_marker_content_merges_into_an_existing_key(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "mergesIntoExistingKeys")
        assert result == {"original": True, "added": True, "nested": {"existing": "new"}}

    async def test_existence_is_decided_again_at_every_level(self) -> None:
        # `nested.existing` has an ordinary definition, so the marker patches
        # it. `nested.missing` has none, so the marker creates nothing.
        result = await evaluate_file(NIX_TEST_FILE, "respectsNestedExistence")
        assert result == {"existing": "new"}

    async def test_an_mk_if_false_base_does_not_count_as_existing(self) -> None:
        # The classification runs after the module system processes its own
        # properties, so a switched-off definition is absent, not present.
        assert await evaluate_file(NIX_TEST_FILE, "falseBaseStaysAbsent") is True

    async def test_definitions_with_no_marker_merge_as_plain_attrs_of(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "plainDefinitionsStillMerge")
        assert result == {"object": {"first": True, "second": True}}

    async def test_value_metadata_covers_the_included_keys_only(self) -> None:
        assert await evaluate_file(NIX_TEST_FILE, "exposesMetadataForIncludedKeysOnly") is True

    async def test_a_path_patches_an_object_that_exists(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "pathPatchesExistingObject")
        assert result == {"replicas": 4, "patched": "true", "stringPath": "true"}

    async def test_a_list_path_carries_a_key_containing_a_dot(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "pathListFormSupportsDottedKeys")
        assert result == {"replicas": 1, "listPath": "true"}

    async def test_a_path_creates_no_level_that_is_missing(self) -> None:
        assert await evaluate_file(NIX_TEST_FILE, "pathDoesNotCreateMissingLevels") is True

    async def test_an_empty_path_is_rejected(self) -> None:
        with pytest.raises(nanopynix.NixError, match="non-empty attribute path"):
            await evaluate_file(NIX_TEST_FILE, "emptyPathThrows")

    async def test_an_empty_path_component_is_rejected(self) -> None:
        with pytest.raises(nanopynix.NixError, match="non-empty attribute path"):
            await evaluate_file(NIX_TEST_FILE, "emptyStringComponentThrows")

    async def test_a_quoted_string_path_is_rejected(self) -> None:
        # A string path is split on ".", not parsed. Refuse a quoted key
        # rather than give the quote a meaning; the list form takes it.
        with pytest.raises(nanopynix.NixError, match="do not support quoting"):
            await evaluate_file(NIX_TEST_FILE, "quotedStringPathThrows")

    async def test_a_marker_at_the_option_root_is_rejected(self) -> None:
        # Nothing above the option can decide whether the option exists, so a
        # bare marker there has no meaning. It must not merge as an ordinary
        # attribute set and leave `_type` in the output.
        with pytest.raises(nanopynix.NixError, match="is not of type"):
            await evaluate_file(NIX_TEST_FILE, "markerAtOptionRootThrows")


class TestPrioritiesAndMarkers:
    """Priorities, on an ordinary definition and inside marker content.

    `mergeEntry` decides existence first and hands the survivors to `elemType`
    second, so the ordinary module rules have to keep working on both sides of
    that split.
    """

    async def test_two_markers_on_one_key_both_apply(self) -> None:
        # Neither marker is a definition that makes the key exist; the
        # ordinary one is. Both contribute their content once it does.
        result = await evaluate_file(NIX_TEST_FILE, "twoMarkersOnOneKey")
        assert result == {"present": {"base": True, "first": 1, "second": 2}}

    async def test_marker_content_obeys_its_own_priorities(self) -> None:
        # The content merges by the normal rules: a `mkDefault` loses to the
        # ordinary definition and still fills a field it leaves alone.
        result = await evaluate_file(NIX_TEST_FILE, "markerContentDefaults")
        assert result == {"present": {"replicas": 1, "paused": True}}

    async def test_an_mk_if_true_base_counts_as_existing(self) -> None:
        # The other half of `test_an_mk_if_false_base_does_not_count_as_existing`.
        result = await evaluate_file(NIX_TEST_FILE, "trueBaseExists")
        assert result == {"conditionalBase": {"base": True, "patch": True}}

    async def test_a_marker_patches_a_scalar_key(self) -> None:
        # The key's value need not be an attribute set. `mergeEntry` takes the
        # slow path (the marker carries a `_type`) and the content merges
        # against a scalar like any other definition.
        result = await evaluate_file(NIX_TEST_FILE, "markerOnAScalarKey")
        assert result == {"replicas": 3}

    async def test_a_scalar_marker_still_creates_nothing(self) -> None:
        assert await evaluate_file(NIX_TEST_FILE, "markerOnlyScalarKeyStaysAbsent") is True

    async def test_a_forced_ordinary_definition_takes_the_slow_path(self) -> None:
        # `mergeEntry`'s fast path fires only when no definition of the key
        # carries a `_type`. A `mkForce` carries one, so this goes the long way
        # round and must still merge exactly as `attrsOf` would.
        result = await evaluate_file(NIX_TEST_FILE, "forcedOrdinaryDefinitionTakesTheSlowPath")
        assert result == {"present": {"replicas": 3}}

    async def test_a_conditional_marker_is_allowed(self) -> None:
        # `mkIf` changes no priority, so it is the ordinary way to write a
        # patch that is itself conditional. It stays legal.
        result = await evaluate_file(NIX_TEST_FILE, "conditionalMarkerIsAllowed")
        assert result == {"present": {"base": True, "patch": True}}

    async def test_a_switched_off_conditional_marker_patches_nothing(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "conditionalMarkerSwitchedOff")
        assert result == {"present": {"base": True}}


class TestTypeComposition:
    """`functor`, `substSubModules` and `getSubOptions`.

    These make the type behave like `attrsOf` to the rest of the module system
    rather than only to a value. Nothing exercised them, and each has its own
    consumer that fails in its own way without it.
    """

    async def test_a_submodule_element_type_survives_a_second_declaration(self) -> None:
        # `substSubModules`, which the module system calls when it pushes a
        # further module into a `submodule` element type. Without it the second
        # declaration's module is dropped and `extra` never appears.
        #
        # The marker still works through a submodule element type, and a key
        # only a marker defines is still omitted.
        result = await evaluate_file(NIX_TEST_FILE, "submoduleElementValue")
        assert result == {"application": {"replicas": 3, "extra": "patched"}}

    async def test_sub_options_name_the_element_s_options(self) -> None:
        # `getSubOptions`, which documentation generation walks. It reaches
        # both declarations' options, under a `<name>` placeholder.
        result = await evaluate_file(NIX_TEST_FILE, "subOptionNames")
        assert result == ["_module", "extra", "replicas"]

    async def test_a_self_referential_element_type_cannot_be_redeclared(self) -> None:
        """A known limit, pinned with the control that says whose it is.

        `functor.binOp` hands `typeMerge` the element type, and a
        self-referential one has no bottom, so the walk never terminates. This
        is nixpkgs' `oneOf`/`either` `binOp` rather than anything
        `conditionalAttrsOf` does -- the control below is the same shape built
        from plain `attrsOf` and fails identically.

        The shape that does work is a submodule element type, above: it defers
        instead of recursing. If nixpkgs ever makes `typeMerge` handle a
        recursive type, this test is what notices.
        """
        with pytest.raises(nanopynix.NixError, match="stack overflow"):
            await evaluate_file(NIX_TEST_FILE, "twoDeclarationsOfOneOption")

    async def test_the_same_limit_applies_to_a_plain_attrs_of_type(self) -> None:
        # The control. Same failure, no `conditionalAttrsOf` anywhere in it.
        with pytest.raises(nanopynix.NixError, match="stack overflow"):
            await evaluate_file(NIX_TEST_FILE, "twoDeclarationsOfAPlainRecursiveOption")


class TestAPriorityAroundAMarkerIsRejected:
    """A priority belongs inside the marker's content, never around the marker.

    Around it, the definition cannot work in either direction. A priority above
    the key's ordinary definitions outranks them: `filterOverrides` drops them,
    the marker finds no ordinary definition left, and the key it meant to patch
    is *deleted*. A priority below them is itself dropped and the marker does
    nothing at all.

    That deletion is the reason this throws rather than being documented.
    `kubernetes.objects` is a `conditionalAttrsOf` at the namespace, Kind and
    name levels, so the same slip one level up empties a whole Kind in silence.
    """

    async def test_a_forced_marker_is_rejected(self) -> None:
        with pytest.raises(nanopynix.NixError, match="puts a priority around an"):
            await evaluate_file(NIX_TEST_FILE, "forcedMarkerThrows")

    async def test_the_error_names_the_option_and_the_fix(self) -> None:
        with pytest.raises(nanopynix.NixError, match=r"value\.present"):
            await evaluate_file(NIX_TEST_FILE, "forcedMarkerThrows")
        with pytest.raises(nanopynix.NixError, match=r"lib\.mkForce 3"):
            await evaluate_file(NIX_TEST_FILE, "forcedMarkerThrows")

    async def test_a_defaulted_marker_is_rejected_too(self) -> None:
        # The direction that merely does nothing. Refused by the same rule,
        # rather than left as a definition that silently never applies.
        with pytest.raises(nanopynix.NixError, match="puts a priority around an"):
            await evaluate_file(NIX_TEST_FILE, "defaultedMarkerThrows")

    async def test_an_override_nested_under_an_mk_if_is_rejected(self) -> None:
        # The wrappers nest, so the check follows them. `mkIf` is not itself an
        # offence, and an override under one still is.
        with pytest.raises(nanopynix.NixError, match="puts a priority around an"):
            await evaluate_file(NIX_TEST_FILE, "markerForcedUnderAnMkIfThrows")

    async def test_an_override_inside_an_mk_merge_is_rejected(self) -> None:
        with pytest.raises(nanopynix.NixError, match="puts a priority around an"):
            await evaluate_file(NIX_TEST_FILE, "markerForcedInsideAnMkMergeThrows")
