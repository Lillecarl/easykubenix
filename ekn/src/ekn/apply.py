from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, cast

import kr8s
import structlog
from kr8s.asyncio.objects import APIObject, get_class, new_class
from nanopynix.models import JsonValue

if TYPE_CHECKING:
    from kr8s._api import Api  # kr8s.asyncio.api() returns this, not kr8s.Api

_log = structlog.get_logger()

DEFAULT_DISCRIMINATOR_LABEL = "ekn.dev/discriminator"

# Where a kind with no configured priority sorts -- in practice, every custom
# resource, since `ekn.resourcePriority` lists only built-in kinds.
#
# Deliberately a round number in the middle of nothing, rather than "last".
# Helm's rule for kinds outside its InstallOrder is "unknown kind is last"
# (the literal comment in pkg/release/v1/util/kind_sorter.go) and that rule is
# wrong: the last entries of that order are the admission webhook
# configurations, so sorting unknowns after it applies every custom resource
# *behind* the webhooks that intercept it. During a bootstrap the webhook's
# backing workload was applied seconds earlier and is not serving yet, so each
# intercepted write blocks for the webhook's full `timeoutSeconds`. On
# kube-prometheus-stack that is two 10s webhooks over 19 PrometheusRules:
# roughly six minutes of an apply doing nothing.
#
# So this sits above Helm's whole range (numbered in tens, topping out at 380)
# but below anything `ekn.resourcePriority` numbers past it. That leaves
# 381..999 for "late, but before custom resources" and 1001+ for "after custom
# resources", which is where easykubenix/ekn.nix puts APIService and the two
# webhook configurations.
#
# tests/test_eval.py asserts that relationship against the real evaluated
# option -- it is the only thing tying these numbers to that file, and it
# spans two languages, so renumbering either side fails loudly rather than
# silently reintroducing the stall.
DEFAULT_BARRIER_PRIORITY = 1000

# Server-side-apply field manager, unless a caller names another. A GitOps
# target can (`gitOps.targets.<name>.fieldManager`), so that a bootstrap apply
# can hand its objects to the controller that takes them over rather than
# leaving every field it set owned by `ekn` forever -- SSA only releases a
# field when its owning manager stops declaring it, and a bootstrap apply
# never runs again.
DEFAULT_FIELD_MANAGER = "ekn"

type Manifest = dict[str, JsonValue]


def barriers(
    objects: list[Manifest],
    resource_priority: dict[str, int],
) -> list[list[Manifest]]:
    """Group objects into ordered apply barriers by kind priority.

    `resource_priority` is `ekn.resourcePriority` (Helm's InstallOrder by
    default): objects whose kind has a lower number land in an earlier
    barrier -- fully applied, and for CRDs waited on to become Established,
    before the next barrier starts.

    Kinds with no configured priority share a barrier at
    `DEFAULT_BARRIER_PRIORITY`, which is *not* last: kinds numbered above it
    apply afterwards. See that constant for why.
    """
    grouped: dict[int, list[Manifest]] = {}
    for obj in objects:
        kind = obj.get("kind", "")
        kind_str = kind if isinstance(kind, str) else ""
        priority = resource_priority.get(kind_str, DEFAULT_BARRIER_PRIORITY)
        grouped.setdefault(priority, []).append(obj)
    return [grouped[priority] for priority in sorted(grouped)]


async def build_object(spec: Manifest, api: Api) -> APIObject:
    """Turn a raw manifest dict into a kr8s APIObject, resolving plural/
    namespaced-ness for kinds kr8s doesn't have a builtin class for (i.e.
    almost every CRD) against the live API server's own discovery info --
    the same mechanism kr8s's own `Api.async_get` uses, rather than
    guessing a plural by string mangling.
    """
    kind = spec["kind"]
    if not isinstance(kind, str):
        raise TypeError(f"manifest 'kind' must be a string, got {type(kind).__name__}")
    api_version = spec.get("apiVersion", "v1")
    if not isinstance(api_version, str):
        raise TypeError(f"manifest 'apiVersion' must be a string, got {type(api_version).__name__}")
    try:
        cls = get_class(kind, api_version)
    except KeyError:
        group = api_version.split("/", 1)[0] if "/" in api_version else None
        lookup = f"{kind}.{group}" if group else kind
        # kr8s.Api.async_lookup_kind's `kind` parameter has no upstream type
        # annotation, so pyright can't resolve the member's own type fully.
        _, plural, namespaced = await api.async_lookup_kind(lookup)  # pyright: ignore[reportUnknownMemberType] -- kr8s async_lookup_kind's kind param has no upstream type annotation
        cls = new_class(kind, api_version, namespaced=namespaced, plural=plural)
    return cls(spec, api=api)


