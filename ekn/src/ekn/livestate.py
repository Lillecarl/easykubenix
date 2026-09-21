"""What the cluster already holds, and what follows from it.

One LIST per kind, asking for object metadata only, answers four questions
that a direct apply needs and that nothing else can answer cheaply:

- has anybody written this object since `ekn` last applied it
  (`resourceVersion`);
- has this object's rendered manifest changed since it was last applied
  (`ekn.dev/manifest-hash`);
- which field managers other than ours own parts of it (`managedFields`);
- is it in this apply's scope at all (`ekn.dev/environment`,
  `ekn.dev/deployment-unit`).

**Metadata only, and the reason is a number.** A full sweep of a live
cluster -- 71 kinds, 997 objects -- moves 45.5 MB, of which 38.1 MB is the
OpenAPI schemas of 188 CustomResourceDefinitions that none of the three
consumers reads. The same sweep asking for `PartialObjectMetadataList` is
2.56 MB, 18 times smaller, and `ObjectMeta` is exactly and only what the
three need. Measured by solid-kubernetes on nixlab2; issue
Lillecarl/easykubenix#28.

`sweep` is that LIST, and the rest of this module is the decisions made from
what it returns.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import kr8s
import structlog

from .apply import DEFAULT_ENVIRONMENT_LABEL, DEFAULT_UNIT_LABEL, KindNotServedError, build_object

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from kr8s.asyncio import Api

    from .apply import Manifest

_log = structlog.get_logger()

HASH_ANNOTATION = "ekn.dev/manifest-hash"
"""Carries `sha256:<hex>` of the object as rendered.

Written into the manifest by the render, not by `ekn` at apply time. That is
the whole point: an object a GitOps engine synced from the committed YAML
carries it too, so a fast run against an ArgoCD-managed cluster finds it.
Computed at apply time it would exist only on objects `ekn` itself last
applied, and the first fast run after any sync would find nothing.
"""

OUR_MANAGERS = frozenset({"ekn"})
"""Field managers that are this tool, so not worth reporting as foreign."""


@dataclass(frozen=True)
class LiveObject:
    """One object as the metadata sweep saw it."""

    key: tuple[str, str, str]
    manifest_hash: str | None = None
    environment: str | None = None
    unit: str | None = None
    managers: frozenset[str] = field(default_factory=frozenset)
    resource_version: str | None = None
    """What the API server says this object is at now.

    Opaque, and only ever compared for equality -- it is a string the server
    may shape however it likes. It moves on every write by anybody, including
    a writer using our own field-manager name, which `managers` cannot
    distinguish. A server-side apply that changes nothing does **not** move
    it, which is what lets `fastcache` compare it against the one its last
    apply returned.
    """


ACCEPT_METADATA = "application/json;as=PartialObjectMetadataList;g=meta.k8s.io;v=v1"
"""Ask for `ObjectMeta` and nothing else.

Measured by solid-kubernetes on nixlab2: the same sweep is 45.5 MB as whole
objects and 2.56 MB as metadata, and 38.1 MB of the difference is the
OpenAPI schemas of 188 CustomResourceDefinitions that no caller here reads.
"""

CACHED_LIST = {"resourceVersion": "0"}
"""Serve this LIST from the API server's watch cache.

`Cacher.List` answers a LIST with `resourceVersion=0` from
`watchCache.WaitUntilFreshAndGet` -- in memory, no etcd round trip and no
conversion. Without it each kind is a quorum read against etcd, and a sweep
meant to make an apply cheaper starts costing what the apply costs.

**The list may be slightly behind.** For the `fastcache` gate that is safe
in one direction and self-healing in the other: an older `resourceVersion`
differs from the recorded one, so the object is applied; and an object
written in the last instant may still read as the recorded value, so it is
skipped **once**. A skip records nothing, so the next run reads the moved
value and applies. Do not "fix" this by dropping the parameter.

