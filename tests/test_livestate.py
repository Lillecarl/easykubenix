"""The read phase: the sweep, and the decisions made from what it returned.

Issue Lillecarl/easykubenix#28.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import kr8s
import pytest

# The same API server double the apply suite uses, so one description of a
# cluster serves the sweep, the apply and the prune.
from test_apply import FakeApi

from ekn.livestate import (
    HASH_ANNOTATION,
    ForeignOwner,
    LiveObject,
    canonical_json,
    desired_hash,
    foreign_owners,
    manifest_hash,
    skippable,
    strip_hash_annotation,
    sweep,
)

if TYPE_CHECKING:
    from ekn.apply import Manifest

ENV = "nixlab2"


def manifest(name: str = "a", hash_value: str | None = None, **annotations: str) -> Manifest:
    metadata: dict[str, Any] = {"name": name, "namespace": "default"}
    combined = dict(annotations)
    if hash_value is not None:
        combined[HASH_ANNOTATION] = hash_value
    if combined:
        metadata["annotations"] = combined
    return {"apiVersion": "v1", "kind": "ConfigMap", "metadata": metadata}


def live(
    name: str = "a",
    hash_value: str | None = None,
    environment: str | None = ENV,
    managers: frozenset[str] = frozenset({"ekn"}),
) -> LiveObject:
    return LiveObject(
        key=("default", "ConfigMap", name),
        manifest_hash=hash_value,
        environment=environment,
        managers=managers,
    )


#: What `skippable` is told the delivery engine's managers are. Every call
#: has to say, because the argument has no default -- see its docstring.
ENGINE: frozenset[str] = frozenset({"argocd-controller", "kube-controller-manager"})


class TestTheHash:
    def test_it_ignores_its_own_annotation(self) -> None:
        """The annotation is part of the object, so hashing it in would make
        the value depend on itself."""
        without = manifest()
        with_hash = manifest(hash_value="sha256:whatever")

        assert manifest_hash(without) == manifest_hash(with_hash)

    def test_it_keeps_every_other_annotation(self) -> None:
        assert manifest_hash(manifest()) != manifest_hash(manifest(other="value"))

    def test_stripping_leaves_no_empty_annotations_key(self) -> None:
        """An empty `annotations: {}` is not the same JSON as no annotations
        at all, and the two producers have to agree byte for byte."""
        stripped = strip_hash_annotation(manifest(hash_value="sha256:x"))
        metadata = stripped["metadata"]
        assert isinstance(metadata, dict)
        assert "annotations" not in metadata

    def test_canonical_json_is_stable_under_key_order(self) -> None:
        a: Manifest = {"b": 1, "a": 2}
        b: Manifest = {"a": 2, "b": 1}
        assert canonical_json(a) == canonical_json(b) == '{"a":2,"b":1}'

    def test_a_render_without_the_annotation_has_no_desired_hash(self) -> None:
        assert desired_hash(manifest()) is None
        assert desired_hash(manifest(hash_value="sha256:x")) == "sha256:x"


class TestSkipping:
    """Three conditions, and the third is the one easily left out.

    `assume_unchanged` is not among them: whether the operator asked is
    `fastcache`'s question, and this one is only ever asked about an object
    no record on this machine covers.
    """

    def test_a_matching_hash_is_skipped(self) -> None:
        spec = manifest(hash_value="sha256:x")

        assert skippable(spec, live(hash_value="sha256:x"), environment=ENV, engine_managers=ENGINE)

    def test_a_changed_hash_is_never_skipped(self) -> None:
        """Negative control: the one thing fast mode must never get wrong."""
        spec = manifest(hash_value="sha256:new")

        assert not skippable(spec, live(hash_value="sha256:old"), environment=ENV, engine_managers=ENGINE)

    def test_an_object_the_cluster_does_not_have_is_never_skipped(self) -> None:
        assert not skippable(manifest(hash_value="sha256:x"), None, environment=ENV, engine_managers=ENGINE)

    def test_an_unstamped_object_is_applied_once(self) -> None:
        """The condition that dissolves the fast-mode/prune conflict.

        An object ArgoCD applied carries the hash (it is in the committed
        YAML) but no `ekn.dev/environment`, which is what `--prune` selects
        by. Skipping it would leave prune deleting an object that is present
        and correct. So the first converge applies it, stamps it, and every
        converge after skips it.
        """
        spec = manifest(hash_value="sha256:x")

        assert not skippable(
            spec, live(hash_value="sha256:x", environment=None), environment=ENV, engine_managers=ENGINE
        )
        assert skippable(spec, live(hash_value="sha256:x"), environment=ENV, engine_managers=ENGINE)

    def test_another_environments_stamp_does_not_count(self) -> None:
        spec = manifest(hash_value="sha256:x")

        assert not skippable(
            spec, live(hash_value="sha256:x", environment="other"), environment=ENV, engine_managers=ENGINE
        )

    def test_a_render_with_no_hash_is_never_skipped(self) -> None:
        """A configuration rendered before the annotation existed, and every
        object the render deliberately leaves bare: SOPS-encrypted, seeded."""
        assert not skippable(manifest(), live(hash_value="sha256:x"), environment=ENV, engine_managers=ENGINE)

    @pytest.mark.parametrize("manager", ["kubectl-edit", "kubectl-client-side-apply", "some-operator"])
    def test_a_foreign_manager_makes_it_unskippable(self, manager: str) -> None:
        """The condition that makes content drift visible at all.

        Both hashes are annotations, and neither is recomputed from live
        content, so a `kubectl edit` that changes an image leaves them
        equal. The manager it leaves behind is the only trace, and this is
        what reads it. Without this the object is skipped for ever and the
        edit is never repaired -- reported by the operator this mode is
        for.
        """
        spec = manifest(hash_value="sha256:x")
        seen = live(hash_value="sha256:x", managers=frozenset({"ekn", manager}))

        assert not skippable(spec, seen, environment=ENV, engine_managers=ENGINE)

    def test_the_engine_and_its_controllers_do_not_block_a_skip(self) -> None:
        """The other half, and the one that decides whether this is usable.

        `kube-controller-manager` owns fields on most objects by design. If
        it counted, nearly nothing would be skippable and fast mode would
        quietly stop being fast -- so `engine_managers` is what the caller
        says is expected, and expected owners do not block.
        """
        spec = manifest(hash_value="sha256:x")
        seen = live(
            hash_value="sha256:x",
            managers=frozenset({"ekn", "kube-controller-manager", "argocd-controller"}),
        )

        assert skippable(spec, seen, environment=ENV, engine_managers=ENGINE)


class TestForeignOwners:
    ENGINE = ("argocd-controller", "kube-controller-manager")

    def test_the_engine_and_ourselves_are_not_reported(self) -> None:
        """Taking fields from the GitOps engine is the intent of this mode,
        so reporting it would bury the surprising half in noise."""
        obj = LiveObject(
            key=("vm", "Deployment", "victoria-metrics-operator"),
            managers=frozenset({"argocd-controller", "kube-controller-manager", "ekn"}),
        )

        assert foreign_owners([obj], engine_managers=self.ENGINE) == []

    def test_a_controller_that_owns_a_field_is_reported(self) -> None:
        obj = LiveObject(
            key=("default", "Service", "traefik"),
            managers=frozenset({"argocd-controller", "cilium-operator-lb-ipam"}),
        )

        assert foreign_owners([obj], engine_managers=self.ENGINE) == [
            ForeignOwner(("default", "Service", "traefik"), ("cilium-operator-lb-ipam",)),
        ]

    def test_a_person_who_ran_a_rollout_is_reported(self) -> None:
        """The case in miniature: somebody ran `kubectl rollout restart`,
        that manager still owns a field, and nothing says so."""
        obj = LiveObject(key=("apps", "Deployment", "web"), managers=frozenset({"ekn", "kubectl-rollout"}))

        assert foreign_owners([obj], engine_managers=self.ENGINE)[0].managers == ("kubectl-rollout",)

    def test_the_managers_are_sorted(self) -> None:
        obj = LiveObject(key=("a", "B", "c"), managers=frozenset({"zeta", "alpha"}))

        assert foreign_owners([obj], engine_managers=())[0].managers == ("alpha", "zeta")


#: One kind the API server serves, described as discovery describes it.
VPA: dict[str, Any] = {
    "version": "autoscaling.k8s.io/v1",
    "kind": "VerticalPodAutoscaler",
    "name": "verticalpodautoscalers",
    "singularName": "verticalpodautoscaler",
    "namespaced": True,
}
#: A second one, so a test can show that one bad kind does not take the rest
#: of the sweep with it.
CRD: dict[str, Any] = {
    "version": "apiextensions.k8s.io/v1",
    "kind": "CustomResourceDefinition",
    "name": "customresourcedefinitions",
    "singularName": "customresourcedefinition",
    "namespaced": False,
}

KINDS = [("VerticalPodAutoscaler", "autoscaling.k8s.io/v1")]


class TestTheSweep:
    """One metadata LIST per kind. The request's shape is asserted by the API
    server double itself -- metadata-only and `resourceVersion=0` are the two
    measurements this sweep exists for, so a change to either fails there."""

    async def test_it_reads_what_the_three_consumers_need(self) -> None:
        api = FakeApi(resources=[VPA], listed=[("default", "VerticalPodAutoscaler", "vpa")])
        api.live[("default", "VerticalPodAutoscaler", "vpa")] |= {
            "annotations": {HASH_ANNOTATION: "sha256:abc"},
            "labels": {"ekn.dev/environment": ENV, "ekn.dev/deployment-unit": "core"},
            "managedFields": [{"manager": "ekn"}, {"manager": "kubectl-edit"}],
        }

        state = await sweep(api, KINDS)

        assert state == {
            ("default", "VerticalPodAutoscaler", "vpa"): LiveObject(
                key=("default", "VerticalPodAutoscaler", "vpa"),
                manifest_hash="sha256:abc",
                environment=ENV,
                unit="core",
                managers=frozenset({"ekn", "kubectl-edit"}),
                resource_version="1",
            )
        }

    async def test_the_key_carries_the_kind_that_was_listed(self) -> None:
        """Every item of a `PartialObjectMetadataList` reports `kind:
        PartialObjectMetadata`. Keyed by that, the sweep matches no manifest
        and the gate skips nothing, for ever, while looking healthy."""
        api = FakeApi(resources=[VPA], listed=[("default", "VerticalPodAutoscaler", "vpa")])

        state = await sweep(api, KINDS)

        assert [key[1] for key in state] == ["VerticalPodAutoscaler"]

    async def test_a_kind_the_cluster_does_not_serve_is_left_out(self) -> None:
        """On a first apply the CustomResourceDefinition that establishes a
        kind is in this same run, so the kind is not served yet. That is the
        ordinary state and not a failure."""
        api = FakeApi(resources=[VPA], listed=[("default", "VerticalPodAutoscaler", "vpa")])

        state = await sweep(api, [*KINDS, ("NeverServed", "livestate.test/v1")])

        assert list(state) == [("default", "VerticalPodAutoscaler", "vpa")]

    @pytest.mark.parametrize("status", [403, 404, 405])
    async def test_a_kind_that_cannot_be_listed_is_left_out(self, status: int) -> None:
        """403 is RBAC that covers writing a kind but not listing it; 404 and
        405 are the built-in kinds with no list verb. None of them is a reason
        to fail a run, and every object of such a kind is simply applied."""
        api = FakeApi(
            resources=[VPA, CRD],
            listed=[("default", "VerticalPodAutoscaler", "vpa")],
            deny={"CustomResourceDefinition": status},
        )

        state = await sweep(api, [*KINDS, ("CustomResourceDefinition", "apiextensions.k8s.io/v1")])

        assert list(state) == [("default", "VerticalPodAutoscaler", "vpa")]
        assert "CustomResourceDefinition" in api.swept

    async def test_another_status_is_not_swallowed(self) -> None:
        """A sweep that answered "nothing is skippable" to a broken API server
        would be indistinguishable from one that answered it correctly."""
        api = FakeApi(resources=[VPA], deny={"VerticalPodAutoscaler": 500})

        with pytest.raises(kr8s.ServerError):
            await sweep(api, KINDS)

    async def test_the_selector_narrows_it(self) -> None:
        """`ekn.dev/environment=<env>`: an object without it is not one `ekn`
        applied, and one cluster holds several environments."""
        api = FakeApi(
            resources=[VPA],
            listed=[("default", "VerticalPodAutoscaler", "mine"), ("default", "VerticalPodAutoscaler", "theirs")],
        )
        api.live[("default", "VerticalPodAutoscaler", "mine")]["labels"] = {"ekn.dev/environment": ENV}

        state = await sweep(api, KINDS, selector=f"ekn.dev/environment={ENV}")

        assert list(state) == [("default", "VerticalPodAutoscaler", "mine")]

    async def test_one_request_per_kind(self) -> None:
        """Whatever the kind appears as in the argument. A sweep is paid for
        once per kind, not once per object."""
        api = FakeApi(resources=[VPA], listed=[("default", "VerticalPodAutoscaler", f"vpa{n}") for n in range(5)])

        await sweep(api, [*KINDS, *KINDS])

        assert api.swept == ["VerticalPodAutoscaler"]
