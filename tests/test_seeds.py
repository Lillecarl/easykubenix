from __future__ import annotations

import base64
from typing import Any

import pytest

from ekn import seeds


def secret(*, annotations: dict[str, str] | None = None, **string_data: str) -> dict[str, Any]:
    """An ArgoCD-repository-credential-shaped Secret: mostly ordinary
    configuration, with a reference in one field."""
    metadata: dict[str, Any] = {"name": "repo-creds", "namespace": "argocd"}
    if annotations is not None:
        metadata["annotations"] = annotations
    return {
        "apiVersion": "v1",
        "kind": "Secret",
        "metadata": metadata,
        "stringData": {"type": "git", "username": "ci-token", **string_data},
    }


SEEDED = secret(
    annotations={"ekn.dev/env-0": "ARGOCD_REPO_PASSWORD"},
    password="$ekn:env:ARGOCD_REPO_PASSWORD",
)


class FakeObject:
    """Stands in for a kr8s APIObject: either present, or not."""

    def __init__(self, raw: dict[str, Any] | None) -> None:
        self._raw = raw

    async def async_refresh(self) -> None:
        if self._raw is None:
            import kr8s

            raise kr8s.NotFoundError("absent")

    @property
    def raw(self) -> dict[str, Any]:
        assert self._raw is not None
        return self._raw


@pytest.fixture
def absent(monkeypatch: pytest.MonkeyPatch) -> None:
    async def build_object(_spec: dict[str, Any], _api: object) -> FakeObject:
        return FakeObject(None)

    monkeypatch.setattr(seeds, "build_object", build_object)


def live_with(**data: str) -> dict[str, Any]:
    return {"data": {key: base64.b64encode(value.encode()).decode() for key, value in data.items()}}


