from __future__ import annotations

import copy
import difflib
import os
from http import HTTPStatus
from typing import TYPE_CHECKING, Any

import kr8s
import yaml

from ekn import seeds
from ekn.apply import build_object, ssa_apply

if TYPE_CHECKING:
    from kr8s._api import Api  # kr8s.asyncio.api() returns this, not kr8s.Api
    from nanopynix.models import JsonValue

    from ekn.apply import Manifest

# Fields the API server/controllers own, not us -- present on every live
# object and every dry-run-applied result regardless of whether the spec
# actually changed. Diffing these would just be noise on top of the real
# question ("what would this apply change").
_NOISY_METADATA_KEYS = (
    "managedFields",
    "resourceVersion",
    "generation",
    "uid",
    "creationTimestamp",
    "selfLink",
)


#: What a seeded field's value is replaced by in diff output.
_REDACTED = "<redacted: seeded credential>"


def _redact(obj: Manifest, fields: set[str]) -> Manifest:
    """Blank every seeded field, in both `stringData` and `data`.

    It is the *predicted* side that leaks: a live Secret comes back
    base64-encoded in `data`, but the predicted object holds the substituted
    plaintext, and it would be rendered straight into the diff. Both sides
    are redacted anyway -- base64 is not concealment, and a diff that shows
    one side is still a diff that shows the secret changed to something
    printable.
    """
    if not fields:
        return obj
    result = dict(obj)
    for section in ("stringData", "data"):
        values = result.get(section)
        if isinstance(values, dict):
            result[section] = {key: (_REDACTED if key in fields else value) for key, value in values.items()}
    return result


def _normalize(raw: Any) -> Manifest:
    # `APIObject.raw` is a python-box `Box`, not a plain dict -- PyYAML
    # doesn't know how to render it and falls back to a `!!python/object`
    # tag. `to_dict()` (a no-op for already-plain dicts, e.g. a dry-run
    # apply's response) recursively unwraps it first.
    to_dict = getattr(raw, "to_dict", None)
    obj: Manifest = to_dict() if to_dict is not None else raw
    obj = copy.deepcopy(obj)
    obj.pop("status", None)
    metadata = obj.get("metadata")
    if isinstance(metadata, dict):
        for key in _NOISY_METADATA_KEYS:
            metadata.pop(key, None)
        annotations = metadata.get("annotations")
        if isinstance(annotations, dict):
            annotations.pop("kubectl.kubernetes.io/last-applied-configuration", None)
    return obj


def _dump(raw: Any) -> str:
    return yaml.dump(_normalize(raw), default_flow_style=False, sort_keys=True)


def _object_label(spec: Manifest) -> str:
    metadata_value: JsonValue = spec.get("metadata") or {}
    metadata: Manifest = metadata_value if isinstance(metadata_value, dict) else {}
    namespace = metadata.get("namespace", "none")
    kind = spec.get("kind", "?")
    name = metadata.get("name", "?")
    return f"{namespace}/{kind}/{name}"


def _is_missing_namespace_error(exc: kr8s.ServerError) -> bool:
    """True for the specific 404 a dry-run apply gets when the object's own
    *namespace* doesn't exist yet -- e.g. a fresh bootstrap target where the
    Namespace object is itself one of the objects being diffed/applied.
    Distinct from the object itself being 404 (kr8s.NotFoundError, from
    async_refresh) -- this one comes from the raw dry-run PATCH, which
    doesn't get that translation.
    """
    response = exc.response
    if response is None or response.status_code != HTTPStatus.NOT_FOUND:
        return False
    try:
        body: JsonValue = response.json()
    except ValueError:
        return False
    if not isinstance(body, dict):
        return False
    details = body.get("details")
    return isinstance(details, dict) and details.get("kind") == "namespaces"


async def cluster_diff(
    objects: list[Manifest],
    *,
    api: Api,
    field_manager: str = "ekn",
) -> str:
    """Diff `objects` against the live cluster: for each, compute what a
    real `ssa_apply` would produce (via a server-side-apply dry run) and
    unified-diff it against the object's current live state (empty if it
    doesn't exist yet). Doesn't apply, prune, or wait on anything -- purely
    a preview of what `ekn kubeapply`/`ekn validate` would actually change.
    """
    chunks: list[str] = []
    for spec in sorted(objects, key=_object_label):
        label = _object_label(spec)

        # A seeded credential follows the same rule the apply does, keyed on
        # the variable rather than on the object.
        #
        # Unset is the steady state, and an apply would neither update nor
        # remove it, so there is nothing to report. Without this the rendered
        # side holds `$ekn:env:VARNAME` while the live side holds the real
        # credential, and the two can never converge: a permanent false
        # positive, on exactly the objects where that is worst.
        #
        # Set means an apply *would* act, so the diff should say so.
        variables = seeds.annotated_variables(spec)
        seeded_fields: set[str] = set()
        if variables:
            if not any(seeds.is_supplied(variable) for variable in variables):
                chunks.append(f"# {label}: seeded, not compared ({', '.join(variables)} not set)\n")
                continue
            seeded_fields = {reference.path[-1] for reference in seeds.references(spec) if reference.path}
            # Rebinding `spec`: the substituted object is exactly what an
            # apply would send, so that is what the diff must compare.
            spec = seeds.substitute(spec, {v: os.environ[v] for v in variables if seeds.is_supplied(v)})

        # A kind whose CRD hasn't landed on this cluster yet (e.g. a fresh
        # bootstrap target that hasn't been applied yet) can't be resolved
        # to a plural/namespaced REST endpoint at all -- kr8s's own
        # discovery raises ValueError for it, distinct from the object
        # itself just not existing (kr8s.NotFoundError, handled below).
        # Surface this per-object instead of aborting the whole diff.
        try:
            live_obj = await build_object(spec, api)
        except ValueError as exc:
            chunks.append(
                f"# {label}: cannot diff -- {exc} (kind not yet registered on "
                "this cluster; its CRD would need to apply first)\n",
            )
            continue

        try:
            await live_obj.async_refresh()
            live_yaml = _dump(_redact(_normalize(live_obj.raw), seeded_fields))
        except kr8s.NotFoundError:
            live_yaml = ""

        desired_obj = await build_object(spec, api)
        try:
            predicted = await ssa_apply(desired_obj, field_manager=field_manager, dry_run=True)
        except kr8s.ServerError as exc:
            if not _is_missing_namespace_error(exc):
                raise
            # The server can't compute a merged/defaulted result against a
            # namespace that doesn't exist yet -- fall back to the desired
            # spec itself so this still reads as "would be newly created"
            # (matching the plain new-object case) instead of aborting.
            predicted = spec
        predicted_yaml = _dump(_redact(_normalize(predicted), seeded_fields))

        if live_yaml == predicted_yaml:
            continue

        diff = difflib.unified_diff(
            live_yaml.splitlines(keepends=True),
            predicted_yaml.splitlines(keepends=True),
            fromfile=f"live/{label}",
            tofile=f"predicted/{label}",
        )
        chunks.append("".join(diff))

    return "".join(chunks)


__all__ = ["cluster_diff"]