No `limit` either, so there is one request per kind: the API server rejects
a `continue` token sent together with a resource version, so a paged sweep
would have to give this up.
"""

_UNSWEEPABLE = frozenset({403, 404, 405})
"""Answers that mean "nothing of this kind is skippable", not "the run failed".

403 is an apply whose RBAC covers writing a kind but not listing it. 404 and
405 are the seven built-in kinds that are served and cannot be listed at all
(`apply._NO_LIST_VERB`). A kind that answers any of these simply contributes
no live objects, and every object of it is applied.

A 403 costs three reauthentication attempts inside `kr8s.Api.call_api`
before it is raised, so a denied kind is slow as well as empty.
"""


def _managers(metadata: Mapping[str, Any]) -> frozenset[str]:
    """Who owns part of the object proper.

    **A `subresource` entry is not one of them.** `managedFields` records the
    subresource a write went through, and a write to `status` or `scale`
    cannot have changed a field a manifest declares -- so counting it would
    make every object a controller reports on unskippable, by design and for
    ever.

    Measured on a kubeadm cluster: without this, 2 of 7 objects in a bare
    generation could never be skipped on their rendered hash. One is the
    CustomResourceDefinition, whose status `apiextensions-apiserver` writes
    on every cluster -- and CRDs are the entire reason the cold route exists.

    An allowlist cannot replace this. The set of components that report
    status on something is open-ended, and each one left out costs a full
    apply of every object it touches.
    """
    entries = metadata.get("managedFields")
    if not isinstance(entries, list):
        return frozenset()
    return frozenset(
        entry["manager"]
        for entry in entries
        if isinstance(entry, dict) and isinstance(entry.get("manager"), str) and not entry.get("subresource")
    )


def _live_object(kind: str, item: Mapping[str, Any]) -> LiveObject:
    """One `PartialObjectMetadata` as this module's record of it.

    **Keyed by the kind that was listed, never by the item's own.** Every
    item of a `PartialObjectMetadataList` reports `kind:
    PartialObjectMetadata`, which matches no manifest. The prune loop keys
    its own scan the same way, for a different reason and with the same
    consequence if it is got wrong.
    """
    metadata_value = item.get("metadata")
    metadata: Mapping[str, Any] = metadata_value if isinstance(metadata_value, dict) else {}
    annotations = metadata.get("annotations") or {}
    labels = metadata.get("labels") or {}

    def string(source: Any, key: str) -> str | None:
        value = source.get(key) if isinstance(source, dict) else None
        return value if isinstance(value, str) else None

    return LiveObject(
        key=(
            string(metadata, "namespace") or "none",
            kind,
            string(metadata, "name") or "?",
        ),
        manifest_hash=string(annotations, HASH_ANNOTATION),
        environment=string(labels, DEFAULT_ENVIRONMENT_LABEL),
        unit=string(labels, DEFAULT_UNIT_LABEL),
        managers=_managers(metadata),
        resource_version=string(metadata, "resourceVersion"),
    )


async def sweep(
    api: Api,
    kinds: Iterable[tuple[str, str]],
    *,
    selector: str | None = None,
) -> dict[tuple[str, str, str], LiveObject]:
    """One metadata LIST per kind, as `(namespace, kind, name)` records.

    *kinds* is `(kind, apiVersion)` -- the two fields that identify a resource
    exactly, so this resolves each through `apply.discover` rather than through
    a lookup by name. *selector* is a label selector narrowing the sweep,
    normally `ekn.dev/environment=<env>`: an object that does not carry it is
    not one `ekn` applied, and the callers of this only ask about objects
    `ekn` applied.

    A kind the cluster does not serve, or will not let this credential list,
    is left out rather than raised. On a first apply some CustomResourceDefinitions
    of this very run are not Established yet, and that is the ordinary state
    rather than a failure. Every consumer answers "no record" the same way it
    answers "not swept": by doing the work.
    """
    live: dict[tuple[str, str, str], LiveObject] = {}
    params = dict(CACHED_LIST)
    if selector:
        params["labelSelector"] = selector
    for kind, api_version in sorted(set(kinds)):
        try:
            cls = type(await build_object({"kind": kind, "apiVersion": api_version}, api))
        except KindNotServedError:
            _log.debug("not sweeping unserved kind", kind=kind, api_version=api_version)
            continue
        try:
            async with api.call_api(
                method="GET",
                version=cls.version,
                url=cls.endpoint,
                params=params,
                headers={"Accept": ACCEPT_METADATA},
            ) as response:
                payload: Any = response.json()
        except kr8s.ServerError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in _UNSWEEPABLE:
                raise
            _log.debug("kind not swept", kind=kind, api_version=api_version, status=status)
            continue
        items = payload.get("items") if isinstance(payload, dict) else None
        for item in items if isinstance(items, list) else []:
            if isinstance(item, dict):
                obj = _live_object(kind, item)
                live[obj.key] = obj
    _log.debug("swept", kinds=len(set(kinds)), objects=len(live), selector=selector)
    return live


def canonical_json(spec: Manifest) -> str:
    """The exact bytes both hash implementations must agree on.

    **Two producers compute this hash and they have to match exactly.** The
    render computes it in Nix for every generated object; `kubernetes.rawFiles`
    are parsed in Python, because `builtins.toJSON` reorders their keys, so
    those hash here instead. A difference of one separator makes every raw
    file look changed on every run, for ever, and the symptom is a fast mode
    that is not fast rather than an error.

    Sorted keys, no spaces, and no trailing newline.
    """
    return json.dumps(spec, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def strip_hash_annotation(spec: Manifest) -> Manifest:
    """The object as hashed: itself, without the hash annotation.

    Only that key. `ekn.dev/environment` deliberately stays out of the
    stripping, because it is never in the rendered object either -- `ekn`
    stamps it at apply time. So the same object applied by ArgoCD and by
    `ekn` hashes the same, which is what lets a fast run skip work an engine
    did.
    """
    metadata_value = spec.get("metadata") or {}
    if not isinstance(metadata_value, dict):
        return spec
    annotations_value = metadata_value.get("annotations") or {}
    if not isinstance(annotations_value, dict) or HASH_ANNOTATION not in annotations_value:
        return spec
    annotations = {k: v for k, v in annotations_value.items() if k != HASH_ANNOTATION}
    metadata = dict(metadata_value)
    if annotations:
        metadata["annotations"] = annotations
    else:
        del metadata["annotations"]
    stripped = dict(spec)
    stripped["metadata"] = metadata
    return stripped


def manifest_hash(spec: Manifest) -> str:
    """The `sha256:<hex>` this object should carry.

    For `kubernetes.rawFiles`, which are parsed here rather than rendered by
    Nix. Every generated object gets the same value computed in Nix, and the
    two must agree exactly -- see `canonical_json`.
    """
    body = canonical_json(strip_hash_annotation(spec))
    return f"sha256:{hashlib.sha256(body.encode()).hexdigest()}"


def desired_hash(spec: Manifest) -> str | None:
    """The hash the render wrote onto this object, or None if it wrote none.

    None is not an error. A configuration rendered before the annotation
    existed has none, and the only correct answer for such an object is to
    apply it.
    """
    metadata_value = spec.get("metadata") or {}
    metadata = metadata_value if isinstance(metadata_value, dict) else {}
    annotations_value = metadata.get("annotations") or {}
    annotations = annotations_value if isinstance(annotations_value, dict) else {}
    value = annotations.get(HASH_ANNOTATION)
    return value if isinstance(value, str) else None


def skippable(
    spec: Manifest,
    seen: LiveObject | None,
    *,
    environment: str,
    engine_managers: Iterable[str],
    ours: Iterable[str] = OUR_MANAGERS,
) -> bool:
    """True when this object may be left alone, knowing only what the sweep saw.

    The answer for an object no record on this machine covers: a first run, a
    new checkout, a cache whose format moved. Three conditions, and the last
    two are the ones that are easy to leave out:

    1. the live hash equals the rendered one;
    2. **the live object already carries `ekn.dev/environment=<env>`;**
    3. **no manager outside *ours* and *engine_managers* owns part of it.**

    Without the second, a fast run skips objects a GitOps engine applied and
    `ekn` never stamped -- and a later `--prune` selects by exactly that
    label, so it deletes objects that are present and correct. The condition
    also makes the first converge honest: it applies every such object once,
    stamps it, and every converge after that skips it.

    This is why fast mode and `--prune` are not in conflict. An earlier
    reading of this design had them mutually exclusive; the label condition
    is exact where that was merely conservative.

    **Without the third, content drift is invisible.** Both hashes are
    annotations: the rendered one from the render, the live one from what was
    applied. Neither is recomputed from live content, so a `kubectl edit` that
    changes a Deployment's image leaves the annotation alone and the hashes
    match. What that edit does leave is a field manager -- `kubectl-edit`, or
    `kubectl-client-side-apply` for an apply -- and the same sweep that reads
    the hash reads those. Reported by the operator this mode exists for.

    **And one case even the third cannot see: a write under a manager we
    already allow.** `ekn` itself is one, so `kubectl apply
    --field-manager=ekn` leaves nothing this function can read. `fastcache`
    catches it from the next run, because a skip here records where the
    object was; a write that happened before this machine ever saw the object
    is invisible to both, for ever.

    *seen* is the sweep's record, keyed by the **built** object's identity
    rather than the manifest's -- a manifest naming no namespace lives in the
    API's default one. The caller does that lookup, because only the caller
    has built the object.

    *engine_managers* has no default on purpose. It decides how strict this
    is, and getting it wrong is not symmetric: too narrow makes nearly
    everything unskippable, because `kube-controller-manager` owns fields on
    most objects by design, and the mode quietly stops being fast. See
    `foreign_owners`, which answers the same question for reporting.
    """
    wanted = desired_hash(spec)
    if wanted is None or seen is None or seen.manifest_hash != wanted:
        return False
    if seen.environment != environment:
        return False
    return not (seen.managers - frozenset(ours) - frozenset(engine_managers))


@dataclass(frozen=True)
class ForeignOwner:
    """An object a manager other than ours owns part of."""

    key: tuple[str, str, str]
    managers: tuple[str, ...]


def foreign_owners(
    live: Iterable[LiveObject],
    *,
    engine_managers: Iterable[str],
    ours: Iterable[str] = OUR_MANAGERS,
) -> list[ForeignOwner]:
    """Objects owned by a manager that is neither ours nor the GitOps engine's.

    A server-side apply with `force=true` takes every field it names, from
    whatever manager held it, and says nothing. Taking fields from the GitOps
    engine is the intent of this mode, so those are filtered out. What is
    left is the surprising half: a mutating controller, an operator, or a
    person who ran `kubectl rollout restart` and still owns a field.

    `kube-controller-manager` belongs in `engine_managers` for the same
    reason ArgoCD does -- it owns fields on nearly everything, by design.

    **`engine_managers` must never gate a delete.** It is a "do not report
    this as surprising" list, and the two questions look similar enough to
    merge by accident. `apply.DEFAULT_DELIVERY_MANAGERS` is the one that
    decides what a prune may delete, and it is deliberately a different set:
    the endpoints controller *is* `kube-controller-manager`, so a prune that
    trusted this list would delete every Endpoints object in scope --
    measured, on a live cluster, as six including `kube-system/coredns`.
    """
    ignored = {*ours, *engine_managers}
    owners: list[ForeignOwner] = []
    for obj in live:
        foreign = sorted(manager for manager in obj.managers if manager not in ignored)
        if foreign:
            owners.append(ForeignOwner(obj.key, tuple(foreign)))
    return owners


__all__ = [
    "ACCEPT_METADATA",
    "CACHED_LIST",
    "HASH_ANNOTATION",
    "OUR_MANAGERS",
    "ForeignOwner",
    "LiveObject",
    "canonical_json",
    "desired_hash",
    "foreign_owners",
    "manifest_hash",
    "skippable",
    "strip_hash_annotation",
    "sweep",
]
