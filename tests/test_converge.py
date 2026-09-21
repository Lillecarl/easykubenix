"""The converging apply: what it retries, what it refuses, and when it stops.

Every test here scripts the API server's answer rather than running one. The
classification is decided entirely by what an apply *raises*, so a fake
`Api` would only add a layer between the test and the branch it is about --
`converge_barrier` takes the apply step as a callable for that reason.

Several of these are negative controls, and each guards a failure that looks
healthy from outside: a retried 403 reports an RBAC error as a slow success,
an object that never applies must not vanish from the report, a recreate of
the wrong kind destroys data no re-apply brings back, and an apply that
*succeeds* against an object being deleted is the one failure the retry
ladder cannot see at all. Issue Lillecarl/easykubenix#28.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, ClassVar

import anyio
import anyio.lowlevel
import httpx
import kr8s
import pytest
from structlog.testing import capture_logs

from ekn.apply import KindNotServedError
from ekn.converge import (
    RECREATE_OPT_OUT_ANNOTATION,
    Disposition,
    _wait_before_retrying,
    classify,
    converge_barrier,
    converge_objects,
    converge_queue,
    diagnose,
    may_recreate,
    object_key,
    settled,
    sort_for_apply,
)

if TYPE_CHECKING:
    from ekn.apply import Manifest


@pytest.fixture(scope="module")
def anyio_backend() -> str:
    return "asyncio"


def applied_object(**metadata: Any) -> Any:
    """What a successful apply hands back: the object as the server returned
    it. Only `metadata` is read, and only to see whether the object this run
    just "applied" is on its way out -- see `_applied_but_terminating`."""
    return SimpleNamespace(raw={"metadata": metadata})


async def _no_sleep(_seconds: float) -> None:
    """A retry that waits for nothing, so a test of the queue's shape costs
    no wall-clock."""


def server_error(status: int, message: str = "nope", causes: list[str] | None = None) -> kr8s.ServerError:
    """A `kr8s.ServerError` carrying the `Status` body the API server sends."""
    body: dict[str, Any] = {"kind": "Status", "message": message}
    if causes is not None:
        body["details"] = {"causes": [{"message": cause} for cause in causes]}
    return kr8s.ServerError(
        message,
        response=httpx.Response(status_code=status, json=body, request=httpx.Request("PATCH", "http://api/x")),
    )


def server_error_with_reasons(status: int, message: str, causes: list[tuple[str, str]]) -> kr8s.ServerError:
    """A `Status` whose causes carry a `reason`, which is how an immutable
    StatefulSet update arrives -- its message holds no telling phrase."""
    body: dict[str, Any] = {
        "kind": "Status",
        "message": message,
        "details": {"causes": [{"reason": reason, "message": text} for reason, text in causes]},
    }
    return kr8s.ServerError(
        message,
        response=httpx.Response(status_code=status, json=body, request=httpx.Request("PATCH", "http://api/x")),
    )


def server_error_with_fields(
    status: int,
    message: str,
    *,
    reason: str | None = None,
    causes: list[tuple[str, str, str]] | None = None,
    retry_after: int | None = None,
) -> kr8s.ServerError:
    """A `Status` with everything the API server can attach to a failure:
    its machine-readable reason, per-cause field paths, and how long it is
    asking us to wait."""
    details: dict[str, Any] = {}
    if causes is not None:
        details["causes"] = [
            {"reason": cause_reason, "field": field_path, "message": text} for cause_reason, field_path, text in causes
        ]
    if retry_after is not None:
        details["retryAfterSeconds"] = retry_after
    body: dict[str, Any] = {"kind": "Status", "status": "Failure", "message": message, "details": details}
    if reason is not None:
        body["reason"] = reason
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

    def test_a_403_from_a_terminating_namespace_is_retried(self) -> None:
        """Captured on Kubernetes 1.36. The namespace finishes terminating,
        this run recreates it -- it is in the desired set -- and the object
        lands. Pruning makes this ordinary: prune deletes namespaces while
        applies are still in flight."""
        exc = server_error_with_fields(
            403,
            'configmaps "after-term" is forbidden: unable to create new content in '
            "namespace ekn-term because it is being terminated",
            reason="Forbidden",
            causes=[("NamespaceTerminating", "metadata.namespace", "namespace ekn-term is being terminated")],
        )

        assert classify(exc) is Disposition.RETRY

    def test_another_403_with_causes_is_still_terminal(self) -> None:
        """The narrow half of the rule. A 403 that carries causes but not
        this one must not ride in on the same branch -- the discriminator is
        the cause reason, not the presence of a `details` block."""
        exc = server_error_with_fields(
            403,
            "configmaps is forbidden: User cannot create resource",
            reason="Forbidden",
            causes=[("SomethingElse", "metadata.namespace", "no")],
        )

        assert classify(exc) is Disposition.TERMINAL

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
                return applied_object()
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
            return applied_object()

        failures = await converge_barrier([good, bad], apply=apply, settle_seconds=1.0)

        assert [f.key for f in failures] == [("default", "ConfigMap", "bad")]
        assert failures[0].disposition is Disposition.TERMINAL

    async def test_a_clean_barrier_reports_nothing(self) -> None:
        async def apply(_spec: Manifest) -> Any:
            return applied_object()

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
            return applied_object()

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
            return applied_object()

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
            return applied_object()

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
        sentinel = applied_object()

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


class TestTheCorpus:
    """The shapes a real API server actually returns.

    Captured from a live cluster by solid-kubernetes rather than invented
    here, because the first version of this table was invented and three of
    these did not match it. Issue Lillecarl/easykubenix#28.
    """

    def test_a_kind_the_server_does_not_serve_is_retried(self) -> None:
        """The most common retryable condition of a 188-CRD converge, and it
        is not an HTTP error: discovery answers 200 and lacks the kind, so
        the apply never reaches a PATCH. It used to land on TERMINAL."""
        exc = KindNotServedError(
            "the API server serves no CiliumNetworkPolicy in cilium.io/v2. "
            "A CustomResourceDefinition that establishes it has to be applied first.",
        )

        assert classify(exc) is Disposition.RETRY

    def test_an_ordinary_value_error_is_still_terminal(self) -> None:
        """`KindNotServedError` is a ValueError, so the narrow check matters."""
        assert classify(ValueError("a bug in ekn")) is Disposition.TERMINAL

    def test_a_service_cluster_ip_is_immutable(self) -> None:
        """ "may not change once set" is not "may not be changed". 57 Services
        in one render, so a table carrying only the second misses them all."""
        exc = server_error(
            422,
            'Service "traefik" is invalid',
            causes=['spec.clusterIPs[0]: Invalid value: []string{"10.43.0.1"}: may not change once set'],
        )

        assert classify(exc) is Disposition.RECREATE

    def test_a_statefulset_forbidden_update_is_immutable(self) -> None:
        """Carries neither phrase, and is recognised by its cause reason."""
        exc = server_error_with_reasons(
            422,
            'StatefulSet.apps "pynixd" is invalid',
            [
                (
                    "FieldValueForbidden",
                    "spec: Forbidden: updates to statefulset spec for fields other than 'replicas', "
                    "'ordinals', 'template', 'updateStrategy', 'persistentVolumeClaimRetentionPolicy' "
                    "and 'minReadySeconds' are forbidden",
                ),
            ],
        )

        assert classify(exc) is Disposition.RECREATE

    def test_a_forbidden_field_that_is_not_an_update_is_terminal(self) -> None:
        """Narrower than "every FieldValueForbidden" on purpose. That reason
        also covers a field that may not be set at all, which is a
        configuration error -- and a wrong RECREATE is a delete."""
        exc = server_error_with_reasons(
            422,
            'Pod "web" is invalid',
            [("FieldValueForbidden", "spec.nodeName: Forbidden: may not be set for this Pod")],
        )

        assert classify(exc) is Disposition.TERMINAL

    def test_the_job_message_is_matched_inside_a_long_go_dump(self) -> None:
        """The real `causes[0].message` for a Job is 2365 bytes of Go struct
        dump with the phrase at the end. A short fixture does not represent
        what the substring search runs over."""
        dump = "spec.template: Invalid value: core.PodTemplateSpec{" + ("Field:nil, " * 200) + "}: field is immutable"
        assert len(dump) > 2000
        exc = server_error(422, 'Job.batch "migrate" is invalid', causes=[dump])

        assert classify(exc) is Disposition.RECREATE


class TestStatefulSetVolumes:
    """A StatefulSet's PVCs outlive it only if something says so."""

    def statefulset(self, *, templates: bool, when_deleted: str | None = None) -> Manifest:
        spec: dict[str, Any] = {}
        if templates:
            spec["volumeClaimTemplates"] = [{"metadata": {"name": "data"}}]
        if when_deleted is not None:
            spec["persistentVolumeClaimRetentionPolicy"] = {"whenDeleted": when_deleted}
        return {
            "apiVersion": "apps/v1",
            "kind": "StatefulSet",
            "metadata": {"name": "db", "namespace": "default"},
            "spec": spec,
        }

    def test_no_volume_claim_templates_is_allowed(self) -> None:
        assert may_recreate(self.statefulset(templates=False))

    def test_an_explicit_retain_is_allowed(self) -> None:
        assert may_recreate(self.statefulset(templates=True, when_deleted="Retain"))

    def test_saying_nothing_is_refused(self) -> None:
        """The API default is Retain, so nothing is lost today. It is a
        default, and a chart bump can set Delete with nobody reading the
        diff -- so the policy has to be written down, not inferred."""
        assert not may_recreate(self.statefulset(templates=True))

    def test_delete_is_refused(self) -> None:
        assert not may_recreate(self.statefulset(templates=True, when_deleted="Delete"))

    async def test_the_refusal_says_what_to_do(self) -> None:
        async def apply(_spec: Manifest) -> Any:
            raise server_error(422, causes=["spec: field is immutable"])

        async def delete(_spec: Manifest) -> None:
            return

        failures = await converge_barrier(
            [self.statefulset(templates=True)],
            apply=apply,
            delete=delete,
            allow_recreate=True,
            settle_seconds=1.0,
        )

        assert "whenDeleted = Retain" in failures[0].error


