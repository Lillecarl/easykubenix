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

    async def test_present_and_unset_leaves_the_credential_alone(
        self, present: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The steady state. The rendered object must never be applied as it
        # stands: server-side apply under field manager `ekn` would overwrite
        # the live credential with the literal reference. The value already
        # in the cluster goes back into the field instead, so the apply is a
        # no-op for the credential and ordinary for every other field.
        monkeypatch.delenv("ARGOCD_REPO_PASSWORD", raising=False)
        plan = await seeds.resolve([SEEDED], api=object())
        assert [a.verb for a in plan.actions] == ["skip"]
        assert plan.objects[0]["stringData"]["password"] == "live-value"
        assert seeds.REFERENCE_PREFIX not in str(plan.objects[0])

    async def test_present_and_set_to_the_same_value_is_a_no_op(
        self, present: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A variable left exported must not churn the credential. The object
        # is still applied, carrying the value the cluster already holds, so
        # any other field on it reconciles.
        monkeypatch.setenv("ARGOCD_REPO_PASSWORD", "live-value")
        plan = await seeds.resolve([SEEDED], api=object())
        assert [a.verb for a in plan.actions] == ["unchanged"]
        assert plan.objects[0]["stringData"]["password"] == "live-value"

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


class TestTable:
    def test_it_lines_the_columns_up(self) -> None:
        rows = [
            seeds.SeedRow("SHORT", "argocd", "Secret", "repo-creds", "stringData.password", True, False),
            seeds.SeedRow(
                "A_MUCH_LONGER_VARIABLE_NAME", "external-secrets", "Secret", "store", "stringData.token", False, None
            ),
        ]
        lines = seeds.table(rows).splitlines()
        assert lines[0].startswith("VARIABLE")
        # Every row's OBJECT column starts at the same offset.
        offsets = {line.index("argocd") for line in lines[1:2]}
        assert lines[2].index("external-secrets") in offsets

    def test_unknown_presence_is_not_reported_as_absent(self) -> None:
        row = seeds.SeedRow("V", "ns", "Secret", "n", "stringData.p", False, None)
        assert seeds.table([row]).splitlines()[1].split()[-1] == "?"
        present = row._replace(in_cluster=False)
        assert seeds.table([present]).splitlines()[1].split()[-1] == "no"


class TestEmptyIsUnset:
    """An exported-but-empty variable is not a value.

    A shell exports one from a substitution that produced nothing. Treating
    it as set is worse than treating it as missing: the apply succeeds, the
    Secret exists with a zero-length credential, and only the consumer's
    authentication failure says otherwise.
    """

    def test_empty_is_not_supplied(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARGOCD_REPO_PASSWORD", "")
        assert seeds.is_supplied("ARGOCD_REPO_PASSWORD") is False
        monkeypatch.setenv("ARGOCD_REPO_PASSWORD", "x")
        assert seeds.is_supplied("ARGOCD_REPO_PASSWORD") is True

    async def test_absent_and_empty_aborts_instead_of_creating(
        self, absent: None, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("ARGOCD_REPO_PASSWORD", "")
        with pytest.raises(seeds.MissingVariablesError, match="ARGOCD_REPO_PASSWORD is not set"):
            await seeds.resolve([SEEDED], api=object())

    def test_describe_reports_empty_as_not_set(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("ARGOCD_REPO_PASSWORD", "")
        assert seeds.describe([SEEDED])[0].is_set is False


class TestOtherFieldsStillReconcile:
    """Leaving the credential alone must not freeze the rest of the object.

    A seeded Secret is ordinary configuration with a credential in one
    field, so the other fields are exactly the ones somebody edits next. An
    earlier version dropped the whole object, and an unrelated `username`
    change silently failed to apply until the password itself changed.
    """

    async def test_a_skip_still_applies_the_object(
        self, present: dict[str, Any], monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("ARGOCD_REPO_PASSWORD", raising=False)
        edited = secret(
            annotations={"ekn.dev/env-0": "ARGOCD_REPO_PASSWORD"},
            password="$ekn:env:ARGOCD_REPO_PASSWORD",
        )
        edited["stringData"]["username"] = "oauth2"
        plan = await seeds.resolve([edited], api=object())

        assert [a.verb for a in plan.actions] == ["skip"]
        assert len(plan.objects) == 1
        # The edit lands...
        assert plan.objects[0]["stringData"]["username"] == "oauth2"
        # ...and the credential is written back exactly as the cluster holds
        # it, so the apply is a no-op for that field rather than a deletion.
        assert plan.objects[0]["stringData"]["password"] == "live-value"

    async def test_an_unreadable_live_value_falls_back_to_dropping(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Nothing to reconstruct the field from, so applying would delete it.
        # Leave the object out entirely instead.
        async def build_object(_spec: dict[str, Any], _api: object) -> FakeObject:
            return FakeObject({"data": {}})

        monkeypatch.setattr(seeds, "build_object", build_object)
        monkeypatch.delenv("ARGOCD_REPO_PASSWORD", raising=False)
        plan = await seeds.resolve([SEEDED], api=object())
        assert [a.verb for a in plan.actions] == ["skip"]
        assert plan.objects == []
