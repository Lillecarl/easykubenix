from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, cast

import kr8s
import structlog
from kr8s.asyncio.objects import APIObject, get_class, new_class
from nanopynix.models import JsonValue

if TYPE_CHECKING:
    from collections.abc import Collection, Mapping

    from kr8s._api import Api  # kr8s.asyncio.api() returns this, not kr8s.Api

_log = structlog.get_logger()

# Stamped by `ekn` at apply time, on every object it applies. Its value is
# `ekn.environment`. Deliberately not rendered into the manifests: an object a
# GitOps engine synced from the committed YAML must not carry it, or `ekn` and
# that engine both consider it theirs to prune.
DEFAULT_ENVIRONMENT_LABEL = "ekn.dev/environment"

# Rendered by easykubenix onto every object in a deployment unit (see
# gitops.nix), so it is on the object whoever applies it. `ekn` never writes
# it, and only reads it to scope a prune.
DEFAULT_UNIT_LABEL = "ekn.dev/deployment-unit"

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

DEFAULT_DELIVERY_MANAGERS = frozenset({DEFAULT_FIELD_MANAGER})
"""Field managers whose objects a prune may delete.

**Deliberately not the engine-manager set, and the difference is the whole
point.** That set decides what is *not* worth reporting as a foreign owner,
and `kube-controller-manager` belongs in it because it owns a field on
nearly everything. It must never gate a delete: the endpoints controller
*is* `kube-controller-manager`, so a prune that trusted that set would
delete every Endpoints object it could see.

Measured on nixlab2 (2026-09-17), read-only. Of 111 objects inside the
selector this module builds, **14 would have been deleted by a prune that
checked labels alone**, and none had ever been touched by `ekn` or by the
GitOps engine. They are controller-generated children that *inherit their
parent's labels*: the endpoints controller copies a Service's
`metadata.labels` onto its Endpoints and EndpointSlice, and cert-manager
copies a Certificate's onto its CertificateRequest.

Six of those fourteen carry no `ownerReferences` at all -- the legacy
endpoints controller sets none -- and one of the six is
`kube-system/coredns`. So this check is the load-bearing one and `_owner`
cannot stand in for it.

The number grows with what this mode does. Only 6 Services carried the
environment label then, because only a bootstrap unit had been applied by
`ekn`; a full apply stamps it on all 57, and every child inherits it.
"""

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


class KindNotServedError(ValueError):
    """The API server serves no such kind, so nothing can be applied to it.

    Raised by `discover`, which never reaches a PATCH: discovery answers
    200 and simply lacks the kind. A converging apply has to tell this from
    a real failure, because it is the ordinary state of a custom kind whose
    CustomResourceDefinition is in a later part of the same run.

    **A `ValueError` and not a `LookupError`.** `clusterdiff` already
    catches `ValueError` from `build_object` for precisely this case, and
    reports it per object rather than aborting the diff. A new base class
    would silently stop that working.
    """


async def discover(api: Api, kind: str, api_version: str) -> tuple[str, bool]:
    """The plural name and the namespaced-ness of one kind, from the API
    server's own discovery document.

    **Not `api.async_lookup_kind`.** That takes a `"Kind.group"` string, puts
    it through kr8s' `parse_kind` -- which lowercases it -- and then matches
    the result against each resource's plural, its Kind, its singular and its
    short names. A CustomResourceDefinition whose singular is not simply the
    Kind in lower case matches none of the four:
    `NetworkAttachmentDefinition` becomes `networkattachmentdefinition`, while
    the resource offers `network-attachment-definitions`,
    `NetworkAttachmentDefinition` and `network-attachment-definition`. The
    apply then dies with `ValueError: Kind networkattachmentdefinition not
    found`, seconds after waiting for that very CRD to become Established.

    Most CRDs name their singular as the lowercased Kind -- `prometheusrule`,
    `verticalpodautoscaler` -- which is why this went unseen for so long. A
    hyphenated singular is legal and common in the CNI ecosystem.

    A manifest carries the two fields that identify a resource exactly, so
    those are what this matches on, and nothing here changes their case.

    The second read is the other half. kr8s caches discovery for six hours,
    because kubectl does, and a CRD an earlier barrier of this same apply
    created is not in a cache filled before it existed. The uncached read only
    happens when the cached answer misses, so an apply that introduces no new
    kind still costs one discovery.
    """
    for fetch in (api.async_api_resources, api.async_api_resources_uncached):
        # kr8s.Api's discovery methods have no upstream return annotation.
        for resource in await fetch():  # pyright: ignore[reportUnknownVariableType] -- kr8s Api discovery methods are unannotated upstream
            if resource.get("kind") == kind and resource.get("version") == api_version:
                return resource["name"], resource["namespaced"]
    msg = (
        f"the API server serves no {kind} in {api_version}. "
        "A CustomResourceDefinition that establishes it has to be applied first."
    )
    raise KindNotServedError(msg)