class TestTheQueue:
    """Helm order, N at a time, and what a failure carries with it."""

    PRIORITY: ClassVar[dict[str, int]] = {"Namespace": 10, "CustomResourceDefinition": 20, "Deployment": 100}

    def test_it_sorts_by_helm_order(self) -> None:
        specs = [
            manifest(kind="Deployment", name="web"),
            manifest(kind="Namespace", name="ns"),
            manifest(kind="CustomResourceDefinition", name="crd"),
        ]

        order = [object_key(spec)[1] for spec in sort_for_apply(specs, self.PRIORITY)]

        assert order == ["Namespace", "CustomResourceDefinition", "Deployment"]

    def test_an_unnumbered_kind_is_not_last(self) -> None:
        """`DEFAULT_BARRIER_PRIORITY` is 1000 and sits above Helm's range but
        below the webhook configurations. See that constant for the six
        minutes of stalled apply that bought the rule."""
        specs = [
            manifest(kind="ValidatingWebhookConfiguration", name="hook"),
            manifest(kind="CiliumNetworkPolicy", name="cnp"),
        ]
        priority = {"ValidatingWebhookConfiguration": 1010}

        order = [object_key(spec)[1] for spec in sort_for_apply(specs, priority)]

        assert order == ["CiliumNetworkPolicy", "ValidatingWebhookConfiguration"]

    async def test_it_applies_several_at_once(self) -> None:
        in_flight = [0]
        peak = [0]

        async def apply(_spec: Manifest) -> Any:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
            await anyio.lowlevel.checkpoint()
            in_flight[0] -= 1
            return applied_object()

        report = await converge_queue(
            [manifest(name=f"cm{i}") for i in range(20)],
            apply=apply,
            concurrency=4,
        )

        assert report.applied == 20
        assert report.ok
        assert peak[0] > 1, "nothing ran concurrently"
        assert peak[0] <= 4

    async def test_concurrency_of_one_is_serial(self) -> None:
        peak = [0]
        in_flight = [0]

        async def apply(_spec: Manifest) -> Any:
            in_flight[0] += 1
            peak[0] = max(peak[0], in_flight[0])
            await anyio.lowlevel.checkpoint()
            in_flight[0] -= 1
            return applied_object()

        await converge_queue([manifest(name=f"cm{i}") for i in range(5)], apply=apply, concurrency=1)

        assert peak[0] == 1

    async def test_it_names_each_kind_as_it_reaches_it(self) -> None:
        """The only thing a converging run says while it is running.

        It exists to be lined up against a measurement taken beside the
        run -- on nixlab2 an apiserver gained 982 MiB inside one sampling
        window and nothing in the log said which objects were in flight.
        So what this pins is that every kind is named, once, with how many
        of it there are.
        """

        async def apply(_spec: Manifest) -> Any:
            return applied_object()

        specs = [
            *[manifest(kind="CustomResourceDefinition", name=f"crd{i}") for i in range(3)],
            *[manifest(kind="ConfigMap", name=f"cm{i}") for i in range(2)],
        ]
        with capture_logs() as logs:
            report = await converge_queue(specs, apply=apply, concurrency=1)

        assert report.applied == 5
        said = [entry for entry in logs if entry["event"] == "converging kind"]
        assert [(entry["kind"], entry["objects"]) for entry in said] == [
            ("CustomResourceDefinition", 3),
            ("ConfigMap", 2),
        ], "each kind once, in the order the queue reached it, with its count"


