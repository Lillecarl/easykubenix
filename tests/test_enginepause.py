"""Stopping the GitOps engine, and being able to start it again.

Every test here is about one property: a pause that cannot be resumed is
worse than no pause, because it leaves a cluster with no reconciler and
nobody watching. Issue Lillecarl/easykubenix#28.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest

from ekn.enginepause import (
    PAUSED_REPLICAS_ANNOTATION,
    EnginePauseError,
    Paused,
    Workload,
    object_keys,
    pause,
    resume_reminder,
    without_pause_targets,
)


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


class FakeWorkload:
    """One Deployment/StatefulSet, as `enginepause` touches it."""

    def __init__(self, namespace: str, name: str, replicas: int, annotations: dict[str, str] | None = None) -> None:
        self.raw: dict[str, Any] = {
            "metadata": {"namespace": namespace, "name": name, "annotations": dict(annotations or {})},
            "spec": {"replicas": replicas},
        }
        self.scaled_to: list[int] = []

    async def async_scale(self, replicas: int) -> None:
        self.scaled_to.append(replicas)
        self.raw["spec"]["replicas"] = replicas

    async def async_annotate(self, annotations: dict[str, str | None]) -> None:
        current = self.raw["metadata"]["annotations"]
        for key, value in annotations.items():
            if value is None:
                # kr8s sends a strategic-merge patch, where null removes it.
                current.pop(key, None)
            else:
                current[key] = value

    @property
    def replicas(self) -> int:
        return self.raw["spec"]["replicas"]

    @property
    def annotations(self) -> dict[str, str]:
        return self.raw["metadata"]["annotations"]


class FakeApi:
    """Resolves a workload by (kind, namespace, name), or raises NotFound."""

    def __init__(self, objects: dict[tuple[str, str, str], FakeWorkload]) -> None:
        self.objects = objects


@pytest.fixture
def patched(monkeypatch: pytest.MonkeyPatch):
    """Route `_fetch` at the fake cluster.

    `_fetch` is patched rather than kr8s' classes, because kr8s resolves a
    class method against a live `Api` and this suite is about the pause
    logic, not about kr8s.
    """
    import ekn.enginepause as ep

    async def _fetch(api: Any, workload: Workload) -> Any:
        found = api.objects.get((workload.kind, workload.namespace, workload.name))
        if found is None:
            msg = f"{workload} does not exist, so the engine cannot be paused by scaling it"
            raise EnginePauseError(msg)
        return found

    monkeypatch.setattr(ep, "_fetch", _fetch)
    return ep


CONTROLLER = Workload(namespace="argocd", name="argo-cd-argocd-application-controller", kind="StatefulSet")


def _cluster(replicas: int = 1, annotations: dict[str, str] | None = None) -> tuple[FakeApi, FakeWorkload]:
    obj = FakeWorkload(CONTROLLER.namespace, CONTROLLER.name, replicas, annotations)
    return FakeApi({(CONTROLLER.kind, CONTROLLER.namespace, CONTROLLER.name): obj}), obj


class TestPause:
    async def test_it_records_the_count_before_scaling(self, patched: Any) -> None:
        """The order is load-bearing. Annotating after scaling leaves a window
        where a killed `ekn` has stopped the engine and left nothing saying
        how to start it again."""
        api, obj = _cluster(replicas=3)

        result = await patched.pause([CONTROLLER], api=api)

        assert obj.annotations[PAUSED_REPLICAS_ANNOTATION] == "3"
        assert obj.replicas == 0
        assert result == [Paused(CONTROLLER, 3)]

    async def test_pausing_twice_does_not_record_zero(self, patched: Any) -> None:
        """The failure this module exists to avoid, reached by running the
        tool twice: the second pause observes 0 replicas, and recording that
        would make the resume restore zero -- a paused engine reporting
        itself successfully resumed."""
        api, obj = _cluster(replicas=0, annotations={PAUSED_REPLICAS_ANNOTATION: "3"})

        result = await patched.pause([CONTROLLER], api=api)

        assert obj.annotations[PAUSED_REPLICAS_ANNOTATION] == "3"
        assert result == [Paused(CONTROLLER, 3, already=True)]

    async def test_a_missing_workload_is_an_error(self, patched: Any) -> None:
        """A pause that silently did nothing would let the apply run against
        a live engine believing it was stopped."""
        with pytest.raises(EnginePauseError, match="does not exist"):
            await patched.pause([CONTROLLER], api=FakeApi({}))

    async def test_an_unscalable_kind_is_refused(self) -> None:
        with pytest.raises(EnginePauseError, match="Deployment, StatefulSet"):
            await pause([Workload("argocd", "whatever", kind="CronJob")], api=FakeApi({}))  # type: ignore[arg-type]


class TestResume:
    async def test_it_restores_and_clears_the_annotation(self, patched: Any) -> None:
        api, obj = _cluster(replicas=0, annotations={PAUSED_REPLICAS_ANNOTATION: "2"})

        failed = await patched.resume([Paused(CONTROLLER, 2)], api=api)

        assert failed == []
        assert obj.replicas == 2
        assert PAUSED_REPLICAS_ANNOTATION not in obj.annotations

    async def test_one_failure_does_not_abandon_the_rest(self, patched: Any) -> None:
        """A partly-resumed engine is the worst outcome, so each workload is
        independent and every failure is reported rather than raised."""
        other = Workload(namespace="argocd", name="argocd-repo-server")
        api, obj = _cluster(replicas=0, annotations={PAUSED_REPLICAS_ANNOTATION: "1"})

        failed = await patched.resume([Paused(other, 1), Paused(CONTROLLER, 1)], api=api)

        assert len(failed) == 1
        assert "argocd-repo-server" in failed[0]
        assert obj.replicas == 1


class TestStillPaused:
    async def test_it_finds_an_engine_an_earlier_run_left_down(self, patched: Any) -> None:
        """What makes the crash-safety usable: a later run finds the stopped
        engine without being told."""
        api, _ = _cluster(replicas=0, annotations={PAUSED_REPLICAS_ANNOTATION: "1"})

        assert await patched.still_paused([CONTROLLER], api=api) == [Paused(CONTROLLER, 1, already=True)]

    async def test_a_running_engine_is_not_reported(self, patched: Any) -> None:
        api, _ = _cluster(replicas=1)

        assert await patched.still_paused([CONTROLLER], api=api) == []


class TestTheEngineIsInTheApplySet:
    """Measured on nixlab2: all six ArgoCD workloads are in
    `kubernetes.generated` with `replicas: 1`, including the controller the
    pause scales to zero. Applying that set wakes the engine up in the middle
    of the apply that paused it.
    """

    CONTROLLER_SPEC: ClassVar[dict[str, Any]] = {
        "apiVersion": "apps/v1",
        "kind": "StatefulSet",
        "metadata": {"namespace": "argocd", "name": CONTROLLER.name},
        "spec": {"replicas": 1},
    }
    OTHER_SPEC: ClassVar[dict[str, Any]] = {
        "apiVersion": "v1",
        "kind": "ConfigMap",
        "metadata": {"namespace": "argocd", "name": "unrelated"},
    }

    def test_the_pause_target_is_not_applied(self) -> None:
        kept = without_pause_targets([self.CONTROLLER_SPEC, self.OTHER_SPEC], [CONTROLLER])

        assert [o["metadata"]["name"] for o in kept] == ["unrelated"]

    def test_everything_else_is_still_applied(self) -> None:
        """Negative control. Excluding too much would make a full deploy
        quietly stop deploying the namespace its engine lives in."""
        kept = without_pause_targets([self.OTHER_SPEC], [CONTROLLER])

        assert kept == [self.OTHER_SPEC]

    def test_only_the_statefulset_is_excluded_of_seven_objects_sharing_its_name(self) -> None:
        """Measured on nixlab2: seven generated objects carry the controller's
        name, and only one is the pause target. Two are cluster-scoped, so the
        `"none"` half of the key is load-bearing here rather than theoretical.

        A match on name, or on namespace and name, would silently drop the
        engine's own RBAC from every full apply -- so a change to it would
        never be delivered, and nothing would say so.
        """
        same_name = [
            {"kind": "NetworkPolicy", "metadata": {"namespace": "argocd", "name": CONTROLLER.name}},
            {"kind": "Role", "metadata": {"namespace": "argocd", "name": CONTROLLER.name}},
            {"kind": "RoleBinding", "metadata": {"namespace": "argocd", "name": CONTROLLER.name}},
            {"kind": "ServiceMonitor", "metadata": {"namespace": "argocd", "name": CONTROLLER.name}},
            self.CONTROLLER_SPEC,
            {"kind": "ClusterRole", "metadata": {"name": CONTROLLER.name}},
            {"kind": "ClusterRoleBinding", "metadata": {"name": CONTROLLER.name}},
        ]

        kept = without_pause_targets(same_name, [CONTROLLER])

        assert len(kept) == len(same_name) - 1
        assert [o["kind"] for o in kept] == [
            "NetworkPolicy",
            "Role",
            "RoleBinding",
            "ServiceMonitor",
            "ClusterRole",
            "ClusterRoleBinding",
        ]

    def test_the_pause_target_is_protected_from_the_prune(self) -> None:
        """The other half, and skipping alone is worse without it: an object
        absent from the desired set is one a prune deletes, so skipping alone
        turns "the engine un-pauses itself" into "the apply deletes its own
        engine"."""
        assert object_keys([CONTROLLER]) == {("argocd", "StatefulSet", CONTROLLER.name)}

    def test_the_protect_key_matches_the_key_the_prune_builds(self) -> None:
        """`prune_generation` keys objects `(namespace or "none", kind,
        name)`. A `protect` entry in any other shape silently protects
        nothing."""
        from ekn.apply import _object_key

        class _Obj:
            namespace = "argocd"
            kind = "StatefulSet"
            name = CONTROLLER.name

        assert _object_key(_Obj()) in object_keys([CONTROLLER])  # type: ignore[arg-type]


def test_the_reminder_names_every_workload_and_its_count() -> None:
    message = resume_reminder([Paused(CONTROLLER, 3)])

    assert CONTROLLER.name in message
    assert "3 replicas" in message
    assert PAUSED_REPLICAS_ANNOTATION in message
