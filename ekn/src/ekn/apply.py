from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import kr8s
import structlog
from kr8s.asyncio.objects import APIObject, get_class, new_class

if TYPE_CHECKING:
    from kr8s._api import Api  # kr8s.asyncio.api() returns this, not kr8s.Api

_log = structlog.get_logger()

DEFAULT_DISCRIMINATOR_LABEL = "ekn.dev/discriminator"
_DEFAULT_BARRIER_PRIORITY = 100


def barriers(
    objects: list[dict[str, Any]], resource_priority: dict[str, int]
) -> list[list[dict[str, Any]]]:
    """Group objects into ordered apply barriers by kind priority.

    Mirrors kluctl's resourcePriority: objects whose kind has a lower
    configured priority number (e.g. Namespace/CustomResourceDefinition)
    land in an earlier barrier -- fully applied (and, for CRDs, waited on to
    become Established) before the next barrier starts. Kinds with no
    configured priority all land together in one final barrier.
    """
    grouped: dict[int, list[dict[str, Any]]] = {}
    for obj in objects:
        priority = resource_priority.get(obj.get("kind", ""), _DEFAULT_BARRIER_PRIORITY)
        grouped.setdefault(priority, []).append(obj)
    return [grouped[priority] for priority in sorted(grouped)]


async def _build_object(spec: dict[str, Any], api: Api) -> APIObject:
    """Turn a raw manifest dict into a kr8s APIObject, resolving plural/
    namespaced-ness for kinds kr8s doesn't have a builtin class for (i.e.
    almost every CRD) against the live API server's own discovery info --
    the same mechanism kr8s's own `Api.async_get` uses, rather than
    guessing a plural by string mangling.
    """
    kind = spec["kind"]
    api_version = spec.get("apiVersion", "v1")
    try:
        cls = get_class(kind, api_version)
    except KeyError:
        group = api_version.split("/", 1)[0] if "/" in api_version else None
        lookup = f"{kind}.{group}" if group else kind
        _, plural, namespaced = await api.async_lookup_kind(lookup)
        cls = new_class(kind, api_version, namespaced=namespaced, plural=plural)
    return cls(spec, api=api)


async def ssa_apply(obj: APIObject, *, field_manager: str, force: bool = True) -> None:
    """Server-side apply.

    kr8s's `.patch()` only supports merge-patch/json-patch content types --
    issue the PATCH ourselves with the `application/apply-patch+yaml`
    content type `kubectl apply --server-side` uses, which the API server
    accepts with a plain JSON body just as well as YAML.
    """
    api = obj.api
    assert api is not None
    async with api.call_api(
        "PATCH",
        version=obj.version,
        url=f"{obj.endpoint}/{obj.name}",
        namespace=obj.namespace,
        content=json.dumps(dict(obj.raw)),
        headers={"Content-Type": "application/apply-patch+yaml"},
        params={"fieldManager": field_manager, "force": "true" if force else "false"},
    ) as resp:
        obj.raw = resp.json()


def _object_key(obj: APIObject) -> tuple[str, str, str]:
    return (obj.namespace or "none", obj.kind, obj.name)


def _with_discriminator_label(
    spec: dict[str, Any], label: str, value: str
) -> dict[str, Any]:
    labeled = dict(spec)
    metadata = dict(labeled.get("metadata") or {})
    labels = dict(metadata.get("labels") or {})
    labels[label] = value
    metadata["labels"] = labels
    labeled["metadata"] = metadata
    return labeled


async def apply_and_prune(
    objects: list[dict[str, Any]],
    *,
    api: Api,
    discriminator: str,
    discriminator_label: str = DEFAULT_DISCRIMINATOR_LABEL,
    resource_priority: dict[str, int] | None = None,
    field_manager: str = "ekn",
    crd_establish_timeout: int = 60,
    prune: bool = True,
) -> None:
    """Apply `objects` in barrier order, then (if `prune`) prune anything
    previously applied under the same discriminator that this run no longer
    generates.

    Known limitation: pruning only scans kinds present in *this* apply --
    if every object of some kind is removed from the generated config in one
    go, stale objects of that now-absent kind won't be found or deleted.
    Fine for the ephemeral, always-fresh apiserver `ekn validate` runs this
    against; needs a kind list independent of the current apply set (e.g.
    from `kubernetes.apiMappings`) before this drives a real, persistent
    cluster.

    `prune=False` (the default for `ekn kubeapply` against a real cluster,
    e.g. a narrow `--target` slice) avoids pruning objects that are simply
    outside the current apply's scope -- the same "two controllers fighting
    over pruning" concern kluctl.nix's `excludeGitopsTargets` documents.
    """
    resource_priority = resource_priority or {}
    desired_keys: set[tuple[str, str, str]] = set()
    kinds: set[str] = set()

    for tier in barriers(objects, resource_priority):
        applied: list[APIObject] = []
        for spec in tier:
            labeled = _with_discriminator_label(spec, discriminator_label, discriminator)
            obj = await _build_object(labeled, api)
            await ssa_apply(obj, field_manager=field_manager)
            applied.append(obj)
            desired_keys.add(_object_key(obj))
            kinds.add(obj.kind)
            _log.info("applied", kind=obj.kind, namespace=obj.namespace, name=obj.name)

        crds = [obj for obj in applied if obj.kind == "CustomResourceDefinition"]
        for crd in crds:
            await crd.wait("condition=Established", timeout=crd_establish_timeout)

    if not prune:
        return

    for kind in kinds:
        async for obj in api.async_get(
            kind,
            namespace=kr8s.ALL,
            label_selector={discriminator_label: discriminator},
        ):
            if not isinstance(obj, APIObject):
                continue
            key = _object_key(obj)
            if key not in desired_keys:
                _log.info("pruning", kind=obj.kind, namespace=obj.namespace, name=obj.name)
                await obj.delete()


__all__ = ["DEFAULT_DISCRIMINATOR_LABEL", "apply_and_prune", "barriers", "ssa_apply"]
