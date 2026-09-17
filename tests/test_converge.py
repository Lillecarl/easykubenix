"""The converging apply: what it retries, what it refuses, and when it stops.

Every test here scripts the API server's answer rather than running one. The
classification is decided entirely by what an apply *raises*, so a fake
`Api` would only add a layer between the test and the branch it is about --
`converge_barrier` takes the apply step as a callable for that reason.

Three of these are negative controls, and each guards a failure that looks
healthy from outside: a retried 403 reports an RBAC error as a slow success,
an object that never applies must not vanish from the report, and a recreate
of the wrong kind destroys data no re-apply brings back.
Issue Lillecarl/easykubenix#28.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import httpx
import kr8s
import pytest

from ekn.converge import (
    RECREATE_OPT_OUT_ANNOTATION,
    Disposition,
    classify,
    converge_barrier,
    converge_objects,
    may_recreate,
    object_key,
    settled,
)

if TYPE_CHECKING:
    from ekn.apply import Manifest


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def server_error(status: int, message: str = "nope", causes: list[str] | None = None) -> kr8s.ServerError:
    """A `kr8s.ServerError` carrying the `Status` body the API server sends."""
    body: dict[str, Any] = {"kind": "Status", "message": message}
    if causes is not None:
        body["details"] = {"causes": [{"message": cause} for cause in causes]}
    return kr8s.ServerError(
        message,
        response=httpx.Response(status_code=status, json=body, request=httpx.Request("PATCH", "http://api/x")),
    )


def manifest(kind: str = "ConfigMap", name: str = "a", namespace: str = "default", **metadata: Any) -> Manifest:
    return {
        "apiVersion": "v1",
        "kind": kind,
        "metadata": {"name": name, "namespace": namespace, **metadata},
    }


class TestClassify:
    """Which answers mean "wait" and which mean "stop"."""

    @pytest.mark.parametrize("status", [404, 409, 429, 500, 502, 503, 504])
    def test_the_cluster_has_not_caught_up(self, status: int) -> None:
        assert classify(server_error(status)) is Disposition.RETRY

    def test_a_403_is_never_retried(self) -> None:
        """Negative control. Retrying an RBAC refusal for the settle window
        turns a clear failure into a slow one, and the run still fails."""
        assert classify(server_error(403, "forbidden")) is Disposition.TERMINAL

    def test_a_schema_error_is_terminal(self) -> None:
        """A 422 that is not about immutability does not become true by
        waiting."""
        assert classify(server_error(422, causes=["spec.replicas: Invalid value: -1"])) is Disposition.TERMINAL

    def test_an_immutable_field_asks_for_a_recreate(self) -> None:
        """The completed-Job case: `spec.template` is immutable, so every
        apply after the first fails forever."""
        assert classify(server_error(422, causes=["spec.template: field is immutable"])) is Disposition.RECREATE

    def test_a_transport_failure_is_retried(self) -> None:
        """A webhook that is not answering yet, rather than one that refused."""
        assert classify(kr8s.ServerError("connection refused")) is Disposition.RETRY
        assert classify(TimeoutError()) is Disposition.RETRY

    def test_an_unknown_exception_is_terminal(self) -> None:
        """Nothing retries by default. An error this table does not know is
        far more likely to be a defect here than a cluster catching up."""
        assert classify(ValueError("a bug in ekn")) is Disposition.TERMINAL


class TestTheSettleTimer:
    """ "No progress for N seconds", not "N seconds after the queue emptied"."""

    def test_it_measures_from_the_last_success(self) -> None:
        assert not settled(last_progress=100.0, now=150.0, settle_seconds=60.0)
        assert settled(last_progress=100.0, now=161.0, settle_seconds=60.0)

    async def test_progress_keeps_a_slow_barrier_open(self) -> None:
        """A barrier where one object succeeds per round is making progress,
        and must not be cut off by a deadline set when the queue emptied."""
        now = [0.0]
        remaining = [manifest(name=f"cm{i}") for i in range(5)]
        applied: list[str] = []

        async def apply(spec: Manifest) -> Any:
            # One succeeds per pass; the rest report the cluster catching up.
            if spec is remaining[len(applied)]:
                applied.append(object_key(spec)[2])
                return object()
            raise server_error(404)

        async def sleep(seconds: float) -> None:
            now[0] += seconds

        failures = await converge_barrier(
            remaining,
            apply=apply,
            settle_seconds=10.0,
            clock=lambda: now[0],
            sleep=sleep,
        )

        assert failures == []
        assert len(applied) == 5

    async def test_a_barrier_that_never_progresses_gives_up(self) -> None:
        now = [0.0]

        async def apply(_spec: Manifest) -> Any:
            raise server_error(503, "still starting")

        async def sleep(seconds: float) -> None:
            now[0] += seconds

        failures = await converge_barrier(
            [manifest()],
            apply=apply,
            settle_seconds=5.0,
            clock=lambda: now[0],
            sleep=sleep,
        )

        assert [f.key for f in failures] == [("default", "ConfigMap", "a")]
        assert "still starting" in failures[0].error


class TestTheReport:
    async def test_a_permanently_failing_object_is_reported(self) -> None:
        """Negative control. The whole interface of a converging run is
        "what did not apply" -- an object that silently vanishes from the
        report is a half-applied cluster called green."""
        good, bad = manifest(name="good"), manifest(name="bad")

        async def apply(spec: Manifest) -> Any:
            if object_key(spec)[2] == "bad":
                raise server_error(403, "forbidden")
            return object()

        failures = await converge_barrier([good, bad], apply=apply, settle_seconds=1.0)

        assert [f.key for f in failures] == [("default", "ConfigMap", "bad")]
        assert failures[0].disposition is Disposition.TERMINAL

    async def test_a_clean_barrier_reports_nothing(self) -> None:
        async def apply(_spec: Manifest) -> Any:
            return object()

        assert await converge_barrier([manifest()], apply=apply) == []

    async def test_a_terminal_object_is_not_retried(self) -> None:
        """A 403 costs exactly one call, however long the barrier runs."""
        calls = [0]

        async def apply(_spec: Manifest) -> Any:
            calls[0] += 1
            raise server_error(403)

        await converge_barrier([manifest()], apply=apply, settle_seconds=1.0)

        assert calls[0] == 1


class TestRecreate:
    """A delete is the one action here that no later run undoes."""

    IMMUTABLE = "spec.template: field is immutable"

    async def test_it_is_refused_without_the_flag(self) -> None:
        deleted: list[str] = []

        async def apply(_spec: Manifest) -> Any:
            raise server_error(422, causes=[self.IMMUTABLE])

        async def delete(spec: Manifest) -> None:
            deleted.append(object_key(spec)[2])

        failures = await converge_barrier(
            [manifest(kind="Job", name="migrate")],
            apply=apply,
            delete=delete,
            settle_seconds=1.0,
        )

        assert deleted == []
        assert failures[0].disposition is Disposition.TERMINAL
        assert "--allow-recreate" in failures[0].error

    async def test_it_deletes_and_applies_again_with_the_flag(self) -> None:
        deleted: list[str] = []
        calls = [0]

        async def apply(_spec: Manifest) -> Any:
            calls[0] += 1
            if calls[0] == 1:
                raise server_error(422, causes=[self.IMMUTABLE])
            return object()

        async def delete(spec: Manifest) -> None:
            deleted.append(object_key(spec)[2])

        failures = await converge_barrier(
            [manifest(kind="Job", name="migrate")],
            apply=apply,
            delete=delete,
            allow_recreate=True,
            settle_seconds=1.0,
        )

        assert deleted == ["migrate"]
        assert failures == []

    @pytest.mark.parametrize(
        "kind",
        ["PersistentVolumeClaim", "Secret", "Namespace", "CustomResourceDefinition"],
    )
    async def test_a_denied_kind_is_never_deleted(self, kind: str) -> None:
        """Negative control. Each of these loses data that no re-apply brings
        back, so an immutable-field error on one is a fault to report."""
        deleted: list[str] = []

        async def apply(_spec: Manifest) -> Any:
            raise server_error(422, causes=[self.IMMUTABLE])

        async def delete(spec: Manifest) -> None:
            deleted.append(object_key(spec)[2])

        failures = await converge_barrier(
            [manifest(kind=kind, name="precious")],
            apply=apply,
            delete=delete,
            allow_recreate=True,
            settle_seconds=1.0,
        )

        assert deleted == []
        assert failures[0].disposition is Disposition.TERMINAL
        assert "never recreated" in failures[0].error

    def test_the_opt_out_annotation_keeps_recreate_away(self) -> None:
        assert may_recreate(manifest(kind="Job"))
        assert not may_recreate(manifest(kind="Job", annotations={RECREATE_OPT_OUT_ANNOTATION: "true"}))


class TestBarrierWalking:
    """Barriers are an ordering, so an unfinished one is a decision point."""

    async def test_an_unfinished_barrier_stops_the_run(self) -> None:
        """Without `--keep-going`. Carrying on applies objects whose
        prerequisites are known to be missing, and the second wave of
        failures says nothing about the first."""
        attempted: list[str] = []

        async def apply(spec: Manifest) -> Any:
            name = object_key(spec)[2]
            attempted.append(name)
            if name == "first":
                raise server_error(403)
            return object()

        failures = await converge_objects(
            [[manifest(name="first")], [manifest(name="second")]],
            apply=apply,
            settle_seconds=1.0,
        )

        assert attempted == ["first"]
        assert [f.key[2] for f in failures] == ["first"]

    async def test_keep_going_reaches_the_later_barriers(self) -> None:
        attempted: list[str] = []

        async def apply(spec: Manifest) -> Any:
            name = object_key(spec)[2]
            attempted.append(name)
            if name == "first":
                raise server_error(403)
            return object()

        failures = await converge_objects(
            [[manifest(name="first")], [manifest(name="second")]],
            apply=apply,
            keep_going=True,
            settle_seconds=1.0,
        )

        assert attempted == ["first", "second"]
        assert [f.key[2] for f in failures] == ["first"]

    async def test_the_hook_sees_what_the_barrier_applied(self) -> None:
        """`after_barrier` is how a caller waits for CRDs to be Established
        without this module knowing what a CRD is."""
        seen: list[int] = []
        sentinel = object()

        async def apply(_spec: Manifest) -> Any:
            return sentinel

        async def after_barrier(applied: Any) -> None:
            seen.append(len(applied))

        await converge_objects(
            [[manifest(name="a"), manifest(name="b")], [manifest(name="c")]],
            apply=apply,
            after_barrier=after_barrier,
        )

        assert seen == [2, 1]


class TestTheRecreateRace:
    """Delete-then-create races itself unless the delete waits.

    A delete that returns when the API server accepted it lets the create
    reach the API server first, which answers `409 AlreadyExists`. That
    classifies as RETRY, so the run converges anyway -- which is why the
    defect shows up as a slow barrier and a puzzling log instead of an
    error. Named here so it reads as what it is.
    """

    IMMUTABLE = "spec.template: field is immutable"

    async def test_a_409_after_the_delete_names_the_race(self) -> None:
        calls = [0]

        async def apply(_spec: Manifest) -> Any:
            calls[0] += 1
            if calls[0] == 1:
                raise server_error(422, causes=[self.IMMUTABLE])
            raise server_error(409, "object is being deleted")

        async def delete(_spec: Manifest) -> None:
            return

        failures = await converge_barrier(
            [manifest(kind="Job", name="migrate")],
            apply=apply,
            delete=delete,
            allow_recreate=True,
            settle_seconds=1.0,
        )

        assert failures[0].disposition is Disposition.TERMINAL
        assert "raced" in failures[0].error
        assert "poll until the object is absent" in failures[0].error

    async def test_a_failed_delete_says_so(self) -> None:
        async def apply(_spec: Manifest) -> Any:
            raise server_error(422, causes=[self.IMMUTABLE])

        async def delete(_spec: Manifest) -> None:
            raise server_error(403, "forbidden")

        failures = await converge_barrier(
            [manifest(kind="Job", name="migrate")],
            apply=apply,
            delete=delete,
            allow_recreate=True,
            settle_seconds=1.0,
        )

        assert "deleting it for a recreate" in failures[0].error