@pytest.fixture
def present(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    state = live_with(password="live-value")

    async def build_object(_spec: dict[str, Any], _api: object) -> FakeObject:
        return FakeObject(state)

    monkeypatch.setattr(seeds, "build_object", build_object)
    return state


class TestDiscovery:
    def test_an_object_is_seeded_by_its_annotations(self) -> None:
        # Shallow: this is what lets a caller decide without walking.
        assert seeds.is_seeded(SEEDED)
        assert seeds.annotated_variables(SEEDED) == ["ARGOCD_REPO_PASSWORD"]

    def test_an_object_holding_a_reference_but_unmarked_is_not_seeded(self) -> None:
        assert not seeds.is_seeded(secret(password="$ekn:env:ARGOCD_REPO_PASSWORD"))

    def test_an_ordinary_object_is_not_seeded(self) -> None:
        assert not seeds.is_seeded(secret(password="hunter2"))
        assert seeds.annotated_variables({"kind": "ConfigMap"}) == []

    def test_variables_come_back_in_index_order(self) -> None:
        # "10" must not sort before "2". The annotation index is a number.
        obj = secret(
            annotations={f"ekn.dev/env-{index}": f"VAR_{index}" for index in range(11)},
            password="$ekn:env:VAR_0",
        )
        assert seeds.annotated_variables(obj)[:3] == ["VAR_0", "VAR_1", "VAR_2"]
        assert seeds.annotated_variables(obj)[-1] == "VAR_10"

    def test_references_carry_the_path_they_sit_at(self) -> None:
        assert seeds.references(SEEDED) == [seeds.SeedReference(("stringData", "password"), "ARGOCD_REPO_PASSWORD")]


class TestSubstitution:
    def test_a_value_replaces_the_reference_and_nothing_else(self) -> None:
        result = seeds.substitute(SEEDED, {"ARGOCD_REPO_PASSWORD": "s3cret"})
        assert result["stringData"] == {
            "type": "git",
            "username": "ci-token",
            "password": "s3cret",
        }

    def test_substitution_does_not_mutate_the_input(self) -> None:
        seeds.substitute(SEEDED, {"ARGOCD_REPO_PASSWORD": "s3cret"})
        assert SEEDED["stringData"]["password"] == "$ekn:env:ARGOCD_REPO_PASSWORD"

    @pytest.mark.parametrize("value", ['a"quote', "a\\backslash", "a\nnewline", "£unicode"])
    def test_an_awkward_value_survives(self, value: str) -> None:
        # Structural, not textual. Splicing into rendered JSON would break on
        # every one of these.
        result = seeds.substitute(SEEDED, {"ARGOCD_REPO_PASSWORD": value})
        assert result["stringData"]["password"] == value


class TestResolve:
    async def test_absent_and_set_creates(self, absent: None, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARGOCD_REPO_PASSWORD", "s3cret")
        plan = await seeds.resolve([SEEDED], api=object())
        assert [a.verb for a in plan.actions] == ["create"]
        assert plan.objects[0]["stringData"]["password"] == "s3cret"

    async def test_absent_and_unset_aborts_before_applying_anything(
        self, absent: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ARGOCD_REPO_PASSWORD", raising=False)
        with pytest.raises(seeds.MissingVariablesError, match="ARGOCD_REPO_PASSWORD is not set"):
            await seeds.resolve([SEEDED], api=object())

    async def test_every_missing_variable_is_listed_at_once(
        self, absent: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Bootstrapping a cluster with two seeds must not be two failed
        # applies.
        monkeypatch.delenv("ARGOCD_REPO_PASSWORD", raising=False)
        monkeypatch.delenv("ESO_TOKEN", raising=False)
        other = secret(annotations={"ekn.dev/env-0": "ESO_TOKEN"}, password="$ekn:env:ESO_TOKEN")
        with pytest.raises(seeds.MissingVariablesError) as caught:
            await seeds.resolve([SEEDED, other], api=object())
        assert "ARGOCD_REPO_PASSWORD is not set" in str(caught.value)
        assert "ESO_TOKEN is not set" in str(caught.value)

    async def test_present_and_unset_skips_and_drops_the_object(
        self, present: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The steady state. The object must be REMOVED from the apply set,
        # not applied unchanged: server-side apply under field manager `ekn`
        # would overwrite the live credential with the literal reference.
        monkeypatch.delenv("ARGOCD_REPO_PASSWORD", raising=False)
        plan = await seeds.resolve([SEEDED], api=object())
        assert [a.verb for a in plan.actions] == ["skip"]
        assert plan.objects == []

    async def test_present_and_set_to_the_same_value_is_a_no_op(
        self, present: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A variable left exported must not churn the cluster.
        monkeypatch.setenv("ARGOCD_REPO_PASSWORD", "live-value")
        plan = await seeds.resolve([SEEDED], api=object())
        assert [a.verb for a in plan.actions] == ["unchanged"]
        assert plan.objects == []

    async def test_present_and_set_to_a_different_value_updates(
        self, present: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARGOCD_REPO_PASSWORD", "rotated")
        plan = await seeds.resolve([SEEDED], api=object())
        assert [a.verb for a in plan.actions] == ["update"]
        assert plan.objects[0]["stringData"]["password"] == "rotated"

    async def test_a_declared_but_unreferenced_variable_still_reports(self) -> None:
        # The annotation and the reference are written together, so a
        # mismatch means something is wrong. Report it rather than hide it.
        obj = secret(
            annotations={"ekn.dev/env-0": "ARGOCD_REPO_PASSWORD", "ekn.dev/env-1": "STRAY"},
            password="$ekn:env:ARGOCD_REPO_PASSWORD",
        )
        rows = seeds.describe([obj])
        assert [(row.variable, row.field) for row in rows] == [
            ("ARGOCD_REPO_PASSWORD", "stringData.password"),
            ("STRAY", ""),
        ]

    async def test_an_unseeded_object_passes_through_untouched(self, absent: None) -> None:
        plain = {"apiVersion": "v1", "kind": "ConfigMap", "metadata": {"name": "plain"}}
        plan = await seeds.resolve([plain], api=object())
        assert plan.objects == [plain]
        assert plan.actions == []


class TestDescribe:
    def test_it_names_the_variable_the_object_and_the_field(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARGOCD_REPO_PASSWORD", "s3cret")
        rows = seeds.describe([SEEDED])
        assert len(rows) == 1
        assert rows[0].variable == "ARGOCD_REPO_PASSWORD"
        assert rows[0].identity == "argocd/Secret/repo-creds"
        assert rows[0].field == "stringData.password"
        assert rows[0].is_set is True
        # No cluster consulted yet: unknown, not absent.
        assert rows[0].in_cluster is None

    def test_it_ignores_objects_that_need_nothing(self) -> None:
        assert seeds.describe([secret(password="hunter2")]) == []

    async def test_without_a_cluster_presence_stays_unknown(self) -> None:
        # "Unknown" and "absent" are different answers, and an operator
        # bringing up a new cluster has to tell them apart.
        rows = await seeds.inspect(seeds.describe([SEEDED]), api=None)
        assert rows[0].in_cluster is None

    async def test_with_a_cluster_presence_is_filled_in(self, present: dict[str, Any]) -> None:
        rows = await seeds.inspect(seeds.describe([SEEDED]), api=object())
        assert rows[0].in_cluster is True

    async def test_an_absent_object_reports_absent(self, absent: None) -> None:
        rows = await seeds.inspect(seeds.describe([SEEDED]), api=object())
        assert rows[0].in_cluster is False
