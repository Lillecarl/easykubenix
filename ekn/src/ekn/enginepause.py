"""Stop the GitOps engine writing, and be able to start it again.

A direct apply and a running GitOps engine are two writers of the same
objects. The engine reconciles what git says; a direct apply writes what the
working copy says. Whichever ran last wins, so a direct apply against a live
engine is undone at the engine's next sync, and a field this apply removed
is restored -- with nothing reporting either.

So a full deploy pauses the engine first. That is the whole reason this
module exists, and it is also why it is not a convenience: **a pause that
cannot be resumed is worse than no pause**, because it leaves the cluster
with no reconciler and nobody watching.

Everything here is therefore built around one property: the state needed to
resume lives **on the cluster, on the object itself**, written before the
change it describes. `ekn` can be killed at any point and a later run still
knows what to restore.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import kr8s
import structlog
from kr8s.asyncio.objects import Deployment, StatefulSet

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

    from kr8s._api import Api
    from kr8s.asyncio.objects import APIObject

    from .apply import Manifest

_log = structlog.get_logger()

PAUSED_REPLICAS_ANNOTATION = "ekn.dev/paused-replicas"
"""The replica count to restore, written before scaling to zero.

On the workload rather than in a file or a ConfigMap of our own, because it
has to survive `ekn` being killed, the machine it ran on going away, and
somebody else running the resume. An annotation on the object is the only
place that is true of all three.

