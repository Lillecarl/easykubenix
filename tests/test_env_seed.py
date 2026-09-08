from __future__ import annotations

from pathlib import Path

import nanopynix
import pytest

from ekn.eval import evaluate_file

PROJECT_ROOT = Path(__file__).resolve().parent.parent
NIX_TEST_FILE = PROJECT_ROOT / "tests/test_env_seed.nix"


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class TestEnvSeed:
    async def test_a_reference_is_a_plain_string(self) -> None:
        # Not a marked attrset. A Secret's `stringData` is
        # `map[string]string`, so an attrset there makes the rendered
        # manifest fail schema validation -- and validation.nix pipes the
        # manifest straight into kubeconform, outside the CLI, where nothing
        # can substitute first.
        assert await evaluate_file(NIX_TEST_FILE, "referenceIsAString") is True
        assert await evaluate_file(NIX_TEST_FILE, "reference") == "$ekn:env:ARGOCD_REPO_PASSWORD"

    async def test_the_prefix_is_stable(self) -> None:
        # The CLI matches on this, so it is part of the contract between
        # the module system and `ekn`.
        assert await evaluate_file(NIX_TEST_FILE, "prefix") == "$ekn:env:"

    async def test_a_reference_is_recognised_again(self) -> None:
        assert await evaluate_file(NIX_TEST_FILE, "recognisesItsOwnReference") is True
        assert await evaluate_file(NIX_TEST_FILE, "readsTheVariableBack") == "ARGOCD_REPO_PASSWORD"

    async def test_an_ordinary_value_is_not_a_reference(self) -> None:
        assert await evaluate_file(NIX_TEST_FILE, "rejectsAnOrdinaryString") is True
        assert await evaluate_file(NIX_TEST_FILE, "rejectsANonString") is True

    async def test_the_walk_finds_a_nested_reference(self) -> None:
        assert await evaluate_file(NIX_TEST_FILE, "findsAReferenceInAnObject") is True
        assert await evaluate_file(NIX_TEST_FILE, "findsAReferenceUnderAList") is True
        assert await evaluate_file(NIX_TEST_FILE, "findsNothingInAPlainObject") is True

    async def test_marking_an_object_records_its_variables(self) -> None:
        # `envSeeded` is the one deep walk, and it runs only over an object
        # its author marked -- never over a resource set.
        assert await evaluate_file(NIX_TEST_FILE, "annotationsFromOneVariable") == {
            "ekn.dev/env-0": "ARGOCD_REPO_PASSWORD"
        }
        assert await evaluate_file(NIX_TEST_FILE, "annotationsAreNumbered") == {
            "ekn.dev/env-0": "ARGOCD_REPO_PASSWORD",
            "ekn.dev/env-1": "ARGOCD_REPO_SSHKEY",
        }

    async def test_marking_leaves_the_object_otherwise_alone(self) -> None:
        result = await evaluate_file(NIX_TEST_FILE, "markingKeepsTheObject")
        assert isinstance(result, dict)
        assert result["password"] == "$ekn:env:ARGOCD_REPO_PASSWORD"
        assert result["username"] == "ci-token"

    async def test_a_seeded_object_is_recognised_by_its_annotation(self) -> None:
        # Shallow and bounded on purpose. Reading annotations is what lets
        # the exportable filter, the kluctl exclusion and the GitOps
        # assertion answer the question without walking every resource.
        assert await evaluate_file(NIX_TEST_FILE, "seededObjectReadsTheAnnotation") is True
        assert await evaluate_file(NIX_TEST_FILE, "seededObjectIgnoresAPlainSecret") is True

    async def test_an_unmarked_object_is_not_seeded(self) -> None:
        # Even holding a reference. The annotation is the contract, so that
        # nothing downstream has to descend into an object to find out.
        assert await evaluate_file(NIX_TEST_FILE, "seededObjectIgnoresAnUnmarkedObject") is True

    async def test_the_variables_read_back_from_the_annotations(self) -> None:
        expected = ["ARGOCD_REPO_PASSWORD", "ARGOCD_REPO_SSHKEY"]
        assert await evaluate_file(NIX_TEST_FILE, "variablesReadBackFromAnnotations") == expected
        assert await evaluate_file(NIX_TEST_FILE, "collectsVariablesInOrder") == expected

    async def test_marking_an_object_with_no_reference_is_rejected(self) -> None:
        with pytest.raises(nanopynix.NixError, match=r"holds no ekn\.envSeed reference"):
            await evaluate_file(NIX_TEST_FILE, "markingWithoutAReferenceThrows")

    @pytest.mark.parametrize(
        "attribute",
        ["emptyNameThrows", "leadingDigitThrows", "punctuationThrows"],
    )
    async def test_an_unusable_variable_name_is_rejected(self, attribute: str) -> None:
        # A name no shell can export is a reference nothing can ever
        # resolve. Fail at evaluation rather than let a literal sentinel
        # reach a cluster.
        with pytest.raises(nanopynix.NixError, match="not a usable environment variable"):
            await evaluate_file(NIX_TEST_FILE, attribute)

    async def test_a_non_string_variable_name_is_rejected(self) -> None:
        with pytest.raises(nanopynix.NixError, match="must be a string"):
            await evaluate_file(NIX_TEST_FILE, "nonStringThrows")

    async def test_reading_the_variable_of_a_non_reference_is_rejected(self) -> None:
        with pytest.raises(nanopynix.NixError, match=r"not an ekn\.envSeed reference"):
            await evaluate_file(NIX_TEST_FILE, "variableOfNonSeedThrows")