async def ssa_apply(
    obj: APIObject,
    *,
    field_manager: str,
    force: bool = True,
    dry_run: bool = False,
) -> Manifest:
    """Server-side apply.

    kr8s's `.patch()` only supports merge-patch/json-patch content types --
    issue the PATCH ourselves with the `application/apply-patch+yaml`
    content type `kubectl apply --server-side` uses, which the API server
    accepts with a plain JSON body just as well as YAML.

    `dry_run=True` (used by `ekn clusterdiff`) asks the API server to
    compute and return the would-be-merged object without persisting
    anything -- `obj.raw` is left untouched in that case, since it isn't a
    real apply.
    """
    # kr8s.APIObject.api's property getter has no upstream return annotation
    # (kr8s/_objects.py), so pyright can only infer a partially-Unknown union
    # for it -- cast to the precise type its docstring/behavior guarantees.
    api = cast("Api | None", obj.api)  # pyright: ignore[reportUnknownMemberType] -- kr8s APIObject.api getter has no upstream return annotation
    if api is None:
        raise RuntimeError("APIObject has no attached kr8s Api instance")
    params = {"fieldManager": field_manager, "force": "true" if force else "false"}
    if dry_run:
        params["dryRun"] = "All"
    # kr8s.Api.call_api's **kwargs has no upstream type annotation.
    async with api.call_api(  # pyright: ignore[reportUnknownMemberType] -- kr8s Api.call_api's **kwargs has no upstream type annotation
        "PATCH",
        version=obj.version,
        url=f"{obj.endpoint}/{obj.name}",
        namespace=obj.namespace,
        content=json.dumps(dict(obj.raw)),
        headers={"Content-Type": "application/apply-patch+yaml"},
        params=params,
    ) as resp:
        result: JsonValue = resp.json()
    if not isinstance(result, dict):
        raise TypeError(f"server-side apply response must be an object, got {type(result).__name__}")
    if not dry_run:
        obj.raw = result
    return result


async def apply_one(spec: Manifest, api: Api, *, field_manager: str) -> APIObject:
    """Build and server-side-apply a single manifest, returning the resulting
    APIObject.

    The shared "put this object on the cluster" primitive: `apply_and_prune`'s
    tier loop calls this per discriminator-labeled object it tracks for
    pruning, and `ekn.sops.ensure_age_identities`' cluster-bootstrap step
    calls it directly for its Namespace/Secret objects -- which deliberately
    skip discriminator labeling (they aren't part of any `apply_and_prune`
    generation and must never be pruned), so that decision stays with each
    caller rather than being baked in here.
    """
    obj = await build_object(spec, api)
    await ssa_apply(obj, field_manager=field_manager)
    return obj


def _object_key(obj: APIObject) -> tuple[str, str, str]:
    return (obj.namespace or "none", obj.kind, obj.name)


def _with_discriminator_label(spec: Manifest, label: str, value: str) -> Manifest:
    labeled = dict(spec)
    metadata_value = labeled.get("metadata") or {}
    metadata: Manifest = dict(metadata_value) if isinstance(metadata_value, dict) else {}
    labels_value = metadata.get("labels") or {}
    labels: dict[str, JsonValue] = dict(labels_value) if isinstance(labels_value, dict) else {}
    labels[label] = value
    metadata["labels"] = labels
    labeled["metadata"] = metadata
    return labeled


async def _wait_established(crd: APIObject, timeout: float) -> None:
    """Wait for one CRD to report Established, tolerating a status that is not there yet.

    `kr8s`' own `wait` reads `.status.conditions` and hands it to
    `list_dict_unpack`, which iterates its argument. A CRD the API server
    has accepted but not yet given a status has no `conditions` at all, so
    that argument is `None` and the call raises::

        TypeError: 'NoneType' object is not iterable

    A race, and a narrow one -- the apiextensions controller fills the
    status in well under a second -- so it passes almost every time and
    then kills a bootstrap that happens to lose it. Seen against a
    freshly-created `applicationsets.argoproj.io`, mid-apply, with the
    barriers before it already in the cluster.

    Retrying is the whole fix: the next read finds a status. Only
    `TypeError` is swallowed, and only until the deadline the caller
    already asked for, so a CRD that genuinely never establishes still
    fails rather than spinning.
    """
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_running_loop().time()
        if remaining <= 0:
            msg = f"CRD {crd.name} did not become Established within {timeout}s"
            raise TimeoutError(msg)
        try:
            await crd.wait("condition=Established", timeout=remaining)
        except TypeError:
            # No status yet. Let the controller get there rather than
            # hammering the API server, then look again.
            _log.debug("CRD has no status yet, retrying", name=crd.name)
            await asyncio.sleep(0.5)
            continue
        return