class TestFastMode:
    """The skip check, which is the whole of `--assume-unchanged` here."""

    async def test_an_unchanged_object_is_not_applied(self) -> None:
        applied: list[str] = []

        async def apply(spec: Manifest) -> Any:
            applied.append(object_key(spec)[2])
            return applied_object()

        async def should_skip(spec: Manifest) -> bool:
            return object_key(spec)[2] == "same"

        report = await converge_queue(
            [manifest(name="same"), manifest(name="changed")],
            apply=apply,
            should_skip=should_skip,
        )

        assert applied == ["changed"]
        assert report.skipped == 1
        assert report.applied == 1

    async def test_without_a_skip_check_everything_is_applied(self) -> None:
        applied: list[str] = []

        async def apply(spec: Manifest) -> Any:
            applied.append(object_key(spec)[2])
            return applied_object()

        report = await converge_queue([manifest(name="a"), manifest(name="b")], apply=apply)

        assert sorted(applied) == ["a", "b"]
        assert report.skipped == 0


class TestTheDiagnosis:
    """What a failed apply can be asked, beyond its message."""

    def test_it_names_the_offending_field(self) -> None:
        """The single most useful fact in a rejected manifest, and the one
        thing that stops a person going back to the cluster to ask."""
        exc = server_error_with_fields(
            422,
            'Deployment.apps "web" is invalid',
            reason="Invalid",
            causes=[("FieldValueInvalid", "spec.replicas", "must be greater than or equal to 0")],
        )

        found = diagnose(exc)

        assert found.status_code == 422
        assert found.reason == "Invalid"
        assert found.causes[0].field_path == "spec.replicas"
        assert "field=spec.replicas" in found.summary()
        assert "reason=Invalid" in found.summary()

    def test_it_reads_the_servers_own_retry_delay(self) -> None:
        """`retryAfterSeconds` comes with a 429 and with a 503 from a
        Timeout. It is the API server saying how long it needs."""
        exc = server_error_with_fields(429, "too many requests", reason="TooManyRequests", retry_after=7)

        assert diagnose(exc).retry_after == 7.0

    def test_a_plain_exception_still_diagnoses(self) -> None:
        found = diagnose(ValueError("a bug in ekn"))

        assert found.message == "a bug in ekn"
        assert found.status_code is None
        assert found.causes == ()

    async def test_the_failure_carries_it(self) -> None:
        async def apply(_spec: Manifest) -> Any:
            raise server_error_with_fields(
                422,
                'Deployment.apps "web" is invalid',
                reason="Invalid",
                causes=[("FieldValueInvalid", "spec.replicas", "must be >= 0")],
            )

        report = await converge_queue([manifest(kind="Deployment", name="web")], apply=apply, settle_seconds=1.0)

        failure = report.failures[0]
        assert failure.diagnosis is not None
        assert failure.diagnosis.causes[0].field_path == "spec.replicas"
        assert "field=spec.replicas" in failure.error

    async def test_it_counts_the_attempts(self) -> None:
        """A report saying an object failed once and one saying it failed
        eleven times are different reports."""
        now = [0.0]

        async def apply(_spec: Manifest) -> Any:
            raise server_error(503, "still starting")

        async def sleep(seconds: float) -> None:
            now[0] += seconds

        report = await converge_queue(
            [manifest()],
            apply=apply,
            settle_seconds=10.0,
            clock=lambda: now[0],
            sleep=sleep,
        )

        assert report.failures[0].attempts > 1