async def build_object(spec: Manifest, api: Api) -> APIObject:
    """Turn a raw manifest dict into a kr8s APIObject, resolving plural/
    namespaced-ness for kinds kr8s doesn't have a builtin class for (i.e.
    almost every CRD) against the live API server's own discovery info,
    rather than guessing a plural by string mangling.
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
        plural, namespaced = await discover(api, kind, api_version)
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
    tier loop calls this per environment-labeled object it tracks for
    pruning, and `ekn.sops.ensure_age_identities`' cluster-bootstrap step
    calls it directly for its Namespace/Secret objects -- which deliberately
    skip environment labeling (they aren't part of any `apply_and_prune`
    generation and must never be pruned), so that decision stays with each
    caller rather than being baked in here.
    """
    obj = await build_object(spec, api)
    await ssa_apply(obj, field_manager=field_manager)
    return obj


def _object_key(obj: APIObject) -> tuple[str, str, str]:
    return (obj.namespace or "none", obj.kind, obj.name)


def with_environment_label(spec: Manifest, label: str, value: str) -> Manifest:
    """Stamp `ekn.dev/environment` onto a copy of `spec`.

    Public because both applies need it and both must stamp identically: a
    converging run and a barrier run have to leave the same prune scope, and
    an object stamped by only one of them is one the other would delete.
    """
    labeled = dict(spec)
    metadata_value = labeled.get("metadata") or {}
    metadata: Manifest = dict(metadata_value) if isinstance(metadata_value, dict) else {}
    labels_value = metadata.get("labels") or {}
    labels: dict[str, JsonValue] = dict(labels_value) if isinstance(labels_value, dict) else {}
    labels[label] = value
    metadata["labels"] = labels
    labeled["metadata"] = metadata
    return labeled


