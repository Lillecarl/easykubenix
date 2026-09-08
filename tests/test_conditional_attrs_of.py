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