**Never overwritten while it exists.** A second pause would otherwise record
the already-scaled `0` as the original count, and the resume would restore
zero replicas -- a paused engine that reports itself successfully resumed.
That is the failure this module exists to avoid, reached by running the
tool twice.
"""

_SCALABLE: dict[str, type[APIObject]] = {
    "Deployment": Deployment,
    "StatefulSet": StatefulSet,
}


class EnginePauseError(RuntimeError):
    """A pause or resume that did not do what it claimed."""


@dataclass(frozen=True)
class Workload:
    """One engine workload to stop, as the configuration names it."""

    namespace: str
    name: str
    kind: str = "Deployment"

    def __str__(self) -> str:
        return f"{self.kind} {self.namespace}/{self.name}"


@dataclass(frozen=True)
class Paused:
    """One workload this run stopped, and what it has to be restored to."""

    workload: Workload
    replicas: int
    #: True when the annotation was already there, so an earlier run paused
    #: it and this run must not claim the credit -- or the responsibility.
    already: bool = False


async def _fetch(api: Api, workload: Workload) -> APIObject:
    cls = _SCALABLE.get(workload.kind)
    if cls is None:
        served = ", ".join(sorted(_SCALABLE))
        msg = f"{workload} cannot be scaled: engine pause understands {served}"
        raise EnginePauseError(msg)
    try:
        # The class's own `get`, not `Api.async_get`: that one is an async
        # generator over a list, and this is a single object by name.
        return await cls.get(workload.name, namespace=workload.namespace, api=api)
    except kr8s.NotFoundError as exc:
        msg = f"{workload} does not exist, so the engine cannot be paused by scaling it"
        raise EnginePauseError(msg) from exc


def _recorded_replicas(obj: APIObject) -> int | None:
    metadata = obj.raw.get("metadata")
    annotations = metadata.get("annotations") if isinstance(metadata, dict) else None
    recorded = annotations.get(PAUSED_REPLICAS_ANNOTATION) if isinstance(annotations, dict) else None
    if recorded is None:
        return None
    try:
        return int(recorded)
    except (TypeError, ValueError):
        return None


def _spec_replicas(obj: APIObject) -> int:
    spec = obj.raw.get("spec")
    replicas = spec.get("replicas") if isinstance(spec, dict) else None
    return replicas if isinstance(replicas, int) else 1


async def pause(workloads: Sequence[Workload], *, api: Api) -> list[Paused]:
    """Scale each workload to zero, recording what to restore it to.

    The order is: read, **annotate, then scale**. Annotating after scaling
    would leave a window in which a killed `ekn` has stopped the engine and
    left nothing saying how to start it again.

    A workload already carrying the annotation is left as it is and reported
    with `already=True`. Its recorded count is the truth; this run's observed
    count is `0` and recording that would destroy the only copy of it.
    """
    paused: list[Paused] = []
    for workload in workloads:
        obj = await _fetch(api, workload)
        recorded = _recorded_replicas(obj)
        if recorded is not None:
            _log.warning(
                "already paused by an earlier run",
                workload=str(workload),
                replicas=recorded,
            )
            paused.append(Paused(workload, recorded, already=True))
            continue
        replicas = _spec_replicas(obj)
        await obj.async_annotate({PAUSED_REPLICAS_ANNOTATION: str(replicas)})
        await obj.async_scale(0)
        _log.info("paused", workload=str(workload), replicas=replicas)
        paused.append(Paused(workload, replicas))
    return paused


async def resume(paused: Iterable[Paused], *, api: Api) -> list[str]:
    """Restore every workload and clear its annotation.

    Returns the workloads it could **not** restore, as messages. It does not
    raise on the first failure: each workload is independent, and stopping
    early would leave the rest of the engine down because one part of it
    could not be brought up.

    The annotation is removed **after** the scale, for the same reason it is
    written before it: if this dies in between, the annotation still names
    the right count and a later resume is correct. Removing it first would
    turn a crash into a permanently stopped engine.
    """
    failed: list[str] = []
    for entry in paused:
        try:
            obj = await _fetch(api, entry.workload)
            await obj.async_scale(entry.replicas)
            # kr8s sends this as a strategic-merge patch, where a null value
            # removes the key.
            await obj.async_annotate({PAUSED_REPLICAS_ANNOTATION: None})
        # Broad on purpose: every workload is reported and none aborts the
        # rest, because a partly-resumed engine is the worst outcome here.
        except Exception as exc:
            failed.append(f"{entry.workload}: {exc}")
            _log.error("could not resume", workload=str(entry.workload), error=str(exc))
        else:
            _log.info("resumed", workload=str(entry.workload), replicas=entry.replicas)
    return failed


async def still_paused(workloads: Sequence[Workload], *, api: Api) -> list[Paused]:
    """Workloads carrying the annotation right now.

    What makes the pause crash-safe usable rather than merely crash-safe: a
    later run can find an engine an earlier run stopped and never restarted,
    without being told about it.
    """
    found: list[Paused] = []
    for workload in workloads:
        try:
            obj = await _fetch(api, workload)
        except EnginePauseError:
            continue
        recorded = _recorded_replicas(obj)
        if recorded is not None:
            found.append(Paused(workload, recorded, already=True))
    return found


def object_keys(workloads: Iterable[Workload]) -> set[tuple[str, str, str]]:
    """The pause targets, keyed as `apply` keys every object.

    Feed this to `apply_and_prune`'s / `prune_generation`'s `protect`. See
    `without_pause_targets` for why both halves are needed.
    """
    return {(w.namespace, w.kind, w.name) for w in workloads}


def without_pause_targets(
    objects: Iterable[Manifest],
    workloads: Iterable[Workload],
) -> list[Manifest]:
    """The apply set with the engine's own workloads removed.

    **The engine is usually in the apply set.** Measured on nixlab2: all six
    ArgoCD workloads are in `kubernetes.generated`, each declaring
    `replicas: 1`, and the application controller the pause scales to zero is
    one of them. Applying that set re-applies `replicas: 1` to the object the
    pause just zeroed, so the engine wakes up *in the middle of the apply
    that paused it* -- with every Application to reconcile while `ekn` is
    still writing. Under a concurrent queue the moment is unpredictable, so
    the symptom is "some objects reverted, some not".

    **Skipping is only half the fix.** An object skipped from the apply set is
    absent from the desired set, and absence is what a prune answers with a
    delete -- so skipping alone turns "the engine un-pauses itself" into "the
    apply deletes its own engine". The other half is `object_keys`, which is
    exactly `protect`'s meaning: this run could not safely produce it, which
    is not the same as it having been removed from the configuration.

    The workload keeps whatever the configuration says for it; this run
    simply is not the run that applies it. The next apply with the engine
    running writes `replicas: 1` again.

    **Matching is on the full `(namespace, kind, name)` triple, and that is
    load-bearing rather than tidy.** On nixlab2 seven generated objects share
    the controller's name -- NetworkPolicy, Role, RoleBinding, ServiceMonitor
    and StatefulSet in `argocd`, plus a cluster-scoped ClusterRole and
    ClusterRoleBinding. Only the StatefulSet is the pause target. A match on
    name, or on namespace and name, would silently drop the engine's own RBAC
    from every full apply, so a change to it would never be delivered and
    nothing would say so.

    **Consequence worth knowing: this mode can never update the engine's own
    reconciler.** That follows from the exclusion and is correct while paused,
    but it means an upgrade to that workload converges cleanly and leaves the
    object stale. Logged at warning for that reason, rather than passed over
    in silence.
    """
    excluded = object_keys(workloads)
    kept: list[Manifest] = []
    for spec in objects:
        metadata = spec.get("metadata")
        namespace = metadata.get("namespace") if isinstance(metadata, dict) else None
        name = metadata.get("name") if isinstance(metadata, dict) else None
        key = (
            str(namespace) if isinstance(namespace, str) else "none",
            str(spec.get("kind")),
            str(name) if isinstance(name, str) else "",
        )
        if key in excluded:
            _log.warning(
                "held by the engine pause, so this run cannot update it",
                kind=key[1],
                namespace=key[0],
                name=key[2],
            )
            continue
        kept.append(spec)
    return kept


def resume_reminder(paused: Iterable[Paused]) -> str:
    """What to tell someone whose run left the engine stopped."""
    listed = "\n".join(f"  {entry.workload} -> {entry.replicas} replicas" for entry in paused)
    return (
        f"The GitOps engine is still scaled to zero:\n{listed}\n"
        f"Nothing is reconciling this cluster until it is restored. Each workload carries "
        f"{PAUSED_REPLICAS_ANNOTATION} with the count to restore, so `ekn` can finish this "
        f"on the next run -- but until then the cluster has no reconciler."
    )


__all__ = [
    "PAUSED_REPLICAS_ANNOTATION",
    "EnginePauseError",
    "Paused",
    "Workload",
    "object_keys",
    "pause",
    "resume",
    "resume_reminder",
    "still_paused",
    "without_pause_targets",
]