async def apply_and_prune(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
    objects: list[Manifest],
    *,
    api: Api,
    discriminator: str,
    discriminator_label: str = DEFAULT_DISCRIMINATOR_LABEL,
    resource_priority: dict[str, int] | None = None,
    field_manager: str = DEFAULT_FIELD_MANAGER,
    crd_establish_timeout: int = 60,
    prune: bool = True,
    prune_kinds: set[str] | None = None,
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

    `prune_kinds`, when given, is unioned into the kinds scanned for pruning
    alongside whatever kinds this apply itself touched -- an additive seam
    for the eventual fix above (an `apiMappings`-sourced kind list), added
    now so that fix won't be a breaking signature change later. No caller
    passes it yet; `None` (the default) preserves today's current-apply-only
    scanning exactly.

    `prune=False` (the default for `ekn kubeapply` against a real cluster,
    e.g. a narrow `--target` slice) avoids pruning objects that are simply
    outside the current apply's scope -- the same "two controllers fighting
    over pruning" concern kluctl.nix's `excludeGitopsTargets` documents.
    """
    resource_priority = resource_priority or {}
    desired_keys: set[tuple[str, str, str]] = set()
    kinds: set[str] = set()

    # Progress is logged at INFO per barrier, not per object. An apply of a few
    # hundred objects otherwise runs completely silently for minutes -- every
    # CRD in it can hold a barrier open for up to `crd_establish_timeout`, and
    # a caller with no output cannot tell that from a hang. Per-object stays at
    # DEBUG; the barrier is the unit where the waiting actually happens.
    tiers = barriers(objects, resource_priority)
    for index, tier in enumerate(tiers, start=1):
        _log.info("applying", barrier=f"{index}/{len(tiers)}", objects=len(tier))
        applied: list[APIObject] = []
        for spec in tier:
            labeled = _with_discriminator_label(spec, discriminator_label, discriminator)
            obj = await apply_one(labeled, api, field_manager=field_manager)
            applied.append(obj)
            desired_keys.add(_object_key(obj))
            kinds.add(obj.kind)
            _log.debug("applied", kind=obj.kind, namespace=obj.namespace, name=obj.name)

        crds = [obj for obj in applied if obj.kind == "CustomResourceDefinition"]
        if crds:
            _log.info("waiting for CRDs to become Established", count=len(crds), timeout=crd_establish_timeout)
        for crd in crds:
            await _wait_established(crd, crd_establish_timeout)

    if not prune:
        return

    scan_kinds = kinds | (prune_kinds or set())
    _log.info("pruning", kinds=len(scan_kinds), discriminator=discriminator)
    for kind in scan_kinds:
        # kr8s.Api.async_get's `label_selector`/`field_selector` params and its
        # `APIObject | dict` yield type are both bare-`dict`/unannotated
        # upstream, so pyright can't resolve the member or the loop variable.
        async for obj in api.async_get(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- kr8s Api.async_get's selector params and yield type are unannotated upstream
            kind,
            namespace=kr8s.ALL,
            label_selector={discriminator_label: discriminator},
        ):
            if not isinstance(obj, APIObject):
                continue
            # Not `_object_key(obj)`: for CRD kinds (no static kr8s class),
            # `api.async_get`'s own internal `async_lookup_kind` call
            # reassigns its `kind` param to a `"singular.group/version"`
            # string, which `new_class` then mis-splits on the first "." --
            # the listed object's `.kind` ends up as the lowercase singular
            # name (e.g. "verticalpodautoscaler"), not the PascalCase Kind
            # (e.g. "VerticalPodAutoscaler") `desired_keys` was built from
            # while applying. Use the loop's own `kind` (identical to what
            # `_object_key` used at apply time) instead of trusting the
            # listed object's mangled one -- otherwise every CRD-based
            # object's key mismatches and everything gets "pruned".
            key = (obj.namespace or "none", kind, obj.name)
            if key not in desired_keys:
                _log.info("pruning", kind=kind, namespace=obj.namespace, name=obj.name)
                await obj.delete()


__all__ = [
    "DEFAULT_BARRIER_PRIORITY",
    "DEFAULT_DISCRIMINATOR_LABEL",
    "DEFAULT_FIELD_MANAGER",
    "Manifest",
    "apply_and_prune",
    "apply_one",
    "barriers",
    "build_object",
    "ssa_apply",
]
