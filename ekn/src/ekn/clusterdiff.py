from __future__ import annotations

import copy
import difflib
from typing import TYPE_CHECKING, Any

import kr8s
import yaml

from ekn.apply import _build_object, ssa_apply

if TYPE_CHECKING:
    from kr8s._api import Api  # kr8s.asyncio.api() returns this, not kr8s.Api

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


def _normalize(raw: Any) -> dict[str, Any]:
    # `APIObject.raw` is a python-box `Box`, not a plain dict -- PyYAML
    # doesn't know how to render it and falls back to a `!!python/object`
    # tag. `to_dict()` (a no-op for already-plain dicts, e.g. a dry-run
    # apply's response) recursively unwraps it first.
    to_dict = getattr(raw, "to_dict", None)
    obj: dict[str, Any] = to_dict() if to_dict is not None else raw
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


def _object_label(spec: dict[str, Any]) -> str:
    metadata = spec.get("metadata") or {}
    namespace = metadata.get("namespace", "none")
    kind = spec.get("kind", "?")
    name = metadata.get("name", "?")
    return f"{namespace}/{kind}/{name}"


async def cluster_diff(
    objects: list[dict[str, Any]],
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

        live_obj = await _build_object(spec, api)
        try:
            await live_obj.async_refresh()
            live_yaml = _dump(live_obj.raw)
        except kr8s.NotFoundError:
            live_yaml = ""

        desired_obj = await _build_object(spec, api)
        predicted = await ssa_apply(desired_obj, field_manager=field_manager, dry_run=True)
        predicted_yaml = _dump(predicted)

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