class TestAnApplyThatSucceedsAndDoesNotConverge:
    """Applying an object that is being deleted returns 200, not an error.

    Measured on Kubernetes 1.36: a namespace in `phase=Terminating` answers
    `serverside-applied` with a `Warning:` header and exit 0, and still goes
    away. The retry ladder cannot see it, because nothing failed. This is
    the hole that makes a run end non-zero while naming the wrong object.
    """

    async def test_it_is_re_queued_rather_than_counted_as_applied(self) -> None:
        attempts = [0]

        async def apply(_spec: Manifest) -> Any:
            attempts[0] += 1
            # Gone by the second attempt, which is what happens on a cluster:
            # the namespace finishes terminating and this run recreates it.
            if attempts[0] == 1:
                return applied_object(name="ekn-term", deletionTimestamp="2026-09-17T10:25:19Z")
            return applied_object(name="ekn-term")

        report = await converge_queue([manifest(kind="Namespace", name="ekn-term")], apply=apply, sleep=_no_sleep)

        assert attempts[0] == 2
        assert report.applied == 1
        assert report.ok

    async def test_a_run_that_never_converges_names_the_terminating_object(self) -> None:
        """The third control. Left uncaught, the settle window expires on the
        *dependent* object's 404 and the report blames that, never the
        namespace whose apply "succeeded" and was undone."""
        now = [0.0]

        async def apply(_spec: Manifest) -> Any:
            return applied_object(name="ekn-term", deletionTimestamp="2026-09-17T10:25:19Z")

        async def sleep(seconds: float) -> None:
            now[0] += seconds

        report = await converge_queue(
            [manifest(kind="Namespace", name="ekn-term")],
            apply=apply,
            settle_seconds=10.0,
            clock=lambda: now[0],
            sleep=sleep,
        )

        assert not report.ok
        assert report.applied == 0
        failure = report.failures[0]
        assert failure.key == ("default", "Namespace", "ekn-term")
        assert "being deleted" in failure.error
        assert failure.diagnosis is not None
        assert failure.diagnosis.causes[0].field_path == "metadata.deletionTimestamp"

    async def test_an_ordinary_apply_is_still_progress(self) -> None:
        """Negative control: the check reads `deletionTimestamp` and nothing
        else, so an object without one must not be re-queued for ever."""

        async def apply(_spec: Manifest) -> Any:
            return applied_object(name="ekn-term")

        report = await converge_queue([manifest(kind="Namespace", name="ekn-term")], apply=apply)

        assert report.applied == 1
        assert report.ok


class TestTheRetryDelay:
    """The server's own answer beats our backoff curve."""

    def test_the_curve_is_used_when_the_server_says_nothing(self) -> None:
        assert _wait_before_retrying(0, None) == 1.0
        assert _wait_before_retrying(1, None) == 2.0

    def test_the_server_wins_when_it_asks_for_longer(self) -> None:
        assert _wait_before_retrying(0, 30.0) == 30.0

    def test_the_curve_wins_when_it_is_longer(self) -> None:
        assert _wait_before_retrying(4, 2.0) == 15.0

    def test_the_cap_does_not_apply_to_an_instruction(self) -> None:
        """`MAX_BACKOFF_SECONDS` caps our guess. Capping the API server's
        instruction is just ignoring it more politely."""
        assert _wait_before_retrying(0, 120.0) == 120.0
