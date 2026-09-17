"""The kr8s half of a converging apply: what `converge` takes as callables.

`converge` names its steps as Protocols and takes them injected, so every
decision it makes is testable without an API server. This module is the
other end of those seams -- the real `Api`-backed implementations, and the
one entry point `ekn kubeapply --converge` calls.

Split out rather than put in `converge` so that module keeps no `Api` of its
own, and rather than put in `apply` so the barrier apply and the converging
apply do not grow into each other. They answer different questions: `apply`
applies a generation and prunes what it dropped, this converges a set.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import structlog

from .apply import (
    DEFAULT_ENVIRONMENT_LABEL,
    DEFAULT_FIELD_MANAGER,
    build_object,
    ssa_apply,
    with_environment_label,
)
from .converge import DEFAULT_CONCURRENCY, DEFAULT_SETTLE_SECONDS, ConvergeReport, converge_queue

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from kr8s._api import Api
    from kr8s.asyncio.objects import APIObject

    from .apply import Manifest

_log = structlog.get_logger()

DELETE_TIMEOUT_SECONDS = 120.0
"""How long a recreate waits for the object to actually be gone.

Not unbounded. A finalizer that never runs would otherwise hold one worker
for the whole run with nothing saying why -- and a recreate is only reached
for an object this apply has already decided it must delete.
"""


def _applier(
    api: Api,
    *,
    environment: str,
    field_manager: str,
    environment_label: str,
    desired: dict[tuple[str, str, str], type[APIObject]],
):
    """The apply step, recording what it built into `desired` for the prune.

    Recorded after `build_object` and **before** the apply, for two reasons.
    The key has to come from the built object -- a namespaced manifest that
    names no namespace resolves to the API's default namespace, and a key
    taken from the manifest would read `none` where the prune scan reads
    `default`. And an object that never converges still belongs in the
    desired set: absence from it means "removed from the configuration",
    which is answered with a delete, and a failed apply is not a removal.
    """

    async def apply(spec: Manifest) -> APIObject:
        obj = await build_object(
            with_environment_label(spec, environment_label, environment),
            api,
        )
        desired[(obj.namespace or "none", obj.kind, obj.name)] = type(obj)
        await ssa_apply(obj, field_manager=field_manager)
        return obj

    return apply


def _deleter(api: Api):
    """A delete that does not return until the object is gone.

    `DeleteOne` requires this, and the requirement is not pedantry: a delete
    that returns while the object still exists lets the recreate's apply race
    it, and the `409 AlreadyExists` that follows reads as an ordinary
    conflict. Waiting here is what keeps that failure from being reported as
    a slow barrier somewhere else.
    """

    async def delete(spec: Manifest) -> None:
        obj = await build_object(spec, api)
        await obj.async_delete()
        try:
            # kr8s' own watch-based wait, not a poll: it returns as soon as the
            # object is gone and issues no request in between. `"delete"` is a
            # condition kr8s handles specially -- a `NotFoundError` on the
            # first refresh is the success case.
            await obj.async_wait(["delete"], timeout=DELETE_TIMEOUT_SECONDS)
        except TimeoutError as exc:
            metadata = spec.get("metadata")
            name = metadata.get("name") if isinstance(metadata, dict) else None
            msg = (
                f"{spec.get('kind')}/{name} still exists {DELETE_TIMEOUT_SECONDS:.0f}s after being deleted, "
                f"most likely a finalizer. Re-applying now would race the delete."
            )
            raise TimeoutError(msg) from exc

    return delete


async def converge_direct(  # noqa: PLR0913 -- one caller, and each argument is a knob `ekn kubeapply` exposes
    objects: Sequence[Manifest],
    *,
    api: Api,
    environment: str,
    resource_priority: Mapping[str, int] | None = None,
    field_manager: str = DEFAULT_FIELD_MANAGER,
    environment_label: str = DEFAULT_ENVIRONMENT_LABEL,
    concurrency: int = DEFAULT_CONCURRENCY,
    settle_seconds: float = DEFAULT_SETTLE_SECONDS,
    allow_recreate: bool = False,
) -> tuple[ConvergeReport, dict[tuple[str, str, str], type[APIObject]]]:
    """Apply `objects` until the cluster stops changing, or until it stops
    making progress.

    Returns the report and the desired set this run produced, which is what
    `apply.prune_generation` needs to delete what the configuration dropped.

    Every object is stamped with `environment_label`, exactly as
    `apply_and_prune` stamps it, so a converging run and a barrier run leave
    the same cluster state and the same prune scope.
    """
    _log.info(
        "converging",
        objects=len(objects),
        concurrency=concurrency,
        settle=settle_seconds,
        recreate=allow_recreate,
    )
    desired: dict[tuple[str, str, str], type[APIObject]] = {}
    report = await converge_queue(
        objects,
        apply=_applier(
            api,
            environment=environment,
            field_manager=field_manager,
            environment_label=environment_label,
            desired=desired,
        ),
        delete=_deleter(api) if allow_recreate else None,
        resource_priority=resource_priority,
        concurrency=concurrency,
        settle_seconds=settle_seconds,
        allow_recreate=allow_recreate,
    )
    return report, desired


def report_failures(report: ConvergeReport) -> None:
    """Print what did not converge, worst first.

    One line per object and then its diagnosis, rather than a traceback: a
    converging run reaches the end with several independent failures, and a
    traceback shows one of them.
    """
    _log.info("converged", applied=report.applied, skipped=report.skipped, failed=len(report.failures))
    for failure in report.failures:
        namespace, kind, name = failure.key
        _log.error(
            "did not converge",
            kind=kind,
            namespace=namespace,
            name=name,
            disposition=failure.disposition.name,
            attempts=failure.attempts,
            error=failure.error,
        )


__all__ = ["converge_direct", "report_failures"]
