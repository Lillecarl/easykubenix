"""What the cluster already holds, and what follows from it.

One LIST per kind, asking for object metadata only, answers three questions
that a direct apply needs and that nothing else can answer cheaply:

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

This module holds the decisions made from that data. The sweep itself
belongs to the caller, which owns the `Api`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from .converge import object_key

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from .apply import Manifest

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
    live: Mapping[tuple[str, str, str], LiveObject],
    *,
    environment: str,
    assume_unchanged: bool,
) -> bool:
    """True when this object may be left alone.

    Three conditions, and the third is the one that is easy to leave out:

    1. the operator asked for it;
    2. the live hash equals the rendered one;
    3. **the live object already carries `ekn.dev/environment=<env>`.**

    Without the third, a fast run skips objects a GitOps engine applied and
    `ekn` never stamped -- and a later `--prune` selects by exactly that
    label, so it deletes objects that are present and correct. The condition
    also makes the first converge honest: it applies every such object once,
    stamps it, and every converge after that skips it.

    This is why fast mode and `--prune` are not in conflict. An earlier
    reading of this design had them mutually exclusive; the label condition
    is exact where that was merely conservative.
    """
    if not assume_unchanged:
        return False
    wanted = desired_hash(spec)
    if wanted is None:
        return False
    seen = live.get(object_key(spec))
    if seen is None or seen.manifest_hash != wanted:
        return False
    return seen.environment == environment


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
    """
    ignored = {*ours, *engine_managers}
    owners: list[ForeignOwner] = []
    for obj in live:
        foreign = sorted(manager for manager in obj.managers if manager not in ignored)
        if foreign:
            owners.append(ForeignOwner(obj.key, tuple(foreign)))
    return owners


__all__ = [
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
]