async def _wait_established(crd: APIObject, seconds: float) -> None:
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
    `TypeError` is swallowed, and only until the deadline, so a CRD that
    genuinely never establishes still fails rather than spinning.

    **`asyncio.timeout`, and not a `timeout=` argument.** This used to do the
    arithmetic itself -- a deadline, and what is left of it on each turn --
    and hand the remainder to `kr8s`' `wait`. One context manager bounds the
    whole loop instead, the sleeps between the retries as well as the watch
    inside them. It also keeps a float away from that `wait`, which annotates
    its own `timeout` as `int | None` although the `async_wait` under it takes
    `int | float | None`.

    *seconds*, and not *timeout*: the value is the argument of the context
    manager below, not a deadline this function passes on to something else.
    `ASYNC109` reads the name, and the name it warns about means the second
    thing.
    """
    try:
        async with asyncio.timeout(seconds):
            while True:
                try:
                    await crd.wait("condition=Established")
                except TypeError:
                    # No status yet. Let the controller get there rather than
                    # hammering the API server, then look again.
                    _log.debug("CRD has no status yet, retrying", name=crd.name)
                    await asyncio.sleep(0.5)
                    continue
                return
    except TimeoutError as exc:
        msg = f"CRD {crd.name} did not become Established within {seconds}s"
        raise TimeoutError(msg) from exc


async def apply_and_prune(  # noqa: PLR0913 -- tracked complexity/arg-count debt, see TODO.md
    objects: list[Manifest],
    *,
    api: Api,
    environment: str,
    unit: str | None = None,
    environment_label: str = DEFAULT_ENVIRONMENT_LABEL,
    unit_label: str = DEFAULT_UNIT_LABEL,
    resource_priority: dict[str, int] | None = None,
    field_manager: str = DEFAULT_FIELD_MANAGER,
    crd_establish_timeout: int = 60,
    prune: bool = True,
    prune_kinds: Mapping[str, str] | None = None,
    hand_applied: Collection[str] = (),
    declared_units: Collection[str] | None = None,
    protect: set[tuple[str, str, str]] | None = None,
) -> None:
    """Apply `objects` in barrier order, then (if `prune`) prune anything
    previously applied in the same scope that this run no longer generates.

    `environment` is stamped onto every applied object as `environment_label`.
    `unit` names the deployment unit this apply covers, or `None` for a
    whole-instance apply; it is never stamped, because easykubenix renders
    `unit_label` into the manifests themselves. Together they pick the prune
    scope -- see `_prune`.

    `hand_applied` names the units a whole-instance prune must not touch, and
    is ignored when `unit` is set. See `prune_selector`. `declared_units` is
    every unit the configuration declares, read only to warn -- see `_prune`.
    `None` means the caller has no registry to check against, so it says
    nothing rather than warning about every labelled object it prunes.

    `prune_kinds` maps kind to apiVersion, and each entry is scanned for
    pruning whether or not this apply touched that kind. `ekn kubeapply`
    passes `kubernetes.apiMappings`. Without it, pruning only scans kinds
    present in *this* apply, so removing the last object of some kind from
    the configuration leaves every stale object of that kind behind -- which
    is exactly the case a whole-instance `--prune` exists to answer.

    A kind the cluster does not serve is skipped, not an error: a mapping is
    what the configuration knows, not what this cluster has. It carries the
    apiVersion rather than the bare kind because `_prune` needs a *class*,
    and resolving a bare name goes through the `async_lookup_kind` path
    `discover` exists to avoid.

    Residual gap, and it needs API discovery to close: `apiMappings` is
    evaluated from the current configuration, and a removed Helm chart takes
    the mappings its CRDs taught with it (see `lib/importYaml.nix`). Built-in
    kinds are covered; the custom kinds of a wholly removed component are
    not.

    `protect` names `(namespace, kind, name)` identities that pruning must
    never delete, even though this apply did not produce them. Absence from
    the desired set normally means "removed from the configuration"; for
    these it means "this run could not safely produce it", which is not the
    same thing and must not be answered with a delete.

    The case it exists for is a seeded credential. `seeds.resolve` leaves an
    object out of the apply set when the live value cannot be read back --
    a non-UTF-8 value, or the key already gone -- because there is nothing to
    write back and applying the rendered object would overwrite the
    credential with the literal `$ekn:env:VARNAME`. Without this, the very
    next `--prune` deletes a credential nobody can recreate: the variable was
    exported once, at bootstrap, and is long gone from the environment.

    `prune=False` (the default for `ekn kubeapply` against a real cluster,
    e.g. a narrow `--target` slice) avoids pruning objects that are simply
    outside the current apply's scope -- the same "two controllers fighting
    over pruning" concern kluctl.nix's `excludeGitopsTargets` documents.
    """
    resource_priority = resource_priority or {}
    protected = protect or set()
    if unit is not None:
        _check_unit_labels(objects, unit, unit_label)
    # Every object this apply produced, keyed as the prune scan keys them, and
    # carrying the class it was applied through. See `prune_generation`.
    desired: dict[tuple[str, str, str], type[APIObject]] = {}

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
            labeled = with_environment_label(spec, environment_label, environment)
            obj = await apply_one(labeled, api, field_manager=field_manager)
            applied.append(obj)
            desired[_object_key(obj)] = type(obj)
            _log.debug("applied", kind=obj.kind, namespace=obj.namespace, name=obj.name)

        crds = [obj for obj in applied if obj.kind == "CustomResourceDefinition"]
        if crds:
            _log.info("waiting for CRDs to become Established", count=len(crds), timeout=crd_establish_timeout)
        for crd in crds:
            await _wait_established(crd, crd_establish_timeout)

    if not prune:
        return

    await prune_generation(
        api,
        desired=desired,
        environment=environment,
        unit=unit,
        hand_applied=hand_applied,
        declared_units=declared_units,
        prune_kinds=prune_kinds,
        protect=protected,
        environment_label=environment_label,
        unit_label=unit_label,
        # This apply's own manager as well as the default. A unit applies as
        # the controller that takes its objects over, so a prune that only
        # knew `ekn` would refuse to delete anything that unit ever applied.
        delivery_managers={*DEFAULT_DELIVERY_MANAGERS, field_manager},
    )


async def prune_generation(  # noqa: PLR0913 -- every argument names part of a delete scope, and a dict would hide them
    api: Api,
    *,
    desired: Mapping[tuple[str, str, str], type[APIObject]],
    environment: str,
    unit: str | None = None,
    hand_applied: Collection[str] = (),
    declared_units: Collection[str] | None = None,
    prune_kinds: Mapping[str, str] | None = None,
    protect: Collection[tuple[str, str, str]] = (),
    environment_label: str = DEFAULT_ENVIRONMENT_LABEL,
    unit_label: str = DEFAULT_UNIT_LABEL,
    delivery_managers: Collection[str] = DEFAULT_DELIVERY_MANAGERS,
) -> None:
    """Delete what this environment holds and this generation does not.

    `desired` maps every object the generation produced to the class it was
    applied through. **Built from the applied objects, never from the raw
    manifests**: a namespaced manifest that names no namespace resolves to
    the API's default namespace, so a key taken from the manifest would read
    `none` where the scan reads `default`, and the object would be pruned on
    the next run.

    Separate from `apply_and_prune` because two applies now share it -- the
    barrier one above, and the converging one in `directapply` -- and the
    scope must not be able to drift between them.
    """
    classes = {key[1]: cls for key, cls in desired.items()}
    classes |= await _extra_classes(api, prune_kinds or {}, already=set(classes))
    await _prune(
        api=api,
        selector=prune_selector(
            environment=environment,
            unit=unit,
            hand_applied=hand_applied,
            environment_label=environment_label,
            unit_label=unit_label,
        ),
        scan_kinds=set(classes),
        classes=classes,
        desired_keys=set(desired),
        protected=set(protect),
        declared_units=declared_units,
        unit_label=unit_label,
        delivery_managers=delivery_managers,
    )


def _check_unit_labels(objects: list[Manifest], unit: str, unit_label: str) -> None:
    """Refuse an apply whose objects do not all belong to the unit it claims.

    A `--target <name>` apply prunes by `unit_label=<name>`, so an object in
    its set that carries a different value -- or none -- is applied into a
    scope its own prune will never look at. Absent the label it is worse than
    orphaned: a whole-instance `--prune` excludes the hand-applied units by
    their label value, and `notin` matches an unlabelled object, so it takes
    the object as its own and deletes it.

    Nothing here should be reachable: easykubenix renders the label onto every
    object in a unit, and `eval._raw_manifest_in_unit` adds it to the one kind
    of object Nix never sees. This is the check that says so, and it fails
    with the object named rather than with a deletion two applies later.
    """
    wrong: list[str] = []
    for spec in objects:
        metadata = spec.get("metadata")
        labels = metadata.get("labels") if isinstance(metadata, dict) else None
        found = labels.get(unit_label) if isinstance(labels, dict) else None
        if found != unit:
            name = metadata.get("name", "<unnamed>") if isinstance(metadata, dict) else "<unnamed>"
            wrong.append(f"  {spec.get('kind', '<unknown>')}/{name}: {found!r}")
    if wrong:
        listed = "\n".join(wrong)
        msg = (
            f"these objects do not carry {unit_label}={unit!r}, which is the scope this apply prunes by:\n"
            f"{listed}\n"
            f"An object applied into a unit without the unit's label is deleted by the next "
            f"whole-instance prune, whose selector excludes hand-applied units by label value "
            f"and therefore matches an object carrying no unit label at all."
        )
        raise ValueError(msg)


def prune_selector(
    *,
    environment: str,
    unit: str | None,
    hand_applied: Collection[str] = (),
    environment_label: str = DEFAULT_ENVIRONMENT_LABEL,
    unit_label: str = DEFAULT_UNIT_LABEL,
) -> str:
    """The label selector naming everything one apply is allowed to prune.

    Two scopes, and the difference between them is the deployment-unit label:

    - A `--target X` apply owns this environment's objects in that unit:
      `ekn.dev/environment=E,ekn.dev/deployment-unit=X`.
    - A whole-instance apply (`unit is None`) owns this environment's objects
      except those of the units in `hand_applied`:
      `ekn.dev/environment=E,ekn.dev/deployment-unit notin (bootstrap,cni)`.

    `hand_applied` names the units whose objects never reach
    `kubernetes.generated` -- each renders a whole nested instance, and only
    `ekn kubeapply --target <name>` applies it. They carry the environment
    label, because `ekn` applied them, so a whole-instance prune lists them,
    finds them absent from its own desired set, and deletes them. That is
    ArgoCD and the CNI. The caller builds the set by *inverting* the
    discriminator at easykubenix/kubernetes.nix:1249 -- every declared unit
    except the routing-only ones -- so a unit of a class nobody has written
    yet is excluded rather than pruned.

    **`notin` also matches an object that carries no unit label at all**, so
    this one clause replaces the `!ekn.dev/deployment-unit` it used to be and
    loses nothing. Measured on a live cluster (2026-09-17) rather than taken
    from the documentation, with a control object carrying no unit label,
    because the natural counts on that cluster are identical under both
    behaviours -- it has no unlabelled objects -- and a `notin` that did not
    subsume would silently narrow the scope to nothing while looking healthy.

    An empty `hand_applied` emits **no unit clause at all**, rather than an
    empty `notin ()`, which the API server rejects: `labels.NewRequirement`
    refuses `in`/`notin` with an empty value set. That is the ordinary shape
    for a config with no nested units, which is what `ekn validate` applies.

    Returned as a string rather than a dict because neither a set-based clause
    nor a not-exists clause has a dict form. `kr8s` passes a string selector
    through verbatim as `labelSelector`.
    """
    if unit is not None:
        return f"{environment_label}={environment},{unit_label}={unit}"
    if not hand_applied:
        return f"{environment_label}={environment}"
    excluded = ",".join(sorted(hand_applied))
    return f"{environment_label}={environment},{unit_label} notin ({excluded})"


async def _extra_classes(
    api: Api,
    prune_kinds: Mapping[str, str],
    *,
    already: set[str],
) -> dict[str, type[APIObject]]:
    """Resolve each `prune_kinds` entry this apply did not already touch.

    A kind the cluster does not serve is skipped. `apiMappings` is what the
    configuration knows; a cluster that never had the CRD is the ordinary
    case, not a reason to abandon a prune whose apply half already ran.
    """
    resolved: dict[str, type[APIObject]] = {}
    for kind, api_version in prune_kinds.items():
        if kind in already:
            continue
        try:
            resolved[kind] = type(await build_object({"kind": kind, "apiVersion": api_version}, api))
        except KindNotServedError:
            _log.debug("not scanning unserved kind", kind=kind, api_version=api_version)
    return resolved


def _delivered_by(obj: APIObject, delivery_managers: Collection[str]) -> bool:
    """True when something that delivers configuration applied this object.

    An object no delivery manager has ever touched was not put there by us,
    whatever labels it carries, so removing it from the configuration cannot
    be what its presence means.
    """
    metadata = obj.raw.get("metadata")
    entries = metadata.get("managedFields") if isinstance(metadata, dict) else None
    if not isinstance(entries, list):
        return False
    return any(isinstance(entry, dict) and entry.get("manager") in delivery_managers for entry in entries)


def _unit_of(obj: APIObject, unit_label: str) -> str | None:
    metadata = obj.raw.get("metadata")
    labels = metadata.get("labels") if isinstance(metadata, dict) else None
    unit = labels.get(unit_label) if isinstance(labels, dict) else None
    return unit if isinstance(unit, str) else None


def _owner(obj: APIObject) -> str | None:
    """The controller that owns `obj`, if any -- and therefore why not to
    delete it.

    An object with `ownerReferences` was created by something that is still
    running, so deleting it is either churn (the owner recreates it) or a
    loss. The case that forces the rule is an External Secrets Operator
    Secret: on nixlab2 all eight carry `ownerReferences: [ExternalSecret]`
    and hold the only copy of every Harbor and oauth2-proxy credential.

    A hard rule and not a flag. Those eight stay out of prune scope today
    only because ESO does not copy `ekn.dev/environment` into what it
    materialises -- someone else's template, which can start copying it at
    any time. This guard does not depend on that one holding.

    **Kept as defence in depth, not because it was shown to be necessary.**
    Simulated against every object in scope on nixlab2 (2026-09-17), this
    check alone left 7 of 15 candidates and `_delivered_by` alone left 1 --
    every object this one catches, that one catches too. The case it would
    cover on its own is an object the engine applied, and so carries a
    delivery manager, that a controller later adopted with an
    ownerReference. Plausible; that cluster does not contain one.
    """
    metadata = obj.raw.get("metadata")
    refs = metadata.get("ownerReferences") if isinstance(metadata, dict) else None
    if not isinstance(refs, list) or not refs:
        return None
    first = refs[0]
    if not isinstance(first, dict):
        return "<unknown>"
    return f"{first.get('kind', '<unknown>')}/{first.get('name', '<unnamed>')}"


_NO_LIST_VERB = frozenset({404, 405})
"""What the API server answers for a served kind that cannot be listed.

