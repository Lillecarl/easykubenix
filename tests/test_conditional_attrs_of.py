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

    async def test_forcing_a_marker_deletes_the_key_it_meant_to_patch(self) -> None:
        """Recorded, not endorsed -- and worse than it looks.

        conditionalAttrsOf.nix says to put a priority inside the content and
        never around the marker. This is the mistake it warns about, and the
        warning understates it. `mergeEntry` runs `filterOverrides` inside its
        collector, so a forced marker at priority 50 outranks the ordinary
        definition at 100 and removes it from `collected`. `ordinary` is then
        empty, the key stops existing, and the value the patch aimed at is
        gone.

        `kubernetes.objects` is a `conditionalAttrsOf` at the namespace, Kind
        and name levels, so the same mistake one level up deletes every object
        of a Kind rather than patching one.

        This test states what happens today. Making the type refuse it is a
        decision about the type, not about this test.
        """
        result = await evaluate_file(NIX_TEST_FILE, "forcedMarkerAroundAnExistingKey")
        assert result == {}