Seven of them on a stock cluster, all reached through `apiMappings`:
`Binding` and `LocalSubjectAccessReview` answer `404 NotFound`, and
`SelfSubjectAccessReview`, `SelfSubjectReview`, `SelfSubjectRulesReview`,
`SubjectAccessReview` and `TokenReview` answer `405 MethodNotAllowed`.

`KindNotServedError` does not cover these: discovery resolves every one of
them, so `_extra_classes` builds a class happily and the failure only
arrives at the LIST. Measured on nixlab2, 2026-09-17.
"""


async def _list_for_prune(
    api: Api,
    target: str | type[APIObject],
    *,
    selector: str,
    kind: str,
) -> list[APIObject]:
    """Everything of one kind inside the prune scope.

    Wraps only the listing, never the deletes that follow it: a kind that
    cannot be listed is ordinary, and a delete that fails is not.
    """
    try:
        # kr8s.Api.async_get's `label_selector`/`field_selector` params and its
        # `APIObject | dict` yield type are both bare-`dict`/unannotated
        # upstream, so pyright can't resolve the member or the loop variable.
        return [
            obj
            async for obj in api.async_get(  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType] -- kr8s Api.async_get's selector params and yield type are unannotated upstream
                target,
                namespace=kr8s.ALL,
                label_selector=selector,
            )
            if isinstance(obj, APIObject)
        ]
    except kr8s.ServerError as exc:
        response = exc.response
        if response is not None and response.status_code in _NO_LIST_VERB:
            _log.debug("not scanning kind with no list verb", kind=kind, status=response.status_code)
            return []
        raise


async def _prune(  # noqa: PLR0913 -- one caller, and every argument is state that caller built
    *,
    api: Api,
    selector: str,
    scan_kinds: set[str],
    classes: dict[str, type[APIObject]],
    desired_keys: set[tuple[str, str, str]],
    protected: set[tuple[str, str, str]],
    declared_units: Collection[str] | None = None,
    unit_label: str = DEFAULT_UNIT_LABEL,
    delivery_managers: Collection[str] = DEFAULT_DELIVERY_MANAGERS,
) -> None:
    """Delete objects in this run's prune scope that it did not produce.

    Split out of `apply_and_prune` to keep that function under the complexity
    limit rather than growing its `noqa`. The apply half and the prune half
    share only the values passed here. See `prune_selector` for the scope.

    `declared_units` is every unit the configuration declares, and is only
    read to warn. Deleting an object of a unit the configuration no longer
    names is correct -- it is what removing a unit means -- but it is also
    one line of config away from deleting ArgoCD and the CNI, and before the
    set-based selector the not-exists clause hid that case entirely. `None`
    means the caller holds no such registry, and warns about nothing.
    """
    _log.info("pruning", kinds=len(scan_kinds), selector=selector)
    for kind in scan_kinds:
        # The class, when this apply built one. `async_get` takes either, and
        # a name sends it through `async_lookup_kind` -- the lowercasing lookup
        # `discover` above exists to avoid, which fails outright on a CRD whose
        # singular is not the lowercased Kind, and which otherwise hands back a
        # `"singular.group/version"` string that `new_class` mis-splits so that
        # every listed object reports a lowercase `.kind`.
        #
        # `_extra_classes` resolves the `prune_kinds` entries through the same
        # path, so in practice every kind here has one. The string fallback
        # stays as the safe default for a caller that built `classes` itself.
        target: str | type[APIObject] = classes.get(kind, kind)
        for obj in await _list_for_prune(api, target, selector=selector, kind=kind):
            # Not `_object_key(obj)`. Passing the class above keeps `.kind`
            # right for every kind this apply touched, but a `prune_kinds`
            # name still goes through `async_lookup_kind`, which reassigns its
            # `kind` param to a `"singular.group/version"` string that
            # `new_class` mis-splits on the first "." -- the listed object's
            # `.kind` ends up as the lowercase singular name (e.g.
            # "verticalpodautoscaler"), not the PascalCase Kind (e.g.
            # "VerticalPodAutoscaler") `desired_keys` was built from
            # while applying. Use the loop's own `kind` (identical to what
            # `_object_key` used at apply time) instead of trusting the
            # listed object's mangled one -- otherwise every CRD-based
            # object's key mismatches and everything gets "pruned".
            key = (obj.namespace or "none", kind, obj.name)
            if key in desired_keys:
                continue
            if key in protected:
                # Absent from `desired_keys` and still not ours to delete.
                # See `protect` on `apply_and_prune`.
                _log.info("keeping protected object", kind=kind, namespace=obj.namespace, name=obj.name)
                continue
            owner = _owner(obj)
            if owner is not None:
                _log.info("keeping owned object", kind=kind, namespace=obj.namespace, name=obj.name, owner=owner)
                continue
            if not _delivered_by(obj, delivery_managers):
                # Carries our labels and was never applied by us: a
                # controller-generated child that inherited them from its
                # parent. See DEFAULT_DELIVERY_MANAGERS.
                _log.info("keeping object we never applied", kind=kind, namespace=obj.namespace, name=obj.name)
                continue
            unit = _unit_of(obj, unit_label)
            if declared_units is not None and unit is not None and unit not in declared_units:
                _log.warning(
                    "pruning an object of a unit this configuration no longer declares",
                    kind=kind,
                    namespace=obj.namespace,
                    name=obj.name,
                    unit=unit,
                )
            _log.info("pruning", kind=kind, namespace=obj.namespace, name=obj.name)
            await obj.delete()


__all__ = [
    "DEFAULT_BARRIER_PRIORITY",
    "DEFAULT_DELIVERY_MANAGERS",
    "DEFAULT_ENVIRONMENT_LABEL",
    "DEFAULT_FIELD_MANAGER",
    "DEFAULT_UNIT_LABEL",
    "Manifest",
    "apply_and_prune",
    "apply_one",
    "barriers",
    "build_object",
    "discover",
    "prune_generation",
    "prune_selector",
    "ssa_apply",
    "with_environment_label",
]
